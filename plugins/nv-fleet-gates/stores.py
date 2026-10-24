"""Per-profile session-state store for nv-fleet-gates.

``plugin_db("nv-fleet-gates")`` resolves through ``get_hermes_home()`` to
``<HERMES_HOME>/plugin-data/nv-fleet-gates/data.db`` — per-profile, which is
correct because a session runs in exactly one profile. Each call opens a fresh
WAL connection, so every write is committed and the connection closed before
return (a later read on a new connection then sees it).

Freshness (LOOP-F37-1): critique and file-mutation events share ONE
autoincrement id space, so "the critique is fresh for a required stage" == the
latest critique event for that stage has a higher id than the latest mutation
event for the session. This ports NanoClaw's finish-writes-then-critique
invariant: a mutation after a critique restales it.
"""

from __future__ import annotations

from typing import Iterable

from plugins.plugin_storage import plugin_db

_PLUGIN = "nv-fleet-gates"


def _conn():
    conn = plugin_db(_PLUGIN)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "session_id TEXT NOT NULL, "
        "kind TEXT NOT NULL, "
        "stage TEXT)"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS plans (session_id TEXT PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS invitations ("
        "repo TEXT NOT NULL, pr INTEGER NOT NULL, PRIMARY KEY (repo, pr))"
    )
    conn.commit()
    return conn


def record_critique(session_id: str, stage: str) -> None:
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO events (session_id, kind, stage) VALUES (?, 'critique', ?)",
            (session_id, stage),
        )
        conn.commit()
    finally:
        conn.close()


def record_mutation(session_id: str) -> None:
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO events (session_id, kind, stage) VALUES (?, 'mutation', NULL)",
            (session_id,),
        )
        conn.commit()
    finally:
        conn.close()


def critique_fresh(session_id: str, required_stages: Iterable[str]) -> bool:
    stages = [s for s in (required_stages or [])]
    if not stages:
        return True
    conn = _conn()
    try:
        mut = conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM events WHERE session_id = ? AND kind = 'mutation'",
            (session_id,),
        ).fetchone()[0]
        for stage in stages:
            crit = conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM events "
                "WHERE session_id = ? AND kind = 'critique' AND stage = ?",
                (session_id, stage),
            ).fetchone()[0]
            if crit == 0 or crit < mut:
                return False
        return True
    finally:
        conn.close()


def record_plan(session_id: str) -> None:
    conn = _conn()
    try:
        conn.execute("INSERT OR IGNORE INTO plans (session_id) VALUES (?)", (session_id,))
        conn.commit()
    finally:
        conn.close()


def has_plan(session_id: str) -> bool:
    conn = _conn()
    try:
        row = conn.execute("SELECT 1 FROM plans WHERE session_id = ?", (session_id,)).fetchone()
    finally:
        conn.close()
    return row is not None


def record_invitation(repo: str, pr) -> None:
    conn = _conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO invitations (repo, pr) VALUES (?, ?)", (repo, int(pr))
        )
        conn.commit()
    finally:
        conn.close()


def has_invitation(repo, pr) -> bool:
    if repo is None or pr is None:
        return False
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM invitations WHERE repo = ? AND pr = ?", (repo, int(pr))
        ).fetchone()
    finally:
        conn.close()
    return row is not None
