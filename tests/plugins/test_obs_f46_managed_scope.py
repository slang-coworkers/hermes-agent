"""Managed-scope policy for OBS-F46's active-.env capture writer.

The ADR scopes the managed-scope behavior of ``apply_capture_env`` as a
code-review checkpoint rather than part of the three hermetic acceptance
criteria, so these live beside — not inside — the AC test and are NOT
``test_ac_obs_f46_*`` (they mint no acceptance-criterion id). They prove the
write-time policy contract: enabling capture in a coworker's active ``.env`` must
never silently override machine-global managed policy.

- A managed scope that already provides a truthy ``HERMES_DUMP_REQUESTS`` is a
  no-op success (the machine already enabled it; do not fight it).
- A managed scope that pins the key falsy, or a package-managed install that owns
  the environment, is an explicit operator action — surfaced as an error, never a
  silent local override.

The plugin is loaded through the real discovery path (as the AC test does) so the
behavior under test is the one that actually registers.
"""

from pathlib import Path
import shutil

import pytest
import yaml

from hermes_cli import managed_scope
from hermes_cli.plugins import PluginManager

PLUGIN_KEY = "nv-coworker-compose"
REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_SRC = REPO_ROOT / "plugins" / PLUGIN_KEY

CAPTURE_KEY = "HERMES_DUMP_REQUESTS"


def _load(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    plugins_dir = home / "plugins"
    plugins_dir.mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, plugins_dir / PLUGIN_KEY)
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": [PLUGIN_KEY]}}),
        encoding="utf-8",
    )
    empty_bundled = tmp_path / "bundled-plugins"
    empty_bundled.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(empty_bundled))
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled is True and loaded.error is None and loaded.module is not None
    return loaded.module


def _managed_dir_with(tmp_path, monkeypatch, env_body):
    """Point HERMES_MANAGED_DIR at a tmp managed scope carrying *env_body*.

    HERMES_MANAGED_DIR is honored even under pytest (managed_scope.get_managed_dir
    resolves the override before the pytest guard), so this exercises the real
    managed-scope reader.
    """
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / ".env").write_text(env_body, encoding="utf-8")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    managed_scope.invalidate_managed_cache()
    return managed


@pytest.mark.parametrize("truthy", ["true", "yes"])
def test_managed_truthy_is_noop_success(tmp_path, monkeypatch, truthy):
    module = _load(tmp_path, monkeypatch)
    _managed_dir_with(tmp_path, monkeypatch, f"{CAPTURE_KEY}={truthy}\n")

    profile = tmp_path / "prof"
    profile.mkdir()
    module.apply_capture_env(str(profile))

    # Managed scope already enabled it machine-wide: no profile write at all.
    assert not (profile / ".env").exists()


def test_managed_falsy_raises_and_leaves_target_untouched(tmp_path, monkeypatch):
    module = _load(tmp_path, monkeypatch)
    _managed_dir_with(tmp_path, monkeypatch, f"{CAPTURE_KEY}=false\n")

    profile = tmp_path / "prof"
    profile.mkdir()
    with pytest.raises(module.CaptureEnvManagedError):
        module.apply_capture_env(str(profile))

    # A managed falsy pin is an administrative decision: no silent local override.
    assert not (profile / ".env").exists()


def test_package_managed_install_raises_and_leaves_target_untouched(tmp_path, monkeypatch):
    module = _load(tmp_path, monkeypatch)
    # No managed .env carries the key, but the install is package-managed.
    managed_scope.invalidate_managed_cache()
    monkeypatch.setenv("HERMES_MANAGED", "nixos")

    profile = tmp_path / "prof"
    profile.mkdir()
    with pytest.raises(module.CaptureEnvManagedError):
        module.apply_capture_env(str(profile))

    assert not (profile / ".env").exists()
