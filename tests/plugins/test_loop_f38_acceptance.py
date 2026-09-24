"""Acceptance test for LOOP-F38 — Plan gate, intent router, workflow-state reset, buddy monitor.

ADOPT-verify row (ex-MERGE->LOOP-F37): no plugin, no core edit. Each test exercises
a native Hermes surface through its real public import against a sandboxed HERMES_HOME
and asserts a differential a one-line regression would flip.

Target on the fork: tests/plugins/test_loop_f38_acceptance.py
Run with: scripts/run_tests.sh tests/plugins/test_loop_f38_acceptance.py

AC id -> node id (AC-LOOP-F38-<n> lowercased, '-' -> '_'):
  1..9 -> test_ac_loop_f38_1 .. test_ac_loop_f38_9

test_ac_loop_f38_9 is the doc negative control: it fails on the stock tree (the doc
page is absent) and passes once the page is committed. The other eight pass on stock.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# tests/plugins/test_loop_f38_acceptance.py -> parents[2] == repo root (CWD-independent).
_REPO = Path(__file__).resolve().parents[2]
DOC = _REPO / "website" / "docs" / "user-guide" / "slang-coworkers-loop-controls.md"

# (symbol on the cite row, exact tag: anchor, exact main: anchor). tag anchors are verified
# at the pinned tree; main anchors are advisory anchors at the evidence-resolution commit 08b140d14.
_DUAL_CITES = [
    ("build_plan_prompt", "agent/plan_prompt.py:78", "agent/plan_prompt.py:20"),
    ("mark_workspace_edited", "agent/verification_evidence.py:677", "agent/verification_evidence.py:27"),
    ("build_verify_on_stop_nudge", "agent/verification_stop.py:233", "agent/verification_stop.py:1"),
    ("on_session_reset", "website/docs/user-guide/features/hooks.md:461", "website/docs/user-guide/features/hooks.md:461"),
    ("on_session_start", "website/docs/user-guide/features/hooks.md:458", "website/docs/user-guide/features/hooks.md:458"),
    ("is_first_turn", "website/docs/user-guide/features/hooks.md:446", "website/docs/user-guide/features/hooks.md:446"),
    ("format_for_injection", "tools/todo_tool.py:151", "tools/todo_tool.py:1"),
    ("TODO_INJECTION_HEADER", "tools/todo_tool.py:42", "tools/todo_tool.py:21"),
    ("complete_structured", "agent/plugin_llm.py:811", "agent/plugin_llm.py:460"),
    ("snapshot_recent_messages", "agent/review_engine.py:63", "agent/review_engine.py:1"),
    ("is_background_review_enabled", "agent/background_review.py:291", "agent/background_review.py:1"),
    ("auxiliary.background_review.enabled", "hermes_cli/config_defaults.py:1356", "hermes_cli/config_defaults.py:741"),
    ("decompose_task", "hermes_cli/kanban_decompose.py:271", "hermes_cli/kanban_decompose.py:298"),
]


def test_ac_loop_f38_1() -> None:
    """Plan-mode prompt: /plan instructs the model to write only the plan under .hermes/plans/."""
    from agent.plan_prompt import build_plan_prompt

    prompt = build_plan_prompt("route the LOOP-F38 work")
    assert ".hermes/plans/" in prompt
    assert "Do not edit project files except the plan markdown file itself." in prompt
    assert "Do not implement code" in prompt


def test_ac_loop_f38_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Edit-tracking + verify-on-stop: an unverified edit yields a bounded nudge naming it."""
    from agent.verification_evidence import mark_workspace_edited
    from agent.verification_stop import build_verify_on_stop_nudge

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    project = tmp_path / "proj"
    project.mkdir()
    # A real on-disk project the verifier recognises: its scripts define the checks.
    (project / "package.json").write_text(
        '{"scripts": {"test": "vitest", "lint": "eslint ."}}', encoding="utf-8")
    (project / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    changed = str(project / "src" / "app.ts")

    mark_workspace_edited(session_id="s1", cwd=project, paths=[changed])
    nudge = build_verify_on_stop_nudge(session_id="s1", changed_paths=[changed])
    assert nudge is not None
    assert "fresh passing verification evidence" in nudge
    assert changed in nudge

    assert build_verify_on_stop_nudge(
        session_id="s1", changed_paths=[changed], attempts=2, max_attempts=2) is None
    assert build_verify_on_stop_nudge(session_id="s2", changed_paths=[]) is None


def test_ac_loop_f38_3() -> None:
    """Workflow-state reset: new_session performs the core reset and fires on_session_reset."""
    from cli import HermesCLI
    from hermes_cli.plugins import VALID_HOOKS
    from tools.todo_tool import TodoStore

    assert {"on_session_reset", "on_session_start"} <= set(VALID_HOOKS)

    with patch("hermes_cli.lifecycle.finalize_session"), \
            patch("hermes_cli.lifecycle.invoke_hook") as mock_invoke_hook:
        cli = HermesCLI()
        cli.agent = MagicMock()
        cli.agent.session_id = "loop-f38-session"
        cli.new_session(silent=True)
        # The core reset: new_session zeroes session state and swaps in a fresh TodoStore.
        cli.agent.reset_session_state.assert_called_once()
        assert isinstance(cli.agent._todo_store, TodoStore)
        # The on_session_reset hook fires around that boundary as a plugin signal.
        assert any(
            c.args == ("on_session_reset",)
            and c.kwargs.get("session_id") == cli.session_id
            and c.kwargs.get("platform") == "cli"
            for c in mock_invoke_hook.call_args_list
        )


def test_ac_loop_f38_4(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Per-turn re-entry: pre_llm_call delivers is_first_turn (True then False) to a plugin."""
    import yaml

    from hermes_cli.plugins import PluginManager

    home = tmp_path / "hermes_test"
    pdir = home / "plugins" / "loop_f38_probe"
    pdir.mkdir(parents=True)
    (pdir / "plugin.yaml").write_text(
        yaml.safe_dump({"name": "loop_f38_probe", "version": "0.1.0",
                        "description": "LOOP-F38 is_first_turn probe"}), encoding="utf-8")
    (pdir / "__init__.py").write_text(
        "def register(ctx):\n"
        "    ctx.register_hook('pre_llm_call', lambda **kw: {'context': f\"first={kw['is_first_turn']}\"})\n",
        encoding="utf-8")
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["loop_f38_probe"]}}), encoding="utf-8")
    bundled = tmp_path / "empty_bundled"
    bundled.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")

    mgr = PluginManager()
    mgr.discover_and_load()

    first = mgr.invoke_hook("pre_llm_call", session_id="s1", user_message="hi",
                            conversation_history=[], is_first_turn=True, model="test")
    later = mgr.invoke_hook("pre_llm_call", session_id="s1", user_message="again",
                            conversation_history=[], is_first_turn=False, model="test")
    assert first[0]["context"] == "first=True"
    assert later[0]["context"] == "first=False"


def test_ac_loop_f38_5() -> None:
    """Compaction-surviving checklist: format_for_injection re-emits active todos with the header."""
    from tools.todo_tool import TodoStore

    store = TodoStore()
    store.write([
        {"id": "1", "content": "loop-f38-done-task", "status": "completed"},
        {"id": "2", "content": "loop-f38-sentinel-task", "status": "pending"},
        {"id": "3", "content": "loop-f38-active-task", "status": "in_progress"},
    ])
    text = store.format_for_injection()
    assert text is not None
    assert "loop-f38-sentinel-task" in text
    assert "loop-f38-active-task" in text
    assert "context compression" in text.lower()
    assert "loop-f38-done-task" not in text
    assert "loop-f38-absent-task" not in text


def test_ac_loop_f38_6(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Intent routing (shipped): kanban decompose routes a child to the aux-named profile; invalid falls back."""
    import json as jsonlib
    from types import SimpleNamespace

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_decompose as decomp

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    def _aux(content: str):
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.content = content
        return resp

    def _roster(names):
        # The decomposer builds its roster + valid-set from these profile lookups.
        profiles = [SimpleNamespace(name=n, is_default=(i == 0), description=f"desc {n}",
                                    description_auto=False, model="m", provider="p", skill_count=1)
                    for i, n in enumerate(names)]
        return [
            patch("hermes_cli.profiles.list_profiles", return_value=profiles),
            patch("hermes_cli.profiles.profile_exists", side_effect=lambda x: x in names),
            patch("hermes_cli.profiles.get_active_profile_name", return_value=names[0]),
        ]

    # Valid assignees: each decomposed child is routed to the specialist the aux model named.
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="ship a feature", triage=True)
    payload = jsonlib.dumps({"fanout": True, "rationale": "split", "tasks": [
        {"title": "research", "body": "look it up", "assignee": "researcher", "parents": []},
        {"title": "build", "body": "code it", "assignee": "engineer", "parents": [0]}]})
    started = _roster(["orchestrator", "researcher", "engineer"])
    for p in started:
        p.start()
    try:
        with patch("agent.auxiliary_client.call_llm", return_value=_aux(payload)), \
                patch("agent.auxiliary_client.get_auxiliary_extra_body", return_value={}):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in started:
            p.stop()
    assert outcome.ok, outcome.reason
    assert outcome.child_ids and len(outcome.child_ids) == 2
    with kb.connect() as conn:
        assert kb.get_task(conn, outcome.child_ids[0]).assignee == "researcher"
        assert kb.get_task(conn, outcome.child_ids[1]).assignee == "engineer"

    # Invalid assignee: routing falls back to the configured default.
    with kb.connect() as conn:
        tid2 = kb.create_task(conn, title="route me", triage=True)
    payload2 = jsonlib.dumps({"fanout": False, "rationale": "single",
                              "title": "T", "body": "b", "assignee": "made_up"})
    started2 = _roster(["orchestrator", "fallback"])
    for p in started2:
        p.start()
    try:
        with patch("agent.auxiliary_client.call_llm", return_value=_aux(payload2)), \
                patch("agent.auxiliary_client.get_auxiliary_extra_body", return_value={}), \
                patch("hermes_cli.kanban_decompose._load_config",
                      return_value={"kanban": {"default_assignee": "fallback"}}):
            outcome2 = decomp.decompose_task(tid2, author="me")
    finally:
        for p in started2:
            p.stop()
    assert outcome2.ok, outcome2.reason
    with kb.connect() as conn:
        assert kb.get_task(conn, tid2).assignee == "fallback"


def test_ac_loop_f38_7() -> None:
    """Buddy monitor: /review builds an independent review from a session snapshot."""
    from agent.review_engine import (
        build_review_task,
        format_dispatch_note,
        snapshot_recent_messages,
    )

    messages = (
        [{"role": "system", "content": "sys"}]
        + [{"role": "user", "content": "review my PR loop-f38-sentinel"}]
        + [{"role": "assistant", "content": "PR #99 opened"}]
        + [{"role": "tool", "content": "tool output"}]
    )
    snapshot = snapshot_recent_messages(messages)
    assert snapshot and all(m["role"] in ("user", "assistant") for m in snapshot)

    goal, context = build_review_task(snapshot, "focus on security")
    assert "reviewer" in goal.lower()
    assert "loop-f38-sentinel" in context
    assert "PR #99 opened" in context
    assert "focus on security" in context

    note = format_dispatch_note({"status": "dispatched", "review_model": "opus"}, "security")
    assert "re-enter" in note


def test_ac_loop_f38_8() -> None:
    """Buddy monitor: the post-turn background_review fork is gated by its config master switch."""
    from agent.background_review import is_background_review_enabled
    from hermes_cli.config import DEFAULT_CONFIG

    assert is_background_review_enabled({"enabled": True}) is True
    assert is_background_review_enabled({"enabled": False}) is False
    assert isinstance(
        DEFAULT_CONFIG["auxiliary"]["background_review"]["enabled"], bool)


def test_ac_loop_f38_9() -> None:
    """The slang-coworkers loop-controls doc page exists and maps the four controls with dual cites."""
    assert DOC.is_file(), f"doc page missing: {DOC}"
    text = DOC.read_text(encoding="utf-8")

    for marker in ("Plan gate", "Workflow-state reset", "Intent router", "Buddy monitor"):
        assert marker in text, f"doc missing behaviour section: {marker!r}"

    # The equivalence caveats must survive in the delivered page: cross-turn gate
    # deferred to LOOP-F37, no automatic intent classifier, per-section caveats.
    for caveat in ("LOOP-F37", "classifier", "Caveat"):
        assert caveat in text, f"doc missing equivalence caveat: {caveat!r}"

    for n in range(1, 10):
        assert f"AC-LOOP-F38-{n}" in text, f"doc does not enumerate AC-LOOP-F38-{n}"

    lines = text.splitlines()
    for symbol, tag_anchor, main_anchor in _DUAL_CITES:
        rows = [ln for ln in lines if symbol in ln and "tag:" in ln]
        assert rows, f"no dual-cite row for {symbol!r}"
        assert any(
            f"tag: `{tag_anchor}`" in ln and f"main: `{main_anchor}`" in ln for ln in rows
        ), f"{symbol!r} lacks its dual cite tag: `{tag_anchor}` / main: `{main_anchor}`"
