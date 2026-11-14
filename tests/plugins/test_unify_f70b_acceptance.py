"""Acceptance: nv-coworker-compose fixes for OpenShell 0.1 installs, the worker Dockerfile lint, the env-placeholder
prefix, the v01 unit test's inputs and unused imports.

One test per acceptance criterion. Synthetic inputs are built under ``tmp_path``; the shipped Dockerfile and the
plugin files are read as data, and nothing is read from ``tests/e2e-scenarios``.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import hermes_yaml as yaml

from hermes_cli.plugins import PluginManager

REPO = Path(__file__).resolve().parents[2]
PLUGIN_KEY = "nv-coworker-compose"
PLUGIN_SRC = REPO / "plugins" / PLUGIN_KEY
OPENSHELL = PLUGIN_SRC / "openshell"
V01_UNIT = REPO / "tests" / "plugins" / "test_nv_coworker_compose_openshell_v01_unit.py"
REFUSED_DIR = REPO / "tests" / "e2e-scenarios"
PREFLIGHT_TEST = "test_local_install_openshell_takes_a_0072_inference_spec_to_the_gateway_preflight"
SHA = "a" * 40
# Assembled at run time so that no file in the repository carries the prefix as one literal.
PREFIX = ":".join(("open" + "shell", "re" + "solve", "env")) + ":"

# Runs pytest with every open/listdir/scandir under argv[1] refused; exits 97 if the refusal is not armed.
REFUSING_PYTEST = """
import os, sys
refused = os.path.abspath(sys.argv[1])

def _refuse(event, args):
    if event in ("open", "os.listdir", "os.scandir") and args and isinstance(args[0], (str, bytes, os.PathLike)):
        path = os.path.abspath(os.fsdecode(args[0]))
        if path == refused or path.startswith(refused + os.sep):
            raise PermissionError("read refused: " + path)

sys.addaudithook(_refuse)
try:
    open(os.path.join(refused, "probe"), encoding="utf-8")
except PermissionError:
    pass
else:
    sys.exit(97)
import pytest
sys.exit(pytest.main(sys.argv[2:]))
"""


def _openshell(name: str):
    spec = importlib.util.spec_from_file_location(f"unify_f70b_acceptance_{name}", OPENSHELL / f"{name}.py")
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


@pytest.fixture
def installer(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(tmp_path / "managed"))
    return _openshell("installer")


@pytest.fixture
def dockerfile_lint():
    return _openshell("dockerfile_lint")


def _v01_spec(tmp_path: Path) -> Path:
    """An openshell_api 0.1 spec with an inference provider and the profile file it names."""
    root = tmp_path / "spec"
    (root / "spines").mkdir(parents=True)
    profile = root / "example-llm.yaml"
    profile.write_text(yaml.safe_dump({
        "id": "example-llm",
        "endpoints": [{"host": "llm.example.com", "port": 443}],
        "credentials": [{"env_vars": ["PROVIDER_API_KEY"]}],
    }), encoding="utf-8")
    (root / "spines" / "base.yaml").write_text(
        yaml.safe_dump({"config": {"plugins": {"enabled": [PLUGIN_KEY]}}}), encoding="utf-8")
    spec = {
        "project": "fleet", "substrate": "openshell", "default_profile": "default",
        "egress": {
            "openshell_api": "0.1", "sandbox_image": "registry.example.com/hermes/worker:1",
            "inference_provider": {
                "provider": "custom", "base_url": "https://llm.example.com/v1", "api_mode": "chat_completions",
                "model_id": "example-model", "credential_env": "PROVIDER_API_KEY",
                "attribution_tag_prefix": "fleet", "profile": "example-llm", "profile_path": str(profile),
            },
        },
        "spines": {"base": {"source": "spines/base.yaml"}},
        "types": {"builder": {"extends": ["base"]}},
    }
    path = root / "coworker-types.yaml"
    path.write_text(yaml.safe_dump(spec), encoding="utf-8")
    return path


def _flat(tree: dict, prefix: str = "") -> dict:
    out = {}
    for key, value in tree.items():
        if isinstance(value, dict):
            out.update(_flat(value, f"{prefix}{key}."))
        else:
            out[f"{prefix}{key}"] = value
    return out


def _dockerfile(tmp_path: Path, run: str) -> Path:
    path = tmp_path / "Dockerfile"
    path.write_text(f"FROM registry.example.com/base@sha256:{'0' * 64}\nRUN {run}\nUSER 1000\n", encoding="utf-8")
    return path


def _direct_block() -> dict:
    return {"mode": "direct", "provider": "direct-endpoint", "base_url": "https://llm.example.com/v1",
            "upstream_base_url": "https://llm.example.com/v1", "api_mode": "chat_completions",
            "model_id": "example-model", "credential_provider": "example-inference",
            "credential_env": "PROVIDER_API_KEY", "gateway_image": "example-gateway:1",
            "attribution_tag_prefix": "fleet"}


def test_ac_unify_f70_b_5(installer, tmp_path):
    """`build_plan` returns a plan, with its default-config edit, for an `openshell_api: "0.1"` spec that declares `egress.inference_provider`"""
    spec = _v01_spec(tmp_path)
    steps = installer.build_plan(installer.collect_home_state(installer.load_spec(spec), spec), str(spec), SHA)
    assert "default_edit" in [step.tag for step in steps]


def test_ac_unify_f70_b_6(installer, tmp_path):
    """(derived) That spec's default-config diff follows the 0.1 rules: `model.provider` is the spec's provider and `providers.<provider>.api_key` is `${env:<credential_env>}`"""
    _, diff, _ = installer.default_config_diff({}, str(_v01_spec(tmp_path)))
    flat = _flat(diff)
    assert flat["model.provider"] == "custom"
    assert flat["providers.custom.api_key"] == "${env:PROVIDER_API_KEY}"


def test_ac_unify_f70_b_7(dockerfile_lint, tmp_path):
    """`dockerfile_lint` accepts a plain `apt-get upgrade`: neither a `RUN apt-get -y upgrade` nor the shipped `openshell/Dockerfile.worker-0.1` draws a `no-apt-upgrade` finding"""
    plain = dockerfile_lint.lint(str(_dockerfile(tmp_path, "apt-get update && apt-get -y upgrade")))
    assert "no-apt-upgrade" not in [finding[1] for finding in plain.findings], plain.report
    shipped = dockerfile_lint.lint(str(OPENSHELL / "Dockerfile.worker-0.1"))
    assert "no-apt-upgrade" not in [finding[1] for finding in shipped.findings], shipped.report


def test_ac_unify_f70_b_8(dockerfile_lint, tmp_path):
    """(derived) `apt-get dist-upgrade` and `apt-get full-upgrade` each still draw a `no-apt-upgrade` ERROR"""
    for verb in ("dist-upgrade", "full-upgrade"):
        result = dockerfile_lint.lint(str(_dockerfile(tmp_path, f"apt-get update && apt-get -y {verb}")))
        assert (dockerfile_lint.ERROR, "no-apt-upgrade") in [finding[:2] for finding in result.findings], result.report


def test_ac_unify_f70_b_9(compose):
    """(derived) The direct-mode credential is unchanged: the validated block's `api_key`, and the `providers.<provider>.api_key` the enforcer writes, are OpenShell's env-placeholder prefix followed by `credential_env`"""
    resolved = compose._validate_openshell_inference_provider(_direct_block())
    assert resolved["api_key"] == PREFIX + "PROVIDER_API_KEY"
    config: dict = {}
    compose._enforce_openshell_inference_provider(config, "default", resolved)
    assert config["providers"]["direct-endpoint"]["api_key"] == PREFIX + "PROVIDER_API_KEY"


def test_ac_unify_f70_b_10():
    """No file under `plugins/nv-coworker-compose/`, and not the v01 unit test, carries OpenShell's env-placeholder prefix as one literal"""
    files = [path for path in PLUGIN_SRC.rglob("*") if path.is_file() and "__pycache__" not in path.parts]
    carriers = [str(path.relative_to(REPO)) for path in [*files, V01_UNIT] if PREFIX.encode() in path.read_bytes()]
    assert carriers == []


def test_ac_unify_f70_b_11():
    """The v01 unit test file passes with every read under `tests/e2e-scenarios/` refused, and both cases of its 0.0.72 gateway-preflight test still pass"""
    proc = subprocess.run(
        [sys.executable, "-c", REFUSING_PYTEST, str(REFUSED_DIR), "-q", "-rA", "-p", "no:cacheprovider",
         "--rootdir", str(REPO), str(V01_UNIT)],
        cwd=REPO, capture_output=True, text=True, timeout=900, check=False)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output[-4000:]
    passed = re.findall(rf"^PASSED \S*::{PREFLIGHT_TEST}\[", output, re.MULTILINE)
    assert len(passed) >= 2, output[-4000:]


def test_ac_unify_f70_b_12():
    """`ruff check --select F401` reports no unused import in `compose.py` or `openshell/installer.py`"""
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--isolated", "--no-cache", "--select", "F401",
         "--output-format", "concise", str(PLUGIN_SRC / "compose.py"), str(OPENSHELL / "installer.py")],
        cwd=REPO, capture_output=True, text=True, timeout=120, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
