"""nv-coworker-compose — render Hermes coworker-type profile distributions from a
coworker-types.yaml lego spec and onboard them into a Bot-Mode gateway.

The onboarding functions and the gateway transport they use are defined together
in this package module so the transport is a single swappable surface: every
gateway effect (profiles.create / profiles.configure / session.* / groups.*)
flows through the one ``_dispatch_rpc`` seam, and the read-only liveness check
through ``_gateway_preflight`` → ``_ws_probe``. That keeps the coupling to
``tui_gateway.server`` (an internal callable, not documented PluginContext API)
confined to a single function that can be replaced if that surface moves.

``_dispatch_rpc`` chooses the transport: in-process gateway-tool execution calls
``tui_gateway.server.handle_request`` directly; the standalone CLI path (an
explicit ``--gateway-url`` carrying a ?token/?ticket credential) routes to an
authenticated WebSocket bound once, after a read-only liveness preflight, and
reused for the whole onboarding so a single-use ticket is not spent twice.
"""

from __future__ import annotations

import asyncio
import contextvars
import itertools
import json
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from hermes_cli.profiles import get_active_profile_name

from .compose import CompositionError, compose, load_spec
from .tools import CREATE_AGENT_SCHEMA, ONBOARD_COWORKER_SCHEMA, ONBOARD_PROJECT_SCHEMA

logger = logging.getLogger(__name__)

# Canonical Bot Chat session title (tools/bot_mode_probe.py:41).
BOT_CHAT_TITLE = "Bot Chat"

_TOOLSET = "coworker_admin"
_CLONE_PREFIX = "nv_onboard_clone_"

# tui_gateway hosted-room error code for an absent room (tui_gateway/methods_groups.py:559).
_ROOM_NOT_FOUND_CODE = 4114

_rpc_ids = itertools.count(1)

# Bound {"url", "requester"} while the standalone CLI path is active; None for
# in-process gateway-tool/slash execution.
_standalone_ctx: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "nv_coworker_standalone_ctx", default=None
)

# A WebSocket requester opened by the preflight probe, handed to the onboarding
# call so the authenticated connection (and any single-use ticket) is reused.
_pending_ws_requester: Optional[Callable[[Dict[str, Any]], Any]] = None


class RpcError(Exception):
    """A JSON-RPC error returned by the gateway; carries the structured code."""

    def __init__(self, error: Any):
        self.code: Optional[int] = None
        self.data: Any = None
        message = str(error)
        if isinstance(error, dict):
            self.code = error.get("code")
            self.data = error.get("data")
            message = str(error.get("message") or error)
        super().__init__(message)


# ---------------------------------------------------------------------------
# Gateway URL credential form + read-only liveness preflight
# ---------------------------------------------------------------------------

def validate_gateway_url(url: str) -> Tuple[bool, str]:
    """Local credential-form check, before any transport.

    Rejects a ``?internal`` URL (the process-lifetime credential reserved for
    WS clients the server spawns itself) and a URL with no ``?token``/``?ticket``
    credential; accepts a well-formed loopback token/ticket URL.
    """
    try:
        parsed = urlparse(url or "")
    except (ValueError, TypeError):
        return (False, "invalid_url")
    query = parse_qs(parsed.query)
    if "internal" in query:
        return (False, "forbidden_internal")
    if "token" in query or "ticket" in query:
        return (True, "ok")
    return (False, "no_credential")


def _ws_connect(url: str):
    """Open a raw WebSocket connection (lazy import); replaceable in tests."""
    from websockets.sync.client import connect  # pragma: no cover - runtime-only path

    return connect(url, open_timeout=5)


def _drain_ready(conn) -> bool:
    """Consume the unsolicited ``gateway.ready`` event sent on connection accept
    (tui_gateway/ws.py:369-374). Returns False when the first frame reports a
    JSON-RPC error instead (a rejected credential)."""
    raw = conn.recv(timeout=10)
    frame = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    return not (isinstance(frame, dict) and frame.get("error"))


def _open_ws_requester(url: str) -> Callable[[Dict[str, Any]], Any]:
    """Open an authenticated WebSocket, consume ``gateway.ready``, and return a
    request->envelope callable that correlates responses by JSON-RPC ``id``
    (ignoring unsolicited event frames). Secondary (standalone-CLI) transport."""
    conn = _ws_connect(url)
    _drain_ready(conn)

    def _request(request: Dict[str, Any]) -> Any:
        conn.send(json.dumps(request))
        while True:
            raw = conn.recv(timeout=30)
            frame = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            if isinstance(frame, dict) and frame.get("id") == request.get("id"):
                return frame
            # A frame without the matching id is an unsolicited event/broadcast.

    _request._conn = conn  # type: ignore[attr-defined]
    return _request


def _ws_probe(url: str) -> Dict[str, Any]:
    """Read-only liveness probe: open the connection, consume ``gateway.ready``,
    and retain the requester for the onboarding call so the socket (and its
    single-use ticket) is not spent twice. Raises ``ConnectionError``/``OSError``
    when the gateway is unreachable; ``{"authed": False}`` on a rejected
    credential."""
    global _pending_ws_requester
    conn = _ws_connect(url)
    try:
        authed = _drain_ready(conn)
    except (ConnectionError, OSError):
        raise
    except Exception as exc:
        raise ConnectionError(str(exc)) from exc
    if not authed:
        try:
            conn.close()
        except Exception:
            logger.debug("probe close failed", exc_info=True)
        return {"authed": False, "reason": "auth_rejected"}

    def _request(request: Dict[str, Any]) -> Any:
        conn.send(json.dumps(request))
        while True:
            raw = conn.recv(timeout=30)
            frame = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            if isinstance(frame, dict) and frame.get("id") == request.get("id"):
                return frame

    _request._conn = conn  # type: ignore[attr-defined]
    _pending_ws_requester = _request
    return {"authed": True}


def _gateway_preflight(url: str) -> Tuple[bool, str]:
    """Refuse before any mutation: credential-form check, then a liveness probe."""
    ok, reason = validate_gateway_url(url)
    if not ok:
        return (False, reason)
    try:
        resp = _ws_probe(url)
    except (ConnectionError, OSError):
        return (False, "connection_refused")
    if not isinstance(resp, dict) or not resp.get("authed"):
        return (False, "auth_rejected")
    return (True, "ok")


# ---------------------------------------------------------------------------
# RPC transport — one seam, two transports, wire adaptation
# ---------------------------------------------------------------------------

def _unwrap_rpc_envelope(resp: Any) -> Any:
    """Normalize a JSON-RPC response: raise on error, return the result body."""
    if resp is None:
        return None
    if isinstance(resp, dict):
        if resp.get("error"):
            raise RpcError(resp["error"])
        if "result" in resp:
            return resp["result"]
    return resp


def _room_exists(resp: Any) -> bool:
    """True when a groups.state response reports an existing room. Tolerates the
    absent-shape ``{"exists": False}`` and the present-shape ``{"room": {...}}``."""
    if not isinstance(resp, dict):
        return False
    return bool(resp.get("exists")) or isinstance(resp.get("room"), dict)


def _member_object(member: Any) -> Dict[str, Any]:
    """Adapt a member name to the object hosted rooms require (each member must be
    an object — gateway/hosted_rooms.py:343-344)."""
    if isinstance(member, dict):
        return member
    name = str(member)
    return {"member_id": name, "profile": name, "handle": name}


def _gateway_handle_request(request: Dict[str, Any]) -> Any:
    """In-process dispatch to the running gateway (lazy import); replaceable in tests."""
    from tui_gateway import server  # pragma: no cover - runtime-only path

    return server.handle_request(request)


def _dispatch_rpc(method: str, params: Dict[str, Any]) -> Any:
    """Dispatch one JSON-RPC method and return its normalized result body.

    Routes to the standalone authenticated WS requester when the standalone
    context is bound, otherwise to the in-process gateway handler. Adapts the
    wire shape the real gateway expects — ``groups.create`` members become
    member objects, and an absent-room ``groups.state`` (error 4114) is
    normalized to ``{"exists": False}`` rather than raised — while the logical
    call sites keep working with profile-name strings.
    """
    wire = dict(params)
    if method == "groups.create" and isinstance(wire.get("members"), list):
        wire["members"] = [_member_object(m) for m in wire["members"]]

    request = {"jsonrpc": "2.0", "id": next(_rpc_ids), "method": method, "params": wire}
    ctx = _standalone_ctx.get()
    if ctx is not None:
        if ctx.get("requester") is None:
            ctx["requester"] = _open_ws_requester(ctx["url"])
        envelope = ctx["requester"](request)
    else:
        envelope = _gateway_handle_request(request)

    try:
        return _unwrap_rpc_envelope(envelope)
    except RpcError as exc:
        if method == "groups.state" and exc.code == _ROOM_NOT_FOUND_CODE:
            return {"exists": False}
        raise


def _close_standalone(token) -> None:
    ctx = _standalone_ctx.get()
    if ctx is not None:
        requester = ctx.get("requester")
        conn = getattr(requester, "_conn", None)
        if conn is not None:
            try:  # pragma: no cover - runtime-only path
                conn.close()
            except Exception:  # pragma: no cover
                logger.debug("standalone WS close failed", exc_info=True)
    _standalone_ctx.reset(token)


# ---------------------------------------------------------------------------
# Project onboarding
# ---------------------------------------------------------------------------

def _looks_like_url(source: str) -> bool:
    s = (source or "").strip()
    return s.startswith(("http://", "https://", "git@", "ssh://", "git://")) or s.endswith(".git")


def _clone_repo(url: str) -> str:
    """Shallow-clone a git URL into a temp dir; return its path (lazy import)."""
    import subprocess  # pragma: no cover - runtime-only path

    dest = tempfile.mkdtemp(prefix=_CLONE_PREFIX)
    try:  # pragma: no cover - runtime-only path
        subprocess.run(
            ["git", "clone", "--depth", "1", url, dest],
            check=True, capture_output=True, stdin=subprocess.DEVNULL,
        )
    except Exception as exc:  # pragma: no cover
        shutil.rmtree(dest, ignore_errors=True)
        raise RuntimeError(f"clone failed for {url}: {exc}") from exc
    return dest


def _is_binary(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            chunk = fh.read(4096)
    except OSError:
        return True
    if b"\x00" in chunk:
        return True
    try:
        chunk.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _scan_source_files(root: Path, max_files: int) -> List[Tuple[str, str]]:
    """Return up to ``max_files`` (relpath, text) eligible source files, filtering
    symlinks, VCS/dependency dirs, and binaries FIRST, then sorting, then taking
    the bound — so a binary never consumes a slot."""
    skip_dirs = {".git", ".hg", ".svn", "node_modules", "__pycache__"}
    candidates: List[Tuple[str, Path]] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root)
        if any(part in skip_dirs for part in rel.parts):
            continue
        if _is_binary(path):
            continue
        candidates.append((str(rel), path))
    candidates.sort(key=lambda pair: pair[0])
    picked = candidates[: max(0, int(max_files))]
    return [(rel, path.read_text(encoding="utf-8", errors="ignore")) for rel, path in picked]


def _write_project_scaffold(scaffold: Path, project: str, files: List[Tuple[str, str]]) -> None:
    scaffold.mkdir(parents=True, exist_ok=True)
    (scaffold / "distribution.yaml").write_text(
        json.dumps({
            "name": project or "project",
            "version": "0.1.0",
            "description": f"Scaffolded Hermes project distribution: {project}",
            "distribution_owned": ["SOUL.md", "config.yaml", "mcp.json", "skills", "distribution.yaml"],
        }),
        encoding="utf-8",
    )
    (scaffold / "SOUL.md").write_text(f"# {project or 'project'}\n\nProject spine scaffolded by nv-coworker-compose.\n", encoding="utf-8")
    (scaffold / "mcp.json").write_text(json.dumps({"mcpServers": {}}, indent=2) + "\n", encoding="utf-8")
    skills_dir = scaffold / "skills"
    skills_dir.mkdir(exist_ok=True)
    for idx, (_rel, text) in enumerate(files):
        cap = skills_dir / f"capability-{idx}"
        cap.mkdir(exist_ok=True)
        (cap / "SKILL.md").write_text(
            f"---\nname: capability-{idx}\ndescription: Capability derived from a scanned source file.\n---\n{text}\n",
            encoding="utf-8",
        )


def onboard_project(source: str, settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Scaffold a project distribution from a local path or a git URL."""
    settings = settings or {}
    max_files = int(settings.get("max_files", 64))
    out = settings.get("out")

    cloned: Optional[Path] = None
    if _looks_like_url(source):
        src_path = Path(_clone_repo(source))
        cloned = src_path
    else:
        src_path = Path(source)
    if not src_path.exists():
        return {"ok": False, "error": f"source not found: {source}"}

    try:
        files = _scan_source_files(src_path, max_files)
        scaffold = Path(out) if out else src_path.parent / f"{src_path.name}-dist"
        _write_project_scaffold(scaffold, src_path.name, files)
    finally:
        # Clean only a dir WE cloned (our temp prefix) — never a caller's path.
        if cloned is not None and cloned.name.startswith(_CLONE_PREFIX):
            shutil.rmtree(cloned, ignore_errors=True)

    return {"ok": True, "distribution": str(scaffold)}


# ---------------------------------------------------------------------------
# Coworker onboarding — Bot-Mode identity, canonical Bot Chat, standing rooms
# ---------------------------------------------------------------------------

def _profile_revisions() -> Dict[str, Dict[str, int]]:
    resp = _dispatch_rpc("profiles.list", {})
    out: Dict[str, Dict[str, int]] = {}
    if isinstance(resp, dict):
        for profile in (resp.get("profiles") or []):
            if isinstance(profile, dict) and profile.get("name"):
                out[profile["name"]] = profile.get("ui_meta_revisions") or {}
    return out


def _has_bot_chat(rows: Any) -> bool:
    if not isinstance(rows, dict):
        return False
    for session in (rows.get("sessions") or []):
        if isinstance(session, dict) and session.get("title") == BOT_CHAT_TITLE:
            return True
    return False


def _ensure_canonical_bot_chat(profile: str) -> bool:
    """Materialise the profile's canonical Bot Chat: lookup exact title -> adopt
    existing -> else create + title to materialise the durable row -> re-consult
    + adopt on a uniqueness/title race. Returns True only when confirmed; a
    session hiccup is logged and reported (never aborts onboarding)."""
    try:
        listed = _dispatch_rpc("session.list", {"profile": profile, "title": BOT_CHAT_TITLE, "include_hidden": True})
        if _has_bot_chat(listed):
            return True
        created = _dispatch_rpc(
            "session.create",
            {"profile": profile, "title": BOT_CHAT_TITLE, "hidden": True, "follow_profile_config": True},
        )
        sid = None
        if isinstance(created, dict):
            sid = created.get("session_id") or created.get("stored_session_id")
        if not sid:
            relisted = _dispatch_rpc("session.list", {"profile": profile, "title": BOT_CHAT_TITLE, "include_hidden": True})
            return _has_bot_chat(relisted)
        try:
            _dispatch_rpc("session.title", {"profile": profile, "session_id": sid, "title": BOT_CHAT_TITLE})
            return True
        except RpcError:
            relisted = _dispatch_rpc("session.list", {"profile": profile, "title": BOT_CHAT_TITLE, "include_hidden": True})
            return _has_bot_chat(relisted)
    except Exception:
        logger.warning("canonical Bot Chat for %s not confirmed", profile, exc_info=True)
        return False


def _install(rendered_dir: str, name: str) -> None:
    from hermes_cli.profile_distribution import install_distribution
    install_distribution(str(rendered_dir), name=name, force=True)


def _configure_bot_meta(profile: str, ui_meta: Dict[str, Any], revisions: Dict[str, Dict[str, int]]) -> bool:
    """Write ui_meta['hermes-bots'] via per-key CAS; one retry from the current
    revision on a CAS conflict. Returns True when the write applied."""
    for attempt in range(2):
        current = int((revisions.get(profile) or {}).get("hermes-bots", 0))
        try:
            result = _dispatch_rpc("profiles.configure", {
                "name": profile,
                "ui_meta": {"hermes-bots": ui_meta},
                "ui_meta_expected_revisions": {"hermes-bots": current},
            })
        except RpcError:
            if attempt == 0:
                revisions[profile] = (_profile_revisions().get(profile) or {})
                continue
            return False
        if isinstance(result, dict) and isinstance(result.get("ui_meta_revisions"), dict):
            revisions[profile] = result["ui_meta_revisions"]
        return not isinstance(result, dict) or bool(result.get("applied", {}).get("ui_meta", True))
    return False


def _run_onboard(spec: str) -> Dict[str, Any]:
    data = load_spec(spec)
    types = data.get("types") or {}
    failures: List[str] = []

    with tempfile.TemporaryDirectory(prefix="nv_coworker_onboard_") as tmp:
        rendered = compose(spec, tmp)
        # Install only the coworker TYPE profiles; the DEFAULT multiplexer config
        # is applied to the gateway root separately (install_distribution rejects
        # the name "default").
        for tname in types:
            try:
                _install(rendered[tname], tname)
            except Exception as exc:
                failures.append(f"install {tname}: {exc}")

        revisions = _profile_revisions()
        for tname, tinfo in types.items():
            ui_meta = (tinfo or {}).get("ui_meta") or {}
            if not _configure_bot_meta(tname, ui_meta, revisions):
                failures.append(f"ui_meta configure failed for {tname}")
            if not _ensure_canonical_bot_chat(tname):
                failures.append(f"bot chat not confirmed for {tname}")

        for room_id, room in (data.get("rooms") or {}).items():
            members = (room or {}).get("members") or []
            try:
                state = _dispatch_rpc("groups.state", {"room_id": room_id})
                exists = _room_exists(state)
            except RpcError as exc:
                failures.append(f"groups.state {room_id}: {exc}")
                exists = True  # don't attempt a create we can't verify
            if not exists:
                try:
                    _dispatch_rpc("groups.create", {
                        "room_id": room_id,
                        "name": (room or {}).get("name", room_id),
                        "members": members,
                    })
                except RpcError as exc:
                    failures.append(f"groups.create {room_id}: {exc}")

    if failures:
        return {"ok": False, "error": "; ".join(failures), "warnings": failures}
    return {"ok": True}


def onboard_coworker(spec: str, settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Render + install coworker distributions and write their Bot-Mode identity.

    Standalone path (``settings['gateway_url']`` present): a read-only liveness
    preflight runs FIRST and, on failure, the onboard refuses with zero profile
    mutations. In-process path (no URL, e.g. the gateway tool): dispatch runs in
    the gateway process, no URL preflight."""
    global _pending_ws_requester
    settings = settings or {}
    url = settings.get("gateway_url")
    if url:
        _pending_ws_requester = None
        ok, reason = _gateway_preflight(url)
        if not ok:
            return {"ok": False, "error": f"gateway preflight failed: {reason}"}
        requester = _pending_ws_requester
        _pending_ws_requester = None
        token = _standalone_ctx.set({"url": url, "requester": requester})
        try:
            return _run_onboard(spec)
        finally:
            _close_standalone(token)
    return _run_onboard(spec)


# ---------------------------------------------------------------------------
# Gateway tool handlers (JSON-string returning; orchestrator-gated; never raise)
# ---------------------------------------------------------------------------

def _tool_onboard_coworker(args: Dict[str, Any], **_kwargs) -> str:
    try:
        spec = args.get("spec") or args.get("coworker_types")
        if not spec:
            return json.dumps({"ok": False, "error": "missing 'spec'"})
        return json.dumps(onboard_coworker(spec, settings={}))
    except Exception as exc:
        logger.warning("onboard_coworker tool failed", exc_info=True)
        return json.dumps({"ok": False, "error": str(exc)})


def _tool_onboard_project(args: Dict[str, Any], **_kwargs) -> str:
    try:
        source = args.get("source") or args.get("src")
        if not source:
            return json.dumps({"ok": False, "error": "missing 'source'"})
        return json.dumps(onboard_project(source, settings=dict(args)))
    except Exception as exc:
        logger.warning("onboard_project tool failed", exc_info=True)
        return json.dumps({"ok": False, "error": str(exc)})


def _tool_create_agent(args: Dict[str, Any], **_kwargs) -> str:
    try:
        _dispatch_rpc("profiles.create", dict(args))
        return json.dumps({"ok": True, "created": args.get("name")})
    except Exception as exc:
        logger.warning("create_agent tool failed", exc_info=True)
        return json.dumps({"ok": False, "error": str(exc)})


# ---------------------------------------------------------------------------
# Slash-command + CLI handlers
# ---------------------------------------------------------------------------

async def _slash_onboard_coworker(args: str = "", **_kwargs) -> str:
    spec = (args or "").strip()
    if not spec:
        return "usage: /onboard-coworker <coworker-types.yaml>"
    result = await asyncio.to_thread(onboard_coworker, spec, {})
    return json.dumps(result)


async def _slash_onboard_project(args: str = "", **_kwargs) -> str:
    source = (args or "").strip()
    if not source:
        return "usage: /onboard-project <path|url>"
    result = await asyncio.to_thread(onboard_project, source, {})
    return json.dumps(result)


def _cli_coworker(args, **_kwargs) -> int:
    if getattr(args, "coworker_command", None) == "compose":
        out = getattr(args, "out", None) or "coworkers-out"
        rendered = compose(args.spec, out)
        print(json.dumps({"ok": True, "rendered": rendered}, indent=2))
        return 0
    print("usage: hermes coworker compose <coworker-types.yaml> [--out DIR]")
    return 2


def _cli_onboard(args, **_kwargs) -> int:
    sub = getattr(args, "onboard_command", None)
    if sub == "project":
        settings = {"out": getattr(args, "out", None), "max_files": getattr(args, "max_files", 64)}
        result = onboard_project(args.src, settings=settings)
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1
    if sub == "coworker":
        settings = {"gateway_url": getattr(args, "gateway_url", None)}
        result = onboard_coworker(args.spec, settings=settings)
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1
    print("usage: hermes onboard {project <path|url>|coworker <coworker-types.yaml>}")
    return 2


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    from .cli import setup_coworker, setup_onboard

    # Plugin-relative key: get_config reads plugins.entries.nv-coworker-compose.settings.<key>.
    orchestrator_profile = ctx.get_config("orchestrator_profile", "orchestrator")

    def _check_orchestrator_only(*_a, **_k) -> bool:
        return get_active_profile_name() == orchestrator_profile

    ctx.register_tool(
        name="onboard_coworker", toolset=_TOOLSET, schema=ONBOARD_COWORKER_SCHEMA,
        handler=_tool_onboard_coworker, check_fn=_check_orchestrator_only,
    )
    ctx.register_tool(
        name="onboard_project", toolset=_TOOLSET, schema=ONBOARD_PROJECT_SCHEMA,
        handler=_tool_onboard_project, check_fn=_check_orchestrator_only,
    )
    ctx.register_tool(
        name="create_agent", toolset=_TOOLSET, schema=CREATE_AGENT_SCHEMA,
        handler=_tool_create_agent, check_fn=_check_orchestrator_only,
    )

    ctx.register_cli_command(
        name="coworker",
        help="Compose Hermes coworker profile distributions from a lego spec",
        setup_fn=setup_coworker, handler_fn=_cli_coworker,
        description="Render per-coworker-type Hermes profile distributions from a coworker-types.yaml spec.",
    )
    ctx.register_cli_command(
        name="onboard",
        help="Onboard a project or coworker roster into the fleet",
        setup_fn=setup_onboard, handler_fn=_cli_onboard,
        description="Scaffold a project distribution, or render+onboard coworker profiles into a gateway.",
    )

    ctx.register_command(
        name="onboard-project", handler=_slash_onboard_project,
        description="Scaffold a Hermes project distribution from a path or git URL.",
        args_hint="<path|url>",
    )
    ctx.register_command(
        name="onboard-coworker", handler=_slash_onboard_coworker,
        description="Render + onboard coworker profiles into the gateway.",
        args_hint="<coworker-types.yaml>",
    )


__all__ = [
    "compose", "load_spec", "CompositionError",
    "onboard_coworker", "onboard_project",
    "validate_gateway_url", "_gateway_preflight", "_ws_probe",
    "_dispatch_rpc", "_clone_repo", "register",
]
