"""Unit tests for the RT-F01 render enforcement in nv-coworker-compose.

These complement (do not overlap) the acceptance test: they pin behaviours the
per-criterion drives only exercise for the ``webhook`` platform, so a future
refactor that special-cased ``webhook`` could not silently pass. The plugin is
loaded from an isolated HERMES_HOME with an empty bundled dir through the real
``PluginManager().discover_and_load()`` path, matching the acceptance shape.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_KEY = "nv-coworker-compose"
PLUGIN_SRC = REPO_ROOT / "plugins" / PLUGIN_KEY


@pytest.fixture()
def module(tmp_path, monkeypatch):
    from hermes_cli.plugins import PluginManager

    hermes_home = tmp_path / "hermes_home"
    (hermes_home / "plugins").mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, hermes_home / "plugins" / PLUGIN_KEY)
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": [PLUGIN_KEY]}}), encoding="utf-8"
    )
    bundled = tmp_path / "empty_bundled"
    bundled.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")

    manager = PluginManager()
    manager.discover_and_load()
    entry = manager._plugins[PLUGIN_KEY]
    assert entry.enabled is True and entry.error is None and entry.module is not None
    return entry.module


def _write_spec(spec_dir: Path, spec: dict) -> Path:
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spines").mkdir(exist_ok=True)
    (spec_dir / "spines" / "base.yaml").write_text(
        yaml.safe_dump({"identity": "Base.", "invariants": ["Help."], "config": {}}, sort_keys=False),
        encoding="utf-8",
    )
    wf = spec_dir / "workflows" / "wf"
    wf.mkdir(parents=True, exist_ok=True)
    (wf / "SKILL.md").write_text("# wf\n\nWork.\n", encoding="utf-8")
    path = spec_dir / "coworker-types.yaml"
    path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    return path


def _spec() -> dict:
    return {
        "orchestrator_profile": "orchestrator",
        "default_profile": "default",
        "spines": {"base": {"source": "spines/base.yaml"}},
        "default_config": {},
        "types": {
            "orchestrator": {"extends": ["base"], "identity": "Orch.", "workflows": ["wf"], "config": {}},
            "worker": {"extends": ["base"], "identity": "Work.", "workflows": ["wf"], "config": {}},
        },
    }


def _render(module, tmp_path, spec, tag):
    module.compose(str(_write_spec(tmp_path / f"spec_{tag}", spec)), str(tmp_path / f"out_{tag}"))


def test_layout_refuses_noncanonical_non_webhook_platform(module, tmp_path):
    """The canonical-layout check is platform-generic, not webhook-specific: a
    coworker spelling a NON-port-binding, non-webhook platform (discord) under a
    gateway.<platform> alias is refused just as a webhook alias is."""
    spec = _spec()
    spec["types"]["worker"]["config"] = {"gateway": {"discord": {"enabled": True}}}
    with pytest.raises(module.CompositionError):
        _render(module, tmp_path, spec, "discord_alias")


def test_layout_accepts_canonical_non_port_binding_platform(module, tmp_path):
    """A coworker enabling a non-port-binding platform under the canonical
    platforms.<x> map renders — the layout/port-binding checks refuse only
    aliases and port binders, not every platform."""
    spec = _spec()
    spec["types"]["worker"]["config"] = {"platforms": {"discord": {"enabled": True}}}
    _render(module, tmp_path, spec, "discord_canonical")


def test_enforce_multiplex_sets_roster_when_spec_is_silent(module, tmp_path):
    """A spec that never mentions multiplexing still renders a DEFAULT config
    whose gateway.multiplex_profiles is True and whose allowlist is the sorted
    roster — enforcement is explicit, never by assumption."""
    out = tmp_path / "out_silent"
    module.compose(str(_write_spec(tmp_path / "spec_silent", _spec())), str(out))
    default_cfg = yaml.safe_load((out / "default" / "config.yaml").read_text(encoding="utf-8"))
    gateway = default_cfg["gateway"]
    assert gateway["multiplex_profiles"] is True
    assert gateway["multiplex_profile_allowlist"] == ["orchestrator", "worker"]
