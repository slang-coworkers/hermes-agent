"""Acceptance coverage for SCHED-F34 per-profile timezone behavior."""

import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import hermes_time


@pytest.fixture(autouse=True)
def _fresh_tz_cache(monkeypatch):
    monkeypatch.delenv("HERMES_TIMEZONE", raising=False)
    hermes_time.reset_cache()
    yield
    hermes_time.reset_cache()


def _profile_home(tmp_path: Path, name: str, tz_value: str) -> Path:
    home = tmp_path / name
    home.mkdir()
    (home / "config.yaml").write_text(f"timezone: {tz_value}\n", encoding="utf-8")
    return home


def _activate(monkeypatch, home: Path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(home))
    hermes_time.reset_cache()


def _make_agent(**overrides):
    # Fixture shape mirrors tests/agent/test_system_prompt.py so
    # build_system_prompt_parts renders the volatile timestamp block.
    base = dict(
        load_soul_identity=False,
        skip_context_files=False,
        valid_tool_names=[],
        _task_completion_guidance=False,
        _tool_use_enforcement=False,
        _environment_probe=False,
        _kanban_worker_guidance="",
        _memory_store=None,
        _memory_manager=None,
        model="",
        provider="",
        platform="",
        pass_session_id=False,
        session_id="",
        session_start=None,
        _bot_chat_timeless_prompt=False,
        _emit_status=lambda *_args, **_kwargs: None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _eastern_offset_at(dt: datetime) -> timedelta:
    # Offset at dt's own instant, so a run on a DST-transition day is correct.
    return dt.astimezone(ZoneInfo("America/New_York")).utcoffset()


def _started_line(prompt: str) -> str:
    for line in prompt.splitlines():
        if line.startswith("Conversation started:"):
            return line
    raise AssertionError("no 'Conversation started:' line in prompt")


def test_ac_sched_f34_1(tmp_path, monkeypatch):
    """With HERMES_TIMEZONE unset, a profile config.yaml `timezone: Asia/Kolkata`
    resolves to that zone and now() carries +05:30."""
    _activate(monkeypatch, _profile_home(tmp_path, "kolkata", "Asia/Kolkata"))

    tz = hermes_time.get_timezone()
    assert tz is not None
    assert tz.key == "Asia/Kolkata"
    assert hermes_time.now().utcoffset() == timedelta(hours=5, minutes=30)


def test_ac_sched_f34_2(tmp_path, monkeypatch):
    """HERMES_TIMEZONE env overrides the profile config.yaml timezone value.

    This is the precedence the gateway relies on: the launch profile's config
    timezone is bridged into HERMES_TIMEZONE (gateway/run.py:2862), and env wins
    over any profile config."""
    _activate(monkeypatch, _profile_home(tmp_path, "kolkata", "Asia/Kolkata"))

    monkeypatch.setenv("HERMES_TIMEZONE", "America/New_York")
    hermes_time.reset_cache()

    tz = hermes_time.get_timezone()
    assert tz is not None
    assert tz.key == "America/New_York"


def test_ac_sched_f34_3(tmp_path, monkeypatch, caplog):
    """An invalid IANA value warns and falls back to server-local without
    raising: get_timezone() is None and now() is tz-aware."""
    _activate(monkeypatch, _profile_home(tmp_path, "bad", "Not/AZone"))

    with caplog.at_level(logging.WARNING):
        resolved = hermes_time.get_timezone()
        stamp = hermes_time.now()

    assert resolved is None
    assert stamp.tzinfo is not None
    assert any("timezone" in rec.getMessage().lower() for rec in caplog.records)


def test_ac_sched_f34_4(tmp_path, monkeypatch):
    """Two profiles under different HERMES_HOMEs resolve independently across
    switches (including switching back) with no bridged HERMES_TIMEZONE — the
    path-keyed cache is the mechanism multiplexed coworker profiles use, so one
    profile's zone never leaks into another."""
    tokyo = _profile_home(tmp_path, "tokyo", "Asia/Tokyo")
    newyork = _profile_home(tmp_path, "newyork", "America/New_York")

    monkeypatch.setenv("HERMES_HOME", str(tokyo))
    assert hermes_time.get_timezone().key == "Asia/Tokyo"

    monkeypatch.setenv("HERMES_HOME", str(newyork))
    assert hermes_time.get_timezone().key == "America/New_York"

    monkeypatch.setenv("HERMES_HOME", str(tokyo))
    assert hermes_time.get_timezone().key == "Asia/Tokyo"


def test_ac_sched_f34_5(tmp_path, monkeypatch):
    """A daily cron (0 14 * * *) under a profile with timezone America/New_York
    anchors next_run_at to that zone: Eastern offset and wall-clock hour 14, not
    the host/backend UTC zone."""
    _activate(monkeypatch, _profile_home(tmp_path, "newyork", "America/New_York"))

    from cron.jobs import compute_next_run, parse_schedule

    next_run_iso = compute_next_run(parse_schedule("0 14 * * *"))
    assert next_run_iso is not None
    next_run = datetime.fromisoformat(next_run_iso)

    assert next_run.utcoffset() == _eastern_offset_at(next_run)
    assert next_run.utcoffset() != timedelta(0)
    assert (next_run.hour, next_run.minute) == (14, 0)


def test_ac_sched_f34_6(tmp_path, monkeypatch):
    """The system-prompt clock renders the configured profile zone: the volatile
    'Conversation started:' line names the IANA id and its UTC offset, and a
    different configured zone yields a different line."""
    from agent.system_prompt import build_system_prompt_parts

    _activate(monkeypatch, _profile_home(tmp_path, "newyork", "America/New_York"))
    ny_line = _started_line(build_system_prompt_parts(_make_agent())["volatile"])
    assert "America/New_York" in ny_line
    assert ("UTC-05:00" in ny_line) or ("UTC-04:00" in ny_line)

    _activate(monkeypatch, _profile_home(tmp_path, "kolkata", "Asia/Kolkata"))
    kol_line = _started_line(build_system_prompt_parts(_make_agent())["volatile"])
    assert "Asia/Kolkata" in kol_line
    assert "UTC+05:30" in kol_line
    assert "America/New_York" not in kol_line
    assert ny_line != kol_line


def test_ac_sched_f34_7(tmp_path, monkeypatch):
    """A profile's durable cron run records use the configured zone: the
    execution-ledger claimed/started/finished stamps carry the Eastern offset,
    and the save_job_output() run-log filename uses the profile-local wall clock
    (offset from host-UTC by >= 4h)."""
    _activate(monkeypatch, _profile_home(tmp_path, "newyork", "America/New_York"))

    import cron.executions as executions
    from cron.jobs import save_job_output

    monkeypatch.setattr(
        executions, "EXECUTIONS_FILE", tmp_path / "ledger" / "cron" / "executions.db"
    )

    claimed = executions.create_execution("job-1", source="builtin")
    running = executions.mark_execution_running(claimed["id"])
    finished = executions.finish_execution(claimed["id"], success=True)

    for stamp_iso in (
        claimed["claimed_at"],
        running["started_at"],
        finished["finished_at"],
    ):
        dt = datetime.fromisoformat(stamp_iso)
        assert dt.utcoffset() == _eastern_offset_at(dt)
        assert dt.utcoffset() != timedelta(0)

    before = hermes_time.now()
    out_path = save_job_output("job-1", "body")
    after = hermes_time.now()

    stem_dt = datetime.strptime(out_path.stem, "%Y-%m-%d_%H-%M-%S")
    assert (
        before.replace(tzinfo=None) - timedelta(seconds=2)
        <= stem_dt
        <= after.replace(tzinfo=None) + timedelta(seconds=2)
    )
    # The profile (Eastern, UTC-4/-5) and host (UTC) wall clocks differ by the
    # profile's offset; the gap therefore exceeds ~4h (minus a truncation margin).
    assert abs((stem_dt - datetime.now()).total_seconds()) > 4 * 3600 - 120


def test_ac_sched_f34_8(tmp_path, monkeypatch):
    """The operator/bot split: a standard application-log record through
    RedactingFormatter renders host-local time, while hermes_time.now() resolves
    to the active profile zone."""
    from agent.redact import RedactingFormatter

    _activate(monkeypatch, _profile_home(tmp_path, "newyork", "America/New_York"))

    epoch = 1700000000.0
    record = logging.LogRecord("acc", logging.INFO, __file__, 1, "msg", None, None)
    record.created = epoch
    formatter = RedactingFormatter(
        "%(asctime)s|%(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    asctime = formatter.format(record).split("|", 1)[0]

    assert asctime == time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))
    eastern_render = datetime.fromtimestamp(
        epoch, ZoneInfo("America/New_York")
    ).strftime("%Y-%m-%d %H:%M:%S")
    assert asctime != eastern_render

    now = hermes_time.now()
    tz = hermes_time.get_timezone()
    assert tz is not None and tz.key == "America/New_York"
    assert now.utcoffset() == _eastern_offset_at(now)


def test_ac_sched_f34_9(tmp_path, monkeypatch):
    """Multiplex runtime path: with the DEFAULT/multiplexer profile's timezone
    empty (nothing bridged into HERMES_TIMEZONE), entering each coworker profile
    via the gateway's set_hermes_home_override contextvar seam resolves that
    coworker's own config zone independently."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    default_home = tmp_path / "multiplexer"
    default_home.mkdir()
    (default_home / "config.yaml").write_text('timezone: ""\n', encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    monkeypatch.delenv("HERMES_TIMEZONE", raising=False)
    hermes_time.reset_cache()
    # An empty multiplexer timezone resolves to server-local, so the launch
    # bridge (gateway/run.py:2862-2865, guarded on a truthy value) sets nothing.
    assert hermes_time.get_timezone() is None

    tokyo = _profile_home(tmp_path, "cowork-tokyo", "Asia/Tokyo")
    newyork = _profile_home(tmp_path, "cowork-ny", "America/New_York")
    for home, key in ((tokyo, "Asia/Tokyo"), (newyork, "America/New_York")):
        token = set_hermes_home_override(str(home))
        try:
            hermes_time.reset_cache()
            assert hermes_time.get_timezone().key == key
        finally:
            reset_hermes_home_override(token)
