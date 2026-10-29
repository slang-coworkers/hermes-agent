"""Tests for configurable background process notification modes.

The gateway process watcher pushes status updates to users' chats when
background terminal commands run.  ``display.background_process_notifications``
controls verbosity: off | result | error | all (default).

Contributed by @PeterFile (PR #593), reimplemented on current main.
"""

import asyncio
import contextlib
import contextvars
import queue
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner, _parse_session_key
from gateway.run_notifications import INTERNAL_NOTIFICATION_FOOTER


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class AdmittingHandler(AsyncMock):
    """Fake transport whose successful insertion issues the production receipt."""

    async def _execute_mock_call(self, event, *args, **kwargs):
        result = await super()._execute_mock_call(event, *args, **kwargs)
        event._gateway_accepted = True
        return result


class _FakeRegistry:
    """Return pre-canned sessions, then None once exhausted."""

    def __init__(self, sessions, consumed=False):
        self._sessions = list(sessions)
        self._consumed = consumed

    def get(self, session_id):
        if self._sessions:
            return self._sessions.pop(0)
        return None

    def is_completion_consumed(self, session_id):
        return self._consumed


def _build_runner(monkeypatch, tmp_path, mode: str) -> GatewayRunner:
    """Create a GatewayRunner with a fake config for the given mode."""
    (tmp_path / "config.yaml").write_text(
        f"display:\n  background_process_notifications: {mode}\n",
        encoding="utf-8",
    )

    import gateway.run as gateway_run

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner = GatewayRunner(GatewayConfig())
    adapter = SimpleNamespace(send=AsyncMock(), handle_message=AdmittingHandler())
    runner.adapters[Platform.TELEGRAM] = adapter
    return runner


def _watcher_dict(session_id="proc_test", thread_id=""):
    d = {
        "session_id": session_id,
        "check_interval": 0,
        "platform": "telegram",
        "chat_id": "123",
    }
    if thread_id:
        d["thread_id"] = thread_id
    return d


def _watch_event(session_id="proc_watch", thread_id="42"):
    return {
        "type": "watch_match",
        "session_id": session_id,
        "session_key": f"agent:main:telegram:dm:123:{thread_id}",
        "pattern": "READY",
        "command": "build",
        "output": "READY\n",
    }


def _published():
    """The completion event of a session whose owner already published its result (the watcher waits for it)."""
    event = threading.Event()
    event.set()
    return event


# ---------------------------------------------------------------------------
# _load_background_notifications_mode unit tests
# ---------------------------------------------------------------------------

class TestLoadBackgroundNotificationsMode:


    def test_unknown_mode_falls_back_to_concise(self, monkeypatch, tmp_path):
        (tmp_path / "config.yaml").write_text(
            "display:\n  background_process_notifications: bogus\n"
        )
        import gateway.run as gw
        monkeypatch.setattr(gw, "_hermes_home", tmp_path)
        monkeypatch.delenv("HERMES_BACKGROUND_NOTIFICATIONS", raising=False)
        assert GatewayRunner._load_background_notifications_mode() == "concise"

    def test_reads_config_yaml(self, monkeypatch, tmp_path):
        (tmp_path / "config.yaml").write_text(
            "display:\n  background_process_notifications: error\n"
        )
        import gateway.run as gw
        monkeypatch.setattr(gw, "_hermes_home", tmp_path)
        monkeypatch.delenv("HERMES_BACKGROUND_NOTIFICATIONS", raising=False)
        assert GatewayRunner._load_background_notifications_mode() == "error"


# ---------------------------------------------------------------------------
# _run_process_watcher integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consumed_completion_skips_raw_notification(monkeypatch, tmp_path):
    """#65379: after process(wait) already returned the completion inline,
    the gateway watcher must NOT also push the raw
    "[Background process ... finished with exit code ...]" message.

    The agent-notify branch already honored _completion_consumed, but its
    skip fell through to the text-notification branch, double-delivering the
    same output to the chat (observed on Slack with
    background_process_notifications: all)."""
    import tools.process_registry as pr_module

    sessions = [SimpleNamespace(
        output_buffer="done\n", exited=True, exit_code=0, command="sleep 1; echo done", _completion_event=_published(),
    )]
    monkeypatch.setattr(
        pr_module, "process_registry", _FakeRegistry(sessions, consumed=True)
    )

    async def _instant_sleep(*_a, **_kw):
        pass
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    runner = _build_runner(monkeypatch, tmp_path, "all")
    adapter = runner.adapters[Platform.TELEGRAM]

    # notify_on_complete=True mirrors the reported scenario: the watcher's
    # agent-notify skip must not fall through to a raw adapter.send().
    watcher = _watcher_dict()
    watcher["notify_on_complete"] = True
    await runner._run_process_watcher(watcher)

    adapter.send.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("launching_turn_busy", [True, False])
async def test_agent_notify_receipt_only_while_launching_turn_is_busy(
    monkeypatch, tmp_path, launching_turn_busy
):
    """#112033: with notify_on_complete the agent reports the result itself, so the chat gets no
    separate receipt — except while the launching turn is still running, when the injection only
    queues a follow-up and the concise receipt is the only thing the user would see."""
    import tools.process_registry as pr_module

    sessions = [SimpleNamespace(
        output_buffer="done\n", exited=True, exit_code=0, command="echo done", started_at=None,
        _completion_event=_published(),
    )]
    monkeypatch.setattr(pr_module, "process_registry", _FakeRegistry(sessions, consumed=False))

    async def _instant_sleep(*_a, **_kw):
        pass
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    runner = _build_runner(monkeypatch, tmp_path, "concise")
    runner._enqueue_process_completion_notification = AsyncMock(return_value=True)
    adapter = runner.adapters[Platform.TELEGRAM]
    watcher = {**_watcher_dict(), "session_key": "agent:main:telegram:dm:123", "notify_on_complete": True}
    adapter._active_sessions = {watcher["session_key"]: asyncio.Event()} if launching_turn_busy else {}

    await runner._run_process_watcher(watcher)

    runner._enqueue_process_completion_notification.assert_awaited_once()
    if launching_turn_busy:
        adapter.send.assert_awaited_once()
        assert adapter.send.await_args.args[1].startswith("✅ Background task finished")
    else:
        adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_arm_process_watcher_schedules_on_live_loop_only(monkeypatch, tmp_path):
    """#112033: a watcher registered mid-turn starts on the gateway loop at once while the gateway
    serves; before/after that the caller keeps it for the startup / post-turn drain."""
    runner = _build_runner(monkeypatch, tmp_path, "concise")
    started = []

    async def _fake_watcher(watcher):
        started.append(watcher["session_id"])
    runner._run_process_watcher = _fake_watcher
    runner._gateway_loop = asyncio.get_running_loop()

    runner._running = False
    assert runner.arm_process_watcher({"session_id": "proc_early"}) is False

    runner._running = True
    assert runner.arm_process_watcher({"session_id": "proc_live"}) is True
    await asyncio.sleep(0.05)
    assert started == ["proc_live"]


@pytest.mark.asyncio
async def test_consumed_completion_skips_raw_notification_without_agent_notify(
    monkeypatch, tmp_path
):
    """#65379 variant: same double-delivery guard for plain watchers
    (notify_on_complete=False) — wait/log consumption suppresses the raw
    completion message in every mode."""
    import tools.process_registry as pr_module

    sessions = [SimpleNamespace(
        output_buffer="done\n", exited=True, exit_code=0, command="echo done", _completion_event=_published(),
    )]
    monkeypatch.setattr(
        pr_module, "process_registry", _FakeRegistry(sessions, consumed=True)
    )

    async def _instant_sleep(*_a, **_kw):
        pass
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    runner = _build_runner(monkeypatch, tmp_path, "all")
    adapter = runner.adapters[Platform.TELEGRAM]

    await runner._run_process_watcher(_watcher_dict())

    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_inject_watch_notification_routes_from_session_store_origin(monkeypatch, tmp_path):
    from gateway.session import SessionSource

    runner = _build_runner(monkeypatch, tmp_path, "all")
    adapter = runner.adapters[Platform.TELEGRAM]
    runner.session_store._entries["agent:main:telegram:group:-100:42"] = SimpleNamespace(
        origin=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-100",
            chat_type="group",
            thread_id="42",
            user_id="123",
            user_name="Emiliyan",
        )
    )

    evt = {
        "session_id": "proc_watch",
        "session_key": "agent:main:telegram:group:-100:42",
    }

    await runner._inject_watch_notification("[SYSTEM: Background process matched]", evt)

    adapter.handle_message.assert_awaited_once()
    synth_event = adapter.handle_message.await_args.args[0]
    assert synth_event.internal is True
    assert synth_event.source.platform == Platform.TELEGRAM
    assert synth_event.source.chat_id == "-100"
    assert synth_event.source.chat_type == "group"
    assert synth_event.source.thread_id == "42"
    assert synth_event.source.user_id == "123"
    assert synth_event.source.user_name == "Emiliyan"


@pytest.mark.asyncio
async def test_post_turn_watch_drain_off_consumes_without_injecting(monkeypatch, tmp_path):
    runner = _build_runner(monkeypatch, tmp_path, "off")
    adapter = runner.adapters[Platform.TELEGRAM]
    completion_queue = queue.Queue()
    completion_queue.put(_watch_event("proc_one"))
    completion_queue.put(_watch_event("proc_two"))
    async_event = {"type": "async_delegation", "session_id": "delegate_one"}
    completion_queue.put(async_event)

    await runner._drain_watch_notifications(completion_queue)

    adapter.handle_message.assert_not_awaited()
    assert completion_queue.qsize() == 1
    assert completion_queue.get_nowait() is async_event


@pytest.mark.asyncio
async def test_post_turn_watch_drain_all_injects_from_queued_event_origin(monkeypatch, tmp_path):
    from gateway.session import SessionSource

    runner = _build_runner(monkeypatch, tmp_path, "all")
    adapter = runner.adapters[Platform.TELEGRAM]
    runner.session_store._entries["agent:main:telegram:dm:123:42"] = SimpleNamespace(
        origin=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="123",
            chat_type="dm",
            thread_id="42",
            user_id="proc_owner",
            user_name="alice",
        )
    )
    completion_queue = queue.Queue()
    completion_queue.put(_watch_event())
    async_event = {"type": "async_delegation", "session_id": "delegate_one"}
    completion_queue.put(async_event)

    await runner._drain_watch_notifications(completion_queue)

    adapter.handle_message.assert_awaited_once()
    synth_event = adapter.handle_message.await_args.args[0]
    assert synth_event.source.thread_id == "42"
    assert synth_event.source.user_id == "proc_owner"
    assert completion_queue.qsize() == 1
    assert completion_queue.get_nowait() is async_event


@pytest.mark.asyncio
async def test_inject_watch_notification_drops_stale_trigger_reply_anchor(monkeypatch, tmp_path):
    """Regression for #52694: the synthetic watch event must not reuse the triggering
    message id as its reply anchor.

    The id names the message that STARTED the process; by the time the process exits the
    user has often moved on, and a reply quoting that id answers a stale message (a finished
    background job visibly replying to an old Discord DM message from another topic).
    Routing stays on the persisted origin — Telegram DM-topic lanes route anchor-less
    synthetic sends through the topic's message_thread_id (#87051) — and the original id
    survives only as event metadata for debugging.
    """
    from gateway.session import SessionSource

    runner = _build_runner(monkeypatch, tmp_path, "all")
    adapter = runner.adapters[Platform.TELEGRAM]
    runner.session_store._entries["agent:main:telegram:dm:123:24296"] = SimpleNamespace(
        origin=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="123",
            chat_type="dm",
            thread_id="24296",
            user_id="1",
            user_name="Fabio",
            message_id="777",  # persisted origin: an equally stale triggering id
        )
    )

    evt = {
        "session_id": "proc_watch",
        "session_key": "agent:main:telegram:dm:123:24296",
        "message_id": "777",
    }

    await runner._inject_watch_notification("[SYSTEM: Background process matched]", evt)

    adapter.handle_message.assert_awaited_once()
    synth_event = adapter.handle_message.await_args.args[0]
    assert synth_event.internal is True
    # No stale anchor on the event OR the restored origin it routes through.
    assert synth_event.message_id is None
    assert synth_event.source.message_id is None
    # Routing provenance is untouched: the notification still lands in the origin topic.
    assert synth_event.source.thread_id == "24296"
    # The derived final-reply anchor is empty on every platform branch.
    from gateway.platforms.base import _reply_anchor_for_event
    assert _reply_anchor_for_event(synth_event) is None
    # The original id survives for debugging only.
    assert synth_event.metadata["original_trigger_message_id"] == "777"
    # Unambiguous machine provenance: SYSTEM prefix still leads, footer appended, origin tagged.
    assert synth_event.text.startswith("[SYSTEM: ")
    assert synth_event.text.rstrip().endswith(INTERNAL_NOTIFICATION_FOOTER)
    assert synth_event.metadata["notification_origin"] == "process_registry_synthetic"


@pytest.mark.asyncio
async def test_inject_watch_notification_loads_session_store_off_loop(monkeypatch, tmp_path):
    from gateway.session import SessionSource

    runner = _build_runner(monkeypatch, tmp_path, "all")
    adapter = runner.adapters[Platform.TELEGRAM]
    runner.session_store._entries["agent:main:telegram:dm:123:24296"] = SimpleNamespace(
        origin=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="123",
            chat_type="dm",
            thread_id="24296",
            user_id="1",
            user_name="Fabio",
        )
    )
    loop_thread = threading.get_ident()
    load_threads = []
    real_ensure_loaded = runner.session_store._ensure_loaded

    def spy_ensure_loaded():
        load_threads.append(threading.get_ident())
        return real_ensure_loaded()

    monkeypatch.setattr(runner.session_store, "_ensure_loaded", spy_ensure_loaded)

    await runner._inject_watch_notification(
        "[SYSTEM: Background process matched]",
        {
            "session_id": "proc_watch",
            "session_key": "agent:main:telegram:dm:123:24296",
            "message_id": "777",
        },
    )

    adapter.handle_message.assert_awaited_once()
    assert load_threads
    assert all(thread_id != loop_thread for thread_id in load_threads)


@pytest.mark.asyncio
async def test_inject_watch_notification_ignores_foreground_event_source(monkeypatch, tmp_path):
    """Negative test: watch notification must NOT route to the foreground thread."""
    from gateway.session import SessionSource

    runner = _build_runner(monkeypatch, tmp_path, "all")
    adapter = runner.adapters[Platform.TELEGRAM]

    # Session store has the process's original thread (thread 42)
    runner.session_store._entries["agent:main:telegram:group:-100:42"] = SimpleNamespace(
        origin=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-100",
            chat_type="group",
            thread_id="42",
            user_id="proc_owner",
            user_name="alice",
        )
    )

    # The evt dict carries the correct session_key — NOT a foreground event
    evt = {
        "session_id": "proc_cross_thread",
        "session_key": "agent:main:telegram:group:-100:42",
    }

    await runner._inject_watch_notification("[SYSTEM: watch match]", evt)

    adapter.handle_message.assert_awaited_once()
    synth_event = adapter.handle_message.await_args.args[0]
    # Must route to thread 42 (process origin), NOT some other thread
    assert synth_event.source.thread_id == "42"
    assert synth_event.source.user_id == "proc_owner"


# ---------------------------------------------------------------------------
# concise mode — pretty one-liner instead of the raw output dump
# ---------------------------------------------------------------------------


class TestConciseFormatter:

    def test_success_is_one_line_without_output(self):
        from gateway.run import _format_concise_process_notification
        text = _format_concise_process_notification(
            "proc_abc", "python3 scan_fleet.py --all", 0,
            "1300\n1400\n1500\n{...huge json...}",
            duration_seconds=754,
        )
        assert text.startswith("✅ Background task finished")
        assert "scan_fleet.py" in text
        assert "12m 34s" in text
        # The raw output must NOT appear on success
        assert "1300" not in text
        assert "\n" not in text

    def test_failure_appends_short_tail(self):
        from gateway.run import _format_concise_process_notification
        out = "\n".join(f"line{i}" for i in range(50)) + "\nTraceback: boom"
        text = _format_concise_process_notification(
            "proc_abc", "make build", 2, out,
        )
        assert text.startswith("❌ Background task failed")
        assert "exit 2" in text and "Traceback: boom" in text
        assert "rerun" in text
        # Only a short tail, not the whole output
        assert "line0" not in text

    def test_long_command_is_truncated(self):
        from gateway.run import _format_concise_process_notification
        text = _format_concise_process_notification(
            "proc_abc", "x" * 300, 0, "",
        )
        assert "…" in text
        assert len(text) < 200


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True, "recheck_fails"])
async def test_agent_notify_refused_completion_backs_off_then_sends_plain_notice(monkeypatch, tmp_path, committed):
    """A refused completion injection must not poll an exited process forever, and must not be
    dropped either: bounded backoff, then the plain completion notice (#138940). A last refusal
    whose admission actually committed (lost ACK) must not add a second notice."""
    import tools.process_registry as pr_module
    from gateway import session_automation
    from gateway import run_notifications as rn

    exited = SimpleNamespace(
        output_buffer="done\n", exited=True, exit_code=0, command="echo done", started_at=1.0,
        _completion_event=_published(),
    )
    monkeypatch.setattr(pr_module, "process_registry", _FakeRegistry([exited] * 20, consumed=False))
    sleeps = []

    async def _record_sleep(delay, *_a, **_kw):
        sleeps.append(delay)
    monkeypatch.setattr(asyncio, "sleep", _record_sleep)

    runner = _build_runner(monkeypatch, tmp_path, "concise")
    runner._enqueue_process_completion_notification = AsyncMock(return_value=False)
    runner.session_authority = object()
    inside = []

    @contextlib.asynccontextmanager
    async def _scope(evt):
        inside.append(evt)
        try:
            yield
        finally:
            inside.pop()
    monkeypatch.setattr(runner, "_completion_event_scope", _scope)

    def _admission(_runner, _evt):
        assert inside and inside[-1] is _evt, "recheck ran outside the owning profile scope"
        if committed == "recheck_fails":
            raise RuntimeError("store closed")
        return {"admission_id": "committed"} if committed else None
    monkeypatch.setattr(session_automation, "completion_admission", _admission)

    await runner._run_process_watcher({**_watcher_dict(), "check_interval": 5, "notify_on_complete": True})

    assert runner._enqueue_process_completion_notification.await_count == rn._MAX_COMPLETION_INJECTION_ATTEMPTS
    assert sleeps == [5, 10, 20, 40, 60, 60, 60, 60]
    adapter = runner.adapters[Platform.TELEGRAM]
    if committed is True:
        adapter.send.assert_not_awaited()
    else:
        adapter.send.assert_awaited_once()
        assert adapter.send.await_args.args[1].startswith("✅ Background task finished")


@pytest.mark.asyncio
async def test_concise_mode_no_interim_output_updates(monkeypatch, tmp_path):
    """concise never pushes 'is still running~ New output' interim updates."""
    import tools.process_registry as pr_module

    running = SimpleNamespace(
        output_buffer="chunk one\n", exited=False, exit_code=None,
        command="sleep 100", started_at=None,
    )
    done = SimpleNamespace(
        output_buffer="chunk one\nchunk two\n", exited=True, exit_code=0,
        command="sleep 100", started_at=None, _completion_event=_published(),
    )
    monkeypatch.setattr(
        pr_module, "process_registry", _FakeRegistry([running, done], consumed=False)
    )

    async def _instant_sleep(*_a, **_kw):
        pass
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    runner = _build_runner(monkeypatch, tmp_path, "concise")
    adapter = runner.adapters[Platform.TELEGRAM]

    await runner._run_process_watcher(_watcher_dict())

    # Exactly one send: the final concise message; no interim updates.
    adapter.send.assert_awaited_once()
    sent_text = adapter.send.await_args.args[1]
    assert "is still running" not in sent_text
    assert sent_text.startswith("✅ Background task finished")


# ---------------------------------------------------------------------------
# _parse_session_key helper
# ---------------------------------------------------------------------------


def test_parse_session_key_with_extra_parts():
    """6th part in a group key may be a user_id, not a thread_id — omit it."""
    result = _parse_session_key("agent:main:discord:group:chan123:thread456")
    assert result == {"platform": "discord", "chat_type": "group", "chat_id": "chan123"}


def test_parse_session_key_named_profile():
    """A named-profile namespace parses and is reported as ``profile``."""
    result = _parse_session_key("agent:work:telegram:dm:123")
    assert result == {
        "profile": "work",
        "platform": "telegram",
        "chat_type": "dm",
        "chat_id": "123",
    }


def test_parse_session_key_named_profile_thread_id():
    """Thread-slot handling is identical for named-profile keys."""
    result = _parse_session_key("agent:work:telegram:thread:100:42")
    assert result == {
        "profile": "work",
        "platform": "telegram",
        "chat_type": "thread",
        "chat_id": "100",
        "thread_id": "42",
    }


def test_parse_session_key_named_profile_group_suffix_omitted():
    """Group-suffix (user_id) stays omitted for named-profile keys too."""
    result = _parse_session_key("agent:work:discord:group:chan1:user9")
    assert result == {
        "profile": "work",
        "platform": "discord",
        "chat_type": "group",
        "chat_id": "chan1",
    }


def test_parse_session_key_main_shape_unchanged():
    """``main`` keys keep their historical dict shape exactly (no ``profile`` key)."""
    result = _parse_session_key("agent:main:telegram:dm:123:42")
    assert result == {
        "platform": "telegram",
        "chat_type": "dm",
        "chat_id": "123",
        "thread_id": "42",
    }


def test_parse_session_key_rejects_invalid_namespace():
    """A namespace slot that cannot be a profile id still parses to None."""
    assert _parse_session_key("agent:Bad_NS:telegram:dm:123") is None
    assert _parse_session_key("agent:main:telegram:dm") is None
    assert _parse_session_key("raw-session-id") is None


def test_build_process_event_source_named_profile_key(monkeypatch, tmp_path):
    """A synthetic event keyed by a named-profile session resolves platform/chat and keeps
    the profile, instead of being unresolvable and dropped with a warning."""
    runner = _build_runner(monkeypatch, tmp_path, "all")
    source = runner._build_process_event_source({
        "session_id": "proc_watch",
        "session_key": "agent:work:telegram:dm:123",
    })
    assert source is not None
    assert source.platform is Platform.TELEGRAM
    assert source.chat_id == "123"
    assert source.chat_type == "dm"
    assert source.profile == "work"


# ---------------------------------------------------------------------------
# api_server (stateless) wake routing — gateway/wake.py self-post path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_inject_watch_notification_raw_session_key_self_posts(monkeypatch, tmp_path):
    """An event whose session_key is a RAW api_server session id (not an
    agent:main:... structured key) must wake the real session via the
    /v1/chat/completions self-post instead of being dropped for missing
    routing metadata."""
    runner = _build_runner(monkeypatch, tmp_path, "all")
    api_adapter = SimpleNamespace(
        supports_async_delivery=False,
        handle_message=AdmittingHandler(),
        _host="127.0.0.1", _port=8642, _api_key="k", _model_name="m",
    )
    runner.adapters[Platform.API_SERVER] = api_adapter

    posts = []

    async def fake_self_post(adapter, *, text, session_id):
        posts.append({"text": text, "session_id": session_id})

    import gateway.wake as wake_mod
    monkeypatch.setattr(wake_mod, "_self_post_chat_completion", fake_self_post)

    evt = {
        "session_id": "proc_watch",
        "session_key": "raw-hq-session-id",  # no agent:main:... structure
    }
    result = await runner._inject_watch_notification("[SYSTEM: subagent finished]", evt)

    assert result is True
    api_adapter.handle_message.assert_not_awaited()
    # Same presentation contract as the push path: leading SYSTEM prefix intact, machine-origin
    # footer appended — this text becomes a role=user turn on the stateless surface too.
    assert len(posts) == 1
    assert posts[0]["session_id"] == "raw-hq-session-id"
    assert posts[0]["text"].startswith("[SYSTEM: subagent finished]")
    assert posts[0]["text"].rstrip().endswith(INTERNAL_NOTIFICATION_FOOTER)


@pytest.mark.asyncio
async def test_inject_watch_notification_origin_session_id_wins(monkeypatch, tmp_path):
    """origin_session_id (stamped at dispatch time by async_delegation) takes
    precedence as the wake target."""
    runner = _build_runner(monkeypatch, tmp_path, "all")
    api_adapter = SimpleNamespace(
        supports_async_delivery=False,
        handle_message=AdmittingHandler(),
        _host="127.0.0.1", _port=8642, _api_key="k", _model_name="m",
    )
    runner.adapters[Platform.API_SERVER] = api_adapter

    posts = []

    async def fake_self_post(adapter, *, text, session_id):
        posts.append(session_id)

    import gateway.wake as wake_mod
    monkeypatch.setattr(wake_mod, "_self_post_chat_completion", fake_self_post)

    evt = {
        "session_id": "proc_watch",
        "session_key": "",
        "origin_session_id": "raw-origin-sid",
    }
    result = await runner._inject_watch_notification("[SYSTEM: done]", evt)
    assert result is True
    assert posts == ["raw-origin-sid"]


@pytest.mark.asyncio
async def test_async_delegation_apiserver_persists_delivery_not_self_post(
    monkeypatch, tmp_path,
):
    """#85957: an async_delegation completion targeting a stateless api_server
    session must be persisted as a durable DELIVERY row — never self-POSTed
    to /v1/chat/completions as a new role=user prompt (which starts an
    unauthorized agent turn after the client-owned parent turn ended)."""
    runner = _build_runner(monkeypatch, tmp_path, "all")
    api_adapter = SimpleNamespace(
        supports_async_delivery=False,
        handle_message=AdmittingHandler(),
        _host="127.0.0.1", _port=8642, _api_key="k", _model_name="m",
    )
    runner.adapters[Platform.API_SERVER] = api_adapter

    import gateway.wake as wake_mod

    posts = []

    async def fake_self_post(adapter, *, text, session_id):
        posts.append(session_id)

    persisted = []

    async def fake_persist(adapter, *, text, session_id, evt=None):
        persisted.append({"text": text, "session_id": session_id, "evt": evt})

    monkeypatch.setattr(wake_mod, "_self_post_chat_completion", fake_self_post)
    monkeypatch.setattr(wake_mod, "persist_delegation_delivery", fake_persist)

    evt = {
        "type": "async_delegation",
        "delegation_id": "deleg_85957",
        "session_key": "raw-hq-session-id",  # no agent:main:... structure
        "origin_session_id": "raw-hq-session-id",
        "status": "completed",
    }
    result = await runner._inject_watch_notification(
        "[ASYNC DELEGATION BATCH COMPLETE — deleg_85957]", evt,
    )

    assert result is True
    assert posts == []  # the self-POST user-turn wake must NOT fire
    api_adapter.handle_message.assert_not_awaited()
    assert len(persisted) == 1
    assert persisted[0]["session_id"] == "raw-hq-session-id"
    assert persisted[0]["evt"]["delegation_id"] == "deleg_85957"
    # Persist-only route stays verbatim: the client reads this row, no model turn is woken.
    assert persisted[0]["text"] == "[ASYNC DELEGATION BATCH COMPLETE — deleg_85957]"


@pytest.mark.asyncio
async def test_async_delegation_apiserver_persist_failure_is_retryable(
    monkeypatch, tmp_path,
):
    """A failed delivery persist returns False so the durable claim is
    released and the completion retried — never silently lost."""
    runner = _build_runner(monkeypatch, tmp_path, "all")
    api_adapter = SimpleNamespace(
        supports_async_delivery=False,
        handle_message=AdmittingHandler(),
        _host="127.0.0.1", _port=8642, _api_key="k", _model_name="m",
    )
    runner.adapters[Platform.API_SERVER] = api_adapter

    import gateway.wake as wake_mod

    async def fail_persist(adapter, *, text, session_id, evt=None):
        raise RuntimeError("state.db unavailable")

    monkeypatch.setattr(wake_mod, "persist_delegation_delivery", fail_persist)

    evt = {
        "type": "async_delegation",
        "delegation_id": "deleg_fail",
        "session_key": "raw-sid",
    }
    result = await runner._inject_watch_notification("[BATCH COMPLETE]", evt)
    assert result is False


def test_gateway_drain_retains_and_formats_overflow_events():
    """watch_overflow_* events must survive the gateway drain and render
    their summary — previously they were discarded at the drain (only
    watch_match/watch_disabled were retained) and had no formatter branch."""
    import asyncio
    from gateway.run import (
        _drain_gateway_watch_events,
        _format_gateway_process_notification,
    )

    queue = asyncio.Queue()
    tripped = {
        "type": "watch_overflow_tripped",
        "message": "watch flood detected: 47 notifications suppressed for pattern 'ERROR'",
        "session_id": "proc_a1b2",
    }
    released = {
        "type": "watch_overflow_released",
        "message": "watch flood released: notifications resumed for pattern 'ERROR'",
        "session_id": "proc_a1b2",
    }
    queue.put_nowait(tripped)
    queue.put_nowait(released)

    retained = _drain_gateway_watch_events(queue)
    assert retained == [tripped, released]

    out_tripped = _format_gateway_process_notification(tripped)
    assert "47 notifications suppressed" in out_tripped
    assert "exit code" not in out_tripped
    out_released = _format_gateway_process_notification(released)
    assert "notifications resumed" in out_released
    assert "exit code" not in out_released


@pytest.mark.asyncio
async def test_raw_output_modes_are_human_facing(monkeypatch, tmp_path):
    """#54266: the chat-facing watcher messages (final in all/result/error, interim in all) carry a
    status header and the (ANSI-stripped) output, never the internal ``proc_*`` id or the bracketed
    ``[Background process …~ …]`` debug wrapper. Full output stays available via the process tool."""
    import tools.process_registry as pr_module

    running = SimpleNamespace(output_buffer="\x1b[32mstep 1 ok\x1b[0m\n", exited=False, exit_code=None,
                              command="make -j8 all", started_at=None)
    done = SimpleNamespace(output_buffer="\x1b[32mstep 1 ok\x1b[0m\n\x1b[31mlinker error\x1b[0m\n", exited=True,
                           exit_code=2, command="make -j8 all", started_at=None, _completion_event=_published())
    monkeypatch.setattr(pr_module, "process_registry", _FakeRegistry([running, done]))

    async def _instant_sleep(*_a, **_kw):
        pass
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    runner = _build_runner(monkeypatch, tmp_path, "all")
    adapter = runner.adapters[Platform.TELEGRAM]
    await runner._run_process_watcher(_watcher_dict(session_id="proc_deadbeef"))

    sent = [call.args[1] for call in adapter.send.await_args_list]
    assert len(sent) == 2
    interim, final = sent
    assert interim.startswith("⏳ Background task still running") and "step 1 ok" in interim
    assert final.startswith("❌ Background task failed") and "exit 2" in final and "linker error" in final
    for text in sent:
        assert "proc_deadbeef" not in text and "[Background process" not in text and "~" not in text
        assert "\x1b[" not in text
        assert "make -j8 all" in text


# ---------------------------------------------------------------------------
# The watcher waits for its process's publish (FLEET-F62.h E11 (vi))
# ---------------------------------------------------------------------------

def _unpublished(output="done\n", exit_code=0, command="echo done"):
    """An exited process whose owner has not finished publishing its result yet."""
    return SimpleNamespace(output_buffer=output, exited=True, exit_code=exit_code, command=command, started_at=None,
                           _completion_event=threading.Event())


class _HeldRegistry:
    """Always returns the same session; never consumed."""

    def __init__(self, session):
        self.session = session

    def get(self, session_id):
        return self.session

    def is_completion_consumed(self, session_id):
        return False


def _ticking(monkeypatch, on_tick):
    """Replace asyncio.sleep: ``on_tick(n)`` runs before each watcher tick n (1-based); other sleeps still yield."""
    real_sleep = asyncio.sleep
    ticks = {"n": 0}

    async def _sleep(delay, *a, **kw):
        if delay == 0:
            ticks["n"] += 1
            on_tick(ticks["n"])
        await real_sleep(0)
    monkeypatch.setattr(asyncio, "sleep", _sleep)
    return ticks


def _agent_watcher():
    return {**_watcher_dict(), "session_key": "agent:main:telegram:dm:123", "notify_on_complete": True}


@pytest.mark.asyncio
async def test_watcher_defers_an_exit_until_its_publish_is_done(monkeypatch, tmp_path):
    """An exited process whose result is not yet published gets no completion and no notice; once the owner's
    publish sets the event, exactly one normal completion follows."""
    import tools.process_registry as pr_module

    session = _unpublished()
    monkeypatch.setattr(pr_module, "process_registry", _HeldRegistry(session))
    runner = _build_runner(monkeypatch, tmp_path, "concise")
    runner._enqueue_process_completion_notification = AsyncMock(return_value=True)
    adapter = runner.adapters[Platform.TELEGRAM]
    seen = []

    def on_tick(n):
        seen.append((n, runner._enqueue_process_completion_notification.await_count, adapter.send.await_count))
        if n == 4:
            session._completion_event.set()
    _ticking(monkeypatch, on_tick)

    await runner._run_process_watcher(_agent_watcher())

    assert [s[1:] for s in seen[:4]] == [(0, 0)] * 4, seen
    runner._enqueue_process_completion_notification.assert_awaited_once()
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_watcher_sends_one_output_free_notice_when_the_publish_is_late(monkeypatch, tmp_path):
    """Held beyond 12 ticks: exactly one unverified-exit notice (no output, command or exit code); the watcher then
    ends, so a publish that lands just after the notice gets no completion from it (UPG-F69.a)."""
    import tools.process_registry as pr_module

    session = _unpublished(output="SECRET-OUTPUT\n", exit_code=3, command="make release")
    monkeypatch.setattr(pr_module, "process_registry", _HeldRegistry(session))
    runner = _build_runner(monkeypatch, tmp_path, "concise")
    runner._enqueue_process_completion_notification = AsyncMock(return_value=True)
    adapter = runner.adapters[Platform.TELEGRAM]
    adapter.send.side_effect = lambda *a, **kw: session._completion_event.set()

    await asyncio.wait_for(runner._run_process_watcher(_agent_watcher()), timeout=30)

    notices = [call.args[1] for call in adapter.send.await_args_list]
    assert len(notices) == 1, notices
    assert "proc_test" in notices[0] and "outcome is unknown" in notices[0], notices[0]
    for leak in ("SECRET-OUTPUT", "make release", "3"):
        assert leak not in notices[0].replace("proc_test", ""), notices[0]
    runner._enqueue_process_completion_notification.assert_not_awaited()


@pytest.mark.asyncio
async def test_watcher_deferral_never_blocks_the_loop(monkeypatch, tmp_path):
    """While the watcher defers, a concurrent coroutine keeps running on the same loop."""
    import tools.process_registry as pr_module

    session = _unpublished()
    monkeypatch.setattr(pr_module, "process_registry", _HeldRegistry(session))
    runner = _build_runner(monkeypatch, tmp_path, "concise")
    runner._enqueue_process_completion_notification = AsyncMock(return_value=True)
    beats, deferred_ticks = [], []
    unpatched_sleep = asyncio.sleep

    def on_tick(n):
        deferred_ticks.append(n)
        if n >= 4 and len(beats) >= 5:
            session._completion_event.set()
    _ticking(monkeypatch, on_tick)

    async def heartbeat():
        while not session._completion_event.is_set():
            beats.append(len(deferred_ticks))
            await unpatched_sleep(0)

    await asyncio.wait_for(asyncio.gather(runner._run_process_watcher(_agent_watcher()), heartbeat()), timeout=30)

    assert len(deferred_ticks) >= 4 and len(beats) >= 5 and len(set(beats)) > 1, (beats, deferred_ticks)
    runner._enqueue_process_completion_notification.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_notify, expected", [(True, 1), (False, 0)])
async def test_notify_off_watcher_notice_follows_agent_notify(monkeypatch, tmp_path, agent_notify, expected):
    """notify_mode=off with agent_notify defers like any non-silent watcher, so a late publish gets the safety notice;
    off without agent_notify is silent and never sends anything."""
    import tools.process_registry as pr_module

    session = _unpublished()
    monkeypatch.setattr(pr_module, "process_registry", _HeldRegistry(session))
    runner = _build_runner(monkeypatch, tmp_path, "off")
    runner._enqueue_process_completion_notification = AsyncMock(return_value=True)
    adapter = runner.adapters[Platform.TELEGRAM]
    _ticking(monkeypatch, lambda n: session._completion_event.set() if n == 20 else None)
    watcher = {**_agent_watcher(), "notify_on_complete": agent_notify}

    await asyncio.wait_for(runner._run_process_watcher(watcher), timeout=30)

    sent = [call.args[1] for call in adapter.send.await_args_list]
    assert len(sent) == expected, sent
    assert all("outcome is unknown" in text for text in sent), sent


@pytest.mark.asyncio
async def test_named_profile_unverified_notice(monkeypatch, tmp_path):
    """A named profile's deferred notice is rendered from that profile's catalog (its display.language)."""
    import tools.process_registry as pr_module
    from agent.i18n import reset_language_cache, t
    from hermes_cli.profiles import get_profile_dir

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_BASE_HOME", str(tmp_path))
    work = get_profile_dir("work")
    (work / "locales").mkdir(parents=True)
    (work / "config.yaml").write_text("display:\n  language: en\n", encoding="utf-8")
    (work / "locales" / "en.yaml").write_text(
        "gateway:\n  background:\n    unverified_exit: \"WORK-CATALOG {session_id}\"\n", encoding="utf-8")
    reset_language_cache()
    session = _unpublished()
    monkeypatch.setattr(pr_module, "process_registry", _HeldRegistry(session))
    runner = _build_runner(monkeypatch, tmp_path, "concise")
    runner._enqueue_process_completion_notification = AsyncMock(return_value=True)
    adapter = runner.adapters[Platform.TELEGRAM]
    runner._profile_adapters = {"work": {Platform.TELEGRAM: adapter}}   # a named profile sends through its own map
    _ticking(monkeypatch, lambda n: session._completion_event.set() if n == 20 else None)
    watcher = {**_agent_watcher(), "session_key": "agent:work:telegram:dm:123"}

    try:
        await asyncio.wait_for(runner._run_process_watcher(watcher), timeout=60)
        default_text = t("gateway.background.unverified_exit", session_id="proc_test")
    finally:
        reset_language_cache()

    sent = [call.args[1] for call in adapter.send.await_args_list]
    assert sent == ["WORK-CATALOG proc_test"], (sent, default_text)
    assert default_text != sent[0]


def _install_held_transform(monkeypatch, home, release, entered):
    """A real transform_terminal_output plugin that rewrites ``"status": "settled"`` and holds the first call (the
    reader's publish) until ``release``: the watcher's own call then shares the reader's hook identity."""
    import hermes_yaml as yaml
    from hermes_cli import plugins as plugins_mod

    plugin = home / "plugins" / "held_transform"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text("name: held_transform\n", encoding="utf-8")
    (plugin / "__init__.py").write_text(
        "def register(ctx):\n"
        "    def _transform(output='', **kw):\n"
        "        import tests.gateway.test_background_process_notifications as t\n"
        "        t._HELD_HOOK['calls'] += 1\n"
        "        if t._HELD_HOOK['calls'] == 1:\n"
        "            t._HELD_HOOK['entered'].set()\n"
        "            t._HELD_HOOK['release'].wait(60)\n"
        "        return output.replace('\"status\": \"settled\"', '\"status\": \"rewritten\"')\n"
        "    ctx.register_hook('transform_terminal_output', _transform)\n",
        encoding="utf-8")
    (home / "config.yaml").write_text(yaml.safe_dump({
        "plugins": {"enabled": ["held_transform"], "hook_callback_timeout": 120},
        "display": {"background_process_notifications": "concise"},
    }), encoding="utf-8")
    _HELD_HOOK.update(calls=0, entered=entered, release=release)
    monkeypatch.setattr(plugins_mod, "_plugin_manager", plugins_mod.PluginManager())
    plugins_mod.discover_plugins()


_HELD_HOOK: dict = {}


def _settled_session(registry, monkeypatch):
    """A real registry session that prints a settled payload, spawned under one bound call id as a tool call is.
    Returns it with the spawning context: core arms the live watcher from that thread (``arm_process_watcher`` →
    ``run_coroutine_threadsafe``), so the watcher's task runs in a copy of it and carries the same call id."""
    import shlex
    import sys

    from tools import process_registry as pr_module
    from tools.approval_context import set_current_observability_context

    monkeypatch.setattr(pr_module, "process_registry", registry)
    spawn_ctx = contextvars.Context()
    spawn_ctx.run(set_current_observability_context, tool_call_id="call-watcher-test")
    script = 'import json; print(json.dumps({"status": "settled", "reply": "R"}))'
    session = spawn_ctx.run(registry.spawn_local, f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}",
                            task_id="t-watch")
    session.notify_on_complete = True
    return session, spawn_ctx


@pytest.mark.asyncio
async def test_watcher_builds_from_the_published_transform_not_its_own_racing_call(monkeypatch, tmp_path):
    """The reader's publish transform is held under the call's hook identity while the watcher ticks with the session
    exited: the watcher must wait for the publish, so the agent is fed the transformed text, never raw settled."""
    import os

    from tools.process_registry import ProcessRegistry

    home = __import__("pathlib").Path(os.environ["HERMES_HOME"])
    release, entered = threading.Event(), threading.Event()
    _install_held_transform(monkeypatch, home, release, entered)
    registry = ProcessRegistry()
    session, spawn_ctx = _settled_session(registry, monkeypatch)
    runner = _build_runner(monkeypatch, home, "concise")
    injected, unset_ticks = [], []
    runner._enqueue_process_completion_notification = AsyncMock(
        side_effect=lambda text, evt: injected.append(text) or True)
    assert await asyncio.to_thread(entered.wait, 30), "the reader's publish never reached the transform"

    real_sleep = asyncio.sleep

    async def _sleep(delay, *a, **kw):
        if delay == 0 and session.exited and not session._completion_event.is_set():
            unset_ticks.append(1)
            if len(unset_ticks) == 3:
                release.set()
        await real_sleep(0.01)
    monkeypatch.setattr(asyncio, "sleep", _sleep)
    watcher = _agent_watcher() | {"session_id": session.id}
    task = asyncio.get_running_loop().create_task(runner._run_process_watcher(watcher), context=spawn_ctx.copy())
    try:
        await asyncio.wait_for(task, timeout=60)
    finally:
        release.set()

    assert unset_ticks, "the watcher never ticked while the publish was held"
    assert len(injected) == 1, injected
    output = injected[0].split("Output:", 1)[1]                  # the command line quotes the script itself
    assert '"status": "rewritten"' in output and '"status": "settled"' not in output, injected[0]


@pytest.mark.asyncio
async def test_prune_during_publish(monkeypatch, tmp_path):
    """A finished session older than the TTL whose publish is still running is not pruned by a concurrent spawn: the
    registry keeps the same object (no reload), the watcher sends nothing until the event is set, then exactly one
    completion built from that session; the next prune, with the session published, removes it."""
    import shlex
    import sys

    from tools import process_registry as pr_module
    from tools.process_registry import ProcessRegistry, ProcessSession

    registry = ProcessRegistry()
    monkeypatch.setattr(pr_module, "process_registry", registry)
    session = ProcessSession(id="proc_prunehold", command="echo done", task_id="t-prune", output_buffer="done\n",
                             notify_on_complete=True, started_at=__import__("time").time() - pr_module.FINISHED_TTL_SECONDS - 60)
    registry._running[session.id] = session
    gate, in_publish = threading.Event(), threading.Event()
    real_publish = registry._publish_finished

    def held_publish(s, *a, **k):
        if s is session:
            in_publish.set()
            assert gate.wait(timeout=60), "publish gate never released"
        return real_publish(s, *a, **k)
    monkeypatch.setattr(registry, "_publish_finished", held_publish)
    finisher = threading.Thread(target=registry._finish_exited, args=(session, 0), daemon=True)
    finisher.start()
    assert await asyncio.to_thread(in_publish.wait, 30)
    assert registry._finished.get(session.id) is session and not session._completion_event.is_set()

    other = registry.spawn_local(f"{shlex.quote(sys.executable)} -c pass", task_id="t-prune-other")
    assert registry._finished.get(session.id) is session, "an unpublished session was pruned"
    assert registry.get(session.id) is session, "get returned a reload, not the live session"

    runner = _build_runner(monkeypatch, tmp_path, "concise")
    injected = []
    runner._enqueue_process_completion_notification = AsyncMock(
        side_effect=lambda text, evt: injected.append((text, evt)) or True)
    adapter = runner.adapters[Platform.TELEGRAM]
    held_ticks = []

    def on_tick(n):
        if n <= 5:
            held_ticks.append((n, len(injected), adapter.send.await_count))
        if n == 5:                                    # several ticks while held, fewer than the notice's 12
            gate.set()
            assert session._completion_event.wait(timeout=30), "the publish never set the event"
    _ticking(monkeypatch, on_tick)
    await asyncio.wait_for(runner._run_process_watcher(_agent_watcher() | {"session_id": session.id}), timeout=30)
    finisher.join(timeout=30)
    assert held_ticks == [(n, 0, 0) for n in range(1, 6)], held_ticks
    assert not adapter.send.await_count, adapter.send.await_args_list
    assert len(injected) == 1 and injected[0][1]["session_id"] == session.id, injected
    assert "done" in injected[0][0]
    await asyncio.to_thread(lambda: other._reader_thread.join(timeout=30))
    with registry._lock:
        registry._prune_if_needed()
    assert session.id not in registry._finished, "a published, aged session is pruned as usual"
