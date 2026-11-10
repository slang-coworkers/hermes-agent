"""OpenShell 0.1 settings that carry no deployment defaults: the k8s allow-list, the broker address, the
provider profile path and the messaging hint all come from the spec or the command line."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest
import hermes_yaml as yaml

from hermes_cli.plugins import PluginManager

PLUGIN_KEY = "nv-coworker-compose"
PLUGIN_SRC = Path(__file__).resolve().parents[2] / "plugins" / PLUGIN_KEY
OPENSHELL = PLUGIN_SRC / "openshell"


def _openshell(name: str):
    spec = importlib.util.spec_from_file_location(f"nv_coworker_compose_v01_test_{name}", OPENSHELL / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def compose(tmp_path, monkeypatch):
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
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled is True and loaded.error is None
    return sys.modules[loaded.module.__name__ + ".compose"]


POLICY = {
    "version": 1,
    "network_policies": {
        "web": {"endpoints": [{"host": "api.example.com", "port": 443}, {"host": "llm.example.com", "port": 443}],
                "binaries": [{"path": "/usr/bin/curl"}]},
    },
}


def _hosts(result) -> set:
    rules = result.document.get("network_policies") or {}
    return {ep["host"] for rule in rules.values() for ep in rule.get("endpoints") or []}


def test_the_k8s_port_needs_an_explicit_allow_list():
    policy_port = _openshell("policy_port")
    with pytest.raises(policy_port.PolicyPortError, match="--allow-host"):
        policy_port.port_policy(POLICY, target="k8s")


def test_the_k8s_port_keeps_allow_listed_hosts_and_drops_inference_hosts():
    policy_port = _openshell("policy_port")
    allowed = ["api.example.com", "llm.example.com"]
    assert _hosts(policy_port.port_policy(POLICY, target="k8s", allowed_hosts=allowed)) == set(allowed)
    ported = policy_port.port_policy(POLICY, target="k8s", allowed_hosts=allowed, inference_hosts=["llm.example.com"])
    assert _hosts(ported) == {"api.example.com"}


def test_remote_mode_needs_a_broker_address():
    remote = _openshell("remote")
    spec = {"openshell": {"mode": "remote"}}
    with pytest.raises(remote.RemoteConfigError, match="broker address"):
        remote.resolve_config(spec, env={})
    addr = "tcp://broker.fleet.svc.cluster.local:18777"
    assert remote.resolve_config(spec, env={"OSH_BROKER_ADDR": addr})["broker_addr"] == addr
    assert remote.resolve_config({}, env={})["mode"] == remote.MODE_LOCAL


def _provider_block(**extra) -> dict:
    return {"provider": "custom", "base_url": "https://llm.example.com/v1", "api_mode": "chat_completions",
            "model_id": "example-model", "credential_env": "PROVIDER_API_KEY", "attribution_tag_prefix": "fleet",
            **extra}


def test_the_v01_provider_needs_a_named_profile(compose):
    with pytest.raises(compose.CompositionError, match="profile must be a non-empty string"):
        compose._validate_openshell_inference_provider_v01(_provider_block())


def test_the_v01_provider_profile_path_must_be_absolute(compose, tmp_path):
    with pytest.raises(compose.CompositionError, match="absolute path"):
        compose._validate_openshell_inference_provider_v01(
            _provider_block(profile="example-llm", profile_path="profiles/example-llm.yaml"))


def test_the_v01_provider_reads_the_profile_the_spec_names(compose, tmp_path):
    profile = tmp_path / "example-llm.yaml"
    profile.write_text(yaml.safe_dump({
        "id": "example-llm",
        "endpoints": [{"host": "llm.example.com", "port": 443}],
        "credentials": [{"env_vars": ["PROVIDER_API_KEY"]}],
    }), encoding="utf-8")
    resolved = compose._validate_openshell_inference_provider_v01(
        _provider_block(profile="example-llm", profile_path=str(profile)))
    assert (resolved["profile"], resolved["openshell_provider"]) == ("example-llm", "example-llm")
    assert resolved["api_key_value"] == "${env:PROVIDER_API_KEY}"
    with pytest.raises(compose.CompositionError, match="not an endpoint of provider profile"):
        compose._validate_openshell_inference_provider_v01(
            _provider_block(profile="example-llm", profile_path=str(profile),
                            base_url="https://other.example.com/v1"))


def _team(**extra) -> dict:
    return {"project": "fleet", "messaging_section": "wired", "types": {"orch": {}, "builder": {}},
            "wires": [["orch", "builder"]], **extra}


def test_the_messaging_section_names_no_ingress_unless_the_spec_does(compose):
    plain = compose._fleet_messaging_section("builder", _team(), "orch")
    assert "ask @orch to hand it to me." in plain
    hinted = compose._fleet_messaging_section(
        "builder", _team(messaging_section_ingress_hint="open an issue on the tracker"), "orch")
    assert "ask @orch to hand it to me, or open an issue on the tracker." in hinted
    with pytest.raises(compose.CompositionError, match="one-line string"):
        compose._fleet_messaging_section("builder", _team(messaging_section_ingress_hint='say "hi"'), "orch")
