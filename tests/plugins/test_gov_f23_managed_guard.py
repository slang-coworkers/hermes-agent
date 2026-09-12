"""Focused unit tests for the GOV-F23 render guards.

* a coworker type literally named ``managed`` is REJECTED (it would collide with
  the machine-wide managed-scope fragment directory ``out_root/managed``), rather
  than silently overwriting the fragment;
* a malformed per-role ``command_allowlist`` / ``approvals.deny`` (non-list, a
  blank/whitespace member, or a non-mapping ``approvals`` block) is REJECTED with
  ``CompositionError``, never a raw ``TypeError`` and never a silently-inert rule.

The plugin is loaded through the real discovery path from a tmp_path HERMES_HOME
with an EMPTY bundled dir, then the render is invoked as a module function on a
mutated copy of the GOV-F23 fixture spec.
"""
from __future__ import annotations

import copy
import shutil
from pathlib import Path

import pytest
import yaml

from hermes_cli.plugins import PluginManager

PLUGIN_KEY = "nv-coworker-compose"
PLUGIN_SRC = Path(__file__).resolve().parents[2] / "plugins" / PLUGIN_KEY
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "gov-f23"
SPEC_NAME = "coworker-types.yaml"


def _load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A tmp HERMES_HOME with the plugin copied in and enabled; return the loaded plugin."""
    home = tmp_path / "hermes-home"
    (home / "plugins").mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, home / "plugins" / PLUGIN_KEY)
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": [PLUGIN_KEY]}}), encoding="utf-8"
    )
    empty_bundled = tmp_path / "bundled-plugins"
    empty_bundled.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(empty_bundled))
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled is True, getattr(loaded, "error", None)
    assert loaded.module is not None
    return loaded


def _mutated_spec(tmp_path: Path, mutate) -> Path:
    """Copy the GOV-F23 fixture dir, apply ``mutate`` to the parsed spec dict,
    write it back, and return the mutated spec path (so relative spine/skill
    paths still resolve inside the copied tree)."""
    fixture = tmp_path / "fixture"
    shutil.copytree(FIXTURE_DIR, fixture)
    spec_path = fixture / SPEC_NAME
    data = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    mutate(data)
    spec_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return spec_path


def test_managed_type_name_rejected(tmp_path, monkeypatch):
    """A coworker type named 'managed' collides with out_root/managed and must
    raise, never overwrite the fragment."""
    loaded = _load(tmp_path, monkeypatch)

    def _add_managed_type(data):
        data.setdefault("types", {})["managed"] = {"extends": ["base"], "identity": "X"}

    spec = _mutated_spec(tmp_path, _add_managed_type)
    with pytest.raises(loaded.module.CompositionError) as exc:
        loaded.module.compose(str(spec), str(tmp_path / "out"))
    assert "managed" in str(exc.value).lower()
    # the reserved dir must not have been written for a rejected spec
    assert not (tmp_path / "out" / "managed" / "config.yaml").exists()


def test_command_allowlist_non_list_rejected(tmp_path, monkeypatch):
    """A non-list command_allowlist is a spec error (CompositionError, not TypeError)."""
    loaded = _load(tmp_path, monkeypatch)

    def _bad(data):
        data["types"]["builder"]["config"]["command_allowlist"] = "rm -rf build"

    spec = _mutated_spec(tmp_path, _bad)
    with pytest.raises(loaded.module.CompositionError) as exc:
        loaded.module.compose(str(spec), str(tmp_path / "out"))
    assert "command_allowlist" in str(exc.value)


def test_command_allowlist_blank_member_rejected(tmp_path, monkeypatch):
    """A blank/whitespace allowlist member is an over-broad, silently-inert rule
    and must be rejected."""
    loaded = _load(tmp_path, monkeypatch)

    def _bad(data):
        data["types"]["builder"]["config"]["command_allowlist"] = ["rm -rf build", "   "]

    spec = _mutated_spec(tmp_path, _bad)
    with pytest.raises(loaded.module.CompositionError) as exc:
        loaded.module.compose(str(spec), str(tmp_path / "out"))
    assert "command_allowlist" in str(exc.value)


def test_approvals_deny_blank_member_rejected(tmp_path, monkeypatch):
    """A blank approvals.deny member is a silently-inert deny rule and must be rejected."""
    loaded = _load(tmp_path, monkeypatch)

    def _bad(data):
        deny = copy.deepcopy(data["types"]["reviewer"]["config"]["approvals"]["deny"])
        deny.append("")
        data["types"]["reviewer"]["config"]["approvals"]["deny"] = deny

    spec = _mutated_spec(tmp_path, _bad)
    with pytest.raises(loaded.module.CompositionError) as exc:
        loaded.module.compose(str(spec), str(tmp_path / "out"))
    assert "approvals.deny" in str(exc.value)


@pytest.mark.parametrize("bad_value", ["malformed", ["a", "b"], 123])
def test_non_mapping_approvals_rejected(tmp_path, monkeypatch, bad_value):
    """A present non-mapping approvals block is a spec error — the strip would
    skip it, it would be written raw, and the managed overlay would then replace
    it. Reject it fail-closed rather than pass it through."""
    loaded = _load(tmp_path, monkeypatch)

    def _bad(data):
        data["types"]["builder"]["config"]["approvals"] = bad_value

    spec = _mutated_spec(tmp_path, _bad)
    with pytest.raises(loaded.module.CompositionError) as exc:
        loaded.module.compose(str(spec), str(tmp_path / "out"))
    assert "approvals must be a mapping" in str(exc.value)


def test_valid_spec_still_composes(tmp_path, monkeypatch):
    """Control: the unmutated fixture composes and emits the managed fragment (so
    the guards above are proven to reject a mutation, not a broken baseline)."""
    loaded = _load(tmp_path, monkeypatch)
    spec = _mutated_spec(tmp_path, lambda data: None)
    out = tmp_path / "out"
    rendered = loaded.module.compose(str(spec), str(out))
    assert set(rendered) == {"default", "builder", "reviewer"}
    assert (out / "managed" / "config.yaml").exists()
    assert "managed" not in rendered
