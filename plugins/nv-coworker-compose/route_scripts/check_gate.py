"""LOOP-F40 CI-gate route script for `check_suite` deliveries.

Rendered by nv-coworker-compose into the DEFAULT profile's ``scripts/`` and
installed to ``$HERMES_HOME/scripts/check_gate.py``. The webhook adapter runs
it as a subprocess with the ``check_suite`` payload on stdin.

On a ``success`` for the PR's current head it promotes that head's parked card
to ``ready`` (the ordinary dispatcher then spawns the reviewer) and stays
silent; a stale-head success does not promote; a non-success returns the
payload so the fixer-bound route wakes the fixer, and it deletes any earlier
green recorded for that exact head so a superseded success cannot promote a
later card. ``evaluate()`` is imported and driven directly by the acceptance
test.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


def _open_gate(gate_db_path) -> sqlite3.Connection:
    # Schema is kept byte-identical to pr_ci_gate._open_gate: either script may
    # be the first to open the store (a check_suite success can arrive before
    # its pull_request event), so both must create the same tables.
    conn = sqlite3.connect(str(gate_db_path), timeout=30, isolation_level=None)
    from hermes_state import apply_wal_with_fallback

    apply_wal_with_fallback(conn, db_label="loop-f40-ci-gate.db")
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
    cs = payload.get("check_suite") or {}
    repo = ((payload.get("repository") or {}).get("full_name")) or ""
    head_sha = cs.get("head_sha") or ""
    conclusion = cs.get("conclusion")
    pr_numbers = [
        p.get("number")
        for p in (cs.get("pull_requests") or [])
        if isinstance(p, dict) and p.get("number") is not None
    ]
    return repo, head_sha, conclusion, pr_numbers


def evaluate(payload, *, gate_db_path, kanban_db_path):
    """Promote the current-head card on CI success; wake the fixer on failure.

    Returns the webhook ``(should_continue, transformed)`` tuple: ``(False,
    None)`` on success/stale (silent — no needless fixer wake), ``(True,
    payload)`` on a non-success so the fixer-bound route dispatches the fixer.
    """
    repo, head_sha, conclusion, pr_numbers = _extract(payload)
    success = conclusion == "success"
    if not repo or not head_sha or not pr_numbers:
        return (not success), (None if success else payload)

    from hermes_cli import kanban_db

    gate = _open_gate(gate_db_path)
    to_unblock: list[str] = []
    to_reblock: list[str] = []
    try:
        gate.execute("BEGIN IMMEDIATE")
        for pr in pr_numbers:
            row = gate.execute(
                "SELECT latest_head_sha, current_task_id FROM pr_gate "
                "WHERE repo = ? AND pr = ?",
                (repo, pr),
            ).fetchone()
            latest = row[0] if row else None
            task_id = row[1] if row else None
            if success:
                # Record the green head so a not-yet-created card is promoted by
                # pr_ci_gate on creation (out-of-order webhooks).
                gate.execute(
                    "INSERT OR IGNORE INTO green (repo, pr, head_sha) VALUES (?, ?, ?)",
                    (repo, pr, head_sha),
                )
                if latest == head_sha and task_id:
                    to_unblock.append(task_id)
            else:
                # A non-success for this exact head invalidates any earlier green
                # (success -> failure -> PR must leave the card blocked).
                gate.execute(
                    "DELETE FROM green WHERE repo = ? AND pr = ? AND head_sha = ?",
                    (repo, pr, head_sha),
                )
                # CI regressed for the current head after an earlier green: the
                # reviewer must not stay dispatchable, so un-promote the card.
                if latest == head_sha and task_id:
                    to_reblock.append(task_id)
        if to_unblock or to_reblock:
            with kanban_db.connect_closing(db_path=Path(kanban_db_path)) as kconn:
                for task_id in to_unblock:
                    kanban_db.unblock_task(kconn, task_id)
                for task_id in to_reblock:
                    # Only an unclaimed ready card is demoted; a running/done card
                    # (a review already in flight or finished) is left alone. The
                    # sticky `blocked` event is required so recompute_ready does
                    # not immediately re-promote the demoted (parent-less) card.
                    with kanban_db.write_txn(kconn):
                        cas = kconn.execute(
                            "UPDATE tasks SET status = 'blocked' "
                            "WHERE id = ? AND status = 'ready' AND claim_lock IS NULL",
                            (task_id,),
                        )
                        if cas.rowcount == 1:
                            kanban_db._append_event(
                                kconn, task_id, "blocked",
                                {"reason": "loop-f40: CI regressed", "kind": "transient"},
                            )
        gate.execute("COMMIT")
    except Exception:
        gate.execute("ROLLBACK")
        raise
    finally:
        gate.close()

    return (not success), (None if success else payload)


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
