"""OpenShell 0.1 settings that carry no deployment defaults: the k8s allow-list, the broker address, the
provider profile path and the messaging hint all come from the spec or the command line."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
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


SHA = "a" * 40
OSH_SPEC = PLUGIN_SRC.parents[1] / "tests" / "e2e-scenarios" / "OSH-F64" / "spec"


def _spec_copy(tmp_path, **top):
    root = tmp_path / "spec"
    shutil.copytree(OSH_SPEC, root)
    path = root / "coworker-types.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data.update(top)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _install_sources(inst, spec_path):
    state = inst.collect_home_state(inst.load_spec(spec_path), spec_path)
    steps = inst.build_plan(state, str(spec_path), SHA)
    return {step.command[step.command.index("install") + 1] for step in steps
            if step.tag == "plugin_install" and isinstance(step.command, list)}


@pytest.fixture
def installer(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(tmp_path / "managed"))
    return _openshell("installer")


def test_plugin_installs_come_from_the_public_fork_unless_the_spec_names_a_source(installer, tmp_path):
    sources = _install_sources(installer, _spec_copy(tmp_path))
    assert sources and all(s.startswith("slang-coworkers/hermes-agent/plugins/") for s in sources)


@pytest.mark.parametrize("source", ["example-org/hermes-agent", "https://git.example.test/team/hermes-agent.git"])
def test_the_spec_plugin_source_feeds_every_plugin_install(installer, tmp_path, source):
    sources = _install_sources(installer, _spec_copy(tmp_path, plugin_source=source))
    assert sources and all(s.startswith(f"{source}/plugins/") for s in sources)


@pytest.mark.parametrize("source", ["", "just-a-name", "https://git.example.test/team/hermes-agent", 7])
def test_a_plugin_source_hermes_cannot_resolve_is_refused(installer, tmp_path, source):
    with pytest.raises(ValueError, match="plugin_source"):
        _install_sources(installer, _spec_copy(tmp_path, plugin_source=source))


def test_the_fork_preflight_asks_git_about_the_spec_plugin_source(installer, tmp_path, monkeypatch):
    asked = []

    def _git(*argv, **_kw):
        asked.append(argv[-1])
        return type("P", (), {"returncode": 1, "stdout": ""})()

    monkeypatch.setattr(installer, "_git", _git)
    source = "https://git.example.test/team/hermes-agent.git"
    installer._fork_preflight(tmp_path, SHA, None, fork_url=installer._plugin_git_url(source))
    assert asked == [source]


def test_the_remote_installer_refuses_a_fork_source_and_the_local_one_forwards_it():
    remote = _openshell("remote")
    seen = {}

    class _Inst:
        def plan_text(self, *a, **kw):
            seen["plan"] = kw
            return ""

        def apply(self, *a, **kw):
            seen["apply"] = kw

    with pytest.raises(remote.RemoteConfigError, match="--fork-source"):
        remote.RemoteInstaller({}, inst=_Inst()).apply("s.yaml", SHA, fork_source="/srv/fork.git")
    with pytest.raises(remote.RemoteConfigError, match="--fork-source"):
        remote.RemoteInstaller({}, inst=_Inst()).plan_text("s.yaml", SHA, fork_source="/srv/fork.git")
    local = remote.LocalInstaller(_Inst())
    local.plan_text("s.yaml", SHA, fork_source="/srv/fork.git")
    local.apply("s.yaml", SHA, fork_source="/srv/fork.git")
    assert seen["plan"]["fork_source"] == seen["apply"]["fork_source"] == "/srv/fork.git"
    assert seen["plan"]["openshell_mode"] == seen["apply"]["openshell_mode"] == remote.MODE_LOCAL


def _real_run_inside_a_workload(tmp_path, spec_text):
    spec = tmp_path / "guard.yaml"
    spec.write_text(spec_text, encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", ""), "HERMES_PYTHON": sys.executable, "OPENSHELL_SANDBOX": "1"}
    return subprocess.run(["bash", str(OPENSHELL / "install-into-sandbox.sh"), str(spec), "--ref", SHA],
                          env=env, capture_output=True, text=True, check=False, timeout=120)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize(("spec_text", "says"), [
    ("egress:\n  openshell_api: '0.1'\n", "install into the hermes-gateway-agent pod"),
    ("- not\n- a mapping\n", "cannot read egress.openshell_api"),
    ("egress: [\n", "cannot read egress.openshell_api"),
])
def test_a_real_run_inside_an_openshell_workload_refuses_a_0_1_or_unreadable_spec(tmp_path, spec_text, says):
    proc = _real_run_inside_a_workload(tmp_path, spec_text)
    assert proc.returncode == 2 and says in proc.stderr, proc.stderr


@pytest.mark.platforms("posix")
def test_a_real_run_inside_an_openshell_workload_lets_a_0_0_spec_through_the_guard(tmp_path):
    proc = _real_run_inside_a_workload(tmp_path, "egress:\n  openshell_api: '0.0'\n")
    assert proc.returncode == 2 and "HERMES_HOME must be set" in proc.stderr, proc.stderr
