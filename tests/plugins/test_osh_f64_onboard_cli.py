"""OSH-F64 §D7.1: the onecli-onboard CLI must be invoked as
``hermes -p default onecli-onboard --profile <role>`` from the gateway root.

hermes-main consumes the FIRST ``-p``/``--profile`` in argv as the GLOBAL profile
selector and strips it before argparse (``hermes_cli/main.py`` ``_apply_profile_override``
:586-590 breaks on the first match, :719-722 strips it). So the correct form sets the
onboarding context to ``default`` (whose config carries the configured grant map)
and leaves the onboard handler's own ``--profile <role>`` intact; a bare
``hermes onecli-onboard --profile <role>`` loses the handler's required flag AND sets the
wrong (role) context. This drives the REGISTERED command (its setup_fn + handler_fn from
``register(ctx)``) so it also proves the handler consumes the grant map the §D7.1 render
populates. Hermetic, no network.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

import pytest

try:
    import yaml
except Exception:  # pragma: no cover - yaml is a dev dependency
    yaml = None

PLUGIN_KEY = "podman-onecli"


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "plugins" / PLUGIN_KEY / "plugin.yaml").exists():
            return parent
    pytest.fail("could not locate repo root")


def _load_onboard(tmp_path, monkeypatch, grants):
    """Discover podman-onecli from an isolated HERMES_HOME whose config grants ``grants``
    under ``profile_secret_sets``; return (manager, loaded plugin module, hermes_home)."""
    assert yaml is not None
    home = tmp_path / "home"
    (home / "plugins").mkdir(parents=True)
    shutil.copytree(_repo_root() / "plugins" / PLUGIN_KEY, home / "plugins" / PLUGIN_KEY)
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    cfg = {"plugins": {"enabled": [PLUGIN_KEY],
                       "entries": {PLUGIN_KEY: {"settings": {"profile_secret_sets": grants}}}}}
    (home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    from hermes_cli.plugins import PluginManager

    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled is True and loaded.error is None and loaded.module is not None
    return manager, loaded.module, home


def test_onecli_onboard_cli_default_context(tmp_path, monkeypatch):
    """`hermes -p default onecli-onboard --profile <role>` delivers args.profile=<role> to the
    registered onboard handler under the default context; a bare form is stripped by hermes-main."""
    manager, module, home = _load_onboard(tmp_path, monkeypatch, {"builder": ["Anthropic-Dev"]})
    # A supervisor marker would divert the sticky-active-profile branch; scrub them so the
    # explicit -p/--profile is the only selector in play.
    for var in ("HERMES_SUPERVISED_CHILD", "HERMES_S6_SUPERVISED_CHILD",
                "HERMES_GATEWAY_EXTERNAL_SUPERVISOR", "INVOCATION_ID"):
        monkeypatch.delenv(var, raising=False)
    # resolve_profile_env("builder") requires a live named profile under the root: the dir plus an
    # identity marker (hermes_constants.py _PROFILE_IDENTITY_MARKERS); "default" resolves to the root.
    (home / "profiles" / "builder").mkdir(parents=True)
    (home / "profiles" / "builder" / "config.yaml").write_text("model: {}\n", encoding="utf-8")

    entry = manager._cli_commands["onecli-onboard"]
    assert entry["setup_fn"] is not None and entry["handler_fn"] is not None
    from hermes_cli import main as hmain

    def _subparser():
        p = argparse.ArgumentParser(prog="onecli-onboard")
        entry["setup_fn"](p)
        return p

    monkeypatch.setattr(sys, "argv",
                        ["hermes", "-p", "default", "onecli-onboard", "--profile", "builder"])
    hmain._apply_profile_override()
    assert os.environ["HERMES_HOME"] == str(home)
    assert sys.argv == ["hermes", "onecli-onboard", "--profile", "builder"]
    ns = _subparser().parse_args(sys.argv[2:])
    assert ns.profile == "builder"

    calls: dict = {}
    monkeypatch.setattr(module.oneclient, "ensure_agent",
                        lambda identifier: calls.__setitem__("ensure", identifier))
    monkeypatch.setattr(module.oneclient, "set_secrets",
                        lambda identifier, secrets: calls.__setitem__("set", (identifier, list(secrets))))
    monkeypatch.setattr(module.oneclient, "get_container_config", lambda agent: {"agent": agent})
    entry["handler_fn"](ns)
    assert calls["ensure"] == "builder"
    assert calls["set"] == ("builder", ["Anthropic-Dev"])

    monkeypatch.setattr(sys, "argv", ["hermes", "onecli-onboard", "--profile", "builder"])
    hmain._apply_profile_override()
    assert os.environ["HERMES_HOME"] == str(home / "profiles" / "builder")
    assert sys.argv == ["hermes", "onecli-onboard"]
    with pytest.raises(SystemExit):
        _subparser().parse_args(sys.argv[2:])
