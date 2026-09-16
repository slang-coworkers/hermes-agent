"""Unit tests for the ISO-F13 mount-composition robustness guards.

These cover fail-closed edges that sit alongside the AC-ISO-F13-* acceptance
criteria (which live in test_nv_coworker_compose_mounts_acceptance.py): a mount
host path that smuggles a ':' (and thus extra host:container:mode fields), and a
malformed non-list terminal.docker_volumes that an earlier writer would otherwise
normalize away before the mount-composition check can reject it. Same loader shape
as the acceptance test: the plugin is loaded through the real discovery path from
an isolated HERMES_HOME with an empty HERMES_BUNDLED_PLUGINS, then driven through
module.compose() on inline specs written under tmp_path.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from hermes_cli.plugins import PluginManager

pytestmark = pytest.mark.linux_only

PLUGIN_KEY = "nv-coworker-compose"
PLUGIN_SRC = Path(__file__).resolve().parents[2] / "plugins" / PLUGIN_KEY


def _write_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    plugins_dir = home / "plugins"
    plugins_dir.mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, plugins_dir / PLUGIN_KEY)
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": [PLUGIN_KEY]}}), encoding="utf-8"
    )
    empty_bundled = tmp_path / "bundled-plugins"
    empty_bundled.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(empty_bundled))
    return home


@pytest.fixture
def module(tmp_path, monkeypatch):
    _write_home(tmp_path, monkeypatch)
    manager = PluginManager()
    manager.discover_and_load()
    entry = manager._plugins[PLUGIN_KEY]
    assert entry.enabled is True and entry.error is None and entry.module is not None
    return entry.module


def _type(*, container_persistent=False, docker_volumes=None):
    terminal = {"backend": "docker", "docker_image": "hermes-sandbox:pinned",
                "container_persistent": container_persistent}
    if docker_volumes is not None:
        terminal["docker_volumes"] = docker_volumes
    return {"identity": "FIX", "config": {"terminal": terminal}}


def _write(spec_dir, spec):
    spec_dir.mkdir(parents=True, exist_ok=True)
    path = spec_dir / "coworker-types.yaml"
    path.write_text(yaml.safe_dump(spec), encoding="utf-8")
    return path


def _base(**over):
    spec = {
        "workspace_root": "/data/coworkers",
        "install_surfaces": [],
        "orchestrator_profile": "fixer",
        "default_profile": "default",
        "types": {"fixer": _type()},
    }
    spec.update(over)
    return spec


def test_install_surface_with_colon_refused(module, tmp_path):
    """A ':' in an install surface would smuggle a :/workspace destination + extra
    mount fields — it must be refused, not string-formatted into a volume spec."""
    spec = _write(tmp_path / "s", _base(install_surfaces=["/data/tools:/workspace"]))
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(tmp_path / "out"))


def test_workspace_root_with_colon_refused(module, tmp_path):
    """A ':' in workspace_root is refused for the same reason."""
    spec = _write(tmp_path / "s", _base(workspace_root="/data/x:/y"))
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(tmp_path / "out"))


def test_scalar_docker_volumes_with_clone_refused(module, tmp_path):
    """A non-list terminal.docker_volumes must be rejected BEFORE _enforce_shared_learnings
    normalizes it to a list — otherwise a malformed/injected scalar is silently dropped
    when a shared-learnings clone is configured."""
    spec = _write(
        tmp_path / "s",
        _base(
            shared_learnings_root="/data/learnings-clone",
            types={"fixer": _type(docker_volumes="/etc:/etc:ro")},
        ),
    )
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(tmp_path / "out"))


def test_scalar_docker_volumes_without_clone_refused(module, tmp_path):
    """The same non-list guard fires with no clone configured."""
    spec = _write(
        tmp_path / "s",
        _base(types={"fixer": _type(docker_volumes="/etc:/etc:ro")}),
    )
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(tmp_path / "out"))


def test_nonlist_install_surfaces_refused(module, tmp_path):
    """A non-list install_surfaces (e.g. a bare string) is refused, not treated as
    an iterable of surface declarations."""
    spec = _write(tmp_path / "s", _base(install_surfaces="/data/tools"))
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(tmp_path / "out"))


def test_install_surface_at_clone_destination_refused(module, tmp_path):
    """An install surface equal to the shared-learnings mount point collides with the
    clone's container destination (both would emit -v ...:/mnt/shared-learnings) and
    must be refused so the closed set never carries a shadowing mount."""
    spec = _write(tmp_path / "s", _base(
        shared_learnings_root="/data/learnings-clone",
        install_surfaces=[module.WIKI_MOUNT],
    ))
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(tmp_path / "out"))


def test_symlink_loop_workspace_root_refused(module, tmp_path):
    """A workspace_root that is a symlink loop fails closed: Path.resolve() raises
    RuntimeError on 3.11/3.12 and the containment check must convert that to a
    CompositionError, not let it escape raw."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.symlink_to(b)
    b.symlink_to(a)
    spec = _write(tmp_path / "s", _base(workspace_root=str(a)))
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(tmp_path / "out"))
