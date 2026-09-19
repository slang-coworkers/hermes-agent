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
    """Append a sticky `blocked` event (the caller already holds a write_txn).

    A `blocked` status with no `blocked` task event is auto-recoverable, so the
    dispatcher's recompute_ready promotes it to `ready`. A `blocked` event makes
    it sticky until an `unblocked` event (unblock_task) fires.
    """
    from hermes_cli.kanban_db import _append_event

    _append_event(kconn, task_id, "blocked", {"reason": reason, "kind": "transient"})


def _ensure_sticky_block(kconn, task_id: str, reason: str) -> None:
    """Make a blocked card sticky in its own write_txn (used right after create)."""
    from hermes_cli.kanban_db import _has_sticky_block, write_txn

    with write_txn(kconn):
        row = kconn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row and row[0] == "blocked" and not _has_sticky_block(kconn, task_id):
            _sticky_block(kconn, task_id, reason)


def _open_gate(gate_db_path) -> sqlite3.Connection:
    # isolation_level=None + explicit BEGIN IMMEDIATE gives us the write lock
    # up front so concurrent duplicate deliveries serialize on this DB across
    # the whole read-decide-mutate section (the caller owns the transaction).
    conn = sqlite3.connect(str(gate_db_path), timeout=30, isolation_level=None)
    # Use Hermes's WAL helper rather than a raw pragma: it falls back to DELETE
    # on WAL-incompatible filesystems and on SQLite builds carrying the
    # WAL-reset corruption bug (the same safeguard kanban_db uses).
    from hermes_state import apply_wal_with_fallback

    apply_wal_with_fallback(conn, db_label="loop-f40-ci-gate.db")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS pr_gate ("
        "repo TEXT, pr INTEGER, latest_head_sha TEXT, current_task_id TEXT, "
        "updated_at INTEGER, PRIMARY KEY (repo, pr))"
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
    return repo, pr, head_sha, action


def evaluate(payload, *, gate_db_path, kanban_db_path):
    """Park a per-head blocked review card and stay silent.

    Returns the webhook ``(should_continue, transformed)`` tuple — always
    ``(False, None)``: a pull_request delivery never dispatches a run itself.
    """
    repo, pr, head_sha, action = _extract(payload)
    if not repo or pr is None or not head_sha or action not in _PR_ACTIONS:
        return False, None

    from hermes_cli import kanban_db

    gate = _open_gate(gate_db_path)
    try:
        gate.execute("BEGIN IMMEDIATE")
        row = gate.execute(
            "SELECT latest_head_sha, current_task_id FROM pr_gate WHERE repo = ? AND pr = ?",
            (repo, pr),
        ).fetchone()
        prior_head = row[0] if row else None
        prior_task = row[1] if row else None

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
                    # Retire the prior card with ONE status-guarded UPDATE (the
                    # column clears mirror kanban_db.archive_task). A
                    # read-status-then-archive_task form is a TOCTOU: archive_task
                    # archives any non-archived state, so the dispatcher claiming
                    # the card (ready->running) between the read and the archive
                    # would archive a live worker. As a single guarded UPDATE it
                    # serializes with the dispatcher's own guarded claim
                    # (UPDATE ... status='running' WHERE status='ready' AND
                    # claim_lock IS NULL) under SQLite single-writer: whichever
                    # commits first wins, so a running/done card is never archived
                    # and a still-blocked/ready superseded head always is.
                    with kanban_db.write_txn(kconn):
                        kconn.execute(
                            "UPDATE tasks SET status = 'archived', "
                            "claim_lock = NULL, claim_expires = NULL, worker_pid = NULL "
                            "WHERE id = ? AND status IN ('blocked', 'ready')",
                            (prior_task,),
                        )

            key = _key(repo, pr, head_sha)
            # create_task with an idempotency_key returns the existing
            # NON-archived card's id, so a duplicate delivery makes no new card
            # and a resync back to a previously-archived head makes a fresh one.
            # skills=["sdlc-review"] rides on the card so the ordinary-lane spawn
            # surfaces `--skills sdlc-review` on the reviewer run (the ordinary
            # ready lane does not auto-load the review skill; only the native
            # review lane force-appends it).
            task_id = kanban_db.create_task(
                kconn,
                title=f"Review {repo}#{pr} @ {head_sha[:12]}",
                assignee=REVIEWER_ASSIGNEE,
                idempotency_key=key,
                initial_status="blocked",
                skills=["sdlc-review"],
            )
            # A create_task blocked card carries no `blocked` task event, so the
            # dispatcher's recompute_ready treats it as auto-recoverable and
            # promotes it straight to `ready` — defeating the CI gate. Make the
            # parked card sticky-blocked so it stays parked until check_gate's
            # unblock_task (which emits `unblocked`) releases it on CI-green.
            _ensure_sticky_block(kconn, task_id, "loop-f40: awaiting CI green")

            gate.execute(
                "INSERT INTO pr_gate (repo, pr, latest_head_sha, current_task_id, "
                "updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(repo, pr) DO UPDATE SET "
                "latest_head_sha = excluded.latest_head_sha, "
                "current_task_id = excluded.current_task_id, "
                "updated_at = excluded.updated_at",
                (repo, pr, head_sha, task_id, int(time.time())),
            )

            # Out-of-order: a check_suite success for this head already landed
            # (recorded in `green`) before this pull_request event created the
            # card — promote it now.
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


def _default_gate_db_path() -> str:
    from plugins.plugin_storage import plugin_data_dir

    return str(plugin_data_dir("nv-coworker-compose") / "loop-f40-ci-gate.db")


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
        gate_db_path=_default_gate_db_path(),
        kanban_db_path=_default_kanban_db_path(),
    )
    print(json.dumps(transformed) if keep and transformed is not None else "[SILENT]")


if __name__ == "__main__":
    _main()
