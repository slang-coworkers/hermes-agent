"""ISO-F12 acceptance — on_wake / race-free restart / paused kill switch.

Hermetic behaviour contracts for the native Hermes lifecycle-control surface at
tag v2026.8.31: the profile-aware ESTOP kill switch (engage/resume, per-bot and
fleet-wide, notice-not-queue); the race-free restart path (GatewayRunner.request_restart
drains active work then exits with EX_TEMPFAIL) with resume_pending re-arming
interrupted sessions; and the on_wake mapping — native kanban worker recovery
(dispatch_once reclaims a stale card, _default_spawn respawns the same task) — with
deliver_wake the separate creator-session wake path whose internal turn is exempt
from the pause gate.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent import estop
from hermes_cli import kanban_db as kb


DOC_PATH = "website/docs/user-guide/slang-coworkers-pause-restart-wake.md"


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    estop._reset_log_state_for_tests()
    return tmp_path


class _FakeSource:
    platform = None
    chat_id = "c1"
    user_id = "u1"
    user_name = "user"
    chat_type = "dm"
    profile = None


class _FakeEvent:
    internal = False
    text = "hello"

    def __init__(self):
        self.source = _FakeSource()


@pytest.fixture
def kanban_conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "profiles" / "w").mkdir(parents=True)  # dispatch_once skips unknown profiles
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    with kb.connect() as conn:
        yield conn


def test_ac_iso_f12_1(hermes_home):
    """Kill switch engages and resumes with no restart; reason round-trips."""
    assert estop.is_engaged() is False
    assert estop.paused_reply() is None

    estop.engage(reason="ops incident")
    assert (hermes_home / "ESTOP").exists()
    assert estop.is_engaged() is True

    state = estop.get_state()
    assert state is not None
    assert state["reason"] == "ops incident"
    assert state["engaged_at"]

    notice = estop.paused_reply()
    assert notice is not None
    assert "paused" in notice.lower()
    assert "ops incident" in notice

    assert estop.disengage() is True
    assert not (hermes_home / "ESTOP").exists()
    assert estop.is_engaged() is False


def test_ac_iso_f12_2(tmp_path, monkeypatch):
    """`hermes -p <bot> pause` pauses one bot; the fleet-root sentinel pauses all."""
    from hermes_cli.subcommands.pause import cmd_pause

    root = tmp_path / "hermes-root"
    bot_a = root / "profiles" / "bot-a"
    bot_b = root / "profiles" / "bot-b"
    bot_a.mkdir(parents=True)
    bot_b.mkdir(parents=True)

    monkeypatch.setenv("HERMES_HOME", str(bot_a))
    estop._reset_log_state_for_tests()
    assert cmd_pause(argparse.Namespace(reason="per-bot")) == 0
    assert (bot_a / "ESTOP").exists()
    assert estop.is_engaged() is True

    monkeypatch.setenv("HERMES_HOME", str(bot_b))
    estop._reset_log_state_for_tests()
    assert estop.is_engaged() is False

    (root / "ESTOP").write_text('{"reason": "fleet stop"}\n', encoding="utf-8")
    assert estop.is_engaged() is True


def test_ac_iso_f12_3(hermes_home, monkeypatch):
    """The kill switch halts cron ticks and kanban spawns, and resume restores them."""
    from cron import scheduler
    from gateway.kanban_watchers import _kanban_dispatch_allowed

    calls = []

    def _fake_get_due_jobs():
        calls.append(1)
        return []

    monkeypatch.setattr(scheduler, "get_due_jobs", _fake_get_due_jobs)

    assert _kanban_dispatch_allowed() is True

    estop.engage(reason="test")
    assert scheduler.tick(verbose=False) == 0
    assert calls == []
    assert _kanban_dispatch_allowed() is False

    estop.disengage()
    scheduler.tick(verbose=False)
    assert calls == [1]
    assert _kanban_dispatch_allowed() is True


@pytest.mark.asyncio
async def test_ac_iso_f12_4(hermes_home):
    """The documented difference: a paused inbound is answered with the notice, not queued."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._is_user_authorized = lambda source: True
    estop.engage(reason="maintenance")
    # A notice string returned in place of enqueueing is the drop-not-queue contract.
    reply = await runner._handle_message(_FakeEvent())
    assert reply is not None
    assert "paused" in reply.lower()
    assert "maintenance" in reply


@pytest.mark.asyncio
async def test_ac_iso_f12_5(hermes_home):
    """Wake and in-flight work are exempt: deliver_wake's internal turn is not dropped by the kill switch."""
    from gateway.config import Platform
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource
    from gateway.wake import adapter_supports_push, deliver_wake

    class _RecordingAdapter:  # no supports_async_delivery attr -> push-capable
        def __init__(self):
            self.handled = []

        async def handle_message(self, event):
            self.handled.append(event)

    adapter = _RecordingAdapter()
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat-1", chat_type="group")
    await deliver_wake(adapter, text="resume task", source=source)
    assert len(adapter.handled) == 1
    wake_event = adapter.handled[0]
    assert wake_event.internal is True
    assert wake_event.text == "resume task"

    # The estop gate (`if not is_internal:`) is bypassed for an internal turn,
    # so `_session_key_for_source` (the first self-call AFTER the gate) is
    # reached; a paused-and-dropped turn would `return _paused_notice` before
    # it. Raising a sentinel there is the differential: reaching it proves the
    # gate did not drop the wake.
    class _PastEstopGate(RuntimeError):
        pass

    runner = object.__new__(GatewayRunner)

    def _probe_after_estop_gate(_source):
        raise _PastEstopGate

    runner._session_key_for_source = _probe_after_estop_gate
    estop.engage()
    with pytest.raises(_PastEstopGate):
        await runner._handle_message(wake_event)

    class _NoPushAdapter:
        supports_async_delivery = False

    assert adapter_supports_push(adapter) is True
    assert adapter_supports_push(_NoPushAdapter()) is False


@pytest.mark.asyncio
async def test_ac_iso_f12_6(monkeypatch):
    """Race-free restart: stop() drains before exit and the service-restart path sets EX_TEMPFAIL."""
    from gateway.restart import (
        EXTERNAL_GATEWAY_SUPERVISOR_ENV,
        GATEWAY_SERVICE_RESTART_EXIT_CODE,
        is_gateway_supervisor_process,
    )
    from tests.gateway.restart_test_helpers import make_restart_runner

    # Supervisor-restart contract: the OS "restart me" exit code, recognised only
    # when the external-supervisor env is present.
    assert GATEWAY_SERVICE_RESTART_EXIT_CODE == getattr(os, "EX_TEMPFAIL", 75)
    assert is_gateway_supervisor_process({EXTERNAL_GATEWAY_SUPERVISOR_ENV: "1"}) is True
    assert is_gateway_supervisor_process({}) is False

    # Drain-before-stop: stop() is deferred until the active turn finishes.
    runner, _adapter = make_restart_runner()
    runner.stop = AsyncMock()
    runner._launch_detached_restart_command = AsyncMock()
    runner._restart_after_turn_timeout = 5.0
    session_key = "agent:main:telegram:dm:123"
    runner._running_agents[session_key] = MagicMock()

    assert runner.request_restart(detached=False, via_service=True) is True
    assert runner._draining is True

    await asyncio.sleep(0.25)
    runner.stop.assert_not_awaited()

    del runner._running_agents[session_key]
    await runner._restart_task
    runner.stop.assert_awaited_once_with(
        restart=True, detached_restart=False, service_restart=True
    )

    # The real service-restart stop path exits with EX_TEMPFAIL (and spawns no helper).
    service_runner, service_adapter = make_restart_runner()
    service_adapter.disconnect = AsyncMock()
    service_runner._restart_requested = True
    service_runner._restart_via_service = True
    monkeypatch.setattr(
        "subprocess.Popen",
        lambda *args, **kwargs: pytest.fail(
            f"planned service exit must not spawn a restart helper: {args}"
        ),
    )
    with patch("gateway.status.remove_pid_file"), patch("gateway.status.write_runtime_status"):
        await service_runner.stop()
    assert service_runner._exit_code == GATEWAY_SERVICE_RESTART_EXIT_CODE


@pytest.mark.asyncio
async def test_ac_iso_f12_7(tmp_path):
    """resume_pending persists across a restart and re-arms the interrupted session on the next boot."""
    from datetime import datetime

    from gateway.config import GatewayConfig, Platform
    from gateway.run import _should_clear_resume_pending_after_turn
    from gateway.session import SessionEntry, SessionSource, SessionStore
    from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source

    # Durable persistence: the marker survives a FRESH SessionStore over the same state.db.
    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="u1")
    entry = store.get_or_create_session(source)
    assert store.mark_resume_pending(entry.session_key) is True

    reloaded = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    reloaded._ensure_loaded()
    revived = reloaded._entries[entry.session_key]
    assert revived.resume_pending is True
    assert revived.resume_reason == "restart_timeout"

    # Clearing the marked session persists too: a third reload shows it removed.
    assert reloaded.clear_resume_pending(entry.session_key) is True
    again = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    again._ensure_loaded()
    assert again._entries[entry.session_key].resume_pending is False

    # After-turn predicate: an interrupted/failed turn keeps the marker so the next
    # boot re-arms it; only a clean completion clears it.
    assert _should_clear_resume_pending_after_turn({"final_response": "done"}) is True
    assert _should_clear_resume_pending_after_turn({"completed": True}) is True
    assert _should_clear_resume_pending_after_turn({"interrupted": True}) is False
    assert _should_clear_resume_pending_after_turn({"failed": True}) is False
    assert _should_clear_resume_pending_after_turn({"partial": True}) is False
    assert _should_clear_resume_pending_after_turn({"completed": False}) is False
    assert _should_clear_resume_pending_after_turn({"error": "boom"}) is False

    # Startup re-arm: a resume_pending session is redispatched as exactly one internal turn.
    runner, adapter = make_restart_runner()
    src = make_restart_source(chat_id="tg-chat")
    pending = SessionEntry(
        session_key="agent:main:telegram:dm:tg-chat",
        session_id="sid-tg",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        origin=src,
        platform=Platform.TELEGRAM,
        chat_type="dm",
        resume_pending=True,
        resume_reason="restart_interrupted",  # must be in _AUTO_RESUME_REASONS
        last_resume_marked_at=datetime.now(),  # recent -> inside the freshness window
    )
    runner.session_store._entries = {pending.session_key: pending}
    adapter.handle_message = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}

    scheduled = runner._schedule_resume_pending_sessions(platform=Platform.TELEGRAM)
    await asyncio.sleep(0)  # let the spawned task reach adapter.handle_message
    assert scheduled == 1
    adapter.handle_message.assert_awaited_once()
    dispatched = adapter.handle_message.await_args.args[0]
    assert dispatched.source == src
    assert dispatched.internal is True


def test_ac_iso_f12_8():
    """The doc page maps the three behaviours onto Hermes with per-row tag:/main: citations."""
    doc = Path(DOC_PATH).read_text(encoding="utf-8")
    low = doc.lower()

    assert "on_wake" in low or "wake" in low
    assert "restart" in low
    assert "pause" in low

    for sym in ("is_engaged", "paused_reply", "resume_pending", "deliver_wake"):
        assert sym in low, sym
    assert "external_gateway_supervisor_env" in low or "restart.py" in low

    assert "notice" in low
    assert ("not queued" in low) or ("dropped" in low)

    # Each behaviour→symbol pairing must sit on exactly one table row that carries
    # both a `tag:` and a `main:` `path:line` citation — the required citation form.
    rows = [ln for ln in doc.splitlines() if ln.strip().startswith("|")]
    for kw, sym in (
        ("pause", "is_engaged"),
        ("restart", "request_restart"),
        ("respawn", "_default_spawn"),
    ):
        matches = [r for r in rows if kw.lower() in r.lower() and sym.lower() in r.lower()]
        assert len(matches) == 1, (kw, sym, len(matches))
        row = matches[0]
        assert re.search(r"tag:\s*`[^`]+/[^`]+:\d+`", row), (kw, "tag")
        assert re.search(r"main:\s*`[^`]+/[^`]+:\d+`", row), (kw, "main")


def test_ac_iso_f12_9(kanban_conn, monkeypatch):
    """on_wake respawn: a stale kanban card is reclaimed and the real _default_spawn re-dispatches the same task id."""
    from agent.prompt_builder import KANBAN_GUIDANCE

    conn = kanban_conn
    task_id = kb.create_task(
        conn,
        title="orphaned running card",
        body="MARKER: convert manual.pdf to text",
        assignee="w",
    )

    # claim_task opens a real run; reconcile later closes THAT run as "reclaimed",
    # which is what makes build_worker_context render "Prior attempts on this task".
    kb.claim_task(conn, task_id)

    # Orphan it (running, no live claim) but keep current_run_id so reconcile has a run to close.
    conn.execute(
        "UPDATE tasks SET status='running', claim_lock=NULL, claim_expires=NULL, "
        "worker_pid=NULL WHERE id=?",
        (task_id,),
    )
    conn.commit()

    captured = {}

    class _RecordedProc:
        pid = 4242

    def _record_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kwargs.get("env") or {})
        return _RecordedProc()

    # Drive the REAL _default_spawn but record its subprocess instead of running it.
    monkeypatch.setattr("subprocess.Popen", _record_popen)
    monkeypatch.setattr(kb, "_retag_legacy_worker_sessions", lambda _root: None)
    # Keep the memory-pressure guard from skipping the spawn on a loaded host.
    monkeypatch.setattr(kb, "_memory_pressure_level", lambda *a, **k: "unknown")

    result = kb.dispatch_once(conn, dry_run=False)
    assert task_id in result.reconciled_orphans
    assert task_id in [s[0] for s in result.spawned]

    assert captured["cmd"][-3:] == ["chat", "-q", f"work kanban task {task_id}"]
    assert captured["env"]["HERMES_KANBAN_TASK"] == task_id

    ctx = kb.build_worker_context(conn, task_id)
    assert "Prior attempts on this task" in ctx
    assert "orphaned running card" in ctx

    assert "Call `kanban_show()` first" in KANBAN_GUIDANCE
