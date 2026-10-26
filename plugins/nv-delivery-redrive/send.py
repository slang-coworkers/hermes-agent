"""Send side (ADR D3) and the busy completion (D7, AC-10).

The ``tool_execution`` middleware never short-circuits and never alters arguments: every ``message_agent`` call
reaches core's complete authorization and core's own delivery. It only observes the ack and records a ticket.
"""

from __future__ import annotations

import contextlib
import json
import logging
import shlex
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from tools.approval_context import (
    _approval_session_id,
    _approval_turn_id,
    reset_current_observability_context,
    set_current_observability_context,
)

from . import probe, settings, store
from .notify import UNVERIFIED

logger = logging.getLogger(__name__)

TOOL = "message_agent"
REFUSAL_REASON = "target_busy"
COMMIT_WAIT_SECONDS = 10.0


def _sender(home: str) -> tuple[Path, str, dict[str, Path]]:
    from tools.bot_mode_probe import _hermes_root, _profile_name, _roster

    root = _hermes_root(Path(home))
    return root, _profile_name(Path(home)), dict(_roster(root))


def _resolve_target(target: Any, me: str, roster: dict[str, Path], root: Path) -> Optional[str]:
    from tools.bot_mode_dm import _LOCAL_TARGET_RE, _resolve_local_name

    raw = str(target or "").strip().lstrip("@")
    if not raw or "/" in raw or "@" in raw:
        return None
    resolved = _resolve_local_name(raw, list(roster), root)
    if resolved is None or resolved == me or not _LOCAL_TARGET_RE.match(resolved):
        return None
    return resolved


def build_content(me: str, sender_home: Path, message: Any) -> str:
    """The attributed text exactly as core builds it (``tools/bot_mode_dm.py`` ``message_agent_tool``)."""
    from tools.bot_mode_probe import _display_name, _handle

    return f"Message from 🤖 {_display_name(me, sender_home)} (@{_handle(me)}): " + str(message or "").strip()


def _runner_dm_path(command: str) -> Optional[str]:
    from tools.bot_mode_dm import _runner_argv

    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if "--run-delivery" not in parts:
        return None
    parsed = _runner_argv(parts[parts.index("--run-delivery"):])
    return parsed[2] if parsed else None


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None


def _capture(ack: dict[str, Any]) -> tuple[Optional[str], Optional[str], Optional[int], Optional[float]]:
    """``(dm_path, content, runner pid, runner create_time)`` from what core actually wrote."""
    from tools import bot_mode_dm
    from tools.process_registry import process_registry

    dm_path = content = pid = create_time = None
    session = process_registry.get(str(ack.get("process_id") or "")) if ack.get("process_id") else None
    if session is not None:
        dm_path = _runner_dm_path(session.command or "")
        pid = session.pid
        create_time = runner_create_time(pid)
    if dm_path:
        intent = _read_text(Path(bot_mode_dm._live_intent_file(dm_path)))
        if intent:
            try:
                content = json.loads(intent).get("message")
            except ValueError:
                content = None
        if content is None:
            content = _read_text(Path(dm_path))
    return dm_path, content, pid, create_time


def runner_create_time(pid: Optional[int]) -> Optional[float]:
    if not pid:
        return None
    try:
        import psutil

        return float(psutil.Process(pid).create_time())
    except Exception:
        logger.debug("nv-delivery-redrive: no start time for pid %s", pid, exc_info=True)
        return None


def _live_dm_path(delivery_id: str) -> Optional[str]:
    """The DM path of a live admission whose runner never started: its intent file names the same id."""
    from tools import bot_mode_dm

    try:
        for intent in bot_mode_dm._dm_dir().glob("*.live.json"):
            data = json.loads(intent.read_text(encoding="utf-8-sig"))
            if data.get("delivery_id") == delivery_id:
                return str(intent)[: -len(".live.json")]
    except (OSError, ValueError):
        logger.debug("nv-delivery-redrive: intent scan failed", exc_info=True)
    return None


def middleware(tool_name: str = "", args: Any = None, next_call: Any = None, task_id: str = "",
               session_id: str = "", **kwargs: Any) -> Any:
    if tool_name != TOOL:
        return next_call(args)
    # D5a.5: core's reader copies this context at spawn, so the runner's completion transform carries this call's id;
    # with none, every runner completion shares one hook gate key and core skips all but the first that overlap.
    call_id = kwargs.get("tool_call_id")
    tokens = set_current_observability_context(
        turn_id=_approval_turn_id.get(),
        tool_call_id=call_id if isinstance(call_id, str) and call_id else f"nv-redrive:{uuid.uuid4().hex}",
        session_id=_approval_session_id.get())
    called = False

    def bound_next(payload: Any) -> Any:
        nonlocal called
        called = True
        return next_call(payload)
    try:
        if not isinstance(args, dict):
            return next_call(args)
        try:
            return _send(args, bound_next, task_id, session_id)
        except Exception:
            # Core would run the call itself after a middleware error, outside this binding.
            if called:
                raise
            logger.warning("nv-delivery-redrive: send-side setup failed; passing through", exc_info=True)
            return next_call(args)
    finally:
        reset_current_observability_context(tokens)


def _send(args: dict, next_call: Any, task_id: str, session_id: str) -> Any:
    try:
        from hermes_constants import get_hermes_home

        home = str(get_hermes_home())
        root, me, roster = _sender(home)
        target = _resolve_target(args.get("target"), me, roster, root)
        orch = settings.orchestrator(root) if target else None
        tracked = bool(target and orch and settings.heartbeat_fresh())
    except Exception:
        logger.warning("nv-delivery-redrive: send-side setup failed; passing through", exc_info=True)
        tracked = False
    if not tracked:
        return next_call(args)
    target_home = roster[target]
    baseline_state, baseline = probe.max_row(target_home, db_seen=False)
    inflight, fallback = None, None
    try:
        # D5a.1: core spawns the runner inside next_call, so its completion can reach the transform before the
        # ticket below is committed; this row tells the transform the send is still in flight.
        inflight = store.open_inflight(sender_session_id=session_id or "", task_id=task_id or "", target=target,
                                       target_home=str(target_home), baseline=baseline)
    except Exception:
        logger.warning("nv-delivery-redrive: in-flight row not recorded; gating on an in-process record",
                       exc_info=True)
        fallback = _open_fallback(task_id or "", target)
    out, bound = None, False
    try:
        out = next_call(args)
        tracked_out = out
        process_id, dm_path, retargeted = _dispatched(out, target, target_home)
        _bind(inflight, fallback, process_id, dm_path, untracked=retargeted)
        bound = True
        if not retargeted and inflight is not None:
            try:
                tracked_out = _track(out, args, me=me, root=root, sender_home=Path(home), target=target,
                                     target_home=target_home, baseline_state=baseline_state, baseline=baseline,
                                     session_id=session_id, inflight=inflight)
            except Exception:
                logger.warning("nv-delivery-redrive: could not record a ticket; core's ack returned unchanged",
                               exc_info=True)
                tracked_out = out
    finally:
        if not bound:
            with contextlib.suppress(Exception):
                _bind(inflight, fallback, None, None, untracked=True)
    if tracked_out is out:
        with contextlib.suppress(Exception):
            store.close_inflight(inflight, "untracked")
    return tracked_out


def _dispatched(out: Any, target: str, target_home: Path) -> tuple[Optional[str], Optional[str], bool]:
    """``(process_id, dm_path, retargeted)`` of what core dispatched. ``retargeted`` is True when a ``pre_tool_call``
    ``modify`` changed ``target``, or the dispatched recipient cannot be confirmed (D3.3, E9)."""
    from tools import bot_live_delivery
    from tools.bot_mode_probe import _handle
    from tools.process_registry import process_registry

    try:
        ack = json.loads(out) if isinstance(out, str) else None
    except ValueError:
        ack = None
    if not isinstance(ack, dict) or not ack.get("delivery_id"):
        return None, None, False
    process_id = str(ack.get("process_id") or "") or None
    if process_id:
        session = process_registry.get(process_id)
        command = session.command if session is not None else ""
        dm_path = _runner_dm_path(command or "")
        return process_id, dm_path, dm_path is None or _runner_target(command or "") != target
    receipt = bot_live_delivery.read_delivery_result(target_home, ack["delivery_id"])
    confirmed = receipt is not None and ack.get("to") == f"@{_handle(target)}"
    return None, _live_dm_path(ack["delivery_id"]) if confirmed else None, not confirmed


def _bind(inflight: Optional[int], fallback: Optional[dict[str, Any]], process_id: Optional[str],
          dm_path: Optional[str], *, untracked: bool) -> None:
    if inflight is not None:
        store.bind_inflight(inflight, process_id=process_id, dm_path=dm_path, untracked=untracked)
    if fallback is not None:
        from tools.process_registry import process_registry

        session = process_registry.get(process_id) if process_id else None
        with _fallback_lock:
            fallback.update(process_id=process_id, dm_path=dm_path, bound=True)
            # No runner to gate, or its completion was already published (and gated) before this bind.
            if dm_path is None or session is None or session._completion_event.is_set():
                _fallbacks.remove(fallback)


# D5a.2: a send whose in-flight insert failed is still gated, from this record, until its runner's completion is.
_fallbacks: list[dict[str, Any]] = []
_fallback_lock = threading.Lock()


def _open_fallback(task_id: str, target: str) -> dict[str, Any]:
    record = {"task_id": task_id, "target": target, "created_monotonic": time.monotonic(), "process_id": None,
              "dm_path": None, "bound": False}
    with _fallback_lock:
        _fallbacks.append(record)
    return record


def _fallbacks_for(task_id: str) -> list[dict[str, Any]]:
    with _fallback_lock:
        return [dict(r) for r in _fallbacks if r["task_id"] == task_id]


def _drop_fallback(dm_path: str) -> None:
    with _fallback_lock:
        _fallbacks[:] = [r for r in _fallbacks if not (r["bound"] and r["dm_path"] == dm_path)]


def _track(out: Any, args: dict[str, Any], *, me: str, root: Path, sender_home: Path, target: str,
           target_home: Path, baseline_state: str, baseline: Optional[int], session_id: str,
           inflight: Optional[int] = None) -> Any:
    from tools import bot_live_delivery, bot_mode_dm

    if not isinstance(out, str):
        return out
    try:
        ack = json.loads(out)
    except ValueError:
        return out
    did = ack.get("delivery_id") if isinstance(ack, dict) else None
    if not did or ack.get("status") not in ("queued", "claimed", "settled"):
        return out
    dm_path, content, pid, create_time = _capture(ack)
    path = "cli"
    if not ack.get("process_id"):
        receipt = bot_live_delivery.read_delivery_result(target_home, did)
        if receipt is None:
            return out
        path = "live"
        dm_path = _live_dm_path(did)
        content = receipt.get("message")
    elif bot_live_delivery.read_delivery_result(target_home, did) is not None:
        path = "live"
    verified = content is not None and dm_path is not None and bot_mode_dm._dm_delivery_id(dm_path) == did
    if content is None:
        content = build_content(me, sender_home, args.get("message"))
    now = time.time()
    window = settings.get("redrive_window_seconds")
    author = {"id": f"bot:{me}", "name": "hermes" if me == "default" else me, "is_bot": True}
    fields = {
        "delivery_id": did, "kind": "message", "sender": me, "target": target, "target_home": str(target_home),
        "sender_home": str(sender_home), "sender_session_id": session_id or None,
        "author_json": json.dumps(author, separators=(",", ":")), "content": content,
        "content_verified": 1 if verified else 0, "dm_path": dm_path, "path": path, "state": "pending",
        "process_id": ack.get("process_id"), "created_at": now, "deadline": now + window,
        "next_attempt_at": now + settings.get("retry_backoff_seconds"),
        "db_seen": 0 if baseline_state == probe.COLD else 1,
    }
    attempt = {"state": "running" if ack.get("process_id") else "over", "dm_path": dm_path, "pid": pid,
               "pid_create_time": create_time, "baseline": baseline, "spawned_at": now}
    store.commit_ticket(fields, attempt, inflight)
    ack["redrive"] = {"tracked": True, "delivery_id": did, "deadline": fields["deadline"]}
    return json.dumps(ack)


def post_tool_call(tool_name: str = "", args: Any = None, result: Any = None, **kwargs: Any) -> None:
    """Fix a provisional ticket's content from the FINAL args (after any ``pre_tool_call`` modify)."""
    if tool_name != TOOL or not isinstance(args, dict):
        return
    try:
        text = result if isinstance(result, str) else None
        ack = json.loads(text) if text else None
        did = ack.get("delivery_id") if isinstance(ack, dict) else None
        if not did:
            return
        ticket = store.ticket(did)
        if ticket is None or ticket["content_verified"]:
            return
        content = build_content(ticket["sender"], Path(ticket["sender_home"]), args.get("message"))
        store.verify_content(did, content)
    except Exception:
        logger.debug("nv-delivery-redrive: post_tool_call verification failed", exc_info=True)
    return


def clean_refusal(output: str) -> bool:
    """Core's runner refused before the target turn started: ``{"reason": "target_busy", "error": …}``."""
    for line in reversed([ln for ln in str(output or "").split("\n") if ln.strip()]):
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        return isinstance(payload, dict) and payload.get("reason") == REFUSAL_REASON and "status" not in payload
    return False


def _runner_target(command: str) -> Optional[str]:
    """The target profile of a ``--run-delivery`` command (its ``hermes -p <profile> …`` argv)."""
    from tools.bot_mode_dm import _runner_argv

    try:
        parts = shlex.split(command)
        parsed = _runner_argv(parts[parts.index("--run-delivery"):])
    except ValueError:
        return None
    argv = parsed[3] if parsed else []
    return argv[argv.index("-p") + 1] if "-p" in argv[:-1] else None


def _runner_session(task_ids: set[str], command: str) -> Any:
    """``(task_id, session)`` of the registry runner running ``command`` for one of ``task_ids``, else None. Finished
    sessions count whether or not a poll already observed them: core moves a session to finished before it
    publishes, so this transform can run after a poll saw the exit."""
    from tools.process_registry import process_registry

    for task_id in task_ids:
        for entry in process_registry.list_sessions(task_id=task_id):
            session = process_registry.get(str(entry.get("session_id") or ""))
            if session is not None and session.owner_task_id == task_id and session.command == command:
                return task_id, session
    return None


def _own_bound(dm_path: str) -> Optional[dict[str, Any]]:
    """This runner's own in-flight row or fallback record, once its call has bound it (D5a.1)."""
    row = store.bound_inflight(dm_path)
    if row is not None:
        return row
    with _fallback_lock:
        found = [dict(r) for r in _fallbacks if r["bound"] and r["dm_path"] == dm_path]
    return {**found[-1], "state": "fallback"} if found else None


def _await_ticket(dm_path: str, command: str) -> Any:
    """D5a.2: the committed ticket for ``dm_path``; ``None`` when this runner's send is untracked; ``probe.UNKNOWN``
    to fail closed. Reads in-flight rows only: binding and marking them is their own call's middleware's job."""
    ticket = store.ticket_by_dm_path(dm_path)
    if ticket is not None:
        return ticket
    if store.published_for(dm_path) == "ambiguous":
        return probe.UNKNOWN
    real = _runner_target(command)
    deadline = time.monotonic() + COMMIT_WAIT_SECONDS
    own, latched = _own_bound(dm_path), False
    if own is None:
        rows = store.prebind_rows()
        tasks = {r["task_id"] for r in rows if r["task_id"]}
        with _fallback_lock:
            tasks |= {r["task_id"] for r in _fallbacks if not r["bound"] and r["task_id"]}
        found = _runner_session(tasks, command)
        if found is None:
            return None
        task_id = found[0]
        candidates = [r for r in rows if r["task_id"] == task_id]
        candidates += [r for r in _fallbacks_for(task_id) if not r["bound"]]
        if not any(c["target"] == real for c in candidates):
            return None
        latched = True
        while own is None:
            if time.monotonic() >= deadline:
                return probe.UNKNOWN
            time.sleep(0.05)
            ticket = store.ticket_by_dm_path(dm_path)
            if ticket is not None:
                return ticket
            own = _own_bound(dm_path)
    if own["target"] != real:
        return probe.UNKNOWN if latched else None
    while True:
        ticket = store.ticket_by_dm_path(dm_path)
        if ticket is not None:
            return ticket
        if own["state"] == "untracked":
            return None
        if time.monotonic() >= deadline:
            return probe.UNKNOWN
        time.sleep(0.2)
        own = _own_bound(dm_path) or own


def _claims_row(ticket: dict[str, Any]) -> bool:
    from . import drain

    state, row = probe.max_row(ticket["target_home"], db_seen=bool(ticket["db_seen"]))
    view = drain.NowView(ticket["delivery_id"], row if state != probe.UNKNOWN else None)
    return isinstance(drain.verdict_for(ticket, view), tuple)


def reported_success(output: str, returncode: Any) -> bool:
    """A runner completion that reports the delivery as done: a live ``settled`` payload or a CLI exit-0 reply."""
    from . import drain

    payload = drain._last_json(output)
    if payload and payload.get("delivery_id") and payload.get("status"):
        return payload["status"] == "settled"
    return drain.classify({"rc": returncode, "out": output}) == "exit0"


def _gate(command: str, dm_path: str, output: str, returncode: Any) -> Optional[str]:
    """D5a.2: core's success text passes only when this ticket claims its row; otherwise the completion says so."""
    started = time.monotonic()
    ticket = _await_ticket(dm_path, command)
    if ticket is None:
        return None
    # Past core's own bound on this hook, core has already published its text unchanged (the waived fail-open):
    # recording a status here would claim a publication that never happened.
    record = time.monotonic() - started < settings.hook_timeout()
    if ticket is not probe.UNKNOWN and _claims_row(ticket):
        if record:
            store.set_published(ticket["delivery_id"], "settled")
        return None
    if ticket is probe.UNKNOWN:
        from tools.bot_mode_dm import _dm_delivery_id

        did, target = _dm_delivery_id(dm_path), _runner_target(command) or ""
        if record:
            store.publish_unticketed(dm_path, "ambiguous")
    else:
        did, target = ticket["delivery_id"], ticket["target"]
        if record:
            store.set_published(did, "ambiguous")
    return json.dumps({"status": "ambiguous", "reason": "unknown", "delivery_id": did, "to": f"@{target}",
                       "detail": UNVERIFIED})


def transform_terminal_output(command: str = "", output: str = "", returncode: Any = None, **kwargs: Any) -> Any:
    """Record attempt 1's outcome; rewrite a tracked clean refusal into the truthful queued shape, and a reported
    success whose row is unproven into the D5a unverified shape."""
    if "--run-delivery" not in str(command or "") or returncode is None:
        return None
    try:
        dm_path = _runner_dm_path(command)
        if not dm_path:
            return None
        outcome = json.dumps({"rc": returncode, "out": output})
        attempt = store.attempt_by_dm_path(dm_path)
        if attempt is not None:
            store.record_attempt_outcome(attempt["delivery_id"], 1, outcome)
        if reported_success(output, returncode):
            if not settings.tracking_live(store.root()):
                return None
            try:
                gated = _gate(command, dm_path, output, returncode)
            finally:
                _drop_fallback(dm_path)
            if attempt is None and (attempt := store.attempt_by_dm_path(dm_path)) is not None:
                store.record_attempt_outcome(attempt["delivery_id"], 1, outcome)
            return gated
        _drop_fallback(dm_path)
        if attempt is None or not clean_refusal(output) or not settings.tracking_live(store.root()):
            return None
        ticket = store.ticket(attempt["delivery_id"]) or {}
        did = attempt["delivery_id"]
        return json.dumps({
            "status": "queued", "delivery_id": did, "to": f"@{ticket.get('target', '')}", "reason": REFUSAL_REASON,
            "redrive": {"tracked": True, "delivery_id": did, "deadline": ticket.get("deadline")},
            "detail": (f"Queued for automatic re-delivery under {did}; do NOT resend. "
                       "The outcome follows as a delivery notice."),
        })
    except Exception:
        logger.warning("nv-delivery-redrive: completion transform failed; core output kept", exc_info=True)
        return None
