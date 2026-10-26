"""The FLEET-F62.h fleet harness: a Bot-Mode-managed root with the plugin loaded through real discovery, a fake target
CLI, the delivery-runner shim, and the db, process and completion helpers the acceptance tests and their siblings share.

Its fixtures are registered by name as ``fleet`` and ``live_owner``: a module imports ``fleet_fixture`` /
``live_owner_fixture`` and its tests take ``fleet`` / ``live_owner``.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
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
# Every wait below is a ceiling, never a window: under load (>= 6.5x measured) one runner boot can use most of 30 s.
_WAIT_FLOOR = 180.0

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
    if spec.get("hold") and os.path.exists(spec["hold"]):
        open(spec["hold"] + ".in", "w", encoding="utf-8").close()
        while os.path.exists(spec["hold"]):
            time.sleep(0.05)
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
    """(session id, every message) of the Bot Chat lineage the CLI resolves in ``home``. A db the fake CLI has created
    but not yet given its schema reads as empty; every other db error still fails the test."""
    from hermes_state import SessionDB

    if not (Path(home) / "state.db").exists():
        return None, []
    try:
        db = SessionDB(db_path=Path(home) / "state.db", read_only=True)
        try:
            row = db.get_session_by_title("Bot Chat")
            if row is None:
                return None, []
            sid = db.get_compression_tip(row["id"]) or row["id"]
            return sid, db.get_messages(sid, include_inactive=True, include_ancestors=True)
        finally:
            db.close()
    except sqlite3.OperationalError as exc:
        if not str(exc).startswith("no such table"):
            raise
        return None, []


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
        for key, value in json.loads(overrides.read_text(encoding="utf-8-sig")).items():
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


@pytest.fixture(name="fleet")
def fleet_fixture(tmp_path, monkeypatch):
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
    # The rig ticks by hand and boots runners under load, so neither a tick-free wait (D8 staleness) nor a re-drive
    # chain (the window) may outrun these; the cases that test either bound set their own.
    settings = {"redrive_window_seconds": 300, "max_attempts": 6, "retry_backoff_seconds": 1,
                "heartbeat_stale_seconds": 600, "orchestrator_profile": ORCH}
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
        data = json.loads(target_cfg.read_text(encoding="utf-8-sig"))
        data[profile] = spec
        target_cfg.write_text(json.dumps(data), encoding="utf-8")

    def settings_(**overrides):
        for home in homes.values():
            data = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8-sig"))
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
                # health: allow HX013 -- the pause between drainer ticks is the scenario's elapsed time (windows, staleness and retention expire across ticks)
                time.sleep(pause)

    def rows(home_name, needle):
        _sid, msgs = _bot_chat(homes[home_name])
        return [m for m in msgs if m.get("role") == "user" and needle in str(m.get("content") or "")
                and m.get("display_kind") != "process_complete"]

    def until(predicate, *, ticks=True, timeout=30.0, pause=0.25):
        deadline = time.monotonic() + max(timeout, _WAIT_FLOOR)
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
    return [int(json.loads(line)["pid"]) for line in fleet.pidlog.read_text(encoding="utf-8-sig").splitlines()]


def _pidlog_profiles(fleet):
    """Every profile a fake target CLI logged, in start order (written once it has read its spec)."""
    if not fleet.pidlog.exists():
        return []
    return [json.loads(line)["profile"] for line in fleet.pidlog.read_text(encoding="utf-8-sig").splitlines()]


def _spec_read(fleet, started):
    """Whether a target CLI sent after ``started`` TARGET pidlog lines has read its spec. A reconfigure is safe only then:
    the CLI's process (``_has_query_child``) exists before its ``json.load``, so an earlier reconfigure can reach it."""
    return _pidlog_profiles(fleet).count(TARGET) > started


def _alive(pid):
    import psutil

    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def _has_query_child(runner):
    """Whether the runner has started its target CLI. A process that exits mid-scan, reaped or not, is skipped: psutil
    raises NoSuchProcess (ZombieProcess is one) for it, and that means "not this one", never a failed wait."""
    import psutil

    try:
        children = runner.children(recursive=True)
    except psutil.NoSuchProcess:
        return False
    for child in children:
        try:
            if "--query-file" in " ".join(child.cmdline()):
                return True
        except psutil.NoSuchProcess:
            continue
    return False


def _kill(runner, *, tree=True):
    """SIGKILL the runner (after its descendants, with ``tree``). A process that has already exited is as good as
    killed; any other error still raises."""
    import psutil

    procs = [runner]
    if tree:
        try:
            procs = runner.children(recursive=True) + procs
        except psutil.NoSuchProcess:
            pass
    for proc in procs:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass


def _busy(fleet, profile=TARGET):
    """Hold ``profile``'s real cross-process turn lock (core's flock), as a long target turn does."""
    return fleet.bot_relay.acquire_turn_lock(fleet.root, profile, timeout_seconds=0)


def _wait_runner_done(fleet, process_id, timeout=30.0):
    timeout = max(timeout, _WAIT_FLOOR)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session = fleet.registry.get(process_id)
        if session is None or session.exited:
            return
        time.sleep(0.1)
    raise AssertionError(f"runner {process_id} still running after {timeout}s")


def _wait_completion(fleet, process_id, timeout=30.0):
    """Core sets ``_completion_event`` only once the runner's completion notice is queued, or its publish failed
    (tools/process_registry.py:1610-1616)."""
    assert fleet.until(lambda: (s := fleet.registry.get(process_id)) is None or s._completion_event.is_set(),
                       ticks=False, timeout=timeout)


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
            record = json.loads(path.read_text(encoding="utf-8-sig"))
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


@pytest.fixture(name="live_owner")
def live_owner_fixture(fleet):
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
