"""Durable state for nv-cost-cap.

``plugin_db("nv-cost-cap")`` resolves through ``get_hermes_home()`` to
``<HERMES_HOME>/plugin-data/nv-cost-cap/data.db`` — per-profile, which is
correct because a session runs in exactly one profile. Each helper opens a
fresh WAL connection, commits, and closes before returning.

Three per-profile tables:
  * ``calls`` — the fast-path per-``api_request_id`` ledger (INSERT OR IGNORE),
    so re-delivering a response with the same id never double-counts.
  * ``cap_state`` — the per-session running state (effective spend, the
    per-fire and per-day window baselines, the fire generation, blocked flag).
  * ``episodes`` — escalation / breach / daily / unknown_pricing episodes,
    deduplicated by a UNIQUE ``dedup_key``.

The one fleet-uniform value — the owner-pinned fleet-default ceiling — lives in
``fleet_default`` in the DEFAULT profile's plugin_db, opened under a
context-local ``set_hermes_home_override(get_profile_dir("default"))`` (never
``os.environ``) so every profile resolves the identical value.
"""

from __future__ import annotations

import math
import uuid
from typing import Any, Dict, List, Optional

PLUGIN_KEY = "nv-cost-cap"


def _ensure_schema(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS calls ("
        "session_id TEXT NOT NULL, api_request_id TEXT NOT NULL, "
        "amount_usd REAL NOT NULL DEFAULT 0, priced INTEGER NOT NULL DEFAULT 1, "
        "PRIMARY KEY (session_id, api_request_id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS cap_state ("
        "session_id TEXT PRIMARY KEY, "
        "effective_usd REAL NOT NULL DEFAULT 0, "
        "window_start_total REAL NOT NULL DEFAULT 0, "
        "last_evaluated_total REAL NOT NULL DEFAULT 0, "
        "last_unpriced_count INTEGER NOT NULL DEFAULT 0, "
        "budget_gen INTEGER NOT NULL DEFAULT 0, "
        "blocked INTEGER NOT NULL DEFAULT 0, "
        "immortal INTEGER NOT NULL DEFAULT 0, "
        "platform TEXT, "
        "day_key TEXT, "
        "day_start_total REAL NOT NULL DEFAULT 0)"
    )
    _migrate_cap_state(conn)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS episodes ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "session_id TEXT NOT NULL, episode_id TEXT NOT NULL, "
        "kind TEXT NOT NULL, budget_gen INTEGER NOT NULL DEFAULT 0, "
        "day TEXT, dedup_key TEXT NOT NULL UNIQUE)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS fleet_default ("
        "k TEXT PRIMARY KEY, value_usd REAL)"
    )
    conn.commit()


# Columns added after the initial cap_state shape; an additive migration keeps a
# data.db written by an earlier plugin version loadable (new columns get defaults).
_CAP_STATE_ADDED_COLUMNS = (
    ("immortal", "INTEGER NOT NULL DEFAULT 0"),
    ("platform", "TEXT"),
)


def _migrate_cap_state(conn) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(cap_state)").fetchall()}
    for name, decl in _CAP_STATE_ADDED_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE cap_state ADD COLUMN {name} {decl}")
    conn.commit()


def _conn():
    from plugins.plugin_storage import plugin_db

    conn = plugin_db(PLUGIN_KEY)
    _ensure_schema(conn)
    return conn


def _sanitize(value) -> float:
    # A NaN cost would silently disable Tier-2 (NaN >= ceiling is always False);
    # treat any non-finite accrual as zero so it is never counted as "spent".
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(f) or math.isinf(f):
        return 0.0
    return f


# --- fast-path accrual -----------------------------------------------------

def record_call(session_id: str, api_request_id: str, amount_usd: float, priced: bool) -> float:
    """INSERT OR IGNORE one call; return the running priced fast-path total.

    Idempotent by ``(session_id, api_request_id)``: a re-delivered id leaves the
    total unchanged; a new id adds its price. An unpriced call is stored with
    ``priced=0`` and excluded from the total (it is enforced separately, never
    counted as free).
    """
    conn = _conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO calls (session_id, api_request_id, amount_usd, priced) "
            "VALUES (?, ?, ?, ?)",
            (session_id, api_request_id, _sanitize(amount_usd), 1 if priced else 0),
        )
        conn.commit()
        row = conn.execute(
            "SELECT COALESCE(SUM(amount_usd), 0) FROM calls "
            "WHERE session_id = ? AND priced = 1",
            (session_id,),
        ).fetchone()
        return float(row[0] or 0.0)
    finally:
        conn.close()


def unpriced_count(session_id: str) -> int:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM calls WHERE session_id = ? AND priced = 0",
            (session_id,),
        ).fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


# --- per-session state -----------------------------------------------------

_STATE_DEFAULTS: Dict[str, Any] = {
    "effective_usd": 0.0,
    "window_start_total": 0.0,
    "last_evaluated_total": 0.0,
    "last_unpriced_count": 0,
    "budget_gen": 0,
    "blocked": 0,
    "immortal": 0,
    "platform": None,
    "day_key": None,
    "day_start_total": 0.0,
}


def session_exists(session_id: str) -> bool:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM cap_state WHERE session_id = ?", (session_id,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def get_state(session_id: str) -> Dict[str, Any]:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT effective_usd, window_start_total, last_evaluated_total, "
            "last_unpriced_count, budget_gen, blocked, immortal, platform, day_key, day_start_total "
            "FROM cap_state WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return dict(_STATE_DEFAULTS)
    return {
        "effective_usd": float(row[0]),
        "window_start_total": float(row[1]),
        "last_evaluated_total": float(row[2]),
        "last_unpriced_count": int(row[3]),
        "budget_gen": int(row[4]),
        "blocked": int(row[5]),
        "immortal": int(row[6]),
        "platform": row[7],
        "day_key": row[8],
        "day_start_total": float(row[9]),
    }


def set_state(session_id: str, **fields) -> None:
    """Read-modify-write the full state row (upsert), preserving unset columns."""
    if not fields:
        return
    st = get_state(session_id)
    st.update(fields)
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO cap_state (session_id, effective_usd, window_start_total, "
            "last_evaluated_total, last_unpriced_count, budget_gen, blocked, immortal, "
            "platform, day_key, day_start_total) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "effective_usd=excluded.effective_usd, "
            "window_start_total=excluded.window_start_total, "
            "last_evaluated_total=excluded.last_evaluated_total, "
            "last_unpriced_count=excluded.last_unpriced_count, "
            "budget_gen=excluded.budget_gen, blocked=excluded.blocked, "
            "immortal=excluded.immortal, platform=excluded.platform, "
            "day_key=excluded.day_key, day_start_total=excluded.day_start_total",
            (
                session_id,
                _sanitize(st["effective_usd"]),
                _sanitize(st["window_start_total"]),
                _sanitize(st["last_evaluated_total"]),
                int(st["last_unpriced_count"]),
                int(st["budget_gen"]),
                int(st["blocked"]),
                int(st["immortal"]),
                st["platform"],
                st["day_key"],
                _sanitize(st["day_start_total"]),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def all_states() -> List[Dict[str, Any]]:
    """Every tracked per-session state row (for ``hermes cost-cap show``)."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT session_id, effective_usd, window_start_total, blocked, immortal "
            "FROM cap_state ORDER BY session_id ASC"
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "session_id": r[0],
            "effective_usd": float(r[1]),
            "window_start_total": float(r[2]),
            "blocked": bool(r[3]),
            "immortal": bool(r[4]),
        }
        for r in rows
    ]


def bump_effective(session_id: str, value: float) -> float:
    """Persist ``effective = max(previous, value)`` and return it.

    Monotone: a stale async reconcile that reads a lower total than the
    fast path already accrued never lowers spend (which would re-permit a
    paid call). The two sources estimate the SAME calls, so max — not sum —
    is correct.
    """
    value = _sanitize(value)
    conn = _conn()
    try:
        # One atomic statement so two interleaving callbacks cannot overwrite a
        # larger effective with a smaller stale value.
        conn.execute(
            "INSERT INTO cap_state (session_id, effective_usd) VALUES (?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "effective_usd = MAX(cap_state.effective_usd, excluded.effective_usd)",
            (session_id, value),
        )
        conn.commit()
        row = conn.execute(
            "SELECT effective_usd FROM cap_state WHERE session_id = ?", (session_id,)
        ).fetchone()
        return float(row[0]) if row else value
    finally:
        conn.close()


# --- episodes --------------------------------------------------------------

def record_episode(
    session_id: str, kind: str, budget_gen: int, day: Optional[str], dedup_key: str
) -> Optional[str]:
    """INSERT OR IGNORE one episode; return its episode_id if newly inserted, else None."""
    episode_id = uuid.uuid4().hex
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO episodes "
            "(session_id, episode_id, kind, budget_gen, day, dedup_key) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, episode_id, kind, int(budget_gen), day, dedup_key),
        )
        conn.commit()
        return episode_id if cur.rowcount == 1 else None
    finally:
        conn.close()


def episodes(session_id: str) -> List[Dict[str, Any]]:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT episode_id, budget_gen, day, kind FROM episodes "
            "WHERE session_id = ? ORDER BY id ASC",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"episodeId": r[0], "budgetGen": int(r[1]), "day": r[2], "kind": r[3]}
        for r in rows
    ]


# --- owner-pinned fleet-default ceiling ------------------------------------

def _fleet_conn():
    """Open the plugin_db pinned to the DEFAULT profile home (the fleet root).

    ``plugin_db`` resolves ``get_hermes_home()`` at call time, so the connection
    must be opened WHILE the context-local override is active; the returned
    connection is already bound to the default-home file after the override is
    reset.
    """
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_cli.profiles import get_profile_dir

    token = set_hermes_home_override(str(get_profile_dir("default")))
    try:
        return _conn()
    finally:
        reset_hermes_home_override(token)


def set_fleet_default(value: float) -> None:
    conn = _fleet_conn()
    try:
        conn.execute(
            "INSERT INTO fleet_default (k, value_usd) VALUES ('ceiling', ?) "
            "ON CONFLICT(k) DO UPDATE SET value_usd=excluded.value_usd",
            (_sanitize(value),),
        )
        conn.commit()
    finally:
        conn.close()


def get_fleet_default() -> Optional[float]:
    conn = _fleet_conn()
    try:
        row = conn.execute(
            "SELECT value_usd FROM fleet_default WHERE k='ceiling'"
        ).fetchone()
    finally:
        conn.close()
    if row is None or row[0] is None:
        return None
    return float(row[0])
