"""The single fleet ledger for nv-ingress.

One ``plugin_db("nv-ingress")`` lives under the DEFAULT home and is opened from
every profile with a context-local home override (the nv-artifact ``_db``
pattern). That way the staging route script, the bridge, the claim hook in a
coworker profile, and the CLI all read and write one file. Only absolute
imports are used, because the staging route script loads this module by path.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, List, Optional

PLUGIN_KEY = "nv-ingress"
_BUSY_RETRIES = 5
_BUSY_SLEEP_S = 0.05

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS pr_owner (
        repo TEXT NOT NULL, pr INTEGER NOT NULL, owner_profile TEXT NOT NULL,
        session_id TEXT NOT NULL, thread_id TEXT NOT NULL, claimed_at REAL NOT NULL,
        superseded_by INTEGER, UNIQUE (repo, pr))""",
    """CREATE TABLE IF NOT EXISTS pr_heads (
        repo TEXT NOT NULL, pr INTEGER NOT NULL, head_sha TEXT, head_branch TEXT,
        open INTEGER NOT NULL DEFAULT 1, updated_at REAL NOT NULL, UNIQUE (repo, pr))""",
    """CREATE TABLE IF NOT EXISTS outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, delivery_id TEXT NOT NULL,
        event TEXT, repo TEXT, kind TEXT, number INTEGER, head_sha TEXT, head_branch TEXT,
        stage TEXT NOT NULL, self_authored INTEGER NOT NULL DEFAULT 0, envelope TEXT NOT NULL,
        created_at REAL NOT NULL, resolved INTEGER NOT NULL DEFAULT 0,
        UNIQUE (source, delivery_id))""",
    """CREATE TABLE IF NOT EXISTS issue_seen (
        source TEXT NOT NULL, repo TEXT NOT NULL, number INTEGER NOT NULL,
        UNIQUE (source, repo, number))""",
    # pr = 0 means the delivery names no PR (an issue, or CI not yet associated).
    # target_kind: 'owner' (resume the owner session), 'bot_chat' (the orchestrator),
    # NULL (not resolved yet: re-resolved on every drain pass).
    """CREATE TABLE IF NOT EXISTS deliveries (
        id INTEGER PRIMARY KEY AUTOINCREMENT, outbox_id INTEGER NOT NULL,
        outbox_delivery TEXT NOT NULL, repo TEXT, pr INTEGER NOT NULL DEFAULT 0,
        target_profile TEXT, target_session TEXT, target_kind TEXT, state TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL DEFAULT 0,
        created_at REAL NOT NULL, UNIQUE (outbox_id, pr))""",
    """CREATE TABLE IF NOT EXISTS claim_refusals (
        repo TEXT NOT NULL, pr INTEGER NOT NULL, holder TEXT, claimant TEXT,
        claimant_session TEXT, reason TEXT, at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS unclaimable (
        repo TEXT NOT NULL, pr INTEGER NOT NULL, claimant TEXT, reason TEXT, at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS runaway (
        repo TEXT NOT NULL, pr INTEGER NOT NULL, hour INTEGER NOT NULL,
        UNIQUE (repo, pr, hour))""",
    """CREATE TABLE IF NOT EXISTS budget (
        repo TEXT NOT NULL, pr INTEGER NOT NULL, hour INTEGER NOT NULL,
        count INTEGER NOT NULL DEFAULT 0, UNIQUE (repo, pr, hour))""",
    """CREATE TABLE IF NOT EXISTS guard_state (
        id INTEGER PRIMARY KEY CHECK (id = 1), breach INTEGER NOT NULL,
        routes TEXT NOT NULL, reason TEXT, at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS drain_lease (
        id INTEGER PRIMARY KEY CHECK (id = 1), holder TEXT NOT NULL, expires_at REAL NOT NULL)""",
)


def _is_locked(exc: Exception) -> bool:
    text = str(exc).lower()
    return isinstance(exc, sqlite3.OperationalError) and ("locked" in text or "busy" in text)


def ledger_home():
    """The DEFAULT home: the one place the fleet ledger lives, from any profile."""
    from hermes_cli.profiles import get_profile_dir

    return get_profile_dir("default")


def exists() -> bool:
    return (ledger_home() / "plugin-data" / PLUGIN_KEY / "data.db").is_file()


def connect() -> sqlite3.Connection:
    """Open the fleet ledger (schema ensured), pinned to the DEFAULT home."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from plugins.plugin_storage import plugin_db

    attempt = 0
    while True:
        conn = None
        try:
            token = set_hermes_home_override(str(ledger_home()))
            try:
                conn = plugin_db(PLUGIN_KEY)
            finally:
                reset_hermes_home_override(token)
            conn.execute("PRAGMA busy_timeout=5000")
            for ddl in _SCHEMA:
                conn.execute(ddl)
            conn.commit()
            return conn
        except sqlite3.OperationalError as exc:
            if conn is not None:
                conn.close()
            attempt += 1
            if not _is_locked(exc) or attempt >= _BUSY_RETRIES:
                raise
            time.sleep(_BUSY_SLEEP_S * attempt)


# --------------------------------------------------------------------------- staging


def _is_self_authored(env: Dict[str, Any], self_logins: List[str]) -> bool:
    return env.get("sender_type") == "Bot" or (env.get("sender") or "") in set(self_logins or [])


def _qualifying_issue(env: Dict[str, Any], labels: List[str]) -> bool:
    wanted = set(labels or [])
    have = set(env.get("labels") or [])
    return env.get("action") in ("opened", "labeled") and bool(wanted & have)


def stage(env: Dict[str, Any], settings: Dict[str, Any]) -> Dict[str, Any]:
    """Durably stage one envelope (D4) and return it with ``stage`` + ``self_authored``.

    One transaction: the outbox row (idempotent on the delivery id), the issue's
    logical key, and the PR head record. ``new`` rows are the drain's work list.
    """
    out = dict(env)
    out["self_authored"] = _is_self_authored(env, settings.get("self_logins") or [])
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        dup = conn.execute("SELECT stage FROM outbox WHERE source = ? AND delivery_id = ?",
                           (env.get("source"), env.get("delivery_id"))).fetchone()
        if dup is not None:
            conn.rollback()
            out["stage"] = "duplicate"
            return out
        kind = env.get("kind")
        if kind == "issue":
            if not _qualifying_issue(env, settings.get("issue_labels") or []):
                stage_name = "unrouted"
            else:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO issue_seen (source, repo, number) VALUES (?, ?, ?)",
                    (env.get("source"), env.get("repo"), env.get("number")))
                stage_name = "new" if cur.rowcount == 1 else "suppressed"
        elif kind in ("pr", "ci", "review", "comment"):
            stage_name = "new"
        else:
            stage_name = "unrouted"
        _record_heads(conn, env)
        conn.execute(
            "INSERT INTO outbox (source, delivery_id, event, repo, kind, number, head_sha, head_branch,"
            " stage, self_authored, envelope, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (env.get("source"), env.get("delivery_id"), env.get("event"), env.get("repo"), kind,
             env.get("number"), env.get("head_sha"), env.get("head_branch"), stage_name,
             int(out["self_authored"]), json.dumps(out, sort_keys=True), time.time()))
        conn.commit()
        out["stage"] = stage_name
        return out
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _record_heads(conn: sqlite3.Connection, env: Dict[str, Any]) -> None:
    """Record (repo, pr) -> head from any PR-bearing event that carries a head (D4)."""
    nums = env.get("pr_numbers") or []
    if not env.get("repo") or not nums or not (env.get("head_sha") or env.get("head_branch")):
        return
    is_pr_event = env.get("event") == "pull_request"
    now = time.time()
    for pr in nums:
        conn.execute(
            "INSERT INTO pr_heads (repo, pr, head_sha, head_branch, open, updated_at) VALUES (?,?,?,?,1,?)"
            " ON CONFLICT(repo, pr) DO UPDATE SET"
            " head_sha = COALESCE(excluded.head_sha, pr_heads.head_sha),"
            " head_branch = COALESCE(excluded.head_branch, pr_heads.head_branch),"
            " updated_at = excluded.updated_at",
            (env["repo"], int(pr), env.get("head_sha"), env.get("head_branch"), now))
        if is_pr_event and env.get("action") in ("closed", "reopened"):
            conn.execute("UPDATE pr_heads SET open = ? WHERE repo = ? AND pr = ?",
                         (0 if env["action"] == "closed" else 1, env["repo"], int(pr)))


# --------------------------------------------------------------------------- ownership


def resolve_owner(repo: str, pr: int) -> Optional[sqlite3.Row]:
    """The single owner lookup: ``(owner_profile, session_id, thread_id)`` or None."""
    conn = connect()
    try:
        return conn.execute(
            "SELECT owner_profile, session_id, thread_id FROM pr_owner WHERE repo = ? AND pr = ?",
            (str(repo), int(pr))).fetchone()
    finally:
        conn.close()


def open_claimed(conn: sqlite3.Connection, repo: str) -> List[sqlite3.Row]:
    """Open, unsuperseded claimed PRs in ``repo`` with whatever head is recorded."""
    conn.row_factory = sqlite3.Row
    return list(conn.execute(
        "SELECT o.pr, o.owner_profile, o.session_id, h.head_sha, h.head_branch"
        " FROM pr_owner o LEFT JOIN pr_heads h ON h.repo = o.repo AND h.pr = o.pr"
        " WHERE o.repo = ? AND o.superseded_by IS NULL AND COALESCE(h.open, 1) = 1",
        (str(repo),)))


# --------------------------------------------------------------------------- drain lease


def _holder_alive(holder: str) -> bool:
    """A lease holder is ``<pid>:<create_time>:<thread>``; dead process -> stealable."""
    try:
        import psutil

        pid_s, created_s, _thread = holder.split(":", 2)
        proc = psutil.Process(int(pid_s))
        return abs(proc.create_time() - float(created_s)) < 1.0
    except Exception:
        return False


def acquire_lease(holder: str, ttl_seconds: float) -> bool:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT holder, expires_at FROM drain_lease WHERE id = 1").fetchone()
        now = time.time()
        # A live holder past its deadline is a slow delivery, not an abandoned one:
        # taking its lease would let a second pass run the same pending row.
        if row is not None and row[0] != holder and _holder_alive(row[0]):
            conn.rollback()
            return False
        conn.execute("INSERT INTO drain_lease (id, holder, expires_at) VALUES (1, ?, ?)"
                     " ON CONFLICT(id) DO UPDATE SET holder = excluded.holder,"
                     " expires_at = excluded.expires_at", (holder, now + ttl_seconds))
        conn.commit()
        return True
    finally:
        conn.close()


def release_lease(holder: str) -> None:
    conn = connect()
    try:
        conn.execute("DELETE FROM drain_lease WHERE id = 1 AND holder = ?", (holder,))
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- guard state


def record_guard(breach: bool, routes: List[str], reason: Optional[str]) -> None:
    conn = connect()
    try:
        if breach:
            conn.execute("INSERT INTO guard_state (id, breach, routes, reason, at) VALUES (1, 1, ?, ?, ?)"
                         " ON CONFLICT(id) DO UPDATE SET breach = 1, routes = excluded.routes,"
                         " reason = excluded.reason, at = excluded.at",
                         (json.dumps(sorted(routes)), reason, time.time()))
        else:
            conn.execute("DELETE FROM guard_state WHERE id = 1")
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- status

STUCK_ATTEMPTS = 3


def status() -> Dict[str, Any]:
    """The ``hermes ingress status --json`` object. Never a secret, header value or transcript."""
    conn = connect()
    try:
        pending, stuck = [], []
        for d_id, repo, pr, head, state, attempts, target in conn.execute(
                "SELECT d.outbox_delivery, d.repo, d.pr, o.head_sha, d.state, d.attempts, d.target_profile"
                " FROM deliveries d JOIN outbox o ON o.id = d.outbox_id"
                " WHERE d.state IN ('pending', 'awaiting_association') ORDER BY d.id"):
            item = {"delivery_id": d_id, "repo": repo, "pr": pr or None, "head_sha": head, "state": state}
            pending.append(item)
            if state == "pending" and attempts >= STUCK_ATTEMPTS:
                stuck.append({**item, "target_profile": target, "attempts": attempts})
        runaway = [{"repo": r, "pr": p, "hour": h} for r, p, h in
                   conn.execute("SELECT repo, pr, hour FROM runaway ORDER BY rowid")]
        refusals = [{"repo": r, "pr": p, "holder": h, "claimant": c, "reason": why} for r, p, h, c, why in
                    conn.execute("SELECT repo, pr, holder, claimant, reason FROM claim_refusals ORDER BY rowid")]
        unclaimable = [{"repo": r, "pr": p, "claimant": c, "reason": why} for r, p, c, why in
                       conn.execute("SELECT repo, pr, claimant, reason FROM unclaimable ORDER BY rowid")]
        row = conn.execute("SELECT breach, routes, reason FROM guard_state WHERE id = 1").fetchone()
        guard = ({"breach": bool(row[0]), "routes": json.loads(row[1]), "reason": row[2]} if row
                 else {"breach": False, "routes": [], "reason": None})
        return {"pending": pending, "stuck": stuck, "runaway": runaway, "refusals": refusals,
                "unclaimable": unclaimable, "webhook_guard": guard}
    finally:
        conn.close()
