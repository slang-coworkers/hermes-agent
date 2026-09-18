"""RT-F09 — Pluggable router seams: hermetic acceptance test.

Loads a real plugin through the plugin discovery path, makes it the active
manager, and exercises Hermes' router seams: the gateway pipeline
(``GatewayRunner._handle_message``) honours a ``pre_gateway_dispatch``
directive (AC-1); the production tool-gate dispatcher
(``_dispatch_pre_tool_call_hooks``) returns the plugin's block message (AC-2);
and the shared production dispatch entry (``lifecycle.invoke_hook``) reaches
the ``on_session_start``/``on_session_end``/``on_session_reset`` lifecycle
callbacks and keys strictly on the underscore hook name, distinct from the
colon-named gateway event bus (AC-3, AC-4).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import yaml
import pytest

import hermes_cli.lifecycle as lifecycle
import hermes_cli.plugins as plugins_mod
from hermes_cli.plugins import PluginManager
from hermes_cli.plugin_dev import doctor_plugin

PLUGIN_KEY = "router-seams-fixture"

FIXTURE_YAML = {
    "name": PLUGIN_KEY,
    "version": "0.1.0",
    "description": "Fixture proving the RT-F09 router seams are pluggable.",
    "provides_hooks": [
        "pre_gateway_dispatch",
        "pre_tool_call",
        "on_session_start",
        "on_session_end",
        "on_session_reset",
    ],
}

# Every callback takes **kwargs so it is doctor-clean and receives the full
# production payload; observers are module globals read back off loaded.module.
FIXTURE_INIT = '''\
pre_gateway_calls = []
seen_sessions = []


def _pre_gateway_dispatch(**kwargs):
    pre_gateway_calls.append("fired")
    return {"action": "skip", "reason": "handled-by-router-seams-fixture"}


def _pre_tool_call(**kwargs):
    return {"action": "block", "message": "blocked by router-seams-fixture"}


def _on_session_start(**kwargs):
    seen_sessions.append(("on_session_start", kwargs.get("session_id")))


def _on_session_end(**kwargs):
    seen_sessions.append(("on_session_end", kwargs.get("session_id")))


def _on_session_reset(**kwargs):
    seen_sessions.append(("on_session_reset", kwargs.get("session_id")))


def register(ctx):
    ctx.register_hook("pre_gateway_dispatch", _pre_gateway_dispatch)
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("on_session_reset", _on_session_reset)
'''


def _clear_auth_env(monkeypatch):
    for key in (
        "TELEGRAM_ALLOWED_USERS", "WHATSAPP_ALLOWED_USERS", "GATEWAY_ALLOWED_USERS",
        "TELEGRAM_ALLOW_ALL_USERS", "WHATSAPP_ALLOW_ALL_USERS", "GATEWAY_ALLOW_ALL_USERS",
    ):
        monkeypatch.delenv(key, raising=False)


def _make_event(text="hello"):
    from gateway.config import Platform
    from gateway.platforms.base import MessageEvent
    from gateway.session import SessionSource

    return MessageEvent(
        text=text,
        message_id="m1",
        source=SessionSource(
            platform=Platform.WHATSAPP,
            user_id="15551234567@s.whatsapp.net",
            chat_id="15551234567@s.whatsapp.net",
            user_name="tester",
            chat_type="dm",
        ),
    )


def _make_runner():
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.WHATSAPP: PlatformConfig(enabled=True)})
    adapter = SimpleNamespace(send=AsyncMock())
    runner.adapters = {Platform.WHATSAPP: adapter}
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    runner.pairing_store._is_rate_limited.return_value = False
    runner.session_store = MagicMock()
    runner._running_agents = {}
    runner._update_prompt_pending = {}
    return runner, adapter


@pytest.fixture
def active_plugin(tmp_path, monkeypatch):
    """Discover + load the fixture from an isolated HERMES_HOME and make it active."""
    hermes_home = tmp_path / "hermes-home"
    plugin_dir = hermes_home / "plugins" / PLUGIN_KEY
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(yaml.safe_dump(FIXTURE_YAML), encoding="utf-8")
    (plugin_dir / "__init__.py").write_text(FIXTURE_INIT, encoding="utf-8")
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": [PLUGIN_KEY]}}), encoding="utf-8"
    )

    empty_bundled = tmp_path / "empty-bundled"
    empty_bundled.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(empty_bundled))

    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled is True
    assert loaded.error is None
    assert loaded.module is not None

    # Doctor-clean on a stock tree (self-isolating temp home + empty bundled dir).
    assert doctor_plugin(str(plugin_dir)).ok

    # Route production dispatch (lifecycle.invoke_hook / _dispatch_pre_tool_call_hooks)
    # through this discovered manager.
    monkeypatch.setattr(plugins_mod, "_plugin_manager", manager)
    return loaded


@pytest.mark.asyncio
async def test_ac_rt_f09_1(active_plugin, monkeypatch):
    """AC-RT-F09-1: the gateway pipeline invokes pre_gateway_dispatch and honours its skip directive (short-circuits before agent dispatch)."""
    assert "pre_gateway_dispatch" in active_plugin.hooks_registered
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", "*")
    runner, adapter = _make_runner()
    dispatched = {"called": False}

    async def _capture(event, source, _quick_key, _run_generation):
        dispatched["called"] = True
        return "ok"

    runner._handle_message_with_agent = _capture
    active_plugin.module.pre_gateway_calls.clear()

    result = await runner._handle_message(_make_event("hello"))

    assert active_plugin.module.pre_gateway_calls == ["fired"]
    assert result is None
    assert dispatched["called"] is False
    adapter.send.assert_not_awaited()


def test_ac_rt_f09_2(active_plugin):
    """AC-RT-F09-2: pre_tool_call is a pluggable gate honoured by the production single-fire dispatcher."""
    assert "pre_tool_call" in active_plugin.hooks_registered
    block_msg, modified_args = plugins_mod._dispatch_pre_tool_call_hooks(
        "write_file", {}, task_id="t1"
    )
    assert block_msg == "blocked by router-seams-fixture"
    assert modified_args is None


def test_ac_rt_f09_3(active_plugin):
    """AC-RT-F09-3: on_session_start/end/reset are reached through the shared production dispatch entry."""
    for hook in ("on_session_start", "on_session_end", "on_session_reset"):
        assert hook in active_plugin.hooks_registered
    active_plugin.module.seen_sessions.clear()
    lifecycle.invoke_hook("on_session_start", session_id="s-start")
    lifecycle.invoke_hook("on_session_end", session_id="s-end")
    lifecycle.invoke_hook("on_session_reset", session_id="s-reset")
    assert active_plugin.module.seen_sessions == [
        ("on_session_start", "s-start"),
        ("on_session_end", "s-end"),
        ("on_session_reset", "s-reset"),
    ]


@pytest.mark.asyncio
async def test_ac_rt_f09_4(active_plugin):
    """AC-RT-F09-4: the gateway HookRegistry bus (session:start) and the plugin on_session_start hook are separate — the gateway event never reaches the plugin callback."""
    from gateway.hooks import HookRegistry

    active_plugin.module.seen_sessions.clear()
    colon_seen = []
    registry = HookRegistry()

    def _record_colon(event_type, context):
        colon_seen.append((event_type, context["session_id"]))

    registry._handlers["session:start"] = [_record_colon]
    await registry.emit("session:start", {"session_id": "colon"})

    assert colon_seen == [("session:start", "colon")]
    assert active_plugin.module.seen_sessions == []

    lifecycle.invoke_hook("on_session_start", session_id="under")
    assert active_plugin.module.seen_sessions == [("on_session_start", "under")]
