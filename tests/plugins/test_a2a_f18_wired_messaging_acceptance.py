"""Hermetic acceptance coverage for A2A-F18 wired-only agent messaging.

Covers AC-A2A-F18-3 (a gated wiring edge escalates a message_agent send to the human
approval gate) and AC-A2A-F18-4 (the runbook documents the NanoClaw->Hermes mapping).
AC-A2A-F18-1 (unwired-edge block + bidirectional idempotent wiring) and AC-A2A-F18-2
(delegate_task is not gated) are proven by the pre-existing
tests/plugins/test_nv_fleet_gates_acceptance.py::test_ac_a2a_f18_1/2 and are not
redefined here.
"""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path

import pytest
import yaml

from hermes_cli.plugins import PluginManager

_REPO = Path(__file__).resolve().parents[2]
PLUGIN_KEY = "nv-fleet-gates"
PLUGIN_SRC = _REPO / "plugins" / PLUGIN_KEY
DOC = _REPO / "website" / "docs" / "developer-guide" / "plugins" / "nv-fleet-gates.md"


def _settings(hermes_home, edges_db):
    return {
        "edges_db_path": str(edges_db),
        "enforce_sandbox": False,
        "plan_gate": False,
        "skills_root": str(hermes_home / "skills"),
        "shared_learnings_path": str(hermes_home / "shared-learnings"),
    }


def _prime_registry(monkeypatch, hermes_home, bundled):
    monkeypatch.setenv("HOME", str(hermes_home.parent.parent / "fakehome"))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.delenv("HERMES_ENABLE_PROJECT_PLUGINS", raising=False)
    # The plugin's load-time restricted-set assertion reads the running registry;
    # importing tools.registry alone does not register the built-in tools.
    import model_tools

    model_tools.discover_builtin_tools()


def _load(tmp_path, monkeypatch, *, profile):
    hermes_home = tmp_path / "home" / profile
    (hermes_home / "plugins").mkdir(parents=True)
    bundled = tmp_path / "bundled_empty"
    bundled.mkdir()
    edges_db = tmp_path / "fleet" / "edges.db"
    edges_db.parent.mkdir(parents=True, exist_ok=True)
    if not PLUGIN_SRC.exists():
        pytest.fail(f"covering-surface plugin missing (stock-tree red): {PLUGIN_SRC}")
    shutil.copytree(PLUGIN_SRC, hermes_home / "plugins" / PLUGIN_KEY)
    cfg = {
        "plugins": {
            "enabled": [PLUGIN_KEY],
            "entries": {PLUGIN_KEY: {"settings": _settings(hermes_home, edges_db)}},
        }
    }
    (hermes_home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    _prime_registry(monkeypatch, hermes_home, bundled)
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled is True
    assert loaded.error is None
    assert loaded.module is not None
    assert "pre_tool_call" in loaded.hooks_registered
    return manager


def _gate(manager, tool_name, args, session_id="sess-1"):
    results = manager.invoke_hook(
        "pre_tool_call", tool_name=tool_name, args=args, session_id=session_id,
        task_id="task-1", tool_call_id="tc-1", turn_id="turn-1")
    directives = [r for r in (results or []) if isinstance(r, dict) and r.get("action")]
    return directives[0] if directives else None


def _wire(manager, *argv):
    entry = manager._cli_commands["wire"]
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="cmd")
    entry["setup_fn"](sub.add_parser("wire"))
    ns = parser.parse_args(["wire", *argv])
    handler = entry.get("handler_fn") or getattr(ns, "func", None)
    return handler(ns)


def test_ac_a2a_f18_3(tmp_path, monkeypatch):
    """A gated wiring edge escalates a message_agent send to the human approval gate with rule_key wire:<from>:<to>; an ungated wired edge does not escalate."""
    manager = _load(tmp_path, monkeypatch, profile="bot-a")
    _wire(manager, "add", "bot-a", "bot-b")
    _wire(manager, "add", "bot-a", "bot-c", "--gated")
    gated = _gate(manager, "message_agent", {"target": "bot-c", "message": "hi"})
    assert gated is not None and gated.get("action") == "approve"
    assert gated.get("rule_key") == "wire:bot-a:bot-c"
    ungated = _gate(manager, "message_agent", {"target": "bot-b", "message": "hi"})
    assert ungated is None


def test_ac_a2a_f18_4(tmp_path, monkeypatch):
    """The runbook section documents the NanoClaw routing-map+ACL mapping onto the nv-fleet-gates covering surface, in the tag:/main: citation form, naming the wire verbs and the gated approval."""
    assert DOC.exists(), f"covering-surface doc missing: {DOC}"
    text = DOC.read_text(encoding="utf-8")
    marker = "## Wired-only agent messaging (A2A-F18)"
    assert marker in text, "A2A-F18 runbook section missing from nv-fleet-gates.md"
    rest = text[text.index(marker) + len(marker):]
    nxt = rest.find("\n## ")
    section = rest if nxt == -1 else rest[:nxt]
    low = section.lower()
    for claim in (
        "nanoclaw",
        "routing map",
        "hermes wire add",
        "hermes wire remove",
        "hermes wire list",
        "human approval",
        "message_agent_tool",
        "edges_db_path",
        "gated",
    ):
        assert claim in low, f"A2A-F18 runbook does not document: {claim!r}"
    assert ("acl" in low) or ("access-control list" in low)
    assert ("not wired" in low) or ("unwired" in low)
    assert "wire:" in section
    # AC-4 requires at least one of A2A-F18's own gap-matrix evidence anchors, in tag:/main:
    # form, so a generic citation of an unrelated file cannot satisfy the doc-contract.
    assert re.search(
        r"tag:\s*`tools/bot_mode_dm\.py:241`[^\n]*\|\s*main:\s*`tools/bot_mode_dm\.py:174`"
        r"|tag:\s*`tools/bot_mode_dm\.py:187`[^\n]*\|\s*main:\s*`tools/bot_mode_probe\.py:71`",
        section,
    ), "A2A-F18 runbook lacks a tag:/main: dual citation of an A2A-F18 evidence anchor"
