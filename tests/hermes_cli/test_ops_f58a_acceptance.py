"""Acceptance test for OPS-F58.a — Ops tooling (migrations, uninstall, doctor).

CONFIGURE / adopt-native row: no plugin, no core edit. Proves the adopted
native ops stack behaves as the slang-coworkers on-call runbook claims, proves
the fleet mechanisms the runbook relies on, and proves the runbook artifact.

Target on the fork: tests/hermes_cli/test_ops_f58a_acceptance.py
Run with: scripts/run_tests.sh tests/hermes_cli/test_ops_f58a_acceptance.py

AC id -> node id map (the req id carries a '.', so '.' and '-' both -> '_'):
  AC-OPS-F58.a-1  -> test_ac_ops_f58_a_1    AC-OPS-F58.a-6  -> test_ac_ops_f58_a_6
  AC-OPS-F58.a-2  -> test_ac_ops_f58_a_2    AC-OPS-F58.a-7  -> test_ac_ops_f58_a_7
  AC-OPS-F58.a-3  -> test_ac_ops_f58_a_3    AC-OPS-F58.a-8  -> test_ac_ops_f58_a_8
  AC-OPS-F58.a-4  -> test_ac_ops_f58_a_4    AC-OPS-F58.a-9  -> test_ac_ops_f58_a_9
  AC-OPS-F58.a-5  -> test_ac_ops_f58_a_5    AC-OPS-F58.a-10 -> test_ac_ops_f58_a_10

Fail-before/pass-after: the FILE fails on stock v2026.8.31 because
test_ac_ops_f58_a_6 errors (runbook page absent). The other nine are
behaviour-contract conformance proofs of the adopted native stack and pass on
stock by design — the shape dispatch-plan.md:53 asks of a CONFIGURE row.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

RUNBOOK = Path("website/docs/user-guide/slang-coworkers-oncall-runbook.md")


def test_ac_ops_f58_a_1(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """hermes config migrate is table-driven and enforces the support floor.

    An in-range config is stamped to the current version; a config below
    SUPPORT_FLOOR_VERSION is left untouched.
    """
    from hermes_cli.config import DEFAULT_CONFIG, migrate_config, read_raw_config
    from hermes_cli.config_migrations import MIGRATIONS, SUPPORT_FLOOR_VERSION

    targets = [t for t, _fn in MIGRATIONS]
    assert targets == sorted(targets) and all(callable(fn) for _t, fn in MIGRATIONS)
    assert all(t >= SUPPORT_FLOOR_VERSION for t in targets)
    latest = int(DEFAULT_CONFIG["_config_version"])
    assert latest >= targets[-1] > SUPPORT_FLOOR_VERSION

    import yaml

    def _write_home(name: str, version: int) -> Path:
        home = tmp_path / name
        home.mkdir()
        (home / "config.yaml").write_text(
            yaml.safe_dump({"_config_version": version, "model": {"provider": "nous"}}),
            encoding="utf-8",
        )
        return home

    in_range = _write_home("in_range", SUPPORT_FLOOR_VERSION)
    monkeypatch.setenv("HERMES_HOME", str(in_range))
    migrate_config(interactive=False, quiet=True)
    migrated = read_raw_config()
    assert migrated["_config_version"] == latest
    assert migrated["model"]["provider"] == "nous"

    below = _write_home("below_floor", SUPPORT_FLOOR_VERSION - 1)
    below_before = (below / "config.yaml").read_bytes()
    monkeypatch.setenv("HERMES_HOME", str(below))
    migrate_config(interactive=False, quiet=True)
    assert (below / "config.yaml").read_bytes() == below_before


def test_ac_ops_f58_a_2(tmp_path: Path) -> None:
    """State-DB schema reconciliation self-heals a missing column on open.

    Dropping a reconcilable column and reopening re-adds it (declarative, no
    version-gated migration) while existing rows survive.
    """
    from hermes_state import SessionDB

    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    db.create_session("kept", source="test")
    db.close()

    table, column = "sessions", "handoff_error"
    with sqlite3.connect(db_path) as raw:
        assert column in _columns(raw, table)
        raw.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        assert column not in _columns(raw, table)

    SessionDB(db_path).close()

    with sqlite3.connect(db_path) as raw:
        assert column in _columns(raw, table)
        assert raw.execute("SELECT id FROM sessions WHERE id='kept'").fetchone() == ("kept",)


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def test_ac_ops_f58_a_3(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    """hermes uninstall --dry-run is non-destructive.

    Against a populated temp HERMES_HOME it removes nothing (never reaches the
    destructive worker) and prints a plan.
    """
    import hermes_cli.uninstall as uninstall

    home = tmp_path / "home"
    (home / "profiles" / "orchestrator").mkdir(parents=True)
    sentinels = [home / "config.yaml", home / "state.db",
                 home / "profiles" / "orchestrator" / "config.yaml"]
    for p in sentinels:
        p.write_text("sentinel", encoding="utf-8")

    monkeypatch.setattr(uninstall, "get_hermes_home", lambda: home)
    monkeypatch.setattr(uninstall, "get_project_root", lambda: tmp_path / "project")
    monkeypatch.setattr(uninstall, "_is_default_hermes_home", lambda *a, **k: True)
    monkeypatch.setattr(uninstall, "_discover_named_profiles", lambda *a, **k: [])

    def _must_not_run(*a, **k):
        raise AssertionError("dry-run must not perform the uninstall")

    monkeypatch.setattr(uninstall, "_perform_uninstall", _must_not_run)

    uninstall.run_uninstall(SimpleNamespace(dry_run=True, yes=True, full=True))

    out = capsys.readouterr().out.lower()
    assert "would" in out and "remove" in out
    for p in sentinels:
        assert p.exists()


def test_ac_ops_f58_a_4(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Rotating file logging honours config and the formatter redacts secrets.

    logging.max_size_mb / logging.backup_count from config.yaml size the
    agent.log RotatingFileHandler; RedactingFormatter scrubs a planted token.
    """
    import hermes_logging
    from agent.redact import RedactingFormatter
    from hermes_logging import RotatingFileHandler

    max_size_mb, backup_count = 7, 4
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(
        f"logging:\n  max_size_mb: {max_size_mb}\n  backup_count: {backup_count}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))

    try:
        # The suite has already initialized process-global logging; force a
        # complete setup pass while requesting this test's HERMES_HOME.
        log_dir = hermes_logging.setup_logging(hermes_home=home, force=True)
        # Assert on THIS home's agent.log handler specifically: other HERMES_HOMEs
        # legitimately keep their own agent.log handler (the conftest import binds
        # one; multi-home routing is intended), so a process-wide count is wrong.
        expected = Path(log_dir, "agent.log").resolve()
        agent_handlers = [
            h for h in hermes_logging.rotating_file_handlers()
            if isinstance(h, RotatingFileHandler)
            and Path(getattr(h, "baseFilename", "")).resolve() == expected
        ]
        assert len(agent_handlers) == 1
        assert agent_handlers[0].maxBytes == max_size_mb * 1024 * 1024
        assert agent_handlers[0].backupCount == backup_count
    finally:
        hermes_logging._reset_queued_handlers()

    secret = "ghp_0123456789abcdefABCDEF0123456789abcd"
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, f"token={secret}", None, None)
    assert secret not in RedactingFormatter("%(message)s").format(rec)


def test_ac_ops_f58_a_5(tmp_path: Path) -> None:
    """The fleet starts only the default multiplexer slot; coworkers stay down.

    container_boot autostarts the default (running) slot and registers each
    coworker slot — one left 'stopped', one with no gateway_state.json — without
    starting them.
    """
    from hermes_cli import container_boot

    (tmp_path / "gateway_state.json").write_text(
        json.dumps({"desired_state": "running"}), encoding="utf-8"
    )

    def _make_coworker(name: str, state: str | None) -> None:
        home = tmp_path / "profiles" / name
        home.mkdir(parents=True)
        (home / "SOUL.md").write_text("x", encoding="utf-8")
        if state is not None:
            (home / "gateway_state.json").write_text(
                json.dumps({"desired_state": state}), encoding="utf-8"
            )

    _make_coworker("worker-a", "stopped")
    _make_coworker("worker-b", None)
    scandir = tmp_path / "s6-scandir"
    scandir.mkdir()

    actions = container_boot.reconcile_profile_gateways(
        hermes_home=tmp_path, scandir=scandir, dry_run=True
    )
    assert {a.profile: a.action for a in actions} == {
        "default": "started",
        "worker-a": "registered",
        "worker-b": "registered",
    }


def test_ac_ops_f58_a_6() -> None:
    """The slang-coworkers on-call runbook exists and documents the ops procedures.

    Config-migration irreversibility (39->41, backup, SOUL.md re-render), the
    fleet-wide hermes update snapshot/cron restore, uninstall --dry-run and its
    --yes confirmation, doctor + debug share, the one-gateway multiplex model
    with coworker slots not autostarting, the supervised gateway-run child's
    EX_CONFIG refusal after gateway start, the gateway run --no-supervise --force
    override, and the MEM-F44 retention cross-reference.
    """
    assert RUNBOOK.exists(), f"runbook page missing: {RUNBOOK}"
    low = RUNBOOK.read_text(encoding="utf-8").lower()

    assert "config migrate" in low
    assert "39" in low and "41" in low and "irreversible" in low
    assert "soul.md" in low and "backup" in low
    assert "hermes update" in low
    assert "snapshot" in low and "cron" in low and "restore" in low
    assert "hermes doctor" in low and "debug share" in low
    assert "hermes uninstall --dry-run" in low and "--yes" in low
    assert ("multiplex_profiles" in low or "multiplex" in low) and "desired_state" in low
    assert "only the default" in low
    assert "hermes -p" in low and ("hard error" in low or "refus" in low)
    assert "hermes gateway run --no-supervise --force" in low
    assert "sessions.auto_prune" in low or "mem-f44" in low


def test_ac_ops_f58_a_7(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Fleet-wide config migration upgrades installed sibling profiles.

    A configured sibling behind the current version is migrated up on disk with
    user settings preserved; the active profile is left untouched.
    """
    import yaml

    import hermes_cli.profiles as profiles_mod
    import hermes_cli.update_cmd as update_cmd
    import hermes_constants
    from hermes_cli.config import DEFAULT_CONFIG
    from hermes_cli.config_migrations import SUPPORT_FLOOR_VERSION

    latest = int(DEFAULT_CONFIG["_config_version"])
    profiles_root = tmp_path / "profiles"

    def _write_profile(name: str) -> Path:
        home = profiles_root / name
        home.mkdir(parents=True)
        (home / "config.yaml").write_text(
            yaml.safe_dump(
                {"_config_version": SUPPORT_FLOOR_VERSION, "model": {"provider": "nous"}}
            ),
            encoding="utf-8",
        )
        return home

    # Both start behind, so "active skipped" is proven by the active file NOT moving.
    active = _write_profile("active")
    sibling = _write_profile("research")
    active_before = (active / "config.yaml").read_bytes()

    monkeypatch.setattr(profiles_mod, "_get_profiles_root", lambda: profiles_root)
    monkeypatch.setattr(hermes_constants, "get_process_hermes_home", lambda: active)
    monkeypatch.setattr(update_cmd, "_reload_config_modules", lambda: None)

    migrated = update_cmd._migrate_sibling_profile_configs()

    assert "research" in [m[0] for m in migrated] and "active" not in [m[0] for m in migrated]
    sib = yaml.safe_load((sibling / "config.yaml").read_text(encoding="utf-8"))
    assert sib["_config_version"] == latest and sib["model"]["provider"] == "nous"
    assert (active / "config.yaml").read_bytes() == active_before


def test_ac_ops_f58_a_8(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The fleet-wide update safety net restores lost cron jobs per profile.

    After snapshotting all profiles and emptying a coworker's cron/jobs.json,
    the restore pass re-hydrates that profile's jobs from its own snapshot and
    leaves healthy profiles untouched.
    """
    import hermes_cli.backup as backup

    def _mk_profile(home: Path, jobs: int) -> Path:
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("model: {}\n", encoding="utf-8")
        cron = home / "cron"
        cron.mkdir(exist_ok=True)
        cron.joinpath("jobs.json").write_text(
            json.dumps({"jobs": [{"id": f"j{i}"} for i in range(jobs)]}), encoding="utf-8"
        )
        return home

    default_home = _mk_profile(tmp_path / "home", jobs=3)
    work = _mk_profile(tmp_path / "home" / "profiles" / "work", jobs=5)
    healthy = _mk_profile(tmp_path / "home" / "profiles" / "research", jobs=2)
    monkeypatch.setattr("hermes_cli.profiles._get_default_hermes_home", lambda: default_home)
    monkeypatch.setattr(
        "hermes_cli.profiles._get_profiles_root", lambda: tmp_path / "home" / "profiles"
    )
    monkeypatch.setattr(
        "hermes_cli.profiles._PROFILE_ID_RE",
        re.compile(r"^[a-z0-9][a-z0-9_-]*$"),
        raising=False,
    )

    snaps = backup.create_pre_update_snapshots_all_profiles(invoking_home=default_home, keep=1)
    work_jobs = work / "cron" / "jobs.json"
    work_jobs.write_text(json.dumps({"jobs": []}), encoding="utf-8")

    restored = backup.restore_cron_jobs_all_profiles(snaps, invoking_home=default_home)

    assert [r["profile"] for r in restored] == ["work"]
    assert restored[0]["job_count"] == 5
    assert len(json.loads(work_jobs.read_text(encoding="utf-8"))["jobs"]) == 5
    assert len(json.loads((healthy / "cron" / "jobs.json").read_text(encoding="utf-8"))["jobs"]) == 2


def test_ac_ops_f58_a_9(monkeypatch: pytest.MonkeyPatch) -> None:
    """The double-bind guard refuses a second gateway for a multiplexed profile.

    _guard_named_profile_under_multiplexer (run in the supervised gateway-run
    child that `hermes -p <bot> gateway start` brings up) exits with the
    fatal-config code when the default gateway serves that profile; force=True
    bypasses it.
    """
    import hermes_cli.gateway as gateway

    monkeypatch.setattr(gateway, "named_profile_served_by_running_multiplexer", lambda: True)
    monkeypatch.setattr(gateway, "_profile_suffix", lambda: "worker-a")

    with pytest.raises(SystemExit) as excinfo:
        gateway._guard_named_profile_under_multiplexer(force=False)
    assert excinfo.value.code == gateway.GATEWAY_FATAL_CONFIG_EXIT_CODE

    assert gateway._guard_named_profile_under_multiplexer(force=True) is None


def test_ac_ops_f58_a_10(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """The triage commands are wired and `debug share --local` runs offline.

    doctor and debug share are installed CLI subcommands, and the --local
    debug-share path renders a report without any network upload.
    """
    import argparse

    import hermes_cli.debug as debug
    import hermes_cli.doctor as doctor
    from hermes_cli.debug import run_debug_share
    from hermes_cli.subcommands.debug import build_debug_parser
    from hermes_cli.subcommands.doctor import build_doctor_parser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    build_doctor_parser(sub, cmd_doctor=doctor.run_doctor)
    build_debug_parser(sub, cmd_debug=debug.run_debug)
    assert parser.parse_args(["doctor"]).func is doctor.run_doctor
    debug_args = parser.parse_args(["debug", "share", "--local"])
    assert debug_args.func is debug.run_debug and debug_args.local is True

    def _no_upload(*a, **k):
        raise AssertionError("--local must not upload")

    monkeypatch.setattr(debug, "upload_to_pastebin", _no_upload)
    run_debug_share(
        SimpleNamespace(local=True, lines=50, expire=7, nous=False, no_redact=False)
    )
    assert capsys.readouterr().out.strip()
