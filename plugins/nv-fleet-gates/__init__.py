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
    "process_manage", "delegate_task", "memory",
}

# Fleet-admin names always orchestrator-only (unioned with settings.admin_tools).
# cronjob_manage is orchestrator-only fleet-wide (stricter than "cron for another
# profile"): a worker owns no cron in this fleet.
_VENDORED_ADMIN = {"cronjob_manage"}

_MENTION_RE = re.compile(r"@\w")


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

    # --- LOAD-TIME RESTRICTED-SET ASSERTION ---------------------------------
    # A raise here makes discover_and_load record loaded.error and leave the
    # plugin disabled (fail-LOAD, not fail-open).
    from tools.registry import registry

    running = set(registry.get_all_tool_names() or [])
    # Only meaningful against a POPULATED builtin registry. `hermes plugins
    # doctor` calls register() with an empty registry (builtins undiscovered),
    # so gate on "at least one core group present" to avoid a false fail-load.
    populated = any(any(s in running for s in grp) for grp in _CORE_PRESENCE_GROUPS)
    if populated:
        missing = [grp[0] for grp in _CORE_PRESENCE_GROUPS if not any(s in running for s in grp)]
        if missing:
            raise RuntimeError(
                "nv-fleet-gates refusing to load: restricted core tools absent from the "
                f"running registry (renamed/removed?): {sorted(missing)}"
            )
    # A shell-capable/dangerous-named registry tool that the gate does not floor
    # would let the fleet run un-gated shell. message_agent is the one documented
    # exclusion — it is injected, never registry.register-ed, so it never appears
    # here and is matched by constant instead.
    unclassified = sorted(
        n for n in running
        if predicates.looks_dangerous_name(n) and canonicalise(n) not in _RESTRICTED_CANON
    )
    if unclassified:
        raise RuntimeError(
            "nv-fleet-gates refusing to load: shell-capable/dangerous registry tools are "
            f"not in the gated restricted set: {unclassified}"
        )

    # --- role / identity ----------------------------------------------------
    def _role() -> str:
        # Managed profile_roles is authoritative; a per-profile role setting is
        # ignored, so it cannot escalate a worker.
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
        if _is_orch():  # orchestrator is exempt from the host-writer path check
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
        gh = (
            predicates.parse_gh_pr(predicates.command_text(canon, args))
            if canon in ("terminal", "execute_code")
            else None
        )
        marked = (
            canon == "message_agent"
            and predicates.is_stage_marked((args or {}).get("message"), stage_markers)
        ) or predicates.is_kanban_terminator(canon)
        if marked or gh is not None:
            if not stores.critique_fresh(session_id, required_stages):
                return _block(
                    f"critique gate: run /codex-critique for stages {required_stages} before this "
                    "delivery"
                )
            if gh is not None and gh.verb == "comment":
                if not stores.has_invitation(gh.repo, gh.pr):
                    return _block(
                        f"invitation gate: no human invitation for {gh.repo}#{gh.pr}; a human must "
                        "@-mention the bot on the PR first"
                    )
        return None

    def _gate(tool_name=None, args=None, session_id=None, **kwargs):
        try:
            canon = canonicalise(tool_name or "")
            args = args if isinstance(args, dict) else {}
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

    _last_slash_session = {"key": None}

    def _capture_session(surface=None, command=None, session_key=None, **kwargs):
        # Best-effort slash correlation: the authoritative critique path is the
        # codex_critique TOOL (session_id in kwargs); this only helps the gateway
        # /codex-critique alias bind to a session.
        if command in ("codex-critique", "/codex-critique") and session_key:
            _last_slash_session["key"] = session_key
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
        session_id = kwargs.get("session_id") or (args or {}).get("session_id")
        stage = (args or {}).get("stage") or (required_stages[0] if required_stages else "OUTPUT_REVIEW")
        try:
            verdict = await _run_critique(stage)
        except Exception as exc:
            logger.warning("codex_critique LLM lane failed", exc_info=True)
            return json.dumps({"ok": False, "error": str(exc), "session_id": session_id})
        if session_id:
            # A critique HAVING RUN (not its verdict) is what the gate requires,
            # porting NanoClaw's edits_since_critique==0 invariant.
            stores.record_critique(session_id, stage)
        return json.dumps({"ok": True, "session_id": session_id, "stage": stage, "verdict": verdict})

    def _check_codex_critique(*_a, **_k) -> bool:
        return True  # every session may run its own critique

    async def _slash_codex_critique(raw_args: str = "", **kwargs):
        stage = (raw_args or "").strip().split()[0] if (raw_args or "").strip() else (
            required_stages[0] if required_stages else "OUTPUT_REVIEW"
        )
        session_id = kwargs.get("session_id") or _last_slash_session.get("key")
        try:
            verdict = await _run_critique(stage)
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        if session_id:
            stores.record_critique(session_id, stage)
        return json.dumps({"ok": True, "session_id": session_id, "stage": stage, "verdict": verdict})

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

    # --- registration (assertion has already passed) ------------------------
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
