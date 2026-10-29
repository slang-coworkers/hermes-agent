"""OSH-F64.f acceptance: the openshell installer reconciles an EXISTING gateway default profile's
model route to the rendered default under the single-authority posture (egress.inference_provider).

The oracle is the render itself: ``compose()`` writes ``<out>/default/config.yaml`` with the
single-authority provider block (compose.py:4466-4470), and the installed default must end with
the same route. Shape: tests/plugins/test_osh_f64_b_acceptance.py (isolated HERMES_HOME, empty
bundled dir, real PluginManager discovery; installer loaded by path from the discovered copy).
"""
import copy
import importlib.util
import os
import shutil
from pathlib import Path

import pytest
import hermes_yaml as yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_KEY = "nv-coworker-compose"
PLUGIN_SRC = REPO_ROOT / "plugins" / PLUGIN_KEY
# The committed OSH-F64.b single-authority spec: substrate openshell + egress.inference_provider
# (provider compatible-endpoint, attribution_tag_prefix osh-f64b, default_profile default).
SPEC_DIR = REPO_ROOT / "tests" / "e2e-scenarios" / "OSH-F64.b" / "spec"
SPEC = SPEC_DIR / "coworker-types.yaml"
BACKUP_NAME = "config.yaml.osh-f64.bak"

# An existing gateway default whose route predates the install: a foreign primary provider, a
# foreign block under the SAME key the render selects, and every second route the render scrubs
# (compose.py:1783-1838), each carrying a distinct foreign credential or host.
FOREIGN_DEFAULT = {
    "model": {
        "provider": "openrouter",
        "default": "openrouter/foreign-model",
        "api_key": "sk-or-FOREIGN",
        "api": "https://model-api.example/v1",
        "base_url": "https://openrouter.ai/api/v1",
        "api_base": "https://model-apibase.example/v1",
        "api_mode": "chat_completions",
        "key_env": "FOREIGN_MODEL_KEY_ENV",
        "api_key_env": "FOREIGN_MODEL_API_KEY_ENV",
        "default_headers": {"Authorization": "Bearer FOREIGN-MODEL-HEADER"},
        "extra_headers": {"Authorization": "Bearer FOREIGN-MODEL-EXTRA"},
        "context_length": 200000,
    },
    "providers": {
        "openrouter": {"base_url": "https://openrouter.ai/api/v1", "api_key": "sk-or-FOREIGN"},
        "compatible-endpoint": {"base_url": "https://foreign.example/v1", "api_key": "sk-FOREIGN",
                                "request_timeout_seconds": 30},
    },
    "custom_providers": [{"name": "legacy", "base_url": "https://inference.local/v1", "api_key": "sk-LEGACY"}],
    "provider": "root-foreign-provider",
    "base_url": "https://root.example/v1",
    "api_base": "https://root-apibase.example/v1",
    "delegation": {"provider": "openrouter", "base_url": "https://deleg.example/v1", "api_key": "sk-DELEG",
                   "api_mode": "chat_completions",
                   "request_overrides": {"extra_headers": {"Authorization": "Bearer FOREIGN-DELEG-HEADER"}},
                   "model": "operator-deleg-model", "max_iterations": 7},
    "fallback_model": {"provider": "openrouter", "model": "foreign-fallback", "api_key": "sk-FALLBACK"},
    "fallback_providers": [{"provider": "openrouter", "api_key": "sk-FALLBACK2"}],
    "auxiliary": {
        "vision": {"provider": "openrouter", "base_url": "https://aux.example/v1", "api_mode": "chat_completions",
                   "api_key": "sk-AUX", "key_env": "FOREIGN_AUX_KEY_ENV", "api_key_env": "FOREIGN_AUX_API_KEY_ENV",
                   "fallback_chain": [{"provider": "openrouter"}], "model": "operator-vision-model"},
        # A task name holding a dot: the dotted-key diff vocabulary must not split it (ADR §D3).
        "vision.v2": {"api_key": "sk-DOTTED", "model": "operator-dotted-model"},
    },
    "terminal": {"backend": "openshell"},
    "operator_top_level": {"keep": ["verbatim", 1]},
    "plugins": {"enabled": ["operator-plugin"],
                "entries": {"operator-plugin": {"settings": {"keep": "me"}}}},
}
FOREIGN_ROUTE_KEYS = (
    "custom_providers", "provider", "base_url", "api_base", "fallback_model", "fallback_providers",
    "model.api_key", "model.api", "model.base_url", "model.api_base", "model.api_mode", "model.key_env",
    "model.api_key_env", "model.default_headers", "model.extra_headers",
    "delegation.provider", "delegation.base_url", "delegation.api_key", "delegation.api_mode",
    "delegation.request_overrides",
    "auxiliary.vision.provider", "auxiliary.vision.base_url", "auxiliary.vision.api_mode",
    "auxiliary.vision.api_key", "auxiliary.vision.key_env", "auxiliary.vision.api_key_env",
    "auxiliary.vision.fallback_chain",
)
FOREIGN_MARKERS = ("sk-or-FOREIGN", "sk-FOREIGN", "sk-LEGACY", "sk-DELEG", "sk-AUX", "sk-DOTTED",
                   "sk-FALLBACK", "FOREIGN-MODEL-HEADER", "FOREIGN-MODEL-EXTRA", "FOREIGN-DELEG-HEADER",
                   "FOREIGN_MODEL_KEY_ENV", "FOREIGN_AUX_KEY_ENV", "openrouter.ai", "foreign.example",
                   "root.example", "deleg.example", "aux.example", "model-api.example", "apibase.example")
STRING_MODEL_DEFAULT = {"model": "openrouter/foreign-model",
                        "providers": {"openrouter": {"api_key": "sk-or-FOREIGN"}}}
OPERATOR_KEYS = ("model.context_length", "delegation.model", "delegation.max_iterations",
                 "auxiliary.vision.model", "terminal", "operator_top_level",
                 "plugins.entries.operator-plugin")
PRE_FIX_TOP_LEVEL = ("gateway", "plugins", "secrets")
ROUTE_SUBTREES = ("model", "providers", "custom_providers", "provider", "base_url", "api_base", "delegation",
                  "fallback_model", "fallback_providers", "auxiliary", "attribution_tag")


def _get(cfg, dotted):
    node = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


@pytest.fixture()
def loaded(tmp_path, monkeypatch):
    """Load nv-coworker-compose from an isolated HERMES_HOME via the real scanner."""
    hermes_home = tmp_path / "home"
    (hermes_home / "plugins").mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, hermes_home / "plugins" / PLUGIN_KEY,
                    ignore=shutil.ignore_patterns("__pycache__"))
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": [PLUGIN_KEY]}}), encoding="utf-8")
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")

    from hermes_cli.plugins import PluginManager

    manager = PluginManager()
    manager.discover_and_load()
    plugin = manager._plugins[PLUGIN_KEY]
    assert plugin.enabled is True
    assert plugin.error is None
    assert plugin.module is not None
    assert "coworker" in manager._cli_commands
    return plugin


@pytest.fixture()
def gateway(loaded, tmp_path, monkeypatch):
    """The installer of the DISCOVERED plugin copy, pointed at a separate gateway home whose
    config.yaml is the existing default profile it edits in place."""
    path = Path(os.environ["HERMES_HOME"]) / "plugins" / PLUGIN_KEY / "openshell" / "installer.py"
    spec = importlib.util.spec_from_file_location("osh_f64_f_installer", path)
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    home = tmp_path / "gateway-home"
    home.mkdir()
    monkeypatch.setattr(installer, "_require_home", lambda: str(home))
    monkeypatch.setattr(installer, "_managed_dir", lambda: None)
    return installer, home


@pytest.fixture()
def rendered_default(loaded, tmp_path):
    """The render's own default profile config for the single-authority spec (the oracle)."""
    out = tmp_path / "render"
    loaded.module.compose(str(SPEC), str(out))
    cfg = yaml.safe_load((out / "default" / "config.yaml").read_text(encoding="utf-8"))
    assert _get(cfg, "providers")[0] and _get(cfg, "model.provider")[0], (
        "precondition: the single-authority render writes the default profile's provider route")
    return cfg


def _write_default(home, cfg):
    (home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")


def _read_default(home):
    return yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))


def _install_default(installer, home, spec_path=SPEC):
    """Run the two in-place default-profile steps of a real apply — the backup, then the
    default edit — through the installer's own executor (installer.py `_execute_step`)."""
    ctx = {"home": str(home), "managed_dir": "", "spec_path": str(spec_path)}
    installer._execute_step(installer.Step("# backup", "backup"), ctx)
    installer._execute_step(installer.Step("# edit default config", "default_edit"), ctx)
    return _read_default(home)


def _pre_fix_installed(installer, existing):
    """A default the pre-fix installer left behind: every key it owned is applied, the route is not."""
    _, diff, _ = installer.default_config_diff(existing, str(SPEC))
    owned = {k: v for k, v in diff.items() if k.split(".")[0] in PRE_FIX_TOP_LEVEL}
    return installer.apply_config_diff(existing, owned)


def _route(cfg):
    return {
        "providers": _get(cfg, "providers")[1],
        "model.provider": _get(cfg, "model.provider")[1],
        "model.default": _get(cfg, "model.default")[1],
        "attribution_tag": _get(cfg, "attribution_tag")[1],
    }


def test_ac_osh_f64_f_1(gateway, rendered_default):
    """Under a spec carrying egress.inference_provider, installing over an existing default with a foreign route (a mapping or the legacy string `model` form) leaves its providers, model.provider, model.default and attribution_tag equal to the rendered default, with no foreign route or credential surviving and the original kept once in config.yaml.osh-f64.bak."""
    installer, home = gateway
    _write_default(home, FOREIGN_DEFAULT)
    original = (home / "config.yaml").read_bytes()
    _, _, backup_required = installer.default_config_diff(copy.deepcopy(FOREIGN_DEFAULT), str(SPEC))
    assert backup_required, "a config-changing install of a home without a backup must take one"

    installed = _install_default(installer, home)

    assert _route(installed) == _route(rendered_default), (
        "the installed default's route must equal the rendered default/config.yaml route")
    for dotted in FOREIGN_ROUTE_KEYS:
        assert not _get(installed, dotted)[0], f"the foreign route key {dotted} must not survive"
    assert set(installed["providers"]) == set(rendered_default["providers"]), (
        "providers is replaced whole: no foreign provider block survives beside the rendered one")
    dumped = yaml.safe_dump(installed)
    for marker in FOREIGN_MARKERS:
        assert marker not in dumped, f"the foreign credential/host {marker!r} must not survive"
    assert (home / BACKUP_NAME).read_bytes() == original, "the backup is the original default, byte for byte"

    # The legacy string form of `model` is a route too: it is replaced, not refused.
    string_home = home.parent / "gateway-home-string-model"
    string_home.mkdir()
    _write_default(string_home, STRING_MODEL_DEFAULT)
    assert _route(_install_default(installer, string_home)) == _route(rendered_default), (
        "a string-form model must be reconciled to the rendered default route")


def test_ac_osh_f64_f_2(gateway, rendered_default):
    """A default the pre-fix installer already installed (its keys applied, config.yaml.osh-f64.bak present, route still foreign) is reconciled on the next run without a second backup, and the existing backup stays byte-identical."""
    installer, home = gateway
    pre_fix = _pre_fix_installed(installer, copy.deepcopy(FOREIGN_DEFAULT))
    _write_default(home, pre_fix)
    (home / BACKUP_NAME).write_text(yaml.safe_dump(FOREIGN_DEFAULT), encoding="utf-8")
    first_backup = (home / BACKUP_NAME).read_bytes()

    _, diff, backup_required = installer.default_config_diff(copy.deepcopy(pre_fix), str(SPEC),
                                                             backup_present=True)
    assert diff, "a pre-fix installed home on a foreign route must plan the default edit"
    assert not backup_required, "a home that already has its backup must not take a second one"

    installed = _install_default(installer, home)

    assert _route(installed) == _route(rendered_default), (
        "the re-run must reconcile the pre-fix home's route to the rendered default")
    assert (home / BACKUP_NAME).read_bytes() == first_backup, (
        "the first install's backup must stay byte-identical on the re-run")


def test_ac_osh_f64_f_3(gateway, rendered_default):
    """Re-running the install on a reconciled default plans no edit and no backup: default_config_diff returns an empty diff with backup_required False, and re-executing the edit leaves the default and its backup byte-identical."""
    installer, home = gateway
    _write_default(home, FOREIGN_DEFAULT)
    assert _route(_install_default(installer, home)) == _route(rendered_default), (
        "precondition: the first run reconciled the route")
    reconciled = (home / "config.yaml").read_bytes()
    backup = (home / BACKUP_NAME).read_bytes()

    _, diff, backup_required = installer.default_config_diff(_read_default(home), str(SPEC),
                                                             backup_present=True)
    assert diff == {}, f"a second run must plan no default edit, got {sorted(diff)}"
    assert not backup_required, "a second run must not take a backup"
    # Even re-executed by hand, the edit is a byte-level fixed point and the backup is never retaken.
    _install_default(installer, home)
    assert (home / "config.yaml").read_bytes() == reconciled
    assert (home / BACKUP_NAME).read_bytes() == backup


def test_ac_osh_f64_f_4(gateway, tmp_path):
    """Under a spec without egress.inference_provider, the install leaves the existing default's model, providers and every other route subtree exactly as they were (the pre-fix behaviour)."""
    installer, home = gateway
    legacy_dir = tmp_path / "legacy-spec"
    shutil.copytree(SPEC_DIR, legacy_dir)
    legacy_spec = legacy_dir / "coworker-types.yaml"
    data = yaml.safe_load(legacy_spec.read_text(encoding="utf-8"))
    data["egress"].pop("inference_provider")
    legacy_spec.write_text(yaml.safe_dump(data), encoding="utf-8")

    _, diff, _ = installer.default_config_diff(copy.deepcopy(FOREIGN_DEFAULT), str(legacy_spec))
    assert all(k.split(".")[0] in PRE_FIX_TOP_LEVEL for k in diff), (
        f"without inference_provider the edit must stay on the pre-fix keys, got {sorted(diff)}")
    _write_default(home, FOREIGN_DEFAULT)
    installed = _install_default(installer, home, legacy_spec)
    for key in ROUTE_SUBTREES:
        assert _get(installed, key) == _get(FOREIGN_DEFAULT, key), (
            f"without inference_provider the default's {key} must be left exactly as it was")


def test_ac_osh_f64_f_5(gateway, rendered_default):
    """Operator keys outside the reconciled route/auth set (model.context_length, delegation.model and limits, an auxiliary task's bare model, unrelated top-level keys and plugin entries) survive the reconcile verbatim."""
    installer, home = gateway
    _write_default(home, FOREIGN_DEFAULT)

    installed = _install_default(installer, home)

    assert _route(installed) == _route(rendered_default), "precondition: the route was reconciled"
    for dotted in OPERATOR_KEYS:
        assert _get(installed, dotted) == _get(FOREIGN_DEFAULT, dotted), (
            f"the operator key {dotted} must survive the reconcile verbatim")
    assert installed["auxiliary"]["vision.v2"] == {"model": "operator-dotted-model"}, (
        "a dotted task name keeps its bare model and loses only its route/auth keys")
    assert "operator-plugin" in installed["plugins"]["enabled"], (
        "an operator-enabled plugin must survive the plugins.enabled union")
