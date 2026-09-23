"""Acceptance test for GOV-F26 (ADOPT-verify) — cli_scope enforcement and the
host command gate, covered by the merged ``nv-fleet-gates`` fleet-admin scope.

Ships as ``tests/plugins/test_gov_f26_fleet_admin_scope_acceptance.py``. Mirrors
``tests/hermes_cli/test_plugin_api_compat.py``: loads ``nv-fleet-gates`` from an
isolated ``HERMES_HOME`` with an EMPTY bundled-plugin dir through the real
``PluginManager().discover_and_load()`` path, primes the registry with
``model_tools.discover_builtin_tools()`` so the gate's restricted-set backstop is
satisfied (and cannot mask the fleet-admin outcome), then drives the one
``pre_tool_call`` gate and asserts the OUTCOME per acceptance criterion.

One ``test_ac_gov_f26_<n>`` per ADR ``pytest:`` acceptance-criterion id.

MUST fail on the stock v2026.8.31 tree (``plugins/nv-fleet-gates/`` absent -> the
fixture's ``pytest.fail``) and pass on the baseline where the plugin is merged.
Behaviour contract only: no model lists, no version literals, no reading of
source files, no network, nothing under ``~/.hermes``.
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


def _base_settings(edges_db, **overrides):
    settings = {
        "profile_roles": {"orch": "orchestrator", "worker-a": "worker"},
        "enforce_sandbox": False,
        "expected_backend": "docker",
        "required_stages": ["OUTPUT_REVIEW"],
        "plan_gate": True,
        "edges_db_path": str(edges_db),
        "stage_markers": ["[Fix Report]", "[Review Verdict]"],
        "admin_tools": ["onboard_coworker", "onboard_project", "create_agent",
                        "record_decision", "record_human_verdict"],
    }
    settings.update(overrides)
    return settings


def _prime_registry(monkeypatch, hermes_home, bundled):
    monkeypatch.setenv("HOME", str(hermes_home.parent.parent / "fakehome"))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.delenv("HERMES_ENABLE_PROJECT_PLUGINS", raising=False)
    # importing tools.registry alone does NOT register the built-ins; the gate's
    # restricted-set backstop reads registry.get_all_tool_names(), so prime it.
    import model_tools

    model_tools.discover_builtin_tools()


def _load(tmp_path, monkeypatch, *, profile="worker-a", edges_db=None, **settings_overrides):
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

    _prime_registry(monkeypatch, hermes_home, bundled)
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


def test_ac_gov_f26_1(tmp_path, monkeypatch):
    """An admin-shaped tool in admin_tools is blocked by name for a worker and passes for the orchestrator."""
    worker, _, edges = _load(tmp_path, monkeypatch, profile="worker-a")
    _loaded(worker)
    assert _act(_gate(worker, "onboard_coworker")) == "block"

    orch, _, _ = _load(tmp_path, monkeypatch, profile="orch", edges_db=edges)
    _loaded(orch)
    assert _act(_gate(orch, "onboard_coworker")) != "block"


def test_ac_gov_f26_2(tmp_path, monkeypatch):
    """The vendored cronjob_manage is blocked for a worker with admin_tools=[]; its legacy alias cronjob is canonicalised to the same denial."""
    worker, _, _ = _load(tmp_path, monkeypatch, profile="worker-a", admin_tools=[])
    _loaded(worker)
    assert _act(_gate(worker, "cronjob_manage", {"action": "add"})) == "block"
    assert _act(_gate(worker, "cronjob", {"action": "add"})) == "block"


def test_ac_gov_f26_3(tmp_path, monkeypatch):
    """A managed profile_roles pin that omits the worker makes it default to worker; a self-added local role survives config merging but is ignored by the managed-only lookup, so the admin tool stays blocked."""
    managed = tmp_path / "managed"
    managed.mkdir()
    # The managed pin names ONLY the orchestrator; worker-a is deliberately absent,
    # so roles come solely from this map and worker-a falls to the default "worker".
    # A deep-merge of the local map below would instead let worker-a's own
    # "orchestrator" entry survive — this fixture is what tells the two apart.
    (managed / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"entries": {PLUGIN_KEY: {
            "settings": {"profile_roles": {"orch": "orchestrator"}}}}}}),
        encoding="utf-8")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    manager, _, _ = _load(tmp_path, monkeypatch, profile="worker-a",
                          profile_roles={"worker-a": "orchestrator", "orch": "orchestrator"})
    _loaded(manager)
    assert _act(_gate(manager, "onboard_coworker")) == "block"
