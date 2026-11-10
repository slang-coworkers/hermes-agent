"""nv-coworker-compose: the opt-in `retention:` fleet-spec block (max-age retention).

Absent, nothing changes (the MEM-F44 invariant sessions.auto_prune False stands). Declared, every rendered profile
(coworkers and the DEFAULT) gets sessions.retention_days / sessions.min_interval_hours explicitly, the kanban
done-subscription GC capped at the same window (never disabled), and sessions.auto_prune = retention.enforce
(default false: a dry run deletes nothing). Invalid blocks are refused before any profile is written.
"""

from __future__ import annotations

import importlib
import shutil
from pathlib import Path

import pytest
import hermes_yaml as yaml  # Hermes >= 0.22 ships no PyYAML (ruamel-backed safe_load/safe_dump)

from hermes_cli.plugins import PluginManager

pytestmark = pytest.mark.platforms("linux")

PLUGIN_KEY = "nv-coworker-compose"
PLUGIN_SRC = Path(__file__).resolve().parents[2] / "plugins" / PLUGIN_KEY


@pytest.fixture
def module(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    (home / "plugins").mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, home / "plugins" / PLUGIN_KEY)
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": [PLUGIN_KEY]}}), encoding="utf-8")
    (tmp_path / "bundled").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(tmp_path / "bundled"))
    manager = PluginManager()
    manager.discover_and_load()
    entry = manager._plugins[PLUGIN_KEY]
    assert entry.enabled is True and entry.error is None and entry.module is not None
    return entry.module


def _spec(tmp_path, retention=None, fixer_config=None):
    terminal = {"backend": "docker", "docker_image": "hermes-sandbox:pinned", "container_persistent": False}
    spec = {"workspace_root": "/data/coworkers", "install_surfaces": [], "orchestrator_profile": "fixer",
            "default_profile": "default",
            "types": {"fixer": {"identity": "FIX", "config": {"terminal": dict(terminal), **(fixer_config or {})}},
                      "reviewer": {"identity": "REV", "config": {"terminal": dict(terminal)}}}}
    if retention is not None:
        spec["retention"] = retention
    d = tmp_path / "spec"
    d.mkdir(parents=True, exist_ok=True)
    (d / "coworker-types.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")
    return d / "coworker-types.yaml"


def _configs(module, tmp_path, spec):
    out = tmp_path / "out"
    rendered = module.compose(str(spec), str(out))
    return {name: yaml.safe_load((Path(path) / "config.yaml").read_text(encoding="utf-8"))
            for name, path in rendered.items()}


def test_absent_block_keeps_the_mem_f44_invariant(module, tmp_path):
    for name, cfg in _configs(module, tmp_path, _spec(tmp_path)).items():
        assert cfg["sessions"]["auto_prune"] is False, name
        assert "retention_days" not in cfg["sessions"], name
        assert "done_sub_retention_days" not in (cfg.get("kanban") or {}), name


@pytest.mark.parametrize("enforce", [None, False, True])
def test_declared_block_renders_every_profile(module, tmp_path, enforce):
    block = {"max_age_days": 90} if enforce is None else {"max_age_days": 90, "enforce": enforce}
    cfgs = _configs(module, tmp_path, _spec(tmp_path, block))
    assert set(cfgs) == {"default", "fixer", "reviewer"}
    for name, cfg in cfgs.items():
        s = cfg["sessions"]
        assert s["retention_days"] == 90 and s["min_interval_hours"] == 24, name
        assert s["auto_prune"] is (enforce is True), name          # default false = dry run
        assert s["auto_archive"] is True, name                      # the other invariants stand
        assert cfg["checkpoints"]["auto_prune"] is False, name
        assert cfg["kanban"]["done_sub_retention_days"] == 30, name


def test_kanban_window_is_capped_never_disabled(module, tmp_path):
    comp = importlib.import_module(module.__name__ + ".compose")
    for declared, days, want in ((7, 90, 7), (120, 90, 90), (0, 90, 30), (-1, 90, 30), (True, 90, 30), (30, 14, 14)):
        cfg = {"kanban": {"done_sub_retention_days": declared}}
        comp._enforce_retention(cfg, {"max_age_days": days, "enforce": False})
        assert cfg["kanban"]["done_sub_retention_days"] == want, (declared, days)
    cfgs = _configs(module, tmp_path, _spec(tmp_path, {"max_age_days": 60},
                                            fixer_config={"kanban": {"done_sub_retention_days": 400}}))
    assert cfgs["fixer"]["kanban"]["done_sub_retention_days"] == 60
    assert cfgs["reviewer"]["kanban"]["done_sub_retention_days"] == 30


def test_a_type_cannot_turn_pruning_on_or_widen_the_window(module, tmp_path):
    over = {"sessions": {"auto_prune": True, "retention_days": 400, "min_interval_hours": 1000}}
    cfgs = _configs(module, tmp_path, _spec(tmp_path, {"max_age_days": 90}, fixer_config=over))
    assert cfgs["fixer"]["sessions"]["auto_prune"] is False
    assert cfgs["fixer"]["sessions"]["retention_days"] == 90 and cfgs["fixer"]["sessions"]["min_interval_hours"] == 24


@pytest.mark.parametrize("block", [
    {"max_age_days": 0}, {"max_age_days": 366}, {"max_age_days": "90"}, {"max_age_days": True}, {},
    {"max_age_days": 90, "enforce": "yes"}, {"max_age_days": 90, "enforce": 1}, {"max_age_days": 90, "dry_run": True},
    [90], "90",
])
def test_invalid_blocks_are_refused_before_anything_is_written(module, tmp_path, block):
    spec = _spec(tmp_path, block)
    with pytest.raises(module.CompositionError, match="retention"):
        module.compose(str(spec), str(tmp_path / "out"))
    assert not (tmp_path / "out" / "fixer" / "config.yaml").exists()
