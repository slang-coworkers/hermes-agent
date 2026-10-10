"""Acceptance test for FLEET-F62.g: findings-carrying codex_critique + delivered-means-delivered message_agent.

Ships as ``tests/plugins/test_fleet_f62_g_acceptance.py``. One ``test_ac_fleet_f62_g_<n>`` per ADR
acceptance criterion (16 pytest ids, ADR ``reports/fleet-f62.g.md`` order).

(a) ids 1-9: loads ``plugins/nv-fleet-gates`` through the real ``PluginManager().discover_and_load()`` from
an isolated ``HERMES_HOME`` with an EMPTY bundled-plugin dir (shape of
``tests/hermes_cli/test_plugin_api_compat.py``), drives the real ``codex_critique`` tool / slash alias with
the host LLM lane stubbed at ``PluginLlm.acomplete_structured``, and reads gate outcomes through the real
``pre_tool_call`` hook.

(b) ids 10-16: drives the real ``message_agent_tool`` -> real ``terminal_tool(background=True,
_host_local=True)`` -> real delivery runner -> a FAKE target ``hermes`` executable that sits beside a fake
``sys.executable`` (so ``bot_relay._hermes_cli()`` resolves it). The fake target writes its inbound row
into the target profile's Bot Chat with the real ``SessionDB`` exactly as the real CLI's turn-start
persistence does, then replies on stdout.

Behaviour contract only: no source reading, no network, nothing under ~/.hermes.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import textwrap
import time
from pathlib import Path

import pytest
import yaml

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
    import agent.plugin_llm as plugin_llm
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


# --------------------------------------------------------------------------- #
# (b) fixtures — real message_agent -> real background runner -> fake target `hermes`
# --------------------------------------------------------------------------- #
_FAKE_TARGET = textwrap.dedent('''\
    #!{python}
    """Fake target `hermes -p <profile> chat ... -Q --query-file <f>` for FLEET-F62.g."""
    import json, os, sys, time, uuid
    sys.path.insert(0, {repo!r})
    cfg = json.load(open({cfg!r}, encoding="utf-8"))
    argv = sys.argv[1:]
    profile = argv[argv.index("-p") + 1]
    query = open(argv[argv.index("--query-file") + 1], encoding="utf-8").read()
    open({pidfile!r}, "w", encoding="utf-8").write(str(os.getpid()))
    if cfg["mode"] == "exit3":
        print("Error: FAKE_TARGET_REFUSED", file=sys.stderr)
        sys.exit(3)
    if cfg["mode"] == "exit0_unpersisted":
        print(cfg.get("reply", "REPLY"))
        sys.exit(0)
    time.sleep(cfg["pre"])
    from hermes_state import SessionDB
    root = os.environ["HERMES_HOME"]
    db_path = os.path.join(root, "state.db") if profile == "default" else os.path.join(
        root, "profiles", profile, "state.db")
    db = SessionDB(db_path=__import__("pathlib").Path(db_path))
    sid = db.resolve_session_by_title("Bot Chat")              # the CLI's own -c resolver (latest "#N")
    sid = (db.get_compression_tip(sid) or sid) if sid else sid
    if not sid:
        sid = db.create_session(uuid.uuid4().hex, source="cli")
        db.set_session_title(sid, "Bot Chat")
    if cfg["mode"] == "prefix_only":
        db.append_message(sid, role="user", content=query[:256])
        print(cfg.get("reply", "REPLY"))
        sys.exit(0)
    db.append_message(sid, role="user", content=query)   # turn-start persistence, before any model call
    open({accepted!r}, "w", encoding="utf-8").write(repr(time.time()))
    time.sleep(cfg["post"])
    print(cfg.get("reply", "REPLY"))
''')


class _FakeDB:
    def __init__(self, home, title):
        self.db_path = str(home / "state.db")
        self._title = title

    def get_session_title(self, _sid):
        return self._title


class _FakeAgent:
    def __init__(self, home):
        self._session_db = _FakeDB(home, "Bot Chat")
        self.session_id = "sess-sender"
        self._session_title_hint = None
        self._bot_mode_protocol = True
        self.tools = []
        self.valid_tool_names = set()


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """A Bot-Mode-managed root (sender = default 'hermes', target = 'researcher') + a fake target CLI."""
    from tools import bot_mode_dm, bot_mode_probe, bot_relay
    from tools.process_registry import process_registry

    bot_mode_probe._reset_cache_for_tests()
    root = tmp_path / ".hermes"
    target = root / "profiles" / "researcher"
    target.mkdir(parents=True)
    (target / "profile.yaml").write_text(
        "description: teammate\nui_meta:\n  hermes-bots:\n    shape: cloud\n", encoding="utf-8")
    (root / "config.yaml").write_text(yaml.safe_dump({"bot_mode": {"turn_wait_seconds": 1}}),
                                      encoding="utf-8")
    dm_dir = tmp_path / "dm"
    dm_dir.mkdir(mode=0o700)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setattr(bot_mode_dm, "_dm_dir", lambda: dm_dir)
    monkeypatch.setattr(bot_mode_dm, "_DELIVERY_ACK_SLACK_SECONDS", 20, raising=False)
    cfg = tmp_path / "fake-target.json"
    paths = {"pidfile": str(tmp_path / "target.pid"), "accepted": str(tmp_path / "accepted.txt")}
    fake = tmp_path / "bin" / "hermes"
    fake.parent.mkdir()
    fake.write_text(_FAKE_TARGET.format(python=sys.executable, repo=str(_REPO), cfg=str(cfg), **paths),
                    encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setattr(bot_relay, "_hermes_cli", lambda: str(fake))

    def configure(mode="accept", pre=0.2, post=0.0, reply="REPLY"):
        cfg.write_text(json.dumps({"mode": mode, "pre": pre, "post": post, "reply": reply}), encoding="utf-8")

    def send(body="status? PAYLOAD_F62G"):
        return json.loads(bot_mode_dm.message_agent_tool(
            target="researcher", message=body, agent=_FakeAgent(root)))

    def transcript():
        from hermes_state import SessionDB
        db_path = target / "state.db"
        if not db_path.exists():
            return []
        db = SessionDB(db_path=db_path)
        sid = db.resolve_session_by_title("Bot Chat")
        sid = (db.get_compression_tip(sid) or sid) if sid else sid
        return [m for m in db.get_messages(sid) if m.get("role") == "user"] if sid else []

    def seed_continuation():
        """An older exact-title "Bot Chat" plus the newer "Bot Chat #2" continuation the CLI resumes."""
        from hermes_state import SessionDB
        db = SessionDB(db_path=target / "state.db")
        old = db.create_session("bc-old", source="cli")
        db.set_session_title(old, "Bot Chat")
        cont = db.create_session("bc-2", source="cli")
        db.set_session_title(cont, "Bot Chat #2")
        return db, old, cont

    def seed_compressed():
        """A titled "Bot Chat" that was compressed into an untitled live continuation (its tip)."""
        from hermes_state import SessionDB
        db = SessionDB(db_path=target / "state.db")
        parent = db.create_session("bc-parent", source="cli")
        db.set_session_title(parent, "Bot Chat")
        db.end_session(parent, "compression")
        tip = db.create_session("bc-tip", source="cli", parent_session_id=parent)
        return db, parent, tip

    ns = type("Fleet", (), {})()
    ns.seed_continuation = seed_continuation
    ns.seed_compressed = seed_compressed
    ns.root, ns.dm_dir, ns.configure, ns.send, ns.transcript = root, dm_dir, configure, send, transcript
    ns.pidfile, ns.accepted, ns.registry, ns.bot_relay = Path(paths["pidfile"]), Path(paths["accepted"]), \
        process_registry, bot_relay
    yield ns
    process_registry.kill_all(consume_output=True)
    if ns.pidfile.exists():
        try:
            os.kill(int(ns.pidfile.read_text(encoding="utf-8")), 9)
        except (OSError, ValueError):
            pass
    bot_mode_probe._reset_cache_for_tests()


def _pid_alive(pid, wait=5.0):
    import psutil
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return False
        except psutil.NoSuchProcess:
            return False
        time.sleep(0.1)
    return True


def _no_autonomous_completion(registry, proc_id):
    """The failed runner carries no completion contract: nothing queued, no gateway watcher registered."""
    queued = []
    while not registry.completion_queue.empty():
        queued.append(registry.completion_queue.get_nowait())
    for evt in queued:
        registry.completion_queue.put(evt)
    assert not [e for e in queued if e.get("session_id") == proc_id], queued
    assert not [w for w in registry.pending_watchers if w.get("session_id") == proc_id]
    assert registry.get(proc_id).notify_on_complete is False


def _delivery_sessions(registry):
    return [s for s in registry.list_sessions() if "--run-delivery" in (s.get("command") or "")]


# --------------------------------------------------------------------------- #
# (b) message_agent — ids 10-16 (real processes; POSIX process tree + flock)
# --------------------------------------------------------------------------- #
def _no_payload(fleet):
    return not any("PAYLOAD_F62G" in (m.get("content") or "") for m in fleet.transcript())


@pytest.mark.linux_only
def test_ac_fleet_f62_g_10(fleet):
    """A local delivery whose target's turn lock stays held past bot_mode.turn_wait_seconds returns an error with reason target_busy and no status; the runner is stopped, its completion is marked consumed, and the DM payload file is gone."""
    fleet.configure(mode="accept")
    with fleet.bot_relay.acquire_turn_lock(fleet.root, "researcher", timeout_seconds=0):
        out = fleet.send()
        sessions = _delivery_sessions(fleet.registry)
    assert "status" not in out, out
    assert out.get("reason") == "target_busy", out
    assert sessions, "the delivery runner was never spawned"
    proc_id = sessions[-1]["session_id"]
    assert fleet.registry.get(proc_id).exited
    assert fleet.registry.is_completion_consumed(proc_id)
    _no_autonomous_completion(fleet.registry, proc_id)
    assert list(fleet.dm_dir.iterdir()) == []
    assert _no_payload(fleet)


@pytest.mark.linux_only
@pytest.mark.parametrize("how", ["target_exits", "target_exit0_unpersisted", "runner_killed"])
def test_ac_fleet_f62_g_11(fleet, how):
    """A local delivery whose target exits before accepting the message (non-zero, or zero without persisting it), or whose runner is killed before acceptance, returns an error (no status sent), queues no autonomous completion, and the target transcript holds no copy of the message."""
    import threading
    if how == "target_exits":
        fleet.configure(mode="exit3")
    elif how == "target_exit0_unpersisted":
        fleet.configure(mode="exit0_unpersisted")
    else:
        fleet.configure(mode="accept", pre=6.0)

        def _kill_runner():
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                live = [s for s in _delivery_sessions(fleet.registry) if s.get("status") == "running"]
                if live and fleet.pidfile.exists():
                    fleet.registry.kill_process(live[-1]["session_id"], consume_output=False)
                    return
                time.sleep(0.1)

        threading.Thread(target=_kill_runner, daemon=True).start()
    out = fleet.send()
    assert out.get("status") != "sent", out
    assert "error" in out, out
    if how == "target_exit0_unpersisted":
        assert out.get("reason") == "delivery_unconfirmed", out
    _no_autonomous_completion(fleet.registry, _delivery_sessions(fleet.registry)[-1]["session_id"])
    time.sleep(1.0)
    assert _no_payload(fleet)


@pytest.mark.linux_only
def test_ac_fleet_f62_g_12(fleet, monkeypatch):
    """A local delivery the target does not accept within turn_wait_seconds() + _DELIVERY_ACK_SLACK_SECONDS returns an error with reason delivery_timeout; the runner and target are no longer running, the completion is consumed, and the message never appears in the target transcript afterwards."""
    from tools import bot_mode_dm
    monkeypatch.setattr(bot_mode_dm, "_DELIVERY_ACK_SLACK_SECONDS", 2, raising=False)   # bound = 1 + 2 s
    fleet.configure(mode="accept", pre=6.0)
    started = time.monotonic()
    out = fleet.send()
    assert out.get("reason") == "delivery_timeout", out
    assert "status" not in out
    assert time.monotonic() - started < 6.0, "the bound, not the target, ended the wait"
    proc_id = _delivery_sessions(fleet.registry)[-1]["session_id"]
    assert fleet.registry.get(proc_id).exited
    _no_autonomous_completion(fleet.registry, proc_id)
    if fleet.pidfile.exists():                                        # absent = the target never started
        assert not _pid_alive(int(fleet.pidfile.read_text(encoding="utf-8"))), "the target turn must be stopped"
    time.sleep(max(0.0, started + 7.5 - time.monotonic()))           # past the fake target's own accept time
    assert _no_payload(fleet), "a timed-out delivery must never land later"


@pytest.mark.linux_only
@pytest.mark.parametrize("shape", ["fresh", "continuation", "compressed", "duplicate", "prefix_collision"])
def test_ac_fleet_f62_g_13(fleet, shape):
    """status sent is returned only once the Bot Chat session the target CLI resumes (latest "Bot Chat #N" continuation, projected to its compression tip) holds a role='user' row containing the entire delivered attributed content (stripped), newer than the delivery's own under-lock baseline; an earlier identical message never acknowledges a later one, and a newer row holding only a prefix of the content never acknowledges it."""
    if shape == "prefix_collision":
        long_body = "status? PAYLOAD_F62G " + "x" * 400 + " TAIL_F62G"
        fleet.configure(mode="prefix_only", pre=0.5)
        out = fleet.send(body=long_body)
        assert out.get("status") != "sent", out
        assert out.get("reason") == "delivery_unconfirmed", out
        assert not [m for m in fleet.transcript() if "TAIL_F62G" in (m.get("content") or "")]
        return
    fleet.configure(mode="accept", pre=1.0, post=0.2)
    if shape == "continuation":
        db, old, cont = fleet.seed_continuation()
    if shape == "compressed":
        db, old, cont = fleet.seed_compressed()
    if shape == "duplicate":
        first = fleet.send()
        assert first.get("status") == "sent", first
        fleet.configure(mode="accept", pre=1.5, post=0.2)
    out = fleet.send()
    rows = fleet.transcript()                                         # read at the moment "sent" returned
    assert out.get("status") == "sent", out
    mine = [m for m in rows if "PAYLOAD_F62G" in (m.get("content") or "")
            and (m.get("content") or "").startswith("Message from")]
    assert len(mine) == (2 if shape == "duplicate" else 1), "'sent' returned before the target accepted THIS message"
    if shape in ("continuation", "compressed"):
        assert not [m for m in db.get_messages(old) if "PAYLOAD_F62G" in (m.get("content") or "")]
        assert [m for m in db.get_messages(cont) if "PAYLOAD_F62G" in (m.get("content") or "")]


def _assert_one_reply_completion(fleet, proc_id):
    deadline = time.monotonic() + 20
    while not fleet.registry.get(proc_id).exited and time.monotonic() < deadline:
        time.sleep(0.2)
    assert fleet.registry.get(proc_id).exited
    import queue as _queue
    mine, others = [], []
    deadline = time.monotonic() + 5                                   # exited is set before the completion is queued
    while time.monotonic() < deadline:
        try:
            evt = fleet.registry.completion_queue.get(timeout=0.2)
        except _queue.Empty:
            if mine:
                break
            continue
        if evt.get("session_id") == proc_id and evt.get("type", "completion") == "completion":
            mine.append(evt)
        else:
            others.append(evt)
    for evt in others:
        fleet.registry.completion_queue.put(evt)
    assert len(mine) == 1, mine
    assert "REPLY_SENTINEL_14" in (mine[0].get("output") or "")
    assert '"state"' not in (mine[0].get("output") or ""), "the ack sideband must not leak into the reply"
    assert not fleet.registry.is_completion_consumed(proc_id)


@pytest.mark.linux_only
@pytest.mark.parametrize("arming", ["normal", "delayed", "gateway"])
def test_ac_fleet_f62_g_14(fleet, monkeypatch, arming):
    """(regression) An accepted delivery stays asynchronous: message_agent returns while the target turn is still running (or holding), and the target's reply arrives afterwards as exactly one unconsumed completion notification carrying the reply text, also when the target replies before the sender has armed notification; a gateway-hosted sender gets exactly one pending_watchers entry for the process, carrying its routing fields."""
    import contextvars
    import gateway.session_context as sc
    monkeypatch.setattr(fleet.registry, "pending_watchers", [])
    route = {"platform": "telegram", "chat_id": "chat-77", "thread_id": "th-9", "user_id": "u-1",
             "user_name": "op", "message_id": "m-5", "session_id": "sess-db-42", "session_key": "gw:chat-77"}
    if arming == "gateway":
        monkeypatch.setattr(sc, "_session_context_engaged", sc._session_context_engaged)
        fleet.configure(mode="accept", pre=0.2, post=3.0, reply="REPLY_SENTINEL_14")

        def _send_in_gateway_turn():
            sc.set_session_vars(**route)
            return fleet.send()

        out = contextvars.copy_context().run(_send_in_gateway_turn)   # bound vars never leak into later tests
        assert out.get("status") == "sent", out
        proc_id = out.get("process_id") or _delivery_sessions(fleet.registry)[-1]["session_id"]
        assert not fleet.registry.get(proc_id).exited, "message_agent must not wait for the reply"
        mine = [w for w in fleet.registry.pending_watchers if w.get("session_id") == proc_id]
        assert len(mine) == 1, fleet.registry.pending_watchers
        w = mine[0]
        assert (w["platform"], w["chat_id"], w["thread_id"], w["user_id"], w["user_name"], w["message_id"]) == (
            "telegram", "chat-77", "th-9", "u-1", "op", "m-5")
        assert w["notify_on_complete"] is True
        assert w["parent_session_id"] == "sess-db-42"
        session = fleet.registry.get(proc_id)
        assert w["session_key"] == session.session_key
        assert session.watcher_platform == "telegram"
        assert session.watcher_interval == 5 and w["check_interval"] == 5
        assert session.notify_on_complete is True
        _assert_one_reply_completion(fleet, proc_id)
        return
    if arming == "delayed":
        from tools import bot_mode_dm
        real_arm = bot_mode_dm._arm_completion_notify

        def _slow_arm(*a, **k):
            time.sleep(2.0)                       # the fake target has replied and exited by now
            return real_arm(*a, **k)

        monkeypatch.setattr(bot_mode_dm, "_arm_completion_notify", _slow_arm)
        fleet.configure(mode="accept", pre=0.2, post=0.0, reply="REPLY_SENTINEL_14")
    else:
        fleet.configure(mode="accept", pre=0.2, post=3.0, reply="REPLY_SENTINEL_14")
    out = fleet.send()
    assert out.get("status") == "sent", out
    proc_id = out.get("process_id") or _delivery_sessions(fleet.registry)[-1]["session_id"]
    if arming == "normal":
        assert not fleet.registry.get(proc_id).exited, "message_agent must not wait for the reply"
    _assert_one_reply_completion(fleet, proc_id)


@pytest.mark.linux_only
def test_ac_fleet_f62_g_15(fleet):
    """(derived) The DM-cache sweep removes aged delivery sidecars (*.signal, *.signal.tmp) as well as aged DM files, and keeps fresh ones."""
    from tools import bot_mode_dm
    old = time.time() - 3 * 24 * 3600
    aged = [fleet.dm_dir / "dm-a.txt.signal", fleet.dm_dir / "dm-b.txt.signal.tmp", fleet.dm_dir / "dm-c.txt"]
    fresh = fleet.dm_dir / "dm-d.txt.signal"
    for path in (*aged, fresh):
        path.write_text("{}", encoding="utf-8")
    for path in aged:
        os.utime(path, (old, old))
    bot_mode_dm.cleanup_bot_dm_cache()
    assert [p.name for p in aged if p.exists()] == []
    assert fresh.exists()


@pytest.mark.linux_only
@pytest.mark.parametrize("channel", ["stateless", "kanban"])
def test_ac_fleet_f62_g_16(fleet, monkeypatch, channel):
    """A sender whose session cannot receive an async completion (a stateless channel via declare_stateless_channel(), or a Kanban worker via HERMES_KANBAN_TASK) gets status delivered_untracked with reason notify_unsupported for an accepted delivery: no pending_watchers entry, notify_on_complete stays False, and the one-shot linger does not wait on the runner."""
    import contextvars
    import gateway.session_context as sc
    monkeypatch.setattr(fleet.registry, "pending_watchers", [])
    fleet.configure(mode="accept", pre=0.2, post=3.0)
    before = {s["session_id"] for s in _delivery_sessions(fleet.registry)}
    if channel == "stateless":
        def _send():
            sc.declare_stateless_channel()
            return fleet.send()
        out = contextvars.copy_context().run(_send)
    else:
        monkeypatch.setenv("HERMES_KANBAN_TASK", "kanban-task-16")
        out = fleet.send()
    assert out.get("status") == "delivered_untracked", out
    assert out.get("reason") == "notify_unsupported", out
    new = [s["session_id"] for s in _delivery_sessions(fleet.registry) if s["session_id"] not in before]
    assert len(new) == 1, new                                         # list_sessions lists running before finished
    proc_id = new[0]
    assert not [w for w in fleet.registry.pending_watchers if w.get("session_id") == proc_id]
    assert fleet.registry.get(proc_id).notify_on_complete is False
    assert not fleet.registry.get(proc_id).exited
    lingered = fleet.registry.wait_for_pending_completions(timeout=1, poll_interval=0.1)
    assert proc_id not in lingered.get("waited", [])
