"""The FLEET-F62.h fleet harness at v0.22.5: a Bot-Mode-managed root with the plugin loaded through real discovery, an
authority stub at core's own test seam, the delivery-runner shim, and the db, process and completion helpers the
acceptance tests and their siblings share.

Its fixture is registered by name as ``fleet``: a module imports ``fleet_fixture`` and its tests take ``fleet``.

A local ``message_agent`` DM at v0.22.5 is admitted by the target profile's session authority (a durable FIFO) over its
``bot_relay.deliver`` RPC. The stub stands in for that RPC exactly where core's own tests do
(``tests/tools/test_bot_live_owner_delivery.py``: ``authority_delivery`` plus a ready ``discover_gateway_endpoint``),
keeping its whole state in the mailbox receipt files, so the test process and core's runner processes (which install
it too, through the shim) see one authority. It never writes a transcript row unless a test asks it to.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
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
TARGET_SID = "bc-live"
# Every wait below is a ceiling, never a window: under load (>= 6.5x measured) one runner boot can use most of 30 s.
_WAIT_FLOOR = 180.0


def _profile(root, name):
    home = root / "profiles" / name
    home.mkdir(parents=True, exist_ok=True)
    (home / "profile.yaml").write_text(
        "description: teammate\nui_meta:\n  hermes-bots:\n    shape: cloud\n", encoding="utf-8")
    return home


def _bot_chat(home):
    """(session id, every message) of the Bot Chat lineage in ``home``. A db without its schema yet reads as empty;
    every other db error still fails the test."""
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
        return db.append_message(session_id, role="user", content=content, **fields)
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


# --------------------------------------------------------------------------- #
# The authority stub (core's T test seam)
# --------------------------------------------------------------------------- #
_AUTHORITY_CFG = "authority.json"
_AUTHORITY_LOG = "authority-calls.jsonl"


def _profile_of(home) -> str:
    home = Path(home).resolve()
    return home.name if home.parent.name == "profiles" else "default"


def _authority_cfg(state_dir) -> dict:
    path = Path(state_dir) / _AUTHORITY_CFG
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}


def _log_call(state_dir, method, home, params):
    with open(Path(state_dir) / _AUTHORITY_LOG, "a", encoding="utf-8") as log:
        log.write(json.dumps({"method": method, "profile": _profile_of(home), "id": params.get("id"),
                              "pid": os.getpid()}) + "\n")


def _answer(record):
    return {k: record[k] for k in ("status", "delivery_id", "profile_home", "session_id", "reply", "refused", "reason")
            if k in record}


def _stub_deliver(state_dir, home, params):
    """``bot_relay.deliver``: a same-id replay returns the existing admission; a changed payload is a conflict.
    ``authority.json`` ``modes[<profile>]`` picks the answer to a new id: ``admit`` (default), ``legacy`` (a queued
    record with no ``admission_id``), ``held`` (the session-writer refusal), ``refuse:<reason>`` (an owner-state
    refusal), ``terminal:<status>:<reason>`` (a receipt already ended)."""
    from hermes_cli.gateway_client import GatewayRPCError
    from tools import bot_live_delivery as mailbox
    from tools.bot_failure_reasons import HELD_NOT_SENT

    _log_call(state_dir, "deliver", home, params)
    mode = _authority_cfg(state_dir).get("modes", {}).get(_profile_of(home), "admit")
    with mailbox._locked(home) as root:
        path = root / f"{params['id']}.json"
        record = mailbox._read(path)
        if record is not None:
            if record["message"] != params["message"] or record.get("author") != params.get("author"):
                raise GatewayRPCError("admission_conflict")
            return _answer(record)
        if mode == "held":
            raise GatewayRPCError(HELD_NOT_SENT, data={"admitted": False})
        if mode.startswith("refuse:"):
            raise GatewayRPCError(mode.split(":", 1)[1])
        record = dict(status="queued", delivery_id=params["id"], profile_home=str(Path(home).resolve()),
                      session_id=params.get("session_id") or TARGET_SID, message=params["message"], reply="",
                      **({"author": params["author"]} if "author" in params else {}))
        if mode != "legacy":
            record["admission_id"] = "adm-" + params["id"][:6]
        if mode.startswith("terminal:"):
            _, status, reason = mode.split(":", 2)
            record.update(status=status, reason=reason, error=f"stub receipt {status} ({reason})",
                          completed_at=time.time_ns())
        mailbox._write(path, record)
    return _answer(record)


def _stub_settle(state_dir, home, params):
    """``bot_relay.settle``: the never-admitted id's terminal not-sent receipt (``gateway/session_bot.py``
    ``_not_sent_record``), or the existing one."""
    from tools import bot_live_delivery as mailbox
    from tools.bot_failure_reasons import HELD_NOT_SENT

    _log_call(state_dir, "settle", home, params)
    with mailbox._locked(home) as root:
        path = root / f"{params['id']}.json"
        record = mailbox._read(path)
        if record is None:
            record = dict(delivery_id=params["id"], profile_home=str(Path(home).resolve()), session_id=TARGET_SID,
                          message=params["message"], status="failed", refused=True, reply="",
                          error="Settled as not sent after a session-writer refusal", reason=HELD_NOT_SENT,
                          completed_at=time.time_ns())
            mailbox._write(path, record)
    return dict(record)


def install_authority(state_dir, setattr_=setattr):
    """Every profile named in ``authority.json`` ``ready`` (default: the target and the orchestrator) has a ready
    authority; every other home keeps core's real discovery."""
    import hermes_cli.gateway_runtime as runtime
    from tools import bot_live_delivery as mailbox

    real = runtime.discover_gateway_endpoint

    def discover(profile_home, *, timeout=2.0):
        if _profile_of(profile_home) not in _authority_cfg(state_dir).get("ready", [TARGET, ORCH]):
            return real(profile_home, timeout=timeout)
        return SimpleNamespace(state="ready", endpoint=SimpleNamespace(instance_id="authority-1"))

    setattr_(runtime, "discover_gateway_endpoint", discover)
    setattr_(mailbox, "authority_delivery", lambda home, params: _stub_deliver(state_dir, home, params))
    setattr_(mailbox, "authority_settle", lambda home, params: _stub_settle(state_dir, home, params))


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
    from tests.plugins.fleet_f62_h_rig import install_authority
    install_authority({state_dir!r})
    from tools import bot_mode_dm
    overrides = Path({state_dir!r}) / "runner.json"
    if overrides.exists():
        for key, value in json.loads(overrides.read_text(encoding="utf-8-sig")).items():
            setattr(bot_mode_dm, key, value)
    raise SystemExit(bot_mode_dm._delivery_main(sys.argv[1:]))
''')


def install_runner_shim(shim_path, setattr_=setattr):
    """Core's delivery runner is a separate process: start it through a shim that installs the authority stub, can
    shorten core's live-wait bounds (``runner.json``) and holds it before core's code runs while ``runner.gate``
    exists. The real ``terminal_tool`` / process registry / completion path still runs it, and the runner is core's own
    ``_wait_live_dm``."""
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
    """A Bot-Mode-managed root: sender = default ('hermes'), target 'researcher', orchestrator; plugin enabled; the
    target's and the orchestrator's authorities are the stub."""
    from hermes_cli import plugins as plugins_mod
    from tools import bot_live_delivery as mailbox
    from tools import bot_mode_dm, bot_mode_probe
    from tools.process_registry import process_registry

    assert PLUGIN_SRC.is_dir(), "plugin source missing"
    bot_mode_probe._reset_cache_for_tests()
    root = tmp_path / ".hermes"
    root.mkdir()
    (root / "profile.yaml").write_text(
        "description: sender\nui_meta:\n  hermes-bots:\n    shape: cloud\n", encoding="utf-8")
    homes = {"default": root, TARGET: _profile(root, TARGET), ORCH: _profile(root, ORCH)}
    # The rig ticks by hand and boots runners under load, so a tick-free wait (D8 staleness) and the follow bound must
    # outlast both; the cases that test either bound set their own.
    settings = {"redrive_window_seconds": 300, "heartbeat_stale_seconds": 600, "orchestrator_profile": ORCH}
    cfg = {"plugins": {"enabled": [PLUGIN_KEY], "entries": {PLUGIN_KEY: {"settings": settings}}}}
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
    state_dir = tmp_path / "runner-state"
    state_dir.mkdir()
    install_authority(state_dir, setattr_=monkeypatch.setattr)
    shim = tmp_path / "runner_shim.py"
    shim.write_text(_RUNNER_SHIM.format(repo=str(_REPO), state_dir=str(state_dir)), encoding="utf-8")
    install_runner_shim(shim, setattr_=monkeypatch.setattr)
    _seed_bot_chat(root, SENDER_SID)
    _seed_bot_chat(homes[TARGET], TARGET_SID)            # the owner's Bot Chat: find_canonical_live_owner needs a store
    _seed_bot_chat(homes[ORCH], "bc-orchestrator")
    task_id = f"f62h-{os.urandom(8).hex()}"              # owns every registry spawn of this test (owner_task_id)

    ns = SimpleNamespace(root=root, homes=homes, dm_dir=dm_dir, state_dir=state_dir, shim=shim,
                         registry=process_registry, task_id=task_id, bot_mode_dm=bot_mode_dm, agent=None,
                         manager=None, loaded=None)

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

    def settings_(**overrides):
        for home in homes.values():
            data = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8-sig"))
            data["plugins"]["entries"][PLUGIN_KEY]["settings"].update(overrides)
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

    ns.load, ns.settings, ns.send, ns.tick, ns.rows, ns.until = load, settings_, send, tick, rows, until
    ns.ticket = lambda did: ns.loaded.module.store.ticket(did)
    ns.attempts = lambda did: ns.loaded.module.store.attempts(did)
    ns.authority = _authority_helpers(state_dir, homes, mailbox)
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


def _authority_helpers(state_dir, homes, mailbox):
    """The owner's side of a stubbed authority: what a profile's FIFO does with an admitted delivery."""
    cfg_path = Path(state_dir) / _AUTHORITY_CFG

    def configure(**changes):
        data = _authority_cfg(state_dir)
        data.update(changes)
        cfg_path.write_text(json.dumps(data), encoding="utf-8")

    def mode(value, profile=TARGET):
        data = _authority_cfg(state_dir)
        data.setdefault("modes", {})[profile] = value
        cfg_path.write_text(json.dumps(data), encoding="utf-8")

    def receipt(did, profile=TARGET):
        return mailbox.read_delivery_result(homes[profile], did)

    def claim(did, profile=TARGET):
        with mailbox._locked(homes[profile]) as root:
            path = root / f"{did}.json"
            record = mailbox._read(path)
            assert record is not None and record["status"] == "queued", record
            record.update(status="claimed", claimed_at=time.time_ns())
            mailbox._write(path, record)
        return record

    def row(did, profile=TARGET):
        """The owner's turn persisted the delivery's role='user' row, as a persisting owner does before it settles."""
        record = receipt(did, profile)
        assert record is not None and record.get("message"), record
        sid, _msgs = _bot_chat(homes[profile])
        return _append_user_row(homes[profile], sid, record["message"])

    def settle(did, reply="LIVE-REPLY", *, row_first=True, profile=TARGET):
        if (receipt(did, profile) or {}).get("status") == "queued":
            claim(did, profile)
        if row_first:
            row(did, profile)
        return mailbox.complete_delivery(homes[profile], did, status="settled", reply=reply)

    def fail(did, reason, error="", *, status="failed", profile=TARGET):
        if (receipt(did, profile) or {}).get("status") == "queued":
            claim(did, profile)
        return mailbox.complete_delivery(homes[profile], did, status=status, reason=reason, error=error)

    def calls(method="deliver", did=None):
        path = Path(state_dir) / _AUTHORITY_LOG
        found = [json.loads(ln) for ln in path.read_text(encoding="utf-8-sig").splitlines()] if path.exists() else []
        return [c for c in found if c["method"] == method and (did is None or c["id"] == did)]

    def tool_admissions(did):
        """``bot_relay.deliver`` calls for ``did`` from this (the gateway) process: the tool's own admission. The
        plugin runs in this process too, so a plugin re-send would add one; core's runner replays from its own pid."""
        return [c for c in calls(did=did) if c["pid"] == os.getpid()]

    return SimpleNamespace(configure=configure, mode=mode, receipt=receipt, claim=claim, row=row, settle=settle,
                           fail=fail, calls=calls, tool_admissions=tool_admissions)


def _kill_own_processes(tmp_path):
    """Stop every process this test started, and only those. Each one names the test's own pytest ``tmp_path`` in its
    argv (shim-started registry runners, the one-shot sender); nothing else can. Rescans, because a runner can start a
    child mid-sweep. Polls instead of reaping: the registry owns its Popens."""
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


def _alive(pid):
    import psutil

    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
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
    """Hold ``profile``'s real cross-process turn lock (core's flock), as a long turn of that profile does."""
    from tools.bot_relay import acquire_turn_lock

    return acquire_turn_lock(fleet.root, profile, timeout_seconds=0)


def _runner_sessions(fleet):
    """Every delivery runner this test's registry ever spawned: core's, one per admitted send (the plugin spawns none)."""
    return [s for s in fleet.registry.list_sessions(task_id=fleet.task_id) if "--run-delivery" in str(s.get("command"))]


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
