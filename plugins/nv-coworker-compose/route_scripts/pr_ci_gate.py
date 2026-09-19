"""LOOP-F40 CI-gate route script for `pull_request` deliveries.

Rendered by nv-coworker-compose into the DEFAULT profile's ``scripts/`` and
installed to ``$HERMES_HOME/scripts/pr_ci_gate.py`` (the path the webhook
resolver expects, gateway/platforms/webhook_filters.py). The webhook adapter
runs it as a subprocess with the ``pull_request`` payload on stdin and reads a
``[SILENT]`` sentinel / JSON transform on stdout.

It parks a per-head *blocked* review card for the reviewer and stays silent
while CI is not green; ``check_gate.py`` promotes the card to ``ready`` once a
``check_suite`` success lands for the same head, at which point the ordinary
kanban dispatcher spawns the reviewer worker. ``evaluate()`` is imported and
driven directly by the acceptance test.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

# The canonical reviewer profile a parked review card is assigned to; the
# ordinary dispatcher spawns `hermes -p <assignee> chat -q` when it goes ready.
REVIEWER_ASSIGNEE = "reviewer"
_PR_ACTIONS = {"opened", "reopened", "synchronize", "ready_for_review"}


def _key(repo: str, pr: int, head_sha: str) -> str:
    return f"loop-f40-review:{repo}#{pr}:{head_sha}"


def _sticky_block(kconn, task_id: str, reason: str) -> None:
    """Append a sticky `blocked` event for a freshly-parked card.

    A `blocked` status with no `blocked` task event is auto-recoverable, so the
    dispatcher's recompute_ready would promote it to `ready`; a `blocked` event
    makes it sticky until an `unblocked` event (unblock_task) fires. The caller
    MUST already hold the kanban write_txn: the append then commits atomically
    with create_task, so no recompute_ready tick can observe the card blocked
    without its sticky event and promote it past the CI gate.
    """
    from hermes_cli.kanban_db import _append_event, _has_sticky_block

    row = kconn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row and row[0] == "blocked" and not _has_sticky_block(kconn, task_id):
        _append_event(kconn, task_id, "blocked", {"reason": reason, "kind": "transient"})


def _open_gate(gate_db_path=None) -> sqlite3.Connection:
    # Production resolves the gate store through the sanctioned plugin_db API
    # (a WAL-configured connection under <hermes_home>/plugin-data/, surviving
    # plugin update/remove); tests pass an explicit path. Either way the caller
    # owns transactions (isolation_level=None + explicit BEGIN IMMEDIATE gives
    # the write lock up front, so concurrent duplicate deliveries serialize on
    # this DB across the whole read-decide-mutate section).
    if gate_db_path is None:
        from plugins.plugin_storage import plugin_db

        conn = plugin_db("nv-coworker-compose", "loop-f40-ci-gate.db")
    else:
        conn = sqlite3.connect(str(gate_db_path), timeout=30)
        # Hermes's WAL helper falls back to DELETE on WAL-incompatible
        # filesystems and SQLite builds with the WAL-reset corruption bug.
        from hermes_state import apply_wal_with_fallback

        apply_wal_with_fallback(conn, db_label="loop-f40-ci-gate.db")
    conn.isolation_level = None
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS pr_gate ("
        "repo TEXT, pr INTEGER, latest_head_sha TEXT, current_task_id TEXT, "
        "pr_updated_at TEXT, updated_at INTEGER, PRIMARY KEY (repo, pr))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS green ("
        "repo TEXT, pr INTEGER, head_sha TEXT, PRIMARY KEY (repo, pr, head_sha))"
    )
    return conn


def _extract(payload: dict):
    repo = ((payload.get("repository") or {}).get("full_name")) or ""
    pr_obj = payload.get("pull_request") or {}
    pr = pr_obj.get("number", payload.get("number"))
    head_sha = ((pr_obj.get("head") or {}).get("sha")) or ""
    action = payload.get("action") or ""
    # pull_request.updated_at is an ISO-8601 UTC string (lexicographically ordered),
    # used for the out-of-order-redelivery guard; may be absent on a synthetic payload.
    updated_at = pr_obj.get("updated_at")
    return repo, pr, head_sha, action, updated_at


def evaluate(payload, *, gate_db_path=None, kanban_db_path):
    """Park a per-head blocked review card and stay silent.

    Returns the webhook ``(should_continue, transformed)`` tuple — always
    ``(False, None)``: a pull_request delivery never dispatches a run itself.
    """
    repo, pr, head_sha, action, updated_at = _extract(payload)
    if not repo or pr is None or not head_sha or action not in _PR_ACTIONS:
        return False, None

    from hermes_cli import kanban_db

    gate = _open_gate(gate_db_path)
    try:
        gate.execute("BEGIN IMMEDIATE")
        row = gate.execute(
            "SELECT latest_head_sha, current_task_id, pr_updated_at FROM pr_gate "
            "WHERE repo = ? AND pr = ?",
            (repo, pr),
        ).fetchone()
        prior_head = row[0] if row else None
        prior_task = row[1] if row else None
        prior_updated_at = row[2] if row else None

        # Ordering guard: route scripts run before X-GitHub-Delivery dedup (webhook.py:787
        # vs :857), so a redelivered/older pull_request can arrive after a newer one. If
        # this delivery's pull_request.updated_at is STRICTLY older than the last one
        # processed, ignore it — no archive, no re-arm, no latest_head_sha/pr_updated_at
        # write. Otherwise an older event for a superseded head would archive the current
        # head's card and re-arm the stale head, and the real head (no new push follows)
        # would never re-arm — its queued review lost. ISO-8601 UTC stamps compare
        # lexicographically; a missing stamp on either side falls back to last-write-wins.
        if updated_at and prior_updated_at and updated_at < prior_updated_at:
            gate.execute("COMMIT")
            return False, None

        with kanban_db.connect_closing(db_path=Path(kanban_db_path)) as kconn:
            # A re-sync to a new head retires the superseded head's still-open
            # card, because the ordinary dispatcher spawns EVERY ready card with
            # no head predicate — a stale ready card would review the wrong head.
            if prior_head and prior_head != head_sha:
                # The superseded head's green is now dead — drop only that head's
                # row (never other heads': a green may be recorded ahead of its
                # own PR event for a still-future head).
                gate.execute(
                    "DELETE FROM green WHERE repo = ? AND pr = ? AND head_sha = ?",
                    (repo, pr, prior_head),
                )
                if prior_task:
                    # Retire the superseded head's card with ONE status-guarded
                    # UPDATE: it serializes with the dispatcher's own guarded claim
                    # (ready->running) under SQLite single-writer, so a running/done
                    # card is never archived while a still-blocked/ready superseded
                    # head always is. Column clears mirror kanban_db.archive_task.
                    with kanban_db.write_txn(kconn):
                        kconn.execute(
                            "UPDATE tasks SET status = 'archived', "
                            "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL "
                            "WHERE id = ? AND status IN ('blocked', 'ready')",
                            (prior_task,),
                        )

            key = _key(repo, pr, head_sha)
            # Create the parked card and make it sticky-blocked in ONE kanban
            # write_txn (create_task nests via savepoint under it). Committing the
            # card and its `blocked` event together closes the window in which a
            # dispatcher recompute_ready tick could see the blocked card without a
            # sticky event and auto-promote it past the CI gate.
            # create_task with an idempotency_key returns the existing non-archived
            # card's id (a duplicate delivery makes no new card; a resync back to a
            # previously-archived head makes a fresh one). skills=["sdlc-review"]
            # rides on the card so the ordinary-lane spawn surfaces
            # `--skills sdlc-review` on the reviewer run (the ordinary ready lane
            # does not auto-load the review skill; only the native review lane does).
            with kanban_db.write_txn(kconn):
                task_id = kanban_db.create_task(
                    kconn,
                    title=f"Review {repo}#{pr} @ {head_sha[:12]}",
                    assignee=REVIEWER_ASSIGNEE,
                    idempotency_key=key,
                    initial_status="blocked",
                    skills=["sdlc-review"],
                )
                _sticky_block(kconn, task_id, "loop-f40: awaiting CI green")

            gate.execute(
                "INSERT INTO pr_gate (repo, pr, latest_head_sha, current_task_id, "
                "pr_updated_at, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(repo, pr) DO UPDATE SET "
                "latest_head_sha = excluded.latest_head_sha, "
                "current_task_id = excluded.current_task_id, "
                "pr_updated_at = excluded.pr_updated_at, "
                "updated_at = excluded.updated_at",
                (repo, pr, head_sha, task_id, updated_at, int(time.time())),
            )

            # Out-of-order: a check_suite success for this head already landed
            # (recorded in `green`) before this pull_request event created the
            # card — promote it now. unblock_task opens its own write_txn, so it
            # runs after the create+sticky txn commits, never nested inside it.
            already_green = gate.execute(
                "SELECT 1 FROM green WHERE repo = ? AND pr = ? AND head_sha = ?",
                (repo, pr, head_sha),
            ).fetchone()
            if already_green:
                kanban_db.unblock_task(kconn, task_id)

        gate.execute("COMMIT")
    except Exception:
        gate.execute("ROLLBACK")
        raise
    finally:
        gate.close()
    return False, None


def _default_kanban_db_path() -> str:
    from hermes_cli.kanban_db import kanban_db_path

    return str(kanban_db_path())


def _main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        print("[SILENT]")
        return
    if not isinstance(payload, dict):
        print("[SILENT]")
        return
    keep, transformed = evaluate(
        payload,
        gate_db_path=None,  # production: plugin_db("nv-coworker-compose", "loop-f40-ci-gate.db")
        kanban_db_path=_default_kanban_db_path(),
    )
    print(json.dumps(transformed) if keep and transformed is not None else "[SILENT]")


if __name__ == "__main__":
    _main()
