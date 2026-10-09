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
from typing import Any, Dict, List, Optional, Tuple

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


def _holder(load_id: str) -> str:
    """One plugin load as a lease holder. A reloaded plugin in the same process is a
    different holder, so it cannot take the row a previous load's pass is still
    delivering (that pass outlives the unload join)."""
    try:
        import psutil

        created = psutil.Process(os.getpid()).create_time()
    except Exception:
        created = 0.0
    return f"{os.getpid()}:{created}:{load_id}"


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


def _group_limit(settings: Dict[str, Any], key: str, default: int) -> int:
    try:
        return max(1, int(settings.get(key) or default))
    except (TypeError, ValueError):
        return default


class _Unit:
    """One delivery turn: a single Bot Chat row, or one coalesced owner group (D5, E13)."""

    def __init__(self, kind: str, profile: str, target: Optional[str], repo: Optional[str], pr: int):
        self.kind, self.profile, self.target, self.repo, self.pr = kind, profile, target, repo, pr
        self.rows: List[Tuple[int, str, Optional[int]]] = []  # (delivery id, marker, pr or None)
        self.blocks: List[str] = []

    def prompt(self) -> str:
        if self.kind == "owner":
            return deliver.group_prompt(self.repo or "?", self.pr, self.blocks)
        return self.blocks[0]


def _commit_done(rows: List[Tuple[int, str, Optional[int]]], repo: Optional[str], settings: Dict[str, Any]) -> None:
    """Mark rows done; a row's FIRST pending -> done counts once toward its PR's hour,
    in the same transaction, whether a turn or a marker reconcile committed it (AC-23)."""
    budget = int(settings.get("per_pr_hourly_budget") or 30)
    conn = ledger.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for d_id, _mark, pr in rows:
            cur = conn.execute("UPDATE deliveries SET state = 'done' WHERE id = ? AND state = 'pending'", (d_id,))
            if cur.rowcount == 1 and pr is not None and repo:
                _count_budget(conn, repo, pr, budget)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _units(due: List[Tuple], settings: Dict[str, Any]) -> List[_Unit]:
    """Partition the due rows into delivery units, in order of each unit's smallest id.

    A row whose marker already sits in its target's lineage is committed here, before
    any attempt, so it never rides a turn. Owner rows group by (profile, session tip,
    repo, PR) up to the row and byte caps; once a group is full, the rest of its rows
    wait for the next pass so id order holds. Bot Chat rows are never batched."""
    max_rows = _group_limit(settings, "max_group_rows", 25)
    max_bytes = _group_limit(settings, "max_group_prompt_bytes", 65536)
    units: List[_Unit] = []
    groups: Dict[Tuple, _Unit] = {}
    full: set = set()
    for d_id, pr, profile, session, kind, _attempts, raw in due:
        try:
            env = json.loads(raw)
            pr_or_none = int(pr) if pr else None
            mark = deliver.marker(env, pr_or_none)
            target = sessions.tip(profile, session) if kind == "owner" else sessions.bot_chat(profile)
            if target and sessions.marker_present(profile, target, mark):
                _commit_done([(d_id, mark, pr_or_none)], env.get("repo"), settings)
                continue
            block = deliver.prompt(env, pr_or_none, to_orchestrator=(kind == "bot_chat"))
            if kind != "owner":
                unit = _Unit(kind, profile, target, env.get("repo"), int(pr or 0))
                unit.rows.append((d_id, mark, pr_or_none))
                unit.blocks.append(block)
                units.append(unit)
                continue
            key = (profile, target, env.get("repo"), int(pr or 0))
            if key in full:
                continue
            unit = groups.get(key)
            if unit is None:
                unit = groups[key] = _Unit("owner", profile, target, env.get("repo"), int(pr or 0))
                units.append(unit)
            elif (len(unit.rows) >= max_rows
                  or len(deliver.group_prompt(unit.repo or "?", unit.pr, unit.blocks + [block]).encode("utf-8"))
                  > max_bytes):
                full.add(key)
                continue
            unit.rows.append((d_id, mark, pr_or_none))
            unit.blocks.append(block)
        except Exception:
            logger.warning("nv-ingress delivery %s could not be prepared; kept pending", d_id, exc_info=True)
    return units


def _current_target(unit: _Unit) -> Optional[str]:
    if unit.kind == "owner":
        return sessions.tip(unit.profile, unit.target) if unit.target else None
    return sessions.bot_chat(unit.profile)


def _landed(unit: _Unit, target: Optional[str]) -> List[Tuple[int, str, Optional[int]]]:
    return [r for r in unit.rows if target and sessions.marker_present(unit.profile, target, r[1])]


def _run_unit(unit: _Unit, settings: Dict[str, Any]) -> None:
    # An earlier unit's turn in this pass may already have committed some of these
    # rows, so the per-row check runs again right before this attempt.
    target = _current_target(unit)
    landed = _landed(unit, target)
    if landed:
        _commit_done(landed, unit.repo, settings)
        kept = [(r, b) for r, b in zip(unit.rows, unit.blocks) if r not in landed]
        unit.rows, unit.blocks = [r for r, _b in kept], [b for _r, b in kept]
        if not unit.rows:
            return
    unit.target = target if unit.kind == "owner" else unit.target
    path = deliver.write_prompt(unit.prompt())
    try:
        argv = (deliver.resume_argv(unit.profile, unit.target, path) if unit.kind == "owner"
                else deliver.bot_chat_argv(unit.profile, path))
        rc = deliver.run_cli(argv, path)
    finally:
        Path(path).unlink(missing_ok=True)
    if rc == 0:
        _commit_done(unit.rows, unit.repo, settings)
        return
    # A turn can commit its user message and still exit non-zero (a first Bot Chat
    # turn creates the session it writes to): rows whose marker landed are done; the
    # rest stay pending for the SAME target, never retargeted (AC-7).
    landed = _landed(unit, _current_target(unit))
    if landed:
        _commit_done(landed, unit.repo, settings)
    rest = [r for r in unit.rows if r not in landed]
    if not rest:
        return
    conn = ledger.connect()
    try:
        for d_id, _mark, _pr in rest:
            attempts = conn.execute("SELECT attempts FROM deliveries WHERE id = ?", (d_id,)).fetchone()[0]
            backoff = min(RESUME_BACKOFF_CAP_SECONDS, 5.0 * (2 ** attempts))
            conn.execute("UPDATE deliveries SET attempts = attempts + 1, next_attempt_at = ? WHERE id = ?",
                         (time.time() + backoff, d_id))
        conn.commit()
    finally:
        conn.close()


def drain_once(settings: Dict[str, Any], load_id: str,
               stop: Optional[threading.Event] = None) -> int:
    """One drain pass: plan, resolve, deliver every due row, coalesced into units (E13).
    Returns rows delivered.
    ``stop`` (the unloading driver's) ends the pass before its next unit."""
    if not is_ledger_home() or not ledger.exists():
        return 0
    orch = str(settings.get("orchestrator_profile") or "orchestrator")
    holder = _holder(load_id)
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
            for unit in _units(due, settings):
                # Renewed before every turn; a live holder is never displaced, whatever its expiry.
                if (stop is not None and stop.is_set()) or not ledger.acquire_lease(holder, _LEASE_TTL_SECONDS):
                    break
                try:
                    _run_unit(unit, settings)
                    delivered += len(unit.rows)
                except Exception:
                    logger.warning("nv-ingress delivery %s failed; kept pending",
                                   [r[0] for r in unit.rows], exc_info=True)
        finally:
            ledger.release_lease(holder)
        return delivered
