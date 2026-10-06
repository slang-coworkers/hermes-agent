"""nv-ingress — the inbound gateway (ING-F66).

A host edge verifies each platform event with secrets held outside the sandbox,
normalizes it to envelope v1, and forwards it to a loopback-only, secret-free
webhook route in the gateway sandbox. The route's script stages the envelope in
the fleet ledger before the adapter's 202. This plugin then delivers each event
exactly once:

* a labelled issue goes to the orchestrator's Bot Chat;
* a PR/review/CI event of a claimed PR goes to the owner's EXISTING session;
* everything unowned goes to the orchestrator.

Ownership is first-claim-wins at PR creation (``post_tool_call``). A remap is
orchestrator-only and approval-gated.
"""

from __future__ import annotations

import contextvars
import functools
import json
import logging
import sys
import threading
import uuid
from typing import Any, Dict

from . import bridge, envelope, ledger, ownership

logger = logging.getLogger(__name__)

PLUGIN_KEY = "nv-ingress"
ROUTED_EVENTS = envelope.ROUTED_EVENTS
_TOOLSET = "nv_ingress"
_CTX = None
_KICK = threading.Event()
_LOAD_ID = uuid.uuid4().hex[:12]
_UNLOAD_JOIN_SECONDS = 5.0


def _settings() -> Dict[str, Any]:
    ctx = _CTX
    if ctx is None:
        return {}
    keys = ("route", "orchestrator_profile", "issue_labels", "per_pr_hourly_budget", "self_logins",
            "drain_interval_seconds", "webhook_guard")
    return {k: ctx.get_config(k) for k in keys}


def _active_profile() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name

        return get_active_profile_name()
    except Exception:
        return "default"


def _is_orchestrator() -> bool:
    return _active_profile() == (_settings().get("orchestrator_profile") or "orchestrator")


def _safe_handler(fn):
    """A tool handler returns a JSON string and never raises."""

    @functools.wraps(fn)
    def _wrapped(args, **kwargs):
        try:
            return fn(args or {}, **kwargs)
        except Exception:
            logger.warning("nv-ingress handler %s failed", fn.__name__, exc_info=True)
            return json.dumps({"status": "error", "reason": "nv-ingress operation failed"})

    return _wrapped


# --------------------------------------------------------------------------- drain


def drain_once() -> int:
    """One drain pass (DEFAULT home only); returns the number of rows delivered."""
    return bridge.drain_once(_settings(), _LOAD_ID)


def _owns_runtime_lock() -> bool:
    try:
        from gateway import status as gateway_status

        return gateway_status.owns_gateway_runtime_lock()
    except Exception:
        return False


def _driver_loop(kick: threading.Event, stop: threading.Event, interval: float, load_id: str) -> None:
    """The only drain driver (D5): a pass on each kick and every ``interval`` seconds,
    and only in the process holding the gateway runtime lock, so a short-lived
    DEFAULT-home process (the CLI, the dashboard) never starts a delivery turn."""
    while not stop.is_set():
        if _owns_runtime_lock():
            try:
                bridge.drain_once(_settings(), load_id, stop)
            except Exception:
                logger.warning("nv-ingress drain pass failed", exc_info=True)
        kick.wait(interval)
        kick.clear()


def _start_driver(ctx) -> None:
    global _KICK
    try:
        interval = max(0.1, float(ctx.get_config("drain_interval_seconds") or 5))
    except (TypeError, ValueError):
        interval = 5.0
    kick, stop = threading.Event(), threading.Event()
    # The copied context keeps the loading manager's home override on the driver thread.
    thread = threading.Thread(target=contextvars.copy_context().run,
                              args=(_driver_loop, kick, stop, interval, _LOAD_ID),
                              name="nv-ingress-drain", daemon=True)
    _KICK = kick

    def _stop_driver() -> None:
        stop.set()
        kick.set()
        # A delivery in flight is not interrupted; its lease keeps the next load off the row.
        thread.join(timeout=_UNLOAD_JOIN_SECONDS)

    ctx.on_unload(_stop_driver)
    thread.start()


def _kick() -> None:
    """Ask the driver for a pass now; the pass never runs on the caller's thread."""
    _KICK.set()


# --------------------------------------------------------------------------- hooks


def _on_pre_gateway_dispatch(event=None, gateway=None, session_store=None, **kwargs):
    """The ingress route never mints a one-shot webhook session: the event is
    already staged, so kick the drain and skip. Every other route is untouched."""
    try:
        from gateway.config import Platform

        source = getattr(event, "source", None)
        route = _settings().get("route") or "ingress"
        if (source is None or getattr(source, "platform", None) != Platform.WEBHOOK
                or getattr(source, "chat_type", None) != "webhook"
                or not str(getattr(source, "chat_id", "") or "").startswith(f"webhook:{route}:")):
            return None
    except Exception:
        logger.warning("nv-ingress pre_gateway_dispatch filter failed", exc_info=True)
        return None
    try:
        _kick()
    except Exception:
        logger.warning("nv-ingress drain kick failed", exc_info=True)
    return {"action": "skip", "reason": "nv-ingress staged the event for owner delivery"}


def _on_kanban_dispatch_tick(**kwargs):
    _kick()


def _on_post_tool_call(tool_name=None, args=None, result=None, status=None, session_id=None, **kwargs):
    """The claim feed: a successful PR-create in a coworker session claims the PR."""
    try:
        if tool_name not in ("terminal", "execute_code") or status not in ("ok", None):
            return None
        args = args or {}
        invocation = str(args.get("command") if tool_name == "terminal" else args.get("code") or "")
        parsed = ownership.parse_create(invocation, str(result or ""))
        if parsed is None:
            return None
        profile, sid = _active_profile(), str(session_id or "")
        if parsed["replaces"] is not None and sid and ownership.has_owner(parsed["repo"], parsed["replaces"]):
            ownership.replace(parsed["repo"], parsed["replaces"], parsed["pr"], profile, sid)
        else:
            ownership.claim(parsed["repo"], parsed["pr"], profile, sid)
    except Exception:
        logger.warning("nv-ingress post_tool_call claim failed", exc_info=True)
    return None


def _remap_rule_key(repo: str, pr: Any, to: str) -> str:
    return f"ingress_remap:{repo}#{pr}->{to}"


def _on_pre_tool_call(tool_name=None, args=None, **kwargs):
    """Escalate an orchestrator remap to the human-approval gate, keyed per (repo, pr, target)."""
    if tool_name != "ingress_remap":
        return None
    if not _is_orchestrator():
        # Role check first: a worker's remap is refused outright, never put to a human.
        return {"action": "block", "message": "ingress_remap is orchestrator-only"}
    args = args or {}
    repo, pr, to = str(args.get("repo") or ""), args.get("pr"), str(args.get("to") or "")
    return {"action": "approve",
            "message": f"Re-point the owner of {repo}#{pr} to profile {to}",
            "rule_key": _remap_rule_key(repo, pr, to)}


# --------------------------------------------------------------------------- tools


@_safe_handler
def _tool_remap(args, **kwargs) -> str:
    if not _is_orchestrator():
        return json.dumps({"status": "refused", "reason": "remap is orchestrator-only"})
    return json.dumps(ownership.remap(str(args.get("repo") or ""), int(args["pr"]), str(args.get("to") or ""),
                                      args.get("session")))


@_safe_handler
def _tool_claim_replacement(args, session_id=None, **kwargs) -> str:
    sid = str(session_id or kwargs.get("task_id") or "")
    if not sid:
        return json.dumps({"status": "refused", "reason": "no session id on the calling turn"})
    return json.dumps(ownership.replace(str(args.get("repo") or ""), int(args["old_pr"]), int(args["new_pr"]),
                                        _active_profile(), sid))


_REMAP_SCHEMA = {
    "name": "ingress_remap",
    "description": ("Re-point the owner of a fleet PR to another coworker profile's session "
                    "(orchestrator only; always asks the human for approval)."),
    "parameters": {"type": "object", "properties": {
        "repo": {"type": "string", "description": "Repository in owner/name form."},
        "pr": {"type": "integer", "description": "Pull-request number."},
        "to": {"type": "string", "description": "The new owning profile."},
        "session": {"type": "string", "description": "Target session id (defaults to its Bot Chat)."},
    }, "required": ["repo", "pr", "to"]},
}

_REPLACEMENT_SCHEMA = {
    "name": "ingress_claim_replacement",
    "description": ("Claim a new PR as the replacement of a PR this session owns; the new PR "
                    "inherits the owner and thread."),
    "parameters": {"type": "object", "properties": {
        "repo": {"type": "string", "description": "Repository in owner/name form."},
        "old_pr": {"type": "integer", "description": "The PR being replaced."},
        "new_pr": {"type": "integer", "description": "The replacement PR."},
    }, "required": ["repo", "old_pr", "new_pr"]},
}


# --------------------------------------------------------------------------- CLI


def _emit(payload: Dict[str, Any], args, *, ok: bool) -> int:
    stream = sys.stdout if ok or getattr(args, "json", False) else sys.stderr
    if getattr(args, "json", False):
        print(json.dumps(payload), file=stream)
    else:
        print(", ".join(f"{k}={v}" for k, v in payload.items()), file=stream)
    return 0 if ok else 1


def _cli_remap(args) -> int:
    if not _is_orchestrator():
        return _emit({"status": "refused", "reason": "remap is orchestrator-only"}, args, ok=False)
    from hermes_cli.plugins import resolve_pre_tool_block

    tool_args = {"repo": args.repo, "pr": int(args.pr), "to": args.to, "session": getattr(args, "session", None)}
    blocked = resolve_pre_tool_block("ingress_remap", tool_args)
    if blocked:
        return _emit({"status": "refused", "reason": blocked}, args, ok=False)
    out = ownership.remap(str(args.repo), int(args.pr), str(args.to), getattr(args, "session", None))
    return _emit(out, args, ok=out.get("status") == "remapped")


def _cli_claim(args) -> int:
    out = ownership.claim(str(args.repo), int(args.pr), _active_profile(), str(args.session or ""))
    return _emit(out, args, ok=out.get("status") in ("claimed", "refreshed"))


def _cli_render_host(args) -> int:
    from . import hostrender

    try:
        import yaml

        with open(args.spec, encoding="utf-8") as handle:
            spec = yaml.safe_load(handle) or {}
        params = hostrender.validate(spec.get("ingress"))
        hostrender.render_host(params, args.out)
    except (OSError, ValueError) as exc:
        return _emit({"status": "error", "reason": str(exc)}, args, ok=False)
    return _emit({"status": "rendered", "out": str(args.out)}, args, ok=True)


def _cli(args) -> int:
    sub = getattr(args, "ingress_command", None)
    if sub == "status":
        return _emit(ledger.status(), args, ok=True)
    if sub == "remap":
        return _cli_remap(args)
    if sub == "claim":
        return _cli_claim(args)
    if sub == "render-host":
        return _cli_render_host(args)
    return _emit({"status": "error", "reason": f"unknown ingress subcommand: {sub}"}, args, ok=False)


def _setup_cli(parser) -> None:
    sub = parser.add_subparsers(dest="ingress_command")
    st = sub.add_parser("status", help="Pending, stuck and runaway deliveries, refusals, guard state")
    st.add_argument("--json", action="store_true", help="Emit one JSON object")
    rm = sub.add_parser("remap", help="Re-point PR ownership (orchestrator only, approval-gated)")
    rm.add_argument("repo", help="Repository in owner/name form")
    rm.add_argument("pr", type=int, help="Pull-request number")
    rm.add_argument("--to", required=True, help="New owning profile")
    rm.add_argument("--session", default=None, help="Target session id (defaults to its Bot Chat)")
    rm.add_argument("--json", action="store_true")
    cl = sub.add_parser("claim", help="Claim a PR for this profile's session (first claim wins)")
    cl.add_argument("repo")
    cl.add_argument("pr", type=int)
    cl.add_argument("--session", required=True, help="The claiming session id")
    cl.add_argument("--json", action="store_true")
    rh = sub.add_parser("render-host", help="Render the host units, edge.yaml and hook events from a spec")
    rh.add_argument("spec", help="Path to coworker-types.yaml with an ingress: block")
    rh.add_argument("--out", required=True, help="Output directory (host/ and hooks/ are written there)")
    rh.add_argument("--json", action="store_true")


# --------------------------------------------------------------------------- register


def _routes_configured(config) -> bool:
    return bool((getattr(config, "extra", None) or {}).get("routes"))


def register(ctx) -> None:
    global _CTX, _LOAD_ID
    _CTX = ctx
    _LOAD_ID = uuid.uuid4().hex[:12]
    ctx.register_tool(name="ingress_remap", toolset=_TOOLSET, schema=_REMAP_SCHEMA, handler=_tool_remap,
                      check_fn=_is_orchestrator, description=_REMAP_SCHEMA["description"])
    ctx.register_tool(name="ingress_claim_replacement", toolset=_TOOLSET, schema=_REPLACEMENT_SCHEMA,
                      handler=_tool_claim_replacement, description=_REPLACEMENT_SCHEMA["description"])
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("on_kanban_dispatch_tick", _on_kanban_dispatch_tick)
    ctx.register_cli_command("ingress", help="Inbound gateway: status, claim, approval-gated remap, host render",
                             setup_fn=_setup_cli, handler_fn=_cli,
                             description="nv-ingress fleet ledger operations.")
    if ctx.get_config("webhook_guard") is True:
        from gateway.platforms.webhook import check_webhook_requirements

        from .webhook_guard import GuardedWebhookAdapter

        ctx.register_platform(name="webhook", label="Webhook", adapter_factory=GuardedWebhookAdapter,
                              check_fn=check_webhook_requirements, is_connected=_routes_configured)
    # The driver's first pass is also the startup drain: a restarted gateway delivers
    # what was staged before it went down, with no new inbound.
    if bridge.is_ledger_home():
        _start_driver(ctx)
