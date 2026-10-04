"""The drain: resolve each staged event to its targets and deliver it exactly once (D5).

The drain runs only in the DEFAULT home, where the fleet ledger lives, so a
coworker profile's process (and the owner's own delivered CLI turn) never drains.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import deliver, ledger, sessions

logger = logging.getLogger(__name__)

RESUME_BACKOFF_CAP_SECONDS = 300.0
_LEASE_TTL_SECONDS = deliver.DELIVERY_TIMEOUT_SECONDS + 60
_PASS_LOCK = threading.Lock()


def is_ledger_home() -> bool:
    from hermes_constants import get_hermes_home

    try:
        return get_hermes_home().resolve() == ledger.ledger_home().resolve()
    except OSError:
        return False


def _holder() -> str:
    """This process as a lease holder: in-process passes are already serialized by
    ``_PASS_LOCK``, so the lease only arbitrates between processes."""
    try:
        import psutil

        created = psutil.Process(os.getpid()).create_time()
    except Exception:
        created = 0.0
    return f"{os.getpid()}:{created}:drain"


# --------------------------------------------------------------------------- resolution


def _owner_target(repo: str, pr: int) -> Optional[Tuple[str, str]]:
    row = ledger.resolve_owner(repo, pr)
    return (row[0], row[1]) if row else None


def _associate(conn, env: Dict[str, Any]) -> Tuple[str, Optional[int]]:
    """For a CI event that names no PR: ``owned`` + the matching claimed PR, ``unowned``
    when shown to belong to no claimed PR, else ``pending`` (D5, AC-8)."""
    claimed = ledger.open_claimed(conn, env.get("repo") or "")
    if not claimed:
        return "unowned", None
    sha, branch = env.get("head_sha"), env.get("head_branch")
    # More than one match is never guessed: the event waits rather than wake the wrong owner.
    for key, value in (("head_sha", sha), ("head_branch", branch)):
        matches = {int(row["pr"]) for row in claimed if value and row[key] == value}
        if len(matches) == 1:
            return "owned", matches.pop()
        if matches:
            return "pending", None
    if branch and all(row["head_branch"] for row in claimed):
        return "unowned", None
    return "pending", None


def _plan(conn, outbox_id: int, env: Dict[str, Any], orch: str) -> None:
    """Create this outbox row's delivery rows (one per PR number)."""
    now = time.time()
    if env.get("kind") == "issue":
        conn.execute("INSERT OR IGNORE INTO deliveries (outbox_id, outbox_delivery, repo, pr, target_profile,"
                     " target_kind, state, created_at) VALUES (?, ?, ?, 0, ?, 'bot_chat', 'pending', ?)",
                     (outbox_id, env["delivery_id"], env.get("repo"), orch, now))
        return
    nums = env.get("pr_numbers") or []
    state = "pending" if nums or env.get("kind") != "ci" else "awaiting_association"
    for pr in nums or [0]:
        conn.execute("INSERT OR IGNORE INTO deliveries (outbox_id, outbox_delivery, repo, pr, state, created_at)"
                     " VALUES (?, ?, ?, ?, ?, ?)", (outbox_id, env["delivery_id"], env.get("repo"), int(pr), state, now))


def _resolve(conn, d_id: int, env: Dict[str, Any], pr: int, orch: str) -> None:
    """Bind an unresolved delivery row to its target, or leave it pending (fail closed)."""
    if not pr:
        verdict, matched = _associate(conn, env)
        if verdict == "pending":
            conn.execute("UPDATE deliveries SET state = 'awaiting_association' WHERE id = ?", (d_id,))
            return
        if verdict == "owned":
            dup = conn.execute("SELECT 1 FROM deliveries WHERE outbox_id = (SELECT outbox_id FROM deliveries"
                               " WHERE id = ?) AND pr = ?", (d_id, matched)).fetchone()
            if dup:
                conn.execute("UPDATE deliveries SET state = 'done' WHERE id = ?", (d_id,))
                return
            conn.execute("UPDATE deliveries SET pr = ? WHERE id = ?", (matched, d_id))
            pr = matched
        else:
            conn.execute("UPDATE deliveries SET target_profile = ?, target_kind = 'bot_chat', state = 'pending'"
                         " WHERE id = ?", (orch, d_id))
            return
    owner = _owner_target(env["repo"], pr)
    if owner is None:
        conn.execute("UPDATE deliveries SET target_profile = ?, target_kind = 'bot_chat', state = 'pending'"
                     " WHERE id = ?", (orch, d_id))
    else:
        conn.execute("UPDATE deliveries SET target_profile = ?, target_session = ?, target_kind = 'owner',"
                     " state = 'pending' WHERE id = ?", (owner[0], owner[1], d_id))


def plan_and_resolve(orch: str) -> None:
    """Plan new outbox rows, then resolve every unresolved delivery. A lookup error
    leaves the row unresolved (pending), never dropped and never misrouted."""
    conn = ledger.connect()
    try:
        for outbox_id, raw in list(conn.execute(
                "SELECT id, envelope FROM outbox WHERE stage = 'new' AND resolved = 0 ORDER BY id")):
            conn.execute("BEGIN IMMEDIATE")
            _plan(conn, outbox_id, json.loads(raw), orch)
            conn.execute("UPDATE outbox SET resolved = 1 WHERE id = ?", (outbox_id,))
            conn.commit()
        rows = list(conn.execute(
            "SELECT d.id, d.pr, o.envelope FROM deliveries d JOIN outbox o ON o.id = d.outbox_id"
            " WHERE d.target_kind IS NULL AND d.state IN ('pending', 'awaiting_association') ORDER BY d.id"))
        for d_id, pr, raw in rows:
            try:
                conn.execute("BEGIN IMMEDIATE")
                _resolve(conn, d_id, json.loads(raw), int(pr or 0), orch)
                conn.commit()
            except Exception:
                conn.rollback()
                logger.warning("nv-ingress: owner resolution failed for delivery %s; kept pending", d_id,
                               exc_info=True)
    finally:
        conn.close()


# --------------------------------------------------------------------------- delivery


def _count_budget(conn, repo: str, pr: int, budget: int) -> None:
    """Record one delivery for (repo, pr) this hour; flag the hour once when over budget.
    Never holds a delivery back: the owner always receives it (AC-23)."""
    hour = int(time.time() // 3600)
    conn.execute("INSERT INTO budget (repo, pr, hour, count) VALUES (?, ?, ?, 1) ON CONFLICT(repo, pr, hour)"
                 " DO UPDATE SET count = count + 1", (repo, pr, hour))
    count = conn.execute("SELECT count FROM budget WHERE repo = ? AND pr = ? AND hour = ?",
                         (repo, pr, hour)).fetchone()[0]
    if count > budget:
        cur = conn.execute("INSERT OR IGNORE INTO runaway (repo, pr, hour) VALUES (?, ?, ?)", (repo, pr, hour))
        if cur.rowcount == 1:
            logger.warning("nv-ingress runaway: %s#%s exceeded %d deliveries this hour; every event is"
                           " still delivered to its owner", repo, pr, budget)


def _deliver_one(row: Tuple, settings: Dict[str, Any]) -> None:
    d_id, pr, profile, session, kind, attempts, raw = row
    env = json.loads(raw)
    pr_or_none = int(pr) if pr else None
    mark = deliver.marker(env, pr_or_none)
    if kind == "owner":
        target = sessions.tip(profile, session)
    else:
        target = sessions.bot_chat(profile)
    if target and sessions.marker_present(profile, target, mark):
        _finish(d_id, "done")
        return
    text = deliver.prompt(env, pr_or_none, unowned=(kind == "bot_chat" and pr_or_none is not None))
    path = deliver.write_prompt(text)
    try:
        argv = (deliver.resume_argv(profile, target, path) if kind == "owner"
                else deliver.bot_chat_argv(profile, path))
        rc = deliver.run_cli(argv, path)
    finally:
        Path(path).unlink(missing_ok=True)
    if rc == 0:
        _finish(d_id, "done")
        if pr_or_none is not None and env.get("repo"):
            conn = ledger.connect()
            try:
                _count_budget(conn, env["repo"], pr_or_none, int(settings.get("per_pr_hourly_budget") or 30))
                conn.commit()
            finally:
                conn.close()
        return
    # A failed delivery stays pending for the SAME target, never retargeted (AC-7).
    conn = ledger.connect()
    try:
        backoff = min(RESUME_BACKOFF_CAP_SECONDS, 5.0 * (2 ** attempts))
        conn.execute("UPDATE deliveries SET attempts = attempts + 1, next_attempt_at = ? WHERE id = ?",
                     (time.time() + backoff, d_id))
        conn.commit()
    finally:
        conn.close()


def _finish(d_id: int, state: str) -> None:
    conn = ledger.connect()
    try:
        conn.execute("UPDATE deliveries SET state = ? WHERE id = ?", (state, d_id))
        conn.commit()
    finally:
        conn.close()


def drain_once(settings: Dict[str, Any]) -> int:
    """One drain pass: plan, resolve, deliver every due row. Returns rows delivered."""
    if not is_ledger_home() or not ledger.exists():
        return 0
    orch = str(settings.get("orchestrator_profile") or "orchestrator")
    holder = _holder()
    with _PASS_LOCK:
        if not ledger.acquire_lease(holder, _LEASE_TTL_SECONDS):
            return 0
        delivered = 0
        try:
            plan_and_resolve(orch)
            conn = ledger.connect()
            try:
                due = list(conn.execute(
                    "SELECT d.id, d.pr, d.target_profile, d.target_session, d.target_kind, d.attempts,"
                    " o.envelope FROM deliveries d JOIN outbox o ON o.id = d.outbox_id"
                    " WHERE d.state = 'pending' AND d.target_kind IS NOT NULL AND d.next_attempt_at <= ?"
                    " ORDER BY d.id", (time.time(),)))
            finally:
                conn.close()
            for row in due:
                if not ledger.acquire_lease(holder, _LEASE_TTL_SECONDS):
                    break
                try:
                    _deliver_one(row, settings)
                    delivered += 1
                except Exception:
                    logger.warning("nv-ingress delivery %s failed; kept pending", row[0], exc_info=True)
        finally:
            ledger.release_lease(holder)
        return delivered
