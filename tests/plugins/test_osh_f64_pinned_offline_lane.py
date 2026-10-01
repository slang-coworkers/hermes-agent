"""The pinned-offline-lane gate on the OpenShell render (OSH-F64 §D6/§D7/§D8).

The chain-dial (§D7), the firecrawl disable (§D8) and the install-scan guard (§D6) fire only
for a fleet that declares ``egress.pinned_offline_lane: true``; every other openshell fleet
(FLEET-F62.c, OSH-F63) keeps the base render, and the gate never changes a worker policy.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

PLUGIN_KEY = "nv-coworker-compose"
FIRECRAWL = {"web-firecrawl", "browser-firecrawl"}


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "plugins" / PLUGIN_KEY / "plugin.yaml").exists():
            return parent
    pytest.fail("could not locate repo root")


def _compose():
    path = _repo_root() / "plugins" / PLUGIN_KEY / "compose.py"
    spec = importlib.util.spec_from_file_location("osh_f64_lane_gate_compose", path)
    mod = importlib.util.module_from_spec(spec)
    # compose.py's frozen @dataclass resolves cls.__module__ via sys.modules at class creation.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _spec_dir(name: str) -> Path:
    base = _repo_root() / "tests" / "e2e-scenarios" / name / "spec"
    return base / "openshell" if (base / "openshell").is_dir() else base


def _copy_spec(tmp_path: Path, name: str, mutate=None) -> Path:
    dst = tmp_path / f"spec-{name}"
    shutil.copytree(_spec_dir(name), dst)
    spec_file = dst / "coworker-types.yaml"
    if mutate is not None:
        data = yaml.safe_load(spec_file.read_text(encoding="utf-8")) or {}
        mutate(data)
        spec_file.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return spec_file


def _render(c, spec_file: Path, out: Path) -> Path:
    c.compose(str(spec_file), str(out))
    return out


def _lane_enforcements(out: Path) -> dict:
    """Which of the three gated enforcements the render emitted, per kind."""
    served, default, chain_dial, firecrawl = [], [], [], []
    for cfg_path in sorted(out.glob("*/config.yaml")):
        if cfg_path.parent.name == "managed":
            continue
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        name = cfg_path.parent.name
        (served if list(cfg_path.parent.glob("policy-*.yaml")) else default).append(name)
        onecli = ((cfg.get("secrets") or {}).get("onecli") or {})
        settings = ((((cfg.get("plugins") or {}).get("entries") or {})
                     .get("podman-onecli") or {}).get("settings") or {})
        if "override_existing" in onecli or "proxy_rewrite" in settings or "ca_bundle" in settings:
            chain_dial.append(name)
        if FIRECRAWL & set((cfg.get("plugins") or {}).get("disabled") or []):
            firecrawl.append(name)
    managed = yaml.safe_load((out / "managed" / "config.yaml").read_text(encoding="utf-8")) or {}
    return {
        "served": served,
        "default": default,
        "chain_dial": chain_dial,
        "firecrawl": firecrawl,
        "scan_guard": (managed.get("plugins") or {}).get("scan_on_install", "absent"),
    }


def _assert_none(found: dict) -> None:
    assert found["served"], "render produced no served coworker"
    assert found["chain_dial"] == [], f"chain-dial must not fire: {found['chain_dial']}"
    assert found["firecrawl"] == [], f"firecrawl disable must not fire: {found['firecrawl']}"
    assert found["scan_guard"] == "absent", "managed scan_on_install guard must not fire"


def _policies(out: Path) -> dict:
    return {p.name: p.read_bytes() for p in sorted(out.rglob("policy-*.yaml"))}


BASE_EGRESS = {
    "proxy_addr": "172.17.0.1:18255",
    "inference_route": "inference-api.nvidia.com:443",
    "sandbox_image": "localhost/hermes-openshell-sandbox:pinned",
}


def test_validator_absent_and_false_are_off_true_is_on():
    c = _compose()
    assert c._validate_openshell_egress(dict(BASE_EGRESS))["pinned_offline_lane"] is False
    assert c._validate_openshell_egress({**BASE_EGRESS, "pinned_offline_lane": False})["pinned_offline_lane"] is False
    assert c._validate_openshell_egress({**BASE_EGRESS, "pinned_offline_lane": True})["pinned_offline_lane"] is True


@pytest.mark.parametrize("bad", [None, "true", "false", 1, 0, [], {}])
def test_validator_non_bool_fails_closed(bad):
    c = _compose()
    with pytest.raises(c.CompositionError, match="pinned_offline_lane"):
        c._validate_openshell_egress({**BASE_EGRESS, "pinned_offline_lane": bad})


def test_shipped_osh_f64_spec_opts_in_and_emits_all_three(tmp_path):
    c = _compose()
    found = _lane_enforcements(_render(c, _copy_spec(tmp_path, "OSH-F64"), tmp_path / "out"))
    assert found["chain_dial"] == found["served"], "every served coworker gets the chain-dial"
    assert set(found["firecrawl"]) == set(found["served"]) | set(found["default"]), \
        "firecrawl is disabled on the default and every served coworker"
    assert found["scan_guard"] is False, "managed fragment sets plugins.scan_on_install: false"


@pytest.mark.parametrize("mutate", [
    lambda d: d["egress"].pop("pinned_offline_lane"),
    lambda d: d["egress"].__setitem__("pinned_offline_lane", False),
], ids=["absent", "false"])
def test_osh_f64_spec_without_opt_in_emits_none_and_same_policies(tmp_path, mutate):
    c = _compose()
    on = _render(c, _copy_spec(tmp_path, "OSH-F64"), tmp_path / "on")
    off = _render(c, _copy_spec(tmp_path / "x", "OSH-F64", mutate), tmp_path / "off")
    _assert_none(_lane_enforcements(off))
    assert _policies(off) == _policies(on), "the gate must never change a worker policy"


@pytest.mark.parametrize("bad", [None, "yes", 1])
def test_osh_f64_spec_malformed_opt_in_fails_closed(tmp_path, bad):
    c = _compose()
    spec_file = _copy_spec(tmp_path, "OSH-F64",
                           lambda d: d["egress"].__setitem__("pinned_offline_lane", bad))
    with pytest.raises(c.CompositionError, match="pinned_offline_lane"):
        c.compose(str(spec_file), str(tmp_path / "out"))


def test_fleet_f62c_spec_cannot_trip_the_gate(tmp_path):
    c = _compose()
    _assert_none(_lane_enforcements(_render(c, _copy_spec(tmp_path, "FLEET-F62.c"), tmp_path / "plain")))

    def _all_osh_f64_fields_but_the_opt_in(data):
        data["egress"].update({
            "broker_addr": "172.17.0.1:18777",
            "filesystem_read_only": ["/opt/osh-lane"],
            "binaries": ["/usr/bin/python3*"],
            "chain_addr": "127.0.0.1:18255",
            "ca_bundle": "/etc/osh-lane/ca-bundle.pem",
        })

    spec_file = _copy_spec(tmp_path / "x", "FLEET-F62.c", _all_osh_f64_fields_but_the_opt_in)
    _assert_none(_lane_enforcements(_render(c, spec_file, tmp_path / "loaded")))


def test_osh_f63_spec_emits_none(tmp_path):
    c = _compose()
    _assert_none(_lane_enforcements(_render(c, _copy_spec(tmp_path, "OSH-F63"), tmp_path / "out")))


GITHUB = {
    "endpoints": ["api.github.com:443", "github.com:443"],
    "binaries": ["/usr/bin/gh", "/usr/bin/git"],
    "methods": ["GET", "POST", "PATCH", "PUT", "DELETE"],
}


def _with_builder_provider(provider: dict):
    def _mutate(data):
        data["providers"] = {"github": provider}
        data["types"]["builder"]["providers"] = ["github"]
    return _mutate


def _policy_for(out: Path, role: str) -> dict:
    (path,) = [p for p in out.rglob("policy-*.yaml") if p.stem.endswith(role)]
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_broker_and_opted_in_provider_render_together(tmp_path):
    """Broker and role-scoped provider egress coexist without widening other roles."""
    c = _compose()
    out = _render(c, _copy_spec(tmp_path, "OSH-F64", _with_builder_provider(GITHUB)), tmp_path / "out")
    doc = _policy_for(out, "builder")
    eps = {(str(e["host"]), int(e["port"])): e for e in doc["network_policies"]["worker-egress"]["endpoints"]}
    assert eps[("172.17.0.1", 18255)] == {"host": "172.17.0.1", "port": 18255, "tls": "skip"}
    assert eps[("172.17.0.1", 18777)] == {"host": "172.17.0.1", "port": 18777, "tls": "skip"}
    assert eps[("inference-api.nvidia.com", 443)]["protocol"] == "rest"
    for hp in (("api.github.com", 443), ("github.com", 443)):
        assert eps[hp]["protocol"] == "rest" and eps[hp]["enforcement"] == "enforce"
        assert sorted(r["allow"]["method"] for r in eps[hp]["rules"]) == sorted(GITHUB["methods"])
    bins = [b["path"] for b in doc["network_policies"]["worker-egress"]["binaries"]]
    assert bins == ["/usr/bin/curl", "/usr/bin/python3*", "/opt/hermes/.venv/bin/python",
                    "/usr/bin/gh", "/usr/bin/git"]
    assert "/opt/osh-lane" in doc["filesystem_policy"]["read_only"]
    other = _policy_for(out, "tester")
    hosts = {str(e["host"]) for e in other["network_policies"]["worker-egress"]["endpoints"]}
    assert not hosts & {"api.github.com", "github.com"}, "a worker that did not opt in gets no provider egress"


@pytest.mark.parametrize("mutate", [
    _with_builder_provider({**GITHUB, "endpoints": ["172.17.0.1:10256"]}),
    lambda d: (_with_builder_provider(GITHUB)(d), d["egress"].__setitem__("broker_addr", "172.17.0.1:22")),
], ids=["provider-control-plane", "broker-ssh-with-provider"])
def test_broker_and_provider_both_pass_the_forbidden_host_guard(tmp_path, mutate):
    c = _compose()
    spec_file = _copy_spec(tmp_path, "OSH-F64", mutate)
    with pytest.raises(c.CompositionError, match="forbidden"):
        c.compose(str(spec_file), str(tmp_path / "out"))


def _installer():
    path = _repo_root() / "plugins" / PLUGIN_KEY / "openshell" / "installer.py"
    spec = importlib.util.spec_from_file_location("osh_f64_lane_gate_installer", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _tree(*roots: Path) -> dict:
    return {str(p): p.read_bytes() for r in roots for p in sorted(r.rglob("*")) if p.is_file()}


_REF = "0" * 40


@pytest.mark.parametrize("source,mutate", [
    ("FLEET-F62.c", None),
    ("OSH-F64", lambda d: d["egress"].pop("pinned_offline_lane")),
    ("OSH-F64", lambda d: d["egress"].__setitem__("pinned_offline_lane", False)),
    ("OSH-F64", lambda d: d["egress"].__setitem__("pinned_offline_lane", "yes")),
    ("OSH-F64", lambda d: d["egress"].__setitem__("pinned_offline_lane", 1)),
    ("OSH-F64", lambda d: d["egress"].__setitem__("pinned_offline_lane", None)),
], ids=["fleet-f62c", "absent", "false", "string", "int", "null"])
def test_installer_refuses_a_spec_outside_the_lane_before_any_mutation(tmp_path, monkeypatch, source, mutate):
    """install-openshell's entry points refuse a spec that does not declare the pinned
    offline lane before collecting state, running a command or writing any file."""
    inst = _installer()
    home, managed = tmp_path / "home", tmp_path / "managed"
    home.mkdir()
    managed.mkdir()
    (home / "config.yaml").write_text("gateway: {multiplex_profiles: false}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))

    def _no_mutation(*_a, **_k):
        raise AssertionError("installer attempted a command or write for a refused spec")

    monkeypatch.setattr(subprocess, "run", _no_mutation)
    monkeypatch.setattr(inst, "_atomic_write_yaml", _no_mutation)
    monkeypatch.setattr(inst, "collect_home_state", _no_mutation)
    spec_file = _copy_spec(tmp_path, source, mutate)
    before = _tree(home, managed)
    for entry in (inst.plan_text, inst.phase_a, inst.apply):
        with pytest.raises(ValueError, match="pinned_offline_lane"):
            entry(str(spec_file), _REF)
    assert _tree(home, managed) == before, "a refused spec must leave HERMES_HOME and the managed dir untouched"


def test_installer_accepts_the_shipped_lane_spec():
    inst = _installer()
    spec_file = _spec_dir("OSH-F64") / "coworker-types.yaml"
    inst._require_openshell(inst.load_spec(spec_file))


def _plugin_module(tmp_path, monkeypatch):
    home, bundled = tmp_path / "hermes_home", tmp_path / "empty_bundled"
    (home / "plugins").mkdir(parents=True)
    bundled.mkdir()
    shutil.copytree(_repo_root() / "plugins" / PLUGIN_KEY, home / "plugins" / PLUGIN_KEY)
    (home / "config.yaml").write_text("plugins:\n  enabled: [nv-coworker-compose]\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    from hermes_cli.plugins import PluginManager

    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled is True and loaded.error is None
    return loaded.module


def _install_args(spec_file: Path, dry_run: bool):
    return argparse.Namespace(coworker_command="install-openshell", spec=str(spec_file), ref=_REF,
                              dry_run=dry_run, gateway_url="ws://127.0.0.1:9/?ticket=t", policy_root=None)


@pytest.mark.parametrize("dry_run", [False, True], ids=["real-run", "dry-run"])
def test_install_openshell_cli_refuses_a_non_lane_spec_before_the_gateway_preflight(
        tmp_path, monkeypatch, capsys, dry_run):
    """`hermes coworker install-openshell` refuses FLEET-F62.c's spec with exit 2 before it
    probes the gateway, so a refused spec never spends the single-use ticket."""
    mod = _plugin_module(tmp_path, monkeypatch)

    def _no_preflight(_url):
        raise AssertionError("gateway preflight reached for a refused spec")

    monkeypatch.setattr(mod, "_gateway_preflight", _no_preflight)
    rc = mod._cli_coworker(_install_args(_spec_dir("FLEET-F62.c") / "coworker-types.yaml", dry_run))
    out = json.loads(capsys.readouterr().out)
    assert rc == 2 and out["ok"] is False and "pinned_offline_lane" in out["error"]


def test_install_openshell_cli_lets_the_lane_spec_reach_the_gateway_preflight(tmp_path, monkeypatch):
    mod = _plugin_module(tmp_path, monkeypatch)
    probed = []

    def _refusing_preflight(url):
        probed.append(url)
        return False, "test stop"

    monkeypatch.setattr(mod, "_gateway_preflight", _refusing_preflight)
    with pytest.raises(RuntimeError, match="gateway preflight failed: test stop"):
        mod._cli_coworker(_install_args(_spec_dir("OSH-F64") / "coworker-types.yaml", False))
    assert probed == ["ws://127.0.0.1:9/?ticket=t"]
