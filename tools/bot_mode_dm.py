"""Bot Mode agent-to-agent DM tool — ``message_agent``.

A structured, Bot-Chat-only tool that lets a Bot Mode agent message a
teammate agent (another Hermes profile on this install, or an agent on a
registered peer gateway) WITHOUT hand-assembling shell commands.

Why this exists (Aug 2026): the Bot Mode teammate protocol taught agents to
DM each other via a prompt-injected ``hermes -p <bot> chat ...`` shellout.
That transport works, but the *invocation* was fragile — quoting traps
(#91339/#91304), temp-file choreography, dead-profile races — and the
Desktop's remote-mention path forwarded raw user text verbatim (#91397).
``message_agent`` replaces the invocation with a real tool call: the message
is a parameter, the target is validated against the live roster, the
attribution prefix is applied server-side, and the reply arrives through the
existing background-process notification path (fire-and-forget, never
blocks the sender's turn).

Containment contract (MUST hold — reviewers check all three):
- The tool schema is injected ONLY into a bot's canonical "Bot Chat"
  session on Bot-Mode-managed installs — the exact same gate as the
  protocol section in ``tools/bot_mode_probe.py``. It is NOT registered in
  the global tool registry, is NOT part of any toolset, and never appears
  in CLI sessions, ordinary gateway chats, group-room member sessions
  (titled "Group: …"), cron agents, or subagents.
- Dispatch is title-gated again at execution time (defense in depth): a
  forged call from a session that shouldn't have the tool returns a
  structured error instead of delivering.
- Everything here is additive. The legacy protocol transports
  (``hermes -p`` / ``hermes peer dm``) keep working for older prompts.

The transports themselves are unchanged and proven:
- local teammate  → ``hermes -p <name> chat --in ~ -c "Bot Chat"
  --create-if-missing -Q --query-file <tmp>`` (one turn, reply on stdout)
- peer teammate   → ``hermes peer dm <peer>[/<name>] < <tmp>``

Both run through ``terminal_tool(background=True)`` so the reply lands as a
completion notification on the sender's NEXT turn — the same wake shape every
Bot Mode agent already knows. A local delivery returns "sent" only once the
target's Bot Chat holds the message, and arms that notification only then;
a peer delivery returns "dispatched".
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

MESSAGE_AGENT_TOOL_NAME = "message_agent"

# Message body cap — generous for real work products, small enough that a
# runaway paste can't turn one DM into a context bomb on the recipient.
MESSAGE_MAX_CHARS = 16000

# A runner normally owns and removes each file. This bounds the residual
# plaintext lifetime if the machine dies after background-spawn acknowledgement
# but before the runner reaches its ``finally`` block.
_DM_DIR_NAME = "hermes-dm"
_DM_STALE_SECONDS = 24 * 60 * 60

# A local delivery is "sent" only once the target's Bot Chat holds the message.
# The sender waits the target's turn-lock budget plus this slack for the target
# CLI to start, resolve its Bot Chat, compress and persist the inbound row.
_DELIVERY_ACK_SLACK_SECONDS = 60
# After its verdict, the runner waits this long for the sender to arm the
# completion notification (and remove the signal file) before it may exit.
_DELIVERY_SIGNAL_HOLD_SECONDS = 30
_DELIVERY_POLL_SECONDS = 0.25
_NOTIFY_UNSUPPORTED = (
    "notify_on_complete / watch_patterns are not available in "
    "this session — it cannot receive an async completion after "
    "the turn ends (a one-shot runner such as `hermes -z`, a "
    "cron job, a Kanban worker, or a stateless HTTP endpoint). "
    "The process is "
    "running in the background; retrieve its result with "
    "process(action='poll') or process(action='wait')."
)

_PEER_TARGET_RE = re.compile(r"^([a-z0-9][a-z0-9_-]{0,63})/([a-zA-Z0-9][a-zA-Z0-9_-]{0,63})$")
_LOCAL_TARGET_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


def message_agent_tool_schema() -> dict:
    """OpenAI-format schema for ``message_agent`` (injected, not registered)."""
    return {
        "type": "function",
        "function": {
            "name": MESSAGE_AGENT_TOOL_NAME,
            "description": (
                "Send a message to ANOTHER agent (teammate) on this install, or to an "
                "agent on a registered peer gateway. This is FIRE-AND-FORGET and "
                "asynchronous, like texting: it validates the target against the live "
                "roster, delivers your message into that agent's own Bot Chat with your "
                "attribution automatically prefixed. A teammate on this install: it "
                "returns once their Bot Chat holds your message (seconds). A peer-gateway "
                "agent: it returns 'dispatched' before receipt is confirmed. It does NOT "
                "return their reply and you must "
                "not wait or poll for one — send it, finish your turn, and the reply "
                "arrives later as a background-process completion notification that "
                "wakes you. COMPOSE the message yourself: write what YOU want to say to "
                "that agent (lead with the point; include the concrete ask or result). "
                "Never paste the user's words verbatim — paraphrase the actionable "
                "substance, and keep private 1:1 chat content private. Message one "
                "clearly relevant teammate when it genuinely helps the user's goal; "
                "don't fan out to several agents unless the user explicitly asked. "
                "Use the teammate roster in your system prompt (names + roles) to pick "
                "the right recipient; targets: a teammate name (e.g. 'researcher'), "
                "'<peer>/<agent>' for an agent on a registered peer gateway "
                "(e.g. 'spark/researcher', or just '<peer>' for the peer's main agent), "
                "or an agent on another connected machine from your roster (use "
                "'<handle>@<connection>' if the same handle exists on several)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": (
                            "Who to message: a teammate profile name from your roster "
                            "('researcher', 'hermes' for the default agent), or "
                            "'<peer>' / '<peer>/<agent>' for a registered peer gateway."
                        ),
                    },
                    "message": {
                        "type": "string",
                        "description": (
                            "The message YOU composed for that agent (max "
                            f"{MESSAGE_MAX_CHARS} chars). Do not include the "
                            "'Message from …' prefix — it is added automatically."
                        ),
                    },
                },
                "required": ["target", "message"],
            },
        },
    }


def ensure_message_agent_tool(agent: Any) -> bool:
    """Inject the ``message_agent`` schema into a Bot Chat agent's tool list.

    Called once per turn from the conversation loop. Idempotent and
    deterministic for the life of a session: the gate (canonical Bot Chat
    title on a Bot-Mode-managed install) is stable from the session's first
    turn, so the tool list is byte-identical across turns — prompt-cache
    safe. Every non-Bot-Chat session fails the gate on every turn and never
    sees the schema. Never raises.
    """
    try:
        if not getattr(agent, "_bot_mode_protocol", True):
            return False
        tools = getattr(agent, "tools", None)
        if tools:
            for tool in tools:
                if (
                    isinstance(tool, dict)
                    and tool.get("function", {}).get("name") == MESSAGE_AGENT_TOOL_NAME
                ):
                    return True
        from tools.bot_mode_probe import BOT_CHAT_TITLE, is_bot_mode_managed

        if _session_title(agent) != BOT_CHAT_TITLE:
            return False
        # Managed-install check, NOT section non-emptiness: a profile whose
        # SOUL.md carries the legacy plugin-appended protocol text gets an
        # empty section (dedupe) but must still receive the tool — otherwise
        # upgraded installs silently lose A2A messaging (Aug 2026).
        if not is_bot_mode_managed(_agent_home(agent)):
            return False
        if agent.tools is None:
            agent.tools = []
        agent.tools.append(message_agent_tool_schema())
        valid = getattr(agent, "valid_tool_names", None)
        if isinstance(valid, set):
            valid.add(MESSAGE_AGENT_TOOL_NAME)
        return True
    except Exception:  # pragma: no cover — must never break a turn
        logger.debug("ensure_message_agent_tool failed", exc_info=True)
        return False


# ── roster resolution ────────────────────────────────────────────────────────


def _hermes_root(home: Path) -> Path:
    if home.parent.name == "profiles":
        return home.parent.parent
    return home


def _self_profile_name(home: Path) -> str:
    if home.parent.name == "profiles":
        return home.name
    return "default"


def _local_roster(root: Path) -> list[str]:
    """Profile names on this install: default + every named profile."""
    names = ["default"]
    try:
        profiles = root / "profiles"
        if profiles.is_dir():
            for child in sorted(profiles.iterdir()):
                if child.is_dir():
                    names.append(child.name)
    except Exception:
        pass
    return names


def _peers(root: Path) -> list[str]:
    try:
        from tools.bot_mode_probe import _peers as _probe_peers

        return _probe_peers(root)
    except Exception:
        return []


def _handle(name: str) -> str:
    return "hermes" if name == "default" else name


def _resolve_local_name(target: str, roster: list[str]) -> Optional[str]:
    """Map a target handle to a profile name ('hermes' → 'default')."""
    want = target.strip()
    if not want:
        return None
    if want.lower() == "hermes":
        return "default" if "default" in roster else None
    for name in roster:
        if name.lower() == want.lower():
            return name
    return None


# ── the tool ─────────────────────────────────────────────────────────────────


def _err(message: str, *, roster: list[str] | None = None, peers: list[str] | None = None) -> str:
    from tools.bot_failure_reasons import classify_agent_error

    payload: dict[str, Any] = {"error": message, "reason": classify_agent_error(message)}
    if roster is not None:
        payload["teammates"] = roster
    if peers is not None:
        payload["peers"] = peers
    return json.dumps(payload)


def message_agent_tool(
    target: str = "",
    message: str = "",
    task_id: Optional[str] = None,
    agent: Any = None,
) -> str:
    """Deliver ``message`` to ``target``'s Bot Chat. Returns a JSON ack/error.

    ``agent`` is the calling AIAgent (threaded by the executor) — used for
    the Bot Chat gate, the sender identity, and the session key so the
    spawned transport is tracked against the right session.
    """
    # ── defense-in-depth gate: only a canonical Bot Chat may deliver ──
    home = _agent_home(agent)
    try:
        from tools.bot_mode_probe import BOT_CHAT_TITLE, is_bot_mode_managed

        title = _session_title(agent)
        if title != BOT_CHAT_TITLE:
            return _err(
                "message_agent is only available in a Bot Mode 'Bot Chat' session. "
                "This session is not one; do not retry."
            )
        if not is_bot_mode_managed(home):
            return _err(
                "This install is not Bot-Mode-managed (no bot roster); "
                "message_agent is unavailable. Do not retry."
            )
    except Exception as exc:  # pragma: no cover — defensive
        return _err(f"Bot Mode gate check failed: {exc}")

    root = _hermes_root(Path(home))
    me = _self_profile_name(Path(home))
    roster = _local_roster(root)
    peers = _peers(root)
    teammates = [_handle(n) for n in roster if n != me]

    body = str(message or "").strip()
    if not body:
        return _err("message is required — compose what you want to say to that agent.")
    if len(body) > MESSAGE_MAX_CHARS:
        return _err(
            f"message too long ({len(body)} chars > {MESSAGE_MAX_CHARS}). "
            "Send the essentials; share large content as a file path instead."
        )

    raw_target = str(target or "").strip().lstrip("@")
    if not raw_target:
        return _err("target is required.", roster=teammates, peers=peers)

    sender_handle = _handle(me)
    prefix = f"Message from 🤖 {sender_handle} (@{sender_handle}): "

    # Background delivery workers may not inherit the gateway venv on PATH.
    from tools.bot_relay import _hermes_cli

    # ── peer target: '<peer>/<agent>' or a bare registered peer name ──
    peer_match = _PEER_TARGET_RE.match(raw_target)
    bare_peer = raw_target.lower() if raw_target.lower() in peers else None
    if peer_match or bare_peer:
        peer_name = peer_match.group(1) if peer_match else bare_peer
        peer_profile = peer_match.group(2) if peer_match else None
        if peer_name not in peers:
            return _err(
                f"No registered peer named '{peer_name}'.", roster=teammates, peers=peers
            )
        dm_target = f"{peer_name}/{peer_profile}" if peer_profile else peer_name
        label = f"@{peer_profile or peer_name} on peer '{peer_name}'"
        # Pin the registry-owning profile (#93935): `hermes peer` resolves
        # bot_peers through load_config(), which is profile-scoped — an
        # unpinned subprocess inherits THIS gateway's profile context, so a
        # secondary-profile bot's peer DM ran against an empty registry and
        # died with "No peer named". The tool-side roster above reads the
        # machine-root config (the default profile's home), so the CLI must
        # run in that same profile to see the same registry. Mirrors the
        # local-teammate path's `-p <resolved>` pin below.
        return _start_delivery(
            [
                _hermes_cli(),
                "-p",
                _self_profile_name(root),
                "peer",
                "dm",
                dm_target,
            ],
            prefix + body,
            label,
            stdin_file=True,
            task_id=task_id,
            agent=agent,
        )

    # ── local teammate ──
    if not _LOCAL_TARGET_RE.match(raw_target) and "@" not in raw_target:
        return _err(f"Invalid target: {raw_target!r}.", roster=teammates, peers=peers)
    resolved = _resolve_local_name(raw_target, roster) if _LOCAL_TARGET_RE.match(raw_target) else None
    if resolved is None:
        # ── cross-connection teammate (Desktop relay) ──
        # Every gateway connected to the user's Desktop is reachable: the
        # relay roster lists agents on the other connections; delivery rides
        # the Desktop's own persistent socket to that gateway.
        relayed = _try_relay_delivery(
            root, raw_target, body, me, sender_handle, task_id=task_id, agent=agent
        )
        if relayed is not None:
            return relayed
        return _err(
            f"No teammate named '{raw_target}' on this install, on a connected "
            "machine, or on a registered peer. Pick a name from the roster "
            "(roles are listed in your system prompt).",
            roster=teammates,
            peers=peers,
        )
    if resolved == me:
        # Same-name target on ANOTHER connection (e.g. this gateway's
        # 'default' messaging the cloud 'default') — try the relay before
        # calling it a self-message.
        relayed = _try_relay_delivery(
            root, raw_target, body, me, sender_handle, task_id=task_id, agent=agent
        )
        if relayed is not None:
            return relayed
        return _err("You can't message yourself. Pick a teammate from the roster.")

    return _start_delivery(
        [
            _hermes_cli(),
            "-p",
            resolved,
            "chat",
            "--in",
            "~",
            "-c",
            "Bot Chat",
            "--create-if-missing",
            "-Q",
        ],
        prefix + body,
        f"@{_handle(resolved)}",
        stdin_file=False,
        task_id=task_id,
        agent=agent,
    )


def _try_relay_delivery(
    root: Path,
    raw_target: str,
    body: str,
    me: str,
    sender_handle: str,
    *,
    task_id: Optional[str],
    agent: Any,
) -> Optional[str]:
    """Cross-connection delivery via the Desktop relay, or None if the
    target doesn't resolve against the relay roster.

    The envelope is queued on disk; the Desktop drains it over RPC and
    delivers on the target connection's own socket. A background waiter is
    spawned immediately so the relayed reply wakes the sender through the
    standard completion-notification path — identical UX to a local DM.
    """
    try:
        from tools.bot_relay import (
            EnvelopeRefusedError,
            enqueue_envelope,
            read_remote_roster,
            resolve_remote_target,
            waiter_command,
        )

        roster = read_remote_roster(root)
        if not roster:
            return None
        match = resolve_remote_target(raw_target, roster)
        if match is None:
            return None
        if match == "ambiguous":
            forms = ", ".join(
                f"{r['handle']}@{r['connection_id']}"
                for r in roster
                if r["handle"].lower() == raw_target.strip().lstrip("@").lower()
            )
            return _err(
                f"'{raw_target}' exists on several connected machines — "
                f"disambiguate with one of: {forms}."
            )
        try:
            envelope = enqueue_envelope(
                root,
                target=match,
                message=f"Message from 🤖 {sender_handle} (@{sender_handle}): {body}",
                sender_profile=me,
                sender_handle=sender_handle,
            )
        except EnvelopeRefusedError as exc:
            # Fail fast: target definitively offline — nothing was queued.
            # Structured refusal so the agent can distinguish it from a
            # resolution error ('runtime_offline' per the #93091 reason enum).
            return json.dumps({"error": str(exc), "reason": exc.reason})
        label = f"@{match['handle']} on {match['connection_label'] or match['connection_id']}"
        return _spawn_delivery(
            waiter_command(root, envelope), label, task_id=task_id, agent=agent
        )
    except Exception:
        logger.debug("relay delivery attempt failed", exc_info=True)
        return None


def _dm_dir() -> Path:
    uid_getter = getattr(os, "getuid", None)
    uid = uid_getter() if callable(uid_getter) else None
    dirname = f"{_DM_DIR_NAME}-{uid}" if uid is not None else _DM_DIR_NAME
    path = Path(tempfile.gettempdir()) / dirname
    path.mkdir(mode=0o700, exist_ok=True)

    # Shared POSIX temp roots need a per-user directory. Fail closed if an
    # attacker pre-created the expected path or replaced it with a symlink.
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise PermissionError(f"DM temp path is not a directory: {path}")
    if uid is not None and info.st_uid != uid:
        raise PermissionError(f"DM temp directory is owned by another user: {path}")
    if stat.S_IMODE(info.st_mode) != 0o700:
        path.chmod(0o700)
    return path


def cleanup_bot_dm_cache(
    max_age_hours: float = _DM_STALE_SECONDS / 3600, *, now: float | None = None
) -> int:
    """Delete orphaned DM payload files older than *max_age_hours*.

    Same contract as the other ``cleanup_*_cache`` helpers — returns the
    number of files removed — so the gateway housekeeping loop can prune
    this cache on the same hourly cadence as the media caches, even on
    installs that never send another DM (the in-band sweep in
    ``_write_dm_file`` only runs when a DM is written).
    """
    cutoff = (time.time() if now is None else now) - max_age_hours * 3600
    removed = 0
    # Include the legacy temp-root locations so upgrades clean files created
    # by versions predating the dedicated directory.
    temp_root = Path(tempfile.gettempdir())
    locations: list[tuple[Path, str]] = [
        (temp_root, "hermes-dm-*.txt"),
        (temp_root, "hermes-relay-dm-*.txt"),
    ]
    try:
        dm_dir = _dm_dir()
        locations += [(dm_dir, "*.txt"), (dm_dir, "*.signal"), (dm_dir, "*.signal.tmp")]
    except OSError:
        pass
    for directory, pattern in locations:
        try:
            for candidate in directory.glob(pattern):
                try:
                    if candidate.is_file() and candidate.stat().st_mtime < cutoff:
                        candidate.unlink()
                        removed += 1
                except OSError:
                    pass
        except OSError:
            pass
    return removed


def _sweep_stale_dm_files(*, now: float | None = None) -> None:
    """Best-effort cleanup for files orphaned before their runner started."""
    cleanup_bot_dm_cache(now=now)


def _write_dm_file(content: str) -> str:
    """The message rides a temp file — never inline shell text."""
    _sweep_stale_dm_files()
    fd, path = tempfile.mkstemp(prefix="dm-", suffix=".txt", dir=_dm_dir(), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
    except BaseException:
        # fdopen owns the descriptor once it succeeds, but if fdopen itself
        # failed the raw descriptor is still ours. Closing twice is harmless.
        try:
            os.close(fd)
        except OSError:
            pass
        _unlink_dm_file(path)
        raise
    return path


def _unlink_dm_file(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _delivery_lock(argv: list[str], *, stdin_file: bool):
    """Per-profile turn lock context for a LOCAL teammate delivery (#93091).

    Local deliveries (``hermes -p <profile> chat …``) collide with relay
    deliveries into the same profile — both run a Bot Chat turn on this
    install — so the turn window is serialized on the shared cross-process
    lock in ``tools.bot_relay``. Peer transports (stdin mode) run on the
    remote gateway; their turn is locked THERE by its own deliver path.
    """
    # The CLI element is matched by basename: local_delivery_command now
    # resolves the venv-relative hermes next to this gateway's interpreter
    # (#93590 — service contexts lack PATH), so argv[0] may be an absolute
    # path (and on Windows carries the .exe suffix). Split on both
    # separators so the shape matches regardless of which platform built
    # the argv.
    cli = (argv[0] if argv else "").rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    if (
        stdin_file
        or len(argv) < 3
        or cli not in ("hermes", "hermes.exe")
        or argv[1] != "-p"
    ):
        return contextlib.nullcontext()
    from tools.bot_relay import acquire_turn_lock

    home = Path(os.getenv("HERMES_HOME") or os.path.expanduser("~/.hermes"))
    return acquire_turn_lock(_hermes_root(home), argv[2])


def _target_db_path(profile: str) -> Path:
    """Use the process home inherited by the selected-profile child CLI."""
    from hermes_constants import get_process_hermes_home

    root = _hermes_root(get_process_hermes_home())
    return root / "state.db" if profile == "default" else root / "profiles" / profile / "state.db"


def _ack_needle(content: str) -> str:
    # The -Q query path persists the query text with only lone surrogates replaced.
    from agent.message_sanitization import _sanitize_surrogates

    return _sanitize_surrogates(content).strip()


def _delivery_baseline(db_path: Path) -> int:
    """Table-wide MAX(messages.id), taken under the target's turn lock.

    0 only when the target has no state.db yet: on an existing database a 0
    baseline would let an older identical message acknowledge this delivery,
    so a read failure raises instead.
    """
    if not db_path.exists():
        return 0
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        try:
            return int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise RuntimeError(f"could not read the target transcript baseline: {exc}") from exc


def _ack_seen(db_path: Path, baseline: int, content: str) -> Optional[bool]:
    """True once the Bot Chat the target CLI resumes holds the whole message.

    Resolves the session exactly as ``-c "Bot Chat"`` does (latest ``Bot Chat
    #N``, then its compression tip). ``None`` means the transcript could not be
    read: the message may or may not be there, so it is never a confirmed absence.
    """
    if not db_path.exists():
        return False
    db = None
    try:
        needle = _ack_needle(content)
        if not needle:
            return False
        from hermes_state import SessionDB
        from tools.bot_mode_probe import BOT_CHAT_TITLE

        db = SessionDB(db_path=db_path, read_only=True)
        sid = db.resolve_session_by_title(BOT_CHAT_TITLE)
        if not sid:
            return False
        sid = db.get_compression_tip(sid) or sid
        return any(
            m.get("role") == "user" and needle in str(m.get("content") or "")
            for m in db.get_messages(sid, after_id=baseline)
        )
    except Exception:
        logger.debug("delivery ack read failed for %s", db_path, exc_info=True)
        return None
    finally:
        if db is not None:
            with contextlib.suppress(Exception):
                db.close()


def _write_signal(signal_file: str, state: dict) -> None:
    tmp = f"{signal_file}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, signal_file)
    except OSError:
        logger.debug("delivery signal write failed", exc_info=True)


def _read_signal(signal_file: str) -> dict:
    try:
        with open(signal_file, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _hold_for_sender(signal_file: str) -> None:
    """Bounded wait until the sender has consumed the verdict (removed the file)."""
    deadline = time.monotonic() + _DELIVERY_SIGNAL_HOLD_SECONDS
    while os.path.exists(signal_file) and time.monotonic() < deadline:
        time.sleep(_DELIVERY_POLL_SECONDS)


def _signal_failed(signal_file: str, error: str, reason: str = "") -> None:
    from tools.bot_failure_reasons import classify_agent_error

    reason = reason or classify_agent_error(error)
    _write_signal(signal_file, {
        "state": "failed",
        "reason": "delivery_unconfirmed" if reason == "unknown" else reason,
        "error": error,
    })
    _hold_for_sender(signal_file)


def _ack_baseline(signal_file: str, db_path: Path, content: str) -> tuple:
    baseline = _delivery_baseline(db_path)
    _write_signal(signal_file, {"state": "pending", "baseline": baseline, "db": str(db_path)})
    return db_path, baseline, content


def _run_acked_turn(argv: list[str], signal_file: str, ack: Optional[tuple]):
    """Run one target turn; with ``ack``, signal ``accepted`` the moment its row lands.

    Returns ``(completed_process, accepted)``. The turn runs on a helper thread
    (``subprocess.run`` drains both pipes) while this thread polls the target's
    Bot Chat; the predicate is checked once more after the turn exits, and
    ``accepted`` is ``None`` when the transcript stayed unreadable by then.
    """
    def _run():
        return subprocess.run(
            argv, check=False, stdin=subprocess.DEVNULL, capture_output=True, text=True
        )

    if ack is None:
        return _run(), False
    done: list = []

    def _worker():
        try:
            done.append(_run())
        except BaseException as exc:
            done.append(exc)

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()
    unreadable_after_exit = 0
    while True:
        finished = not worker.is_alive()
        accepted = _ack_seen(*ack)
        if accepted:
            _write_signal(signal_file, {"state": "accepted"})
            break
        if finished and (accepted is False or unreadable_after_exit >= 2):
            break
        if finished:
            unreadable_after_exit += 1
        time.sleep(_DELIVERY_POLL_SECONDS)
    worker.join()
    if isinstance(done[0], BaseException):
        raise done[0]
    return done[0], accepted


def _run_delivery(argv: list[str], dm_file: str, *, stdin_file: bool) -> int:
    """Run one DM transport and remove its plaintext file after consumption.

    The turn execution window (not the enqueue) holds the target profile's
    cross-process lock, so two deliveries into one profile queue instead of
    racing; a bounded wait ends in a structured 'target_busy' refusal.

    Local (query-file) turns get one policy-gated retry (#93091 item 5):
    transient failures re-run the same session; a context_overflow re-run
    lets the retried turn's pre-API compaction pass compact the Bot Chat
    transcript first (agent/conversation_loop.py) — the sanctioned
    compression lever; no fresh session is ever minted. Auth/quota/config
    failures never retry. Peer transports (stdin mode) retry on their own
    gateway's deliver path, not here.

    A sender that created ``<dm_file>.signal`` waits for acceptance. The local
    branch then reports through it (``pending`` with the under-lock baseline,
    then ``accepted`` once the target's resumed Bot Chat holds the message, or
    ``failed``) and holds until the sender has read the verdict. There the
    retry runs only while the message is confirmed absent: provider 5xx,
    rate-limit and context-overflow failures surface after turn-start
    persistence, so such a turn is not re-run, and a transcript that cannot be
    read is reported as possibly delivered instead of retried.
    """
    signal_file = f"{dm_file}.signal"
    acked = not stdin_file and os.path.exists(signal_file)
    try:
        with _delivery_lock(argv, stdin_file=stdin_file):
            if stdin_file:
                # Keep the file open until the transport exits; cleanup occurs
                # after subprocess.run returns, not merely after stdin reaches EOF.
                with open(dm_file, "r", encoding="utf-8") as stream:
                    return subprocess.run(argv, stdin=stream, check=False).returncode
            ack = None
            if acked:
                with open(dm_file, "r", encoding="utf-8") as f:
                    content = f.read()
                ack = _ack_baseline(signal_file, _target_db_path(argv[2]), content)
            query_argv = [*argv, "--query-file", dm_file]
            proc, accepted = _run_acked_turn(query_argv, signal_file, ack)
            if proc.returncode != 0:
                from tools.bot_failure_reasons import (
                    RETRY_NONE,
                    classify_agent_error,
                    retry_action,
                )

                detail = (proc.stderr or proc.stdout or "").strip()[-500:]
                # Only a confirmed absence is retried: a turn that holds the
                # message, or whose transcript could not be read, is never re-sent.
                if accepted is False and retry_action(classify_agent_error(detail)) != RETRY_NONE:
                    if acked:
                        ack = _ack_baseline(signal_file, ack[0], content)
                    proc, accepted = _run_acked_turn(query_argv, signal_file, ack)
            # Re-emit the transport's streams: stdout is the reply text the
            # completion notification carries back to the sending agent.
            if proc.stdout:
                sys.stdout.write(proc.stdout)
                sys.stdout.flush()
            if proc.stderr:
                sys.stderr.write(proc.stderr)
                sys.stderr.flush()
            if acked and accepted is None:
                _signal_failed(
                    signal_file,
                    "its acceptance could not be confirmed (the transcript was unreadable after the "
                    "turn); the message may already be in its Bot Chat, so check before resending",
                    "delivery_unconfirmed",
                )
            elif acked and not accepted:
                detail = (proc.stderr or proc.stdout or "").strip()[-500:]
                _signal_failed(
                    signal_file,
                    f"target exited {proc.returncode} before its Bot Chat held the message"
                    + (f": {detail}" if detail else ""),
                    "" if proc.returncode else "delivery_unconfirmed",
                )
            elif acked:
                _hold_for_sender(signal_file)
            return proc.returncode
    except Exception as exc:
        if acked:
            _signal_failed(signal_file, str(exc), getattr(exc, "reason", ""))
        raise
    finally:
        _unlink_dm_file(dm_file)
        if acked:
            _unlink_dm_file(signal_file)
            _unlink_dm_file(f"{signal_file}.tmp")


def _delivery_command(argv: list[str], dm_file: str, *, stdin_file: bool) -> str:
    """Build an argv-safe command for the cleanup-owning background runner."""
    runner_argv = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--run-delivery",
        "stdin" if stdin_file else "query-file",
        dm_file,
        *argv,
    ]
    if sys.platform == "win32":
        # The tracked local backend uses Git Bash on native Windows. Forward
        # slashes preserve native drive paths while remaining executable by
        # that shell; backslash-form paths are parsed as command names and die
        # with exit 127 before this runner starts.
        runner_argv = [part.replace("\\", "/") for part in runner_argv]
    return shlex.join(runner_argv)


def _start_delivery(
    argv: list[str],
    content: str,
    label: str,
    *,
    stdin_file: bool,
    task_id: Optional[str],
    agent: Any,
) -> str:
    """Create a DM file and transfer its cleanup ownership to the runner."""
    dm_file = _write_dm_file(content)
    try:
        command = _delivery_command(argv, dm_file, stdin_file=stdin_file)
    except BaseException:
        _unlink_dm_file(dm_file)
        raise
    return _spawn_delivery(
        command,
        label,
        dm_file=dm_file,
        signal_file=None if stdin_file else f"{dm_file}.signal",
        ack_content=None if stdin_file else content,
        task_id=task_id,
        agent=agent,
    )


def _arm_completion_notify(proc_id: str) -> tuple[bool, str]:
    """Give an accepted delivery the completion contract ``terminal_tool`` would.

    Mirrors ``terminal_tool``'s notify block (async-delivery refusal, routing
    fields, gateway watcher). Runs in the sender's own handler context, so the
    session vars are the ones the spawn saw.
    """
    from tools.process_registry import process_registry

    session = process_registry.get(proc_id)
    if session is None or session.exited:
        return False, "exited"
    from gateway.session_context import async_delivery_supported, get_session_env

    if not async_delivery_supported():
        return False, "notify_unsupported"
    if get_session_env("HERMES_SESSION_PLATFORM", ""):
        session.watcher_platform = get_session_env("HERMES_SESSION_PLATFORM", "")
        session.watcher_chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "")
        session.watcher_user_id = get_session_env("HERMES_SESSION_USER_ID", "")
        session.watcher_user_name = get_session_env("HERMES_SESSION_USER_NAME", "")
        session.watcher_thread_id = get_session_env("HERMES_SESSION_THREAD_ID", "")
        session.watcher_message_id = get_session_env("HERMES_SESSION_MESSAGE_ID", "")
        session.parent_session_id = get_session_env("HERMES_SESSION_ID", "")
    session.notify_on_complete = True
    if session.watcher_platform:
        session.watcher_interval = 5
        process_registry.pending_watchers.append({
            "session_id": session.id,
            "check_interval": 5,
            "session_key": session.session_key,
            "platform": session.watcher_platform,
            "chat_id": session.watcher_chat_id,
            "user_id": session.watcher_user_id,
            "user_name": session.watcher_user_name,
            "thread_id": session.watcher_thread_id,
            "message_id": session.watcher_message_id,
            "notify_on_complete": True,
            "parent_session_id": session.parent_session_id,
        })
    return True, ""


def _await_delivery_ack(proc_id: str, signal_file: str, bound: float) -> dict:
    """Wait for the runner's verdict: ``accepted`` | ``failed`` | ``exited`` | ``timeout`` | ``cancelled``.

    Non-verdict states carry the last ``baseline``/``db`` the runner published.
    """
    from tools.interrupt import is_interrupted
    from tools.process_registry import process_registry

    deadline = time.monotonic() + bound
    pending: dict = {}
    while True:
        # Exit is sampled before the file, so a verdict written just before exit wins.
        session = process_registry.get(proc_id)
        exited = session is None or session.exited
        state = _read_signal(signal_file)
        if state.get("state") in ("accepted", "failed"):
            return state
        if state.get("state") == "pending":
            pending = state
        if exited:
            return {**pending, "state": "exited", "exit_code": getattr(session, "exit_code", None)}
        if is_interrupted():
            return {**pending, "state": "cancelled"}
        if time.monotonic() >= deadline:
            return {**pending, "state": "timeout"}
        time.sleep(_DELIVERY_POLL_SECONDS)


def _delivered(proc_id: str, label: str, untracked: str = "") -> str:
    payload: dict[str, Any] = {
        "status": "delivered_untracked" if untracked else "sent",
        "to": label,
        "detail": (
            f"Message delivered into {label}'s Bot Chat, but its reply will not arrive as a "
            "notification in this session; read it in their Bot Chat. Do not resend."
            if untracked else
            f"Message delivered into {label}'s Bot Chat; its reply arrives later as a "
            "completion notification. This is asynchronous — do NOT wait or poll. Finish "
            "your turn now and relay the reply then, attributed to that agent."
        ),
        "process_id": proc_id,
        "sent_at": int(time.time()),
    }
    if untracked:
        payload["reason"] = untracked
    if untracked == "notify_unsupported":
        payload["notify_unsupported"] = _NOTIFY_UNSUPPORTED
    return json.dumps(payload)


def _stop_delivery(proc_id: str) -> bool:
    """Tree-kill an unaccepted delivery (target CLI included); True once it is down."""
    from tools.process_registry import process_registry

    try:
        status = process_registry.kill_process(proc_id, consume_output=True).get("status")
    except Exception:
        logger.warning("message_agent could not stop delivery %s", proc_id, exc_info=True)
        return False
    return status in ("killed", "already_exited")


def _await_local_delivery(
    proc_id: str, label: str, dm_file: str, signal_file: str, content: str
) -> str:
    """Turn the runner's verdict into the tool result; never ``sent`` before acceptance."""
    from tools import bot_relay

    files = [dm_file, signal_file, f"{signal_file}.tmp"]
    bound = bot_relay.turn_wait_seconds() + _DELIVERY_ACK_SLACK_SECONDS
    state = _await_delivery_ack(proc_id, signal_file, bound)
    verdict = state.get("state")
    stopped_by = None
    if verdict in ("timeout", "cancelled"):
        stopped = _stop_delivery(proc_id)
        # An acceptance that landed at the bound is still a delivery: never invite a resend.
        if _read_signal(signal_file).get("state") == "accepted":
            verdict = "accepted"
        elif not stopped:
            # The runner still owns its files.
            return json.dumps({
                "error": (
                    f"Delivery to {label} was not accepted within {int(bound)}s and could "
                    "not be stopped; it may still be delivered."
                ),
                "reason": "delivery_timeout",
            })
        else:
            # The row can land before the runner's next poll writes `accepted`.
            stopped_by = verdict
    if verdict == "accepted":
        armed, why = _arm_completion_notify(proc_id)
        for path in files if why == "exited" else [signal_file]:
            _unlink_dm_file(path)
        return _delivered(proc_id, label, "" if armed else why)
    if verdict == "failed":
        if _stop_delivery(proc_id):
            for path in files:
                _unlink_dm_file(path)
        return json.dumps({
            "error": f"Delivery to {label} failed: {state.get('error') or 'not accepted'}",
            "reason": state.get("reason") or "delivery_unconfirmed",
        })
    for path in files:
        _unlink_dm_file(path)
    # The runner is gone without a verdict (killed, stopped, or its hold
    # expired): re-check acceptance with the baseline it published, if any.
    if state.get("db") and state.get("baseline") is not None:
        seen = _ack_seen(Path(state["db"]), int(state["baseline"]), content)
        if seen:
            return _delivered(proc_id, label, "exited")
        if seen is None:
            return json.dumps({
                "error": (
                    f"Delivery to {label} could not be confirmed (its transcript was unreadable); "
                    f"the message may already be in {label}'s Bot Chat, so check before resending."
                ),
                "reason": "delivery_unconfirmed",
            })
    if stopped_by:
        return json.dumps({
            "error": f"Delivery to {label} was stopped before {label} accepted the message.",
            "reason": "cancelled" if stopped_by == "cancelled" else "delivery_timeout",
        })
    return json.dumps({
        "error": (
            f"Delivery to {label} ended before the message was accepted "
            f"(exit {state.get('exit_code')})."
        ),
        "reason": "delivery_unconfirmed",
    })


def _spawn_delivery(
    command: str,
    label: str,
    *,
    dm_file: Optional[str] = None,
    signal_file: Optional[str] = None,
    ack_content: Optional[str] = None,
    task_id: Optional[str],
    agent: Any,
) -> str:
    """Launch the cleanup-owning runner and transfer file ownership on ack.

    ``dm_file`` is None for relay deliveries: the waiter command watches a
    reply file, and the envelope artifacts are owned and swept by
    ``tools/bot_relay.py`` — there is no plaintext DM tempfile to reclaim.

    ``signal_file`` (local deliveries only) makes this wait until the target
    accepted the message, and spawns WITHOUT ``notify_on_complete``: the
    completion contract is armed only after acceptance, so a delivery that
    fails before it can never wake the sender later.
    """
    transferred = False
    try:
        from tools.terminal_tool import terminal_tool

        if signal_file:
            # Created before the spawn: its presence is what opts the runner into
            # the acceptance protocol. A failed write must abort the delivery.
            Path(signal_file).write_text('{"state": "spawned"}', encoding="utf-8")
        raw = terminal_tool(
            command,
            background=True,
            notify_on_complete=not signal_file,
            task_id=task_id,
            workdir=str(Path(__file__).resolve().parent.parent),
            _host_local=True,
        )
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = {}
        proc_id = parsed.get("session_id") or ""
        if parsed.get("error"):
            return _err(f"Delivery to {label} failed to start: {parsed['error']}")
        if not proc_id:
            return _err(f"Delivery to {label} failed to start: no process id returned")
        # From this point the background runner owns the file and removes it
        # only after the local query-file or peer stdin consumer has finished.
        transferred = True
        if signal_file:
            return _await_local_delivery(proc_id, label, dm_file, signal_file, ack_content or "")
        return json.dumps(
            {
                # A peer turn runs on the peer gateway with no acceptance seam,
                # so it is only "dispatched"; a relay envelope is already queued.
                "status": "dispatched" if dm_file else "sent",
                "to": label,
                "detail": (
                    f"Message dispatched to {label}. This is asynchronous — do NOT wait "
                    "or poll. Finish your turn now; when the delivery completes, its "
                    "notification carries the reply — relay it then, attributed to "
                    "that agent."
                ),
                **({"process_id": proc_id} if proc_id else {}),
                "sent_at": int(time.time()),
            }
        )
    except Exception as exc:
        logger.error("message_agent delivery spawn failed: %s", exc, exc_info=True)
        return _err(f"Delivery to {label} could not be started: {exc}")
    finally:
        if not transferred:
            for path in (dm_file, signal_file):
                if path:
                    _unlink_dm_file(path)


def _delivery_main(args: list[str]) -> int:
    if len(args) < 3 or args[0] != "--run-delivery":
        return 2
    stdin_file = args[1] == "stdin"
    if not stdin_file and args[1] != "query-file":
        return 2
    dm_file = args[2]
    try:
        return _run_delivery(args[3:], dm_file, stdin_file=stdin_file)
    except Exception as exc:
        # 'target_busy' extends the #93091 item-1 structured refusal enum:
        # the queued delivery gave up after its bounded wait — surface the
        # structured payload on stdout so the completion notification carries
        # it back to the sending agent.
        reason = getattr(exc, "reason", "")
        if reason == "target_busy":
            print(json.dumps({"error": str(exc), "reason": "target_busy"}))
            return 1
        print(
            f"message_agent delivery failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1


# ── agent-context helpers (mirror system_prompt.py's resolution) ─────────────


def _agent_home(agent: Any) -> str:
    """The calling agent's OWN home (session-db derived), not ambient env."""
    try:
        sdb = getattr(agent, "_session_db", None)
        db_path = getattr(sdb, "db_path", None)
        if db_path:
            return str(Path(db_path).parent)
    except Exception:
        pass
    return os.getenv("HERMES_HOME") or os.path.expanduser("~/.hermes")


def _session_title(agent: Any) -> str:
    title = str(getattr(agent, "_session_title_hint", "") or "").strip()
    if title:
        return title
    try:
        sdb = getattr(agent, "_session_db", None)
        sid = getattr(agent, "session_id", None)
        if sdb and sid:
            return str(sdb.get_session_title(sid) or "").strip()
    except Exception:
        pass
    return ""


if __name__ == "__main__":  # pragma: no cover - exercised as a background process
    raise SystemExit(_delivery_main(sys.argv[1:]))
