"""Acceptance test for FLEET-F62.g's findings-carrying codex_critique (AC-FLEET-F62.g-1 … -9).

Loads ``plugins/nv-fleet-gates`` through the real ``PluginManager().discover_and_load()`` from an isolated
``HERMES_HOME`` with an EMPTY bundled-plugin dir (shape of ``tests/hermes_cli/test_plugin_api_compat.py``),
drives the real ``codex_critique`` tool / slash alias with the host LLM lane stubbed at
``PluginLlm.acomplete_structured``, and reads gate outcomes through the real ``pre_tool_call`` hook.

Behaviour contract only: no source reading, no network, nothing under ~/.hermes.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import hermes_yaml as yaml

from hermes_cli.plugins import PluginManager
from tools.registry import registry

PLUGIN_KEY = "nv-fleet-gates"
_REPO = Path(__file__).resolve().parents[2]
PLUGIN_SRC = _REPO / "plugins" / PLUGIN_KEY
MARKER = "[Fix Report]"
DIFF = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-x = 1\n+x = 2  # SENTINEL_DIFF_7Q\n"
FINDING = {"location": "x.py:1", "problem": "magic number", "fix": "name the constant"}


# --------------------------------------------------------------------------- #
# (a) fixtures
# --------------------------------------------------------------------------- #
def _load(tmp_path, monkeypatch, **overrides):
    home = tmp_path / "home" / "worker-a"
    (home / "plugins").mkdir(parents=True)
    bundled = tmp_path / "empty-bundled"
    bundled.mkdir()
    edges = tmp_path / "fleet" / "edges.db"
    edges.parent.mkdir(parents=True)
    if not PLUGIN_SRC.exists():
        pytest.fail(f"plugin source missing: {PLUGIN_SRC}")
    shutil.copytree(PLUGIN_SRC, home / "plugins" / PLUGIN_KEY)
    settings = {
        "role": "worker", "profile": "worker-a",
        "profile_roles": {"orch": "orchestrator", "worker-a": "worker"},
        "enforce_sandbox": False, "required_stages": ["OUTPUT_REVIEW"], "plan_gate": False,
        "edges_db_path": str(edges), "stage_markers": [MARKER],
        "skills_root": str(home / "skills"), "shared_learnings_path": str(home / "shared"),
    }
    settings.update(overrides)
    cfg = {"plugins": {"enabled": [PLUGIN_KEY], "entries": {PLUGIN_KEY: {"settings": settings}}}}
    (home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.delenv("HERMES_ENABLE_PROJECT_PLUGINS", raising=False)
    import model_tools
    model_tools.discover_builtin_tools()
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled is True and loaded.error is None and loaded.module is not None
    assert "codex_critique" in loaded.tools_registered
    _wire(manager, "worker-a", "orch")
    return manager


def _wire(manager, frm, to):
    import argparse
    entry = manager._cli_commands["wire"]
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="cmd")
    entry["setup_fn"](sub.add_parser("wire"))
    ns = parser.parse_args(["wire", "add", frm, to])
    (entry.get("handler_fn") or ns.func)(ns)


def _blocked(manager, session_id):
    """True when a wired, stage-marked hand-off is refused by the gate (critique not fresh)."""
    results = manager.invoke_hook(
        "pre_tool_call", tool_name="message_agent",
        args={"target": "orch", "message": f"{MARKER} done"}, session_id=session_id,
        task_id="t", tool_call_id="tc", turn_id="u")
    return any(isinstance(r, dict) and r.get("action") == "block" for r in (results or []))


def _backend(monkeypatch, replies):
    """Stub the host LLM lane; ``replies`` items are dicts (parsed JSON), strings (raw text) or exceptions."""
    from agent import plugin_llm
    calls = []
    queue = list(replies)

    async def _fake(self, **kw):
        calls.append(kw)
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, dict):
            return plugin_llm.PluginLlmStructuredResult(
                text=json.dumps(item), provider="t", model="t", agent_id="t",
                parsed=item, content_type="json")
        return plugin_llm.PluginLlmStructuredResult(
            text=item, provider="t", model="t", agent_id="t", parsed=None, content_type="text")

    monkeypatch.setattr(plugin_llm.PluginLlm, "acomplete_structured", _fake, raising=False)
    return calls


def _sent_text(call):
    """Everything the critique backend was given, as one string."""
    parts = [str(call.get("instructions") or "")]
    for block in call.get("input") or []:
        parts.append(str(block.get("text") if isinstance(block, dict) else getattr(block, "text", block)))
    return "\n".join(parts)


def _critique(session_id="sess-1", **args):
    args.setdefault("stage", "OUTPUT_REVIEW")
    return json.loads(registry.dispatch("codex_critique", args, session_id=session_id))


def _artifacts():
    return [{"name": "diff", "content": DIFF}]


# --------------------------------------------------------------------------- #
# (a) codex_critique — ids 1-9
# --------------------------------------------------------------------------- #
def test_ac_fleet_f62_g_1(tmp_path, monkeypatch):
    """codex_critique sends the caller's stage, task text, and every artifact's name and content to the critique backend."""
    _load(tmp_path, monkeypatch)
    calls = _backend(monkeypatch, [{"verdict": "approve", "stage": "CODE_REVIEW", "findings": []}])
    _critique(stage="CODE_REVIEW", task="TASK_SENTINEL_31 rename x",
              artifacts=[{"name": "patch.diff", "content": DIFF},
                         {"name": "notes.md", "content": "NOTES_SENTINEL_88"}])
    assert len(calls) == 1
    sent = _sent_text(calls[0])
    for needle in ("CODE_REVIEW", "TASK_SENTINEL_31", "patch.diff", "SENTINEL_DIFF_7Q",
                   "notes.md", "NOTES_SENTINEL_88"):
        assert needle in sent, f"{needle!r} never reached the critique backend"


def test_ac_fleet_f62_g_2(tmp_path, monkeypatch):
    """A must-fix reply with at least one well-formed finding (location, problem, fix) is returned to the caller with those findings verbatim, and it records the critique row (gate rule unchanged: a critique that ran counts)."""
    manager = _load(tmp_path, monkeypatch)
    _backend(monkeypatch, [{"verdict": "must-fix", "stage": "OUTPUT_REVIEW", "findings": [FINDING]}])
    assert _blocked(manager, "sess-1")
    out = _critique(artifacts=_artifacts())
    assert out["ok"] is True
    assert out["verdict"] == "must-fix"
    assert out["findings"] == [FINDING]
    assert not _blocked(manager, "sess-1")


@pytest.mark.parametrize("bad", [
    {"verdict": "must-fix", "stage": "OUTPUT_REVIEW"},
    {"verdict": "must-fix", "stage": "OUTPUT_REVIEW", "findings": [{"location": "x.py:1"}]},
    "",
    "not json at all",
    {"verdict": "maybe", "stage": "OUTPUT_REVIEW", "findings": [FINDING]},
    {"verdict": "approve", "stage": "PLAN_REVIEW", "findings": []},
    {"verdict": "approve", "findings": []},
])
def test_ac_fleet_f62_g_3(tmp_path, monkeypatch, bad):
    """A backend reply that is findings-less must-fix, unparseable, empty, with an unknown verdict, or for a missing or different stage yields ok:false, reason critique_invalid_reply, no verdict key, after exactly two backend calls, and records no critique row."""
    manager = _load(tmp_path, monkeypatch)
    calls = _backend(monkeypatch, [bad])
    out = _critique(artifacts=_artifacts())
    assert out["ok"] is False
    assert out["reason"] == "critique_invalid_reply"
    assert "verdict" not in out
    assert len(calls) == 2, "exactly one retry"
    assert _blocked(manager, "sess-1"), "an invalid critique must not satisfy the gate"


def test_ac_fleet_f62_g_4(tmp_path, monkeypatch):
    """(derived) When the first reply is invalid and the retry is valid, the retry's verdict and findings are returned and recorded."""
    manager = _load(tmp_path, monkeypatch)
    calls = _backend(monkeypatch, [{"verdict": "must-fix", "stage": "OUTPUT_REVIEW"},
                                   {"verdict": "approve", "stage": "OUTPUT_REVIEW", "findings": []}])
    out = _critique(artifacts=_artifacts())
    assert len(calls) == 2
    assert out["ok"] is True and out["verdict"] == "approve"
    assert not _blocked(manager, "sess-1")


def test_ac_fleet_f62_g_5(tmp_path, monkeypatch):
    """A backend call that raises yields ok:false, reason critique_backend_error, the error text, no verdict key, and no critique row."""
    manager = _load(tmp_path, monkeypatch)
    _backend(monkeypatch, [RuntimeError("BACKEND_DOWN_42")])
    out = _critique(artifacts=_artifacts())
    assert out["ok"] is False
    assert out["reason"] == "critique_backend_error"
    assert "BACKEND_DOWN_42" in out["error"]
    assert "verdict" not in out
    assert _blocked(manager, "sess-1")


@pytest.mark.parametrize("artifacts", [
    None,
    [],
    [{"name": "diff", "content": "   \n"}],
    [{"name": "/sandbox/work/x.py"}],                    # a path is not content the gateway can read
])
def test_ac_fleet_f62_g_6(tmp_path, monkeypatch, artifacts):
    """A call with no artifact content (artifacts missing, empty, blank-only, or path-only entries) yields ok:false, reason critique_no_artifacts, makes no backend call, and records no row."""
    manager = _load(tmp_path, monkeypatch)
    calls = _backend(monkeypatch, [{"verdict": "approve", "stage": "OUTPUT_REVIEW", "findings": []}])
    args = {} if artifacts is None else {"artifacts": artifacts}
    out = _critique(**args)
    assert out["ok"] is False
    assert out["reason"] == "critique_no_artifacts"
    assert calls == []
    assert _blocked(manager, "sess-1")


def test_ac_fleet_f62_g_7(tmp_path, monkeypatch):
    """A call whose task and artifact text exceed critique_max_chars yields ok:false, reason critique_artifacts_too_large, makes no backend call, and records no row."""
    manager = _load(tmp_path, monkeypatch, critique_max_chars=100)
    calls = _backend(monkeypatch, [{"verdict": "approve", "stage": "OUTPUT_REVIEW", "findings": []}])
    out = _critique(task="t", artifacts=[{"name": "big", "content": "y" * 300}])
    assert out["ok"] is False
    assert out["reason"] == "critique_artifacts_too_large"
    assert calls == []
    assert _blocked(manager, "sess-1")


def _slash(manager, raw, session_id="sess-1"):
    from model_tools import _run_async
    handler = manager._plugin_commands["codex-critique"]["handler"]
    return json.loads(_run_async(handler(raw, session_id=session_id)))


def test_ac_fleet_f62_g_8(tmp_path, monkeypatch):
    """The /codex-critique <STAGE> <text> slash alias reviews <text> as an artifact; a bare /codex-critique <STAGE> yields ok:false, reason critique_no_artifacts, with no row; with no session context it yields ok:false, reason critique_no_session, with no verdict and no backend call."""
    manager = _load(tmp_path, monkeypatch)
    calls = _backend(monkeypatch, [{"verdict": "approve", "stage": "OUTPUT_REVIEW", "findings": []}])
    out = _slash(manager, "OUTPUT_REVIEW SLASH_TEXT_SENTINEL_5 the drafted reply", session_id="sess-s")
    assert out["ok"] is True
    assert "SLASH_TEXT_SENTINEL_5" in _sent_text(calls[0])
    assert not _blocked(manager, "sess-s")
    calls.clear()
    bare = _slash(manager, "OUTPUT_REVIEW", session_id="sess-bare")
    assert bare["ok"] is False and bare["reason"] == "critique_no_artifacts"
    assert calls == []
    assert _blocked(manager, "sess-bare")
    from model_tools import _run_async
    handler = manager._plugin_commands["codex-critique"]["handler"]
    orphan = json.loads(_run_async(handler("OUTPUT_REVIEW some text")))          # no session context at all
    assert orphan["ok"] is False and orphan["reason"] == "critique_no_session"
    assert "verdict" not in orphan
    assert calls == []


def test_ac_fleet_f62_g_9(tmp_path, monkeypatch):
    """(regression) An approve with artifacts records the row for its own session only: that session's marked delivery passes, another session's stays blocked, and a later file mutation re-blocks it."""
    manager = _load(tmp_path, monkeypatch)
    _backend(monkeypatch, [{"verdict": "approve", "stage": "OUTPUT_REVIEW", "findings": []}])
    payload = {"stage": "OUTPUT_REVIEW", "artifacts": _artifacts(), "session_id": "sB"}
    registry.dispatch("codex_critique", payload, session_id="sA")     # model-supplied session_id is ignored
    assert not _blocked(manager, "sA")
    assert _blocked(manager, "sB")
    manager.invoke_hook("post_tool_call", tool_name="write_file",
                        args={"path": "src/x.py", "content": "x=2"},
                        status="ok", result="ok", session_id="sA")
    assert _blocked(manager, "sA"), "a mutation after the critique restales it"

