"""Non-acceptance unit tests for the onecli-onboard handler (CRED-F28).

These cover onboarding guards that are not among the five acceptance criteria:
a profile with no profile_secret_sets entry is rejected before any OneCLI call,
and an identity that does not resolve after ensure_agent/set_secrets raises
(and is not persisted). Loaded through the real discovery path like the
acceptance test, so the registered handler is what is exercised.
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import pytest

from hermes_cli.plugins import PluginManager

PLUGIN_KEY = "podman-onecli"
REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_SRC = REPO_ROOT / "plugins" / PLUGIN_KEY


def _load(tmp_path, monkeypatch, profile_secret_sets_yaml: str):
    hermes_home = tmp_path / "home"
    (hermes_home / "plugins").mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, hermes_home / "plugins" / PLUGIN_KEY)
    (hermes_home / "config.yaml").write_text(
        "plugins:\n"
        f"  enabled: [{PLUGIN_KEY}]\n"
        "  entries:\n"
        f"    {PLUGIN_KEY}:\n"
        "      settings:\n"
        f"        profile_secret_sets:{profile_secret_sets_yaml}\n",
        encoding="utf-8",
    )
    empty_bundled = tmp_path / "empty_bundled"
    empty_bundled.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(empty_bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled is True and loaded.error is None
    return manager, loaded, hermes_home


def _args_for(manager, profile_path: Path):
    entry = manager._cli_commands["onecli-onboard"]
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="_cmd")
    entry["setup_fn"](subs.add_parser("onecli-onboard"))
    return entry, parser.parse_args(["onecli-onboard", "--profile", str(profile_path)])


class _RecordingTransport:
    def __init__(self, config_status):
        self.calls = []
        self._config_status = config_status

    def __call__(self, method, path, *, json=None, **_):
        if method == "POST" and path == "/api/agents":
            self.calls.append(("ensure_agent", json["identifier"]))
            return 201, {"identifier": json["identifier"]}
        if method == "POST" and path.endswith("/secrets"):
            self.calls.append(("set_secrets", None))
            return 200, {}
        if method == "GET" and path.startswith("/v1/container-config"):
            self.calls.append(("get_container_config", None))
            if self._config_status == 404:
                return 404, None
            return 200, {"env": {}}
        raise AssertionError(f"unexpected OneCLI call {method} {path}")


def test_onboard_requires_profile_secret_sets_entry(tmp_path, monkeypatch):
    """A profile with no profile_secret_sets entry is rejected before any OneCLI
    call — onboarding never silently clears grants for an unconfigured profile."""
    manager, loaded, _ = _load(tmp_path, monkeypatch, "\n          cred-f28-bot-a: []")
    transport = _RecordingTransport(config_status=200)
    loaded.module.oneclient.set_transport(transport)

    profile = tmp_path / "profiles" / "cred-f28-bot-z"  # not in the map
    profile.mkdir(parents=True)
    entry, args = _args_for(manager, profile)
    with pytest.raises(ValueError):
        entry["handler_fn"](args)
    assert transport.calls == []


def test_onboard_raises_when_container_config_unresolved(tmp_path, monkeypatch):
    """When get_container_config returns None after ensure_agent/set_secrets, the
    handler raises and does not persist an identity reference."""
    manager, loaded, hermes_home = _load(tmp_path, monkeypatch, "\n          cred-f28-bot-a: []")
    transport = _RecordingTransport(config_status=404)
    loaded.module.oneclient.set_transport(transport)

    profile = tmp_path / "profiles" / "cred-f28-bot-a"
    profile.mkdir(parents=True)
    entry, args = _args_for(manager, profile)
    with pytest.raises(loaded.module.oneclient.OneCLIError):
        entry["handler_fn"](args)
    assert [kind for kind, _ in transport.calls] == [
        "ensure_agent",
        "set_secrets",
        "get_container_config",
    ]
    ref = hermes_home / "plugin-data" / PLUGIN_KEY / "identities" / "cred-f28-bot-a.json"
    assert not ref.exists()


def test_bootstrap_key_uses_source_environment_and_is_protected(tmp_path, monkeypatch):
    """The bootstrap key resolves from the per-fetch environment view (falling
    back to os.environ), and the SecretSource marks it protected so no source
    can overwrite it."""
    from agent.secret_sources.base import reset_source_environment, set_source_environment
    from agent.secret_sources.registry import get_source

    monkeypatch.setenv("ONECLI_API_KEY", "host-key")
    manager, loaded, _ = _load(tmp_path, monkeypatch, "\n          cred-f28-bot-a: []")
    oc = loaded.module.oneclient

    assert oc._api_key() == "host-key"
    token = set_source_environment({"ONECLI_API_KEY": "ctx-key"})
    try:
        assert oc._api_key() == "ctx-key"
    finally:
        reset_source_environment(token)

    source = get_source("onecli", scope=manager.scope_key)
    assert source.protected_env_vars({}) == frozenset({"ONECLI_API_KEY"})


def test_oneclient_requires_secure_base(tmp_path, monkeypatch):
    """The bootstrap key rides an Authorization: Bearer header, so the real
    client refuses to build a request against a cleartext-HTTP base — except a
    loopback host, the documented local-dev exception."""
    _, loaded, _ = _load(tmp_path, monkeypatch, "\n          cred-f28-bot-a: []")
    oc = loaded.module.oneclient

    oc._require_secure_base("https://onecli.example")     # TLS — ok
    oc._require_secure_base("http://127.0.0.1:10255")     # loopback dev — ok
    oc._require_secure_base("http://localhost:8080")      # loopback dev — ok
    for bad in ("http://onecli.example", "http://10.0.0.9:9000", "ftp://onecli.example"):
        with pytest.raises(oc.OneCLIError):
            oc._require_secure_base(bad)


def test_oneclient_validates_identifier_before_transport(tmp_path, monkeypatch):
    """Every OneCLI call re-validates the identifier grammar (defense in depth)
    and refuses before any transport call on an unsafe value, so no unvalidated
    value can reach a request path or JSON body."""
    _, loaded, _ = _load(tmp_path, monkeypatch, "\n          cred-f28-bot-a: []")
    oc = loaded.module.oneclient

    class _NoCall:
        def __call__(self, *a, **k):
            raise AssertionError("transport must not be called on an invalid identifier")

    oc.set_transport(_NoCall())
    try:
        for bad in ("../etc", "a/b", "bad id", "", "-leading", "tok\nen"):
            with pytest.raises(oc.OneCLIError):
                oc.ensure_agent(identifier=bad)
            with pytest.raises(oc.OneCLIError):
                oc.get_container_config(agent=bad)
            with pytest.raises(oc.OneCLIError):
                oc.set_secrets(identifier=bad, secrets=[])
    finally:
        oc.set_transport(None)
