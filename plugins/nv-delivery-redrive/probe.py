"""Three-state transcript probe and live-writer scan (ADR D5).

An attempt's writers are the processes whose command line carries its DM file path: core's runner, the shell
that wraps it, and the target CLI it starts (``--query-file <dm file>``). The probe reads the target's
``state.db`` read-only and never creates it.
"""

from __future__ import annotations

import contextlib
import itertools
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

COLD = "cold"
UNKNOWN = "unknown"


def _db(home: str | os.PathLike) -> Path:
    return Path(home) / "state.db"


def _ro(path: Path) -> sqlite3.Connection:
    from hermes_state_holders import read_only_db_uri

    return sqlite3.connect(read_only_db_uri(path), uri=True, timeout=5.0)


def max_row(home: str | os.PathLike, *, db_seen: bool) -> tuple[str, Optional[int]]:
    """``("ok", MAX(id))``, ``("cold", 0)`` for a db that does not exist and was never observed for the ticket,
    else ``("unknown", None)``. A db observed once and gone since is unknown, never cold."""
    path = _db(home)
    if not path.exists():
        return (UNKNOWN, None) if db_seen else (COLD, 0)
    try:
        conn = _ro(path)
        try:
            value = conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
        finally:
            conn.close()
        return "ok", int(value)
    except (sqlite3.Error, OSError, TypeError, ValueError):
        logger.debug("nv-delivery-redrive: target db %s unreadable", path, exc_info=True)
        return UNKNOWN, None


def matching_origins(home: str | os.PathLike, content: str, *, db_seen: bool) -> Any:
    """Origin ids (``MIN(id)`` per ``message_uid``) of every Bot Chat lineage user message that contains the whole
    ``content``; ``[]`` for a cold target; ``UNKNOWN`` on any read failure or a matching row without a uid."""
    path = _db(home)
    if not path.exists():
        return UNKNOWN if db_seen else []
    try:
        from hermes_state import SessionDB

        db = SessionDB(db_path=path, read_only=True)
        try:
            row = db.get_session_by_title("Bot Chat")
            if row is None:
                return []
            tip = db.get_compression_tip(row["id"]) or row["id"]
            messages = db.get_messages(tip, include_inactive=True, include_ancestors=True)
        finally:
            db.close()
        uids = []
        for message in messages:
            if message.get("role") != "user" or message.get("display_kind") == "process_complete":
                continue
            if content not in str(message.get("content") or ""):
                continue
            uid = message.get("message_uid")
            if not uid:
                return UNKNOWN
            uids.append(uid)
        if not uids:
            return []
        conn = _ro(path)
        try:
            origins = {conn.execute("SELECT MIN(id) FROM messages WHERE message_uid = ?", (uid,)).fetchone()[0]
                       for uid in uids}
        finally:
            conn.close()
        if None in origins:
            return UNKNOWN
        return sorted(int(o) for o in origins)
    except Exception as exc:
        logger.debug("nv-delivery-redrive: probe of %s failed: %s", path, exc)
        return UNKNOWN


def first_assistant_after(home: str | os.PathLike, row_id: int) -> str:
    path = _db(home)
    with contextlib.suppress(Exception):
        conn = _ro(path)
        try:
            row = conn.execute("SELECT content FROM messages WHERE role = 'assistant' AND id > ? ORDER BY id LIMIT 1",
                               (row_id,)).fetchone()
        finally:
            conn.close()
        return str(row[0] or "") if row else ""
    return ""


def scan_processes() -> Optional[list[tuple[int, list[str]]]]:
    """``(pid, argv)`` of every live, non-zombie process; None when the listing itself fails (fail-safe alive)."""
    try:
        import psutil

        me = os.getpid()
        found = []
        for proc in psutil.process_iter(["pid", "cmdline", "status"]):
            info = proc.info
            if info.get("pid") == me or info.get("status") == psutil.STATUS_ZOMBIE:
                continue
            found.append((int(info["pid"]), list(info.get("cmdline") or ())))
        return found
    except Exception:
        logger.warning("nv-delivery-redrive: process listing failed; treating attempts as alive", exc_info=True)
        return None


def writer_pid(procs: Optional[list[tuple[int, list[str]]]], dm_path: Optional[str]) -> Optional[int]:
    """A pid whose argv carries ``dm_path``; ``-1`` when the listing failed (fail-safe: alive)."""
    if not dm_path:
        return None
    if procs is None:
        return -1
    for pid, argv in procs:
        if any(dm_path in part for part in argv):
            return pid
    return None


def target_turn_pid(procs: Optional[list[tuple[int, list[str]]]], dm_path: Optional[str]) -> Optional[int]:
    """A target CLI turn on ``dm_path``: the consecutive argv pair ``--query-file <dm_path>``. Core's runner carries
    ``--run-delivery query-file <dm_path>`` (no dashes), so it never matches. ``-1`` when the listing failed."""
    if not dm_path:
        return None
    if procs is None:
        return -1
    for pid, argv in procs:
        if any(a == "--query-file" and b == dm_path for a, b in itertools.pairwise(argv)):
            return pid
    return None


def target_lock_held(root: str | os.PathLike, profile: str) -> bool:
    """Whether core's per-profile turn lock is held right now (a non-blocking probe, released at once)."""
    try:
        import fcntl

        from tools.bot_relay import turn_lock_path

        path = turn_lock_path(root, profile)
        if not path.exists():
            return False
        fd = os.open(path, os.O_RDWR)
    except (ImportError, OSError):
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)
