"""Typed final outcomes as notice rows (ADR D7) and the one-notice rule for ``settled`` outcomes (D7a, E4).

A notice is a ``process_complete`` row, the shape core's own ``_persist_reply_when_done`` writes. The append is
idempotent: the recipient's lineage is searched first for a row carrying the same ``(delivery_id, notice_kind)``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Optional

from . import store

logger = logging.getLogger(__name__)

SENDER = "sender"
ORCHESTRATOR = "orchestrator"
FINAL = "final"
LATE = "late"
VERIFIED = "verified"
_META_KEY = "nv_delivery_redrive"
UNVERIFIED = ("Could not verify your message in the target's Bot Chat; delivery outcome is unknown. Do NOT resend. "
              "Check the target before considering another send.")
# The D-3 sentence a FINAL carries while a late path is open, by the ticket's ``final_suffix`` snapshot.
_LATER = {"owner_settles": "A later outcome will be reported if the owner settles.",
          "verifiable": "A later outcome will be reported if the message becomes verifiable."}


def text_for(ticket: dict[str, Any], state: str, reason: Optional[str], reply: str, error: str, *,
             late: bool = False) -> str:
    did, target = ticket["delivery_id"], ticket["target"]
    if ticket.get("path") == "core":
        detail = error.rstrip().removesuffix(".")
        if state == "ambiguous":
            return (f"Delivery {did} to @{target} outcome unknown [reason: unknown]: {detail}. The sender was told not "
                    "to resend; the plugin does not track this delivery.")
        return (f"Delivery {did} to @{target} {state} [reason: {reason or 'unknown'}]: {detail}. The sender has core's "
                "own answer; the plugin does not re-send it.")
    head = "Late outcome: delivery" if late else "Delivery"
    later = "" if late else _LATER.get(ticket.get("final_suffix") or "", "")
    later = f" {later}" if later else ""
    if state == "ambiguous" and ticket.get("unverified"):
        return f"{head} {did} to @{target}: {UNVERIFIED}{later}"
    if state == "settled":
        verdict, detail = "settled — reply follows", reply or "(empty reply)"
    elif state == "ambiguous":
        verdict, detail = f"outcome unknown [reason: {reason or 'unknown'}]", error or "the outcome could not be proven"
    else:
        verdict, detail = f"{state} [reason: {reason or 'unknown'}]", error or "the message was NOT delivered"
    tail = " Do NOT resend."
    if state == "ambiguous":
        tail = " Do NOT resend; check their Bot Chat before resending."
    return f"{head} {did} to @{target} {verdict}: {detail}.{tail}{later}"


def queue(conn, ticket: dict[str, Any], recipient: str, kind: str, state: str, reason: Optional[str],
          text: str) -> None:
    meta = {"delivery_id": ticket["delivery_id"], "target": ticket["target"], "state": state,
            "reason": reason, "notice_kind": kind, "recipient": recipient}
    store.queue_notice(conn, ticket["delivery_id"], recipient, kind, text, meta)


def _open_db(home: Path):
    from hermes_state import SessionDB

    return SessionDB(db_path=Path(home) / "state.db")


def _bot_chat_tip(db, *, create: bool) -> Optional[str]:
    row = db.get_session_by_title("Bot Chat")
    if row is not None:
        return db.get_compression_tip(row["id"]) or row["id"]
    if not create:
        return None
    sid = db.create_session(uuid.uuid4().hex, source="cli")
    db.set_session_title(sid, "Bot Chat")
    return sid


def _already_appended(db, session_id: str, delivery_id: str, kind: str, recipient: str) -> bool:
    for message in db.get_messages(session_id, include_inactive=True, include_ancestors=True):
        if message.get("display_kind") != "process_complete":
            continue
        meta = (message.get("display_metadata") or {}).get(_META_KEY) or {}
        if (meta.get("delivery_id"), meta.get("notice_kind"), meta.get("recipient", recipient)) == (
                delivery_id, kind, recipient):
            return True
    return False


def append(notice: dict[str, Any], ticket: dict[str, Any], orchestrator: Optional[tuple[str, Path]]) -> bool:
    """Append one pending notice; True once it is in the recipient's transcript (now or from an earlier try)."""
    if notice["recipient"] == SENDER:
        home, session = Path(ticket["sender_home"]), ticket.get("sender_session_id")
    elif orchestrator is not None:
        home, session = orchestrator[1], None
    else:
        return False
    meta = json.loads(notice["metadata_json"])
    db = _open_db(home)
    try:
        if session:
            tip = db.get_compression_tip(session) or session
        else:
            tip = _bot_chat_tip(db, create=notice["recipient"] == ORCHESTRATOR)
        if not tip:
            return False
        if not _already_appended(db, tip, notice["delivery_id"], notice["notice_kind"], notice["recipient"]):
            db.append_message(tip, "user", content=notice["text"], display_kind="process_complete",
                              display_metadata={"display_text": notice["text"], _META_KEY: meta})
    finally:
        db.close()
    return True


@contextlib.contextmanager
def _delivery_lock():
    """One delivery pass at a time per install root: the appended-check, the append and the mark span three
    databases, so overlapping ticks could otherwise both pass the check before either appends."""
    import fcntl

    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from plugins.plugin_storage import plugin_data_dir

    token = set_hermes_home_override(store.root())
    try:
        path = plugin_data_dir(store.PLUGIN_NAME) / "notices.lock"
    finally:
        reset_hermes_home_override(token)
    with open(path, "a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def deliver_pending(orchestrator: Optional[tuple[str, Path]]) -> None:
    with _delivery_lock():
        pending = store.pending_notices()
        # A failed FINAL append is retried next tick while later rows go on: its LATE/VERIFIED waits behind it.
        final_due = {(n["delivery_id"], n["recipient"]) for n in pending if n["notice_kind"] == FINAL}
        for notice in pending:
            key = (notice["delivery_id"], notice["recipient"])
            if notice["notice_kind"] != FINAL and key in final_due:
                continue
            ticket = store.ticket(notice["delivery_id"])
            if ticket is None:
                continue
            try:
                if append(notice, ticket, orchestrator):
                    store.mark_notice_appended(notice["delivery_id"], notice["recipient"], notice["notice_kind"])
                    if notice["notice_kind"] == FINAL:
                        final_due.discard(key)
            except Exception:
                logger.warning("nv-delivery-redrive: notice %s/%s for %s not appended yet; retrying next tick",
                               notice["recipient"], notice["notice_kind"], notice["delivery_id"], exc_info=True)


def _payload_settled(text: str, delivery_id: str) -> bool:
    for line in reversed([ln for ln in str(text or "").split("\n") if ln.strip()]):
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        return (isinstance(payload, dict) and payload.get("delivery_id") == delivery_id
                and payload.get("status") == "settled")
    return False


def core_carried(ticket: dict[str, Any], output: str, exit_code: Any) -> bool:
    """Whether core's attempt-1 completion carried this ``settled`` outcome itself."""
    if ticket["path"] == "live":
        return _payload_settled(output, ticket["delivery_id"])
    from .send import clean_refusal

    return exit_code == 0 and bool(str(output or "").strip()) and not clean_refusal(output)


def transcript_carried(ticket: dict[str, Any]) -> bool:
    """(b′): the sender's originating session already holds core's completion row carrying the settled outcome. Core
    persists that completion as a ``process_complete`` row, or, through a ``-Q`` one-shot's linger, as a plain user turn
    (``hermes_cli/quiet_single_query.py:192-200``), so every user row naming this process is checked."""
    pid = ticket.get("process_id")
    if not pid:
        return False
    db = None
    try:
        db = _open_db(Path(ticket["sender_home"]))
        session = ticket.get("sender_session_id")
        tip = (db.get_compression_tip(session) or session) if session else _bot_chat_tip(db, create=False)
        if not tip:
            return False
        for message in db.get_messages(tip, include_inactive=True, include_ancestors=True):
            content = str(message.get("content") or "")
            if message.get("role") != "user" or f"Background process {pid}" not in content:
                continue
            output = content.partition("Output:\n")[2]
            if ticket["path"] == "live":
                if _payload_settled(output.rstrip("]"), ticket["delivery_id"]):
                    return True
            elif ticket.get("reply") and ticket["reply"].strip() in output:
                return True
        return False
    except Exception:
        logger.debug("nv-delivery-redrive: sender transcript check failed", exc_info=True)
        return False
    finally:
        if db is not None:
            db.close()
