"""Tests for plugin-triggered turns in existing gateway sessions."""

import asyncio
import concurrent.futures
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    PlatformConfig,
)
from gateway.run import GatewayRunner
from gateway.session import SessionEntry, SessionSource, SessionStore, build_session_key
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest


def _entry(*, origin=True) -> SessionEntry:
    source = None
    if origin:
        source = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="42",
            chat_type="dm",
            user_id="42",
            user_name="tester",
        )
    now = datetime.now()
    return SessionEntry(
        session_key="agent:main:telegram:dm:42",
        session_id="session-42",
        created_at=now,
        updated_at=now,
        origin=source,
        platform=Platform.TELEGRAM,
    )


def _runner(entry: SessionEntry | None, adapter=None) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.session_store = SimpleNamespace()
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store, lookup_by_session_key=AsyncMock(return_value=entry)
    )
    runner.adapters = {Platform.TELEGRAM: adapter} if adapter else {}
    runner._profile_adapters = {}
    runner._running = True
    runner._draining = False
    runner._background_tasks = set()
    runner._is_user_authorized = MagicMock(return_value=True)
    return runner


class _RoutingAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="test"), Platform.TELEGRAM)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        raise AssertionError("network send is not expected")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id, "type": "dm"}


@pytest.mark.asyncio
async def test_plugin_context_routes_through_live_gateway_to_existing_session(
    tmp_path,
    monkeypatch,
):
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({
            "plugins": {"entries": {"notify-plugin": {"allow_gateway_injection": True}}}
        })
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    source = _entry().origin
    entry = store.get_or_create_session(source)
    adapter = _RoutingAdapter()
    adapter.set_message_handler(AsyncMock())
    adapter._active_sessions[entry.session_key] = asyncio.Event()
    pending_user_event = MessageEvent(
        text="human follow-up",
        message_type=MessageType.PHOTO,
        source=source,
        media_urls=["human.jpg"],
        media_types=["image/jpeg"],
    )
    adapter._pending_messages[entry.session_key] = pending_user_event

    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._profile_adapters = {}
    runner._gateway_loop = asyncio.get_running_loop()
    runner._running = True
    runner._draining = False
    runner._background_tasks = set()
    runner._queued_events = {}
    runner._is_user_authorized = MagicMock(return_value=True)
    adapter.set_busy_session_handler(runner._handle_active_session_busy_message)

    manager = PluginManager()
    context = PluginContext(
        PluginManifest(name="notify-plugin", key="notify-plugin", source="user"),
        manager,
    )

    with patch("hermes_cli.plugins.get_plugin_manager", return_value=manager):
        runner._install_plugin_message_injector()
        assert (
            context.inject_message(
                "/approve always",
                session_key=entry.session_key,
            )
            is True
        )
        task = next(iter(runner._background_tasks))
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)

        assert adapter._pending_messages[entry.session_key] is pending_user_event
        queued = runner._queued_events[entry.session_key][0]
        assert pending_user_event.text == "human follow-up"
        assert pending_user_event.media_urls == ["human.jpg"]
        assert pending_user_event.allow_gateway_control is True
        assert queued.text == "/approve always"
        assert queued.allow_gateway_control is False
        assert queued.metadata["gateway_session_id"] == entry.session_id
        adapter._message_handler.assert_not_awaited()

        runner._clear_plugin_message_injector()
        assert manager.has_gateway_message_injector is False


@pytest.mark.asyncio
async def test_dispatch_uses_stored_origin_and_adapter_message_path():
    adapter = SimpleNamespace(handle_message=AsyncMock())
    entry = _entry()
    runner = _runner(entry, adapter)

    accepted = await runner._dispatch_plugin_message_injection(
        session_key=entry.session_key,
        content="check the deployment",
        plugin_id="notify-plugin",
    )

    assert accepted is True
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.text == "check the deployment"
    assert event.internal is True
    assert event.allow_gateway_control is False
    assert event.get_command() is None
    assert event.source == entry.origin
    assert event.source is not entry.origin
    runner._is_user_authorized.assert_called_once_with(
        event.source,
        allow_adapter_delegation=False,
    )
    assert event.metadata == {
        "hermes_plugin_id": "notify-plugin",
        "hermes_plugin_injection": True,
        "gateway_session_key": entry.session_key,
        "gateway_session_id": entry.session_id,
        "gateway_session_strict": True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entry", "with_adapter"),
    [
        (None, True),
        (_entry(origin=False), True),
        (_entry(), False),
    ],
)
async def test_dispatch_rejects_unroutable_session(entry, with_adapter):
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(entry, adapter if with_adapter else None)

    accepted = await runner._dispatch_plugin_message_injection(
        session_key="agent:main:telegram:dm:42",
        content="wake up",
        plugin_id="notify-plugin",
    )

    assert accepted is False
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [False, True])
async def test_dispatch_rechecks_current_authorization(raises):
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(_entry(), adapter)
    if raises:
        runner._is_user_authorized.side_effect = RuntimeError("config unavailable")
    else:
        runner._is_user_authorized.return_value = False

    accepted = await runner._dispatch_plugin_message_injection(
        session_key="agent:main:telegram:dm:42",
        content="wake up",
        plugin_id="notify-plugin",
    )

    assert accepted is False
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_rejects_stored_role_only_authorization(monkeypatch):
    """A stored adapter role grant must be revalidated against current core auth."""
    for key in (
        "DISCORD_ALLOWED_USERS",
        "DISCORD_ALLOW_ALL_USERS",
        "GATEWAY_ALLOWED_USERS",
        "GATEWAY_ALLOW_ALL_USERS",
    ):
        monkeypatch.delenv(key, raising=False)

    adapter = MagicMock(spec=BasePlatformAdapter)
    adapter.handle_message = AsyncMock()
    entry = _entry()
    entry.session_key = "agent:main:discord:dm:42"
    entry.platform = Platform.DISCORD
    source = entry.origin
    assert source is not None
    source.platform = Platform.DISCORD
    source.role_authorized = True

    runner = _runner(entry)
    runner.adapters = {Platform.DISCORD: adapter}
    runner.config = GatewayConfig()
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    del runner._is_user_authorized

    accepted = await runner._dispatch_plugin_message_injection(
        session_key=entry.session_key,
        content="wake up",
        plugin_id="notify-plugin",
    )

    assert accepted is False
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_stops_when_gateway_drains_during_lookup():
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner = _runner(_entry(), adapter)
    lookup_started = asyncio.Event()
    release_lookup = asyncio.Event()

    async def _lookup(_session_key):
        lookup_started.set()
        await release_lookup.wait()
        return _entry()

    runner._async_session_store.lookup_by_session_key = _lookup
    dispatch = asyncio.create_task(
        runner._dispatch_plugin_message_injection(
            session_key="agent:main:telegram:dm:42",
            content="wake up",
            plugin_id="notify-plugin",
        )
    )

    await lookup_started.wait()
    runner._draining = True
    release_lookup.set()

    assert await dispatch is False
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_base_adapter_queues_non_control_plugin_text_for_exact_session():
    adapter = _RoutingAdapter()
    adapter.set_message_handler(AsyncMock())
    source = _entry().origin
    session_key = build_session_key(source)
    adapter._active_sessions[session_key] = asyncio.Event()
    event = MessageEvent(
        text="/approve always",
        message_type=MessageType.TEXT,
        source=source,
        internal=True,
        allow_gateway_control=False,
        metadata={"gateway_session_key": session_key},
    )

    await adapter.handle_message(event)

    adapter._message_handler.assert_not_awaited()
    assert adapter._pending_messages[session_key] is event
    assert adapter._active_sessions[session_key].is_set() is False


@pytest.mark.asyncio
async def test_base_adapter_rejects_derived_session_mismatch():
    adapter = _RoutingAdapter()
    adapter.set_message_handler(AsyncMock())
    event = MessageEvent(
        text="ordinary input",
        source=_entry().origin,
        internal=True,
        allow_gateway_control=False,
        metadata={"gateway_session_key": "agent:main:telegram:dm:other"},
    )

    await adapter.handle_message(event)

    adapter._message_handler.assert_not_awaited()
    assert adapter._active_sessions == {}


@pytest.mark.asyncio
async def test_scheduler_submits_dispatch_on_live_gateway_loop():
    runner = _runner(_entry())
    runner._gateway_loop = asyncio.get_running_loop()
    runner._dispatch_plugin_message_injection = AsyncMock(return_value=True)

    assert (
        runner._schedule_plugin_message_injection(
            session_key="agent:main:telegram:dm:42",
            content="wake up",
            plugin_id="notify-plugin",
        )
        is True
    )

    await asyncio.sleep(0)
    runner._dispatch_plugin_message_injection.assert_awaited_once_with(
        session_key="agent:main:telegram:dm:42",
        content="wake up",
        plugin_id="notify-plugin",
    )


@pytest.mark.asyncio
async def test_scheduler_ignores_same_loop_task_cancellation():
    runner = _runner(_entry())
    loop = asyncio.get_running_loop()
    runner._gateway_loop = loop
    callback_errors = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: callback_errors.append(context))

    blocker = asyncio.Event()

    async def _wait_for_cancellation(**_kwargs):
        await blocker.wait()

    runner._dispatch_plugin_message_injection = _wait_for_cancellation

    try:
        assert (
            runner._schedule_plugin_message_injection(
                session_key="key",
                content="wake up",
                plugin_id="notify-plugin",
            )
            is True
        )

        task = next(iter(runner._background_tasks))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert callback_errors == []


@pytest.mark.asyncio
async def test_scheduler_logs_async_failure_without_callback_error(caplog):
    runner = _runner(_entry())
    loop = asyncio.get_running_loop()
    runner._gateway_loop = loop
    callback_errors = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: callback_errors.append(context))
    runner._dispatch_plugin_message_injection = AsyncMock(
        side_effect=RuntimeError("adapter failed")
    )

    try:
        assert (
            runner._schedule_plugin_message_injection(
                session_key="key",
                content="wake up",
                plugin_id="notify-plugin",
            )
            is True
        )
        task = next(iter(runner._background_tasks))
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert callback_errors == []
    assert "plugin=notify-plugin session=key" in caplog.text


def test_scheduler_uses_threadsafe_bridge_outside_gateway_loop():
    runner = _runner(_entry())
    loop = MagicMock()
    loop.is_closed.return_value = False
    runner._gateway_loop = loop

    def _submit(coro, target_loop, **_kwargs):
        assert target_loop is loop
        coro.close()
        future = concurrent.futures.Future()
        future.set_result(True)
        return future

    with patch("gateway.run.safe_schedule_threadsafe", side_effect=_submit) as submit:
        assert (
            runner._schedule_plugin_message_injection(
                session_key="key",
                content="wake up",
                plugin_id="notify-plugin",
            )
            is True
        )

    submit.assert_called_once()


def test_scheduler_ignores_threadsafe_future_cancellation():
    runner = _runner(_entry())
    loop = MagicMock()
    loop.is_closed.return_value = False
    runner._gateway_loop = loop

    def _submit(coro, _target_loop, **_kwargs):
        coro.close()
        future = concurrent.futures.Future()
        future.cancel()
        return future

    with (
        patch("gateway.run.safe_schedule_threadsafe", side_effect=_submit),
        patch("gateway.run.logger.warning") as warning,
    ):
        assert (
            runner._schedule_plugin_message_injection(
                session_key="key",
                content="wake up",
                plugin_id="notify-plugin",
            )
            is True
        )

    warning.assert_not_called()


def test_scheduler_rejects_stopped_or_closed_gateway():
    runner = _runner(_entry())
    loop = MagicMock()
    loop.is_closed.return_value = False
    runner._gateway_loop = loop
    runner._running = False

    assert (
        runner._schedule_plugin_message_injection(
            session_key="key",
            content="wake up",
            plugin_id="notify-plugin",
        )
        is False
    )
    loop.call_soon_threadsafe.assert_not_called()

    runner._running = True
    runner._gateway_loop = None
    assert (
        runner._schedule_plugin_message_injection(
            session_key="key",
            content="wake up",
            plugin_id="notify-plugin",
        )
        is False
    )

    runner._gateway_loop = loop
    loop.is_closed.return_value = True
    assert (
        runner._schedule_plugin_message_injection(
            session_key="key",
            content="wake up",
            plugin_id="notify-plugin",
        )
        is False
    )
    loop.call_soon_threadsafe.assert_not_called()


def test_scheduler_rejects_submission_failure():
    runner = _runner(_entry())
    loop = MagicMock()
    loop.is_closed.return_value = False
    runner._gateway_loop = loop

    def _reject(coro, _target_loop, **_kwargs):
        coro.close()
        return None

    with patch("gateway.run.safe_schedule_threadsafe", side_effect=_reject):
        assert (
            runner._schedule_plugin_message_injection(
                session_key="key",
                content="wake up",
                plugin_id="notify-plugin",
            )
            is False
        )


def test_install_and_clear_gateway_injector_preserves_newer_owner():
    runner = _runner(_entry())
    manager = PluginManager()

    with patch("hermes_cli.plugins.get_plugin_manager", return_value=manager):
        runner._install_plugin_message_injector()
        assert manager.has_gateway_message_injector is True

        runner._clear_plugin_message_injector()
        assert manager.has_gateway_message_injector is False

        runner._install_plugin_message_injector()

        newer_owner = MagicMock()
        newer_injector = MagicMock(return_value=True)
        manager.set_gateway_message_injector(newer_owner, newer_injector)
        runner._clear_plugin_message_injector()

    assert manager.has_gateway_message_injector is True
    assert manager.inject_gateway_message(value="kept") is True
    newer_injector.assert_called_once_with(value="kept")


# ---------------------------------------------------------------------------
# W2 — durable cross-profile notification delivery (GOV-F25 mandatory core proof)
#
# The nv-artifact delivery path durably enqueues a `notification` on the owning
# card and relies on the kanban notifier's durable-retry wake to resume the
# OWNING (possibly SECONDARY-profile) session via the api_server profile-scoped
# mirror, with the cursor advancing only on a persist-confirmed ack. These tests
# drive the real notifier method _deliver_durable_notifications against a real
# temp kanban.db, with deliver_wake mocked to simulate the ack outcomes.
# ---------------------------------------------------------------------------

OWNER_SESS = "gov-f25-owner-sess-0"
OWNER_PROFILE = "gov-f25-owner"


def _mk_task_and_durable_sub():
    import hermes_cli.kanban_db as kb

    conn = kb.connect()
    try:
        task = kb.create_task(
            conn, title="gov-f25/repo#41", assignee=OWNER_PROFILE,
            idempotency_key="gh-pr-gov-f25-41",
        )
        task_id = task if isinstance(task, str) else getattr(task, "id", task)
        kb.add_notify_sub(
            conn, task_id=task_id, platform="api_server", chat_id=OWNER_SESS,
            notifier_profile=OWNER_PROFILE, delivery_mode="wake", retry_policy="durable",
        )
    finally:
        conn.close()
    return task_id


def _publish(task_id, message, key):
    import hermes_cli.kanban_db as kb

    conn = kb.connect()
    try:
        kb.publish_task_notification(conn, task_id, message, metadata={"idempotency_key": key})
    finally:
        conn.close()


def _peek_delivery(task_id, board=None):
    import hermes_cli.kanban_db as kb

    conn = kb.connect(board=board)
    try:
        sub = kb.list_notify_subs(conn, task_id=task_id)[0]
        _cur, events = kb.unseen_events_for_sub(
            conn, task_id=sub["task_id"], platform=sub["platform"],
            chat_id=sub["chat_id"], thread_id=sub.get("thread_id") or "", kinds=None,
        )
        task = kb.get_task(conn, task_id)
    finally:
        conn.close()
    return {"sub": sub, "events": events, "task": task, "board": board, "durable": True}


def _cursor(task_id):
    import hermes_cli.kanban_db as kb

    conn = kb.connect()
    try:
        sub = kb.list_notify_subs(conn, task_id=task_id)[0]
        return int(sub.get("last_event_id") or 0)
    finally:
        conn.close()


def _sub_exists(task_id):
    import hermes_cli.kanban_db as kb

    conn = kb.connect()
    try:
        return bool(kb.list_notify_subs(conn, task_id=task_id))
    finally:
        conn.close()


def _durable_runner():
    runner = object.__new__(GatewayRunner)
    # The shared api_server adapter carries the bind coords; the profile scoping
    # is done by owner_profile on the self-post, so the adapter itself is opaque.
    runner.adapters = {Platform.API_SERVER: SimpleNamespace(
        _host="127.0.0.1", _port=8642, _api_key="k", _model_name="hermes-agent",
        supports_async_delivery=False,  # api_server is non-push → self-post branch
    )}
    runner._authorization_adapter = MagicMock(return_value=None)
    runner._kanban_sub_fail_counts = {}
    return runner


@pytest.mark.asyncio
async def test_durable_wake_targets_secondary_owner_and_advances_on_persist_ack(monkeypatch):
    """A persist-confirmed durable wake resumes the SECONDARY owner's session
    (never the default profile) and advances the cursor exactly once."""
    task_id = _mk_task_and_durable_sub()
    _publish(task_id, "review submitted on o/r#41 @beefcafe", "o/r#41:D1")

    calls = []

    async def _ok(adapter, *, text, session_id, owner_profile=None, idempotency_key=None, require_persist_ack=None):
        calls.append({"session_id": session_id, "owner_profile": owner_profile,
                      "idempotency_key": idempotency_key, "text": text})

    monkeypatch.setattr("gateway.wake.deliver_wake", _ok)

    runner = _durable_runner()
    await runner._deliver_durable_notifications(_peek_delivery(task_id))

    assert len(calls) == 1
    # (i) resumes the OWNER's session via the profile-scoped mirror.
    assert calls[0]["session_id"] == OWNER_SESS
    assert calls[0]["owner_profile"] == OWNER_PROFILE
    # (ii) never routed to the default profile (owner_profile is explicit).
    assert calls[0]["owner_profile"] not in (None, "", "default")
    assert calls[0]["idempotency_key"] == "o/r#41:D1"
    assert "review submitted on o/r#41 @beefcafe" in calls[0]["text"]
    # (iii) persist-ack confirmed (mock returned) → event consumed (SEEN), so a
    # re-peek returns nothing.
    assert _peek_delivery(task_id)["events"] == []


@pytest.mark.asyncio
async def test_durable_wake_no_persist_ack_leaves_cursor_unmoved(monkeypatch):
    """A wake whose response did NOT confirm persistence (deliver_wake raises)
    must NOT advance the cursor and must NOT drop the sub."""
    task_id = _mk_task_and_durable_sub()
    _publish(task_id, "review submitted", "o/r#41:D1")

    async def _no_ack(adapter, *, text, session_id, owner_profile=None, idempotency_key=None, require_persist_ack=None):
        raise RuntimeError("X-Hermes-Turn-Persisted not true")

    monkeypatch.setattr("gateway.wake.deliver_wake", _no_ack)
    runner = _durable_runner()
    await runner._deliver_durable_notifications(_peek_delivery(task_id))

    # (iii) the poison event was NOT consumed — still unseen for redelivery.
    assert len(_peek_delivery(task_id)["events"]) == 1
    assert _sub_exists(task_id)           # (W2d) never dropped


@pytest.mark.asyncio
async def test_durable_crash_before_ack_redelivers_then_no_rerun(monkeypatch):
    """(iv) A crash before the ack leaves the cursor unmoved → the event is
    RE-DELIVERED on the next tick; after a true ack the cursor has advanced, so
    a later tick does NOT re-run the delivered turn. (v) The retry reuses the
    SAME idempotency key so the server can dedup."""
    task_id = _mk_task_and_durable_sub()
    _publish(task_id, "review submitted", "o/r#41:D1")

    keys = []

    async def _first_fails(adapter, *, text, session_id, owner_profile=None, idempotency_key=None, require_persist_ack=None):
        keys.append(idempotency_key)
        raise RuntimeError("crash before persist ack")

    monkeypatch.setattr("gateway.wake.deliver_wake", _first_fails)
    runner = _durable_runner()
    await runner._deliver_durable_notifications(_peek_delivery(task_id))
    # Crash before the ack → event still unseen (will be re-delivered).
    assert len(_peek_delivery(task_id)["events"]) == 1

    # Next tick: clear the backoff window and let the wake persist this time.
    runner._kanban_durable_backoff.clear()

    async def _ok(adapter, *, text, session_id, owner_profile=None, idempotency_key=None, require_persist_ack=None):
        keys.append(idempotency_key)

    monkeypatch.setattr("gateway.wake.deliver_wake", _ok)
    await runner._deliver_durable_notifications(_peek_delivery(task_id))
    # Now persisted → consumed.
    assert _peek_delivery(task_id)["events"] == []
    # (v) the ambiguous retry reused the stable per-event key.
    assert keys == ["o/r#41:D1", "o/r#41:D1"]

    # A later tick with no new events must not re-run the delivered turn.
    later_calls = []

    async def _spy(adapter, *, text, session_id, owner_profile=None, idempotency_key=None, require_persist_ack=None):
        later_calls.append(idempotency_key)

    monkeypatch.setattr("gateway.wake.deliver_wake", _spy)
    await runner._deliver_durable_notifications(_peek_delivery(task_id))
    assert later_calls == []
    assert _peek_delivery(task_id)["events"] == []


@pytest.mark.asyncio
async def test_durable_delivers_events_in_cursor_order(monkeypatch):
    """Cursor-contiguity: multiple notification events are delivered in ascending
    id order and the cursor advances past all of them."""
    task_id = _mk_task_and_durable_sub()
    _publish(task_id, "first", "o/r#41:D1")
    _publish(task_id, "second", "o/r#41:D2")

    seen = []

    async def _ok(adapter, *, text, session_id, owner_profile=None, idempotency_key=None, require_persist_ack=None):
        seen.append(idempotency_key)

    monkeypatch.setattr("gateway.wake.deliver_wake", _ok)
    runner = _durable_runner()
    await runner._deliver_durable_notifications(_peek_delivery(task_id))

    assert seen == ["o/r#41:D1", "o/r#41:D2"]
    # Both delivered → cursor past the last, so a re-peek is empty.
    assert _peek_delivery(task_id)["events"] == []


@pytest.mark.asyncio
async def test_durable_never_dropped_and_alerts_on_sustained_failure(monkeypatch, caplog):
    """(W2d) A poison delivery is retried with backoff, NEVER drops the sub, never
    advances the cursor, and earns a loud operator alert on sustained failure."""
    import logging

    task_id = _mk_task_and_durable_sub()
    _publish(task_id, "review submitted", "o/r#41:D1")

    async def _always_fail(adapter, *, text, session_id, owner_profile=None, idempotency_key=None, require_persist_ack=None):
        raise RuntimeError("kanban owner unreachable")

    monkeypatch.setattr("gateway.wake.deliver_wake", _always_fail)
    runner = _durable_runner()

    caplog.set_level(logging.ERROR, logger="gateway.kanban_watchers")
    # Drive enough attempts to cross the alert threshold, bypassing the backoff
    # window between attempts (a real deployment waits it out across ticks).
    for _ in range(6):
        await runner._deliver_durable_notifications(_peek_delivery(task_id))
        runner._kanban_durable_backoff.clear()

    # never advanced past the poison event → still unseen for redelivery.
    assert len(_peek_delivery(task_id)["events"]) == 1
    assert _sub_exists(task_id)           # never dropped
    assert any("sustained delivery failure" in r.message for r in caplog.records)


def _mk_task_and_durable_push_sub():
    import hermes_cli.kanban_db as kb

    conn = kb.connect()
    try:
        task = kb.create_task(
            conn, title="push-durable", assignee="p", idempotency_key="push-durable-1",
        )
        task_id = task if isinstance(task, str) else getattr(task, "id", task)
        kb.add_notify_sub(
            conn, task_id=task_id, platform="telegram", chat_id="tg-chat-1",
            notifier_profile="p", chat_type="group",
            delivery_mode="wake", retry_policy="durable",
        )
    finally:
        conn.close()
    return task_id


def _durable_push_runner(*, owner_adapter):
    runner = object.__new__(GatewayRunner)
    default_push = SimpleNamespace()  # no supports_async_delivery → push-capable
    runner.adapters = {Platform.TELEGRAM: default_push}
    runner._authorization_adapter = MagicMock(return_value=owner_adapter)
    runner._kanban_sub_fail_counts = {}
    runner._default_push_adapter = default_push
    return runner


@pytest.mark.asyncio
async def test_durable_push_sub_is_retained_not_advanced(monkeypatch):
    """A durable sub on a PUSH transport cannot be confirmed delivered: a push
    wake (handle_message) returns after merely SPAWNING the turn, so advancing the
    cursor on its return would drop the event if that turn later failed. The
    durable path therefore refuses to deliver on push — it never calls
    deliver_wake, retains the event (cursor unmoved), never drops the sub, and
    records a failure for backoff/alert. (The nv-artifact plugin only registers
    api_server durable subs; this is the defensive branch.)"""
    task_id = _mk_task_and_durable_push_sub()
    _publish(task_id, "push note", "push:1")

    calls = []

    async def _spy(adapter, **kw):
        calls.append(kw)

    monkeypatch.setattr("gateway.wake.deliver_wake", _spy)
    push_adapter = SimpleNamespace()  # owner's own push adapter (present)
    runner = _durable_push_runner(owner_adapter=push_adapter)
    await runner._deliver_durable_notifications(_peek_delivery(task_id))

    assert calls == []                                    # never attempts a push wake
    assert len(_peek_delivery(task_id)["events"]) == 1    # retained (cursor unmoved)
    assert _sub_exists(task_id)                           # never dropped
    assert task_id in {k[0] for k in runner._kanban_durable_backoff}  # failure recorded


@pytest.mark.asyncio
async def test_durable_push_absent_owner_adapter_never_uses_default(monkeypatch):
    """Fail-closed profile resolution: when a push owner has NO adapter of its
    own, the durable path must NOT fall back to the shared/default push adapter
    (that would deliver to the wrong profile). It records a failure and retains
    the event; default-adapter fallback is allowed ONLY for api_server, whose
    profile scoping rides on owner_profile, not on the adapter."""
    task_id = _mk_task_and_durable_push_sub()
    _publish(task_id, "push note", "push:1")

    calls = []

    async def _spy(adapter, **kw):
        calls.append(adapter)

    monkeypatch.setattr("gateway.wake.deliver_wake", _spy)
    runner = _durable_push_runner(owner_adapter=None)  # owner has no adapter
    await runner._deliver_durable_notifications(_peek_delivery(task_id))

    assert calls == []                                    # default adapter never used
    assert len(_peek_delivery(task_id)["events"]) == 1    # retained
    assert _sub_exists(task_id)                           # never dropped


# ---------------------------------------------------------------------------
# W2 — REAL cross-profile persisted-resume proof (GOV-F25 mandatory core test,
# ADR §"Core test — the cross-profile persisted-resume proof").
#
# The production chain runs for real: the profile-prefix middleware resolves
# /p/<owner>/, _handle_chat_completions → _run_agent enters the REAL profile
# runtime scope, the turn persists to profiles/<owner>/state.db, the server emits
# the REAL X-Hermes-Turn-Persisted header, deliver_wake's persist-ack gate reads
# it, and the durable notifier advances its cursor only on that ack. The test
# doubles are the model agent (_create_agent → a fake whose run_conversation
# persists a real message row via SessionDB, since a live model cannot run
# hermetically), secret lookup, and adapter lookup; the fake writes under the
# middleware-set profile scope, so WHICH state.db it lands in is decided by
# production code, not the stub. Assertions (i)-(v) map to the ADR's core-test
# clause. Reuses the ephemeral-loopback-server pattern from test_wake_delivery.
# ---------------------------------------------------------------------------

REAL_OWNER = "gov-f25-owner"
REAL_OWNER_SESS = "gov-f25-owner-sess-real"
# A strong, profile-scoped API_SERVER_KEY (>=16 chars, high entropy) — a named
# /p/<profile>/ request fails closed without one (api_server._expected_api_key).
_WAKE_KEY = "b7e2c9a14f6803d5e9c2f8a1b4d70e6392c5a8f1d0e3b6c9"


class _RealPersistAgent:
    """Stand-in for the model turn: persists ONE real assistant message to the
    profile-scoped state.db (resolved under the live profile runtime scope), and
    reports turn_persisted from a shared knob so a test can force a non-persisted
    turn (bare 200, no ack)."""

    def __init__(self, session_id, state):
        self.session_id = session_id
        self._state = state
        self.session_prompt_tokens = 1
        self.session_completion_tokens = 1
        self.session_total_tokens = 2

    def run_conversation(self, user_message, conversation_history=None, task_id=None, **kw):
        from pathlib import Path as _P

        from hermes_constants import get_hermes_home
        from hermes_state import SessionDB

        self._state["calls"].append(self.session_id)
        persisted = bool(self._state["persist"])
        if persisted:
            db = SessionDB(_P(get_hermes_home()) / "state.db")
            try:
                db.create_session(self.session_id, source="api_server")
            except Exception:
                pass  # already exists on a redelivery
            db.append_message(self.session_id, role="assistant", content="ack: " + user_message)
        return {"final_response": "ack", "session_id": self.session_id,
                "turn_persisted": persisted}

    def interrupt(self):  # pragma: no cover - control hook the server may call
        pass


def _message_count(home, session_id) -> int:
    from hermes_state import SessionDB

    db_path = home / "state.db"
    if not db_path.exists():
        return 0
    return len(SessionDB(db_path).get_messages_as_conversation(session_id))


def _build_wake_adapter(tmp_path, monkeypatch, state):
    """Wire a REAL APIServerAdapter (multiplex on) + its real app + a persisting
    stub agent, in an isolated HERMES_HOME. Returns (adapter, app, owner_home,
    default_home)."""
    from aiohttp import web

    import agent.secret_scope as _ss
    from gateway.platforms.api_server import APIServerAdapter
    from hermes_cli.profiles import get_profile_dir

    home = tmp_path / "hhome"
    (home / "profiles" / REAL_OWNER).mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))

    runner_stub = SimpleNamespace(config=GatewayConfig(multiplex_profiles=True))
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    adapter._api_key = _WAKE_KEY
    adapter.gateway_runner = runner_stub
    adapter._host = "127.0.0.1"
    adapter._model_name = "hermes-agent"

    # The named /p/<owner>/ request resolves its API key from the profile secret
    # scope; return the same strong key deliver_wake authenticates with.
    monkeypatch.setattr(
        _ss, "get_secret",
        lambda name, default="": _WAKE_KEY if name == "API_SERVER_KEY" else default,
    )
    # The model agent is a double; the persist + profile scope are production code.
    monkeypatch.setattr(
        adapter, "_create_agent",
        lambda **kw: _RealPersistAgent(kw.get("session_id"), state),
    )

    app = web.Application(middlewares=[adapter._make_profile_prefix_middleware()])
    for method, path, handler in adapter._http_route_table():
        app.router.add_route(method, path, handler)
        app.router.add_route(method, f"/p/{{profile}}{path}", handler)
    app["api_server_adapter"] = adapter
    app["gateway_runner"] = runner_stub

    owner_home = get_profile_dir(REAL_OWNER)
    default_home = get_profile_dir("default")
    return adapter, app, owner_home, default_home


async def _serve_app(app):
    from aiohttp import web

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


def _mk_real_durable_sub(session_id=REAL_OWNER_SESS, *, idem="gh-pr-gov-f25-77"):
    """A durable api_server wake sub bound to the owner session, plus its card."""
    import hermes_cli.kanban_db as kb

    conn = kb.connect()
    try:
        task = kb.create_task(conn, title="gov-f25/repo#77", assignee=REAL_OWNER,
                              idempotency_key=idem)
        task_id = task if isinstance(task, str) else getattr(task, "id", task)
        kb.add_notify_sub(conn, task_id=task_id, platform="api_server", chat_id=session_id,
                          notifier_profile=REAL_OWNER, delivery_mode="wake", retry_policy="durable")
    finally:
        conn.close()
    return task_id


def _real_durable_runner(adapter):
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.API_SERVER: adapter}
    # A secondary owner has no adapter of its own → api_server falls back to the
    # shared adapter, scoping by owner_profile on the self-post (never push).
    runner._authorization_adapter = MagicMock(return_value=None)
    runner._kanban_sub_fail_counts = {}
    return runner


def test_real_cross_profile_wake_lands_on_owner_not_default_and_gates_on_ack(tmp_path, monkeypatch):
    """(i) A persist-confirmed durable wake lands a real turn on the SECONDARY
    owner's session in profiles/<owner>/state.db; (ii) the DEFAULT profile's
    state.db receives nothing; (iii) a turn that did NOT persist (bare 200, no
    X-Hermes-Turn-Persisted ack) makes the durable caller RAISE, so its cursor
    would not advance."""
    from gateway.wake import deliver_wake

    state = {"persist": True, "calls": []}
    adapter, app, owner_home, default_home = _build_wake_adapter(tmp_path, monkeypatch, state)

    async def run():
        srv, port = await _serve_app(app)
        adapter._port = port
        try:
            # (i) persist-confirmed → resumes the owner session.
            await deliver_wake(adapter, text="review on o/r#77 [A]", session_id=REAL_OWNER_SESS,
                               owner_profile=REAL_OWNER, idempotency_key="o/r#77:A1",
                               require_persist_ack=True)
            # (iii) a non-persisted turn: bare 200, no ack → durable caller raises.
            state["persist"] = False
            with pytest.raises(RuntimeError, match="persist"):
                await deliver_wake(adapter, text="review on o/r#77 [A2]", session_id=REAL_OWNER_SESS,
                                   owner_profile=REAL_OWNER, idempotency_key="o/r#77:A2",
                                   require_persist_ack=True)
        finally:
            await srv.cleanup()

    asyncio.run(run())

    # (i) exactly one real message landed on the owner session.
    assert _message_count(owner_home, REAL_OWNER_SESS) == 1
    # (ii) the default profile's store never received the owner's turn.
    assert _message_count(default_home, REAL_OWNER_SESS) == 0
    # (iii) the non-persisted turn ran but persisted nothing → still one message.
    assert state["calls"] == [REAL_OWNER_SESS, REAL_OWNER_SESS]


def test_real_notifier_advances_cursor_only_on_persist_ack(tmp_path, monkeypatch):
    """(iii) via the REAL durable notifier: a non-persisted turn leaves the cursor
    unmoved (event redelivered); a persist-confirmed turn advances it. Exactly one
    real owner turn persists across the two attempts."""
    from gateway.wake import deliver_wake  # noqa: F401 - ensure real (unmocked)

    state = {"persist": False, "calls": []}
    adapter, app, owner_home, default_home = _build_wake_adapter(tmp_path, monkeypatch, state)
    task_id = _mk_real_durable_sub(idem="gh-pr-gov-f25-77-B")
    _publish(task_id, "review submitted on o/r#77 [B]", "o/r#77:B1")
    runner = _real_durable_runner(adapter)

    async def run():
        srv, port = await _serve_app(app)
        adapter._port = port
        try:
            # Non-persisted turn → no ack → cursor NOT advanced (event still unseen).
            await runner._deliver_durable_notifications(_peek_delivery(task_id))
            assert len(_peek_delivery(task_id)["events"]) == 1
            # Clear the backoff window, let the turn persist → cursor advances.
            runner._kanban_durable_backoff.clear()
            state["persist"] = True
            await runner._deliver_durable_notifications(_peek_delivery(task_id))
            assert _peek_delivery(task_id)["events"] == []
        finally:
            await srv.cleanup()

    asyncio.run(run())

    # Only the persist-confirmed attempt wrote a message to the owner store.
    assert _message_count(owner_home, REAL_OWNER_SESS) == 1
    assert _message_count(default_home, REAL_OWNER_SESS) == 0


def test_real_crash_before_ack_redelivers_then_no_rerun(tmp_path, monkeypatch):
    """(iv) A crash before the ack leaves the cursor unmoved so the notifier
    RE-DELIVERS; after a true ack the cursor has advanced, so a later tick does
    NOT re-run the already-delivered owner turn."""
    state = {"persist": False, "calls": []}
    adapter, app, owner_home, _default = _build_wake_adapter(tmp_path, monkeypatch, state)
    task_id = _mk_real_durable_sub(idem="gh-pr-gov-f25-77-C")
    _publish(task_id, "review submitted on o/r#77 [C]", "o/r#77:C1")
    runner = _real_durable_runner(adapter)

    async def run():
        srv, port = await _serve_app(app)
        adapter._port = port
        try:
            await runner._deliver_durable_notifications(_peek_delivery(task_id))  # crash-before-ack
            assert len(_peek_delivery(task_id)["events"]) == 1                    # redelivered
            runner._kanban_durable_backoff.clear()
            state["persist"] = True
            await runner._deliver_durable_notifications(_peek_delivery(task_id))  # true ack
            assert _peek_delivery(task_id)["events"] == []
            # A later tick with no new events must NOT re-run the delivered turn.
            calls_before = len(state["calls"])
            await runner._deliver_durable_notifications(_peek_delivery(task_id))
            assert len(state["calls"]) == calls_before
        finally:
            await srv.cleanup()

    asyncio.run(run())
    assert _message_count(owner_home, REAL_OWNER_SESS) == 1  # exactly one persisted turn


def test_real_ambiguous_ack_retry_deduped_to_one_owner_turn(tmp_path, monkeypatch):
    """(v) An ambiguous same-key retry (the turn persisted but the ack was lost
    in transit, so the caller retries under the SAME Idempotency-Key) is deduped
    by the api_server idempotency cache to AT MOST ONE owning turn."""
    from gateway.wake import deliver_wake

    state = {"persist": True, "calls": []}
    adapter, app, owner_home, _default = _build_wake_adapter(tmp_path, monkeypatch, state)

    async def run():
        srv, port = await _serve_app(app)
        adapter._port = port
        try:
            # Two deliveries of the SAME event: same body + same Idempotency-Key.
            for _ in range(2):
                await deliver_wake(adapter, text="review on o/r#77 [D]", session_id=REAL_OWNER_SESS,
                                   owner_profile=REAL_OWNER, idempotency_key="o/r#77:D1",
                                   require_persist_ack=True)
        finally:
            await srv.cleanup()

    asyncio.run(run())
    # The second call hit the completed-result idempotency cache → the agent ran
    # once, so exactly one owner turn persisted.
    assert state["calls"] == [REAL_OWNER_SESS]
    assert _message_count(owner_home, REAL_OWNER_SESS) == 1


# ---------------------------------------------------------------------------
# The persist-ack chain feeding the header the wake gate reads: the commit bool
# from _flush_messages_to_session_db must reach the run_conversation result as
# `turn_persisted` (run_agent._persist_and_drain sets agent._last_turn_persisted;
# agent/turn_finalizer.finalize_turn copies it into the result), which
# api_server then emits as X-Hermes-Turn-Persisted. The real cross-profile tests
# above exercise the header/gate with a persistence-controllable stub; this
# exercises the REAL production propagation with a genuine AIAgent turn.
# ---------------------------------------------------------------------------

def _persist_probe_agent():
    from run_agent import AIAgent

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        agent.client = MagicMock()
        agent._cached_system_prompt = "You are helpful."
        agent._use_prompt_caching = False
        agent.tool_delay = 0
        agent.compression_enabled = False
        agent.save_trajectories = False
        return agent


@pytest.mark.parametrize("committed, expected", [(True, True), (False, False)])
def test_turn_persisted_propagates_from_flush_to_result(committed, expected):
    """A real finish_reason=stop turn: the session-db commit bool propagates to
    the run_conversation result's turn_persisted (this is what api_server reads to
    emit X-Hermes-Turn-Persisted, and what the durable wake gate depends on)."""
    from tests.run_agent.test_run_agent import _mock_response

    agent = _persist_probe_agent()
    agent.client.chat.completions.create.side_effect = [
        _mock_response(content="2 + 2 is 4.", finish_reason="stop"),
    ]
    with (
        patch.object(agent, "_flush_messages_to_session_db", return_value=committed),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("what is 2 + 2?")

    assert result["turn_persisted"] is expected
