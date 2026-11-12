"""nv-fleet-gates — the fleet's single ``pre_tool_call`` veto.

Exactly ONE ``pre_tool_call`` callback runs an ordered, block-before-approve
predicate chain and returns the FIRST match:

    canonicalise(tool_name)
      -> APPROVAL         (PR approvals and merges are human-only, every profile)
      -> early_veto       (product hook; none registered = no check)
      -> SANDBOX          (ISO-F10; inert unless enforce_sandbox)
      -> FLEET-ADMIN      (GOV-F26; orchestrator-only admin tools)
      -> WIRING unwired   (A2A-F18; wired-only peer messaging)
      -> MCP-SCOPE        (GOV-F27; mcp_* absent from the profile's rendered
                           allow-list denied, transport-aware, fail-closed)
      -> CRON-DELIVERY    (off unless cron_delivery_guard: a cron create/update/run
                           posts to Slack only where the slack_post allow-list
                           lets it, never with a broadcast mention — cron_delivery.py)
      -> policy_veto      (product hook; none registered = no check)
      -> OUTBOUND         (off unless chain.outbound_policy: no card, no outbound
                           write; the orchestrator, card-less, only replies in its
                           own conversation or posts a top-level status to its
                           allow-listed channel — outbound.py)
      -> delivery_veto    (product hook; none registered = no check)
      -> GATES            (host-writer path / plan gate / critique gate + PR
                           invitation — LOOP-F37/F38, ISO-F10 merged)
      -> WIRING approve   (A2A-F19; gated edge -> escalate to the human gate)

Fail-closed is THIS plugin's own obligation: a raising ``pre_tool_call`` callback
is swallowed by core and the tool PROCEEDS (only a timeout fails closed), so the
whole body is wrapped in ``try/except BaseException`` -> block.

Product hooks (product_hooks.py): a plugin that declares ``requires_plugins:
[nv-fleet-gates]`` registers callbacks at named points through the module
published as ``nv_fleet_gates_product_hooks``, keyed by this profile's home. With
none registered every point takes its stated default, which is this plugin's
behaviour on its own.

Companions: a session-correlated ``codex_critique`` tool (+ ``/codex-critique``
slash alias) records the critique row the GATES consume; ``hermes wire`` writes
the fleet-shared edges store; ``post_tool_call`` records plan creation and
restales the critique on a file mutation or a command that may change the tree;
``pre_gateway_dispatch`` captures the human PR-comment invitation token and the
item each webhook delivery is about (conversation_origin.py);
``transform_tool_result`` and ``transform_terminal_output`` replace any OpenShell
credential placeholder with ``[OpenShell placeholder]`` (placeholder_redact.py).
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home, get_process_hermes_home, hermes_home_key

from . import (commands, conversation_origin, cron_delivery, edges, outbound, placeholder_redact, predicates,
               product_hooks, stores)
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


def _lookup_cron_job(ref: str):
    """The stored cron job an update/run names (by id or name), else None.

    A bare module-global (like ``_edges_lookup``) so a test can rebind it. A
    lookup error propagates to the gate's fail-closed handler.
    """
    from cron.jobs import resolve_job_ref

    return resolve_job_ref(ref) if ref else None


def _slack_home_chat() -> str:
    """The Slack home channel the cron scheduler resolves ``all``/``slack``/no-origin to.

    Hermes >= 0.22 keeps the resolver in ``cron.scheduler_delivery`` (env var, then the
    config.yaml ``home_channel``); older trees had it in ``cron.scheduler``.
    """
    try:
        from cron.scheduler_delivery import _get_home_target_chat_id
    except ImportError:
        try:
            from cron.scheduler import _get_home_target_chat_id
        except ImportError:
            return os.getenv("SLACK_HOME_CHANNEL", "")
    return str(_get_home_target_chat_id("slack") or "")


def _cron_session_facts() -> dict[str, Any]:
    """The live conversation of this tool call, as the cron tool itself reads it.

    ``cron_creator`` is set only inside a cron run, where core stores the run's
    own target for ``origin`` (tools/cronjob_tools.py _resolve_cron_context_deliver).
    """
    from gateway.session_context import get_session_env

    facts: dict[str, Any] = {
        "platform": get_session_env("HERMES_SESSION_PLATFORM", ""),
        "chat_id": get_session_env("HERMES_SESSION_CHAT_ID", ""),
        "thread_id": get_session_env("HERMES_SESSION_THREAD_ID", ""),
        "user_id": get_session_env("HERMES_SESSION_USER_ID", ""),
        "cron_creator": None,
    }
    if str(get_session_env("HERMES_CRON_SESSION", "")).strip().lower() in {"1", "true", "yes", "on"}:
        facts["cron_creator"] = cron_delivery.cron_creator_target(
            get_session_env("HERMES_CRON_AUTO_DELIVER_PLATFORM", "").strip(),
            get_session_env("HERMES_CRON_AUTO_DELIVER_CHAT_ID", "").strip(),
            get_session_env("HERMES_CRON_AUTO_DELIVER_THREAD_ID", "").strip(),
        )
    return facts


def _owned_card(profile: str) -> str:
    """The card this execution owns, else "".

    Identity comes only from the dispatcher's context: ``owned_kanban_task()`` (``HERMES_KANBAN_TASK`` for a
    dispatcher-owned worker; a delegated child or an in-worker cron run owns none) together with
    ``HERMES_KANBAN_RUN_ID`` and ``HERMES_KANBAN_CLAIM_LOCK``, which the dispatcher sets with it (the worker process
    environment, not anything the sandbox can change). The board must agree on all of it: the card is assigned to
    *profile*, ``running``, on that run and that claim lock. Anything missing or different is no card. (An expired
    ``claim_expires`` alone is not checked: the dispatcher extends a live worker's expired claim rather than reclaim
    it, and a real reclaim or reassignment changes the run id and claim lock, which are.)

    This is checked when the tool call starts. A card completed, cancelled or reassigned while a long command runs is
    not seen: a pre-tool gate cannot close that window (outbound.py, "Limits"; the follow-up is a run lease checked
    at egress).

    A bare module-global (like ``_edges_lookup``) so a test can rebind it. A board error propagates; the caller
    treats it as "no card" (fail closed).
    """
    from agent.delegation_context import owned_kanban_task

    task_id = owned_kanban_task()
    run = (os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip()
    lock = (os.environ.get("HERMES_KANBAN_CLAIM_LOCK") or "").strip()
    if not (task_id and run and lock and profile):
        return ""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect

    conn = connect()
    try:
        task = kb.get_task(conn, task_id)
    finally:
        conn.close()
    if (task is None or task.status != "running" or (task.assignee or "") != profile
            or task.current_run_id is None or str(task.current_run_id) != run or (task.claim_lock or "") != lock):
        return ""
    return task_id


def _room_session(session_id, platform: str, home: Optional[str] = None) -> str:
    """``"room"`` / ``"unknown"`` / ``"no"``: whether this session is a hosted group room, from the
    ``room_session_kind`` product hook of ``home`` (default: the active profile home). None registered: ``"no"``."""
    home = str(Path(home).resolve()) if home is not None else str(get_hermes_home().resolve())
    return product_hooks.call("room_session_kind", home=home, default="no", session_id=session_id, platform=platform)


def _outbound_session() -> dict[str, Any]:
    """The live conversation of this tool call, and the target a cron run delivers to."""
    from gateway.session_context import get_session_env

    session: dict[str, Any] = {
        "platform": get_session_env("HERMES_SESSION_PLATFORM", "").strip().lower(),
        "chat_id": get_session_env("HERMES_SESSION_CHAT_ID", "").strip(),
        "thread_id": get_session_env("HERMES_SESSION_THREAD_ID", "").strip(),
        "user_id": get_session_env("HERMES_SESSION_USER_ID", "").strip(),
        "cron_target": None,
    }
    if str(get_session_env("HERMES_CRON_SESSION", "")).strip().lower() in {"1", "true", "yes", "on"}:
        session["cron_target"] = (get_session_env("HERMES_CRON_AUTO_DELIVER_PLATFORM", "").strip(),
                                  get_session_env("HERMES_CRON_AUTO_DELIVER_CHAT_ID", "").strip(),
                                  get_session_env("HERMES_CRON_AUTO_DELIVER_THREAD_ID", "").strip())
    return session


def _block(message: str) -> dict[str, Any]:
    # Core DROPS a block directive that carries no message (a message-less block
    # is a fail-open), so every block outcome is minted here with a non-empty one.
    return {"action": "block", "message": message or "nv-fleet-gates: blocked"}


def _approve(rule_key: str, message: str) -> dict[str, Any]:
    return {"action": "approve", "rule_key": rule_key, "message": message}


def _kanban_tenants(task_ids, board=None) -> dict[str, Optional[str]]:
    """``{task id: tenant}`` read from the kanban board the call names (None: no tenant or no such card)."""
    if not task_ids:
        return {}
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    with kbc.connect_closing(board=board) as conn:
        out: dict[str, Optional[str]] = {}
        for tid in task_ids:
            task = kb.get_task(conn, tid)
            out[tid] = task.tenant if task is not None else None
        return out


def _kanban_task() -> str:
    """The kanban task this execution runs (HERMES_KANBAN_TASK, from the process or the session env), else ""."""
    task = os.environ.get("HERMES_KANBAN_TASK", "")
    if not task:
        try:
            from gateway.session_context import get_session_env
            task = get_session_env("HERMES_KANBAN_TASK", "") or ""
        except ImportError:
            task = ""
    return str(task)


def _current_profile() -> str:
    # Identity is the ACTIVE per-turn profile home (get_hermes_home's context-local
    # override), NOT a plugin-writable setting, so a worker cannot forge its identity
    # through config. Distinct from the launch profile (get_process_hermes_home), which
    # the SANDBOX cross-profile check compares against.
    try:
        return Path(get_hermes_home()).name
    except Exception:
        logger.debug("_current_profile failed", exc_info=True)
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


def _managed_expected_ssh_host():
    """Return the managed per-profile SSH host map, or ``None`` when absent.

    Read it on every gate call so local profile config cannot authorize a target.
    Invalid maps raise and are converted to a block by ``_gate``.
    """
    from hermes_cli import managed_scope

    mc = managed_scope.load_managed_config() or {}
    settings = (
        ((mc.get("plugins") or {}).get("entries") or {}).get(PLUGIN_KEY) or {}
    ).get("settings") or {}
    if "expected_ssh_host" not in settings:
        return None
    hosts = settings["expected_ssh_host"]
    if not isinstance(hosts, dict):
        raise RuntimeError("managed nv-fleet-gates expected_ssh_host is not a mapping")
    return hosts


def _str_list(value, key: str) -> list:
    """A list-of-strings setting; anything else is ignored with a warning (never a raise in register())."""
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return list(value)
    if value not in (None, "", [], ()):
        logger.warning("nv-fleet-gates: %s is not a list of strings; ignored", key)
    return []


def _str_setting(value, key: str) -> str:
    if isinstance(value, str):
        return value
    if value is not None:
        logger.warning("nv-fleet-gates: %s is not a string; ignored", key)
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


_CRITIQUE_VERDICTS = ("approve", "must-fix")
_FINDING_FIELDS = ("location", "problem", "fix")
_CRITIQUE_INSTRUCTIONS = (
    "You are an independent critique gate. Review the task and every artifact below for the "
    "named stage. Return a single JSON object "
    '{{"verdict": "approve"|"must-fix", "stage": "{stage}", '
    '"findings": [{{"location": str, "problem": str, "fix": str}}]}}. '
    "A must-fix carries at least one finding; an approve may list advisory findings."
)


def _critique_artifacts(raw) -> list:
    """Keep the ``{name, content}`` entries that carry text content.

    The reviewer runs in the gateway, outside the caller's sandbox, so a path
    alone is not something it can read.
    """
    out = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict) and isinstance(item.get("content"), str):
            out.append({"name": str(item.get("name") or "artifact"), "content": item["content"]})
    return out


def _validate_critique(parsed, stage):
    """``(verdict, findings)`` for a well-formed reply to ``stage``, else None."""
    if not isinstance(parsed, dict) or parsed.get("stage") != stage:
        return None
    verdict = parsed.get("verdict")
    findings = parsed.get("findings")
    if verdict not in _CRITIQUE_VERDICTS or not isinstance(findings, list):
        return None
    for finding in findings:
        if not isinstance(finding, dict) or not all(
            isinstance(finding.get(k), str) and finding[k].strip() for k in _FINDING_FIELDS
        ):
            return None
    if verdict == "must-fix" and not findings:
        return None
    return verdict, findings


def register(ctx) -> None:
    from .cli import setup_wire

    # --- static per-profile config (config is immutable mid-session) ---
    profile_roles = ctx.get_config("profile_roles", {}) or {}
    enforce_sandbox = bool(ctx.get_config("enforce_sandbox", False))
    expected_backend = ctx.get_config("expected_backend", "docker")
    required_stages = list(ctx.get_config("required_stages", ["OUTPUT_REVIEW"]) or [])
    plan_gate_on = bool(ctx.get_config("plan_gate", True))
    edges_db_path = edges.resolve_db_path(ctx.get_config("edges_db_path"))
    stage_markers = list(ctx.get_config("stage_markers", []) or [])
    admin_tools = set(ctx.get_config("admin_tools", []) or []) | _VENDORED_ADMIN
    skills_root = ctx.get_config("skills_root")
    shared_learnings_path = ctx.get_config("shared_learnings_path")
    # GOV-F27: the full profile-keyed MCP allow-list the compose render writes
    # into every profile ({profile: {sanitized mcp__srv__tool: transport}}); the
    # predicate selects this profile's entry by _current_profile().
    mcp_scope = ctx.get_config("mcp_scope", {}) or {}
    # The per-profile Slack post allow-list ({profile: {channels, users, own_threads_only, thread_reply_only}}),
    # operator-rendered: the cron delivery guard and the OUTBOUND status post read it.
    slack_post_settings = ctx.get_config("slack_post", {}) or {}
    # Three checks that are OFF unless the fleet turns them on: OUTBOUND (outbound.py: no card, no outbound write),
    # the cron delivery guard (cron_delivery.py), and a critique for a push to a matching remote. chain.
    # card_creator_profiles names the PROFILES that may kanban_create from a hosted group room (default: none).
    _chain_cfg = ctx.get_config("chain", {}) or {}
    _chain_cfg = _chain_cfg if isinstance(_chain_cfg, dict) else {}
    outbound_on = _chain_cfg.get("outbound_policy") not in (None, False)
    _creators = _chain_cfg.get("card_creator_profiles") or []
    card_creator_profiles = frozenset(
        str(p) for p in (_creators if isinstance(_creators, (list, tuple)) else []) if isinstance(p, str) and p)
    cron_guard_on = ctx.get_config("cron_delivery_guard", False) not in (None, False)
    # The forge CLIs whose calls are forge writes, the review bot whose mention asks for a review, and the push
    # remotes that need a critique: none by default. A malformed value is ignored (with a warning), never a raise.
    commands.configure(_str_list(ctx.get_config("forge_tools", []), "forge_tools"))
    conversation_origin.configure(_str_setting(ctx.get_config("review_bot", ""), "review_bot"))
    outbound.configure(_str_list(ctx.get_config("critique_push_remotes", []), "critique_push_remotes"))
    # A malformed value must not raise here: register() failing disables the veto.
    try:
        critique_max_chars = int(ctx.get_config("critique_max_chars", 120000))
    except (TypeError, ValueError):
        logger.warning("nv-fleet-gates: invalid critique_max_chars; using 120000")
        critique_max_chars = 120000
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

    # The product-hook registry key; captured once, never re-read from the environment at call time.
    hook_home = str(get_hermes_home().resolve())
    product_hooks._publish_profile(ctx, home=hook_home, profile=_current_profile(), is_orchestrator=_is_orch())

    # --- predicates ---------------------------------------------------------
    _BOARD_REPORT_TOOLS = frozenset({"kanban_show", "kanban_heartbeat", "kanban_comment", "kanban_block"})

    def _sandbox_block(canon, args):
        # message_agent's delivery runs host-local (bot_mode_dm.py `_host_local=True`) as a
        # control-plane child that never consumes the active profile's frozen terminal.* config,
        # so it is not a sandbox-consuming call: exempt it from the ENTIRE sandbox predicate
        # (the cross-profile, backend and task-env checks all target sandbox-consuming tools).
        # Without this, ISO-F10.a's cross-profile refusal below fires for a served (non-launch)
        # profile's message_agent coordination turn (active != launch) and the fleet cannot
        # coordinate on one gateway. _wiring_block still gates message_agent to WIRED targets.
        if not enforce_sandbox or canon == "message_agent":
            return None
        # A non-launch gateway turn is sandboxed only through the per-turn terminal scope
        # (tools/terminal_scope.py), which the gateway binds from the same profile home as the
        # home override. With no scope bound, TERMINAL_* is the LAUNCH profile's process-global
        # config, so refuse before any TERMINAL_* value is trusted. Read live (never captured
        # at register(), which re-runs on a force reload); get_process_hermes_home is
        # override-immune so it stays the launch scope even inside a per-turn override.
        active = get_hermes_home()
        launch = get_process_hermes_home()
        # Read TERMINAL_* through the scope in both branches: a kanban worker runs AS its profile
        # (active == launch) with TERMINAL_* stripped from its env, so its policy exists only in the
        # per-turn scope. With no scope bound terminal_env is the plain process-env read.
        from tools.terminal_scope import (TerminalPolicyRefusal, TerminalPolicyUnavailable, get_terminal_scope,
                                          terminal_env)

        if hermes_home_key(active) != hermes_home_key(launch):
            scope = get_terminal_scope()
            if isinstance(scope, TerminalPolicyRefusal):
                return _block(
                    f"sandbox: refused — the terminal policy of {Path(active).name!r} could not be "
                    f"resolved for this turn ({scope.reason})"
                )
            if scope is None:
                return _block(
                    f"sandbox: cross-profile refused — active profile {Path(active).name!r} is not "
                    f"the launch profile {Path(launch).name!r}; the process-global terminal.* config "
                    f"belongs to {Path(launch).name!r}, so this gateway turn cannot be sandboxed for "
                    f"{Path(active).name!r} (tool-bearing work runs on the kanban board)"
                )
        try:
            backend = terminal_env("TERMINAL_ENV", "local")
            resolved = terminal_env("TERMINAL_SSH_HOST", "")
        except TerminalPolicyUnavailable:
            return _block("sandbox: refused — this profile's terminal policy is unavailable")
        if backend != expected_backend:
            return _block(
                f"sandbox: refused — resolved backend {backend!r} != expected {expected_backend!r}"
            )
        # Check the managed host before readiness because ensure_task_env()
        # eagerly connects to and caches TERMINAL_SSH_HOST.
        if expected_backend == "ssh":
            expected = (_managed_expected_ssh_host() or {}).get(_current_profile())
            if not expected or resolved != expected:
                return _block(
                    f"sandbox: refused — ssh target host {resolved!r} is not this "
                    f"profile's sandbox {expected!r}"
                )
        # A worker whose sandbox is down must still be able to say so. These board tools read its card or record a
        # heartbeat, a comment or a block in the kanban DB from this process; none of them runs in the sandbox.
        # Every later predicate (OUTBOUND's own-card rule included) still applies; kanban_complete and the other
        # terminators still need a ready environment.
        if canon in _BOARD_REPORT_TOOLS:
            return None
        import tools.terminal_tool as tt

        if tt.ensure_task_env() is None:
            return _block("sandbox: task environment is not ready — refusing")
        if canon in ("terminal", "execute_code"):
            if predicates.is_dangerous_command(predicates.command_text(canon, args)):
                return _block(
                    "sandbox: dangerous command refused — the dangerous-command floor is not "
                    "waived inside the sandbox"
                )
        return None

    def _mcp_scope_block(canon, args):
        # GOV-F27: deny any mcp_* tool absent from THIS profile's rendered
        # allow-list (fail-closed), so a bot widening its own config buys nothing
        # — the policy is keyed by the process identity, not a value it can raise.
        if not canon.startswith("mcp_"):
            return None
        scope = mcp_scope.get(_current_profile()) if isinstance(mcp_scope, dict) else None
        reason = predicates.mcp_scope_reason(canon, scope, enforce_sandbox)
        return _block(reason) if reason else None

    def _cron_delivery_block(canon, args):
        # RT-11b: a cron job's output is posted by the gateway, so its deliver target and
        # prompt follow the per-profile slack_post allow-list and broadcast refusal.
        if not cron_guard_on or canon != cron_delivery.CRON_TOOL:
            return None
        stored = None
        if cron_delivery.needs_stored_job(args):
            stored = _lookup_cron_job(str(args.get("job_id") or "").strip())
        profile = _current_profile()
        own = frozenset()
        entry = slack_post_settings.get(profile) if isinstance(slack_post_settings, dict) else None
        if isinstance(entry, dict) and entry.get("own_threads_only"):
            own = frozenset(product_hooks.call("own_post_threads", home=hook_home, default=frozenset(),
                                               profile=profile, env=os.environ))
        reason = cron_delivery.reason(
            args, profile=profile, settings=slack_post_settings,
            session=_cron_session_facts(), home=_slack_home_chat(), stored_job=stored, own_threads=own,
        )
        return _block(reason) if reason else None

    def _outbound_facts(session_id) -> outbound.Facts:
        profile = _current_profile()
        session = _outbound_session()
        try:
            card = _owned_card(profile)
        except Exception as exc:  # the board cannot be read: no card (fail closed)
            logger.warning("nv-fleet-gates outbound policy: card lookup failed (%s); treating as no card",
                           type(exc).__name__, exc_info=True)
            card = ""
        # A room never holds a card. A session that cannot be placed counts as a room unless it owns a verified
        # card (a card-less unknown session is refused anyway; a dispatcher-bound worker is not a room).
        placed = _room_session(session_id, session["platform"], home=hook_home)
        room = placed == "room" or (placed == "unknown" and not card)

        def own_item(project, kind, iid) -> bool:
            # The item this webhook session was asked about (conversation_origin.py); unreadable = not own.
            try:
                return conversation_origin.is_own_gitlab_item(project, kind, iid)
            except Exception:
                logger.warning("nv-fleet-gates outbound policy: origin lookup failed", exc_info=True)
                return False

        status_channels = cron_delivery.allowed_targets(slack_post_settings, profile)["channels"]

        def own(write) -> bool:
            # its own conversation (Slack thread / DM / cron target, or the item it was asked about), or a
            # top-level status post to a channel on its own slack_post allow-list
            return (outbound.own_slack(write, session) or outbound.own_gitlab(write, own_item)
                    or outbound.status_post(write, status_channels))

        return outbound.Facts(profile, _is_orch(), "" if room else card, room, own)

    def _outbound_audit(canon, write_label, rule, facts) -> None:
        logger.warning("AUDIT nv-fleet-gates outbound-policy: refused tool=%s profile=%s write=%s rule=%s "
                       "card=%s room=%s", canon, facts.profile or "-", write_label, rule,
                       "yes" if facts.card else "no", "yes" if facts.room else "no")

    def _outbound_block(canon, args, session_id):
        # No card, no outbound write (outbound.py). Every profile, the orchestrator included; reads and sandbox
        # edits never reach it. Facts are gathered only for a call that writes outward (or kanban_create).
        if not outbound_on:
            return None
        writes = outbound.classify(canon, args, home=hook_home)
        if not writes and canon != outbound.KANBAN_CREATE:
            return None
        facts = _outbound_facts(session_id)
        refusal = outbound.room_create_refusal(canon, facts, card_creator_profiles)
        if refusal:
            _outbound_audit(canon, "kanban:kanban_create", "room-card-create", facts)
            return _block(refusal)
        decision = outbound.decide(writes, facts)
        if decision is None:
            return None
        write, rule, message = decision
        _outbound_audit(canon, write.label(), rule, facts)
        return _block(message)

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
        writes = outbound.classify(canon, args, home=hook_home)
        if any(outbound.critique_egress(w) and w.inline for w in writes):
            return _block(
                "critique gate: forge text must come from a file you wrote BEFORE the critique (--body-file / "
                "--description-file <path>, not --body / --description / '-'): write it with write_file, run "
                "/codex-critique on it, then post that file. Inline text is not what was critiqued."
            )
        if marked or gh_egress or any(outbound.critique_egress(w) for w in writes):
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

    def _approval_block(canon, args):
        # Owner rule, ahead of every other predicate and without a role exemption, so no earlier refusal or
        # orchestrator allowance can stand in for it.
        if canon not in ("terminal", "execute_code"):
            return None
        kind = predicates.pr_approval_or_merge(predicates.command_text(canon, args))
        if kind:
            return _block(
                f"approval gate: PR approvals and merges are human-only (owner rule); {kind} refused for every "
                "profile"
            )
        return None

    def _veto(point, canon, args, session_id, task_id):
        # A product hook's refusal, wrapped like every other block. A raising or out-of-contract veto comes back as
        # a refusal (product_hooks.call), so it blocks too.
        profile = _current_profile()
        facts = {"tool_name": canon, "args": args, "session_id": session_id, "task_id": task_id, "profile": profile}
        if point != "delivery_veto":
            facts.update(is_orchestrator=_is_orch(), kanban_task=_kanban_task())
        if point == "policy_veto":
            facts.update(origin=conversation_origin.current, owned_card=lambda: _owned_card(profile),
                         kanban_tenants=_kanban_tenants, writes=lambda: outbound.classify(canon, args, home=hook_home))
        reason = product_hooks.call(point, home=hook_home, default=None, **facts)
        return _block(reason) if reason else None

    def _gate(tool_name=None, args=None, session_id=None, task_id=None, **kwargs):
        try:
            canon = canonicalise(tool_name or "")
            args = args if isinstance(args, dict) else {}
            backstop = _restricted_block()
            if backstop:
                return backstop
            blocked = _approval_block(canon, args)
            if blocked:
                return blocked
            blocked = _veto("early_veto", canon, args, session_id, task_id)
            if blocked:
                return blocked
            for predicate in (_sandbox_block, _fleet_admin_block, _wiring_block, _mcp_scope_block,
                              _cron_delivery_block):
                blocked = predicate(canon, args)
                if blocked:
                    return blocked
            blocked = _veto("policy_veto", canon, args, session_id, task_id)
            if blocked:
                return blocked
            blocked = _outbound_block(canon, args, session_id)
            if blocked:
                return blocked
            blocked = _veto("delivery_veto", canon, args, session_id, task_id)
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
    def _observe(tool_name=None, args=None, status=None, result=None, session_id=None, task_id=None, **kwargs):
        try:
            if not session_id:
                return
            canon = canonicalise(tool_name or "")
            args = args if isinstance(args, dict) else {}
            # Critique freshness, beyond the plan gate's file mutations: a command that changes the tree or runs a
            # program (git rebase / merge / pull / cherry-pick / am / revert, make, a script) restales the critique,
            # failed or not (a failed rebase can leave the tree changed).
            if predicates.restales_critique(canon, args):
                stores.record_mutation(session_id)
            if not _status_ok(status):
                return
            if predicates.is_mutation(canon, args):
                stores.record_mutation(session_id)  # restales the critique (freshness)
                path = predicates.target_path(canon, args)
                if path and predicates.is_plan_path(path):
                    stores.record_plan(session_id)
        except Exception:
            logger.warning("nv-fleet-gates post_tool_call observer failed", exc_info=True)
        return

    def _transform_tool_result(**kwargs):
        """transform_tool_result for every profile: the ``tool_result_rewrite`` product hook, then the OpenShell
        placeholder redaction on the final text (placeholder_redact.py). Hermes keeps only the FIRST string any
        transform_tool_result callback returns (model_tools.py _apply_transform_tool_result_hook), so the two are
        ONE callback and neither can shadow the other. Never raises; returns None when nothing changed."""
        out = product_hooks.call("tool_result_rewrite", home=hook_home, default=None, **kwargs)
        text = out if isinstance(out, str) else kwargs.get("result")
        redacted, n = placeholder_redact.redact_count(text)
        return redacted if (n or isinstance(out, str)) else None

    def _redact_terminal_output(output=None, **_kwargs):
        """transform_terminal_output for every profile: the OpenShell placeholder redaction (foreground terminal
        output and background-process output, tools/process_registry.py transform_process_output). Never raises;
        returns None when nothing changed, so another plugin's rewrite is not shadowed."""
        redacted, n = placeholder_redact.redact_count(output)
        return redacted if n else None

    def _capture_session(surface=None, command=None, session_key=None, session_id=None, **kwargs):
        # Stash this dispatch's session in a request-local ContextVar so the
        # /codex-critique slash handler running in the same context can correlate.
        # ContextVar (not a shared cell) means a different session's dispatch
        # never sees it. The authoritative path remains the codex_critique tool.
        if command in ("codex-critique", "/codex-critique"):
            sid = session_id or session_key
            if sid:
                _pending_slash_session.set(sid)

    def _capture_invitation(event=None, gateway=None, session_store=None, **kwargs):
        try:
            repo, pr, is_human, body = _parse_gh_comment(event)
            if repo and pr is not None and is_human and bool(_MENTION_RE.search(body or "")):
                stores.record_invitation(repo, pr)
        except Exception:
            logger.warning("nv-fleet-gates pre_gateway_dispatch observer failed", exc_info=True)
        try:  # which item a webhook session is about, for its later turns (conversation_origin.py)
            conversation_origin.record_event(event)
        except Exception:
            logger.warning("nv-fleet-gates conversation origin record failed", exc_info=True)

    # --- codex_critique tool + slash alias ----------------------------------
    def _critique_error(reason, error, stage, session_id):
        return json.dumps({
            "ok": False, "reason": reason, "error": error,
            "stage": stage, "session_id": session_id,
        })

    async def _critique_and_record(stage, task, artifacts, session_id):
        """Review inline artifacts; record the gate row only for a well-formed verdict."""
        task = task if isinstance(task, str) else ""
        artifacts = _critique_artifacts(artifacts)
        if not any(a["content"].strip() for a in artifacts):
            return _critique_error(
                "critique_no_artifacts",
                "pass the diff or file text inline as artifacts=[{name, content}]; "
                "the reviewer cannot read paths in your sandbox",
                stage, session_id,
            )
        total = len(task) + sum(len(a["name"]) + len(a["content"]) for a in artifacts)
        if total > critique_max_chars:
            return _critique_error(
                "critique_artifacts_too_large",
                f"{total} chars > {critique_max_chars}; send the diff, not whole files",
                stage, session_id,
            )
        from agent.plugin_llm import PluginLlm

        blocks = [{"type": "text", "text": f"Stage: {stage}\nTask: {task}"}]
        blocks += [{"type": "text", "text": f"### {a['name']}\n{a['content']}"} for a in artifacts]
        llm = PluginLlm(plugin_id=PLUGIN_KEY)
        for _attempt in range(2):
            try:
                result = await llm.acomplete_structured(
                    instructions=_CRITIQUE_INSTRUCTIONS.format(stage=stage),
                    input=blocks,
                    json_mode=True,
                )
            except Exception as exc:
                logger.warning("codex_critique backend failed", exc_info=True)
                return _critique_error("critique_backend_error", str(exc), stage, session_id)
            valid = _validate_critique(getattr(result, "parsed", None), stage)
            if valid:
                verdict, findings = valid
                if session_id:
                    # A well-formed critique HAVING RUN (not its verdict) is what the
                    # gate requires, porting NanoClaw's edits_since_critique==0 invariant.
                    try:
                        stores.record_critique(session_id, stage)
                    except Exception as exc:
                        logger.warning("codex_critique could not record the critique row", exc_info=True)
                        return _critique_error("critique_record_failed", str(exc), stage, session_id)
                return json.dumps({
                    "ok": True, "session_id": session_id, "stage": stage,
                    "verdict": verdict, "findings": findings,
                })
        return _critique_error(
            "critique_invalid_reply",
            "critique backend returned no well-formed verdict with findings after 2 attempts",
            stage, session_id,
        )

    async def _tool_codex_critique(args, **kwargs):
        # session_id comes ONLY from the authoritative dispatch kwargs, never
        # from model-supplied args — otherwise a model could record a critique
        # for another session and unlock its gate.
        args = args if isinstance(args, dict) else {}
        stage = args.get("stage") or (required_stages[0] if required_stages else "OUTPUT_REVIEW")
        return await _critique_and_record(
            stage, args.get("task"), args.get("artifacts"), kwargs.get("session_id"),
        )

    def _check_codex_critique(*_a, **_k) -> bool:
        return True  # every session may run its own critique

    async def _slash_codex_critique(raw_args: str = "", **kwargs):
        parts = (raw_args or "").strip().split(None, 1)
        stage = parts[0] if parts else (required_stages[0] if required_stages else "OUTPUT_REVIEW")
        text = parts[1].strip() if len(parts) > 1 else ""
        # Authoritative session id only: dispatch kwargs, else the request-local
        # ContextVar the pre_command observer set for this same dispatch.
        session_id = kwargs.get("session_id") or kwargs.get("session_key") or _pending_slash_session.get()
        try:
            if not session_id:
                return json.dumps({
                    "ok": False, "reason": "critique_no_session",
                    "error": "no session context for /codex-critique; invoke the codex_critique tool",
                    "stage": stage,
                })
            return await _critique_and_record(
                stage, "", [{"name": "inline", "content": text}], session_id,
            )
        finally:
            # Consume the request-local hint even on failure so it can't leak.
            _pending_slash_session.set(None)

    CODEX_CRITIQUE_SCHEMA = {
        "description": (
            "Run an independent critique of inline artifacts for the current session and record "
            "the critique row the delivery/egress gate consumes. Pass the diff or file text itself "
            "as artifacts (the reviewer cannot read paths in your sandbox). Returns the verdict "
            "with findings, or ok:false with a reason (no row recorded)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "stage": {
                    "type": "string",
                    "description": "Critique stage (e.g. OUTPUT_REVIEW, CODE_REVIEW, PLAN_REVIEW).",
                },
                "task": {
                    "type": "string",
                    "description": "The request being critiqued, verbatim.",
                },
                "artifacts": {
                    "type": "array",
                    "description": (
                        "What to review, inline: each entry's content is the diff or file text "
                        "itself, never a path. Total size is capped by critique_max_chars."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["name", "content"],
                    },
                },
            },
            "required": ["artifacts"],
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
    ctx.register_hook("transform_tool_result", _transform_tool_result)   # product rewrite, then redaction
    ctx.register_hook("transform_terminal_output", _redact_terminal_output)
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
