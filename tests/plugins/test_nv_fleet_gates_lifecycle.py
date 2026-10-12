"""Lifecycle regression for nv-fleet-gates' restricted-set validation.

The acceptance-test fixture primes a fully-discovered registry, so it cannot
exercise the paths where ``register()`` runs against a still-initializing
registry: ``hermes plugins doctor`` and ``hermes kanban dispatch`` at 0/12, an
in-process dashboard profile-switch reload at 1/12. Refusing to load there would
DISABLE the plugin, removing the veto = fail OPEN. These pin the required
behaviour: a partial registry loads the veto (never fail-open); the gate-time
backstop fails CLOSED if restricted core is genuinely absent at gate time; and a
genuine removal from a complete registry still fail-loads (AC-GOV-F22-1).
Behaviour contract only — no source reads, no network, nothing under ~/.hermes.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from hermes_cli.plugins import PluginManager
from tools.registry import registry

PLUGIN_KEY = "nv-fleet-gates"
_REPO = Path(__file__).resolve().parents[2]
PLUGIN_SRC = _REPO / "plugins" / PLUGIN_KEY

def _canonical_full(mgr):
    """The restricted-core canonical names, read from the plugin's OWN group
    table — single source of truth, not a hard-coded enumeration that would rot
    against a rename. One spelling per group is enough for the presence check."""
    groups = mgr._plugins[PLUGIN_KEY].module._CORE_PRESENCE_GROUPS
    return [grp[-1] for grp in groups]


class _View:
    """Mutable registry view so a test can change what get_all_tool_names returns
    between load time and gate time (register() and the gate both read it)."""

    def __init__(self, names):
        self.names = list(names)


def _install(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "plugins").mkdir(parents=True)
    bundled = tmp_path / "empty-bundled"
    bundled.mkdir()
    if not PLUGIN_SRC.exists():
        pytest.fail(f"plugin source missing: {PLUGIN_SRC}")
    shutil.copytree(PLUGIN_SRC, home / "plugins" / PLUGIN_KEY)
    cfg = {"plugins": {"enabled": [PLUGIN_KEY], "entries": {PLUGIN_KEY: {"settings": {
        "profile": "worker-a",
        "profile_roles": {"worker-a": "worker", "orch": "orchestrator"},
        "edges_db_path": str(tmp_path / "fleet-edges.db"),
        "plan_gate": False,
        "enforce_sandbox": False,
        "stage_markers": ["[Fix Report]"],
        "required_stages": ["OUTPUT_REVIEW"],
    }}}}}
    (home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.delenv("HERMES_ENABLE_PROJECT_PLUGINS", raising=False)
    return home


def _load(monkeypatch, view):
    """Patch the registry view, then load the plugin through the real manager."""
    monkeypatch.setattr(registry, "get_all_tool_names", lambda *a, **k: list(view.names))
    mgr = PluginManager()
    mgr.discover_and_load()
    return mgr


def _gate(mgr, tool_name, args):
    results = mgr.invoke_hook(
        "pre_tool_call", tool_name=tool_name, args=args, session_id="s1",
        task_id="t", tool_call_id="tc", turn_id="tn")
    directives = [r for r in (results or []) if isinstance(r, dict) and r.get("action")]
    return directives[0] if directives else None


def test_partial_registry_loads_veto_not_fail_open(tmp_path, monkeypatch):
    """1/12 restricted groups present (profile-switch reload) loads the veto, never fail-open."""
    home = _install(tmp_path, monkeypatch)
    mgr = _load(monkeypatch, _View(["skill_manage"]))
    loaded = mgr._plugins[PLUGIN_KEY]
    assert loaded.enabled is True
    assert loaded.error is None
    assert "pre_tool_call" in loaded.hooks_registered


def test_kanban_dispatch_empty_registry_loads_veto(tmp_path, monkeypatch):
    """0/12 present (the kanban-dispatch snapshot) also loads the veto, not fail-load."""
    home = _install(tmp_path, monkeypatch)
    mgr = _load(monkeypatch, _View([]))
    loaded = mgr._plugins[PLUGIN_KEY]
    assert loaded.enabled is True
    assert loaded.error is None
    assert "pre_tool_call" in loaded.hooks_registered


def test_partial_load_then_veto_gates_unwired(tmp_path, monkeypatch):
    """Once discovery completes the backstop clears; the veto then gates on policy, not on the partial-registry block."""
    home = _install(tmp_path, monkeypatch)
    view = _View(["skill_manage"])
    mgr = _load(monkeypatch, view)
    assert mgr._plugins[PLUGIN_KEY].enabled is True
    view.names = _canonical_full(mgr)   # registry complete by the first real tool call
    # backstop cleared: a benign, non-restricted call passes (proves the
    # partial->complete transition, not a blanket block)
    assert _gate(mgr, "read_file", {}) is None
    # and the veto still gates policy: an unwired message_agent is refused
    d = _gate(mgr, "message_agent", {"target": "orch", "message": "ping"})
    assert isinstance(d, dict) and d.get("action") == "block"


def test_lazy_backstop_blocks_when_genuinely_missing(tmp_path, monkeypatch):
    """If restricted core is still absent at gate time, the gate fails CLOSED (block), never open."""
    home = _install(tmp_path, monkeypatch)
    view = _View(["skill_manage"])          # stays partial through the gate call
    mgr = _load(monkeypatch, view)
    assert mgr._plugins[PLUGIN_KEY].enabled is True
    d = _gate(mgr, "message_agent", {"target": "orch", "message": "ping"})
    assert isinstance(d, dict) and d.get("action") == "block"


def test_complete_registry_missing_tool_still_fail_loads(tmp_path, monkeypatch):
    """A genuine removal from a COMPLETE registry still fail-loads (AC-GOV-F22-1 preserved)."""
    home = _install(tmp_path, monkeypatch)
    # first load (empty view -> partial -> loads) only reads the plugin's own
    # restricted-core group table, so the "complete" set is never hard-coded here
    full = _canonical_full(_load(monkeypatch, _View([])))
    mgr = _load(monkeypatch, _View([n for n in full if n != full[0]]))
    loaded = mgr._plugins[PLUGIN_KEY]
    assert loaded.enabled is False
    assert loaded.error is not None
