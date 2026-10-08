"""Acceptance test for GOV-F26.a (ADOPT-verify) — the fleet-admin tool inventory
is centrally managed and IMMUTABLE to a non-orchestrator profile.

Ships as ``tests/plugins/test_gov_f26a_managed_admin_immutability_acceptance.py``.
Mirrors ``tests/hermes_cli/test_plugin_api_compat.py`` and the merged
``tests/plugins/test_gov_f26_fleet_admin_scope_acceptance.py``: loads
``nv-fleet-gates`` from an isolated ``HERMES_HOME`` with an EMPTY bundled-plugin
dir through the real ``PluginManager().discover_and_load()`` path, primes the
registry with ``model_tools.discover_builtin_tools()`` (so the gate's
``_restricted_block`` backstop is satisfied and cannot mask the fleet-admin
outcome), pins the fleet-admin inventory in a ``HERMES_MANAGED_DIR`` managed
``config.yaml`` BEFORE loading (the plugin captures ``admin_tools`` once at
register time from ``ctx.get_config``), then drives the one ``pre_tool_call``
gate and asserts the OUTCOME per acceptance criterion.

The immutability is a NATIVE property of the stock config loader: ``get_config``
reads the managed-merged effective config (managed wins), and ``_deep_merge``
replaces a *list* value wholesale — so a managed ``admin_tools`` list overrides a
profile-local ``admin_tools: []``. No plugin or core code is exercised beyond the
already-merged covering surface.

One ``def test_ac_gov_f26_a_<n>`` per ADR ``pytest:`` acceptance-criterion id.

MUST fail on the stock v2026.8.31 tree (``plugins/nv-fleet-gates/`` absent -> the
fixture's ``pytest.fail``) and pass on the baseline where the plugin is merged.
Behaviour contract only: the inventory is a fixture input, never a snapshot of the
plugin's own constants; no reading of source files; no network; nothing under
``~/.hermes``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from hermes_cli.plugins import PluginManager

PLUGIN_KEY = "nv-fleet-gates"
_REPO = Path(__file__).resolve().parents[2]
PLUGIN_SRC = _REPO / "plugins" / PLUGIN_KEY

# The complete fleet-admin inventory: five config-supplied gateway tools plus the
# vendored cron floor. A fixture input the operator pins in managed scope, NOT an
# assertion that the plugin defines exactly these names.
_CONFIG_HALF = ["onboard_coworker", "onboard_project", "create_agent",
                "record_decision", "record_human_verdict"]
_VENDORED = "cronjob_manage"
_INVENTORY = [*_CONFIG_HALF, _VENDORED]


def _base_settings(edges_db, **overrides):
    settings = {
        "profile_roles": {"orch": "orchestrator", "worker-a": "worker"},
        "enforce_sandbox": False,
        "expected_backend": "docker",
        "required_stages": ["OUTPUT_REVIEW"],
        "plan_gate": True,
        "edges_db_path": str(edges_db),
        "stage_markers": ["[Fix Report]", "[Review Verdict]"],
        # The profile-local override under test: a worker emptying its own set.
        "admin_tools": [],
    }
    settings.update(overrides)
    return settings


def _prime_registry(monkeypatch, hermes_home, bundled, managed_dir):
    monkeypatch.setenv("HOME", str(hermes_home.parent.parent / "fakehome"))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_dir))
    monkeypatch.delenv("HERMES_ENABLE_PROJECT_PLUGINS", raising=False)
    # importing tools.registry alone does NOT register the built-ins; the gate's
    # restricted-set backstop reads registry.get_all_tool_names(), so prime it.
    import model_tools

    model_tools.discover_builtin_tools()


def _load(tmp_path, monkeypatch, *, profile="worker-a", edges_db=None,
          managed_admin_tools=None, **settings_overrides):
    hermes_home = tmp_path / "home" / profile
    (hermes_home / "plugins").mkdir(parents=True)
    bundled = tmp_path / "empty-bundled"
    bundled.mkdir(exist_ok=True)
    edges_db = edges_db or (tmp_path / "fleet" / "edges.db")
    edges_db.parent.mkdir(parents=True, exist_ok=True)

    if not PLUGIN_SRC.exists():
        pytest.fail(f"plugin source missing: {PLUGIN_SRC}")

    shutil.copytree(PLUGIN_SRC, hermes_home / "plugins" / PLUGIN_KEY)

    settings = _base_settings(edges_db, **settings_overrides)
    cfg = {"plugins": {"enabled": [PLUGIN_KEY],
                       "entries": {PLUGIN_KEY: {"settings": settings}}}}
    (hermes_home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")

    # The managed dir always exists (so load_managed_config never falls through to
    # a stray /etc/hermes); its config.yaml — the admin_tools pin — is written only
    # when the case supplies one. profile_roles stays profile-local, so identity
    # resolution is unaffected by whether admin_tools is pinned.
    managed_dir = tmp_path / "managed" / profile
    managed_dir.mkdir(parents=True)
    if managed_admin_tools is not None:
        managed_cfg = {"plugins": {"entries": {PLUGIN_KEY: {
            "settings": {"admin_tools": list(managed_admin_tools)}}}}}
        (managed_dir / "config.yaml").write_text(
            yaml.safe_dump(managed_cfg), encoding="utf-8")

    _prime_registry(monkeypatch, hermes_home, bundled, managed_dir)
    manager = PluginManager()
    manager.discover_and_load()
    return manager, hermes_home, edges_db


def _loaded(manager):
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled is True
    assert loaded.error is None
    assert loaded.module is not None
    assert "pre_tool_call" in loaded.hooks_registered
    return loaded


def _gate(manager, tool_name, args=None, session_id="sess-1"):
    results = manager.invoke_hook(
        "pre_tool_call", tool_name=tool_name, args=args or {}, session_id=session_id,
        task_id="task-1", tool_call_id="tc-1", turn_id="turn-1")
    directives = [r for r in (results or []) if isinstance(r, dict) and r.get("action")]
    return directives[0] if directives else None


def _act(directive):
    return directive.get("action") if isinstance(directive, dict) else None


def _args_for(tool_name):
    return {"action": "add"} if tool_name == _VENDORED else {}


def test_ac_gov_f26_a_1(tmp_path, monkeypatch):
    """With the complete fleet-admin inventory pinned in the managed layer, a worker that sets its own local admin_tools:[] still has EVERY inventory member blocked (fail-closed escalation-attempt control)."""
    worker, _, _ = _load(tmp_path, monkeypatch, profile="worker-a",
                         managed_admin_tools=_INVENTORY)
    _loaded(worker)
    for tool_name in _INVENTORY:
        assert _act(_gate(worker, tool_name, _args_for(tool_name))) == "block", tool_name


def test_ac_gov_f26_a_2(tmp_path, monkeypatch):
    """Under the same managed pin and local empty override, the orchestrator profile is admitted for every inventory member (all six — no over-block)."""
    orch, _, _ = _load(tmp_path, monkeypatch, profile="orch",
                      managed_admin_tools=_INVENTORY)
    _loaded(orch)
    for tool_name in _INVENTORY:
        assert _act(_gate(orch, tool_name, _args_for(tool_name))) != "block", tool_name


def test_ac_gov_f26_a_3(tmp_path, monkeypatch):
    """Without a managed pin, the worker's local admin_tools:[] leaves a config-supplied member admitted while the vendored cronjob_manage stays blocked — immutability is conferred by the managed-scope pin, not profile-local config."""
    worker, _, _ = _load(tmp_path, monkeypatch, profile="worker-a",
                        managed_admin_tools=None)
    _loaded(worker)
    assert _act(_gate(worker, "onboard_coworker")) != "block"
    assert _act(_gate(worker, _VENDORED, _args_for(_VENDORED))) == "block"
