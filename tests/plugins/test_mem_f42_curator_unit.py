"""Unit tests for the MEM-F42 curator enforcement in nv-coworker-compose.

These complement (do not overlap) the acceptance test. The acceptance test proves
the keep floor-MAX both ways only on a coworker (reviewer declares keep:12) and
asserts the enforced booleans with ``==``; these pin two behaviours it leaves
implicit through the real ``compose()`` render path from an isolated HERMES_HOME:

  1. the enforced booleans are emitted as YAML booleans, not truthy integers, on
     both a coworker and the DEFAULT multiplexer (a ``== True`` assertion would
     also accept ``1``);
  2. a keep declared ABOVE the floor on the DEFAULT profile is preserved (the
     DEFAULT branch has its own ``_enforce_curator`` call site, so floor-MAX is
     proven there and not only on a coworker).
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
    # Spine carries the curator SAFETY keys UNSAFE, so the render must override
    # them rather than pass them through.
    (spec_dir / "spines" / "base.yaml").write_text(
        yaml.safe_dump(
            {
                "identity": "Base.",
                "invariants": ["Help."],
                "config": {"curator": {"enabled": False, "backup": {"enabled": False, "keep": 0}}},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    wf = spec_dir / "workflows" / "wf"
    wf.mkdir(parents=True, exist_ok=True)
    (wf / "SKILL.md").write_text("# wf\n\nWork.\n", encoding="utf-8")
    path = spec_dir / "coworker-types.yaml"
    path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    return path


def _base_spec(default_config: dict) -> dict:
    return {
        "orchestrator_profile": "orchestrator",
        "default_profile": "default",
        "spines": {"base": {"source": "spines/base.yaml"}},
        "default_config": default_config,
        "types": {
            "orchestrator": {"extends": ["base"], "identity": "Orch.", "workflows": ["wf"], "config": {}},
            "worker": {"extends": ["base"], "identity": "Work.", "workflows": ["wf"], "config": {}},
        },
    }


def _raw(out_root: Path, profile: str) -> dict:
    return yaml.safe_load((out_root / profile / "config.yaml").read_text(encoding="utf-8"))


def test_enforced_curator_booleans_are_strict_bool_on_every_profile(module, tmp_path):
    """curator.enabled / curator.backup.enabled render as YAML booleans, not
    truthy integers, on a coworker and the DEFAULT multiplexer alike."""
    out = tmp_path / "out"
    module.compose(str(_write_spec(tmp_path / "spec", _base_spec({}))), str(out))
    for profile in ("worker", "default"):
        cfg = _raw(out, profile)
        assert cfg["curator"]["enabled"] is True, f"{profile}: enabled not a bool True"
        assert cfg["curator"]["backup"]["enabled"] is True, f"{profile}: backup.enabled not a bool True"


def test_default_profile_keep_above_floor_is_preserved(module, tmp_path):
    """A keep declared above the floor on the DEFAULT profile is preserved (its
    own enforcement call site applies floor-MAX, not a fixed value), while the
    unsafe booleans inherited from the spine are still overridden."""
    out = tmp_path / "out"
    default_config = {"curator": {"backup": {"keep": 12}}}
    module.compose(str(_write_spec(tmp_path / "spec", _base_spec(default_config))), str(out))
    cfg = _raw(out, "default")
    assert cfg["curator"]["backup"]["keep"] == 12
    assert cfg["curator"]["enabled"] is True
    assert cfg["curator"]["backup"]["enabled"] is True
    # A coworker with no declared keep is floored to exactly 5 in the same render.
    assert _raw(out, "worker")["curator"]["backup"]["keep"] == 5
