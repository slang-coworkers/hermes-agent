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

import json
import logging
import math
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

PLUGIN_KEY = "nv-cost-cap"
DEFAULT_CEILING_USD = 25.0
FLEET_CEILING_KEY = f"plugins.entries.{PLUGIN_KEY}.settings.fleet_ceiling_usd"

# COST-F30: a competing concurrent resolver waits for the first writer's IMMEDIATE
# transaction to commit rather than erroring with "database is locked".
_BUSY_TIMEOUT_MS = 5000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        "day_start_total REAL NOT NULL DEFAULT 0, "
        "recon_unpriced INTEGER NOT NULL DEFAULT 0)"
    )
    _migrate_cap_state(conn)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS episodes ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "session_id TEXT NOT NULL, episode_id TEXT NOT NULL, "
        "kind TEXT NOT NULL, budget_gen INTEGER NOT NULL DEFAULT 0, "
        "day TEXT, dedup_key TEXT NOT NULL UNIQUE)"
    )
    _migrate_episodes(conn)
    # COST-F30: one durable resolution row per escalation episode (episode_id PK) — the
    # exactly-once compare-and-set anchor. amount_usd is the ABSOLUTE effect target stored
    # at claim; status walks pending -> applied | cancelled.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS resolutions ("
        "episode_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
        "budget_gen INTEGER NOT NULL DEFAULT 0, decision TEXT NOT NULL, "
        "actor TEXT, amount_usd REAL, status TEXT NOT NULL DEFAULT 'pending', "
        "reason TEXT, ts TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS fleet_default ("
        "k TEXT PRIMARY KEY, value_usd REAL)"
    )
    conn.commit()


# Keep schema upgrades additive so an existing data.db stays loadable (new
# columns are backfilled with defaults rather than requiring a rebuild).
# ``ended_at`` (COST-F30) is set by ``mark_session_closed``; it is intentionally
# NOT surfaced by ``get_state`` so COST-F29 callers' state shape is unchanged.
_CAP_STATE_ADDED_COLUMNS = (
    ("immortal", "INTEGER NOT NULL DEFAULT 0"),
    ("platform", "TEXT"),
    ("recon_unpriced", "INTEGER NOT NULL DEFAULT 0"),
    ("ended_at", "TEXT"),
)

# COST-F30 additive column on ``episodes``: when the crossing was first observed,
# for the dashboard feed and the reconciler's re-raise lease.
_EPISODES_ADDED_COLUMNS = (
    ("raised_at", "TEXT"),
)


def _add_missing_columns(conn, table, added_columns) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, decl in added_columns:
        if name not in existing:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            except sqlite3.OperationalError:
                # A second store module instance (the ctx-free dashboard import loads its
                # own object) can win this ALTER concurrently ("duplicate column name").
                # Tolerate ONLY when the column is now present; otherwise re-raise.
                current = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if name not in current:
                    raise
    conn.commit()


def _migrate_cap_state(conn) -> None:
    _add_missing_columns(conn, "cap_state", _CAP_STATE_ADDED_COLUMNS)


def _migrate_episodes(conn) -> None:
    _add_missing_columns(conn, "episodes", _EPISODES_ADDED_COLUMNS)


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
    "recon_unpriced": 0,
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
            "last_unpriced_count, budget_gen, blocked, immortal, platform, day_key, "
            "day_start_total, recon_unpriced "
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
        "recon_unpriced": int(row[10]),
    }


_REAL_COLUMNS = {"window_start_total", "last_evaluated_total", "day_start_total"}
_INT_COLUMNS = {"last_unpriced_count", "budget_gen", "blocked", "immortal", "recon_unpriced"}
_TEXT_COLUMNS = {"platform", "day_key"}
_SETTABLE_COLUMNS = _REAL_COLUMNS | _INT_COLUMNS | _TEXT_COLUMNS | {"effective_usd"}


def set_state(session_id: str, **fields) -> None:
    """Update ONLY the supplied columns of the state row (no full-row rewrite).

    A full read-modify-write would race ``bump_effective``: a concurrent caller
    could re-persist a stale ``effective_usd`` snapshot and undo a larger bumped
    value. So this touches only the columns passed, and ``effective_usd`` is
    always applied as ``MAX(effective_usd, ?)`` so it can never be lowered.
    """
    if not fields:
        return
    unknown = set(fields) - _SETTABLE_COLUMNS
    if unknown:
        raise ValueError(f"unknown cap_state columns: {sorted(unknown)}")
    clauses = []
    params = []
    for column, value in fields.items():
        if column == "effective_usd":
            clauses.append("effective_usd = MAX(effective_usd, ?)")
            params.append(_sanitize(value))
        elif column in _REAL_COLUMNS:
            clauses.append(f"{column} = ?")
            params.append(_sanitize(value))
        elif column in _INT_COLUMNS:
            clauses.append(f"{column} = ?")
            params.append(int(value))
        else:  # nullable text: platform, day_key
            clauses.append(f"{column} = ?")
            params.append(value)
    params.append(session_id)
    conn = _conn()
    try:
        conn.execute("INSERT OR IGNORE INTO cap_state (session_id) VALUES (?)", (session_id,))
        conn.execute(
            f"UPDATE cap_state SET {', '.join(clauses)} WHERE session_id = ?", params
        )
        conn.commit()
    finally:
        conn.close()


def all_states() -> List[Dict[str, Any]]:
    """Every tracked per-session state row (``hermes cost-cap show`` + the F30-REV-1 belt scan).

    Carries ``ended_at`` and ``recon_unpriced`` so the guarded ESTOP-clear can exclude closed
    sessions and honour the reconcile-path unknown-pricing marker (see ``_belt_is_clear``).
    """
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT session_id, effective_usd, window_start_total, blocked, immortal, "
            "ended_at, recon_unpriced "
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
            "ended_at": r[5],
            "recon_unpriced": bool(r[6]),
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
            "(session_id, episode_id, kind, budget_gen, day, dedup_key, raised_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, episode_id, kind, int(budget_gen), day, dedup_key, _now()),
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


# --- COST-F30: durable exactly-once resolution CAS -------------------------

def _cas_conn():
    """A ``plugin_db`` connection with MANUAL transaction control + ``busy_timeout``.

    ``plugin_db`` sets WAL + ``check_same_thread=False`` but leaves ``busy_timeout``
    at 0, so a second concurrent writer would fail immediately with "database is
    locked" instead of waiting for the first ``BEGIN IMMEDIATE`` to commit.
    ``isolation_level=None`` hands transaction control to the caller so the explicit
    ``BEGIN IMMEDIATE`` / ``COMMIT`` serialize competing resolvers (AC-1(b)).
    """
    from plugins.plugin_storage import plugin_db

    conn = plugin_db(PLUGIN_KEY)
    conn.isolation_level = None
    conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    _ensure_schema(conn)
    return conn


def episode_by_id(episode_id: str) -> Optional[Dict[str, Any]]:
    """Return ``{session_id, budget_gen, kind, day, raised_at}`` for an episode, or None."""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT session_id, budget_gen, kind, day, raised_at FROM episodes "
            "WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {
        "session_id": row[0], "budget_gen": int(row[1]), "kind": row[2],
        "day": row[3], "raised_at": row[4],
    }


def claim_resolution(
    episode_id: str, session_id: str, budget_gen: int, decision: str,
    actor: Optional[str], amount_usd: Optional[float] = None,
) -> bool:
    """Compare-and-set the sole resolution row for ``episode_id``.

    One ``BEGIN IMMEDIATE`` transaction: reject a closed session, require the exact
    ``(episode_id, session_id, budget_gen)`` episode, and INSERT a ``pending`` row only
    if none exists yet. Returns True iff THIS caller inserted it — so two competing
    clicks (or a sequential repeat) yield exactly one True. ``amount_usd`` is the
    ABSOLUTE effect target computed by the caller at claim time.
    """
    conn = _cas_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT ended_at FROM cap_state WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is not None and row[0]:
            conn.execute("ROLLBACK")
            return False
        ep = conn.execute(
            "SELECT 1 FROM episodes WHERE episode_id = ? AND session_id = ? AND budget_gen = ?",
            (episode_id, session_id, int(budget_gen)),
        ).fetchone()
        if ep is None:
            conn.execute("ROLLBACK")
            return False
        existing = conn.execute(
            "SELECT 1 FROM resolutions WHERE episode_id = ?", (episode_id,)
        ).fetchone()
        if existing is not None:
            conn.execute("ROLLBACK")
            return False
        conn.execute(
            "INSERT INTO resolutions "
            "(episode_id, session_id, budget_gen, decision, actor, amount_usd, status, reason, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', NULL, ?)",
            (episode_id, session_id, int(budget_gen), decision, actor,
             None if amount_usd is None else _sanitize(amount_usd), _now()),
        )
        conn.execute("COMMIT")
        return True
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


# Terminal outcomes of apply_effect — callers map these to a granted/reason result.
APPLIED = "applied"
CANCELLED_CLOSED = "cancelled-session-closed"
CANCELLED_STALE = "cancelled-stale-generation"
DEFERRED = "deferred"
NOOP = "noop"

# Profile-local ESTOP-clear dispositions (F30-REV-1 guarded clear). A CLEARED/LEFT/ABSENT
# clear is a success (the resolution still applies); only DEFERRED means the clear could not
# be completed (unlink OSError or tx failure) and the row stays pending for the reconciler.
CLEARED = "estop-cleared"
LEFT = "estop-left"
ABSENT = "estop-absent"


def _cancel_if_closed_or_stale(conn, episode_id, session_id, claimed_gen):
    """Inside an OPEN ``BEGIN IMMEDIATE`` tx: cancel the resolution (mark the row + COMMIT) and
    return the cancel status when the session closed (``CANCELLED_CLOSED``) or its generation
    moved past the claimed one (``CANCELLED_STALE``); else return None with the tx STILL OPEN.

    The generation guard applies to EVERY decision — a ceiling claimed at gen N must not clear a
    gen N+1 block any more than a stale Continue/Stop may lower a newer baseline.
    """
    state = conn.execute(
        "SELECT ended_at, budget_gen FROM cap_state WHERE session_id = ?", (session_id,)
    ).fetchone()
    if state is not None and state[0]:
        conn.execute(
            "UPDATE resolutions SET status='cancelled', reason='session-closed' WHERE episode_id = ?",
            (episode_id,),
        )
        conn.execute("COMMIT")
        return CANCELLED_CLOSED
    current_gen = state[1] if state is not None else None
    if current_gen is not None and int(current_gen) != int(claimed_gen):
        conn.execute(
            "UPDATE resolutions SET status='cancelled', reason='stale-generation' WHERE episode_id = ?",
            (episode_id,),
        )
        conn.execute("COMMIT")
        return CANCELLED_STALE
    return None


def apply_effect(episode_id: str) -> str:
    """Apply the claimed resolution's effect exactly once; idempotent under retry.

    Returns a terminal status the caller maps to granted/reason. Under one ``BEGIN IMMEDIATE``
    the resolution is cancelled if the session closed (``CANCELLED_CLOSED``) or its
    ``cap_state.budget_gen`` moved past the claimed generation (``CANCELLED_STALE``) — for ANY
    decision, so nothing lowers a newer baseline or clears a newer block. Continue assigns the
    kind's ABSOLUTE baseline and clears ``blocked``; a mortal Stop sets ``blocked=1``; a ceiling
    clears ``blocked`` (resume, no baseline advance) only AFTER its exact-value config write
    succeeds and only while still at the claimed generation. A required side-effect that fails
    (the ESTOP clear a Continue/ceiling needs, or the ceiling config write) leaves the row
    ``pending`` and returns ``DEFERRED`` for the reconciler; ``status='applied'`` is marked LAST.
    """
    conn = _cas_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        res = conn.execute(
            "SELECT session_id, budget_gen, decision, amount_usd, status "
            "FROM resolutions WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        if res is None:
            conn.execute("ROLLBACK")
            conn.close()
            return NOOP
        session_id, claimed_gen, decision, amount_usd, status = res
        if status != "pending":
            conn.execute("ROLLBACK")
            conn.close()
            return NOOP
        cancelled = _cancel_if_closed_or_stale(conn, episode_id, session_id, claimed_gen)
        if cancelled is not None:
            conn.close()
            return cancelled
        if decision == "continue":
            kind_row = conn.execute(
                "SELECT kind FROM episodes WHERE episode_id = ?", (episode_id,)
            ).fetchone()
            column = "day_start_total" if (kind_row and kind_row[0] == "daily") else "window_start_total"
            conn.execute(
                f"UPDATE cap_state SET {column} = ?, blocked = 0 WHERE session_id = ?",
                (_sanitize(amount_usd) if amount_usd is not None else 0.0, session_id),
            )
            conn.execute("COMMIT")
        elif decision == "stop":
            conn.execute("UPDATE cap_state SET blocked = 1 WHERE session_id = ?", (session_id,))
            conn.execute("COMMIT")
        else:
            # ceiling: no cap_state mutation here — the config write + the guarded unblock happen
            # AFTER this tx releases the write lock (a file write must not run inside it).
            conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        conn.close()
        raise
    conn.close()

    if decision == "stop":
        _engage_profile_estop(session_id)  # best-effort belt; committed blocked=1 is authoritative
        return _mark_applied(episode_id)
    if decision == "continue":
        # tx1's blocked=0/baseline is committed above; tx2 does the GUARDED ESTOP clear + the
        # status write atomically (F30-REV-1). A non-owned or belt-held sentinel is LEFT while the
        # resolution still applies; only an unlink OSError / tx2 failure -> DEFERRED (reconciler
        # retries — the cap_state mutation is idempotent).
        return _guarded_clear_and_apply(episode_id)

    # ceiling
    if amount_usd is None:
        return DEFERRED
    try:
        wrote = bool(write_profile_ceiling(amount_usd))
    except Exception:
        logger.warning("nv-cost-cap: ceiling config write failed", exc_info=True)
        wrote = False
    if not wrote:
        return DEFERRED
    cancelled = _finalize_ceiling_unblock(episode_id, session_id, claimed_gen)
    if cancelled is not None:
        return cancelled
    return _guarded_clear_and_apply(episode_id)


def _finalize_ceiling_unblock(episode_id, session_id, claimed_gen):
    """After the ceiling config write: clear ``blocked`` ONLY while the session is still unclosed
    and at the claimed generation, so a ceiling never clears a newer generation's block. Returns a
    cancel status (row marked, committed) or None (block cleared, row left pending for the shared
    ESTOP-clear + applied tail). The profile-ceiling value is already written and is benign policy
    even when this now cancels.
    """
    conn = _cas_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status FROM resolutions WHERE episode_id = ?", (episode_id,)
        ).fetchone()
        if row is None or row[0] != "pending":
            conn.execute("ROLLBACK")
            conn.close()
            return NOOP
        cancelled = _cancel_if_closed_or_stale(conn, episode_id, session_id, claimed_gen)
        if cancelled is not None:
            conn.close()
            return cancelled
        conn.execute("UPDATE cap_state SET blocked = 0 WHERE session_id = ?", (session_id,))
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        conn.close()
        raise
    conn.close()
    return None


def _mark_applied(episode_id: str) -> str:
    """Mark the resolution applied LAST (only from pending), after every side-effect succeeded."""
    conn = _conn()
    try:
        conn.execute(
            "UPDATE resolutions SET status='applied' WHERE episode_id = ? AND status = 'pending'",
            (episode_id,),
        )
        conn.commit()
    finally:
        conn.close()
    return APPLIED


def _owned_local_sentinel():
    """Read the PROFILE-LOCAL ESTOP sentinel directly (F30-REV-1). NEVER ``estop.get_state()``,
    which walks ``_candidate_sentinel_paths`` and can surface the FLEET-ROOT reason, mis-attributing
    ownership. Returns True (present + nv-cost-cap-owned: ``reason`` starts ``"nv-cost-cap:"``),
    False (present but FOREIGN — a ``hermes pause`` with a different/absent reason, or a body-less
    sentinel from ``engage()``'s ``touch()`` fail-safe), or None (ABSENT — no local sentinel file).
    """
    try:
        from agent import estop

        path = estop.sentinel_path()
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False  # body-less / unreadable ⇒ not provably ours ⇒ FOREIGN, leave it
        reason = raw.get("reason") if isinstance(raw, dict) else None
        return isinstance(reason, str) and reason.startswith("nv-cost-cap:")
    except Exception:
        logger.warning("nv-cost-cap: reading profile ESTOP ownership failed", exc_info=True)
        return False  # fail toward LEFT — never unlink a sentinel we could not verify as ours


def _belt_is_clear(conn) -> bool:
    """True when NO OPEN non-immortal session in this profile still holds the belt (F30-REV-1) —
    none is blocked, unpriced, or over-ceiling, INCLUDING the just-resolved session whose unblock/
    baseline tx1 already committed. Read on the CALLER's ``BEGIN IMMEDIATE`` connection so a
    concurrent breach writer serialises against the scan+unlink. Belt over-ceiling mirrors the
    runtime evaluator (``effective − window_start_total >= resolved_ceiling``); unpriced mirrors it
    (calls-table ``priced=0`` OR the reconcile-path ``recon_unpriced`` marker).
    """
    ceiling = resolved_ceiling()
    finite = math.isfinite(ceiling)
    rows = conn.execute(
        "SELECT session_id, effective_usd, window_start_total, blocked, recon_unpriced "
        "FROM cap_state WHERE ended_at IS NULL AND immortal = 0"
    ).fetchall()
    for sid, effective, window_start, blocked, recon_unpriced in rows:
        if blocked:
            return False
        if recon_unpriced:
            return False
        if conn.execute(
            "SELECT 1 FROM calls WHERE session_id = ? AND priced = 0 LIMIT 1", (sid,)
        ).fetchone() is not None:
            return False
        if finite and (float(effective) - float(window_start)) >= ceiling:
            return False
    return True


def _unlink_local_sentinel() -> bool:
    """Unlink ONLY the profile-local sentinel (never ``disengage()``, which lifts the fleet-root
    too). Returns True on success/absent, False on ``OSError``."""
    try:
        from agent import estop

        estop.sentinel_path().unlink(missing_ok=True)
        return True
    except OSError:
        logger.warning("nv-cost-cap: unlinking profile ESTOP failed", exc_info=True)
        return False


def _guarded_clear_and_apply(episode_id: str) -> str:
    """Tx2 of the F30-REV-1 contract: under ONE ``BEGIN IMMEDIATE`` scan the belt, conditionally
    unlink the OWNED profile-local sentinel, then mark the resolution applied — atomically. tx1
    already committed the ``cap_state`` unblock, so this never precedes the durable unblock and a
    concurrent breach serialises on this write lock (it commits ``blocked=1`` before the scan ⇒
    sentinel LEFT, or re-engages after the unlink via ``_engage_profile_estop``'s ``exists()``
    guard). Returns APPLIED (owned+belt-clear ⇒ unlinked, or FOREIGN/belt-held/ABSENT ⇒ left) or
    DEFERRED (unlink ``OSError`` or any tx2 failure; the row stays ``pending`` for the reconciler).
    """
    conn = _cas_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        owned = _owned_local_sentinel()
        if owned is True and _belt_is_clear(conn):
            if not _unlink_local_sentinel():
                conn.execute("ROLLBACK")
                conn.close()
                return DEFERRED
        conn.execute(
            "UPDATE resolutions SET status='applied' WHERE episode_id = ? AND status = 'pending'",
            (episode_id,),
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        conn.close()
        logger.warning("nv-cost-cap: guarded ESTOP-clear tx failed for %s", episode_id, exc_info=True)
        return DEFERRED
    conn.close()
    return APPLIED


def _clear_profile_estop() -> str:
    """Profile-scoped guarded clear for the reconciler (F30-REV-1 step d) — NO episode, NO status
    write, NO session-level ``resumes``. Under one ``BEGIN IMMEDIATE``: unlink the local sentinel
    only when it is OWNED and the belt is clear, so an owned sentinel whose SOLE blocker CLOSED
    without a resolution is eventually lifted. Returns CLEARED | LEFT | ABSENT | DEFERRED.
    """
    owned = _owned_local_sentinel()
    if owned is None:
        return ABSENT
    conn = _cas_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if owned is True and _belt_is_clear(conn):
            if not _unlink_local_sentinel():
                conn.execute("ROLLBACK")
                conn.close()
                return DEFERRED
            conn.execute("COMMIT")
            conn.close()
            return CLEARED
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        conn.close()
        logger.warning("nv-cost-cap: reconcile ESTOP-clear tx failed", exc_info=True)
        return DEFERRED
    conn.close()
    return LEFT


def session_resumes(session_id: str) -> bool:
    """Whether the JUST-RESOLVED session is actually runnable now — DECOUPLED from the profile
    belt/clear decision (F30-REV-1). True iff the session's OWN post-resolution predicates are all
    false (``blocked==0`` AND not unpriced AND not over-ceiling) AND no ESTOP is engaged
    (``estop.is_engaged()`` is fleet-root-inclusive, so a still-present operator/fleet pause keeps
    this False even after we lifted our own profile-local sentinel). Fails toward False.
    """
    state = get_state(session_id)
    if state["blocked"]:
        return False
    if unpriced_count(session_id) > 0 or state["recon_unpriced"]:
        return False
    ceiling = resolved_ceiling()
    baseline = state["day_start_total"] if state["immortal"] else state["window_start_total"]
    if math.isfinite(ceiling) and (state["effective_usd"] - baseline) >= ceiling:
        return False
    try:
        from agent import estop

        if estop.is_engaged():
            return False
    except Exception:
        logger.warning("nv-cost-cap: is_engaged check failed", exc_info=True)
        return False
    return True


def _engage_profile_estop(session_id) -> None:
    """Engage the profile-local ESTOP belt for a Stop (best-effort; committed blocked=1 gates)."""
    try:
        from agent import estop

        if not estop.sentinel_path().exists():
            estop.engage(reason=f"nv-cost-cap: session {session_id} stopped by operator")
    except Exception:
        logger.warning("nv-cost-cap: engaging profile ESTOP failed", exc_info=True)


def write_profile_ceiling(amount) -> bool:
    """Write ``profile_ceiling_usd`` into the CURRENT (pinned) profile's config, CTX-FREE.

    Mirrors ``PluginContext.set_config`` (``hermes_cli/plugins.py:1520``) exactly for
    this one subtree: same cross-process ``_locked_plugin_state`` + ``_CONFIG_LOCK`` and
    the same managed-install / managed-key rejection, but via ``config.save_config`` so
    the dashboard-import path (no ``_CTX``) can write it. Returns False (no write) when
    the setting is administrator-managed.
    """
    from hermes_cli import config as config_mod
    from hermes_cli import managed_scope
    from hermes_cli.plugins import _locked_plugin_state

    if config_mod.is_managed():
        return False
    dotted = f"plugins.entries.{PLUGIN_KEY}.settings.profile_ceiling_usd"
    if managed_scope.is_key_managed(dotted):
        return False
    full_path = ("plugins", "entries", PLUGIN_KEY, "settings", "profile_ceiling_usd")
    partial = {"plugins": {"entries": {PLUGIN_KEY: {"settings": {"profile_ceiling_usd": float(amount)}}}}}
    with _locked_plugin_state(config_mod.get_config_path()):
        with config_mod._CONFIG_LOCK:
            config_mod.read_user_config_raw()
            config_mod.save_config(partial, preserve_keys={full_path}, merge_existing=True)
    return True


def ceiling_setting_is_managed() -> bool:
    """True when ``profile_ceiling_usd`` cannot be written here (managed install / key)."""
    try:
        from hermes_cli import config as config_mod
        from hermes_cli import managed_scope

        if config_mod.is_managed():
            return True
        return managed_scope.is_key_managed(
            f"plugins.entries.{PLUGIN_KEY}.settings.profile_ceiling_usd"
        )
    except Exception:
        return False


def resolutions(session_id: str) -> List[Dict[str, Any]]:
    """Every resolution row for a session (episode_id/decision/actor/amount_usd/status/reason)."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT episode_id, decision, actor, amount_usd, status, reason "
            "FROM resolutions WHERE session_id = ? ORDER BY ts ASC, episode_id ASC",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"episode_id": r[0], "decision": r[1], "actor": r[2],
         "amount_usd": (None if r[3] is None else float(r[3])),
         "status": r[4], "reason": r[5]}
        for r in rows
    ]


def claimed_unapplied() -> List[Dict[str, Any]]:
    """Resolution rows still ``pending`` — claimed but not yet applied (reconciler input)."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT episode_id, session_id, budget_gen, decision, amount_usd "
            "FROM resolutions WHERE status = 'pending' ORDER BY ts ASC",
        ).fetchall()
    finally:
        conn.close()
    return [
        {"episode_id": r[0], "session_id": r[1], "budget_gen": int(r[2]),
         "decision": r[3], "amount_usd": (None if r[4] is None else float(r[4]))}
        for r in rows
    ]


def finalise_closed_unresolved() -> int:
    """Write a reconciler cancel row for each closed-but-unresolved episode; return the count.

    A session whose ``ended_at`` is set but that never got a resolution row is finalised
    here (the sole writer of ``actor='reconciler', status='cancelled', reason='session-closed'``
    — the three fields AC-5 asserts) so a later resolve no-ops. Idempotent: the PK on
    ``resolutions.episode_id`` makes a re-run insert nothing.
    """
    conn = _cas_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT e.episode_id, e.session_id, e.budget_gen FROM episodes e "
            "JOIN cap_state c ON c.session_id = e.session_id "
            "WHERE c.ended_at IS NOT NULL AND c.ended_at != '' "
            "AND NOT EXISTS (SELECT 1 FROM resolutions r WHERE r.episode_id = e.episode_id)"
        ).fetchall()
        n = 0
        for episode_id, session_id, budget_gen in rows:
            cur = conn.execute(
                "INSERT OR IGNORE INTO resolutions "
                "(episode_id, session_id, budget_gen, decision, actor, amount_usd, status, reason, ts) "
                "VALUES (?, ?, ?, 'finalise', 'reconciler', NULL, 'cancelled', 'session-closed', ?)",
                (episode_id, session_id, int(budget_gen), _now()),
            )
            if cur.rowcount == 1:
                n += 1
        conn.execute("COMMIT")
        return n
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        conn.close()


def mark_session_closed(session_id: str) -> None:
    """Set ``ended_at`` on an EXISTING row only — a bare UPDATE, never an INSERT.

    The finalize hook carries no owning profile, so it is called once per profile; this
    must therefore be a genuine no-op in every DB except the one that already holds this
    session's ``cap_state`` row (an INSERT would fabricate a closed row in every profile).
    Sets ``ended_at`` only — the reconciler terminalises the open episode.
    """
    conn = _conn()
    try:
        conn.execute(
            "UPDATE cap_state SET ended_at = ? "
            "WHERE session_id = ? AND (ended_at IS NULL OR ended_at = '')",
            (_now(), session_id),
        )
        conn.commit()
    finally:
        conn.close()


def latest_pending_episode(session_id: str) -> Optional[Dict[str, Any]]:
    """The newest episode for a session with no terminal resolution (the gateway /cost target)."""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT e.episode_id, e.budget_gen, e.kind, e.day FROM episodes e "
            "WHERE e.session_id = ? AND NOT EXISTS ("
            "  SELECT 1 FROM resolutions r WHERE r.episode_id = e.episode_id "
            "  AND r.status IN ('applied', 'cancelled')) "
            "ORDER BY e.id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {"episode_id": row[0], "budget_gen": int(row[1]), "kind": row[2], "day": row[3]}


def latest_episode(session_id: str) -> Optional[Dict[str, Any]]:
    """The newest episode for a session REGARDLESS of resolution status.

    The gateway ``/cost`` fallback: once the pending episode is resolved a repeat ``/cost``
    still targets it, so the CAS reports ``already-resolved`` rather than ``no-episode``.
    """
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT episode_id, budget_gen, kind, day FROM episodes "
            "WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {"episode_id": row[0], "budget_gen": int(row[1]), "kind": row[2], "day": row[3]}


def _finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _dig(mapping, dotted_key):
    cur = mapping
    for part in dotted_key.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def resolved_ceiling() -> float:
    """The Tier-2 ceiling for the CURRENT (pinned) profile, resolved CTX-FREE.

    Precedence: per-profile ``profile_ceiling_usd`` override -> managed-scope fleet ceiling ->
    owner-pinned fleet default -> plugin ``default_ceiling_usd`` (fallback ``DEFAULT_CEILING_USD``).
    Reads only ``load_config_readonly`` / ``managed_scope`` / the owner-pinned fleet default, so it
    holds identically on the hook path and the ctx-free dashboard-import path. This is the ONE
    resolver the enforcement path (``__init__.resolve_ceiling``) and the panel display share, so a
    card can never show a ceiling that disagrees with the one actually enforced.
    """
    try:
        from hermes_cli.config import load_config_readonly

        settings = ((load_config_readonly() or {}).get("plugins", {}).get("entries", {})
                    .get(PLUGIN_KEY, {}).get("settings", {})) or {}
    except Exception:
        logger.warning("nv-cost-cap ceiling settings read failed", exc_info=True)
        settings = {}

    override = settings.get("profile_ceiling_usd")
    if override is not None and _finite(override):
        return float(override)
    try:
        from hermes_cli import managed_scope
        from hermes_cli.config import _expand_env_vars

        if managed_scope.is_key_managed(FLEET_CEILING_KEY):
            managed_cfg = _expand_env_vars(managed_scope.load_managed_config() or {})
            managed_value = _dig(managed_cfg, FLEET_CEILING_KEY)
            if managed_value is not None and _finite(managed_value):
                return float(managed_value)
    except Exception:
        logger.warning("nv-cost-cap managed ceiling read failed", exc_info=True)
    try:
        owner_pinned = get_fleet_default()
        if owner_pinned is not None and _finite(owner_pinned):
            return float(owner_pinned)
    except Exception:
        logger.warning("nv-cost-cap owner-pinned ceiling read failed", exc_info=True)
    default = settings.get("default_ceiling_usd")
    return float(default) if default is not None and _finite(default) else DEFAULT_CEILING_USD


def pending_escalations() -> List[Dict[str, Any]]:
    """The dashboard feed: one dict per unresolved episode in the CURRENT profile."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT e.episode_id, e.session_id, e.kind, e.budget_gen, e.raised_at, "
            "  c.effective_usd, c.window_start_total, c.day_start_total, c.immortal, c.blocked "
            "FROM episodes e LEFT JOIN cap_state c ON c.session_id = e.session_id "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM resolutions r WHERE r.episode_id = e.episode_id "
            "  AND r.status IN ('applied', 'cancelled')) "
            "ORDER BY e.id ASC",
        ).fetchall()
    finally:
        conn.close()
    ceiling = resolved_ceiling()
    out: List[Dict[str, Any]] = []
    for r in rows:
        effective = float(r[5]) if r[5] is not None else 0.0
        window_start = float(r[6]) if r[6] is not None else 0.0
        day_start = float(r[7]) if r[7] is not None else 0.0
        base = day_start if r[2] == "daily" else window_start
        out.append({
            "episode_id": r[0],
            "session": r[1],
            "kind": r[2],
            "budget_gen": int(r[3]),
            "raised_at": r[4],
            "spend": round(max(effective - base, 0.0), 6),
            "ceiling": ceiling,
            "immortal": bool(r[8]) if r[8] is not None else False,
            "blocked": bool(r[9]) if r[9] is not None else False,
        })
    return out
