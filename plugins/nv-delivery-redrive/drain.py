"""The durable owner (ADR D6): one pass per kanban dispatch tick, in the gateway process.

Core admits a local delivery to its target's FIFO and owns it (v0.22.5). Each tick writes the heartbeat and
follows every open ticket's receipt: a ``settled`` receipt is taken only with a row claimed for the ticket (D5a), a
receipt still open at the bound ends ``unknown`` with at most one late notice to follow. The drainer never re-sends,
re-admits, spawns a target turn, or cancels an admitted receipt.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
import weakref
from typing import Any, Optional

from . import notify, probe, settings, store

logger = logging.getLogger(__name__)

RECEIPT_TERMINAL = ("settled", "failed", "cancelled", "ambiguous")
INFLIGHT_KEEP_SECONDS = 600.0

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


def _last_json(text: str) -> Optional[dict[str, Any]]:
    for line in reversed([ln for ln in str(text or "").split("\n") if ln.strip()]):
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        return payload if isinstance(payload, dict) else None
    return None


# --- attribution (D5: windows, then counts) ---------------------------------------------------------------------
INF = float("inf")


class NowView:
    """Windows as of now, for the completion transform (D5a.2): a sibling live receipt that is not terminal keeps
    its window open; the gated ticket's own window closes at the current max row."""

    def __init__(self, delivery_id: str, max_row: Optional[int]) -> None:
        self.delivery_id, self.max_row = delivery_id, max_row

    def live_end(self, ticket: dict[str, Any]) -> Optional[float]:
        if ticket["delivery_id"] != self.delivery_id:
            receipt = _live_receipt(ticket)
            if not (isinstance(receipt, dict) and receipt.get("status") in RECEIPT_TERMINAL):
                return INF
        return None if self.max_row is None else float(self.max_row)


def _entries(ticket: dict[str, Any], view: Optional[NowView]) -> list[dict[str, Any]]:
    """One attribution window ``(lo, hi]`` per attempt that may have written the ticket's row. ``hi`` is ``INF``
    while the attempt may still write and None when its end row could not be read. A live ticket has one window,
    closed by the row read when its receipt was first seen terminal: the owner persists before it settles (D5a).
    A v0.22.2 attempt that never ended keeps ``INF``: nothing can prove it over any more."""
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

    T = the cohort's tickets, R = the unclaimed matching origins inside the cohort's windows: R = 0 nothing landed;
    R >= |T| every one landed (lowest row to lowest seq); otherwise which landed is undecidable."""
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
    writers = sorted(tickets.values(), key=lambda t: t["seq"])
    if not hits:
        return {t["delivery_id"]: "absent" for t in writers}
    if len(hits) >= len(writers):
        return {t["delivery_id"]: ("seen", row) for t, row in zip(writers, hits)}
    if persist:
        with store.txn() as conn:
            for t in writers:
                store.update_ticket(t["delivery_id"], conn, undecidable=1)
                t["undecidable"] = 1
    return {t["delivery_id"]: "undecidable" for t in writers}


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
    """Whether ``origin`` can be this ticket's row: inside one of its v0.22.2 attempt windows, or above a live
    ticket's baseline (its owner may still flush the row after it settled). ``default`` answers an unknown bound."""
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
def _final_suffix(ticket: dict[str, Any], late_open: int, unverified: int, matched: Optional[int]) -> str:
    """Which later-outcome sentence the FINAL promises (D-3), fixed when the ticket ends; ``""`` promises none."""
    if ticket["path"] != "live":
        return ""
    if late_open:
        return "owner_settles"
    return "verifiable" if unverified and matched is None else ""


def _finish(ticket: dict[str, Any], state: str, reason: Optional[str], *, reply: str = "", error: str = "",
            matched: Optional[int] = None, late_open: int = 0, unverified: int = 0) -> bool:
    """End a pending ticket. Only the first writer wins (ticks overlap); a loser takes the stored ticket."""
    fields: dict[str, Any] = {"state": state, "reason": reason, "reply": reply, "error": error,
                              "late_open": late_open, "resolved_at": time.time(), "unverified": unverified,
                              "final_suffix": _final_suffix(ticket, late_open, unverified, matched)}
    if matched is not None:
        fields["matched_row_id"] = matched
    with store.txn() as conn:
        won = store.finish_ticket(conn, ticket["delivery_id"], fields)
    if not won:
        ticket.update(store.ticket(ticket["delivery_id"]) or {})
        return False
    ticket.update(fields)
    logger.info("nv-delivery-redrive: %s -> %s (%s)", ticket["delivery_id"], state, reason or "-")
    return True


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
    """Ruling B1 / E4′ item 4: an admitted delivery is followed and reported, never re-sent, re-admitted or
    cancelled; at the bound only an unadmitted legacy record (no ``admission_id``) is cancelled."""
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
    if isinstance(receipt, dict) and receipt.get("status") == "queued" and not receipt.get("admission_id"):
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


def _legacy_pending(ticket: dict[str, Any], now: float) -> None:
    """E4′ item 5: a ``cli`` ticket the v0.22.2 plugin left pending is followed from its receipt, or ends unknown."""
    receipt = _live_receipt(ticket)
    if receipt is probe.UNKNOWN and now < ticket["deadline"]:
        return
    if isinstance(receipt, dict):
        store.update_ticket(ticket["delivery_id"], path="live")
        ticket["path"] = "live"
        _follow_live(ticket, now)
        return
    _finish(ticket, "ambiguous", "unknown", error="pending across the v0.22.5 upgrade; this version does not re-drive")


# --- the one late outcome (D-3, E4′ item 6) ---------------------------------------------------------------------
def _claim_late(ticket: dict[str, Any], late: dict[str, Any], kind: Optional[str], **fields: Any) -> bool:
    """Claim the ticket's one late outcome and queue its notices (``kind``; None closes the late path silently) in
    one transaction: overlapping ticks race here, and only ``late_outcome IS NULL`` decides the winner."""
    fields = {"late_open": 0, "late_outcome": json.dumps(late), **fields}
    if kind is not None:
        fields["resolved_at"] = time.time()
    with store.txn() as conn:
        if not store.claim_late(conn, ticket["delivery_id"], fields):
            return False
        ticket.update(fields)
        if kind is not None:
            text = notify.text_for(ticket, late["state"], late["reason"], late["reply"], late["error"], late=True)
            for recipient in (notify.SENDER, notify.ORCHESTRATOR):
                notify.queue(conn, ticket, recipient, kind, late["state"], late["reason"], text)
    logger.info("nv-delivery-redrive: %s late outcome %s (%s)", ticket["delivery_id"], late.get("state"),
                late.get("reason") or "-")
    return True


def _late_check(ticket: dict[str, Any], now: float) -> None:
    """A live ticket that ended ``unknown`` at its bound, once its receipt is terminal."""
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
            _claim_late(ticket, {**late, "matched_row_id": verdict[1]}, notify.LATE, matched_row_id=verdict[1])
            return
        # DN-E4-9 (1): settled with no row for it yet; the row is looked for until the window ends.
        until = store.start_verify_window(ticket["delivery_id"], now + settings.get("late_verify_seconds"))
        ticket["late_verify_until"] = until
        return
    if receipt["status"] in ("failed", "cancelled"):
        _claim_late(ticket, late, notify.LATE)
        return
    # An ``ambiguous`` receipt is the owner's last word but not a definite outcome: the FINAL ``unknown`` stands.
    _claim_late(ticket, late, None)


def _late_verify(ticket: dict[str, Any], now: float) -> None:
    """D5a.3: a row claimed for an unverified ticket turns it ``settled`` (VERIFIED). A ticket in a DN-E4-9 window
    that ends with no such row gets the one final LATE unverified notice."""
    origin = lone_claim(ticket)
    if origin is not None and origin != probe.UNKNOWN:
        receipt = _live_receipt(ticket)
        reply = str(receipt.get("reply") or "") if isinstance(receipt, dict) else ""
        reply = reply or ticket.get("reply") or probe.first_assistant_after(ticket["target_home"], origin)
        late = {"state": "settled", "reason": None, "matched_row_id": origin, "reply": reply, "error": ""}
        _claim_late(ticket, late, notify.VERIFIED, state="settled", reason=None, reply=reply, error="",
                    matched_row_id=origin, unverified=0)
        return
    until = ticket.get("late_verify_until")
    if until is not None and now >= until:
        late = {"state": "ambiguous", "reason": "unknown", "reply": "", "error": ""}
        _claim_late(ticket, late, notify.LATE, unverified=1)


def _legacy_watch(ticket: dict[str, Any], now: float) -> None:
    """DN-E4-11 (1): a ``cli`` ticket the v0.22.2 plugin closed with a late check open. Only one whose attempt windows
    are all bounded is watched, until its retention ends, for a row it alone can claim; the rest close silently."""
    ats = store.attempts(ticket["delivery_id"])
    bounded = bool(ats) and all(a["state"] == "over" and a["over_row"] is not None for a in ats)
    resolved = ticket.get("resolved_at") or now
    if bounded and now < resolved + settings.get("retention_seconds"):
        origin = lone_claim(ticket)
        if origin is None or origin == probe.UNKNOWN:
            return
        late = {"state": "settled", "reason": None, "matched_row_id": origin, "error": "",
                "reply": probe.first_assistant_after(ticket["target_home"], origin)}
        _claim_late(ticket, late, notify.LATE, matched_row_id=origin)
        return
    _claim_late(ticket, {"state": None, "reason": None, "reply": "", "error": "",
                         "closed": "v0.22.5 upgrade" if not bounded else "watch ended"}, None)


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
        if ticket["path"] == "core":
            # DN-E4-12: the sender already holds core's own answer.
            with store.txn() as conn:
                text = notify.text_for(ticket, ticket["state"], ticket["reason"], "", ticket.get("error") or "")
                notify.queue(conn, ticket, notify.ORCHESTRATOR, notify.FINAL, ticket["state"], ticket["reason"], text)
                store.update_ticket(ticket["delivery_id"], conn, notice_state="done")
            continue
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
        with store.txn() as conn:
            store.delete_ticket(conn, ticket["delivery_id"])


# --- the tick ---------------------------------------------------------------------------------------------------
def tick(**kwargs: Any) -> None:
    """``on_kanban_dispatch_tick``: never raises (invoke_hook would swallow it, but the next tick must run)."""
    now = time.time()
    store.set_heartbeat(now)
    orch = settings.orchestrator(store.root())
    try:
        store.backfill_final_suffix()
    except Exception:
        logger.warning("nv-delivery-redrive: final_suffix backfill failed", exc_info=True)
        return
    for ticket in store.open_tickets():
        try:
            _step(ticket, now)
        except Exception:
            logger.warning("nv-delivery-redrive: tick failed for %s", ticket["delivery_id"], exc_info=True)
    try:
        store.reap_inflight(now, INFLIGHT_KEEP_SECONDS)
        _decide_notices(orch is not None)
        notify.deliver_pending(orch)
        _collect(now)
    except Exception:
        logger.warning("nv-delivery-redrive: notice pass failed", exc_info=True)


def _step(ticket: dict[str, Any], now: float) -> None:
    if ticket["state"] == "pending":
        (_follow_live if ticket["path"] == "live" else _legacy_pending)(ticket, now)
    elif ticket.get("notice_state") != "done" or ticket.get("late_outcome"):
        # E4′ 6a: no late notice before both FINALs are queued, and none after the one late outcome.
        return
    elif ticket["late_open"] and ticket["path"] != "live":
        _legacy_watch(ticket, now)
    elif ticket["late_open"] and ticket.get("late_verify_until") is None:
        _late_check(ticket, now)
    else:
        _late_verify(ticket, now)
