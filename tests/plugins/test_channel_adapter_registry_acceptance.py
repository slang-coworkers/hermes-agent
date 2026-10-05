"""Hermetic acceptance test for CH-F49 — Channel adapter registry / Chat SDK bridge (ADOPT).

Proves the native Hermes platform registry adopts the NanoClaw "channel adapter
registry / Chat SDK bridge" behaviour: a ``kind: platform`` plugin dropped under
``$HERMES_HOME/plugins/`` self-registers a channel adapter through
``ctx.register_platform`` with zero core edits; the registry then serves that
adapter (look-up by name, factory construction); and ``BasePlatformAdapter``
supplies the interactive text fallback a new channel inherits for free.
"""
import asyncio
import textwrap
import types
from pathlib import Path

import pytest
import yaml

from gateway.platform_registry import platform_registry
from hermes_cli.plugins import PluginManager

PLUGIN_KEY = "channel-adapter-registry-probe"
PLATFORM_NAME = "registry_probe"

_PLUGIN_YAML = textwrap.dedent(
    """\
    name: channel-adapter-registry-probe
    version: "0.1.0"
    description: Fixture channel adapter that self-registers via ctx.register_platform.
    kind: platform
    provides_tools: []
    provides_hooks: []
    """
)

_INIT_PY = textwrap.dedent(
    '''\
    from gateway.platforms.base import BasePlatformAdapter, SendResult


    class _ProbeAdapter(BasePlatformAdapter):
        """Minimal channel adapter: no heavy SDK, overrides nothing but the abstract API."""

        def __init__(self, config):
            # BasePlatformAdapter.name is a read-only property and its __init__
            # needs a Platform enum member; the exercised send_clarify -> send
            # path touches neither, so set only what the test reads and skip
            # super().__init__.
            self.config = config
            self.captured = []

        async def connect(self, *, is_reconnect: bool = False) -> bool:
            return True

        async def disconnect(self) -> None:
            return None

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            self.captured.append(content)
            return SendResult(success=True, message_id="probe")

        async def get_chat_info(self, chat_id):
            return {}


    def register(ctx):
        ctx.register_platform(
            name="registry_probe",
            label="Registry Probe",
            adapter_factory=lambda cfg: _ProbeAdapter(cfg),
            check_fn=lambda: True,
        )
    '''
)


@pytest.fixture
def probe_manager(tmp_path, monkeypatch):
    """Load the fixture platform plugin from an isolated HERMES_HOME via real discovery."""
    hermes_home = tmp_path / "hermes-home"
    plugin_dir = hermes_home / "plugins" / PLUGIN_KEY
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(_PLUGIN_YAML, encoding="utf-8")
    (plugin_dir / "__init__.py").write_text(_INIT_PY, encoding="utf-8")
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": [PLUGIN_KEY]}}), encoding="utf-8"
    )
    empty_bundled = tmp_path / "bundled-plugins"
    empty_bundled.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(empty_bundled))

    manager = PluginManager()
    manager.discover_and_load()
    try:
        yield manager
    finally:
        # platform_registry is process-global and NOT reset by conftest.
        platform_registry.unregister(PLATFORM_NAME)


def test_ac_ch_f49_1(probe_manager):
    """A kind: platform plugin under $HERMES_HOME/plugins listed in plugins.enabled self-registers its adapter with zero core edits; platform_registry.get(<name>) returns a PlatformEntry with the plugin's label, plugin_name and source=='plugin', and is_registered(<name>) is True."""
    loaded = probe_manager._plugins[PLUGIN_KEY]
    assert loaded.enabled is True
    assert loaded.error is None
    assert loaded.module is not None

    entry = platform_registry.get(PLATFORM_NAME)
    assert entry is not None
    assert entry.label == "Registry Probe"
    assert entry.plugin_name == PLUGIN_KEY
    assert entry.source == "plugin"
    assert platform_registry.is_registered(PLATFORM_NAME) is True


def test_ac_ch_f49_2(probe_manager):
    """The registry serves the registered adapter behaviourally: create_adapter(<name>, cfg) invokes the plugin-supplied adapter_factory and returns an instance of the plugin's BasePlatformAdapter subclass."""
    adapter = platform_registry.create_adapter(PLATFORM_NAME, types.SimpleNamespace())
    assert adapter is not None
    probe_adapter_cls = probe_manager._plugins[PLUGIN_KEY].module._ProbeAdapter
    assert isinstance(adapter, probe_adapter_cls)


def test_ac_ch_f49_3(probe_manager):
    """A channel adapter that overrides none of the clarify UX inherits BasePlatformAdapter.send_clarify's numbered-text fallback: the delivered message numbers each choice and instructs the user to reply with the number."""
    adapter = platform_registry.create_adapter(PLATFORM_NAME, types.SimpleNamespace())
    assert adapter is not None
    asyncio.run(
        adapter.send_clarify("chat1", "Pick one", ["Apple", "Banana"], "clr-1", "sess-1")
    )
    assert adapter.captured, "send_clarify should have delivered a fallback message"
    text = adapter.captured[-1]
    assert "1. Apple" in text
    assert "2. Banana" in text
    assert "reply with the number" in text.lower()


def test_ac_ch_f49_4():
    """The NanoClaw-parity documentation section maps the channel adapter registry / Chat SDK bridge onto the Hermes platform registry and cites the Hermes surface (gateway/platform_registry.py, register_platform, BasePlatformAdapter) in tag:/main: form."""
    doc = (
        Path(__file__).parents[2]
        / "website/docs/developer-guide/adding-platform-adapters.md"
    ).read_text(encoding="utf-8")
    idx = doc.find("## NanoClaw parity: channel adapter registry")
    assert idx != -1, "parity section heading missing from adding-platform-adapters.md"
    section = doc[idx:]
    for needle in (
        "gateway/platform_registry.py",
        "register_platform",
        "BasePlatformAdapter",
        "tag:",
        "main:",
    ):
        assert needle in section, f"parity section must cite {needle!r}"
