"""Unit contracts for the OSH-F64.e direct-mode inference render (nv-coworker-compose).

Beside the acceptance file: the rendered direct-mode ``api_key`` is exactly OpenShell's canonical
resolve placeholder for ``credential_env`` (openshell-core secrets.rs:9, :489-491), the model route
carries each of the four rules once, the gateway plan line shlex-quotes the spec image, and the
managed (OSH-F64.b) ``api_key`` stays the fixed trigger. No network; imports compose.py directly.
"""

from __future__ import annotations

import importlib.util
import shlex
import sys
from pathlib import Path

import pytest
import yaml

PLUGIN_KEY = "nv-coworker-compose"
REPO_ROOT = Path(__file__).resolve().parents[2]
DIRECT_SPEC = REPO_ROOT / "tests" / "e2e-scenarios" / "OSH-F64.e" / "spec" / "coworker-types.yaml"
MANAGED_SPEC = REPO_ROOT / "tests" / "e2e-scenarios" / "OSH-F64.b" / "spec" / "coworker-types.yaml"


def _compose():
    path = REPO_ROOT / "plugins" / PLUGIN_KEY / "compose.py"
    spec = importlib.util.spec_from_file_location("osh_f64_e_compose_unit", path)
    mod = importlib.util.module_from_spec(spec)
    # compose.py's frozen @dataclass resolves cls.__module__ via sys.modules at class creation.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def compose_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    return _compose()


def _load(path):
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _direct_block():
    return _load(DIRECT_SPEC)["egress"]["inference_provider"]


def test_direct_render_api_key_is_the_canonical_openshell_placeholder(compose_mod, tmp_path):
    key = _direct_block()["credential_env"]
    compose_mod.compose(str(DIRECT_SPEC), str(tmp_path / "out"))
    configs = list((tmp_path / "out").rglob("config.yaml"))
    keys = set()
    for path in configs:
        cfg = _load(path)
        if "model" not in cfg:
            continue
        keys.add(cfg["providers"][cfg["model"]["provider"]]["api_key"])
    assert keys == {f"openshell:resolve:env:{key}"}


def test_direct_model_route_lists_each_rule_once(compose_mod, tmp_path):
    compose_mod.compose(str(DIRECT_SPEC), str(tmp_path / "out"))
    gw = _load(tmp_path / "out" / "default" / "policy-gateway.yaml")
    eps = [ep for net in gw["network_policies"].values() for ep in net["endpoints"]
           if ep["host"] == "inference-api.nvidia.com"]
    assert len(eps) == 1
    rules = [(r["allow"]["method"], r["allow"]["path"]) for r in eps[0]["rules"]]
    assert len(rules) == len(set(rules)) == 4


def test_gateway_plan_line_quotes_the_spec_image(compose_mod):
    data = _load(DIRECT_SPEC)
    data["egress"]["inference_provider"]["gateway_image"] = "img:tag; rm -rf /"
    plan = compose_mod.build_provision_plan(data, compose_mod._resolve_substrate(data))
    gw = [shlex.split(line) for line in plan if " --name osh-f64b-gw " in line]
    assert len(gw) == 1
    argv = gw[0]
    assert argv[argv.index("--from") + 1] == "img:tag; rm -rf /"
    assert plan[0].startswith("openshell sandbox create --name osh-f64b-gw "), (
        "the gateway exists before the workers")


def test_managed_render_keeps_the_fixed_trigger(compose_mod, tmp_path):
    trigger = _load(MANAGED_SPEC)["egress"]["inference_provider"]["rewrite_placeholder"]
    compose_mod.compose(str(MANAGED_SPEC), str(tmp_path / "out"))
    cfg = _load(tmp_path / "out" / "architect" / "config.yaml")
    assert cfg["providers"][cfg["model"]["provider"]]["api_key"] == trigger


@pytest.mark.parametrize("mutate", [
    pytest.param(lambda ip: ip.update(upstream_base_url="https://other.example/v1"),
                 id="upstream-differs-from-base-url"),
    pytest.param(lambda ip: ip.update(credential_provider="bad name"), id="unsafe-provider-name"),
    pytest.param(lambda ip: ip.pop("credential_env"), id="no-credential-env"),
    pytest.param(lambda ip: ip.pop("credential_provider"), id="no-credential-provider"),
])
def test_direct_validator_fails_closed(compose_mod, mutate):
    block = dict(_direct_block())
    mutate(block)
    with pytest.raises(compose_mod.CompositionError):
        compose_mod._validate_openshell_inference_provider(block)


def test_gateway_sandbox_name_cannot_collide_with_a_type(compose_mod):
    data = _load(DIRECT_SPEC)
    data["types"]["gw"] = {"extends": ["base"], "identity": "collides with the gateway sandbox"}
    with pytest.raises(compose_mod.CompositionError):
        compose_mod.build_provision_plan(data, compose_mod._resolve_substrate(data))
