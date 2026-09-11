"""Fleet-shared wiring-edges store for nv-fleet-gates.

The edges table is a fleet-wide fact every profile must read, but
``plugins.plugin_storage.plugin_db`` resolves through ``get_hermes_home()`` and
is therefore PER-PROFILE — it cannot hold a fleet-wide table. So edges live in a
dedicated SQLite DB at the managed-scope-pinned absolute ``settings.edges_db_path``
(outside any profile home); ``hermes wire`` writes it and every profile's
predicate opens it.

Edges are bidirectional: one ``add``/``remove`` writes/clears both directions.
The per-edge ``gated`` flag drives the A2A-F19 approve directive.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import List, Optional, Tuple


def _connect(db_path: str) -> sqlite3.Connection:
    p = Path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS edges ("
        "from_profile TEXT NOT NULL, "
        "to_profile TEXT NOT NULL, "
        "gated INTEGER NOT NULL DEFAULT 0, "
        "PRIMARY KEY (from_profile, to_profile))"
    )
    conn.commit()
    return conn


def add_edge(db_path: str, a: str, b: str, gated: bool = False) -> None:
    g = 1 if gated else 0
    conn = _connect(db_path)
    try:
        # INSERT OR REPLACE keeps `add` idempotent (re-adding is a no-op / gated flip).
        conn.execute(
            "INSERT OR REPLACE INTO edges (from_profile, to_profile, gated) VALUES (?, ?, ?)",
            (a, b, g),
        )
        conn.execute(
            "INSERT OR REPLACE INTO edges (from_profile, to_profile, gated) VALUES (?, ?, ?)",
            (b, a, g),
        )
        conn.commit()
    finally:
        conn.close()


def remove_edge(db_path: str, a: str, b: str) -> None:
    conn = _connect(db_path)
    try:
        conn.execute("DELETE FROM edges WHERE from_profile = ? AND to_profile = ?", (a, b))
        conn.execute("DELETE FROM edges WHERE from_profile = ? AND to_profile = ?", (b, a))
        conn.commit()
    finally:
        conn.close()


def list_edges(db_path: str) -> List[Tuple[str, str, bool]]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT from_profile, to_profile, gated FROM edges ORDER BY from_profile, to_profile"
        ).fetchall()
    finally:
        conn.close()
    return [(r[0], r[1], bool(r[2])) for r in rows]


def lookup(db_path: str, frm: str, to: str) -> Optional[dict]:
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT gated FROM edges WHERE from_profile = ? AND to_profile = ?", (frm, to)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {"from": frm, "to": to, "gated": bool(row[0])}


def wired_teammates(db_path: str, frm: str) -> List[str]:
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT to_profile FROM edges WHERE from_profile = ? ORDER BY to_profile", (frm,)
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]
