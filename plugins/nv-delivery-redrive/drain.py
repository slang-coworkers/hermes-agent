"""The durable owner (ADR D6): one pass per kanban dispatch tick, in the gateway process.

Each tick writes the heartbeat, reconciles attempts against the live-writer scan, then advances every open
ticket per (sender, target) pair in ``seq`` order: attribution and settling for every ticket, a re-drive only while
no earlier ticket of the pair is unresolved. Re-drives are core's own delivery runner, spawned outside the process
registry (so a gateway restart does not kill them), on attempt 1's DM path so core derives the same ``delivery_id``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shlex
import subprocess
import time
import uuid
import weakref
from pathlib import Path
from typing import Any, Optional

from . import notify, probe, settings, store

logger = logging.getLogger(__name__)

RECEIPT_TERMINAL = ("settled", "failed", "cancelled", "ambiguous")
GRACE_BOOT_SECONDS = 60.0
INFLIGHT_KEEP_SECONDS = 600.0
_SHELL_NOISE = ("no job control in this shell", "cannot set terminal process group",
                "tcsetattr: Inappropriate ioctl for device")
_RUNNER_WRAPPER = ("import subprocess, sys; rc = subprocess.call(sys.argv[2:], stdin=subprocess.DEVNULL); "
                   "open(sys.argv[1], 'w', encoding='utf-8').write(str(rc))")

_children: dict[int, subprocess.Popen] = {}
_epochs: weakref.WeakKeyDictionary[Any, str] = weakref.WeakKeyDictionary()


def _registry():
    from tools.process_registry import process_registry

    return process_registry


def epoch() -> str:
    """Identity of this gateway's process registry: a restart (a new registry) re-checks in-process skips."""
    registry = _registry()
    value = _epochs.get(registry)
    if value is None:
        value = _epochs[registry] = uuid.uuid4().hex[:12]
    return value


def _turn_wait() -> float:
    from tools.bot_relay import turn_wait_seconds

    return turn_wait_seconds()


# --- outcomes ---------------------------------------------------------------------------------------------------
def _clean(text: str) -> str:
    lines = [ln for ln in str(text or "").split("\n") if ln.strip() and not any(n in ln for n in _SHELL_NOISE)]
    return "\n".join(lines).strip()


def _last_json(text: str) -> Optional[dict[str, Any]]:
    for line in reversed([ln for ln in str(text or "").split("\n") if ln.strip()]):
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        return payload if isinstance(payload, dict) else None
    return None


PENDING = object()


def attempt_outcome(ticket: dict[str, Any], attempt: dict[str, Any]) -> Any:
    """``{"rc", "out"}`` of an attempt that is over, None when nothing was recorded, or ``PENDING`` while core is
    still publishing attempt 1's completion (its transform has not run yet)."""
    if attempt.get("outcome"):
        with contextlib.suppress(ValueError):
            return json.loads(attempt["outcome"])
    if attempt.get("out_path"):
        try:
            out = Path(attempt["out_path"]).read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            out = ""
        try:
            rc: Optional[int] = int(Path(attempt["out_path"] + ".rc").read_text(encoding="utf-8-sig").strip())
        except (OSError, ValueError):
            rc = None
        return {"rc": rc, "out": out} if (out.strip() or rc is not None) else None
    if attempt["n"] == 1 and ticket.get("process_id"):
        session = _registry().get(ticket["process_id"])
        if session is not None and not session.detached and session._reader_thread is not None:
            if not session._completion_event.is_set():
                return PENDING
            return {"rc": session.exit_code, "out": session.output_buffer}
    return None


def classify(outcome: Any) -> str:
    """``pending`` | ``none`` | ``refusal`` | ``live`` | ``exit0`` | ``failed`` (ADR D5 outcome column)."""
    from .send import clean_refusal

    if outcome is PENDING:
        return "pending"
    if not outcome:
        return "none"
    rc, out = outcome.get("rc"), str(outcome.get("out") or "")
    if clean_refusal(out):
        return "refusal"
    payload = _last_json(out)
    if payload and payload.get("status") and payload.get("delivery_id"):
        return "live"
    if rc is None or rc < 0 or 128 < rc <= 192 or not _clean(out):
        return "none"
    return "exit0" if rc == 0 else "failed"


# --- spawning ---------------------------------------------------------------------------------------------------
def _out_dir() -> Path:
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from plugins.plugin_storage import plugin_data_dir

    token = set_hermes_home_override(store.root())
    try:
        path = plugin_data_dir(store.PLUGIN_NAME) / "attempts"
    finally:
        reset_hermes_home_override(token)
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def _spawn_env() -> dict[str, str]:
    from tools.process_registry import ProcessRegistry

    spawn_env = getattr(ProcessRegistry, "_spawn_env", None)
    return spawn_env({}) if callable(spawn_env) else dict(os.environ)


def spawn_attempt(ticket: dict[str, Any], n: int, out_path: str) -> tuple[int, Optional[float]]:
    """Write the payload to attempt 1's DM path (``O_EXCL``) and start core's runner on it, wrapped so its exit
    code lands beside its stdout for whichever gateway process reads it later."""
    from tools import bot_mode_dm, bot_relay

    from .send import runner_create_time

    fd = os.open(ticket["dm_path"], os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(ticket["content"])
    author = json.loads(ticket["author_json"]) if ticket.get("author_json") else None
    command = bot_mode_dm._delivery_command(
        [bot_relay._hermes_cli(), "-p", ticket["target"], *bot_relay.BOT_CHAT_TURN_ARGS], ticket["dm_path"],
        stdin_file=False, profile_home=Path(ticket["target_home"]), author=author)
    argv = shlex.split(command)
    with open(out_path, "w", encoding="utf-8") as out, open(out_path + ".err", "w", encoding="utf-8") as err:
        proc = subprocess.Popen([argv[0], "-c", _RUNNER_WRAPPER, out_path + ".rc", *argv], stdin=subprocess.DEVNULL,
                                stdout=out, stderr=err, start_new_session=True, env=_spawn_env(),
                                cwd=str(Path(bot_mode_dm.__file__).resolve().parent.parent))
    _children[proc.pid] = proc
    return proc.pid, runner_create_time(proc.pid)


def _reap_children() -> None:
    for pid, proc in list(_children.items()):
        if proc.poll() is not None:
            _children.pop(pid, None)


def _launch(ticket: dict[str, Any], n: int, baseline: int, procs, now: float) -> None:
    dm_path = ticket["dm_path"]
    if os.path.exists(dm_path):
        if probe.writer_pid(probe.scan_processes(), dm_path) is not None:
            return
        # The leftover payload of a runner that died before its own cleanup; nothing holds it.
        with contextlib.suppress(OSError):
            os.unlink(dm_path)
    out_path = str(_out_dir() / f"{ticket['delivery_id'][:32]}-{n}.out")
    store.commit_launching(ticket["delivery_id"], n, dm_path=dm_path, baseline=baseline, out_path=out_path)
    try:
        pid, create_time = spawn_attempt(ticket, n, out_path)
    except Exception:
        logger.warning("nv-delivery-redrive: re-drive %s attempt %d did not start", ticket["delivery_id"], n,
                       exc_info=True)
        store.update_attempt(ticket["delivery_id"], n, state="over", over_at=time.time())
        return
    store.commit_running(ticket["delivery_id"], n, pid=pid, pid_create_time=create_time,
                         next_attempt_at=now + max(settings.get("retry_backoff_seconds"), _turn_wait()))
    logger.info("nv-delivery-redrive: re-drive %s attempt %d to @%s (pid %s)", ticket["delivery_id"], n,
                ticket["target"], pid)


# --- reconcile --------------------------------------------------------------------------------------------------
def _reconcile(procs, ticket: dict[str, Any]) -> None:
    """``launching``/``running`` attempts with no live writer are over; ``over_row`` closes their window."""
    for attempt in store.attempts(ticket["delivery_id"]):
        if attempt["state"] == "over":
            continue
        alive = probe.writer_pid(procs, attempt.get("dm_path"))
        if alive is not None:
            if attempt["state"] == "launching":
                store.update_attempt(ticket["delivery_id"], attempt["n"], state="running",
                                     pid=None if alive < 0 else alive)
            continue
        state, over_row = probe.max_row(ticket["target_home"], db_seen=bool(ticket["db_seen"]))
        now = time.time()
        with store.txn() as conn:
            store.update_attempt(ticket["delivery_id"], attempt["n"], conn, state="over", over_at=now,
                                 over_row=over_row)
            # retry_backoff_seconds is the gap between attempts: it also runs from the end of this one.
            ticket["next_attempt_at"] = max(ticket["next_attempt_at"], now + settings.get("retry_backoff_seconds"))
            store.update_ticket(ticket["delivery_id"], conn, next_attempt_at=ticket["next_attempt_at"])
        if state == "ok" and not ticket["db_seen"]:
            store.update_ticket(ticket["delivery_id"], db_seen=1)
            ticket["db_seen"] = 1


# --- attribution (D5: windows, then counts) ---------------------------------------------------------------------
INF = float("inf")


class NowView:
    """Windows as of now, for the completion transform (D5a.2): the gated ticket's own attempt is over, a sibling
    attempt with no live writer is over, and a sibling live receipt that is terminal closes its window."""

    def __init__(self, delivery_id: str, max_row: Optional[int]) -> None:
        self.delivery_id, self.max_row = delivery_id, max_row
        self._procs: Any = PENDING

    def writer_gone(self, attempt: dict[str, Any]) -> bool:
        if attempt["delivery_id"] == self.delivery_id and attempt["n"] == 1:
            return True
        if self._procs is PENDING:
            self._procs = probe.scan_processes()
        return probe.writer_pid(self._procs, attempt.get("dm_path")) is None

    def live_end(self, ticket: dict[str, Any]) -> Optional[float]:
        if ticket["delivery_id"] != self.delivery_id:
            receipt = _live_receipt(ticket)
            if not (isinstance(receipt, dict) and receipt.get("status") in RECEIPT_TERMINAL):
                return INF
        return None if self.max_row is None else float(self.max_row)


def _entries(ticket: dict[str, Any], view: Optional[NowView]) -> list[dict[str, Any]]:
    """One attribution window ``(lo, hi]`` per attempt that may have written the ticket's row. ``hi`` is ``INF``
    while the attempt may still write and None when its end row could not be read. A live ticket has one window,
    closed by the row read when its receipt was first seen terminal: the owner persists before it settles (D5a)."""
    ats = store.attempts(ticket["delivery_id"])
    if ticket["path"] == "live":
        first = ats[0] if ats else {"delivery_id": ticket["delivery_id"], "n": 1, "baseline": None}
        if ticket.get("receipt_row") is not None:
            hi: Optional[float] = float(ticket["receipt_row"])
        elif view is not None:
            hi = view.live_end(ticket)
        else:
            hi = None if ticket.get("unverified") else INF
        return [{"t": ticket, "a": first, "lo": first.get("baseline"), "hi": hi}]
    out = []
    for a in ats:
        if a["state"] == "over":
            hi = None if a["over_row"] is None else float(a["over_row"])
        elif view is not None and view.writer_gone(a):
            hi = None if view.max_row is None else float(view.max_row)
        else:
            hi = INF
        out.append({"t": ticket, "a": a, "lo": a["baseline"], "hi": hi})
    return out


def _cohorts(tickets: list[dict[str, Any]], view: Optional[NowView] = None) -> list[list[dict[str, Any]]]:
    """Cohorts of same-content attempt windows that overlap, transitively."""
    by_content: dict[str, list[dict[str, Any]]] = {}
    for t in tickets:
        # An ambiguous ticket stays in its cohort's windows: its possible row must not settle a sibling.
        if t["matched_row_id"] is not None or t["state"] not in ("pending", "ambiguous"):
            continue
        by_content.setdefault(t["content"], []).extend(_entries(t, view))
    out = []
    for spans in by_content.values():
        spans.sort(key=lambda e: (_lo(e), _hi(e)))
        current: list[dict[str, Any]] = []
        reach = -INF
        for e in spans:
            if current and _lo(e) >= reach:
                out.append(current)
                current = []
                reach = -INF
            current.append(e)
            reach = max(reach, _hi(e))
        if current:
            out.append(current)
    return out


def _lo(e: dict[str, Any]) -> float:
    return -INF if e["lo"] is None else float(e["lo"])


def _hi(e: dict[str, Any]) -> float:
    return INF if e["hi"] is None else float(e["hi"])


def _attribute(target_home: str, cohort: list[dict[str, Any]], *, persist: bool = True) -> dict[str, Any]:
    """One cohort's verdict per ticket: ``("seen", origin)`` | ``absent`` | ``undecidable`` | ``unknown`` | ``live``.

    T = the cohort's tickets, U = those whose attempts in it are all proven clean refusals, R = the unclaimed
    matching origins inside the cohort's windows: R = 0 nothing landed; R >= |T \\ U| every one landed (lowest row
    to lowest seq); otherwise which landed is undecidable."""
    tickets = {e["t"]["delivery_id"]: e["t"] for e in cohort}
    if any(t["state"] == "ambiguous" for t in tickets.values()):
        return {d: "undecidable" for d, t in tickets.items() if t["state"] == "pending"}
    if any(e["hi"] == INF for e in cohort):
        return {d: "live" for d in tickets}
    if any(t.get("undecidable") for t in tickets.values()):
        # Undecidable is sticky across the cohort: a later tick that sees only part of it must not re-split it.
        return {d: "undecidable" for d in tickets}
    if any(e["lo"] is None or e["hi"] is None for e in cohort):
        return {d: "unknown" for d in tickets}
    first = next(iter(tickets.values()))
    origins = probe.matching_origins(target_home, first["content"],
                                     db_seen=any(t["db_seen"] for t in tickets.values()))
    if origins == probe.UNKNOWN:
        return {d: "unknown" for d in tickets}
    claimed = {t["matched_row_id"] for t in store.tickets_for_pair(first["sender_home"], target_home)
               if t["matched_row_id"] is not None and t["delivery_id"] not in tickets}
    hits = sorted(o for o in origins if o not in claimed and any(e["lo"] < o <= e["hi"] for e in cohort))
    refused = {d for d in tickets if all(classify(attempt_outcome(e["t"], e["a"])) == "refusal"
                                         for e in cohort if e["t"]["delivery_id"] == d)}
    writers = sorted((t for d, t in tickets.items() if d not in refused), key=lambda t: t["seq"])
    result: dict[str, Any] = {d: "absent" for d in refused}
    if not hits:
        result.update({t["delivery_id"]: "absent" for t in writers})
    elif len(hits) >= len(writers):
        result.update({t["delivery_id"]: ("seen", row) for t, row in zip(writers, hits)})
    else:
        result.update({t["delivery_id"]: "undecidable" for t in writers})
        if persist:
            with store.txn() as conn:
                for t in writers:
                    store.update_ticket(t["delivery_id"], conn, undecidable=1)
                    t["undecidable"] = 1
    return result


def verdict_for(ticket: dict[str, Any], view: Optional[NowView] = None) -> Any:
    """This ticket's verdict over every cohort of its pair that holds one of its attempts."""
    subject = {**ticket, "state": "pending", "matched_row_id": None}
    pair = [subject if t["delivery_id"] == ticket["delivery_id"] else t
            for t in store.tickets_for_pair(ticket["sender_home"], ticket["target_home"])
            if t["content"] == ticket["content"]]
    found = []
    for cohort in _cohorts(pair, view):
        if any(e["t"]["delivery_id"] == ticket["delivery_id"] for e in cohort):
            verdict = _attribute(ticket["target_home"], cohort, persist=view is None).get(ticket["delivery_id"])
            if verdict is not None:
                found.append(verdict)
    return _combine(found) if found else "live"


def _may_hold(ticket: dict[str, Any], origin: int, *, default: bool) -> bool:
    """Whether ``origin`` can be this ticket's row: inside one of its CLI attempt windows, or above a live ticket's
    baseline (its owner may still flush the row after it settled). ``default`` answers an unknown bound."""
    if ticket["path"] == "live":
        lows = [a["baseline"] for a in store.attempts(ticket["delivery_id"])]
        return any(default if lo is None else lo < origin for lo in lows) if lows else default
    return any(default if e["lo"] is None or e["hi"] is None else e["lo"] < origin <= e["hi"]
               for e in _entries(ticket, None))


def lone_claim(ticket: dict[str, Any]) -> Any:
    """The row of a ticket that ended without one (a late flush, D5a.3): the lowest unclaimed matching origin it can
    hold that no other unmatched same-content ticket of the pair could own. None if there is none (or it is
    contested), ``probe.UNKNOWN`` when the transcript cannot be read."""
    origins = probe.matching_origins(ticket["target_home"], ticket["content"], db_seen=bool(ticket["db_seen"]))
    if origins == probe.UNKNOWN:
        return probe.UNKNOWN
    pair = store.tickets_for_pair(ticket["sender_home"], ticket["target_home"])
    claimed = {t["matched_row_id"] for t in pair if t["matched_row_id"] is not None}
    others = [t for t in pair if t["delivery_id"] != ticket["delivery_id"] and t["content"] == ticket["content"]
              and t["matched_row_id"] is None and t["state"] in ("pending", "ambiguous")]
    for origin in origins:
        if origin in claimed or not _may_hold(ticket, origin, default=False):
            continue
        return None if any(_may_hold(t, origin, default=True) for t in others) else origin
    return None


def _combine(verdicts: list[Any]) -> Any:
    for kind in ("live", "unknown"):
        if kind in verdicts:
            return kind
    seen = [v for v in verdicts if isinstance(v, tuple)]
    if seen:
        return seen[0]
    return "undecidable" if "undecidable" in verdicts else "absent"


# --- resolution -------------------------------------------------------------------------------------------------
def _finish(ticket: dict[str, Any], state: str, reason: Optional[str], *, reply: str = "", error: str = "",
            matched: Optional[int] = None, late_open: int = 0, settled_by: Optional[int] = None,
            unverified: int = 0) -> None:
    fields: dict[str, Any] = {"state": state, "reason": reason, "reply": reply, "error": error,
                              "late_open": late_open, "resolved_at": time.time(), "unverified": unverified}
    if matched is not None:
        fields["matched_row_id"] = matched
    if settled_by is not None:
        fields["settled_by"] = settled_by
    with store.txn() as conn:
        store.update_ticket(ticket["delivery_id"], conn, **fields)
    ticket.update(fields)
    logger.info("nv-delivery-redrive: %s -> %s (%s)", ticket["delivery_id"], state, reason or "-")


def _settle_from_row(ticket: dict[str, Any], origin: int) -> None:
    ats = store.attempts(ticket["delivery_id"])
    writer = next((a for a in reversed(ats) if a["baseline"] is not None and a["over_row"] is not None
                   and a["baseline"] < origin <= a["over_row"]), ats[-1] if ats else None)
    outcome = attempt_outcome(ticket, writer) if writer else None
    reply = _clean((outcome or {}).get("out", "")) if classify(outcome) == "exit0" else ""
    reply = reply or probe.first_assistant_after(ticket["target_home"], origin)
    _finish(ticket, "settled", None, reply=reply, matched=origin, settled_by=writer["n"] if writer else None)


def _live_receipt(ticket: dict[str, Any]) -> Any:
    from tools import bot_live_delivery

    try:
        return bot_live_delivery.read_delivery_result(ticket["target_home"], ticket["delivery_id"])
    except Exception:
        logger.debug("nv-delivery-redrive: receipt read failed for %s", ticket["delivery_id"], exc_info=True)
        return probe.UNKNOWN


def _close_live_window(ticket: dict[str, Any]) -> bool:
    """Record the row id that closes a live ticket's window, read once its receipt is terminal: the owner persists
    the row before it settles (D5a). False when the transcript cannot be read."""
    if ticket.get("receipt_row") is not None:
        return True
    state, row = probe.max_row(ticket["target_home"], db_seen=bool(ticket["db_seen"]))
    if row is None:
        return False
    store.update_ticket(ticket["delivery_id"], receipt_row=row, db_seen=1 if state == "ok" else ticket["db_seen"])
    ticket["receipt_row"], ticket["db_seen"] = row, 1 if state == "ok" else ticket["db_seen"]
    return True


def _settle_live(ticket: dict[str, Any], receipt: dict[str, Any], now: float) -> None:
    """D5a.3: a terminal ``settled`` receipt is taken only with a row claimed for this ticket."""
    reply = str(receipt.get("reply") or "")
    verdict = verdict_for(ticket) if _close_live_window(ticket) else "unknown"
    if isinstance(verdict, tuple):
        _finish(ticket, "settled", None, reply=reply, matched=verdict[1])
    elif verdict in ("absent", "unknown") or now >= ticket["deadline"]:
        _finish(ticket, "ambiguous", "unknown", reply=reply, error="no row for this delivery in the target's Bot Chat",
                unverified=1)


def _follow_live(ticket: dict[str, Any], now: float) -> None:
    """Ruling B1: an admitted delivery is followed and reported, never re-sent and never re-admitted."""
    from tools import bot_live_delivery
    from tools.bot_failure_reasons import QUEUED_EXPIRED

    receipt = _live_receipt(ticket)
    if isinstance(receipt, dict) and receipt.get("status") == "settled":
        _settle_live(ticket, receipt, now)
        return
    if isinstance(receipt, dict) and receipt.get("status") in RECEIPT_TERMINAL:
        _finish(ticket, receipt["status"], receipt.get("reason") or "unknown", reply=str(receipt.get("reply") or ""),
                error=str(receipt.get("error") or ""))
        return
    if now < ticket["deadline"]:
        return
    if isinstance(receipt, dict) and receipt.get("status") == "queued":
        try:
            receipt = bot_live_delivery.cancel_queued_delivery(
                ticket["target_home"], ticket["delivery_id"], error="queued delivery expired before its owner "
                "claimed it", reason=QUEUED_EXPIRED)
        except Exception:
            logger.warning("nv-delivery-redrive: bound cancel of %s failed", ticket["delivery_id"], exc_info=True)
            receipt = probe.UNKNOWN
        if isinstance(receipt, dict) and receipt.get("status") == "cancelled":
            _finish(ticket, "failed", QUEUED_EXPIRED, error=str(receipt.get("error") or ""))
            return
        if isinstance(receipt, dict) and receipt.get("status") in RECEIPT_TERMINAL:
            _finish(ticket, receipt["status"], receipt.get("reason") or None, reply=str(receipt.get("reply") or ""),
                    error=str(receipt.get("error") or ""))
            return
    _finish(ticket, "ambiguous", "unknown", error="the live owner has not reported this delivery's outcome",
            late_open=1)


def _late_check(ticket: dict[str, Any], procs) -> None:
    """A ticket that ended at its bound with an attempt still open: one late outcome once that attempt resolves."""
    if ticket["path"] == "live":
        receipt = _live_receipt(ticket)
        if not (isinstance(receipt, dict) and receipt.get("status") in RECEIPT_TERMINAL):
            return
        late = {"state": receipt["status"], "reason": receipt.get("reason") or None,
                "reply": str(receipt.get("reply") or ""), "error": str(receipt.get("error") or "")}
        if receipt["status"] == "settled":
            verdict = verdict_for(ticket) if _close_live_window(ticket) else "unknown"
            if verdict == "live":
                return
            if isinstance(verdict, tuple):
                late["matched_row_id"] = verdict[1]
            else:
                late.update(state="ambiguous", reason="unknown", unverified=1)
    else:
        _reconcile(procs, ticket)
        ats = store.attempts(ticket["delivery_id"])
        if any(a["state"] != "over" for a in ats):
            return
        origins = probe.matching_origins(ticket["target_home"], ticket["content"], db_seen=bool(ticket["db_seen"]))
        if origins == probe.UNKNOWN:
            return
        claimed = {t["matched_row_id"] for t in store.tickets_for_pair(ticket["sender_home"], ticket["target_home"])
                   if t["matched_row_id"] is not None}
        hits = [o for o in origins if o not in claimed and any(
            a["baseline"] is not None and a["over_row"] is not None and a["baseline"] < o <= a["over_row"] for a in ats)]
        if hits:
            late = {"state": "settled", "reason": None, "matched_row_id": hits[0],
                    "reply": probe.first_assistant_after(ticket["target_home"], hits[0]), "error": ""}
        elif classify(attempt_outcome(ticket, ats[-1])) == "exit0":
            late = {"state": "ambiguous", "reason": "unknown", "reply": "", "error": "", "unverified": 1}
        else:
            late = {"state": "failed", "reason": "unknown", "reply": "",
                    "error": "the attempt that was still open at the bound ended without a row"}
    logger.info("nv-delivery-redrive: %s late outcome %s (%s)", ticket["delivery_id"], late["state"],
                late["reason"] or "-")
    ticket["late_open"] = 0
    with store.txn() as conn:
        fields: dict[str, Any] = {"late_open": 0, "late_outcome": json.dumps(late), "resolved_at": time.time()}
        if late.get("matched_row_id") is not None:
            fields["matched_row_id"] = late["matched_row_id"]
        if late.get("unverified"):
            fields["unverified"] = ticket["unverified"] = 1
        store.update_ticket(ticket["delivery_id"], conn, **fields)
        text = notify.text_for(ticket, late["state"], late["reason"], late["reply"], late["error"], late=True)
        for recipient in (notify.SENDER, notify.ORCHESTRATOR):
            notify.queue(conn, ticket, recipient, notify.LATE, late["state"], late["reason"], text)


def _late_verify(ticket: dict[str, Any]) -> None:
    """D5a.3: a row claimed for an unverified ticket before it is collected turns it ``settled``."""
    origin = lone_claim(ticket)
    if origin is None or origin == probe.UNKNOWN:
        return
    reply = ticket.get("reply") or probe.first_assistant_after(ticket["target_home"], origin)
    late = {"state": "settled", "reason": None, "matched_row_id": origin, "reply": reply, "error": ""}
    logger.info("nv-delivery-redrive: %s verified late (row %s)", ticket["delivery_id"], origin)
    with store.txn() as conn:
        store.update_ticket(ticket["delivery_id"], conn, state="settled", reason=None, reply=reply, error="",
                            matched_row_id=origin, unverified=0, late_outcome=json.dumps(late),
                            resolved_at=time.time())
        ticket.update(state="settled", reason=None, reply=reply, matched_row_id=origin, unverified=0)
        text = notify.text_for(ticket, "settled", None, reply, "", late=True)
        for recipient in (notify.SENDER, notify.ORCHESTRATOR):
            notify.queue(conn, ticket, recipient, notify.VERIFIED, "settled", None, text)


def _in_flight_grace(ticket: dict[str, Any], procs, now: float) -> bool:
    """D6.5 (E4 r2): a CLI re-drive runner still alive at the bound with no target turn running (booting or waiting
    on the flock) is waited for, up to its create_time + turn_wait_seconds() + 60 s."""
    ats = store.attempts(ticket["delivery_id"])
    last = ats[-1] if ats else None
    if last is None or last["state"] == "over" or not last.get("dm_path"):
        return False
    if probe.target_turn_pid(procs, last["dm_path"]) is not None:
        return False
    born = last.get("pid_create_time") or last.get("spawned_at") or now
    return now < born + _turn_wait() + GRACE_BOOT_SECONDS


def _bound(ticket: dict[str, Any], procs, verdict: Any, now: float) -> None:
    from tools.bot_failure_reasons import QUEUED_EXPIRED

    ats = store.attempts(ticket["delivery_id"])
    logger.debug("nv-delivery-redrive: bound %s verdict=%r attempts=%s", ticket["delivery_id"], verdict,
                 [(a["n"], a["state"], a["baseline"], a["over_row"]) for a in ats])
    still_open = any(a["state"] != "over" for a in ats)
    last = classify(attempt_outcome(ticket, ats[-1])) if ats else "refusal"
    if still_open or verdict in ("live", "unknown", "undecidable"):
        _finish(ticket, "ambiguous", "unknown", error="an attempt may still land or its row is unproven",
                late_open=1 if still_open else 0, unverified=int(not still_open and last == "exit0"))
        return
    if not ticket["content_verified"]:
        _finish(ticket, "ambiguous", "unknown", error="the payload could not be proven identical")
        return
    if last == "exit0":
        _finish(ticket, "ambiguous", "unknown", error="the turn ran but its row is unproven", unverified=1)
    elif last == "failed":
        from tools.bot_failure_reasons import classify_agent_error

        out = _clean((attempt_outcome(ticket, ats[-1]) or {}).get("out", ""))
        _finish(ticket, "failed", classify_agent_error(out), error=out[-500:])
    else:
        _finish(ticket, "failed", QUEUED_EXPIRED,
                error=f"the target stayed busy; not delivered within the {int(settings.get('redrive_window_seconds'))}"
                      f" s re-drive window ({len(ats)} attempts)")


def _advance(ticket: dict[str, Any], verdict: Any, procs, now: float, may_redrive: bool) -> None:
    """D5 table + D6 steps 3-5 for one open CLI ticket whose cohort verdict is known."""
    ats = store.attempts(ticket["delivery_id"])
    if isinstance(verdict, tuple):
        _settle_from_row(ticket, verdict[1])
        return
    if verdict == "undecidable":
        store.update_ticket(ticket["delivery_id"], undecidable=1)
        ticket["undecidable"] = 1
    over_attempts = all(a["state"] == "over" for a in ats)
    last = classify(attempt_outcome(ticket, ats[-1])) if ats else "none"
    if over_attempts and last == "pending":
        return
    if over_attempts and verdict == "absent" and last == "failed" and ticket["content_verified"]:
        _bound(ticket, procs, verdict, now)
        return
    if over_attempts and verdict == "absent" and last == "exit0":
        _finish(ticket, "ambiguous", "unknown", error="the turn ran but its row is unproven", unverified=1)
        return
    if over_attempts and verdict == "absent" and last == "live":
        store.update_ticket(ticket["delivery_id"], path="live")
        ticket["path"] = "live"
        _follow_live(ticket, now)
        return
    at_bound = now >= ticket["deadline"] or (over_attempts and len(ats) >= settings.get("max_attempts"))
    if at_bound:
        if ticket.get("undecidable") or verdict in ("unknown", "undecidable"):
            _bound(ticket, procs, "undecidable" if ticket.get("undecidable") else verdict, now)
            return
        if not over_attempts and _in_flight_grace(ticket, procs, now):
            return
        _bound(ticket, procs, verdict, now)
        return
    retry_ok = (over_attempts and verdict == "absent" and not ticket.get("undecidable")
                and (last == "refusal" or (last == "none" and ticket["content_verified"])))
    if not (retry_ok and may_redrive and ticket["content_verified"]):
        return
    if now < ticket["next_attempt_at"] or len(ats) >= settings.get("max_attempts"):
        return
    if now + _turn_wait() >= ticket["deadline"]:
        return
    receipt = _live_receipt(ticket)
    if receipt is probe.UNKNOWN:
        return
    if isinstance(receipt, dict):
        store.update_ticket(ticket["delivery_id"], path="live")
        ticket["path"] = "live"
        _follow_live(ticket, now)
        return
    state, baseline = probe.max_row(ticket["target_home"], db_seen=bool(ticket["db_seen"]))
    if state == probe.UNKNOWN or baseline is None:
        return
    _launch(ticket, len(ats) + 1, baseline, procs, now)


# --- notices (D7 / D7a) -----------------------------------------------------------------------------------------
def _sender_decision(ticket: dict[str, Any]) -> str:
    """``notice`` | ``skip`` (core's attempt-1 completion carried this settled outcome) | ``defer``."""
    if ticket["state"] != "settled" or ticket.get("published_status") == "ambiguous":
        return "notice"
    if ticket.get("settled_by") not in (None, 1) and ticket["path"] == "cli":
        return "notice"
    pid = ticket.get("process_id")
    if not pid:
        return "notice"
    session = _registry().get(pid)
    in_scope = session is not None and not session.detached and session._reader_thread is not None
    if in_scope:
        if not session._completion_event.is_set():
            return "defer"
        from . import notify as _n

        return "skip" if _n.core_carried(ticket, session.output_buffer, session.exit_code) else "notice"
    if session is not None and not session._completion_event.is_set() and session.detached:
        return "defer"
    return "skip" if notify.transcript_carried(ticket) else "notice"


def _decide_notices(orch_live: bool) -> None:
    current = epoch()
    for ticket in store.undecided_notices(current):
        decision = _sender_decision(ticket)
        if decision == "defer":
            continue
        with store.txn() as conn:
            text = notify.text_for(ticket, ticket["state"], ticket["reason"], ticket.get("reply") or "",
                                   ticket.get("error") or "")
            if decision == "notice":
                notify.queue(conn, ticket, notify.SENDER, notify.FINAL, ticket["state"], ticket["reason"], text)
            orch_due = ticket["state"] != "settled" or (ticket["path"] == "live" and decision == "notice")
            if orch_due:
                notify.queue(conn, ticket, notify.ORCHESTRATOR, notify.FINAL, ticket["state"], ticket["reason"], text)
            store.update_ticket(ticket["delivery_id"], conn,
                                notice_state="done" if decision == "notice" else f"skip:{current}")


def _collect(now: float) -> None:
    cutoff = now - settings.get("retention_seconds")
    for ticket in store.collectable(cutoff):
        for attempt in store.attempts(ticket["delivery_id"]):
            for suffix in ("", ".rc", ".err"):
                if attempt.get("out_path"):
                    with contextlib.suppress(OSError):
                        os.unlink(attempt["out_path"] + suffix)
        with store.txn() as conn:
            store.delete_ticket(conn, ticket["delivery_id"])


# --- the tick ---------------------------------------------------------------------------------------------------
def tick(**kwargs: Any) -> None:
    """``on_kanban_dispatch_tick``: never raises (invoke_hook would swallow it, but the next tick must run)."""
    now = time.time()
    store.set_heartbeat(now)
    _reap_children()
    root = store.root()
    orch = settings.orchestrator(root)
    procs = probe.scan_processes()
    pairs: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for ticket in store.open_tickets():
        pairs.setdefault((ticket["sender_home"], ticket["target_home"]), []).append(ticket)
    for key, tickets in pairs.items():
        tickets.extend(t for t in store.cohort_peers(*key) if t["delivery_id"] not in {x["delivery_id"] for x in tickets})
    for (_sender_home, target_home), tickets in pairs.items():
        try:
            _tick_pair(target_home, tickets, procs, now)
        except Exception:
            logger.warning("nv-delivery-redrive: tick failed for target %s", target_home, exc_info=True)
    try:
        store.expire_inflight(now, INFLIGHT_KEEP_SECONDS)
        _decide_notices(orch is not None)
        notify.deliver_pending(orch)
        _collect(now)
    except Exception:
        logger.warning("nv-delivery-redrive: notice pass failed", exc_info=True)


def _tick_pair(target_home: str, tickets: list[dict[str, Any]], procs, now: float) -> None:
    for ticket in tickets:
        if ticket["state"] == "pending":
            _reconcile(procs, ticket)
    for ticket in tickets:
        if ticket["state"] != "pending" and ticket["late_open"]:
            _late_check(ticket, procs)
        elif ticket["state"] == "ambiguous" and ticket.get("unverified") and ticket["matched_row_id"] is None:
            _late_verify(ticket)
    live = [t for t in tickets if t["state"] == "pending" and t["path"] == "live"]
    for ticket in live:
        _follow_live(ticket, now)
    per_ticket: dict[str, list[Any]] = {}
    for cohort in _cohorts(tickets):
        for did, verdict in _attribute(target_home, cohort).items():
            per_ticket.setdefault(did, []).append(verdict)
    verdicts = {did: _combine(found) for did, found in per_ticket.items()}
    blocked = False
    for ticket in sorted(tickets, key=lambda t: t["seq"]):
        if ticket["state"] != "pending":
            blocked = blocked or bool(ticket["late_open"])
            continue
        if ticket["path"] == "cli":
            _advance(ticket, verdicts.get(ticket["delivery_id"], "live"), procs, now, may_redrive=not blocked)
        if ticket["state"] == "pending" or ticket.get("late_open"):
            blocked = True
