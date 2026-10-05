"""ISO-F17 — Host sweep & restart recovery: hermetic adopt-proof acceptance tests.

Every test proves that stock Hermes v2026.8.31 already implements ISO-F17's own
supervision surface, so each is green on the pinned tree. Non-vacuity lives in each
function's differential control (an assertion a plausible one-line regression flips);
the fail-on-base/pass-on-head negative control is the separate own-diff doc-contract
test tests/hermes_cli/test_iso_f17_doc_contract.py.
"""

from __future__ import annotations

import json
import os
import plistlib
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.restart import (
    GATEWAY_FATAL_CONFIG_EXIT_CODE,
    GATEWAY_SERVICE_RESTART_EXIT_CODE,
)


def _write_profile(
    home: Path, name: str, *, state: str | None = None, desired_state: str | None = None
) -> Path:
    prof = home / "profiles" / name
    prof.mkdir(parents=True, exist_ok=True)
    (prof / "SOUL.md").write_text("# fake profile\n", encoding="utf-8")
    payload: dict[str, object] = {"timestamp": 1234567890}
    if state is not None:
        payload["gateway_state"] = state
    if desired_state is not None:
        payload["desired_state"] = desired_state
    (prof / "gateway_state.json").write_text(json.dumps(payload), encoding="utf-8")
    return prof


def test_ac_iso_f17_1(tmp_path, monkeypatch):
    """One multiplex gateway serves default + valid allowlisted named profiles; a
    named profile absent from the allowlist is excluded."""
    from hermes_cli import profiles as profiles_mod
    from gateway import run as gateway_run

    home = tmp_path / "hermes"
    _write_profile(home, "bota")
    _write_profile(home, "botb")
    monkeypatch.setenv("HERMES_HOME", str(home))

    served = profiles_mod.profiles_to_serve(multiplex=True, profile_allowlist=["bota"])
    names = {name for name, _home in served}
    assert "default" in names
    assert "bota" in names
    # Differential: a named profile absent from the allowlist is excluded — a regression
    # that ignored the allowlist would sweep botb in and flip this.
    assert "botb" not in names

    # The gateway-config-to-roster wiring resolves the same set.
    cfg = SimpleNamespace(multiplex_profiles=True, multiplex_profile_allowlist=["bota"])
    homes = gateway_run._multiplex_profile_homes(cfg)
    wired = {name for name, _home in homes}
    assert "default" in wired and "bota" in wired and "botb" not in wired


def test_ac_iso_f17_2(tmp_path, monkeypatch):
    """container_boot reconciles per-profile s6 slots by desired state: only the
    default slot auto-starts under the multiplex env flag (a running coworker is
    registered DOWN); with it unset the same profile auto-starts; legacy transient
    states normalize to running while startup_failed does not."""
    from hermes_cli import container_boot

    monkeypatch.setattr(
        container_boot, "_read_container_argv", lambda: (), raising=False
    )

    # (a) desired_state reading: explicit verbatim; legacy transient states normalized.
    _write_profile(tmp_path / "h1", "draining_bot", state="draining")
    _write_profile(tmp_path / "h1", "degraded_bot", state="degraded")
    _write_profile(tmp_path / "h1", "failed_bot", desired_state="startup_failed")
    assert (
        container_boot._read_desired_state(
            tmp_path / "h1" / "profiles" / "draining_bot"
        )
        == "running"
    )
    assert (
        container_boot._read_desired_state(
            tmp_path / "h1" / "profiles" / "degraded_bot"
        )
        == "running"
    )
    # Differential: an explicit desired_state is returned verbatim (startup_failed is NOT
    # normalized to running and is excluded from the autostart states).
    assert (
        container_boot._read_desired_state(tmp_path / "h1" / "profiles" / "failed_bot")
        == "startup_failed"
    )

    def _reconcile(multiplex: bool):
        home = tmp_path / ("mux" if multiplex else "solo")
        scandir = tmp_path / ("scan_mux" if multiplex else "scan_solo")
        scandir.mkdir(parents=True)
        # The criterion names a coworker at desired_state="running" — exercise the
        # explicit desired_state path (not the legacy gateway_state fallback).
        _write_profile(home, "coder", desired_state="running")
        # A startup_failed coworker must never be autostart-eligible.
        _write_profile(home, "failed", desired_state="startup_failed")
        # default owns inbound: give the root/default a running prior state.
        (home / "gateway_state.json").write_text(
            json.dumps({"gateway_state": "running", "timestamp": 1234567890}),
            encoding="utf-8",
        )
        if multiplex:
            monkeypatch.setenv("GATEWAY_MULTIPLEX_PROFILES", "1")
        else:
            monkeypatch.delenv("GATEWAY_MULTIPLEX_PROFILES", raising=False)
        actions = container_boot.reconcile_profile_gateways(
            hermes_home=home, scandir=scandir, dry_run=False
        )
        return actions, scandir

    mux_actions, mux_scan = _reconcile(multiplex=True)
    coder = mux_scan / "gateway-coder"
    # Registered (run script written by _register_service, container_boot.py:488) AND held
    # down under the multiplexer — a skip-registration mutant writes no run file.
    assert (coder / "run").exists() and (coder / "down").exists(), (
        "coworker slot registered (run script) but DOWN under the multiplexer"
    )
    # Only the default slot owns inbound: its reconcile action is a start, not down.
    default_started = [
        a
        for a in mux_actions
        if getattr(a, "profile", None) == "default"
        and getattr(a, "action", None) == "started"
    ]
    assert default_started, (
        "the default slot auto-starts (owns inbound) under the multiplexer"
    )
    # Outside multiplex mode the running coworker starts. The `started` action alone is
    # decoupled from the fs write (container_boot.py:205 derives it from should_start
    # regardless of whether _register_service ran), so assert the POSITIVE artifact:
    # _register_service writes gateway-coder/run unconditionally (container_boot.py:488),
    # and its presence proves the slot was actually registered — a skip-registration mutant
    # leaves no run file. down is absent only in the started (not held-down) case.
    _solo_actions, solo_scan = _reconcile(multiplex=False)
    coder_started = [
        a
        for a in _solo_actions
        if getattr(a, "profile", None) == "coder"
        and getattr(a, "action", None) == "started"
    ]
    assert coder_started, "solo mode auto-starts the running coworker"
    assert (solo_scan / "gateway-coder" / "run").exists(), (
        "the started coworker's s6 run script is written (slot actually registered)"
    )
    assert not (solo_scan / "gateway-coder" / "down").exists()
    # A startup_failed coworker stays registered-but-down even in solo mode, with no
    # `started` action (it is not in _AUTOSTART_STATES).
    assert (solo_scan / "gateway-failed" / "down").exists()
    assert not [
        a
        for a in _solo_actions
        if getattr(a, "profile", None) == "failed"
        and getattr(a, "action", None) == "started"
    ]


@pytest.mark.asyncio
async def test_ac_iso_f17_3(tmp_path, monkeypatch):
    """The restart/stop exit-code contract holds at the producer and consumer seams: the
    runner produces 75 on a via-service drain and 78 on fatal config; the systemd unit
    force-restarts on 75 and prevents restart on 78; the s6 finish maps 78->125; launchd
    relaunches."""
    import gateway.status as gw_status
    from hermes_cli import gateway as gw
    from hermes_cli.service_manager import S6ServiceManager
    from tests.gateway.restart_test_helpers import make_restart_runner

    # Producer (75): a via-service drain stamps the restart exit code.
    runner, adapter = make_restart_runner()
    adapter.disconnect = AsyncMock()
    runner._restart_requested = True
    runner._restart_via_service = True
    monkeypatch.setattr(
        gw_status, "remove_pid_file", lambda *a, **k: None, raising=False
    )
    monkeypatch.setattr(
        gw_status, "write_runtime_status", lambda *a, **k: None, raising=False
    )
    await runner.stop()
    assert runner._exit_code == GATEWAY_SERVICE_RESTART_EXIT_CODE

    # Producer (78): a non-retryable fatal startup error stamps the fatal-config code.
    from gateway.config import GatewayConfig, PlatformConfig, Platform
    from gateway.run import GatewayRunner
    from tests.gateway.test_runner_startup_failures import _NonRetryableFailureAdapter

    config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="x")},
        sessions_dir=tmp_path / "sessions",
    )
    fatal_runner = GatewayRunner(config)
    monkeypatch.setattr(
        fatal_runner,
        "_create_adapter",
        lambda platform, platform_config: _NonRetryableFailureAdapter(),
    )
    await fatal_runner.start()
    # Differential: a non-retryable startup error yields the FATAL code, not the restart code.
    assert fatal_runner.exit_code == GATEWAY_FATAL_CONFIG_EXIT_CODE
    assert fatal_runner.exit_code != GATEWAY_SERVICE_RESTART_EXIT_CODE

    # Consumer: the systemd unit force-restarts on the restart code, prevents on fatal;
    # s6 finish maps 78->125 (permanent stop); launchd relaunches unconditionally.
    unit = gw.generate_systemd_unit(system=False)
    assert "Restart=always" in unit
    assert f"RestartForceExitStatus={GATEWAY_SERVICE_RESTART_EXIT_CODE}" in unit
    assert f"RestartPreventExitStatus={GATEWAY_FATAL_CONFIG_EXIT_CODE}" in unit
    assert f"RestartForceExitStatus={GATEWAY_FATAL_CONFIG_EXIT_CODE}" not in unit
    assert f"RestartPreventExitStatus={GATEWAY_SERVICE_RESTART_EXIT_CODE}" not in unit
    finish = S6ServiceManager._render_finish_script()
    # 125 is s6's permanent-down code: a fatal-config (78) child is not respawned.
    assert (
        f'if [ "$1" = "{GATEWAY_FATAL_CONFIG_EXIT_CODE}" ]; then\n  exit 125\nfi'
        in finish
    )
    # KeepAlive must be boolean true (unconditional relaunch), not a conditions dict.
    launchd = plistlib.loads(gw.generate_launchd_plist().encode("utf-8"))
    assert launchd["KeepAlive"] is True
    # 75/78 are the external OS-supervisor protocol contract, not a version snapshot.
    assert GATEWAY_SERVICE_RESTART_EXIT_CODE == 75
    assert GATEWAY_FATAL_CONFIG_EXIT_CODE == 78
    # A clean stop (exit 0) is also a permanent down; every other non-zero status
    # restarts (the trailing `exit 0` fallthrough).
    assert 'if [ "$1" = "0" ]; then\n  exit 125\nfi' in finish
    assert finish.rstrip().endswith("exit 0")


def test_ac_iso_f17_4(monkeypatch):
    """The systemd watchdog is opt-in and its heartbeat is liveness-gated: notify+
    WatchdogSec only when configured; WATCHDOG=1 only on a timely loop tick."""
    from hermes_cli import gateway as gw
    from gateway.config import GatewayConfig
    from gateway import systemd_notify

    monkeypatch.setattr(
        gw,
        "load_gateway_config",
        lambda: GatewayConfig.from_dict({"systemd_watchdog_seconds": 120}),
        raising=False,
    )
    on = gw.generate_systemd_unit(system=False)
    assert (
        "Type=notify" in on and "NotifyAccess=main" in on and "WatchdogSec=120s" in on
    )
    # Differential: with the watchdog off the unit is Type=simple with no WatchdogSec.
    monkeypatch.setattr(
        gw,
        "load_gateway_config",
        lambda: GatewayConfig.from_dict({"systemd_watchdog_seconds": 0}),
        raising=False,
    )
    off = gw.generate_systemd_unit(system=False)
    assert "Type=simple" in off and "WatchdogSec" not in off

    # Runtime heartbeat gating: a timely tick sends WATCHDOG=1; a late tick does not.
    sent: list[str] = []
    monkeypatch.setattr(systemd_notify, "notify", lambda msg: sent.append(msg) or True)
    monkeypatch.setenv("NOTIFY_SOCKET", "@iso-f17")
    monkeypatch.setenv("WATCHDOG_USEC", "20000")
    wd = systemd_notify.SystemdWatchdog(config_enabled=True, lag_tolerance_seconds=0.0)
    assert wd.record_tick(scheduled_at=100.0, now=100.0) is True
    assert "WATCHDOG=1" in sent
    sent.clear()
    assert wd.record_tick(scheduled_at=100.0, now=110.0) is False
    # Differential: a late/lagging tick withholds the heartbeat and reports unhealthy instead
    # (the frozen-loop signal to systemd), never WATCHDOG=1.
    assert "WATCHDOG=1" not in sent
    assert any("STATUS=" in m for m in sent)


def test_ac_iso_f17_5(monkeypatch):
    """The out-of-loop loop-liveness watchdog distinguishes a live loop from a frozen
    one: a frozen loop hard-exits with the restart exit code (75); a live loop does not."""
    import asyncio
    import threading
    from gateway import shutdown_watchdog

    captured: list[int] = []
    monkeypatch.setattr(
        shutdown_watchdog.os, "_exit", lambda code: captured.append(code)
    )

    def _stop(handle):
        for name in ("stop", "cancel"):
            fn = getattr(handle, name, None)
            if callable(fn):
                fn()
        joiner = getattr(handle, "join", None)
        if callable(joiner):
            joiner(timeout=2.0)

    # Frozen loop: a loop object that never dispatches its scheduled probe -> after the
    # strike budget the thread watchdog hard-exits with the restart exit code.
    frozen_loop = (
        asyncio.new_event_loop()
    )  # never run() -> call_soon_threadsafe never fires
    frozen_handle = shutdown_watchdog.start_loop_liveness_watchdog(
        frozen_loop, probe_interval=0.01, probe_timeout=0.01, max_strikes=1
    )
    try:
        deadline = time.monotonic() + 3.0
        while not captured and time.monotonic() < deadline:
            time.sleep(0.02)
        assert captured and captured[0] == GATEWAY_SERVICE_RESTART_EXIT_CODE
    finally:
        _stop(frozen_handle)
        frozen_loop.close()

    # Differential: a LIVE loop that answers its probe must NOT hard-exit.
    captured.clear()
    live_loop = asyncio.new_event_loop()
    spinner = threading.Thread(target=live_loop.run_forever, daemon=True)
    spinner.start()
    live_handle = shutdown_watchdog.start_loop_liveness_watchdog(
        live_loop, probe_interval=0.01, probe_timeout=0.05, max_strikes=2
    )
    try:
        # The strike window here is ~max_strikes*(interval+timeout) ≈ 0.12s; a loop that
        # answers the probe must not hard-exit within this ≥2s wait.
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline and not captured:
            time.sleep(0.02)
        assert captured == []
    finally:
        _stop(live_handle)
        live_loop.call_soon_threadsafe(live_loop.stop)
        spinner.join(timeout=2.0)
        live_loop.close()


@pytest.mark.asyncio
async def test_ac_iso_f17_6(tmp_path, monkeypatch):
    """resume_pending re-arms interrupted sessions at startup: exactly one auto-resume
    for a fresh authorized interrupted session; a clean session and an unauthorized
    session are skipped; reason taxonomy is restart_interrupted / restart_timeout /
    shutdown_timeout."""
    import asyncio
    from gateway.config import Platform
    from gateway.session import SessionEntry
    from tests.gateway.restart_test_helpers import (
        make_restart_runner,
        make_restart_source,
    )

    runner, adapter = make_restart_runner()
    now = datetime.now()
    interrupted = SessionEntry(
        session_key="agent:main:telegram:dm:tg-chat",
        session_id="sid-tg",
        created_at=now,
        updated_at=now,
        origin=make_restart_source(chat_id="tg-chat"),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        resume_pending=True,
        resume_reason="restart_interrupted",
        last_resume_marked_at=now,
    )
    clean = SessionEntry(
        session_key="agent:main:telegram:dm:clean",
        session_id="sid-clean",
        created_at=now,
        updated_at=now,
        origin=make_restart_source(chat_id="clean"),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        resume_pending=False,
    )
    runner.session_store._entries = {
        interrupted.session_key: interrupted,
        clean.session_key: clean,
    }
    adapter.handle_message = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}

    scheduled = runner._schedule_resume_pending_sessions(platform=Platform.TELEGRAM)
    await asyncio.sleep(0)
    assert scheduled == 1, (
        "exactly one auto-resume for the interrupted authorized session"
    )
    adapter.handle_message.assert_awaited_once()
    assert "restart_interrupted" in runner._AUTO_RESUME_REASONS
    assert {"restart_timeout", "shutdown_timeout"} <= set(runner._AUTO_RESUME_REASONS)

    # Skip cases (differentials): none of these should re-arm. The restart-loop breaker
    # (gateway/run.py:13113, gateway.restart_loop_guard) records a boot for EVERY call that
    # has candidates and trips at the 3rd, short-circuiting _schedule_resume_pending_sessions
    # with `return 0` BEFORE the per-entry _is_session_running/auth checks (:13134/:13154) —
    # which would make the differentials below (esp. the unauthorized case) pass on the
    # breaker rather than on the property under test. Neutralize it so each call reaches the
    # candidate loop. Reset the claimed slot + pending flag so only the property differs.
    import gateway.restart_loop_guard as _rlg

    monkeypatch.setattr(_rlg, "check_and_record", lambda *a, **k: False)
    from gateway.run import _AGENT_PENDING_SENTINEL

    # already-running: a claimed slot is not re-armed.
    runner._running_agents.clear()
    interrupted.resume_pending = True
    runner._running_agents[interrupted.session_key] = _AGENT_PENDING_SENTINEL
    assert runner._schedule_resume_pending_sessions(platform=Platform.TELEGRAM) == 0
    # suspended: excluded from candidates.
    runner._running_agents.clear()
    interrupted.resume_pending = True
    interrupted.suspended = True
    assert runner._schedule_resume_pending_sessions(platform=Platform.TELEGRAM) == 0
    interrupted.suspended = False
    # reason outside _AUTO_RESUME_REASONS: excluded.
    interrupted.resume_pending = True
    interrupted.resume_reason = "user_paused"
    assert runner._schedule_resume_pending_sessions(platform=Platform.TELEGRAM) == 0
    interrupted.resume_reason = "restart_interrupted"
    # unauthorized owner: excluded BY the auth gate (:13154), not by the breaker. Assert the
    # auth check actually fires — a mutant that drops the _is_user_authorized filter would
    # leave auth_calls empty and schedule the session (count 1), so this is non-vacuous.
    runner._running_agents.clear()
    interrupted.resume_pending = True
    auth_calls = []

    def _deny_auth(source):
        auth_calls.append(source)
        return False

    runner._is_user_authorized = _deny_auth
    assert runner._schedule_resume_pending_sessions(platform=Platform.TELEGRAM) == 0
    assert auth_calls, (
        "the unauthorized session must be excluded by the auth check, which must run"
    )

    # Exercise the production start() wiring, not only the gating helper above: a real
    # GatewayRunner.start() invokes the resume re-arm at startup (gateway/run.py:14280).
    # The stub adapter's connect() returns True, so _failed_platforms stays empty and the
    # reconnect-path resume call (gateway/run.py:15768) never fires — the recorder counts
    # only the startup call.
    from gateway import run as gateway_run
    from gateway.config import GatewayConfig, PlatformConfig
    from gateway.platforms.base import BasePlatformAdapter
    from gateway.run import GatewayRunner

    class _StubAdapter(BasePlatformAdapter):
        def __init__(self, platform):
            super().__init__(PlatformConfig(enabled=True, token="***"), platform)

        async def connect(self, *, is_reconnect: bool = False):
            return True

        async def disconnect(self):
            self._mark_disconnected()

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            raise NotImplementedError

        async def get_chat_info(self, chat_id):
            return {"id": chat_id}

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "gw_home"))
    # The embedded kanban dispatcher opens a process-lifetime .dispatcher.lock file; its
    # documented escape hatch disables it before the lock is opened, keeping the run hermetic.
    monkeypatch.setenv("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "0")
    cfg = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")},
        sessions_dir=tmp_path / "gw_sessions",
        # Disable the selector-floor timer + out-of-loop liveness watchdog thread so
        # start() spawns no OS-thread guard to leak.
        loop_watchdog=False,
    )
    start_runner = GatewayRunner(cfg)
    monkeypatch.setattr(start_runner, "_create_adapter", lambda p, pc: _StubAdapter(p))
    # Keep unrelated secondary-profile and hosted-room startup inert (both are awaited → AsyncMock).
    monkeypatch.setattr(
        start_runner, "_start_secondary_profile_adapters", AsyncMock(return_value=0)
    )
    monkeypatch.setattr(start_runner, "_ensure_hosted_room_worker", AsyncMock())
    # Suppress the SIGUSR2 faulthandler registration (gateway/run.py:13445) so the run
    # opens no process-lifetime gateway_faulthandler.log.
    monkeypatch.setattr(gateway_run.signal, "SIGUSR2", None, raising=False)
    resume_calls = {"n": 0}
    monkeypatch.setattr(
        start_runner,
        "_schedule_resume_pending_sessions",
        lambda *a, **k: resume_calls.__setitem__("n", resume_calls["n"] + 1) or 0,
    )
    try:
        assert await asyncio.wait_for(start_runner.start(), timeout=30) is True
        assert resume_calls["n"] >= 1, (
            "start() must re-arm resume-pending sessions at startup"
        )
    finally:
        # stop() disarms the heartbeat/liveness guards and sweep-cancels every background
        # task; the os._exit shutdown watchdog is skipped under pytest.
        await asyncio.wait_for(start_runner.stop(), timeout=30)


def test_ac_iso_f17_7(tmp_path, monkeypatch):
    """A durable turn lease held by a dead PID is reclaimed while a live-PID lease is
    retained."""
    import hermes_state
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "state.db")
    db.create_session("shared", source="test")
    dead_holder = "pid=424242:turn=dead:platform=test"
    assert (
        db.try_acquire_session_turn_lease("shared", dead_holder, ttl_seconds=300)
        is True
    )

    probed: list[int] = []

    def _pid_exists(pid: int) -> bool:
        probed.append(pid)
        return False

    monkeypatch.setattr(hermes_state, "psutil", SimpleNamespace(pid_exists=_pid_exists))
    fresh_holder = "pid=525252:turn=fresh:platform=test"
    assert (
        db.try_acquire_session_turn_lease("shared", fresh_holder, ttl_seconds=300)
        is True
    )
    assert probed == [424242]

    # Differential: a lease held by a live owner is retained (owner-fenced) against another caller.
    db.create_session("live-sess", source="test")
    live_holder = f"pid={os.getpid()}:turn=live:platform=test"
    assert (
        db.try_acquire_session_turn_lease("live-sess", live_holder, ttl_seconds=300)
        is True
    )
    other = f"pid={os.getpid()}:turn=other:platform=test"
    assert (
        db.try_acquire_session_turn_lease("live-sess", other, ttl_seconds=300) is False
    )

    # A lease held by a live foreign PID (the psutil.pid_exists path, not the self-pid
    # shortcut) is retained against a contender.
    db.create_session("nonself-sess", source="test")
    live_pid = 777001
    monkeypatch.setattr(
        hermes_state, "psutil", SimpleNamespace(pid_exists=lambda pid: pid == live_pid)
    )
    nonself_holder = f"pid={live_pid}:turn=live:platform=test"
    assert (
        db.try_acquire_session_turn_lease("nonself-sess", nonself_holder, ttl_seconds=300)
        is True
    )
    contender = "pid=777002:turn=other:platform=test"
    assert (
        db.try_acquire_session_turn_lease("nonself-sess", contender, ttl_seconds=300)
        is False
    )


@pytest.fixture
def _kanban_conn(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.discard(str(kb.kanban_db_path(board="default").resolve()))
    kb.init_db()
    with kb.connect() as conn:
        yield kb, conn


def test_ac_iso_f17_8(_kanban_conn, monkeypatch):
    """The kanban dispatcher reclaims a dead-worker claim but extends a live one with a
    fresh heartbeat."""
    kb, conn = _kanban_conn
    host = kb._claimer_id().split(":", 1)[0]
    now = int(time.time())

    # (a) dead worker, expired claim -> reclaimed.
    dead = kb.create_task(conn, title="dead", assignee="a")
    kb.claim_task(conn, dead, claimer=f"{host}:w1")
    kb._set_worker_pid(conn, dead, 4242421)
    conn.execute(
        "UPDATE tasks SET claim_expires=?, last_heartbeat_at=? WHERE id=?",
        (now - 3600, now - 1800, dead),
    )
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    kb.release_stale_claims(conn, signal_fn=lambda _p, _s: None)
    reclaimed = conn.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND kind='reclaimed'", (dead,)
    ).fetchone()
    assert reclaimed is not None

    # (b) live worker, expired claim but FRESH heartbeat -> extended, NOT reclaimed (differential).
    live = kb.create_task(conn, title="live", assignee="a")
    kb.claim_task(conn, live, claimer=f"{host}:w2")
    kb._set_worker_pid(conn, live, 4242422)
    conn.execute(
        "UPDATE tasks SET claim_expires=?, last_heartbeat_at=? WHERE id=?",
        (now - 60, now - 1, live),
    )
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)
    kb.release_stale_claims(conn, signal_fn=lambda _p, _s: None)
    extended = conn.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND kind='claim_extended'", (live,)
    ).fetchone()
    reclaimed_live = conn.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND kind='reclaimed'", (live,)
    ).fetchone()
    assert extended is not None and reclaimed_live is None

    # (c) heartbeat older than the max-stale window with a LIVE pid: the claim is NOT
    # extended — a stale heartbeat leaves the extend path for the reclaim/deferral path
    # (a live worker's reclaim is deferred with a grace). Differential from (b): fresh
    # heartbeat -> extended; stale heartbeat -> not extended.
    stale = kb.create_task(conn, title="stale", assignee="a")
    kb.claim_task(conn, stale, claimer=f"{host}:w3")
    kb._set_worker_pid(conn, stale, 4242423)
    conn.execute(
        "UPDATE tasks SET claim_expires=?, last_heartbeat_at=? WHERE id=?",
        (now - 60, now - (2 * 60 * 60), stale),
    )
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)
    kb.release_stale_claims(conn, signal_fn=lambda _p, _s: None)
    stale_events = {
        row[0]
        for row in conn.execute(
            "SELECT kind FROM task_events WHERE task_id=?", (stale,)
        ).fetchall()
    }
    stale_status = conn.execute(
        "SELECT status FROM tasks WHERE id=?", (stale,)
    ).fetchone()[0]
    # A still-live worker's reclaim is deferred (reclaim_deferred), never an immediate
    # reclaim, and the claim is retained (status stays running). Asserting the absence
    # of `reclaimed` is the real differential — a regression to immediate reclaim would
    # pass the not-extended check alone.
    assert "claim_extended" not in stale_events
    assert "reclaimed" not in stale_events
    assert "reclaim_deferred" in stale_events
    assert stale_status == "running"

    # Seed independent stale-claim and crashed-worker rows so the dispatch tick reports
    # concrete reclaim/crash values (an int count and a task-id list), not just presence.
    rec2 = kb.create_task(conn, title="rec2", assignee="a")
    kb.claim_task(conn, rec2, claimer=f"{host}:d1")
    kb._set_worker_pid(conn, rec2, 4242431)
    conn.execute(
        "UPDATE tasks SET claim_expires=?, last_heartbeat_at=? WHERE id=?",
        (now - 3600, now - 1800, rec2),
    )
    cr2 = kb.create_task(conn, title="cr2", assignee="a")
    kb.claim_task(conn, cr2, claimer=f"{host}:d2")
    kb._set_worker_pid(conn, cr2, 4242432)
    conn.execute(
        "UPDATE tasks SET claim_expires=?, started_at=? WHERE id=?",
        (now + 3600, now - 100, cr2),
    )
    kb._record_worker_exit(4242432, 1 << 8)  # WIFEXITED, WEXITSTATUS=1 -> nonzero exit
    conn.commit()
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    result = kb.dispatch_once(conn, dry_run=True)
    assert result.reclaimed >= 1
    assert cr2 in result.crashed


def test_ac_iso_f17_9(_kanban_conn, monkeypatch):
    """The dispatcher's respawn guards defer re-dispatch: a recent rate-limited run
    holds under cooldown; an auth/quota failure yields blocker_auth; a clean task is
    respawnable. (The rate-limit sentinel exit code is EX_TEMPFAIL=75.)"""
    kb, conn = _kanban_conn
    assert kb.KANBAN_RATE_LIMIT_EXIT_CODE == 75

    now = int(time.time())
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", "300")

    # Rate-limit cooldown: a task with a recent rate_limited run is guarded.
    rl = kb.create_task(conn, title="rl", assignee="a")
    conn.execute(
        "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) "
        "VALUES (?, 'rate_limited', 'rate_limited', ?, ?)",
        (rl, now - 10, now - 5),
    )
    conn.commit()
    monkeypatch.setattr(kb.time, "time", lambda: now + 100)
    assert kb.check_respawn_guard(conn, rl) == "rate_limit_cooldown"
    # Differential: after the cooldown window elapses the guard clears.
    monkeypatch.setattr(kb.time, "time", lambda: now + 400)
    assert kb.check_respawn_guard(conn, rl) is None

    # Auth blocker: a quota/auth last_failure_error yields blocker_auth.
    au = kb.create_task(conn, title="auth", assignee="a")
    conn.execute(
        "UPDATE tasks SET last_failure_error=? WHERE id=?",
        ("401 unauthorized: invalid api key", au),
    )
    conn.commit()
    assert kb.check_respawn_guard(conn, au) == "blocker_auth"

    # A quota-only failure (no 429/rate/auth token) isolates the `quota` alternative of
    # _RESPAWN_BLOCKER_RE (kanban_db.py:7997) — dropping `quota|` makes this stop matching.
    quota = kb.create_task(conn, title="quota", assignee="a")
    conn.execute(
        "UPDATE tasks SET last_failure_error=? WHERE id=?",
        ("insufficient_quota: exceeded your current quota", quota),
    )
    conn.commit()
    assert kb.check_respawn_guard(conn, quota) == "blocker_auth"

    # Differential: a clean task is respawnable (no guard).
    clean = kb.create_task(conn, title="clean", assignee="a")
    assert kb.check_respawn_guard(conn, clean) is None

    # Protocol-violation guard: a worker exits rc=0 with the task still running -> a
    # bounded streak. Below the limit the task stays respawnable; at the limit it trips.
    host = kb._claimer_id().split(":", 1)[0]
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(kb, "_PROTOCOL_VIOLATION_FAILURE_LIMIT", 2, raising=False)

    def _one_violation(task_id: str, pid: int) -> None:
        kb.claim_task(conn, task_id, claimer=f"{host}:pv")
        kb._set_worker_pid(conn, task_id, pid)
        # raw POSIX wait status for a clean exit code 0 (os.WIFEXITED -> True, WEXITSTATUS -> 0)
        kb._record_worker_exit(pid, 0)
        kb.detect_crashed_workers(conn)

    pv = kb.create_task(conn, title="pv", assignee="a")
    _one_violation(pv, 5555551)
    below = conn.execute("SELECT status FROM tasks WHERE id=?", (pv,)).fetchone()[0]
    assert below == "ready"
    _one_violation(pv, 5555552)
    at_limit = conn.execute("SELECT status FROM tasks WHERE id=?", (pv,)).fetchone()[0]
    assert at_limit == "blocked"


def test_ac_iso_f17_10(monkeypatch):
    """Sandbox orphans are reaped by the terminal Docker backend, once per process, and
    only exited+labelled+aged containers are selected — never a running one."""
    from tools.environments import docker as docker_env

    from datetime import timedelta, timezone

    old_iso = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime(
        "%Y-%m-%dT%H:%M:%S.000000000Z"
    )
    young_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    ps_argv: list[str] = []
    rm_ids: list[str] = []

    def _run(cmd, *a, **k):
        argv = list(cmd)
        out = ""
        if "ps" in argv:
            ps_argv.extend(argv)
            out = "cid-old\ncid-young"
        elif "inspect" in argv:
            out = old_iso if argv[-1] == "cid-old" else young_iso
        elif "rm" in argv:
            rm_ids.append(argv[-1])

        class _R:
            returncode = 0
            stdout = out
            stderr = ""

        return _R()

    monkeypatch.setattr(docker_env.subprocess, "run", _run)
    removed = docker_env.reap_orphan_containers(
        max_age_seconds=600, profile_filter="bota", docker_exe="/usr/bin/docker"
    )
    flat = " ".join(ps_argv)
    assert "status=exited" in flat
    assert "label=hermes-agent=1" in flat
    assert "hermes-profile=bota" in flat
    # Differential (principal safety property): the reaper never lists running containers.
    assert "status=running" not in flat
    # Differential: only the aged orphan is removed; the young one (within max_age) is spared.
    assert rm_ids == ["cid-old"] and removed == 1

    # Once-per-process guard + config gate (owner: terminal_tool).
    import tools.terminal_tool as terminal_tool

    calls = {"n": 0}
    monkeypatch.setattr(
        terminal_tool, "_docker_orphan_reaper_ran", False, raising=False
    )
    monkeypatch.setattr(
        "tools.environments.docker.reap_orphan_containers",
        lambda **kw: calls.__setitem__("n", calls["n"] + 1) or 0,
    )
    for _ in range(3):
        terminal_tool._maybe_reap_docker_orphans({"docker_orphan_reaper": True})
    assert calls["n"] == 1
    # Differential: with the config gate off, the sweep does not run.
    monkeypatch.setattr(
        terminal_tool, "_docker_orphan_reaper_ran", False, raising=False
    )
    calls["n"] = 0
    terminal_tool._maybe_reap_docker_orphans({"docker_orphan_reaper": False})
    assert calls["n"] == 0


def test_ac_iso_f17_11(monkeypatch):
    """A named-profile gateway refuses to run while a multiplexer already serves it: the
    guard raises SystemExit(78); under --force or with no multiplexer it does not."""
    from hermes_cli import gateway as gw

    monkeypatch.setattr(
        gw,
        "named_profile_served_by_running_multiplexer",
        lambda *a, **k: True,
        raising=False,
    )
    with pytest.raises(SystemExit) as exc:
        gw._guard_named_profile_under_multiplexer(force=False)
    assert exc.value.code == GATEWAY_FATAL_CONFIG_EXIT_CODE

    # Differential: --force bypasses the guard, and no running multiplexer means no refusal.
    gw._guard_named_profile_under_multiplexer(force=True)
    monkeypatch.setattr(
        gw,
        "named_profile_served_by_running_multiplexer",
        lambda *a, **k: False,
        raising=False,
    )
    gw._guard_named_profile_under_multiplexer(force=False)
