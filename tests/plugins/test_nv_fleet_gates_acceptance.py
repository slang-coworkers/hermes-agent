"""Acceptance test for the LOOP-F37 plugin ``nv-fleet-gates``.

Ships as ``tests/plugins/test_nv_fleet_gates_acceptance.py``. Mirrors
``tests/hermes_cli/test_plugin_api_compat.py``: it loads the plugin from an
isolated ``HERMES_HOME`` with an EMPTY bundled-plugin dir through the real
``PluginManager().discover_and_load()`` path, then drives the one
``pre_tool_call`` gate and the registered ``codex_critique`` tool / ``wire`` CLI
/ ``post_tool_call`` / ``pre_gateway_dispatch`` seams and the native approval
resolver, asserting the OUTCOME of each acceptance criterion through production
seams with realistic payloads.

One ``test_ac_<req_id>_<n>`` per ``pytest:`` acceptance-criterion id, in ADR
order. The ``desktop:`` (AC-A2A-F19-3) and ``live:`` (AC-CH-F51-1) ids are proven
by the spec / scenario file and have no function here.

MUST fail on the stock v2026.8.31 tree (plugin dir absent -> the fixture's
``pytest.fail``) and pass once ``plugins/nv-fleet-gates/`` exists. Behaviour
contract only: no model lists, no version literals, no reading of source files,
no network, nothing under ``~/.hermes``.
"""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

import pytest
import yaml

from hermes_cli.plugins import PluginManager
from tools.registry import registry

PLUGIN_KEY = "nv-fleet-gates"
_REPO = Path(__file__).resolve().parents[2]
PLUGIN_SRC = _REPO / "plugins" / PLUGIN_KEY
COMPOSE_SRC = _REPO / "plugins" / "nv-coworker-compose"
STAGE_MARKERS = ["[Fix Report]", "[Review Verdict]", "[Resolution]", "[handoff]"]


# --------------------------------------------------------------------------- #
# Fixtures / helpers  (all drive production seams)
# --------------------------------------------------------------------------- #
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
    # importing tools.registry alone does NOT register the built-in tools; the
    # plugin's load-time restricted-set assertion reads get_all_tool_names().
    import model_tools
    model_tools.discover_builtin_tools()


def _load(tmp_path, monkeypatch, *, profile="worker-a", role=None, edges_db=None,
          enable=(PLUGIN_KEY,), **settings_overrides):
    hermes_home = tmp_path / "home" / profile
    (hermes_home / "plugins").mkdir(parents=True)
    bundled = tmp_path / "empty-bundled"
    bundled.mkdir(exist_ok=True)
    edges_db = edges_db or (tmp_path / "fleet" / "edges.db")
    edges_db.parent.mkdir(parents=True, exist_ok=True)

    if not PLUGIN_SRC.exists():
        pytest.fail(f"plugin source missing: {PLUGIN_SRC}")   # the deliberate stock-tree red
    shutil.copytree(PLUGIN_SRC, hermes_home / "plugins" / PLUGIN_KEY)
    if "nv-coworker-compose" in enable:
        shutil.copytree(COMPOSE_SRC, hermes_home / "plugins" / "nv-coworker-compose")

    settings = _base_settings(hermes_home, edges_db, **settings_overrides)
    settings["profile"] = profile
    if role is not None:
        settings["role"] = role
    cfg = {"plugins": {"enabled": list(enable),
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
    assert "codex_critique" in loaded.tools_registered
    return loaded


def _gate(manager, tool_name, args, session_id="sess-1", **kwargs):
    results = manager.invoke_hook(
        "pre_tool_call", tool_name=tool_name, args=args, session_id=session_id,
        task_id="task-1", tool_call_id="tc-1", turn_id="turn-1", **kwargs)
    directives = [r for r in (results or []) if isinstance(r, dict) and r.get("action")]
    return directives[0] if directives else None


def _act(d):
    return d.get("action") if isinstance(d, dict) else None


def _run_critique(monkeypatch, session_id, verdict="approve"):
    """Run the real codex_critique tool with a stubbed plugin-LLM lane."""
    import agent.plugin_llm as plugin_llm
    calls = {"n": 0}

    async def _fake(self, **kw):
        calls["n"] += 1
        return plugin_llm.PluginLlmStructuredResult(
            text="{}", provider="test", model="test", agent_id="test",
            parsed={"verdict": verdict, "stage": "OUTPUT_REVIEW"}, content_type="json")

    monkeypatch.setattr(plugin_llm.PluginLlm, "acomplete_structured", _fake, raising=False)
    registry.dispatch("codex_critique", {"stage": "OUTPUT_REVIEW"}, session_id=session_id)
    return calls


def _wire(manager, *argv):
    entry = manager._cli_commands["wire"]
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="cmd")
    entry["setup_fn"](sub.add_parser("wire"))
    ns = parser.parse_args(["wire", *argv])
    handler = entry.get("handler_fn") or getattr(ns, "func", None)
    return handler(ns)


def _gh_comment_event(repo, pr, body, sender_type):
    """A webhook MessageEvent stand-in carrying a GitHub issue_comment payload.

    The GitHub delivery lives in raw_message (metadata is empty for the webhook
    adapter); the builder aligns this to the production MessageEvent constructor.
    """
    class _E:
        platform = "webhook"
        metadata = {}
        raw_message = {
            "action": "created",
            "repository": {"full_name": repo},
            "issue": {"number": pr, "pull_request": {}},
            "comment": {"body": body, "user": {"type": sender_type}},
            "sender": {"type": sender_type},
        }
    return _E()


# --------------------------------------------------------------------------- #
# LOOP-F37 — critique gate, conformance, ordering, codex_critique
# --------------------------------------------------------------------------- #
def test_ac_loop_f37_1(tmp_path, monkeypatch):
    """A stage-marker message_agent or kanban terminating delivery is blocked without a critique, passes with it, and re-blocks after a later mutation."""
    manager, _, _ = _load(tmp_path, monkeypatch)
    _loaded(manager)
    _wire(manager, "add", "worker-a", "orch")            # wired, so only the critique gate is under test
    msg = {"target": "orch", "message": "[Fix Report] done"}
    kanban = {"task_id": "t1"}
    assert _act(_gate(manager, "message_agent", msg)) == "block"
    assert _act(_gate(manager, "kanban_request_review", kanban)) == "block"
    _run_critique(monkeypatch, "sess-1")
    assert _act(_gate(manager, "message_agent", msg)) != "block"
    assert _act(_gate(manager, "kanban_request_review", kanban)) != "block"
    manager.invoke_hook("post_tool_call", tool_name="write_file",
                        args={"path": "src/x.py", "content": "x=1"},
                        status="ok", result="ok", session_id="sess-1")
    assert _act(_gate(manager, "message_agent", msg)) == "block"   # freshness: mutation restales


def test_ac_loop_f37_2(tmp_path, monkeypatch):
    """terminal and execute_code gh pr create|review|comment are blocked without a critique; create/review pass after it, comment stays blocked."""
    manager, _, _ = _load(tmp_path, monkeypatch)
    _loaded(manager)
    cmds = {"create": "gh pr create --title x --body y",
            "review": "gh pr review 12 --approve",
            "comment": "gh pr comment 12 --body hi"}

    def mk(tool, cmd):
        return {"command": cmd} if tool == "terminal" else {"code": cmd}

    for tool in ("terminal", "execute_code"):
        for cmd in cmds.values():
            assert _act(_gate(manager, tool, mk(tool, cmd), session_id="s2")) == "block"
    _run_critique(monkeypatch, "s2")
    for tool in ("terminal", "execute_code"):
        assert _act(_gate(manager, tool, mk(tool, cmds["create"]), session_id="s2")) != "block"
        assert _act(_gate(manager, tool, mk(tool, cmds["review"]), session_id="s2")) != "block"
        assert _act(_gate(manager, tool, mk(tool, cmds["comment"]), session_id="s2")) == "block"


def test_ac_loop_f37_3(tmp_path, monkeypatch):
    """Across the loaded batch-2 fleet (compose + fleet-gates) exactly ONE pre_tool_call callback is registered."""
    if not COMPOSE_SRC.exists():
        pytest.fail(f"compose fixture missing: {COMPOSE_SRC}")
    manager, _, _ = _load(tmp_path, monkeypatch, enable=(PLUGIN_KEY, "nv-coworker-compose"))
    _loaded(manager)
    assert manager._plugins["nv-coworker-compose"].enabled is True
    assert len(manager.iter_hook_callbacks("pre_tool_call")) == 1


def test_ac_loop_f37_4(tmp_path, monkeypatch):
    """Block wins over a gated-edge approve: a gated marked delivery lacking its critique returns block."""
    manager, _, _ = _load(tmp_path, monkeypatch)
    _loaded(manager)
    _wire(manager, "add", "worker-a", "orch", "--gated")
    assert _act(_gate(manager, "message_agent",
                      {"target": "orch", "message": "[handoff] spec"})) == "block"


def test_ac_loop_f37_5(tmp_path, monkeypatch):
    """codex_critique commits a row for its OWN session that flips the gate; a different session is not unlocked; the slash alias is registered."""
    manager, _, _ = _load(tmp_path, monkeypatch)
    _loaded(manager)
    assert "codex-critique" in manager._plugin_commands
    _wire(manager, "add", "worker-a", "orch")
    msg = {"target": "orch", "message": "[Review Verdict] APPROVE"}
    assert _act(_gate(manager, "message_agent", msg, session_id="sA")) == "block"
    other = _run_critique(monkeypatch, "sB")
    assert other["n"] == 1
    assert _act(_gate(manager, "message_agent", msg, session_id="sA")) == "block"
    _run_critique(monkeypatch, "sA")
    assert _act(_gate(manager, "message_agent", msg, session_id="sA")) != "block"


def test_ac_loop_f37_6(tmp_path, monkeypatch):
    """gh pr comment is blocked without a human-invitation for (repo, pr); a bot/no-mention delivery does not authorize; a human mention does."""
    manager, _, _ = _load(tmp_path, monkeypatch)
    _loaded(manager)
    _run_critique(monkeypatch, "s6")                     # satisfy the critique gate to isolate the invitation predicate
    args = {"command": "gh pr comment 12 --repo slang-coworkers/hermes-agent --body hi"}
    assert _act(_gate(manager, "terminal", args, session_id="s6")) == "block"
    manager.invoke_hook("pre_gateway_dispatch",
                        event=_gh_comment_event("slang-coworkers/hermes-agent", 12,
                                                body="thanks", sender_type="Bot"))
    assert _act(_gate(manager, "terminal", args, session_id="s6")) == "block"
    manager.invoke_hook("pre_gateway_dispatch",
                        event=_gh_comment_event("slang-coworkers/hermes-agent", 12,
                                                body="@bot please post", sender_type="User"))
    assert _act(_gate(manager, "terminal", args, session_id="s6")) != "block"


def test_ac_loop_f37_7(tmp_path, monkeypatch):
    """A host-side writer (skill_manage or memory) whose resolved target escapes the caller's tree is blocked for a worker, exempt for the orchestrator, allowed in-tree."""
    worker, home_w, edges = _load(tmp_path, monkeypatch, profile="worker-a")
    _loaded(worker)
    outside = "../../shared-learnings/x/SKILL.md"

    def skill_op(action, file_path):
        return {"operations": [{"name": "x", "action": action,
                                "file_path": file_path, "file_content": "y"}]}

    for action in ("write_file", "remove_file"):        # the operations that carry file_path
        assert _act(_gate(worker, "skill_manage", skill_op(action, outside))) == "block"
    assert _act(_gate(worker, "skill_manage", skill_op("write_file", "SKILL.md"))) != "block"

    # The memory tool's target is a closed set {memory,user}; the escape is at the
    # RESOLVED store path, so point this worker's memories dir at the shared-learnings
    # clone. The schema-valid target still resolves outside the caller's tree -> block.
    mem_add = {"action": "add", "target": "memory", "content": "y"}
    (home_w / "shared-learnings").mkdir(parents=True, exist_ok=True)
    os.symlink(home_w / "shared-learnings", home_w / "memories")
    assert _act(_gate(worker, "memory", mem_add)) == "block"

    intree, home_it, _ = _load(tmp_path, monkeypatch, profile="worker-b", edges_db=edges)
    _loaded(intree)
    (home_it / "memories").mkdir(exist_ok=True)         # a real in-tree memories dir
    assert _act(_gate(intree, "memory", mem_add)) != "block"

    orch, home_o, _ = _load(tmp_path, monkeypatch, profile="orch", role="orchestrator", edges_db=edges)
    _loaded(orch)
    (home_o / "shared-learnings").mkdir(parents=True, exist_ok=True)
    os.symlink(home_o / "shared-learnings", home_o / "memories")
    assert _act(_gate(orch, "skill_manage", skill_op("write_file", outside))) != "block"
    assert _act(_gate(orch, "memory", mem_add)) != "block"   # orchestrator exempt even when the store path escapes


# --------------------------------------------------------------------------- #
# GOV-F22 — guard catalog folded into the veto
# --------------------------------------------------------------------------- #
def test_ac_gov_f22_1(tmp_path, monkeypatch):
    """A missing restricted core name OR an unclassified shell-capable registry tool fails the plugin load."""
    import model_tools
    model_tools.discover_builtin_tools()
    real = registry.get_all_tool_names

    monkeypatch.setattr(registry, "get_all_tool_names",
                        lambda *a, **k: [n for n in real() if n != "terminal"])
    m1, _, _ = _load(tmp_path, monkeypatch, profile="w1")
    assert m1._plugins[PLUGIN_KEY].enabled is False
    assert m1._plugins[PLUGIN_KEY].error is not None

    monkeypatch.setattr(registry, "get_all_tool_names",
                        lambda *a, **k: list(real()) + ["exec_shell_unclassified"])
    m2, _, _ = _load(tmp_path, monkeypatch, profile="w2")
    assert m2._plugins[PLUGIN_KEY].enabled is False
    assert m2._plugins[PLUGIN_KEY].error is not None


def test_ac_gov_f22_2(tmp_path, monkeypatch):
    """Incoming tool names are canonicalised through the vendored alias map before matching."""
    manager, _, _ = _load(tmp_path, monkeypatch)
    _loaded(manager)
    legacy = _act(_gate(manager, "cronjob", {"action": "add"}))
    canonical = _act(_gate(manager, "cronjob_manage", {"action": "add"}))
    assert legacy == canonical == "block"


def test_ac_gov_f22_3(tmp_path, monkeypatch):
    """A predicate that raises yields a block directive (fail-closed), never a passthrough."""
    manager, _, _ = _load(tmp_path, monkeypatch)
    loaded = _loaded(manager)
    monkeypatch.setattr(loaded.module, "_edges_lookup",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
                        raising=False)
    assert _act(_gate(manager, "message_agent", {"target": "orch", "message": "hi"})) == "block"


# --------------------------------------------------------------------------- #
# GOV-F26 — cli_scope / fleet-admin
# --------------------------------------------------------------------------- #
def test_ac_gov_f26_1(tmp_path, monkeypatch):
    """Admin-shaped tools are blocked by name for a worker and pass for the orchestrator."""
    worker, _, edges = _load(tmp_path, monkeypatch, profile="worker-a")
    _loaded(worker)
    assert _act(_gate(worker, "onboard_coworker", {})) == "block"

    orch, _, _ = _load(tmp_path, monkeypatch, profile="orch", role="orchestrator", edges_db=edges)
    _loaded(orch)
    assert _act(_gate(orch, "onboard_coworker", {})) != "block"


def test_ac_gov_f26_2(tmp_path, monkeypatch):
    """The profile->role map is read from managed scope and a per-profile role override cannot widen it."""
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text(yaml.safe_dump({"plugins": {"entries": {PLUGIN_KEY: {
        "settings": {"profile_roles": {"worker-a": "worker"}}}}}}), encoding="utf-8")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    manager, _, _ = _load(tmp_path, monkeypatch, profile="worker-a", role="orchestrator")
    _loaded(manager)
    assert _act(_gate(manager, "onboard_coworker", {})) == "block"


# --------------------------------------------------------------------------- #
# LOOP-F38 — plan gate
# --------------------------------------------------------------------------- #
def test_ac_loop_f38_1(tmp_path, monkeypatch):
    """Mutations are refused until the SAME session created a plan under .hermes/plans/*.md; another session is not unlocked; toggle off = no gate."""
    manager, _, edges = _load(tmp_path, monkeypatch)
    _loaded(manager)
    wf = {"path": "src/x.py", "content": "x = 1"}
    plan = {"path": ".hermes/plans/2026-09-11_plan.md", "content": "# plan"}

    assert _act(_gate(manager, "write_file", wf)) == "block"
    assert _act(_gate(manager, "patch", {"path": "src/x.py", "patch": "@@"})) == "block"
    assert _act(_gate(manager, "terminal", {"command": "sed -i s/a/b/ src/x.py"})) == "block"
    assert _act(_gate(manager, "write_file", plan)) != "block"   # /plan must write its own plan
    manager.invoke_hook("post_tool_call", tool_name="write_file", args=plan,
                        status="ok", result="ok", session_id="sess-1")
    assert _act(_gate(manager, "write_file", wf)) != "block"
    assert _act(_gate(manager, "write_file", wf, session_id="other")) == "block"

    off, _, _ = _load(tmp_path, monkeypatch, profile="worker-c", plan_gate=False, edges_db=edges)
    _loaded(off)
    assert _act(_gate(off, "write_file", wf)) != "block"


# --------------------------------------------------------------------------- #
# A2A-F18 — wired-only messaging
# --------------------------------------------------------------------------- #
def test_ac_a2a_f18_1(tmp_path, monkeypatch):
    """Unwired targets are blocked; ONE wire add wires both directions (idempotent); ONE remove clears both; delegate stays ungated."""
    manager, _, edges = _load(tmp_path, monkeypatch, profile="worker-a")
    _loaded(manager)
    msg = {"target": "orch", "message": "hi"}
    assert _act(_gate(manager, "message_agent", msg)) == "block"
    assert _act(_gate(manager, "kanban_create", {"title": "t", "assignee": "orch"})) == "block"

    _wire(manager, "add", "worker-a", "orch")
    _wire(manager, "add", "worker-a", "orch")            # idempotent: no error, no duplicate row
    assert "worker-a" in str(_wire(manager, "list"))
    assert _act(_gate(manager, "message_agent", msg)) != "block"

    orch, _, _ = _load(tmp_path, monkeypatch, profile="orch", role="orchestrator", edges_db=edges)
    _loaded(orch)
    rev = {"target": "worker-a", "message": "hi"}
    assert _act(_gate(orch, "message_agent", rev)) != "block"   # the one add wired orch->worker-a too

    _wire(manager, "remove", "worker-a", "orch")
    assert _act(_gate(manager, "message_agent", msg)) == "block"
    assert _act(_gate(orch, "message_agent", rev)) == "block"    # the one remove cleared both directions


def test_ac_a2a_f18_2(tmp_path, monkeypatch):
    """delegate_task is NOT gated by the wiring predicate (crosses no edge)."""
    manager, _, _ = _load(tmp_path, monkeypatch)
    _loaded(manager)
    assert _act(_gate(manager, "delegate_task",
                      {"tasks": [{"goal": "g"}], "profile": "orch"})) != "block"


# --------------------------------------------------------------------------- #
# A2A-F19 — gated-edge approval
# --------------------------------------------------------------------------- #
def test_ac_a2a_f19_1(tmp_path, monkeypatch):
    """A gated edge yields action:approve with a stable rule_key wire:<from>:<to>; an ungated wired edge does not approve."""
    manager, _, _ = _load(tmp_path, monkeypatch)
    _loaded(manager)
    _wire(manager, "add", "worker-a", "orch", "--gated")
    msg = {"target": "orch", "message": "hi"}
    d1 = _gate(manager, "message_agent", msg)
    d2 = _gate(manager, "message_agent", msg)
    assert _act(d1) == "approve"
    assert d1.get("rule_key") == "wire:worker-a:orch" == d2.get("rule_key")

    m2, _, _ = _load(tmp_path, monkeypatch, profile="worker-b")
    _loaded(m2)
    _wire(m2, "add", "worker-b", "orch")                 # ungated
    assert _act(_gate(m2, "message_agent", {"target": "orch", "message": "hi"})) != "approve"


def test_ac_a2a_f19_2(tmp_path, monkeypatch):
    """The gated-edge approve directive resolves to a DENY under an unattended (single_query) session; the plugin never branches on mode."""
    manager, _, _ = _load(tmp_path, monkeypatch)
    _loaded(manager)
    _wire(manager, "add", "worker-a", "orch", "--gated")
    assert _act(_gate(manager, "message_agent", {"target": "orch", "message": "hi"})) == "approve"

    import hermes_cli.plugins as plugins_mod
    from tools import approval
    monkeypatch.setattr(plugins_mod, "_plugin_manager", manager, raising=False)
    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "1")   # establish the single-query context
    monkeypatch.setattr(approval, "_get_single_query_approval_mode", lambda: "deny", raising=False)
    resolved = plugins_mod.resolve_pre_tool_block(
        "message_agent", {"target": "orch", "message": "hi"}, session_id="sess-1")
    assert resolved is not None
    assert "single-query" in str(resolved).lower()   # denied specifically by the single-query resolver


# --------------------------------------------------------------------------- #
# ISO-F10 — per-session sandbox predicate (inert until P4)
# --------------------------------------------------------------------------- #
def test_ac_iso_f10_1(tmp_path, monkeypatch):
    """With enforce_sandbox false the sandbox predicate is inert: tool calls pass regardless of backend."""
    monkeypatch.setenv("TERMINAL_ENV", "local")
    manager, _, _ = _load(tmp_path, monkeypatch)
    _loaded(manager)
    _run_critique(monkeypatch, "sess-1")
    assert _act(_gate(manager, "terminal", {"command": "ls"})) != "block"


def test_ac_iso_f10_2(tmp_path, monkeypatch):
    """With enforce_sandbox true: a mismatched backend blocks; on the matching backend stdio mcp_* and dangerous terminal are refused, a benign call passes, and ensure_task_env is consulted."""
    import tools.terminal_tool as tt
    calls = {"env": 0}

    def _ready_env(*a, **k):
        calls["env"] += 1
        return object()                                  # non-None: sandbox env is ready

    monkeypatch.setattr(tt, "ensure_task_env", _ready_env, raising=False)
    # plan_gate off so a mutating terminal reaches the sandbox predicate, not the plan gate
    manager, _, _ = _load(tmp_path, monkeypatch, enforce_sandbox=True, plan_gate=False)
    _loaded(manager)
    _run_critique(monkeypatch, "sess-1")

    monkeypatch.setenv("TERMINAL_ENV", "local")
    assert _act(_gate(manager, "terminal", {"command": "ls"})) == "block"

    monkeypatch.setenv("TERMINAL_ENV", "docker")
    assert _act(_gate(manager, "mcp_some_stdio_tool", {})) == "block"
    assert _act(_gate(manager, "terminal", {"command": "rm -rf /"})) == "block"
    assert _act(_gate(manager, "terminal", {"command": "ls"})) != "block"
    assert calls["env"] >= 1
