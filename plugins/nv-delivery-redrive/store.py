"""Durable ticket state: one WAL sqlite db per install root (``<root>/plugin-data/nv-delivery-redrive/data.db``).

The drainer must see every sender's tickets, and send order is per sender -> target pair across profiles, so the
db is pinned to the install root (the default profile's own plugin-data dir) rather than to each caller's home.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)

PLUGIN_NAME = "nv-delivery-redrive"
OPEN_STATES = ("pending",)
TERMINAL_STATES = ("settled", "failed", "cancelled", "ambiguous")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    delivery_id TEXT PRIMARY KEY,
    seq INTEGER UNIQUE,
    kind TEXT NOT NULL DEFAULT 'message',
    sender TEXT NOT NULL,
    target TEXT NOT NULL,
    target_home TEXT NOT NULL,
    sender_home TEXT NOT NULL,
    sender_session_id TEXT,
    author_json TEXT,
    content TEXT,
    content_verified INTEGER NOT NULL DEFAULT 0,
    dm_path TEXT,
    path TEXT NOT NULL DEFAULT 'cli',
    state TEXT NOT NULL DEFAULT 'pending',
    reason TEXT,
    error TEXT,
    reply TEXT,
    process_id TEXT,
    created_at REAL NOT NULL,
    deadline REAL NOT NULL,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    late_open INTEGER NOT NULL DEFAULT 0,
    matched_row_id INTEGER,
    db_seen INTEGER NOT NULL DEFAULT 0,
    resolved_at REAL,
    notice_state TEXT,
    late_outcome TEXT,
    settled_by INTEGER,
    undecidable INTEGER NOT NULL DEFAULT 0,
    published_status TEXT,
    unverified INTEGER NOT NULL DEFAULT 0,
    receipt_row INTEGER,
    UNIQUE (target_home, matched_row_id)
);
CREATE TABLE IF NOT EXISTS attempts (
    delivery_id TEXT NOT NULL,
    n INTEGER NOT NULL,
    state TEXT NOT NULL,
    dm_path TEXT,
    pid INTEGER,
    pid_create_time REAL,
    baseline INTEGER,
    over_row INTEGER,
    out_path TEXT,
    outcome TEXT,
    spawned_at REAL,
    over_at REAL,
    PRIMARY KEY (delivery_id, n)
);
CREATE TABLE IF NOT EXISTS notices (
    delivery_id TEXT NOT NULL,
    recipient TEXT NOT NULL,
    notice_kind TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    text TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    PRIMARY KEY (delivery_id, recipient, notice_kind)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS inflight (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_session_id TEXT,
    task_id TEXT,
    target TEXT NOT NULL,
    target_home TEXT NOT NULL DEFAULT '',
    baseline INTEGER,
    created_at REAL NOT NULL,
    opener_pid INTEGER,
    opener_boot REAL,
    process_id TEXT,
    dm_path TEXT,
    state TEXT NOT NULL DEFAULT 'open',
    delivery_id TEXT
);
CREATE TABLE IF NOT EXISTS published (dm_path TEXT PRIMARY KEY, status TEXT NOT NULL, at REAL NOT NULL);
"""

_lock = threading.RLock()
_conns: dict[str, sqlite3.Connection] = {}


def root() -> Path:
    from hermes_constants import get_hermes_home
    from tools.bot_mode_probe import _hermes_root

    return _hermes_root(get_hermes_home())


def _connect() -> sqlite3.Connection:
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from plugins.plugin_storage import plugin_db

    base = root()
    key = str(base)
    conn = _conns.get(key)
    if conn is not None:
        return conn
    token = set_hermes_home_override(base)
    try:
        conn = plugin_db(PLUGIN_NAME)
    finally:
        reset_hermes_home_override(token)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    conn.executescript(_SCHEMA)
    _conns[key] = conn
    return conn


def close_all() -> None:
    with _lock:
        for conn in _conns.values():
            with contextlib.suppress(sqlite3.Error):
                conn.close()
        _conns.clear()


@contextlib.contextmanager
def txn() -> Iterator[sqlite3.Connection]:
    """``BEGIN IMMEDIATE`` on the root db: one writer at a time across threads and processes."""
    with _lock:
        conn = _connect()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")


def _read(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with _lock:
        return [dict(r) for r in _connect().execute(sql, params).fetchall()]


# --- meta / heartbeat -------------------------------------------------------------------------------------------
def set_heartbeat(ts: Optional[float] = None) -> None:
    with txn() as conn:
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('heartbeat_at', ?)",
                     (str(time.time() if ts is None else ts),))


def heartbeat_at() -> Optional[float]:
    rows = _read("SELECT value FROM meta WHERE key = 'heartbeat_at'")
    try:
        return float(rows[0]["value"]) if rows else None
    except (TypeError, ValueError):
        return None


# --- tickets ----------------------------------------------------------------------------------------------------
def ticket(delivery_id: str) -> Optional[dict[str, Any]]:
    rows = _read("SELECT * FROM tickets WHERE delivery_id = ?", (delivery_id,))
    return rows[0] if rows else None


def attempts(delivery_id: str) -> list[dict[str, Any]]:
    return _read("SELECT * FROM attempts WHERE delivery_id = ? ORDER BY n", (delivery_id,))


def open_tickets() -> list[dict[str, Any]]:
    """Pending tickets, tickets with an attempt still open after their bound, and D5a-unverified tickets (a later
    row claimed for them still settles them until they are collected)."""
    return _read("SELECT * FROM tickets WHERE state = 'pending' OR late_open = 1 OR (state = 'ambiguous' "
                 "AND unverified = 1 AND matched_row_id IS NULL) ORDER BY seq")


def ticket_by_dm_path(dm_path: str) -> Optional[dict[str, Any]]:
    rows = _read("SELECT * FROM tickets WHERE dm_path = ? ORDER BY seq DESC LIMIT 1", (dm_path,))
    return rows[0] if rows else None


def cohort_peers(sender_home: str, target_home: str) -> list[dict[str, Any]]:
    """Unmatched ``ambiguous`` tickets of a pair: still part of their cohort for row attribution."""
    return _read("SELECT * FROM tickets WHERE sender_home = ? AND target_home = ? AND state = 'ambiguous' "
                 "AND matched_row_id IS NULL ORDER BY seq", (sender_home, target_home))


def undecided_notices(epoch: str) -> list[dict[str, Any]]:
    """Terminal tickets whose sender-notice decision is open, or was a skip made under another gateway process."""
    return _read("SELECT * FROM tickets WHERE state != 'pending' AND (notice_state IS NULL OR "
                 "(notice_state LIKE 'skip:%' AND notice_state != ?)) ORDER BY seq", (f"skip:{epoch}",))


def collectable(cutoff: float) -> list[dict[str, Any]]:
    return _read("SELECT * FROM tickets WHERE state != 'pending' AND late_open = 0 AND notice_state IS NOT NULL "
                 "AND resolved_at IS NOT NULL AND resolved_at <= ? AND NOT EXISTS (SELECT 1 FROM notices n "
                 "WHERE n.delivery_id = tickets.delivery_id AND n.state = 'pending')", (cutoff,))


def tickets_for_pair(sender_home: str, target_home: str) -> list[dict[str, Any]]:
    return _read("SELECT * FROM tickets WHERE sender_home = ? AND target_home = ? ORDER BY seq",
                 (sender_home, target_home))


def commit_ticket(fields: dict[str, Any], attempt: Optional[dict[str, Any]],
                  inflight_id: Optional[int] = None) -> None:
    """The D3.6 ticket insert: ticket + attempt 1 in one transaction. A completion the transform already failed
    closed under this DM path (D5a.2) carries its ``published_status`` over; the in-flight row is closed."""
    with txn() as conn:
        seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM tickets").fetchone()[0]
        row = {**fields, "seq": seq}
        if fields.get("dm_path"):
            found = conn.execute("SELECT status FROM published WHERE dm_path = ?", (fields["dm_path"],)).fetchone()
            if found is not None:
                row["published_status"] = found[0]
                conn.execute("DELETE FROM published WHERE dm_path = ?", (fields["dm_path"],))
        cols = ", ".join(row)
        conn.execute(f"INSERT INTO tickets ({cols}) VALUES ({', '.join('?' for _ in row)})", tuple(row.values()))
        if attempt is not None:
            _insert_attempt(conn, {**attempt, "delivery_id": fields["delivery_id"], "n": 1})
        if inflight_id is not None:
            conn.execute("UPDATE inflight SET state = 'committed', delivery_id = ? WHERE id = ?",
                         (fields["delivery_id"], inflight_id))


def set_published(delivery_id: str, status: str) -> None:
    """What a transformed runner result told the sender. ``ambiguous`` is sticky: a later poll of the same runner
    that passes core's text never un-says the rewrite the sender already read."""
    with txn() as conn:
        conn.execute("UPDATE tickets SET published_status = ? WHERE delivery_id = ? "
                     "AND (published_status IS NULL OR ? = 'ambiguous')", (status, delivery_id, status))


def publish_unticketed(dm_path: str, status: str) -> None:
    """A fail-closed rewrite of a completion whose ticket is not committed yet (D5a.2)."""
    with txn() as conn:
        conn.execute("INSERT OR REPLACE INTO published (dm_path, status, at) VALUES (?, ?, ?)",
                     (dm_path, status, time.time()))


def published_for(dm_path: str) -> Optional[str]:
    rows = _read("SELECT status FROM published WHERE dm_path = ?", (dm_path,))
    return rows[0]["status"] if rows else None


def update_ticket(delivery_id: str, conn: Optional[sqlite3.Connection] = None, **fields: Any) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    params = (*fields.values(), delivery_id)
    if conn is not None:
        conn.execute(f"UPDATE tickets SET {sets} WHERE delivery_id = ?", params)
        return
    with txn() as own:
        own.execute(f"UPDATE tickets SET {sets} WHERE delivery_id = ?", params)


def verify_content(delivery_id: str, content: str) -> None:
    with txn() as conn:
        conn.execute("UPDATE tickets SET content = ?, content_verified = 1 WHERE delivery_id = ? "
                     "AND content_verified = 0", (content, delivery_id))


def delete_ticket(conn: sqlite3.Connection, delivery_id: str) -> None:
    for table in ("attempts", "notices", "tickets"):
        conn.execute(f"DELETE FROM {table} WHERE delivery_id = ?", (delivery_id,))


# --- in-flight sends (D5a.1) -------------------------------------------------------------------------------------
def _opener() -> tuple[int, Optional[float]]:
    """This process's pid and start time: a pid alone can be reused by a later process."""
    pid = os.getpid()
    try:
        import psutil

        return pid, float(psutil.Process(pid).create_time())
    except Exception:
        logger.debug("nv-delivery-redrive: no start time for this process", exc_info=True)
        return pid, None


def _same_boot(a: Optional[float], b: Optional[float]) -> bool:
    return a is not None and b is not None and abs(a - b) < 0.01


def open_inflight(*, sender_session_id: str, task_id: str, target: str, baseline: Optional[int],
                  target_home: str = "") -> int:
    pid, boot = _opener()
    with txn() as conn:
        cur = conn.execute("INSERT INTO inflight (sender_session_id, task_id, target, target_home, baseline, "
                           "created_at, opener_pid, opener_boot) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           (sender_session_id, task_id, target, target_home, baseline, time.time(), pid, boot))
        return int(cur.lastrowid)


def inflight(inflight_id: int) -> Optional[dict[str, Any]]:
    rows = _read("SELECT * FROM inflight WHERE id = ?", (inflight_id,))
    return rows[0] if rows else None


def bind_inflight(inflight_id: int, *, process_id: Optional[str], dm_path: Optional[str], untracked: bool) -> None:
    """D5a.1: only the call's own middleware binds its own row, once ``next_call`` has returned or raised."""
    with txn() as conn:
        conn.execute("UPDATE inflight SET process_id = ?, dm_path = ?, state = CASE WHEN ? THEN 'untracked' "
                     "ELSE state END WHERE id = ? AND state = 'open'", (process_id, dm_path, untracked, inflight_id))


def close_inflight(inflight_id: Optional[int], state: str) -> None:
    if inflight_id is None:
        return
    with txn() as conn:
        conn.execute("UPDATE inflight SET state = ? WHERE id = ? AND state = 'open'", (state, inflight_id))


def bound_inflight(dm_path: str) -> Optional[dict[str, Any]]:
    rows = _read("SELECT * FROM inflight WHERE dm_path = ? AND state != 'abandoned' ORDER BY id DESC LIMIT 1",
                 (dm_path,))
    return rows[0] if rows else None


def prebind_rows() -> list[dict[str, Any]]:
    """D5a.2 candidates: open, unbound rows opened by this gateway process. No age filter (E9.3)."""
    pid, boot = _opener()
    rows = _read("SELECT * FROM inflight WHERE state = 'open' AND process_id IS NULL AND dm_path IS NULL "
                 "AND opener_pid = ?", (pid,))
    return [r for r in rows if _same_boot(r["opener_boot"], boot)]


def reap_inflight(now: float, keep_seconds: float) -> None:
    """Open rows whose opening process is gone are ``abandoned``; closed rows are dropped once nothing waits on them."""
    pid, boot = _opener()
    dead = []
    for row in _read("SELECT id, opener_pid, opener_boot FROM inflight WHERE state = 'open'"):
        if row["opener_pid"] == pid and _same_boot(row["opener_boot"], boot):
            continue
        if not _alive(row["opener_pid"], row["opener_boot"]):
            dead.append(row["id"])
    with txn() as conn:
        conn.executemany("UPDATE inflight SET state = 'abandoned' WHERE id = ? AND state = 'open'",
                         [(i,) for i in dead])
        conn.execute("DELETE FROM inflight WHERE state != 'open' AND created_at <= ?", (now - keep_seconds,))
        # A pre-bind fail-closed publication is copied onto its ticket at commit; while any send is still open, the
        # commit that needs it may still come.
        if not conn.execute("SELECT 1 FROM inflight WHERE state = 'open' LIMIT 1").fetchone():
            conn.execute("DELETE FROM published WHERE at <= ?", (now - keep_seconds,))


def _alive(pid: Optional[int], boot: Optional[float]) -> bool:
    import psutil

    if not pid:
        return False
    try:
        return boot is None or _same_boot(float(psutil.Process(pid).create_time()), boot)
    except psutil.NoSuchProcess:
        return False
    except psutil.Error:
        return True                                     # cannot tell: keep the row open (fail closed)


# --- attempts ---------------------------------------------------------------------------------------------------
def _insert_attempt(conn: sqlite3.Connection, attempt: dict[str, Any]) -> None:
    cols = ", ".join(attempt)
    conn.execute(f"INSERT INTO attempts ({cols}) VALUES ({', '.join('?' for _ in attempt)})", tuple(attempt.values()))


def commit_launching(delivery_id: str, n: int, *, dm_path: str, baseline: int, out_path: str) -> None:
    """Committed BEFORE the out file is opened and the runner spawned (D2 crash safety)."""
    with txn() as conn:
        _insert_attempt(conn, {"delivery_id": delivery_id, "n": n, "state": "launching", "dm_path": dm_path,
                               "baseline": baseline, "out_path": out_path, "spawned_at": time.time()})


def commit_running(delivery_id: str, n: int, *, pid: int, pid_create_time: Optional[float],
                   next_attempt_at: float) -> None:
    with txn() as conn:
        conn.execute("UPDATE attempts SET state = 'running', pid = ?, pid_create_time = ? "
                     "WHERE delivery_id = ? AND n = ? AND state = 'launching'", (pid, pid_create_time, delivery_id, n))
        conn.execute("UPDATE tickets SET next_attempt_at = ? WHERE delivery_id = ?", (next_attempt_at, delivery_id))


def update_attempt(delivery_id: str, n: int, conn: Optional[sqlite3.Connection] = None, **fields: Any) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields)
    params = (*fields.values(), delivery_id, n)
    if conn is not None:
        conn.execute(f"UPDATE attempts SET {sets} WHERE delivery_id = ? AND n = ?", params)
        return
    with txn() as own:
        own.execute(f"UPDATE attempts SET {sets} WHERE delivery_id = ? AND n = ?", params)


def record_attempt_outcome(delivery_id: str, n: int, outcome: str) -> None:
    """The attempt-1 outcome the completion transform saw (core's runner stdout). First write wins."""
    with txn() as conn:
        conn.execute("UPDATE attempts SET outcome = ? WHERE delivery_id = ? AND n = ? AND outcome IS NULL",
                     (outcome, delivery_id, n))


def attempt_by_dm_path(dm_path: str) -> Optional[dict[str, Any]]:
    rows = _read("SELECT a.* FROM attempts a JOIN tickets t ON t.delivery_id = a.delivery_id "
                 "WHERE a.n = 1 AND t.dm_path = ?", (dm_path,))
    return rows[0] if rows else None


# --- notices ----------------------------------------------------------------------------------------------------
def queue_notice(conn: sqlite3.Connection, delivery_id: str, recipient: str, notice_kind: str, text: str,
                 metadata: dict[str, Any]) -> None:
    conn.execute("INSERT OR IGNORE INTO notices (delivery_id, recipient, notice_kind, state, text, metadata_json) "
                 "VALUES (?, ?, ?, 'pending', ?, ?)",
                 (delivery_id, recipient, notice_kind, text, json.dumps(metadata, sort_keys=True)))


def pending_notices() -> list[dict[str, Any]]:
    return _read("SELECT * FROM notices WHERE state = 'pending' ORDER BY rowid")


def notices(delivery_id: str) -> list[dict[str, Any]]:
    return _read("SELECT * FROM notices WHERE delivery_id = ? ORDER BY rowid", (delivery_id,))


def mark_notice_appended(delivery_id: str, recipient: str, notice_kind: str) -> None:
    with txn() as conn:
        conn.execute("UPDATE notices SET state = 'appended' WHERE delivery_id = ? AND recipient = ? "
                     "AND notice_kind = ?", (delivery_id, recipient, notice_kind))
