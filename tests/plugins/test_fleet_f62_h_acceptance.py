"""Acceptance test for FLEET-F62.h: a busy target gets its message re-driven automatically (variant U).

Ships as ``tests/plugins/test_fleet_f62_h_acceptance.py``, written against Hermes ``v0.22.2`` = ``d923a5eb`` (the
0.22.0 delivery line: the per-profile turn flock, the ``target_busy`` refusal, the live-owner mailbox). There is one
``test_ac_fleet_f62_h_<n>`` per active ``pytest:`` row of the ADR ``reports/fleet-f62.h.md`` §Acceptance criteria
(ids 1-14, same order). Id 15 is WITHDRAWN by user ruling A1 (no test def, id kept). Id 16 is a live scenario.

Loads ``plugins/nv-delivery-redrive`` through the real ``PluginManager().discover_and_load()`` from an isolated
``HERMES_HOME`` with an EMPTY bundled-plugin dir (shape of ``tests/hermes_cli/test_plugin_api_compat.py``). Every send
is a real ``message_agent`` tool call run by a real ``AIAgent``'s sequential executor, so core's whole authorization
(scope, ``pre_tool_call`` + ``modify``, pruned-args, guardrails), the plugin middleware and ``post_tool_call`` all
run. ``message_agent`` itself is core's: real ``terminal_tool(background=True, _host_local=True)`` -> real delivery
runner -> real per-profile flock -> a FAKE target ``hermes`` script that writes its inbound row with the real
``SessionDB`` (turn-start persistence) and replies on stdout. The runner process starts through a shim that runs
core's own ``_delivery_main`` and only shortens its live-wait bounds when a test asks for it. Drainer ticks run
through the real ``on_kanban_dispatch_tick`` hook. A gateway restart is a fresh process registry recovered from the
checkpoint plus a freshly loaded plugin manager.

Sender = the default profile (handle ``hermes``, home = the root), target = ``researcher``, orchestrator =
``orchestrator``. Behaviour contract only: no source reading, no network, nothing under ~/.hermes. POSIX only (core's
turn lock is fcntl-based).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import hermes_yaml as yaml

PLUGIN_KEY = "nv-delivery-redrive"
_REPO = next(p for c in Path(__file__).resolve().parents for p in (c, c / "hermes-agent")
             if (p / "hermes_cli" / "plugins.py").is_file())
PLUGIN_SRC = _REPO / "plugins" / PLUGIN_KEY
TARGET = "researcher"
ORCH = "orchestrator"
SENDER_SID = "bc-hermes"

_FAKE_TARGET = textwrap.dedent('''\
    #!{python}
    """Fake `hermes -p <profile> chat --in ~ -c "Bot Chat" --create-if-missing -Q --query-file <f>`."""
    import json, os, sys, time, uuid
    sys.path.insert(0, {repo!r})
    argv = sys.argv[1:]
    profile = argv[argv.index("-p") + 1]
    query = open(argv[argv.index("--query-file") + 1], encoding="utf-8-sig").read()
    spec = json.load(open({cfg!r}, encoding="utf-8"))
    spec = spec.get(profile, spec.get("*", {{}}))
    with open({pidlog!r}, "a", encoding="utf-8") as f:
        f.write(json.dumps({{"profile": profile, "pid": os.getpid()}}) + "\\n")
    time.sleep(spec.get("pre", 0.1))
    if spec.get("mode") == "not_owned":
        print("hermes-refusal-reason: SESSION_NOT_OWNED", file=sys.stderr)
        sys.exit(1)
    if spec.get("mode") == "exit0_unpersisted":
        print("REPLY-FROM-" + profile)
        sys.exit(0)
    from hermes_state import SessionDB
    root = {root!r}
    home = root if profile == "default" else os.path.join(root, "profiles", profile)
    db = SessionDB(db_path=__import__("pathlib").Path(home) / "state.db")
    row = db.get_session_by_title("Bot Chat")
    sid = (db.get_compression_tip(row["id"]) or row["id"]) if row else None
    if not sid:
        sid = db.create_session(uuid.uuid4().hex, source="cli")
        db.set_session_title(sid, "Bot Chat")
    db.append_message(sid, role="user", content=query)      # turn-start persistence
    if spec.get("mode") == "hang_after_row":
        time.sleep(3600)
    time.sleep(spec.get("post", 0.0))
    db.append_message(sid, role="assistant", content="ACK " + profile)
    print("REPLY-FROM-" + profile)
''')


def _profile(root, name):
    home = root / "profiles" / name
    home.mkdir(parents=True, exist_ok=True)
    (home / "profile.yaml").write_text(
        "description: teammate\nui_meta:\n  hermes-bots:\n    shape: cloud\n", encoding="utf-8")
    return home


def _bot_chat(home):
    """(session id, every message) of the Bot Chat lineage the CLI resolves in ``home``."""
    from hermes_state import SessionDB

    if not (Path(home) / "state.db").exists():
        return None, []
    db = SessionDB(db_path=Path(home) / "state.db", read_only=True)
    try:
        row = db.get_session_by_title("Bot Chat")
        if row is None:
            return None, []
        sid = db.get_compression_tip(row["id"]) or row["id"]
        return sid, db.get_messages(sid, include_inactive=True, include_ancestors=True)
    finally:
        db.close()


def _append_user_row(home, session_id, content, **fields):
    from hermes_state import SessionDB

    db = SessionDB(db_path=Path(home) / "state.db")
    try:
        db.append_message(session_id, role="user", content=content, **fields)
    finally:
        db.close()


def _seed_bot_chat(home, session_id):
    from hermes_state import SessionDB

    db = SessionDB(db_path=Path(home) / "state.db")
    try:
        db.create_session(session_id=session_id, source="cli")
        db.set_session_title(session_id, "Bot Chat")
    finally:
        db.close()


def _make_sender_agent():
    """A real AIAgent (tests/agent/test_run_agent.py fixture shape) acting as the default profile's Bot Chat."""
    from run_agent import AIAgent
    from tools.bot_mode_dm import message_agent_tool_schema

    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1",
                        quiet_mode=True, skip_context_files=True, skip_memory=True)
    agent.client = MagicMock()
    agent._session_db = None                       # message_agent's home falls back to HERMES_HOME = the root
    agent.session_id = SENDER_SID
    agent._session_title_hint = "Bot Chat"
    agent.tools = list(agent.tools or []) + [message_agent_tool_schema()]
    agent.valid_tool_names = set(agent.valid_tool_names or set()) | {"message_agent"}
    return agent


def _assistant_turn(target, body, call_id):
    call = SimpleNamespace(id=call_id, type="function",
                           function=SimpleNamespace(name="message_agent",
                                                    arguments=json.dumps({"target": target, "message": body})))
    return SimpleNamespace(content="", tool_calls=[call])


_RUNNER_SHIM = textwrap.dedent('''\
    import json, sys
    from pathlib import Path
    sys.path.insert(0, {repo!r})
    try:
        import hermes_bootstrap  # noqa: F401  (the runner's own boot step)
    except Exception:
        pass
    import time
    gate = Path({state_dir!r}) / "runner.gate"
    while gate.exists():
        time.sleep(0.05)
    from tools import bot_mode_dm
    overrides = Path({state_dir!r}) / "runner.json"
    if overrides.exists():
        for key, value in json.loads(overrides.read_text(encoding="utf-8")).items():
            setattr(bot_mode_dm, key, value)
    raise SystemExit(bot_mode_dm._delivery_main(sys.argv[1:]))
''')


def install_runner_shim(shim_path, setattr_=setattr):
    """Core's delivery runner is a separate process: start it through a shim that can shorten its live-wait bounds
    (``runner.json``) and hold it before core's code runs while ``runner.gate`` exists. The real ``terminal_tool`` /
    process registry / completion path still runs it."""
    import shlex
    import tools.terminal_tool as terminal_tool_module

    real = terminal_tool_module.terminal_tool

    def shimmed(command, *args, **kwargs):
        if isinstance(command, str) and "--run-delivery" in command:
            parts = shlex.split(command)
            command = shlex.join([parts[0], str(shim_path), *parts[parts.index("--run-delivery"):]])
        return real(command, *args, **kwargs)

    setattr_(terminal_tool_module, "terminal_tool", shimmed)


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """A Bot-Mode-managed root: sender = default ('hermes'), target 'researcher', orchestrator; plugin enabled."""
    from hermes_cli import plugins as plugins_mod
    from tools import bot_mode_dm, bot_mode_probe, bot_relay
    from tools.process_registry import process_registry

    assert PLUGIN_SRC.is_dir(), "plugin source missing"
    bot_mode_probe._reset_cache_for_tests()
    root = tmp_path / ".hermes"
    root.mkdir()
    (root / "profile.yaml").write_text(
        "description: sender\nui_meta:\n  hermes-bots:\n    shape: cloud\n", encoding="utf-8")
    homes = {"default": root, TARGET: _profile(root, TARGET), ORCH: _profile(root, ORCH)}
    settings = {"redrive_window_seconds": 40, "max_attempts": 6, "retry_backoff_seconds": 1,
                "heartbeat_stale_seconds": 30, "orchestrator_profile": ORCH}
    cfg = {"plugins": {"enabled": [PLUGIN_KEY], "entries": {PLUGIN_KEY: {"settings": settings}}},
           "bot_mode": {"turn_wait_seconds": 1}}
    for home in homes.values():
        shutil.copytree(PLUGIN_SRC, home / "plugins" / PLUGIN_KEY)
        (home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    dm_dir = tmp_path / "dm"
    dm_dir.mkdir(mode=0o700)
    monkeypatch.setattr(bot_mode_dm, "_dm_dir", lambda: dm_dir)
    target_cfg = tmp_path / "fake-target.json"
    target_cfg.write_text(json.dumps({"*": {}}), encoding="utf-8")
    pidlog = tmp_path / "target-pids.jsonl"
    fake = tmp_path / "bin" / "hermes"
    fake.parent.mkdir()
    fake.write_text(_FAKE_TARGET.format(python=sys.executable, repo=str(_REPO), cfg=str(target_cfg),
                                        pidlog=str(pidlog), root=str(root)), encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setattr(bot_relay, "_hermes_cli", lambda: str(fake))
    state_dir = tmp_path / "runner-state"
    state_dir.mkdir()
    shim = tmp_path / "runner_shim.py"
    shim.write_text(_RUNNER_SHIM.format(repo=str(_REPO), state_dir=str(state_dir)), encoding="utf-8")
    install_runner_shim(shim, setattr_=monkeypatch.setattr)
    _seed_bot_chat(root, SENDER_SID)
    _seed_bot_chat(homes[ORCH], "bc-orchestrator")
    task_id = f"f62h-{os.urandom(8).hex()}"              # owns every registry spawn of this test (owner_task_id)

    ns = SimpleNamespace(root=root, homes=homes, dm_dir=dm_dir, pidlog=pidlog, fake=fake, state_dir=state_dir,
                         shim=shim, registry=process_registry, task_id=task_id,
                         bot_relay=bot_relay, bot_mode_dm=bot_mode_dm, agent=None, manager=None, loaded=None)

    def load():
        plugins_mod._reset_plugin_managers_for_tests()
        manager = plugins_mod.PluginManager()
        manager.discover_and_load()
        loaded = manager._plugins[PLUGIN_KEY]
        assert loaded.enabled is True and loaded.error is None and loaded.module is not None
        for hook in ("on_kanban_dispatch_tick", "transform_terminal_output", "post_tool_call"):
            assert hook in loaded.hooks_registered, (hook, loaded.hooks_registered)
        assert "tool_execution" in loaded.middleware_registered
        monkeypatch.setattr(plugins_mod, "_plugin_manager", manager)
        ns.manager, ns.loaded = manager, loaded
        return manager, loaded

    def configure(profile="*", **spec):
        data = json.loads(target_cfg.read_text(encoding="utf-8"))
        data[profile] = spec
        target_cfg.write_text(json.dumps(data), encoding="utf-8")

    def settings_(**overrides):
        for home in homes.values():
            data = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
            data["plugins"]["entries"][PLUGIN_KEY]["settings"].update(overrides)
            if "turn_wait_seconds" in overrides:
                data["bot_mode"]["turn_wait_seconds"] = overrides["turn_wait_seconds"]
            (home / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
        load()
        tick()

    def send(body, target=TARGET, raw=False):
        """One message_agent tool call through the real sequential executor; the tool result, parsed or raw text."""
        if ns.agent is None:
            ns.agent = _make_sender_agent()
        messages = []
        ns.agent._execute_tool_calls_sequential(
            _assistant_turn(target, body, f"call-{time.monotonic_ns()}"), messages, ns.task_id)
        tool_rows = [m for m in messages if m.get("role") == "tool"]
        assert tool_rows, messages
        content = tool_rows[-1]["content"]
        text = content if isinstance(content, str) else content[0]["text"]
        return text if raw else json.loads(text)

    def tick(n=1, pause=0.0):
        for _ in range(n):
            ns.manager.invoke_hook("on_kanban_dispatch_tick", board=None, profile_name="default",
                                   dry_run=False, outcome="idle", result=None)
            if pause:
                time.sleep(pause)

    def rows(home_name, needle):
        _sid, msgs = _bot_chat(homes[home_name])
        return [m for m in msgs if m.get("role") == "user" and needle in str(m.get("content") or "")
                and m.get("display_kind") != "process_complete"]

    def until(predicate, *, ticks=True, timeout=30.0, pause=0.25):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if ticks:
                tick()
            if predicate():
                return True
            time.sleep(pause)
        return predicate()

    ns.load, ns.configure, ns.settings, ns.send, ns.tick, ns.rows, ns.until = (
        load, configure, settings_, send, tick, rows, until)
    ns.ticket = lambda did: ns.loaded.module.store.ticket(did)
    ns.attempts = lambda did: ns.loaded.module.store.attempts(did)
    load()
    tick()                                               # a live drainer: fresh heartbeat before any send (D8)
    yield ns
    for registry in {id(r): r for r in (process_registry, ns.registry)}.values():
        # Only this test's registry spawns (owner_task_id). Not a lifecycle source, so a persist_on_release runner
        # stops too.
        registry.kill_all(task_id, source="test_teardown", consume_output=True)
    _kill_own_processes(tmp_path)                         # and every process outside the registry
    plugins_mod._reset_plugin_managers_for_tests()
    bot_mode_probe._reset_cache_for_tests()


def _kill_own_processes(tmp_path):
    """Stop every process this test started, and only those. Each one names the test's own pytest ``tmp_path`` in its
    argv (fake target CLIs, shim-started registry runners, the drainer's runners, the one-shot sender); nothing else
    can. Rescans, because a runner can start a child mid-sweep. Polls instead of reaping: the registry owns its Popens."""
    import psutil

    base = str(tmp_path)
    mine = os.path.join(base, "")                       # with the separator: never a sibling dir sharing the prefix

    def gone(proc):
        try:
            return not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE
        except psutil.Error:
            return True

    for _ in range(10):
        procs = [proc for proc in psutil.process_iter(["pid", "cmdline"])
                 if proc.info["pid"] != os.getpid() and any(part == base or mine in part for part in proc.info["cmdline"] or ())]
        if not procs:
            return
        for proc in procs:
            try:
                proc.kill()
            except psutil.Error:
                pass
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not all(gone(proc) for proc in procs):
            time.sleep(0.05)


def _target_pids(fleet):
    """Every target CLI pid the fake ever logged (an orphaned one outlives its runner)."""
    if not fleet.pidlog.exists():
        return []
    return [int(json.loads(line)["pid"]) for line in fleet.pidlog.read_text(encoding="utf-8").splitlines()]


def _alive(pid):
    import psutil

    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def _busy(fleet, profile=TARGET):
    """Hold ``profile``'s real cross-process turn lock (core's flock), as a long target turn does."""
    return fleet.bot_relay.acquire_turn_lock(fleet.root, profile, timeout_seconds=0)


def _wait_runner_done(fleet, process_id, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session = fleet.registry.get(process_id)
        if session is None or session.exited:
            return
        time.sleep(0.1)
    raise AssertionError(f"runner {process_id} still running after {timeout}s")


def _restart_gateway(fleet, monkeypatch, *, crash=False):
    """A gateway restart: a FRESH process registry recovered from the checkpoint (a new gateway process starts with
    an empty one) plus a freshly loaded plugin manager. ``crash`` models an abrupt death, in which the old process never
    collects an exit it had not collected yet. Returns how many runners the new registry recovered as detached."""
    from tools import process_registry as pr_mod

    if crash:
        monkeypatch.setattr(fleet.registry, "_finish_exited", lambda *a, **k: None)
    fresh = pr_mod.ProcessRegistry()
    monkeypatch.setattr(pr_mod, "process_registry", fresh)
    fleet.registry = fresh
    recovered = fresh.recover_from_checkpoint()
    fleet.load()
    return recovered


def _notices(fleet, home_name, delivery_id):
    _sid, msgs = _bot_chat(fleet.homes[home_name])
    return [m for m in msgs if m.get("display_kind") == "process_complete"
            and ((m.get("display_metadata") or {}).get("nv_delivery_redrive") or {}).get("delivery_id") == delivery_id]


def _admissions(home, needle):
    """Every mailbox receipt in ``home`` whose message contains ``needle``: one per admission core ever made."""
    from tools import bot_live_delivery as mailbox

    root = mailbox._root(home)
    found = []
    for path in sorted(root.glob("*.json")) if root.is_dir() else []:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if needle in str(record.get("message") or ""):
            found.append(record)
    return found


def _queued_completion(fleet, process_id):
    queued = []
    while not fleet.registry.completion_queue.empty():
        queued.append(fleet.registry.completion_queue.get_nowait())
    for evt in queued:
        fleet.registry.completion_queue.put(evt)
    mine = [e for e in queued if e.get("session_id") == process_id]
    assert mine, f"no completion notification for {process_id}"
    return mine[-1]


def _completion_output(fleet, process_id):
    evt = _queued_completion(fleet, process_id)
    return str(evt.get("output") or evt.get("output_preview") or "")


def _attempt_ids(fleet, delivery_id):
    """The delivery id core derives from every attempt's DM path (all attempts must reuse attempt 1's path)."""
    return {fleet.bot_mode_dm._dm_delivery_id(a["dm_path"]) for a in fleet.attempts(delivery_id)}


# --------------------------------------------------------------------------- #
# CLI path: re-drive, bound, late acceptance, unreadable transcript
# --------------------------------------------------------------------------- #
@pytest.mark.platforms("linux")
@pytest.mark.parametrize("wait", ["wait1", "wait0"])
def test_ac_fleet_f62_h_1(fleet, wait):
    """A local delivery whose target's turn lock is held past bot_mode.turn_wait_seconds and then released lands exactly once in the target's Bot Chat with no human action. Every attempt runs on the attempt-1 DM path, so _dm_delivery_id of each attempt equals the ack's delivery_id, and the ticket ends settled. Holds with a 1 s and a 0 s turn wait."""
    if wait == "wait0":
        fleet.settings(turn_wait_seconds=0)
    with _busy(fleet):
        ack = fleet.send("status? PAYLOAD_H1")
        did = ack.get("delivery_id")
        assert ack.get("status") == "queued" and did, ack
        assert ack.get("redrive", {}).get("tracked") is True, ack
        _wait_runner_done(fleet, ack["process_id"])
        fleet.tick(3, pause=0.5)
        assert fleet.rows(TARGET, "PAYLOAD_H1") == []
    assert fleet.until(lambda: fleet.ticket(did)["state"] == "settled", timeout=60.0)
    assert len(fleet.rows(TARGET, "PAYLOAD_H1")) == 1
    assert len(fleet.attempts(did)) >= 2
    assert _attempt_ids(fleet, did) == {did}
    # The busy completion only said "queued": the settled outcome arrives as exactly one final sender notice.
    assert fleet.until(lambda: len(_notices(fleet, "default", did)) == 1, timeout=15.0)
    assert "REPLY-FROM-researcher" in str(_notices(fleet, "default", did)[0]["content"])


@pytest.mark.platforms("linux")
def test_ac_fleet_f62_h_2(fleet):
    """A target that stays busy past redrive_window_seconds ends the ticket failed with reason queued_expired after at most max_attempts attempts. No row for that message lands afterwards, even once the target is free."""
    fleet.settings(redrive_window_seconds=12)
    with _busy(fleet):
        ack = fleet.send("status? PAYLOAD_H2")
        did = ack["delivery_id"]
        assert fleet.until(lambda: fleet.ticket(did)["state"] in ("failed", "ambiguous"), timeout=90.0)
        t = fleet.ticket(did)
        assert (t["state"], t["reason"]) == ("failed", "queued_expired"), t
        assert 1 <= len(fleet.attempts(did)) <= 6
    fleet.tick(4, pause=1.0)
    time.sleep(2.0)
    assert fleet.rows(TARGET, "PAYLOAD_H2") == []
    assert fleet.ticket(did)["state"] == "failed"
    assert fleet.until(lambda: len(_notices(fleet, "default", did)) == 1, timeout=15.0)
    assert "queued_expired" in str(_notices(fleet, "default", did)[0]["content"])


def _kill_before_row(fleet, body):
    """Send ``body`` and kill its runner and target CLI in the target's pre-row sleep: no row, no outcome."""
    import psutil

    fleet.configure(TARGET, pre=5.0)
    ack = fleet.send(body)
    runner = psutil.Process(fleet.registry.get(ack["process_id"]).pid)
    assert fleet.until(lambda: any("--query-file" in " ".join(c.cmdline()) for c in runner.children(recursive=True)),
                       ticks=False, timeout=10.0)
    fleet.configure(TARGET, pre=0.1)
    for proc in runner.children(recursive=True) + [runner]:
        proc.kill()
    _wait_runner_done(fleet, ack["process_id"])
    return ack


def _append_target_row(fleet, content, *, message_uid=None):
    from hermes_state import SessionDB

    sid, _msgs = _bot_chat(fleet.homes[TARGET])
    db = SessionDB(db_path=fleet.homes[TARGET] / "state.db")
    try:
        return db.append_message(sid, role="user", content=content, message_uid=message_uid)
    finally:
        db.close()


def _shape_not_a_new_acceptance(fleet, shape):
    """A post-baseline compaction clone of an older identical message, or a prefix-only row, never settles an
    attempt; a continuation tip is followed."""
    from hermes_state import SessionDB

    if shape == "continuation_tip":
        home = fleet.homes[TARGET]
        _seed_bot_chat(home, "bc-r-root")
        db = SessionDB(db_path=home / "state.db")
        try:
            db.end_session("bc-r-root", "compression")
            db.create_session("bc-r-tip", source="cli", parent_session_id="bc-r-root")
        finally:
            db.close()
        ack = fleet.send("status? PAYLOAD_H3_TIP")
        assert fleet.until(lambda: fleet.ticket(ack["delivery_id"])["state"] == "settled", timeout=30.0)
        rows = fleet.rows(TARGET, "PAYLOAD_H3_TIP")
        assert len(rows) == 1 and rows[0]["session_id"] == "bc-r-tip", rows
        assert len(fleet.attempts(ack["delivery_id"])) == 1
        return
    body = "status? PAYLOAD_H3_CLONE" if shape == "compaction_clone_after_baseline" else "status? PAYLOAD_H3_PREFIX"
    older = None
    if shape == "compaction_clone_after_baseline":
        older = fleet.send(body)                          # an earlier identical message lands and settles
        assert fleet.until(lambda: fleet.ticket(older["delivery_id"])["state"] == "settled", timeout=30.0)
    ack = _kill_before_row(fleet, body)
    did = ack["delivery_id"]
    if older is not None:
        original = fleet.rows(TARGET, "PAYLOAD_H3_CLONE")[0]
        _append_target_row(fleet, original["content"], message_uid=original["message_uid"])   # fresh id, same uid
    else:
        whole = fleet.ticket(did)["content"]
        _append_target_row(fleet, whole[: len(whole) // 2])                                  # prefix only
    assert fleet.until(lambda: fleet.ticket(did)["state"] == "settled", timeout=60.0)
    assert len(fleet.attempts(did)) == 2, fleet.attempts(did)                                # it was re-driven
    if older is not None:
        rows = fleet.rows(TARGET, "PAYLOAD_H3_CLONE")
        assert len({r["message_uid"] for r in rows}) == 2, rows
        assert fleet.ticket(did)["matched_row_id"] != fleet.ticket(older["delivery_id"])["matched_row_id"]
    else:
        full = [r for r in fleet.rows(TARGET, "PAYLOAD_H3_PREFIX") if fleet.ticket(did)["content"] in r["content"]]
        assert len(full) == 1, full


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("shape", ["alive_runner", "row_then_killed", "orphan_target",
                                   "killed_before_row_then_identical", "late_row_identical_exit0_unpersisted",
                                   "compaction_clone_after_baseline", "continuation_tip", "prefix_only"])
def test_ac_fleet_f62_h_3(fleet, shape):
    """A first attempt that is accepted late is never re-sent, and the message lands exactly once with the ticket settled. The drainer ticks while that attempt's writer is still alive and before its row exists, so the late acceptance and the re-drive decision really overlap (E1 F3). Three cases: the runner is alive at a tick; the row landed and the runner and target were killed; the runner was SIGKILLed while an orphaned target CLI persisted its row later. Two identical-content cases: (d) an attempt killed before its row, observed over before an identical later message lands, is re-driven once, and both tickets settle with two rows; (e) an attempt that persisted its row but lost its outcome, overlapping an identical later attempt that exited 0 without a row, re-sends neither: one row, and both tickets end ambiguous/unknown. Three probe-shape cases: a post-baseline compaction clone of an older identical message (fresh id, same message_uid) and a prefix-only row never count as the killed attempt's row, so it is re-driven once; and a row that lands in a compression continuation tip settles the ticket."""
    import psutil

    if shape in ("compaction_clone_after_baseline", "continuation_tip", "prefix_only"):
        _shape_not_a_new_acceptance(fleet, shape)
        return

    if shape == "killed_before_row_then_identical":
        fleet.configure(TARGET, pre=5.0)
        first = fleet.send("status? PAYLOAD_H3")
        runner = psutil.Process(fleet.registry.get(first["process_id"]).pid)
        assert fleet.until(lambda: any("--query-file" in " ".join(c.cmdline()) for c in runner.children(recursive=True)),
                           ticks=False, timeout=10.0)
        fleet.configure(TARGET, pre=0.1)
        for proc in runner.children(recursive=True) + [runner]:
            proc.kill()                                  # dies in the pre-row sleep: no row, no recorded outcome
        _wait_runner_done(fleet, first["process_id"])
        fleet.tick()                                     # observed over (its window closes) before the second send
        second = fleet.send("status? PAYLOAD_H3")       # identical content, own window
        _wait_runner_done(fleet, second["process_id"])
        assert fleet.until(lambda: all(fleet.ticket(a["delivery_id"])["state"] == "settled" for a in (first, second)),
                           timeout=60.0)
        t1, t2 = fleet.ticket(first["delivery_id"]), fleet.ticket(second["delivery_id"])
        assert len(fleet.rows(TARGET, "PAYLOAD_H3")) == 2 and t1["matched_row_id"] != t2["matched_row_id"], (t1, t2)
        assert len(fleet.attempts(first["delivery_id"])) == 2 and len(fleet.attempts(second["delivery_id"])) == 1
        return
    if shape == "late_row_identical_exit0_unpersisted":
        fleet.settings(redrive_window_seconds=12)
        fleet.configure(TARGET, pre=3.0)
        first = fleet.send("status? PAYLOAD_H3")
        runner = psutil.Process(fleet.registry.get(first["process_id"]).pid)
        assert fleet.until(lambda: any("--query-file" in " ".join(c.cmdline()) for c in runner.children(recursive=True)),
                           ticks=False, timeout=10.0)
        fleet.configure(TARGET, mode="exit0_unpersisted", pre=0.1)
        runner.kill()                                    # orphaned target writes its row later; its outcome is lost
        _wait_runner_done(fleet, first["process_id"])
        second = fleet.send("status? PAYLOAD_H3")       # overlapping window; exits 0 without persisting
        _wait_runner_done(fleet, second["process_id"])
        assert fleet.until(lambda: len(fleet.rows(TARGET, "PAYLOAD_H3")) == 1, ticks=False, timeout=15.0)
        ids = (first["delivery_id"], second["delivery_id"])
        assert fleet.until(lambda: all(fleet.ticket(d)["state"] != "pending" for d in ids), timeout=60.0)
        for d in ids:
            assert (fleet.ticket(d)["state"], fleet.ticket(d)["reason"]) == ("ambiguous", "unknown"), fleet.ticket(d)
            assert len(fleet.attempts(d)) == 1
        fleet.tick(3, pause=0.5)
        assert len(fleet.rows(TARGET, "PAYLOAD_H3")) == 1
        return
    if shape == "alive_runner":
        fleet.configure(TARGET, pre=6.0)
    elif shape == "row_then_killed":
        fleet.configure(TARGET, pre=0.2, mode="hang_after_row")
    else:
        fleet.configure(TARGET, pre=5.0)
    ack = fleet.send("status? PAYLOAD_H3")
    did = ack["delivery_id"]
    runner = psutil.Process(fleet.registry.get(ack["process_id"]).pid)
    if shape == "alive_runner":
        fleet.tick(4, pause=0.5)                        # still in the target's pre-row sleep: alive, not yet seen
        assert _alive(runner.pid) and fleet.rows(TARGET, "PAYLOAD_H3") == []
        assert len(fleet.attempts(did)) == 1, fleet.attempts(did)
    elif shape == "row_then_killed":
        assert fleet.until(lambda: len(fleet.rows(TARGET, "PAYLOAD_H3")) == 1, ticks=False, timeout=20.0)
        for proc in runner.children(recursive=True) + [runner]:
            proc.kill()
        fleet.tick(2, pause=0.5)
    else:
        assert fleet.until(lambda: any("--query-file" in " ".join(c.cmdline()) for c in runner.children(recursive=True)),
                           ticks=False, timeout=10.0)
        runner.kill()                                    # SIGKILL the runner only: the target CLI is orphaned
        fleet.tick(3, pause=0.5)                         # orphan still before its row: no re-drive may start
        assert any(_alive(p) for p in _target_pids(fleet)) and fleet.rows(TARGET, "PAYLOAD_H3") == []
        assert len(fleet.attempts(did)) == 1, fleet.attempts(did)
    assert fleet.until(lambda: fleet.ticket(did)["state"] == "settled", timeout=30.0)
    assert len(fleet.rows(TARGET, "PAYLOAD_H3")) == 1
    assert len(fleet.attempts(did)) == 1


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("case", ["unreadable", "lost_existing_db"])
def test_ac_fleet_f62_h_4(fleet, case):
    """A delivery whose target transcript cannot be read is never re-attempted, and at the bound it ends ambiguous with reason unknown. This covers a corrupt db, and a db that existed at dispatch and then disappeared."""
    fleet.settings(redrive_window_seconds=12)
    _seed_bot_chat(fleet.homes[TARGET], "bc-existing")    # the target db exists before dispatch
    with _busy(fleet):
        ack = fleet.send("status? PAYLOAD_H4")
        did = ack["delivery_id"]
        _wait_runner_done(fleet, ack["process_id"])
        db = fleet.homes[TARGET] / "state.db"
        if case == "unreadable":
            db.write_bytes(b"not a sqlite database" * 64)
        else:
            for leftover in fleet.homes[TARGET].glob("state.db*"):
                leftover.unlink()                          # an observed db that disappeared is unknown, not absent
        assert fleet.until(lambda: fleet.ticket(did)["state"] in ("failed", "ambiguous", "settled"), timeout=90.0)
    t = fleet.ticket(did)
    assert (t["state"], t["reason"]) == ("ambiguous", "unknown"), t
    assert len(fleet.attempts(did)) == 1


_ONESHOT_SENDER = textwrap.dedent('''\
    import json, sys
    sys.path.insert(0, {repo!r})
    sys.path.insert(0, {testdir!r})
    from pathlib import Path
    from hermes_cli import plugins as plugins_mod
    from tools import bot_mode_dm, bot_relay
    bot_relay._hermes_cli = lambda: {fake!r}
    bot_mode_dm._dm_dir = lambda: Path({dm_dir!r})
    import {module} as t
    t.install_runner_shim({shim!r})
    manager = plugins_mod.PluginManager(); manager.discover_and_load(); plugins_mod._plugin_manager = manager
    agent = t._make_sender_agent()
    messages = []
    agent._execute_tool_calls_sequential(t._assistant_turn("researcher", {body!r}, "call-oneshot"), messages, {task!r})
    print([m for m in messages if m.get("role") == "tool"][-1]["content"])
''')


def _oneshot_send(fleet, tmp_path, body, name="oneshot_sender.py"):
    """Run one message_agent send in a separate sender process that exits right after the ack."""
    script = tmp_path / name
    script.write_text(_ONESHOT_SENDER.format(
        repo=str(_REPO), testdir=str(Path(__file__).resolve().parent), module=Path(__file__).stem,
        fake=str(fleet.fake), dm_dir=str(fleet.dm_dir), shim=str(fleet.shim), body=body, task=fleet.task_id),
        encoding="utf-8")
    return subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env={**os.environ, "HERMES_HOME": str(fleet.root)})


def _ack_of(proc):
    out, err = proc.communicate(timeout=180)
    assert proc.returncode == 0, err[-2000:]
    return json.loads(out.strip().splitlines()[-1])


@pytest.mark.platforms("linux")
def test_ac_fleet_f62_h_5(fleet, tmp_path):
    """A one-shot sender that exits while its delivery is pending on a busy target leaves the delivery owned by the gateway drainer. The drainer delivers it once the target is free, and the target's reply reaches the sender's Bot Chat as one delivery notice naming the delivery_id."""
    with _busy(fleet):
        ack = _ack_of(_oneshot_send(fleet, tmp_path, "status? PAYLOAD_H5"))
        did = ack["delivery_id"]
        assert ack.get("redrive", {}).get("tracked") is True, ack
        fleet.tick(3, pause=1.0)                          # the sender process is gone; the target is still busy
        assert fleet.rows(TARGET, "PAYLOAD_H5") == []
        assert fleet.ticket(did)["state"] == "pending"
    assert fleet.until(lambda: fleet.ticket(did)["state"] == "settled", timeout=60.0)
    assert len(fleet.rows(TARGET, "PAYLOAD_H5")) == 1
    assert fleet.until(lambda: len(_notices(fleet, "default", did)) == 1, timeout=15.0)
    note = str(_notices(fleet, "default", did)[0]["content"])
    assert did in note and ("REPLY-FROM-researcher" in note or "ACK researcher" in note), note


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("case", ["both_refused", "identical_overtake", "orphan_overtake"])
def test_ac_fleet_f62_h_6(fleet, case):
    """Two messages from one sender to one target land once each under their own delivery_ids. When both first attempts were refused busy, they land in send order. When the second, identical message overtakes a busy-refused first one, both still land (two rows, two settled tickets). When the first attempt's runner dies while its target CLI is orphaned and a second message lands before the orphan's row, each message still lands exactly once. Under Option Z the later row comes first in the last two cases: the D4 (i) and (iii) exceptions (user question (a) for (iii))."""
    if case == "orphan_overtake":
        import psutil
        from hermes_cli import middleware

        fleet.configure(TARGET, pre=4.0)
        first = fleet.send("status? PAYLOAD_H6_FIRST")
        runner = psutil.Process(fleet.registry.get(first["process_id"]).pid)
        assert fleet.until(lambda: any("--query-file" in " ".join(c.cmdline()) for c in runner.children(recursive=True)),
                           ticks=False, timeout=10.0)
        fleet.configure(TARGET, pre=0.1)                 # the orphan already read its spec: still 4 s before its row
        runner.kill()                                    # releases the runner-held flock; the target CLI lives on
        _wait_runner_done(fleet, first["process_id"])
        second = fleet.send("status? PAYLOAD_H6_SECOND")
        _wait_runner_done(fleet, second["process_id"])
        ids = (first["delivery_id"], second["delivery_id"])
        assert fleet.until(lambda: all(fleet.ticket(d)["state"] == "settled" for d in ids), timeout=60.0)
        one, two = fleet.rows(TARGET, "PAYLOAD_H6_FIRST"), fleet.rows(TARGET, "PAYLOAD_H6_SECOND")
        assert len(one) == 1 and len(two) == 1, (one, two)
        assert all(len(fleet.attempts(d)) == 1 for d in ids)
        strict = "tool_dispatch" in middleware.VALID_MIDDLEWARE          # Option S holds the second send
        assert (one[0]["id"] < two[0]["id"]) is strict, (one, two)
        return
    body_one = "status? PAYLOAD_H6_FIRST"
    body_two = body_one if case == "identical_overtake" else "status? PAYLOAD_H6_SECOND"
    with _busy(fleet):
        first = fleet.send(body_one)
        _wait_runner_done(fleet, first["process_id"])
        if case == "both_refused":
            second = fleet.send(body_two)
            _wait_runner_done(fleet, second["process_id"])
            fleet.tick(2, pause=0.5)
    if case == "identical_overtake":
        second = fleet.send(body_two)                    # target free, drainer not ticked yet: attempt 1 lands now
        _wait_runner_done(fleet, second["process_id"])
        assert len(fleet.rows(TARGET, "PAYLOAD_H6_FIRST")) == 1
    assert first["delivery_id"] != second["delivery_id"], (first, second)
    assert fleet.until(lambda: all(fleet.ticket(a["delivery_id"])["state"] == "settled" for a in (first, second)),
                       timeout=60.0)
    if case == "identical_overtake":
        assert len(fleet.rows(TARGET, "PAYLOAD_H6_FIRST")) == 2
        return
    one, two = fleet.rows(TARGET, "PAYLOAD_H6_FIRST"), fleet.rows(TARGET, "PAYLOAD_H6_SECOND")
    assert len(one) == 1 and len(two) == 1, (one, two)
    assert one[0]["id"] < two[0]["id"]


_GATE_YAML = ("name: f62h-test-gate\nversion: 0.0.1\ndescription: test gate\nmanifest_version: 1\n"
              "provides_hooks: [pre_tool_call]\nprovides_tools: []\n")
_GATE_INIT = textwrap.dedent('''\
    def register(ctx):
        def _gate(tool_name=None, args=None, **kwargs):
            msg = str((args or {}).get("message"))
            if tool_name == "message_agent" and "FORBIDDEN_H7" in msg:
                return {"action": "block", "message": "f62h-test-gate: blocked"}
            if tool_name == "message_agent" and "MODIFY_ME_H7" in msg:
                return {"action": "modify", "args": {"message": "status? MODIFIED_H7"}}
            return None
        ctx.register_hook("pre_tool_call", _gate)
''')


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("case", ["hook_block", "guardrail_block", "hook_modify"])
def test_ac_fleet_f62_h_7(fleet, case, monkeypatch):
    """Every message_agent call, including one sent while an earlier ticket for the pair is still open, goes through core's complete authorization on the real executor. A pre_tool_call block and a guardrail block each yield no ticket and no row. A pre_tool_call modify of the message is what gets ticketed and delivered."""
    from agent.tool_guardrails import ToolGuardrailDecision

    gate = fleet.root / "plugins" / "f62h-test-gate"
    gate.mkdir(parents=True)
    (gate / "plugin.yaml").write_text(_GATE_YAML, encoding="utf-8")
    (gate / "__init__.py").write_text(_GATE_INIT, encoding="utf-8")
    cfg = yaml.safe_load((fleet.root / "config.yaml").read_text(encoding="utf-8"))
    cfg["plugins"]["enabled"].append("f62h-test-gate")
    (fleet.root / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    fleet.load()
    fleet.tick()
    with _busy(fleet):
        first = fleet.send("status? PAYLOAD_H7_FIRST")   # an open ticket for the pair
        assert fleet.ticket(first["delivery_id"])["state"] == "pending"
        if case == "guardrail_block":
            real = fleet.agent._tool_guardrails.before_call
            monkeypatch.setattr(fleet.agent._tool_guardrails, "before_call", lambda name, args: (
                ToolGuardrailDecision(action="block", code="f62h_test", message="f62h guardrail: blocked",
                                      tool_name=name) if "GUARDED_H7" in str((args or {}).get("message"))
                else real(name, args)))
        body = {"hook_block": "status? FORBIDDEN_H7", "guardrail_block": "status? GUARDED_H7",
                "hook_modify": "status? MODIFY_ME_H7"}[case]
        out = fleet.send(body)
        if case != "hook_modify":
            assert "delivery_id" not in out and "blocked" in json.dumps(out), out
        else:
            assert out.get("delivery_id") and out.get("redrive", {}).get("tracked") is True, out
            _wait_runner_done(fleet, out["process_id"])
        _wait_runner_done(fleet, first["process_id"])
        fleet.tick(2, pause=0.5)
    assert fleet.until(lambda: fleet.ticket(first["delivery_id"])["state"] == "settled", timeout=60.0)
    if case == "hook_modify":
        assert fleet.until(lambda: fleet.ticket(out["delivery_id"])["state"] == "settled", timeout=60.0)
        assert len(fleet.rows(TARGET, "MODIFIED_H7")) == 1
        assert fleet.rows(TARGET, "MODIFY_ME_H7") == []
    else:
        fleet.tick(3, pause=0.5)
        assert fleet.rows(TARGET, "FORBIDDEN_H7") == [] and fleet.rows(TARGET, "GUARDED_H7") == []
    assert len(fleet.rows(TARGET, "PAYLOAD_H7_FIRST")) == 1


# --------------------------------------------------------------------------- #
# Live-owner path
# --------------------------------------------------------------------------- #
@pytest.fixture
def live_owner(fleet):
    """The target's Bot Chat is held by a live, delivery-capable owner (core's own lease API)."""
    from hermes_cli.active_sessions import try_acquire_active_session
    from tools import bot_live_delivery as mailbox

    _seed_bot_chat(fleet.homes[TARGET], "bc-live")
    lease, refusal = try_acquire_active_session(
        session_id="bc-live", surface="desktop", config={}, registry_home=fleet.homes[TARGET],
        metadata={"live_session_id": "live-h8", "bot_live_delivery_consumer": True})
    assert refusal is None and lease is not None
    assert mailbox.find_canonical_live_owner(fleet.homes[TARGET]) is not None
    yield lease
    lease.release()


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("case", ["settled_later", "failed_later", "claimed_owner_gone", "no_runner",
                                  "core_cancelled", "owner_gone_expired", "pending_restart",
                                  "runner_died_after_admission"])
def test_ac_fleet_f62_h_8(fleet, live_owner, case, monkeypatch):
    """(R4 condition 3, ruling B1) A live-owner delivery whose core runner ended (or never started) with the receipt still queued or claimed is followed by the plugin past the runner, and its final outcome is reported to the sender and the orchestrator. The plugin NEVER re-sends or re-admits it: no CLI re-drive, no new mailbox admission, no target row from the plugin. Eight cases: (a) the owner settles later: settled, and the reply reaches the sender; (b) it fails later: failed with the receipt's typed reason; (c) it is claimed and the owner goes away: never re-sent, ambiguous/unknown at the bound; (d) an ack with notification_error and no runner is tracked from its receipt; (e) core's own runner cancels the unclaimed receipt (runtime_offline): reported cancelled with that reason, never re-sent; (f) still queued with the owner gone at the bound: the plugin cancels it under the mailbox lock and reports failed/queued_expired, with no row; (g) core's pending completion ("Delivery remains pending …") reached the sender, the gateway restarts, then the receipt settles: exactly one plugin sender notice with the reply (E2 X2); (h) the runner admitted the message and died before printing its outcome: the drainer finds the receipt first and only follows it."""
    from tools import bot_live_delivery as mailbox
    from tools.bot_failure_reasons import RUNTIME_OFFLINE
    from tools.process_registry_notifications import format_process_notification
    import tools.terminal_tool as terminal_module

    home = fleet.homes[TARGET]
    if case == "claimed_owner_gone":
        fleet.settings(redrive_window_seconds=12)
    elif case == "owner_gone_expired":
        fleet.settings(redrive_window_seconds=8)
    elif case == "no_runner":
        monkeypatch.setattr(terminal_module, "terminal_tool",
                            lambda *a, **k: json.dumps({"error": "spawn refused (test)"}))
    elif case in ("pending_restart", "core_cancelled"):
        # Core's runner gives up only after 30 min, and re-checks the owner once per 300 s window; both shortened
        # inside the runner process (read by the shim) so core's own outcome arrives within the test.
        bounds = {"_LIVE_WAIT_SECONDS": 0.2, "_LIVE_WAIT_MAX_SECONDS": 0.5 if case == "pending_restart" else 20.0}
        (fleet.state_dir / "runner.json").write_text(json.dumps(bounds), encoding="utf-8")
    elif case == "runner_died_after_admission":
        # Send with no live owner (so _start_delivery takes the CLI path) and the runner held before core runs; the
        # owner appears afterwards, so only the runner itself can admit the message (tools/bot_mode_dm.py:617-627).
        live_owner.release()
        (fleet.state_dir / "runner.gate").write_text("", encoding="utf-8")
    ack = fleet.send("status? PAYLOAD_H8")
    did = ack["delivery_id"]
    assert ack.get("redrive", {}).get("tracked") is True, ack
    if case == "runner_died_after_admission":
        from hermes_cli.active_sessions import try_acquire_active_session

        assert mailbox.read_delivery_result(home, did) is None and fleet.ticket(did)["path"] == "cli", ack
        back, refusal = try_acquire_active_session(
            session_id="bc-live", surface="desktop", config={}, registry_home=home,
            metadata={"live_session_id": "live-h8-back", "bot_live_delivery_consumer": True})
        assert refusal is None and back is not None
        monkeypatch.setattr(live_owner, "release", back.release)
        (fleet.state_dir / "runner.gate").unlink()
        assert fleet.until(lambda: mailbox.read_delivery_result(home, did) is not None, ticks=False, timeout=30.0)
    assert mailbox.read_delivery_result(home, did)["status"] == "queued", ack

    def owner_claims():
        claim = mailbox.claim_pending_delivery(home, mailbox.find_canonical_live_owner(home))
        assert claim is not None and claim["delivery_id"] == did, claim

    if case == "no_runner":
        assert "process_id" not in ack and ack.get("notification_error"), ack
    elif case == "pending_restart":
        _wait_runner_done(fleet, ack["process_id"])
        time.sleep(0.5)
        assert "Delivery remains pending" in _completion_output(fleet, ack["process_id"])
        pending = _queued_completion(fleet, ack["process_id"])
    elif case == "core_cancelled":
        live_owner.release()                             # the owner vanishes while core's runner still waits
        _wait_runner_done(fleet, ack["process_id"], timeout=30.0)
        out = json.loads(_completion_output(fleet, ack["process_id"]))
        assert (out.get("status"), out.get("reason")) == ("cancelled", RUNTIME_OFFLINE), out
        receipt = mailbox.read_delivery_result(home, did)
        assert (receipt["status"], receipt["reason"]) == ("cancelled", RUNTIME_OFFLINE), receipt
    else:
        if case == "claimed_owner_gone":
            owner_claims()
        # The runner ends without printing an outcome: a stand-in for its 30-min bound, or for a crash right after
        # it admitted the message (case h), which no completion text can tell the drainer about.
        fleet.registry.kill_process(ack["process_id"], consume_output=True)
        _wait_runner_done(fleet, ack["process_id"])
    if case != "core_cancelled":
        fleet.tick(2, pause=0.5)
        assert fleet.ticket(did)["state"] == "pending"

    if case in ("settled_later", "no_runner", "failed_later"):
        owner_claims()
        if case == "failed_later":
            mailbox.complete_delivery(home, did, status="failed", reason="provider_rate_limit", error="429")
        else:
            mailbox.complete_delivery(home, did, status="settled", reply="LIVE-REPLY-H8")
        final = "failed" if case == "failed_later" else "settled"
        assert fleet.until(lambda: fleet.ticket(did)["state"] == final), fleet.ticket(did)
        if final == "failed":
            assert fleet.ticket(did)["reason"] == "provider_rate_limit", fleet.ticket(did)
        assert fleet.until(lambda: len(_notices(fleet, "default", did)) == 1 and len(_notices(fleet, ORCH, did)) == 1)
        if final == "settled":
            assert "LIVE-REPLY-H8" in str(_notices(fleet, "default", did)[0]["content"])
    elif case == "core_cancelled":
        assert fleet.until(lambda: fleet.ticket(did)["state"] == "cancelled"), fleet.ticket(did)
        assert fleet.ticket(did)["reason"] == RUNTIME_OFFLINE, fleet.ticket(did)
        assert fleet.until(lambda: len(_notices(fleet, "default", did)) == 1 and len(_notices(fleet, ORCH, did)) == 1)
    elif case == "pending_restart":
        # Before it went down, the gateway injected core's pending completion into the sender's Bot Chat.
        _append_user_row(fleet.root, SENDER_SID,
                         format_process_notification({"type": "completion", "session_id": ack["process_id"],
                                                      **pending}),
                         display_kind="process_complete", display_metadata={"display_text": "x"})
        _restart_gateway(fleet, monkeypatch)
        owner_claims()
        mailbox.complete_delivery(home, did, status="settled", reply="LATE-REPLY-H8")
        assert fleet.until(lambda: fleet.ticket(did)["state"] == "settled"), fleet.ticket(did)
        assert fleet.until(lambda: len(_notices(fleet, "default", did)) == 1)
        assert "LATE-REPLY-H8" in str(_notices(fleet, "default", did)[0]["content"])
    elif case == "runner_died_after_admission":
        fleet.tick(4, pause=0.5)
        assert fleet.ticket(did)["state"] == "pending" and len(fleet.attempts(did)) == 1, fleet.attempts(did)
        owner_claims()
        mailbox.complete_delivery(home, did, status="settled", reply="LIVE-REPLY-H8")
        assert fleet.until(lambda: fleet.ticket(did)["state"] == "settled"), fleet.ticket(did)
    else:
        live_owner.release()
        assert fleet.until(lambda: fleet.ticket(did)["state"] in ("ambiguous", "failed", "settled", "cancelled"),
                           timeout=60.0)
        receipt = mailbox.read_delivery_result(home, did)
        if case == "claimed_owner_gone":
            assert (fleet.ticket(did)["state"], fleet.ticket(did)["reason"]) == ("ambiguous", "unknown")
            assert receipt["status"] == "claimed", receipt
        else:
            assert (fleet.ticket(did)["state"], fleet.ticket(did)["reason"]) == ("failed", "queued_expired")
            assert (receipt["status"], receipt["reason"]) == ("cancelled", "queued_expired"), receipt
    assert fleet.until(lambda: len(_notices(fleet, "default", did)) == 1 and len(_notices(fleet, ORCH, did)) == 1,
                       timeout=30.0)
    fleet.tick(3, pause=0.5)
    assert len(_notices(fleet, "default", did)) == 1 and len(_notices(fleet, ORCH, did)) == 1
    assert fleet.rows(TARGET, "PAYLOAD_H8") == []
    assert [r["delivery_id"] for r in _admissions(home, "PAYLOAD_H8")] == [did]
    assert len(fleet.attempts(did)) == 1, fleet.attempts(did)


# --------------------------------------------------------------------------- #
# Notices, the busy completion, restart, the idle regression
# --------------------------------------------------------------------------- #
@pytest.mark.platforms("linux")
@pytest.mark.parametrize("case", ["normal", "busy_orchestrator", "orch_no_bot_chat", "restart_after_append"])
def test_ac_fleet_f62_h_9(fleet, case, monkeypatch):
    """A typed final failure reaches the sender's originating session (its Bot Chat tip) and the orchestrator profile's Bot Chat as exactly one process_complete notice row each. Each row names the delivery_id, the target and the closed-vocabulary reason. This holds when the orchestrator is busy, when it has no Bot Chat yet, and when the drainer restarts between the append and its bookkeeping."""
    fleet.settings(redrive_window_seconds=12)
    if case == "orch_no_bot_chat":
        for leftover in fleet.homes[ORCH].glob("state.db*"):
            leftover.unlink()
    orch_lock = _busy(fleet, ORCH) if case == "busy_orchestrator" else None
    if orch_lock is not None:
        orch_lock.__enter__()
    try:
        with _busy(fleet):
            ack = fleet.send("status? PAYLOAD_H9")
            did = ack["delivery_id"]
            if case == "restart_after_append":
                store = fleet.loaded.module.store
                real_mark = store.mark_notice_appended
                monkeypatch.setattr(store, "mark_notice_appended",
                                    lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash before mark")))
            assert fleet.until(lambda: fleet.ticket(did)["state"] == "failed", timeout=90.0)
            fleet.tick(2, pause=0.5)
            if case == "restart_after_append":
                monkeypatch.setattr(store, "mark_notice_appended", real_mark)
                fleet.load()
                fleet.tick(3, pause=0.5)
    finally:
        if orch_lock is not None:
            orch_lock.__exit__(None, None, None)
    assert fleet.until(lambda: len(_notices(fleet, "default", did)) >= 1 and len(_notices(fleet, ORCH, did)) >= 1,
                       timeout=30.0)
    fleet.tick(3, pause=0.5)
    for who in ("default", ORCH):
        notes = _notices(fleet, who, did)
        assert len(notes) == 1, (who, notes)
        text = str(notes[0]["content"])
        assert did in text and TARGET in text and "queued_expired" in text, text


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("drainer", ["live", "stale", "untracked"])
@pytest.mark.parametrize("kind", ["flock_busy", "not_owned"])
def test_ac_fleet_f62_h_10(fleet, kind, drainer):
    """(derived) While tracking is live, the sender's target_busy runner completion, from a held turn lock (TurnBusyError: "retry shortly") or from a Bot Chat open on another surface (SESSION_NOT_OWNED: "Try again later"), is rewritten to status: queued under the same delivery_id. It keeps reason: target_busy, says the message is queued for automatic re-delivery with "do NOT resend", and contains none of "retry shortly", "Try again later" or "safe to resend" (E1 F4). With a stale drainer heartbeat, or with tracking off (no valid orchestrator_profile), the completion output is byte-for-byte the runner's own output (E2 M2)."""
    from contextlib import nullcontext

    from agent.redact import redact_terminal_output
    from tools.process_registry import _completion_output as core_tail

    if drainer == "stale":
        fleet.loaded.module.store.set_heartbeat(time.time() - 3600)
    elif drainer == "untracked":
        fleet.settings(orchestrator_profile=None)
    if kind == "not_owned":
        fleet.configure(TARGET, mode="not_owned")
    with _busy(fleet) if kind == "flock_busy" else nullcontext():
        ack = fleet.send("status? PAYLOAD_H10")
        _wait_runner_done(fleet, ack["process_id"])
        time.sleep(0.5)
        out = _completion_output(fleet, ack["process_id"])
    if drainer == "live":
        assert ack["delivery_id"] in out and "automatic re-delivery" in out and "do NOT resend" in out, out
        payload = json.loads(out)
        assert (payload.get("status"), payload.get("reason"), payload.get("delivery_id")) == (
            "queued", "target_busy", ack["delivery_id"]), payload
        for stale_advice in ("retry shortly", "Try again later", "safe to resend"):
            assert stale_advice not in out, (stale_advice, out)
        return
    session = fleet.registry.get(ack["process_id"])
    assert out == redact_terminal_output(core_tail(session)["output"], session.command), out
    assert "target_busy" in out and "redrive" not in ack, (out, ack)
    assert ("retry shortly" if kind == "flock_busy" else "Try again later") in out, out


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("case", ["restart", "crash_after_launching", "crash_after_spawn"])
def test_ac_fleet_f62_h_11(fleet, case, monkeypatch):
    """A delivery that is mid re-drive survives a gateway restart (registry kill_all() plus a freshly loaded plugin manager) and then lands exactly once and ends settled. This also holds when the drainer crashed after committing an attempt launching, both before the spawn and after the spawn but before the running commit."""
    with _busy(fleet):
        ack = fleet.send("status? PAYLOAD_H11")
        did = ack["delivery_id"]
        _wait_runner_done(fleet, ack["process_id"])
        if case == "restart":
            fleet.tick(2, pause=1.5)
        else:
            drain, store = fleet.loaded.module.drain, fleet.loaded.module.store
            with monkeypatch.context() as crash:
                if case == "crash_after_launching":
                    crash.setattr(drain, "spawn_attempt",
                                  lambda *a, **k: (_ for _ in ()).throw(SystemExit("crash before spawn")))
                else:
                    crash.setattr(store, "commit_running",
                                  lambda *a, **k: (_ for _ in ()).throw(SystemExit("crash after spawn")))
                # invoke_hook swallows the callback's SystemExit (hermes_cli/plugins_dispatch.py:241): the crash
                # leaves the committed attempt behind exactly as a dead drainer would.
                assert fleet.until(lambda: bool(fleet.attempts(did)) and len(fleet.attempts(did)) >= 2
                                   and fleet.attempts(did)[-1]["state"] == "launching", timeout=8.0)
        fleet.registry.kill_all(fleet.task_id, source="gateway_shutdown")   # every registry spawn here is the test's
        fleet.load()
        assert fleet.ticket(did)["state"] == "pending"
    assert fleet.until(lambda: fleet.ticket(did)["state"] == "settled", timeout=60.0)
    assert len(fleet.rows(TARGET, "PAYLOAD_H11")) == 1
    assert _attempt_ids(fleet, did) == {did}


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("case", ["idle", "settle_during_runner", "transform_missed", "publish_lost_after_persist",
                                  "detached_pruned"])
def test_ac_fleet_f62_h_12(fleet, case, monkeypatch, request):
    """(regression for idle, settle_during_runner and transform_missed; publish_lost_after_persist and detached_pruned need the plugin's (b′) notice) A delivery to an idle target behaves as on 0.22: the ack keeps core's status, delivery_id, process_id and reply_delivery values, the reply completion text is unchanged, the message lands once, and no notice is written. One sender notice, never two (E1 F5): no plugin notice when a live receipt settles while core's runner is completing and a drainer tick lands between core's exited mark and its output transform, or when the plugin's transform records nothing. When core's publication fails after its result write, no plugin notice is written in-process (the documented residual), and exactly one after a gateway restart. When a crash leaves the attempt-1 runner to be recovered as a detached session that core later prunes (_completion_event set, no completion queued), exactly one plugin sender notice is written on that restarted gateway."""
    import re
    import threading

    from tools import bot_live_delivery as mailbox
    from tools import process_registry as pr_mod

    home = fleet.homes[TARGET]
    real_redact = pr_mod._redact_process_result
    if case == "settle_during_runner":
        request.getfixturevalue("live_owner")
    elif case == "transform_missed":
        # The plugin's transform records nothing for this runner (timed out / skipped); core still publishes the
        # completion, and the one-notice rule keys on _completion_event, not on the transform.
        fleet.settings(heartbeat_stale_seconds=2)
        monkeypatch.setattr(fleet.loaded.module.store, "record_attempt_outcome", lambda *a, **k: None)
    elif case == "publish_lost_after_persist":
        # Core persists the runner's result, then its publication raises before completion_queue.put. Patched before
        # the send because an idle target's runner can finish before the send returns.
        def lost(result):
            if (isinstance(result, dict) and result.get("type") == "completion"
                    and "--run-delivery" in str(result.get("command") or "")):
                raise RuntimeError("gateway died before publishing")
            return real_redact(result)
        monkeypatch.setattr(pr_mod, "_redact_process_result", lost)
    elif case == "detached_pruned":
        fleet.configure(TARGET, pre=4.0)                 # the runner must still be alive when the gateway crashes
    ack = fleet.send("status? PAYLOAD_H12")
    did, pid = ack["delivery_id"], ack["process_id"]
    assert ack.get("status") == "queued" and ack.get("reply_delivery") == "notification", ack
    assert re.fullmatch(r"[0-9a-f]{64}", did or ""), ack
    session = fleet.registry.get(pid)
    assert session is not None and "--run-delivery" in session.command
    if case == "settle_during_runner":
        # Core marks the session exited before it publishes the completion and runs the output transform
        # (tools/process_registry.py:1586-1639). Pause the completion inside that window and tick there.
        in_window, release = threading.Event(), threading.Event()

        def paused(result):
            if isinstance(result, dict) and result.get("session_id") == pid:
                in_window.set()
                release.wait(timeout=30)
            return real_redact(result)
        monkeypatch.setattr(pr_mod, "_redact_process_result", paused)
        try:
            claim = mailbox.claim_pending_delivery(home, mailbox.find_canonical_live_owner(home))
            assert claim is not None and claim["delivery_id"] == did, claim
            mailbox.complete_delivery(home, did, status="settled", reply="REPLY-FROM-researcher")
            assert in_window.wait(timeout=40), "the runner's completion never reached the transform"
            assert session.exited
            fleet.tick(3, pause=0.3)                     # exited + settled receipt, transform not yet run
            assert _notices(fleet, "default", did) == [], "exited alone must not authorize a sender notice"
        finally:
            release.set()
        assert fleet.until(lambda: session._completion_event.is_set(), ticks=False, timeout=30.0)
        assert _notices(fleet, "default", did) == []
        assert len(_admissions(home, "PAYLOAD_H12")) == 1
    elif case == "publish_lost_after_persist":
        _wait_runner_done(fleet, pid, timeout=40.0)
        assert fleet.until(lambda: len(fleet.rows(TARGET, "PAYLOAD_H12")) == 1, ticks=False, timeout=10.0)
        assert fleet.until(session._completion_event.is_set, ticks=False, timeout=10.0)   # set even when publish raised
        assert (fleet.root / "logs" / "process-results" / f"{pid}.json").exists()
        fleet.tick(3, pause=0.5)
        assert _notices(fleet, "default", did) == []     # the documented in-process residual
        while not fleet.registry.completion_queue.empty():
            fleet.registry.completion_queue.get_nowait()
        _restart_gateway(fleet, monkeypatch)
        assert fleet.until(lambda: len(_notices(fleet, "default", did)) == 1, timeout=30.0)
        fleet.tick(3, pause=0.5)
        notes = _notices(fleet, "default", did)
        assert len(notes) == 1 and "REPLY-FROM-researcher" in str(notes[0]["content"]), notes
        assert len(fleet.rows(TARGET, "PAYLOAD_H12")) == 1
        return
    elif case == "detached_pruned":
        # The next gateway process recovers the live runner from the checkpoint as a detached session with no pipe.
        # Once its PID is gone core prunes it, sets _completion_event and queues no completion
        # (tools/process_registry.py:1057-1072): only the plugin can tell the sender.
        assert _restart_gateway(fleet, monkeypatch, crash=True) == 1
        recovered = fleet.registry._running.get(pid)
        assert recovered is not None and recovered.detached and not recovered._completion_event.is_set()
        assert fleet.until(lambda: fleet.registry.get(pid) is None, ticks=False, timeout=40.0)
        assert recovered._completion_event.is_set()
        assert fleet.registry.completion_queue.empty()
        assert fleet.until(lambda: len(_notices(fleet, "default", did)) == 1, timeout=30.0)
        fleet.tick(3, pause=0.5)
        notes = _notices(fleet, "default", did)
        assert len(notes) == 1 and did in str(notes[0]["content"]), notes
        assert len(fleet.rows(TARGET, "PAYLOAD_H12")) == 1
        return
    else:
        _wait_runner_done(fleet, pid, timeout=40.0)
        time.sleep(0.5)
        if case == "transform_missed":
            assert (fleet.root / "logs" / "process-results" / f"{pid}.json").exists()
            fleet.tick(4, pause=1.0)                     # well past heartbeat_stale_seconds
        assert len(fleet.rows(TARGET, "PAYLOAD_H12")) == 1
    assert "REPLY-FROM-researcher" in _completion_output(fleet, pid)
    fleet.tick(3, pause=0.5)
    assert _notices(fleet, "default", did) == []
    assert _notices(fleet, ORCH, did) == []


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("case", ["cli_hung_writer", "live_claimed"])
def test_ac_fleet_f62_h_13(fleet, case, request):
    """(derived) At the bound, a ticket whose attempt may still land is never reported queued_expired. A still-running CLI writer, or a live receipt still claimed, ends ambiguous/unknown with its notices, and the drainer starts no re-drive for a later ticket of the pair until that attempt resolves. The ticket survives a retention interval while that attempt is open, and when it resolves, one late-outcome notice follows."""
    from tools import bot_live_delivery as mailbox

    fleet.settings(redrive_window_seconds=10, retention_seconds=1)
    home = fleet.homes[TARGET]
    if case == "cli_hung_writer":
        fleet.configure(TARGET, pre=15.0)               # attempt 1 is a live writer across the whole bound
        first = fleet.send("status? PAYLOAD_H13_FIRST")
        assert fleet.until(lambda: fleet.pidlog.exists(), ticks=False, timeout=10.0)
        fleet.configure(TARGET, pre=0.1)                # the fake reads its spec at start: later turns are quick
    else:
        lease = request.getfixturevalue("live_owner")
        first = fleet.send("status? PAYLOAD_H13_FIRST")
        assert mailbox.claim_pending_delivery(home, mailbox.find_canonical_live_owner(home)) is not None
    did = first["delivery_id"]
    assert fleet.until(lambda: fleet.ticket(did)["state"] != "pending", timeout=40.0)
    t = fleet.ticket(did)
    assert (t["state"], t["reason"]) == ("ambiguous", "unknown"), t
    assert fleet.until(lambda: len(_notices(fleet, "default", did)) == 1 and len(_notices(fleet, ORCH, did)) == 1)
    time.sleep(2.0)
    fleet.tick(2, pause=0.5)                             # past retention: an open late attempt keeps the ticket
    assert fleet.ticket(did) is not None and fleet.ticket(did)["late_open"] == 1
    fleet.settings(retention_seconds=3600)               # the rest asserts post-resolution rows, not GC
    if case == "live_claimed":
        lease.release()
    second = fleet.send("status? PAYLOAD_H13_SECOND")
    fleet.tick(3, pause=0.5)
    if case == "cli_hung_writer":
        assert len(fleet.attempts(second["delivery_id"])) == 1   # core's own first attempt only: no re-drive yet
        assert fleet.until(lambda: len(fleet.rows(TARGET, "PAYLOAD_H13_FIRST")) == 1, timeout=30.0)
    else:
        mailbox.complete_delivery(home, did, status="settled", reply="LATE-H13")
    assert fleet.until(lambda: len(_notices(fleet, "default", did)) == 2, timeout=30.0)   # one late-outcome notice
    assert fleet.ticket(did)["late_open"] == 0
    assert fleet.until(lambda: fleet.ticket(second["delivery_id"])["state"] == "settled", timeout=60.0)
    if case == "cli_hung_writer":
        one, two = fleet.rows(TARGET, "PAYLOAD_H13_FIRST"), fleet.rows(TARGET, "PAYLOAD_H13_SECOND")
        assert len(one) == 1 and len(two) == 1 and one[0]["id"] < two[0]["id"], (one, two)


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("case", ["unset", "unknown_profile"])
def test_ac_fleet_f62_h_14(fleet, case, monkeypatch):
    """(derived) Without a valid orchestrator_profile the plugin tracks nothing: the message_agent tool result is byte-for-byte core's (no redrive key), the runner's target_busy completion is core's own text, and nothing is re-driven."""
    raw = []
    real_start = fleet.bot_mode_dm._start_delivery
    monkeypatch.setattr(fleet.bot_mode_dm, "_start_delivery",
                        lambda *a, **k: raw.append(real_start(*a, **k)) or raw[-1])
    fleet.settings(orchestrator_profile=None if case == "unset" else "no-such-profile")
    with _busy(fleet):
        raw_out = fleet.send("status? PAYLOAD_H14", raw=True)
        assert raw and raw_out == raw[-1], (raw_out, raw)
        ack = json.loads(raw_out)
        assert ack.get("status") == "queued" and "redrive" not in ack, ack
        _wait_runner_done(fleet, ack["process_id"])
        time.sleep(0.5)
        assert "retry shortly" in _completion_output(fleet, ack["process_id"])
    fleet.tick(4, pause=0.5)
    assert fleet.ticket(ack["delivery_id"]) is None
    assert fleet.rows(TARGET, "PAYLOAD_H14") == []


# AC-FLEET-F62.h-15 is WITHDRAWN by user ruling A1 (Option Z; Option S rejected): no test def, id kept.
