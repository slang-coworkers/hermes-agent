"""Pure cost-accounting helpers over the READ-ONLY core ``state.db``.

Nothing here writes anything: ``reconcile_session`` and
``completed_session_totals`` open ``state.db`` read-only and never raise —
a missing DB, a missing table, or a locked read degrades to ``0.0`` / ``[]``,
because these feed enforcement decisions that must not crash the agent loop.
"""

from __future__ import annotations

import math
import os
import sqlite3
from typing import List


def _ro_connect(db_path: str):
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _node_spend(conn, session_id: str) -> float:
    """One session's OWN spend: max(sessions.est, SUM main task='') + SUM aux task<>''.

    The ``max`` reconciles the sessions-row aggregate against the summed
    main-loop rows (the sessions row can carry an absolute residual the
    per-model rows do not); aux is added separately because aux usage is written
    to ``session_model_usage`` WITHOUT touching the sessions row, so it is never
    double-counted by the ``max``.
    """
    try:
        row = conn.execute(
            "SELECT COALESCE(estimated_cost_usd, 0) FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        sess_cost = float(row[0]) if row and row[0] is not None else 0.0
    except sqlite3.OperationalError:
        sess_cost = 0.0
    try:
        main = conn.execute(
            "SELECT COALESCE(SUM(estimated_cost_usd), 0) FROM session_model_usage "
            "WHERE session_id = ? AND task = ''",
            (session_id,),
        ).fetchone()[0]
        aux = conn.execute(
            "SELECT COALESCE(SUM(estimated_cost_usd), 0) FROM session_model_usage "
            "WHERE session_id = ? AND task <> ''",
            (session_id,),
        ).fetchone()[0]
    except sqlite3.OperationalError:
        main, aux = 0.0, 0.0
    return max(sess_cost, float(main or 0.0)) + float(aux or 0.0)


def _lineage(conn, session_id: str) -> set:
    """The target session plus every descendant, via ``parent_session_id`` links."""
    seen: set = set()
    frontier = [session_id]
    while frontier:
        node = frontier.pop()
        if node in seen:
            continue
        seen.add(node)
        try:
            children = conn.execute(
                "SELECT id FROM sessions WHERE parent_session_id = ?", (node,)
            ).fetchall()
        except sqlite3.OperationalError:
            children = []
        for (child_id,) in children:
            if child_id is not None and child_id not in seen:
                frontier.append(child_id)
    return seen


def reconcile_session(session_id: str, *, state_db_path: str) -> float:
    """Authoritative reconciled spend for a session: the recursive lineage sum.

    Subagent cost is rolled into the parent only in-memory, never persisted to
    the parent's rows; delegated children get their own ``sessions`` /
    ``session_model_usage`` rows keyed by the child id with a
    ``parent_session_id`` link. So only the lineage sum (each node's own spend,
    counted once) is subagent-, aux- and codex-inclusive.
    """
    if not session_id or not state_db_path or not os.path.exists(state_db_path):
        return 0.0
    try:
        conn = _ro_connect(state_db_path)
    except sqlite3.OperationalError:
        return 0.0
    try:
        return sum(_node_spend(conn, node) for node in _lineage(conn, session_id))
    finally:
        conn.close()


def _lineage_has_unknown(conn, nodes) -> bool:
    """Whether any lineage node carries a ``cost_status='unknown'`` row.

    Core stamps the status on EITHER core column (``sessions.cost_status`` or
    ``session_model_usage.cost_status``), so both are scanned. A schema without
    the column raises ``OperationalError`` — degraded to "no unknown here", the
    same fail-on-error style as ``_node_spend`` — so enforcement fail-CLOSED is
    driven only by a readable unknown row and the reconcile stays crash-safe.
    """
    ids = [n for n in nodes if n is not None]
    if not ids:
        return False
    placeholders = ",".join("?" * len(ids))
    for table, id_col in (("sessions", "id"), ("session_model_usage", "session_id")):
        try:
            row = conn.execute(
                f"SELECT 1 FROM {table} WHERE {id_col} IN ({placeholders}) "
                "AND cost_status = 'unknown' LIMIT 1",
                ids,
            ).fetchone()
        except sqlite3.OperationalError:
            continue
        if row is not None:
            return True
    return False


def lineage_unknown(session_id: str, *, state_db_path: str) -> bool:
    """Whether the session's lineage contains an unknown-priced row.

    Core persists an unknown-priced call as ``estimated_cost_usd=0.0`` with
    ``cost_status='unknown'``, so the numeric reconcile sum alone is fail-OPEN on
    the authoritative path; the caller turns this signal into a fail-CLOSED
    per-session marker. Read-only, never raises.
    """
    if not session_id or not state_db_path or not os.path.exists(state_db_path):
        return False
    try:
        conn = _ro_connect(state_db_path)
    except sqlite3.OperationalError:
        return False
    try:
        return _lineage_has_unknown(conn, _lineage(conn, session_id))
    finally:
        conn.close()


def completed_session_totals(profile_home: str, window: int) -> List[float]:
    """Per-session OWN totals of the most-recently-COMPLETED sessions.

    Completed = ``ended_at IS NOT NULL``; the population is the ``window`` most
    recent by ``ended_at`` (an in-flight/active session and out-of-window
    older sessions are excluded). Each session is sampled by its OWN total
    (``_node_spend``), NOT the lineage sum — so a delegated child is one
    independent sample and a parent is not double-weighted by its descendants.
    """
    db_path = os.path.join(str(profile_home), "state.db")
    if not os.path.exists(db_path):
        return []
    try:
        conn = _ro_connect(db_path)
    except sqlite3.OperationalError:
        return []
    try:
        try:
            rows = conn.execute(
                "SELECT id FROM sessions WHERE ended_at IS NOT NULL "
                "ORDER BY ended_at DESC, id DESC LIMIT ?",
                (int(window),),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [_node_spend(conn, sid) for (sid,) in rows if sid is not None]
    finally:
        conn.close()


def p90(values: List[float]) -> float:
    """Nearest-rank p90 over ``values``. Empty population -> ``inf`` (no Tier-1)."""
    vals = sorted(float(v) for v in values)
    n = len(vals)
    if n == 0:
        return math.inf
    rank = math.ceil(0.9 * n)
    rank = max(1, min(rank, n))
    return vals[rank - 1]
