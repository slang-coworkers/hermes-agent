"""Conformance tests for the per-edge human-approval mechanism verified by A2A-F19.

Loads the merged ``nv-fleet-gates`` covering surface (LOOP-F37, present in the
baseline ``release/v2026.8.31-e2e-fixed``) through the real
``PluginManager().discover_and_load()`` path, wires a gated edge, and drives the
plugin's real gated-edge directive through Hermes's native human-approval gate.
Passes on the baseline (which carries the plugin) and fails on the pristine
pinned release where the plugin is absent (the ``pytest.fail`` in ``_load``).

Ships as tests/plugins/test_a2a_f19_acceptance.py.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import pytest
import yaml

import hermes_cli.plugins as plugins_mod
from hermes_cli.plugins import PluginManager
from tools import approval

PLUGIN_KEY = "nv-fleet-gates"
_REPO = Path(__file__).resolve().parents[2]
PLUGIN_SRC = _REPO / "plugins" / PLUGIN_KEY
STAGE_MARKERS = ["[Fix Report]", "[Review Verdict]", "[Resolution]", "[handoff]"]

SESSION = "a2a-f19-sess"
PAT_ORCH = "plugin_rule:wire:worker-a:orch"
PAT_WB = "plugin_rule:wire:worker-a:worker-b"

UNATTENDED_MODES = [
    ("_is_single_query_approval_context", "_get_single_query_approval_mode"),
    ("_is_cron_approval_context", "_get_cron_approval_mode"),
    ("_is_unattended_platform_approval_context", "_get_unattended_approval_mode"),
]


def _base_settings(hermes_home, edges_db, **overrides):
    settings = {
        "role": "worker",
        "profile_roles": {"orch": "orchestrator", "worker-a": "worker",
                          "worker-b": "worker", "worker-c": "worker"},
        "enforce_sandbox": False,
        "expected_backend": "docker",
        "required_stages": ["OUTPUT_REVIEW"],
        "plan_gate": True,
        "edges_db_path": str(edges_db),
        "stage_markers": STAGE_MARKERS,
        "admin_tools": ["onboard_coworker", "onboard_project", "create_agent",
                        "record_decision", "record_human_verdict"],
        "skills_root": str(hermes_home / "skills"),
        "shared_learnings_path": str(hermes_home / "shared-learnings"),
    }
    settings.update(overrides)
    return settings


def _prime_registry(monkeypatch, hermes_home, bundled):
    monkeypatch.setenv("HOME", str(hermes_home.parent.parent / "fakehome"))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.delenv("HERMES_ENABLE_PROJECT_PLUGINS", raising=False)
    # The plugin's load-time restricted-set assertion reads get_all_tool_names().
    import model_tools
    model_tools.discover_builtin_tools()


def _load(tmp_path, monkeypatch, *, profile="worker-a", role=None, edges_db=None):
    hermes_home = tmp_path / "home" / profile
    (hermes_home / "plugins").mkdir(parents=True)
    bundled = tmp_path / "empty-bundled"
    bundled.mkdir(exist_ok=True)
    edges_db = edges_db or (tmp_path / "fleet" / "edges.db")
    edges_db.parent.mkdir(parents=True, exist_ok=True)

    if not PLUGIN_SRC.exists():
        pytest.fail(f"plugin source missing: {PLUGIN_SRC}")   # pristine pinned tree: no covering surface
    shutil.copytree(PLUGIN_SRC, hermes_home / "plugins" / PLUGIN_KEY)

    settings = _base_settings(hermes_home, edges_db)
    settings["profile"] = profile
    if role is not None:
        settings["role"] = role
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
    assert "pre_tool_call" in loaded.hooks_registered
    return loaded


def _gate(manager, tool_name, args, session_id="sess-1"):
    results = manager.invoke_hook(
        "pre_tool_call", tool_name=tool_name, args=args, session_id=session_id,
        task_id="task-1", tool_call_id="tc-1", turn_id="turn-1")
    directives = [r for r in (results or []) if isinstance(r, dict) and r.get("action")]
    return directives[0] if directives else None


def _act(d):
    return d.get("action") if isinstance(d, dict) else None


def _wire(manager, *argv):
    entry = manager._cli_commands["wire"]
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="cmd")
    entry["setup_fn"](sub.add_parser("wire"))
    ns = parser.parse_args(["wire", *argv])
    handler = entry.get("handler_fn") or getattr(ns, "func", None)
    return handler(ns)


@pytest.fixture
def native_gate(monkeypatch):
    """Deterministic native approval gate: clean session, not-yolo, interactive, manual mode."""
    monkeypatch.setattr(approval, "get_current_session_key", lambda default="default": SESSION)
    monkeypatch.setattr(approval, "is_current_session_yolo_enabled", lambda: False)
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False, raising=False)
    monkeypatch.setattr(approval, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr("tools.terminal_tool._get_approval_callback", lambda: None, raising=False)
    monkeypatch.setattr(approval, "_is_interactive_cli", lambda: True)
    monkeypatch.setattr(approval, "_is_gateway_approval_context", lambda: False)
    monkeypatch.setattr(approval, "_is_single_query_approval_context", lambda: False)
    monkeypatch.setattr(approval, "_is_cron_approval_context", lambda: False)
    for name in ("_session_approved", "_permanent_approved"):
        obj = getattr(approval, name, None)
        if hasattr(obj, "clear"):
            obj.clear()
    yield
    for name in ("_session_approved", "_permanent_approved"):
        obj = getattr(approval, name, None)
        if hasattr(obj, "clear"):
            obj.clear()


def test_ac_a2a_f19_4(tmp_path, monkeypatch, native_gate):
    """AC-A2A-F19-4: The gated-edge `rule_key` is stable per edge and distinct across edges, so the native `pattern_key = plugin_rule:wire:<from>:<to>` grant is **edge-scoped** (granting edge A lets A pass while edge B stays denied), and `resolve_pre_tool_block` forwards the plugin's `rule_key` into the gate end-to-end."""
    manager, _, _ = _load(tmp_path, monkeypatch)
    _loaded(manager)
    _wire(manager, "add", "worker-a", "orch", "--gated")
    _wire(manager, "add", "worker-a", "worker-b", "--gated")

    d1 = _gate(manager, "message_agent", {"target": "orch", "message": "hi"})
    d2 = _gate(manager, "message_agent", {"target": "orch", "message": "hi"})
    dwb = _gate(manager, "message_agent", {"target": "worker-b", "message": "hi"})
    assert d1.get("rule_key") == "wire:worker-a:orch" == d2.get("rule_key")
    assert dwb.get("rule_key") == "wire:worker-a:worker-b"
    assert d1.get("rule_key") != dwb.get("rule_key")

    monkeypatch.setattr(approval, "prompt_dangerous_approval", lambda *a, **k: "deny")
    assert approval.request_tool_approval(
        "message_agent", "gated", rule_key="wire:worker-a:orch")["pattern_key"] == PAT_ORCH
    # Grant only edge orch; the prompt still denies, so orch passes only if its grant is honored.
    approval.approve_session(SESSION, PAT_ORCH)
    assert approval.request_tool_approval(
        "message_agent", "gated", rule_key="wire:worker-a:orch")["approved"] is True
    assert approval.request_tool_approval(
        "message_agent", "gated", rule_key="wire:worker-a:worker-b")["approved"] is False

    # End-to-end glue: resolve_pre_tool_block forwards the plugin's rule_key into the gate.
    # A core regression substituting the tool name for the rule_key would flip this.
    captured = {}

    def _capture(tool_name, reason, *, rule_key="", approval_callback=None):
        captured["rule_key"] = rule_key
        return {"approved": True, "message": None}

    monkeypatch.setattr(approval, "request_tool_approval", _capture)
    monkeypatch.setattr(plugins_mod, "_plugin_manager", manager, raising=False)
    assert plugins_mod.resolve_pre_tool_block(
        "message_agent", {"target": "orch", "message": "hi"}, session_id=SESSION) is None
    assert captured["rule_key"] == "wire:worker-a:orch"


def test_ac_a2a_f19_5(tmp_path, monkeypatch, native_gate):
    """AC-A2A-F19-5: A denied, timed-out, or gate-error hold **fails closed** (the send is blocked); the same gated edge under an approving decision **releases** the send (no block)."""
    manager, _, _ = _load(tmp_path, monkeypatch)
    _loaded(manager)
    _wire(manager, "add", "worker-a", "orch", "--gated")
    monkeypatch.setattr(plugins_mod, "_plugin_manager", manager, raising=False)
    args = {"target": "orch", "message": "hi"}
    real_rta = approval.request_tool_approval

    monkeypatch.setattr(approval, "prompt_dangerous_approval", lambda *a, **k: "deny")
    denied = plugins_mod.resolve_pre_tool_block("message_agent", args, session_id=SESSION)
    assert denied is not None and "denied" in denied.lower()

    monkeypatch.setattr(approval, "prompt_dangerous_approval", lambda *a, **k: "timeout")
    timed = plugins_mod.resolve_pre_tool_block("message_agent", args, session_id=SESSION)
    assert timed is not None and "silence is not consent" in timed.lower()

    def _boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(approval, "request_tool_approval", _boom)
    errored = plugins_mod.resolve_pre_tool_block("message_agent", args, session_id=SESSION)
    assert errored is not None and "gate failed" in errored.lower()

    monkeypatch.setattr(approval, "request_tool_approval", real_rta)
    monkeypatch.setattr(approval, "prompt_dangerous_approval", lambda *a, **k: "once")
    released = plugins_mod.resolve_pre_tool_block("message_agent", args, session_id=SESSION)
    assert released is None


@pytest.mark.parametrize("ctx_pred, mode_getter", UNATTENDED_MODES)
def test_ac_a2a_f19_6(tmp_path, monkeypatch, native_gate, ctx_pred, mode_getter):
    """AC-A2A-F19-6: (honest limit) A gated-edge hold from an **unattended** session (`single_query_mode` / `cron_mode` / `unattended_mode`, no human) is **denied, not held** under the default `deny` modes; configured as `approve` the same hold auto-approves without a human."""
    manager, _, _ = _load(tmp_path, monkeypatch)
    _loaded(manager)
    _wire(manager, "add", "worker-a", "orch", "--gated")
    monkeypatch.setattr(plugins_mod, "_plugin_manager", manager, raising=False)
    monkeypatch.setattr(approval, "_is_interactive_cli", lambda: False)
    for pred, _g in UNATTENDED_MODES:
        monkeypatch.setattr(approval, pred, (lambda: True) if pred == ctx_pred else (lambda: False))
    args = {"target": "orch", "message": "hi"}

    denied = plugins_mod.resolve_pre_tool_block("message_agent", args, session_id=SESSION)
    assert denied is not None

    monkeypatch.setattr(approval, mode_getter, lambda: "approve")
    released = plugins_mod.resolve_pre_tool_block("message_agent", args, session_id=SESSION)
    assert released is None
