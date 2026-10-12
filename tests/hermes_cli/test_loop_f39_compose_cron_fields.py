"""LOOP-F39 — nv-coworker-compose cron_jobs skill/skills/deliver render fields.

Focused non-AC unit coverage of the compose extension that AC-3 proves end-to-end:
the schema widening in _build_cron_job that lets a spine declare a skill-backed
(optionally Bot-Chat-delivered) cron job, mirroring native create_job. Behaviour
contract only — drives the real _build_cron_job on the loaded plugin module and
asserts its emitted job dict; no snapshots, no source reads, no network.
"""

import json
import shutil

import pytest

from pathlib import Path

_PLUGIN_KEY = "nv-coworker-compose"
_PLUGIN_SRC = Path(__file__).resolve().parents[2] / "plugins" / _PLUGIN_KEY


def _load(tmp_path, monkeypatch):
    """Load the plugin under test from an isolated HERMES_HOME (same shape as the
    acceptance test's _load_compose) and return its .compose submodule (where
    _build_cron_job / CompositionError live; the package __init__ re-exports only
    the public compose()/CompositionError)."""
    import sys

    from hermes_cli.plugins import PluginManager

    home = tmp_path / "hermes-home"
    plugins_dir = home / "plugins"
    plugins_dir.mkdir(parents=True)
    shutil.copytree(_PLUGIN_SRC, plugins_dir / _PLUGIN_KEY)
    (home / "config.yaml").write_text(
        json.dumps({"plugins": {"enabled": [_PLUGIN_KEY]}}), encoding="utf-8"
    )
    empty_bundled = tmp_path / "bundled-plugins"
    empty_bundled.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(empty_bundled))
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[_PLUGIN_KEY]
    assert loaded.enabled is True and loaded.error is None and loaded.module is not None
    return sys.modules[loaded.module.compose.__module__]


def _build(module, decl, profile="orchestrator"):
    return module._build_cron_job(profile, {"name": "j", "schedule": "every 12h", **decl})


def test_singular_skill_normalizes_to_list_and_alias(tmp_path, monkeypatch):
    module = _load(tmp_path, monkeypatch)
    job = _build(module, {"prompt": "p", "skill": "supervise-issues"})
    assert job["skills"] == ["supervise-issues"]
    assert job["skill"] == "supervise-issues"


def test_string_skills_wraps_to_list(tmp_path, monkeypatch):
    module = _load(tmp_path, monkeypatch)
    job = _build(module, {"prompt": "p", "skills": "supervise-issues"})
    assert job["skills"] == ["supervise-issues"]
    assert job["skill"] == "supervise-issues"


def test_list_skills_preserved_ordered_unique(tmp_path, monkeypatch):
    module = _load(tmp_path, monkeypatch)
    job = _build(module, {"prompt": "p", "skills": ["a", "b", "a"]})
    assert job["skills"] == ["a", "b"]
    assert job["skill"] == "a"


def test_skill_only_payload_is_accepted(tmp_path, monkeypatch):
    # no prompt and no script: the skill alone drives the agent turn (native create_job
    # accepts skills-only), so the payload gate must not reject it.
    module = _load(tmp_path, monkeypatch)
    job = _build(module, {"skills": ["supervise-issues"]})
    assert job["skills"] == ["supervise-issues"]
    assert "prompt" not in job and "script" not in job


def test_deliver_passes_through_verbatim(tmp_path, monkeypatch):
    # native create_job persists deliver exactly as given (cron/jobs.py:2432), no strip.
    module = _load(tmp_path, monkeypatch)
    job = _build(module, {"prompt": "p", "skills": ["s"], "deliver": " bot-chat "})
    assert job["deliver"] == " bot-chat "


def test_malformed_skills_element_fails_closed(tmp_path, monkeypatch):
    module = _load(tmp_path, monkeypatch)
    with pytest.raises(module.CompositionError):
        _build(module, {"prompt": "p", "skills": [123]})


def test_empty_skills_list_fails_closed(tmp_path, monkeypatch):
    module = _load(tmp_path, monkeypatch)
    with pytest.raises(module.CompositionError):
        _build(module, {"prompt": "p", "skills": []})


def test_non_list_non_str_skills_fails_closed(tmp_path, monkeypatch):
    module = _load(tmp_path, monkeypatch)
    with pytest.raises(module.CompositionError):
        _build(module, {"prompt": "p", "skills": {"nope": 1}})


def test_blank_deliver_fails_closed(tmp_path, monkeypatch):
    module = _load(tmp_path, monkeypatch)
    with pytest.raises(module.CompositionError):
        _build(module, {"prompt": "p", "deliver": "   "})


def test_omitting_all_three_keeps_job_byte_identical(tmp_path, monkeypatch):
    # a legacy declaration that carries none of the new keys must render exactly as
    # before — no skills/skill/deliver key added — so unchanged distributions stay
    # byte-identical on re-render.
    module = _load(tmp_path, monkeypatch)
    job = _build(module, {"prompt": "p"})
    assert "skills" not in job and "skill" not in job and "deliver" not in job


@pytest.mark.parametrize("key", ["skill", "skills", "deliver"])
def test_declared_null_field_fails_closed(tmp_path, monkeypatch, key):
    # a declared-but-null value (YAML `skill: null`) is an operator error, not "absent":
    # a value-based .get() would silently drop it; presence-based validation must reject it.
    module = _load(tmp_path, monkeypatch)
    with pytest.raises(module.CompositionError):
        _build(module, {"prompt": "p", key: None})


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_scalar_skills_fails_closed(tmp_path, monkeypatch, blank):
    module = _load(tmp_path, monkeypatch)
    with pytest.raises(module.CompositionError):
        _build(module, {"prompt": "p", "skills": blank})
