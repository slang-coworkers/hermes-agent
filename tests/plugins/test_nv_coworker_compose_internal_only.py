"""nv-coworker-compose: the opt-in `internal_only:` fleet-spec block: a fleet that sees only internal data.

Absent, nothing changes. Declared, every rendered profile (coworkers and the DEFAULT) gets agent.disabled_toolsets
(the listed toolsets first, then any a type already disabled) and auxiliary.allowed_providers. Invalid blocks are
refused before any profile is written; a provider list that does not fit a profile's model provider is refused.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import hermes_yaml as yaml  # Hermes >= 0.22 ships no PyYAML (ruamel-backed safe_load/safe_dump)

from hermes_cli.plugins import PluginManager

pytestmark = pytest.mark.platforms("linux")

PLUGIN_KEY = "nv-coworker-compose"
PLUGIN_SRC = Path(__file__).resolve().parents[2] / "plugins" / PLUGIN_KEY
POLICY = ["web", "browser", "tts", "image_gen", "computer_use", "vision"]
HUB = {"hub": {"base_url": "https://hub.example.internal/v1", "api_mode": "chat_completions"}}


@pytest.fixture
def module(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    (home / "plugins").mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, home / "plugins" / PLUGIN_KEY, ignore=shutil.ignore_patterns("__pycache__"))
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


def _spec(tmp_path, block=None, fixer_config=None, default_config=None):
    terminal = {"backend": "docker", "docker_image": "hermes-sandbox:pinned", "container_persistent": False}
    model = {"model": {"provider": "hub", "default": "m"}, "providers": HUB}
    spec = {"workspace_root": "/data/coworkers", "install_surfaces": [], "orchestrator_profile": "fixer",
            "default_profile": "default",
            "default_config": {**model, **(default_config or {})},
            "types": {"fixer": {"identity": "FIX", "config": {"terminal": dict(terminal), **model,
                                                               **(fixer_config or {})}},
                      "reviewer": {"identity": "REV", "config": {"terminal": dict(terminal), **model}}}}
    if block is not None:
        spec["internal_only"] = block
    d = tmp_path / "spec"
    d.mkdir(parents=True, exist_ok=True)
    (d / "coworker-types.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")
    return d / "coworker-types.yaml"


def _configs(module, tmp_path, spec):
    out = tmp_path / "out"
    rendered = module.compose(str(spec), str(out))
    return {name: yaml.safe_load((Path(path) / "config.yaml").read_text(encoding="utf-8"))
            for name, path in rendered.items()}


def test_absent_block_changes_nothing(module, tmp_path):
    for name, cfg in _configs(module, tmp_path, _spec(tmp_path)).items():
        assert "disabled_toolsets" not in (cfg.get("agent") or {}), name
        assert "allowed_providers" not in (cfg.get("auxiliary") or {}), name


def test_declared_block_renders_every_profile(module, tmp_path):
    cfgs = _configs(module, tmp_path, _spec(tmp_path, {"disabled_toolsets": POLICY, "auxiliary_providers": ["hub"]}))
    assert set(cfgs) == {"default", "fixer", "reviewer"}
    for name, cfg in cfgs.items():
        assert cfg["agent"]["disabled_toolsets"] == POLICY, name
        assert cfg["auxiliary"]["allowed_providers"] == ["hub"], name


def test_either_half_alone(module, tmp_path):
    cfgs = _configs(module, tmp_path / "a", _spec(tmp_path / "a", {"disabled_toolsets": ["web"]}))
    assert all(c["agent"]["disabled_toolsets"] == ["web"] for c in cfgs.values())
    assert not any("allowed_providers" in (c.get("auxiliary") or {}) for c in cfgs.values())
    cfgs = _configs(module, tmp_path / "b", _spec(tmp_path / "b", {"auxiliary_providers": ["hub"]}))
    assert all(c["auxiliary"]["allowed_providers"] == ["hub"] for c in cfgs.values())
    assert not any("disabled_toolsets" in (c.get("agent") or {}) for c in cfgs.values())


def test_a_type_keeps_its_own_disabled_toolsets_but_cannot_drop_the_policy(module, tmp_path):
    own = {"agent": {"disabled_toolsets": ["memory", "web"]}}
    cfgs = _configs(module, tmp_path, _spec(tmp_path, {"disabled_toolsets": POLICY}, fixer_config=own))
    assert cfgs["fixer"]["agent"]["disabled_toolsets"] == POLICY + ["memory"]
    assert cfgs["reviewer"]["agent"]["disabled_toolsets"] == POLICY


def test_a_json_string_list_from_hermes_config_set_is_merged(module, tmp_path):
    comp = __import__(module.__name__ + ".compose", fromlist=["compose"])
    cfg = {"agent": {"disabled_toolsets": "['memory']"}, "model": {"provider": "hub"}}
    comp._enforce_internal_only(cfg, {"disabled_toolsets": ["web"]}, "x")
    assert cfg["agent"]["disabled_toolsets"] == ["web", "memory"]


@pytest.mark.parametrize("block", [
    {}, [], "web", {"disabled_toolsets": []}, {"disabled_toolsets": "web"}, {"disabled_toolsets": ["Web"]},
    {"disabled_toolsets": ["web", "web"]}, {"disabled_toolsets": ["no-such-toolset"]},
    {"disabled_toolsets": ["hermes-cli"]}, {"disabled_toolsets": ["coding"]},
    {"auxiliary_providers": []}, {"auxiliary_providers": ["auto"]}, {"auxiliary_providers": ["main"]},
    {"auxiliary_providers": ["custom"]}, {"auxiliary_providers": ["custom:hub"]},
    {"disabled_toolsets": ["web"], "free_only": True},
])
def test_invalid_blocks_are_refused_before_anything_is_written(module, tmp_path, block):
    spec = _spec(tmp_path, block)
    with pytest.raises(module.CompositionError, match="internal_only"):
        module.compose(str(spec), str(tmp_path / "out"))
    assert not (tmp_path / "out" / "fixer" / "config.yaml").exists()


def test_provider_list_must_fit_the_model_provider(module, tmp_path):
    # a listed provider that is neither the model provider nor a providers: entry
    with pytest.raises(module.CompositionError, match="neither the model provider"):
        module.compose(str(_spec(tmp_path / "a", {"auxiliary_providers": ["hub", "openrouter"]})),
                       str(tmp_path / "a" / "out"))
    # the model provider itself must be listed (compression rides it)
    with pytest.raises(module.CompositionError, match="must be in internal_only.auxiliary_providers"):
        module.compose(str(_spec(tmp_path / "b", {"auxiliary_providers": ["other"]},
                                 fixer_config={"providers": {**HUB, "other": {"base_url": "https://o.example/v1"}}})),
                       str(tmp_path / "b" / "out"))


@pytest.mark.parametrize("task_cfg", [{"provider": "openrouter", "model": "a/b"},
                                      {"base_url": "https://elsewhere.example.com/v1", "model": "a/b"}])
def test_an_auxiliary_task_pinned_elsewhere_is_refused(module, tmp_path, task_cfg):
    spec = _spec(tmp_path, {"auxiliary_providers": ["hub"]}, fixer_config={"auxiliary": {"vision": task_cfg}})
    with pytest.raises(module.CompositionError, match="auxiliary.vision is pinned off"):
        module.compose(str(spec), str(tmp_path / "out"))


def test_a_task_pinned_on_the_listed_provider_or_bare_model_is_fine(module, tmp_path):
    aux = {"auxiliary": {"title_generation": {"provider": "hub", "model": "small"},
                         "compression": {"model": "m2"}}}
    cfgs = _configs(module, tmp_path, _spec(tmp_path, {"auxiliary_providers": ["hub"]}, fixer_config=aux))
    assert cfgs["fixer"]["auxiliary"]["title_generation"] == {"provider": "hub", "model": "small"}
    assert cfgs["fixer"]["auxiliary"]["allowed_providers"] == ["hub"]
