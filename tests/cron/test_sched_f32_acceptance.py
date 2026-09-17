"""Acceptance test for SCHED-F32 — Durable scheduled tasks with cron recurrence and run logs.

ADOPT / adopt-native row: Hermes cron already satisfies the requirement, so this test
imports and CALLS the real cron API and asserts the adopt behaviour. It PASSES on the
stock v2026.8.31 tree (see the ADR ## Green-on-stock section) — the plugin
fail-before/pass-after asymmetry does not apply because there is no plugin. Non-vacuity
comes from calling real code (never reading source as text) and asserting differential
invariants over the PERSISTED store, ledger and output files, not frozen constants.

Ships as: tests/cron/test_sched_f32_acceptance.py
Run only via scripts/run_tests.sh (never bare pytest). All writes stay under tmp_path.

Pin: tag v2026.8.31, commit 29112bef099274229cadff79cdff7bf7b99c4b77.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import cron.executions as executions
from cron.jobs import (
    advance_next_run,
    compute_next_run,
    create_job,
    get_job,
    list_jobs,
    load_jobs,
    parse_schedule,
    save_job_output,
    use_cron_store,
)


def test_ac_sched_f32_1(tmp_path, monkeypatch):
    """A recurring schedule (cron and interval) parses to a schedule dict and re-arms to a strictly later run — as the pure compute_next_run advance and as the persisted pre-dispatch advance_next_run bump — whereas a one-shot (delay/ISO) does not re-arm."""
    cron_sched = parse_schedule("*/30 * * * *")
    interval_sched = parse_schedule("every 30m")
    delay_once = parse_schedule("in 30m")

    assert cron_sched["kind"] == "cron"
    assert interval_sched["kind"] == "interval"
    assert delay_once["kind"] == "once"

    # Anchor everything in the future so a past one-shot never reads as already-fired and
    # the re-arm comparisons are deterministic regardless of the wall clock at test time.
    base = (datetime.now(timezone.utc) + timedelta(days=2)).replace(second=0, microsecond=0)
    iso_once = parse_schedule((base + timedelta(hours=1)).replace(tzinfo=None).isoformat())
    assert iso_once["kind"] == "once"

    for recurring in (cron_sched, interval_sched):
        n1 = compute_next_run(recurring, last_run_at=base.isoformat())
        n2 = compute_next_run(recurring, last_run_at=n1)
        assert n1 is not None and n2 is not None
        assert base < datetime.fromisoformat(n1) < datetime.fromisoformat(n2)

    # One-shots do not re-arm. Evaluated against the real clock (before the persisted-re-arm
    # block patches _hermes_now) so their future run_at is not read as already-expired.
    for one_shot in (delay_once, iso_once):
        first = compute_next_run(one_shot)
        assert first is not None
        assert compute_next_run(one_shot, last_run_at=first) is None

    # advance_next_run is the persisted store path (distinct from the pure compute_next_run
    # advances above): it bumps the STORED next_run_at before each fire and skips one-shots so
    # a crash can still retry them. Cover both recurring forms and the one-shot no-op.
    home = tmp_path / "profiles" / "bot-a"
    home.mkdir(parents=True)
    clock = {"now": base}
    with monkeypatch.context() as clock_patch:
        clock_patch.setattr("cron.jobs._hermes_now", lambda: clock["now"])
        with use_cron_store(home):
            for i, recurring in enumerate(("every 30m", "*/30 * * * *")):
                clock["now"] = base
                jid = create_job(prompt="p", schedule=recurring, name=f"rearm-{i}")["id"]
                due0 = get_job(jid)["next_run_at"]
                clock["now"] = base + timedelta(hours=1)
                advance_next_run(jid)
                due1 = get_job(jid)["next_run_at"]
                clock["now"] = base + timedelta(hours=2)
                advance_next_run(jid)
                due2 = get_job(jid)["next_run_at"]
                d0, d1, d2 = (datetime.fromisoformat(x) for x in (due0, due1, due2))
                assert d0 < d1 < d2

            clock["now"] = base
            once_id = create_job(prompt="p", schedule="in 30m", name="rearm-once")["id"]
            once_due = get_job(once_id)["next_run_at"]
            clock["now"] = base + timedelta(hours=1)
            assert advance_next_run(once_id) is False
            assert get_job(once_id)["next_run_at"] == once_due


def test_ac_sched_f32_2(tmp_path):
    """A created recurring job persists to the active profile's cron/jobs.json as a schedule dict and is returned unchanged by a fresh load_jobs(); a second profile store is independent."""
    home_a = tmp_path / "profiles" / "bot-a"
    home_a.mkdir(parents=True)

    with use_cron_store(home_a):
        job = create_job(
            prompt="digest the inbox",
            schedule="every 1h",
            name="hourly-digest",
        )
        reloaded = load_jobs()

    matches = [j for j in reloaded if j.get("name") == "hourly-digest"]
    assert len(matches) == 1
    persisted = matches[0]

    assert persisted["id"] == job["id"]
    assert persisted["schedule"] == job["schedule"]
    assert persisted["next_run_at"] == job["next_run_at"]
    # A dict schedule with no 'kind' silently never fires, so assert the parsed shape
    # was persisted rather than the raw schedule string.
    assert isinstance(persisted["schedule"], dict)
    assert persisted["schedule"].get("kind") in {"cron", "interval"}
    assert persisted["next_run_at"]
    assert (home_a / "cron" / "jobs.json").is_file()

    home_b = tmp_path / "profiles" / "bot-b"
    home_b.mkdir(parents=True)
    with use_cron_store(home_b):
        assert list_jobs(include_disabled=True) == []


def test_ac_sched_f32_3(tmp_path, monkeypatch):
    """A run is recorded across the two durable run-log surfaces: the executions ledger (claimed->running->terminal, terminal immutable) and a per-run output file under the profile's cron/output."""
    home = tmp_path / "profiles" / "bot-a"
    (home / "cron").mkdir(parents=True)
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", home / "cron" / "executions.db")

    ex = executions.create_execution("job-1", source="test")
    assert ex["status"] == "claimed"

    running = executions.mark_execution_running(ex["id"])
    assert running is not None and running["status"] == "running"

    done = executions.finish_execution(ex["id"], success=True)
    assert done is not None and done["status"] == "completed"

    assert any(e.get("id") == ex["id"] for e in executions.list_executions())

    second = executions.finish_execution(ex["id"], success=False, error="late")
    assert second is None
    latest = executions.latest_execution("job-1")
    assert latest is not None and latest["status"] == "completed"

    # The per-run output file is the second run-log surface (the ncl run-log parity).
    with use_cron_store(home):
        output_file = save_job_output("job-1", "run output")
    assert output_file.read_text(encoding="utf-8") == "run output"
    assert output_file.parent.name == "job-1"
    assert output_file.parent.parent.name == "output"
    assert home.resolve() in output_file.resolve().parents
