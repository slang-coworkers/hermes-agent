"""Read-only views of a profile's session history (release ``SessionDB``).

The claim lineage rule (D6) and the exactly-once marker check (D5) both read
another profile's ``state.db``. They do it here, read-only and through public
``SessionDB`` methods only.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

BOT_CHAT_TITLE = "Bot Chat"
_MAX_HOPS = 100


@contextmanager
def _db(profile: str) -> Iterator[Optional[Any]]:
    from hermes_cli.profiles import get_profile_dir
    from hermes_state import SessionDB

    path = get_profile_dir(profile) / "state.db"
    if not path.exists():
        yield None
        return
    db = SessionDB(db_path=path, read_only=True)
    try:
        yield db
    finally:
        db.close()


def get(profile: str, session_id: str) -> Optional[Dict[str, Any]]:
    with _db(profile) as db:
        return db.get_session(session_id) if db is not None and session_id else None


def tip(profile: str, session_id: str) -> str:
    """The live compression tip (release ``get_compression_tip``), or the id itself."""
    with _db(profile) as db:
        if db is None:
            return session_id
        return db.get_compression_tip(session_id) or session_id


def bot_chat(profile: str) -> Optional[str]:
    with _db(profile) as db:
        row = db.get_session_by_title(BOT_CHAT_TITLE) if db is not None else None
        return row.get("id") if row else None


def _is_continuation_child(row: Dict[str, Any]) -> bool:
    """Exclusions ``get_compression_tip`` applies: no branch, delegate or tool child."""
    try:
        cfg = json.loads(row.get("model_config") or "{}")
    except (TypeError, ValueError):
        cfg = {}
    cfg = cfg if isinstance(cfg, dict) else {}
    return ("_branched_from" not in cfg and "_delegate_from" not in cfg
            and (row.get("source") or "") != "tool")


def thread_of(profile: str, session_id: str) -> str:
    """The session's recorded thread, else its lineage root id; never from a PR number."""
    with _db(profile) as db:
        if db is None:
            return session_id
        current, seen = session_id, set()
        root = session_id
        while current and current not in seen and len(seen) < _MAX_HOPS:
            seen.add(current)
            row = db.get_session(current)
            if row is None:
                break
            if row.get("thread_id"):
                return str(row["thread_id"])
            root = current
            current = row.get("parent_session_id")
        return root


def continues(profile: str, claimant: str, claimed: str) -> bool:
    """True when ``claimant`` IS ``claimed`` or a verified compression continuation of it."""
    if claimant == claimed:
        return True
    with _db(profile) as db:
        if db is None:
            return False
        current, seen = claimant, set()
        while current and current not in seen and len(seen) < _MAX_HOPS:
            seen.add(current)
            row = db.get_session(current)
            if row is None or not _is_continuation_child(row):
                return False
            parent_id = row.get("parent_session_id")
            if not parent_id:
                return False
            parent = db.get_session(parent_id)
            if parent is None or parent.get("end_reason") != "compression":
                return False
            if parent_id == claimed:
                return True
            current = parent_id
        return False


def marker_present(profile: str, session_id: str, marker: str) -> bool:
    """True when ``marker`` already sits on a line of the target's lineage (tip back
    to root, compacted rows included): the target turn has already committed."""
    with _db(profile) as db:
        if db is None or not session_id:
            return False
        current: Optional[str] = db.get_compression_tip(session_id) or session_id
        seen = set()
        while current and current not in seen and len(seen) < _MAX_HOPS:
            seen.add(current)
            for msg in db.get_messages(current, include_compacted=True):
                if marker in str(msg.get("content") or "").splitlines():
                    return True
            row = db.get_session(current) or {}
            current = row.get("parent_session_id")
        return False
