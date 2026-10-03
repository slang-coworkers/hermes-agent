"""Served-profile canonical Bot Chat resolver, shared by the CLI verb and the dashboard API.

``__init__.py`` imports this as a package submodule; ``dashboard/plugin_api.py``
loads it by file path (the dashboard importer gives that module no package), so
module-level imports stay stdlib-only and core is imported inside the functions.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

TRANSCRIPT_LIMIT = 500


def served_profiles() -> List[Tuple[str, Path]]:
    """``(name, hermes_home)`` for exactly the profiles the gateway serves."""
    from gateway.config import load_gateway_config
    from hermes_cli.profiles import profiles_to_serve

    cfg = load_gateway_config()
    return [
        (name, Path(home))
        for name, home in profiles_to_serve(
            multiplex=bool(cfg.multiplex_profiles),
            profile_allowlist=cfg.multiplex_profile_allowlist,
        )
    ]


def _canonical_entry(name: str, db_path: Path) -> Optional[Dict[str, Any]]:
    from hermes_state import SessionDB

    db = SessionDB(db_path=db_path, read_only=True)
    try:
        row = db.get_session_by_title(SessionDB.CANONICAL_BOT_CHAT_TITLE)
        # Hidden is what makes the title canonical: a visible session a user
        # named "Bot Chat" is ordinary. Archived rows are skipped because
        # recovering one is a write and this viewer is read-only.
        if not row or not row.get("hidden") or row.get("archived"):
            return None
        tip = db.resolve_resume_session_id(row["id"]) or row["id"]
        tip_row = (db.get_session(tip) or row) if tip != row["id"] else row
        return {
            "profile": name,
            "session_id": row["id"],
            "resolved_id": tip,
            "title": row.get("title") or "",
            "hidden": True,
            "message_count": tip_row.get("message_count") or 0,
            "started_at": row.get("started_at") or 0,
        }
    finally:
        db.close()


def _canonical_entries(profile: Optional[str] = None) -> List[Tuple[Dict[str, Any], Path]]:
    entries: List[Tuple[Dict[str, Any], Path]] = []
    for name, home in served_profiles():
        if profile is not None and name != profile:
            continue
        db_path = home / "state.db"
        if not db_path.is_file():
            continue
        try:
            entry = _canonical_entry(name, db_path)
        except (sqlite3.Error, OSError):
            logger.warning("nv-bot-chat: cannot read session store %s", db_path, exc_info=True)
            continue
        if entry is not None:
            entries.append((entry, db_path))
    return entries


def list_canonical_bot_chats() -> List[Dict[str, Any]]:
    """Every served profile's hidden canonical Bot Chat, in served order."""
    return [entry for entry, _ in _canonical_entries()]


def find_canonical(profile: str, session_id: str) -> Optional[Tuple[Dict[str, Any], Path]]:
    """The served canonical Bot Chat named by ``(profile, session_id)``, or None.

    The session store path comes from the served set, never from the caller: an
    unserved profile opens no store, and only the served profile's canonical row
    is read to check the id, so an unmatched id never reaches the transcript read.
    """
    for entry, db_path in _canonical_entries(profile):
        if session_id in (entry["session_id"], entry["resolved_id"]):
            return entry, db_path
    return None


def read_transcript(entry: Dict[str, Any], db_path: Path) -> List[Dict[str, Any]]:
    """Latest page of the Bot Chat's live tip, compaction-preserved rows included."""
    from agent.compaction_display import project_compaction_message_for_display
    from agent.context_compressor import is_compaction_summary_message
    from hermes_state import SessionDB

    db = SessionDB(db_path=db_path, read_only=True)
    try:
        messages = db.get_messages(
            entry["resolved_id"],
            include_compacted=True,
            limit=TRANSCRIPT_LIMIT,
            latest=True,
        )
    finally:
        db.close()

    # Same display projection the stock /api/sessions/{id}/messages route applies.
    projected: List[Dict[str, Any]] = []
    for message in messages:
        if not is_compaction_summary_message(message):
            projected.append(message)
            continue
        display_view = project_compaction_message_for_display(message)
        shown = message.copy()
        if display_view is None:
            if not shown.get("display_kind"):
                shown["display_kind"] = "hidden"
        else:
            shown["display_content"] = display_view.get("content")
            shown.pop("display_kind", None)
        projected.append(shown)
    return projected
