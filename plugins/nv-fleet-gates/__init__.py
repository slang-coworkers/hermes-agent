"""nv-fleet-gates — the fleet's single ``pre_tool_call`` veto.

Exactly ONE ``pre_tool_call`` callback runs an ordered, block-before-approve
predicate chain and returns the FIRST match:

    canonicalise(tool_name)
      -> SANDBOX          (ISO-F10; inert unless enforce_sandbox)
      -> FLEET-ADMIN      (GOV-F26; orchestrator-only admin tools)
      -> WIRING unwired   (A2A-F18; wired-only peer messaging)
      -> GATES            (host-writer path / plan gate / critique gate + PR
                           invitation — LOOP-F37/F38, ISO-F10 merged)
      -> WIRING approve   (A2A-F19; gated edge -> escalate to the human gate)

Fail-closed is THIS plugin's own obligation: a raising ``pre_tool_call`` callback
is swallowed by core and the tool PROCEEDS (only a timeout fails closed), so the
whole body is wrapped in ``try/except BaseException`` -> block.

Companions: a session-correlated ``codex_critique`` tool (+ ``/codex-critique``
slash alias) records the critique row the GATES consume; ``hermes wire`` writes
the fleet-shared edges store; ``post_tool_call`` records plan creation and
restales the critique on a file mutation; ``pre_gateway_dispatch`` captures the
human PR-comment invitation token.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

from hermes_constants import get_hermes_home

from . import edges, predicates, stores
from .aliases import canonicalise

logger = logging.getLogger(__name__)

PLUGIN_KEY = "nv-fleet-gates"
_TOOLSET = "fleet_gates"

# Core registry-backed tools the gate floors. Each group lists accepted
# spellings (legacy + main) so the load-time presence assertion holds across
# the 2026-09-14 rename cliff (e.g. cronjob -> cronjob_manage).
_CORE_PRESENCE_GROUPS = [
    ("terminal",), ("execute_code",), ("write_file",), ("patch",),
    ("kanban_create",), ("kanban_request_review",), ("kanban_complete",),
    ("kanban_request_changes",), ("skill_manage",),
    ("cronjob", "cronjob_manage"), ("delegate_task",), ("memory",),
]

# Canonical names the gate knows/floors — the tripwire allowlist.
_RESTRICTED_CANON = {
    "terminal", "execute_code", "write_file", "patch",
    "kanban_create", "kanban_request_review", "kanban_complete",
    "kanban_request_changes", "skill_manage", "cronjob_manage",
    "delegate_task", "memory",
}

# Fleet-admin names always orchestrator-only (unioned with settings.admin_tools).
# cronjob_manage is orchestrator-only fleet-wide (stricter than "cron for another
# profile"): a worker owns no cron in this fleet.
_VENDORED_ADMIN = {"cronjob_manage"}

_MENTION_RE = re.compile(r"@\w")

# Raising in register() disables the plugin, removing the veto = fail OPEN, so
# the load-time refusal fires only when the registry is essentially complete (at
# most one restricted group absent — the shape of a rename/removal). A larger
# gap cannot be told apart from a still-initializing registry, so it is not
# raised on; restricted-set enforcement defers to _restricted_block at the gate,
# which fails closed.
_MAX_ABSENT_WHEN_COMPLETE = 1


def _scan_restricted(running):
    """Return (missing restricted-core groups, unclassified shell-capable names).

    ``running`` is the current registry tool-name set. ``message_agent`` is
    injected (never ``registry.register``-ed) so it never appears here; it is
    matched by constant in the gate instead.
    """
    missing = [grp[0] for grp in _CORE_PRESENCE_GROUPS if not any(s in running for s in grp)]
    unclassified = sorted(
        n for n in running
        if predicates.looks_dangerous_name(n) and canonicalise(n) not in _RESTRICTED_CANON
    )
    return missing, unclassified


def _registry_essentially_complete(missing) -> bool:
    """True when the running registry is complete but for a small gap.

    A gap this small (<= _MAX_ABSENT_WHEN_COMPLETE) is treated as a genuine
    rename/removal worth a loud load-time refusal; a larger one cannot be told
    apart from a still-initializing registry (`hermes plugins doctor` and `hermes
    kanban dispatch` see 0/12, an in-process dashboard profile-switch reload
    1/12) and is deferred to the gate-time fail-closed backstop instead.
    """
    return len(missing) <= _MAX_ABSENT_WHEN_COMPLETE

# Request-local (NOT process-global) carrier from the pre_command observer to
# the /codex-critique slash handler in the same dispatch context, so the slash
# alias can correlate to its session without a shared cell that would leak
# across interleaved sessions. Empty when the surfaces do not share a context.
_pending_slash_session: contextvars.ContextVar = contextvars.ContextVar(
    "nv_fleet_gates_slash_session", default=None
)


def _edges_lookup(from_profile: str, to_profile: str, db_path: str):
    """Look up one wiring edge.

    Kept as a bare module-global that the gate calls by name so a fault-injection
    test can rebind ``loaded.module._edges_lookup`` and exercise the fail-closed
    path (AC-GOV-F22-3).
    """
    return edges.lookup(db_path, from_profile, to_profile)


def _block(message: str) -> Dict[str, Any]:
    # Core DROPS a block directive that carries no message (a message-less block
    # is a fail-open), so every block outcome is minted here with a non-empty one.
    return {"action": "block", "message": message or "nv-fleet-gates: blocked"}


def _approve(rule_key: str, message: str) -> Dict[str, Any]:
    return {"action": "approve", "rule_key": rule_key, "message": message}


def _current_profile() -> str:
    # Identity is the profile home the process runs in — NOT a plugin-writable
    # setting — so a worker cannot forge its identity through config.
    try:
        return Path(get_hermes_home()).name
    except Exception:
        return ""


def _managed_profile_roles():
    """The role map the managed layer pins, or None when it does not pin the key.

    Read straight from managed scope, not ``ctx.get_config`` (which returns the
    deep-merged map), so a per-profile addition for an unmanaged profile cannot
    escalate. An EXPLICITLY-empty managed ``profile_roles: {}`` returns ``{}``
    (not None) so it still shuts out the local map — only a genuinely-absent key
    returns None. A non-dict pin is malformed and raises, failing the load
    closed rather than silently falling back to the writable map. Loader errors
    are not swallowed for the same reason.
    """
    from hermes_cli import managed_scope

    mc = managed_scope.load_managed_config() or {}
    settings = (
        ((mc.get("plugins") or {}).get("entries") or {}).get(PLUGIN_KEY) or {}
    ).get("settings") or {}
    if "profile_roles" not in settings:
        return None
    roles = settings["profile_roles"]
    if not isinstance(roles, dict):
        raise RuntimeError("managed nv-fleet-gates profile_roles is not a mapping")
    return roles


def _status_ok(status) -> bool:
    if status is None or status is True:
        return True
    if isinstance(status, str):
        return status.strip().lower() in {"ok", "success", "succeeded", "completed", "done", "0"}
    return False


def _parse_gh_comment(event):
    """Extract (repo, pr, is_human, body) from a webhook issue_comment event.

    The invitation is a session-less webhook signal, so it is keyed (repo, pr) —
    not a session id (there is none on this payload).
    """
    meta = getattr(event, "metadata", None) or {}
    if isinstance(meta, dict) and isinstance(meta.get("gh_pr_invitation"), dict):
        inv = meta["gh_pr_invitation"]
        return inv.get("repo"), inv.get("pr"), bool(inv.get("human")), inv.get("body") or ""
    raw = getattr(event, "raw_message", None) or {}
    if not isinstance(raw, dict) or raw.get("action") != "created":
        return None, None, False, ""
    issue = raw.get("issue") or {}
    if "pull_request" not in issue:  # PR comments only, not plain issues
        return None, None, False, ""
    repo = (raw.get("repository") or {}).get("full_name")
    pr = issue.get("number")
    comment = raw.get("comment") or {}
    stype = (comment.get("user") or {}).get("type") or (raw.get("sender") or {}).get("type")
    return repo, pr, stype == "User", (comment.get("body") or "")


def register(ctx) -> None:
    from .cli import setup_wire

    # --- static per-profile config (config is immutable mid-session) ---
    profile_roles = ctx.get_config("profile_roles", {}) or {}
    enforce_sandbox = bool(ctx.get_config("enforce_sandbox", False))
    expected_backend = ctx.get_config("expected_backend", "docker")
    required_stages = list(ctx.get_config("required_stages", ["OUTPUT_REVIEW"]) or [])
    plan_gate_on = bool(ctx.get_config("plan_gate", True))
    edges_db_path = ctx.get_config("edges_db_path")
    stage_markers = list(ctx.get_config("stage_markers", []) or [])
    admin_tools = set(ctx.get_config("admin_tools", []) or []) | _VENDORED_ADMIN
    skills_root = ctx.get_config("skills_root")
    shared_learnings_path = ctx.get_config("shared_learnings_path")
    # When the managed layer pins the role map, roles come ONLY from it (a
    # profile absent from it defaults to worker) so a per-profile config cannot
    # add itself as orchestrator — ctx.get_config alone returns the deep-merged
    # map, where a per-profile entry for an unmanaged profile would survive.
    managed_profile_roles = _managed_profile_roles()

    # --- load-time restricted-set refusal (best-effort early signal) --------
    # See _registry_essentially_complete: raising here disables the plugin
    # (loaded.error), removing the veto = fail OPEN, so it fires only when the
    # registry is essentially complete. On a still-initializing registry the veto
    # is registered anyway and the check defers to the gate-time backstop
    # (_restricted_block), which fails closed.
    from tools.registry import registry

    _missing0, _unclassified0 = _scan_restricted(set(registry.get_all_tool_names() or []))
    if _registry_essentially_complete(_missing0):
        if _missing0:
            raise RuntimeError(
                "nv-fleet-gates refusing to load: restricted core tools absent from the "
                f"running registry (renamed/removed?): {sorted(_missing0)}"
            )
        # A shell-capable/dangerous-named tool the gate does not floor would let
        # the fleet run un-gated shell. message_agent is injected, never
        # registry.register-ed, so it never appears here (matched by constant).
        if _unclassified0:
            raise RuntimeError(
                "nv-fleet-gates refusing to load: shell-capable/dangerous registry tools are "
                f"not in the gated restricted set: {_unclassified0}"
            )

    # --- role / identity ----------------------------------------------------
    def _role() -> str:
        if managed_profile_roles is not None:
            return managed_profile_roles.get(_current_profile(), "worker")
        return profile_roles.get(_current_profile(), "worker")

    def _is_orch() -> bool:
        return _role() == "orchestrator"

    # --- predicates ---------------------------------------------------------
    def _sandbox_block(canon, args):
        if not enforce_sandbox:
            return None
        backend = os.getenv("TERMINAL_ENV", "local")
        if backend != expected_backend:
            return _block(
                f"sandbox: refused — resolved backend {backend!r} != expected {expected_backend!r}"
            )
        if canon.startswith("mcp_"):
            return _block("sandbox: stdio mcp_* call denied under an enforced container backend")
        import tools.terminal_tool as tt

        if tt.ensure_task_env() is None:
            return _block("sandbox: container task environment is not ready — refusing")
        if canon in ("terminal", "execute_code"):
            if predicates.is_dangerous_command(predicates.command_text(canon, args)):
                return _block(
                    "sandbox: dangerous command refused — the dangerous-command floor is not "
                    "waived inside the sandbox"
                )
        return None

    def _fleet_admin_block(canon, args):
        if _is_orch():
            return None
        if canon in admin_tools:
            return _block(
                f"fleet-admin: {canon} is orchestrator-only; profile {_current_profile()!r} is not "
                "the orchestrator"
            )
        return None

    def _wiring_target(canon, args):
        if not isinstance(args, dict):
            return None
        if canon == "message_agent":
            return args.get("target")
        if canon == "kanban_create":
            return args.get("assignee")
        return None

    def _wiring_block(canon, args):
        to = _wiring_target(canon, args)
        if not to:
            return None
        frm = _current_profile()
        if _edges_lookup(frm, to, edges_db_path) is None:
            mates = edges.wired_teammates(edges_db_path, frm)
            return _block(f"wired-only: {frm} is not wired to {to}; wired teammates: {mates}")
        return None

    def _wiring_approve(canon, args):
        to = _wiring_target(canon, args)
        if not to:
            return None
        frm = _current_profile()
        edge = _edges_lookup(frm, to, edges_db_path)
        if edge is not None and edge.get("gated"):
            return _approve(f"wire:{frm}:{to}", f"gated edge {frm}->{to} requires human approval")
        return None

    def _host_writer_block(canon, args):
        if _is_orch():
            return None
        home = get_hermes_home()
        if canon == "skill_manage":
            base = Path(skills_root) if skills_root else (home / "skills")
            for op in (args.get("operations") or []):
                fp = op.get("file_path") if isinstance(op, dict) else None
                if not fp:
                    continue
                resolved = base / fp
                if shared_learnings_path and predicates.within(resolved, Path(shared_learnings_path)):
                    return _block(f"host-writer: skill_manage target {fp} resolves into the shared-learnings clone")
                if not predicates.within(resolved, base):
                    return _block(f"host-writer: skill_manage target {fp} escapes the caller's skills tree")
            return None
        if canon == "memory":
            store = home / "memories"
            if shared_learnings_path and predicates.within(store, Path(shared_learnings_path)):
                return _block("host-writer: memory store path resolves into the shared-learnings clone")
            if not predicates.within(store, Path(home)):
                return _block("host-writer: memory store path escapes the caller's profile tree")
            return None
        return None

    def _gates_block(canon, args, session_id):
        if canon in ("skill_manage", "memory"):
            return _host_writer_block(canon, args)
        if plan_gate_on and predicates.is_mutation(canon, args):
            path = predicates.target_path(canon, args)
            if not (path and predicates.is_plan_path(path)):  # /plan may write its own plan
                if not stores.has_plan(session_id):
                    return _block(
                        "plan gate: create a plan under .hermes/plans/*.md in this session before "
                        "mutating files"
                    )
        cmd = predicates.command_text(canon, args) if canon in ("terminal", "execute_code") else ""
        gh_egress = predicates.gh_pr_egress(cmd)
        comment_targets = predicates.gh_pr_comment_targets(cmd)
        marked = (
            canon == "message_agent"
            and predicates.is_stage_marked((args or {}).get("message"), stage_markers)
        ) or predicates.is_kanban_terminator(canon)
        if marked or gh_egress:
            if not stores.critique_fresh(session_id, required_stages):
                return _block(
                    f"critique gate: run /codex-critique for stages {required_stages} before this "
                    "delivery"
                )
            # every gh pr comment (any segment) needs its own human invitation;
            # an unparseable target has repo/pr None, which has_invitation denies.
            for repo, pr in comment_targets:
                if not stores.has_invitation(repo, pr):
                    return _block(
                        f"invitation gate: no human invitation for {repo}#{pr}; a human must "
                        "@-mention on the PR first"
                    )
        return None

    # Lazy restricted-set backstop: when register() ran against a still-
    # initializing registry the load-time refusal was skipped (to avoid
    # fail-open), so re-check here on each call and block until the restricted
    # set is complete. A genuine missing-core / unclassified shell tool fails
    # CLOSED (block), never open. Only a clean verdict is cached; once clean the
    # registry does not lose tools mid-session, so later calls skip the re-scan.
    _restricted_checked = {}

    def _restricted_block():
        if _restricted_checked.get("ok"):
            return None
        from tools.registry import registry as _reg

        miss, uncl = _scan_restricted(set(_reg.get_all_tool_names() or []))
        if miss:
            return _block(
                f"nv-fleet-gates: restricted core tools absent at gate time {sorted(miss)} "
                "— refusing (fail-closed)"
            )
        if uncl:
            return _block(
                f"nv-fleet-gates: unclassified shell-capable tools present at gate time {uncl} "
                "— refusing (fail-closed)"
            )
        _restricted_checked["ok"] = True
        return None

    def _gate(tool_name=None, args=None, session_id=None, **kwargs):
        try:
            canon = canonicalise(tool_name or "")
            args = args if isinstance(args, dict) else {}
            backstop = _restricted_block()
            if backstop:
                return backstop
            for predicate in (_sandbox_block, _fleet_admin_block, _wiring_block):
                blocked = predicate(canon, args)
                if blocked:
                    return blocked
            blocked = _gates_block(canon, args, session_id)
            if blocked:
                return blocked
            return _wiring_approve(canon, args)
        except BaseException:  # a raising pre_tool_call is SWALLOWED by core -> tool proceeds; fail closed
            logger.warning("nv-fleet-gates gate errored; failing closed", exc_info=True)
            return _block("nv-fleet-gates internal error — refusing the call (fail-closed)")

    # --- observers ----------------------------------------------------------
    def _observe(tool_name=None, args=None, status=None, result=None, session_id=None, **kwargs):
        try:
            if not session_id or not _status_ok(status):
                return None
            canon = canonicalise(tool_name or "")
            args = args if isinstance(args, dict) else {}
            if predicates.is_mutation(canon, args):
                stores.record_mutation(session_id)  # restales the critique (freshness)
                path = predicates.target_path(canon, args)
                if path and predicates.is_plan_path(path):
                    stores.record_plan(session_id)
        except Exception:
            logger.warning("nv-fleet-gates post_tool_call observer failed", exc_info=True)
        return None

    def _capture_session(surface=None, command=None, session_key=None, session_id=None, **kwargs):
        # Stash this dispatch's session in a request-local ContextVar so the
        # /codex-critique slash handler running in the same context can correlate.
        # ContextVar (not a shared cell) means a different session's dispatch
        # never sees it. The authoritative path remains the codex_critique tool.
        if command in ("codex-critique", "/codex-critique"):
            sid = session_id or session_key
            if sid:
                _pending_slash_session.set(sid)
        return None

    def _capture_invitation(event=None, gateway=None, session_store=None, **kwargs):
        try:
            repo, pr, is_human, body = _parse_gh_comment(event)
            if repo and pr is not None and is_human and bool(_MENTION_RE.search(body or "")):
                stores.record_invitation(repo, pr)
        except Exception:
            logger.warning("nv-fleet-gates pre_gateway_dispatch observer failed", exc_info=True)
        return None

    # --- codex_critique tool + slash alias ----------------------------------
    async def _run_critique(stage: str):
        from agent.plugin_llm import PluginLlm

        llm = PluginLlm(plugin_id=PLUGIN_KEY)
        result = await llm.acomplete_structured(
            instructions=(
                "You are an independent critique gate. Review the work for the named stage and "
                'return a single JSON object {"verdict": "approve"|"must-fix", "stage": <stage>}.'
            ),
            input=[{"type": "text", "text": f"Stage: {stage}. Assess readiness for this delivery."}],
            json_mode=True,
        )
        parsed = getattr(result, "parsed", None) or {}
        return parsed.get("verdict") if isinstance(parsed, dict) else None

    async def _tool_codex_critique(args, **kwargs):
        # session_id comes ONLY from the authoritative dispatch kwargs, never
        # from model-supplied args — otherwise a model could record a critique
        # for another session and unlock its gate.
        session_id = kwargs.get("session_id")
        stage = (args or {}).get("stage") or (required_stages[0] if required_stages else "OUTPUT_REVIEW")
        try:
            verdict = await _run_critique(stage)
            if session_id:
                # A critique HAVING RUN (not its verdict) is what the gate requires,
                # porting NanoClaw's edits_since_critique==0 invariant.
                stores.record_critique(session_id, stage)
        except Exception as exc:
            logger.warning("codex_critique failed", exc_info=True)
            return json.dumps({"ok": False, "error": str(exc), "session_id": session_id})
        return json.dumps({"ok": True, "session_id": session_id, "stage": stage, "verdict": verdict})

    def _check_codex_critique(*_a, **_k) -> bool:
        return True  # every session may run its own critique

    async def _slash_codex_critique(raw_args: str = "", **kwargs):
        stage = (raw_args or "").strip().split()[0] if (raw_args or "").strip() else (
            required_stages[0] if required_stages else "OUTPUT_REVIEW"
        )
        # Authoritative session id only: dispatch kwargs, else the request-local
        # ContextVar the pre_command observer set for this same dispatch.
        session_id = kwargs.get("session_id") or kwargs.get("session_key") or _pending_slash_session.get()
        try:
            verdict = await _run_critique(stage)
            if not session_id:
                # No session to attribute the critique to — report honestly
                # rather than claim success while recording nothing.
                return json.dumps({
                    "ok": False,
                    "error": "no session context for /codex-critique; invoke the codex_critique tool",
                    "stage": stage, "verdict": verdict,
                })
            stores.record_critique(session_id, stage)
            return json.dumps({"ok": True, "session_id": session_id, "stage": stage, "verdict": verdict})
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        finally:
            # Consume the request-local hint even on failure so it can't leak.
            _pending_slash_session.set(None)

    CODEX_CRITIQUE_SCHEMA = {
        "description": (
            "Run an independent critique for the current session and record the critique row the "
            "delivery/egress gate consumes. Correlated by session_id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "stage": {
                    "type": "string",
                    "description": "Critique stage (e.g. OUTPUT_REVIEW, CODE_REVIEW, PLAN_REVIEW).",
                },
            },
        },
    }

    def _cli_wire(ns, **_kwargs):
        cmd = getattr(ns, "wire_command", None)
        if cmd == "add":
            gated = bool(getattr(ns, "gated", False))
            edges.add_edge(edges_db_path, ns.frm, ns.to, gated=gated)
            out = {"ok": True, "action": "add", "from": ns.frm, "to": ns.to, "gated": gated}
            print(json.dumps(out))
            return out
        if cmd == "remove":
            edges.remove_edge(edges_db_path, ns.frm, ns.to)
            out = {"ok": True, "action": "remove", "from": ns.frm, "to": ns.to}
            print(json.dumps(out))
            return out
        if cmd == "list":
            rows = edges.list_edges(edges_db_path)
            print(json.dumps({"edges": rows}))
            return rows
        print("usage: hermes wire {add <from> <to> [--gated]|remove <from> <to>|list}")
        return 2

    # --- registration -------------------------------------------------------
    ctx.register_hook("pre_tool_call", _gate)
    ctx.register_hook("post_tool_call", _observe)
    ctx.register_hook("pre_command", _capture_session)
    ctx.register_hook("pre_gateway_dispatch", _capture_invitation)
    ctx.register_tool(
        name="codex_critique", toolset=_TOOLSET, schema=CODEX_CRITIQUE_SCHEMA,
        handler=_tool_codex_critique, check_fn=_check_codex_critique, is_async=True,
        description="Record a session-correlated critique the delivery/egress gate consumes.",
    )
    ctx.register_command(
        name="codex-critique", handler=_slash_codex_critique,
        description="Run a critique for this session and record the gate row.",
        args_hint="<stage>",
    )
    ctx.register_cli_command(
        name="wire", help="Administer fleet-shared wiring edges",
        setup_fn=setup_wire, handler_fn=_cli_wire,
        description="Add/remove/list bidirectional wiring edges in the fleet-shared edges store.",
    )
