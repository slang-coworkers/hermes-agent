"""message_agent returns "sent" only once the target's Bot Chat holds the message.

Drives the real ``message_agent_tool`` -> real ``terminal_tool(background=True,
_host_local=True)`` -> real delivery runner -> a FAKE target ``hermes`` executable
(``bot_relay._hermes_cli`` is pointed at it). The fake target writes its inbound
row into the target profile's Bot Chat with the real ``SessionDB`` the way the
CLI's turn-start persistence does, then replies on stdout.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# fixtures — real message_agent -> real background runner -> fake target `hermes`
# --------------------------------------------------------------------------- #
_FAKE_TARGET = textwrap.dedent('''\
    #!{python}
    """Fake target `hermes -p <profile> chat ... -Q --query-file <f>` for the delivery-ack tests."""
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
# message_agent acceptance (real processes; POSIX process tree + flock)
# --------------------------------------------------------------------------- #
def _no_payload(fleet):
    return not any("PAYLOAD_F62G" in (m.get("content") or "") for m in fleet.transcript())


@pytest.mark.linux_only
def test_target_busy_is_an_inline_error(fleet):
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
def test_exit_before_acceptance_is_an_inline_error(fleet, how):
    """A local delivery whose target exits before accepting the message (non-zero, or zero without persisting it), or whose runner is killed before acceptance, returns an error (no status sent), queues no autonomous completion, and the target transcript holds no copy of the message."""
    import threading
    if how == "target_exits":
        fleet.configure(mode="exit3")
    elif how == "target_exit0_unpersisted":
        fleet.configure(mode="exit0_unpersisted")
    else:
        # The target would accept only after the kill window, however slow the host.
        fleet.configure(mode="accept", pre=30.0)

        def _kill_runner():
            deadline = time.monotonic() + 25
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
    if how == "runner_killed":
        assert not _pid_alive(int(fleet.pidfile.read_text(encoding="utf-8"))), "the tree-kill must reach the target"
    if how == "target_exit0_unpersisted":
        assert out.get("reason") == "delivery_unconfirmed", out
    _no_autonomous_completion(fleet.registry, _delivery_sessions(fleet.registry)[-1]["session_id"])
    time.sleep(1.0)
    assert _no_payload(fleet)


@pytest.mark.linux_only
def test_unaccepted_delivery_times_out_and_never_lands(fleet, monkeypatch):
    """A local delivery the target does not accept within turn_wait_seconds() + _DELIVERY_ACK_SLACK_SECONDS returns an error with reason delivery_timeout; the runner and target are no longer running, the completion is consumed, and the message never appears in the target transcript afterwards."""
    from tools import bot_mode_dm
    monkeypatch.setattr(bot_mode_dm, "_DELIVERY_ACK_SLACK_SECONDS", 2, raising=False)   # bound = 1 + 2 s
    fleet.configure(mode="accept", pre=12.0)
    started = time.monotonic()
    out = fleet.send()
    assert out.get("reason") == "delivery_timeout", out
    assert "status" not in out
    assert time.monotonic() - started < 10.0, "the bound, not the target, ended the wait"
    proc_id = _delivery_sessions(fleet.registry)[-1]["session_id"]
    assert fleet.registry.get(proc_id).exited
    _no_autonomous_completion(fleet.registry, proc_id)
    if fleet.pidfile.exists():                                        # absent = the target never started
        assert not _pid_alive(int(fleet.pidfile.read_text(encoding="utf-8"))), "the target turn must be stopped"
    time.sleep(max(0.0, started + 13.5 - time.monotonic()))          # past the fake target's own accept time
    assert _no_payload(fleet), "a timed-out delivery must never land later"


@pytest.mark.linux_only
@pytest.mark.parametrize("shape", ["fresh", "continuation", "compressed", "duplicate", "prefix_collision"])
def test_sent_only_once_resumed_bot_chat_holds_the_message(fleet, shape):
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
def test_accepted_delivery_stays_async_with_one_completion(fleet, monkeypatch, arming):
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
def test_dm_cache_sweep_removes_aged_signal_sidecars(fleet):
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
def test_async_unsupported_sender_gets_delivered_untracked(fleet, monkeypatch, channel):
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


@pytest.mark.linux_only
def test_unreadable_baseline_fails_closed_before_the_target_runs(fleet, monkeypatch):
    """An existing target transcript whose baseline cannot be read never acks.

    A 0 baseline there would let an OLDER identical message acknowledge this
    delivery, so the runner refuses before launching the target.
    """
    from hermes_state import SessionDB

    body = "status? PAYLOAD_F62G"
    db = SessionDB(db_path=fleet.root / "profiles" / "researcher" / "state.db")
    sid = db.create_session("bc-old", source="cli")
    db.set_session_title(sid, "Bot Chat")
    db.append_message(sid, role="user", content=f"Message from 🤖 hermes (@hermes): {body}")
    db.close()
    # The runner's first read-only open is its baseline read; fail only that one,
    # so a runner that fell back to 0 would still see the old row and ack.
    shim = fleet.root.parent / "shim"
    shim.mkdir()
    (shim / "sitecustomize.py").write_text(textwrap.dedent('''\
        import sqlite3
        _real, _seen = sqlite3.connect, []
        def _connect(*a, **k):
            if "mode=ro" in str(a[0] if a else k.get("database", "")) and not _seen:
                _seen.append(1)
                raise sqlite3.OperationalError("database is locked")
            return _real(*a, **k)
        sqlite3.connect = _connect
    '''), encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [str(shim), os.environ.get("PYTHONPATH")])))
    fleet.configure(mode="accept")
    out = fleet.send(body)
    assert out.get("status") != "sent", out
    assert "baseline" in out.get("error", ""), out
    assert not fleet.pidfile.exists(), "the target must not run without a baseline"


@pytest.mark.linux_only
def test_accepted_retryable_failure_does_not_redeliver(fleet):
    """A target that persisted the message and then failed retryably is never re-run."""
    import pathlib
    fake = pathlib.Path(fleet.bot_relay._hermes_cli())
    wrapper = fake.with_name("hermes-accept-then-503")
    count = fake.with_name("invocations.txt")
    wrapper.write_text(textwrap.dedent(f'''\
        #!{sys.executable}
        import os, subprocess, sys
        with open({str(count)!r}, "a", encoding="utf-8") as f:
            f.write("x")
        subprocess.run([{str(fake)!r}, *sys.argv[1:]], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        print("Error code: 503 - server error - overloaded", file=sys.stderr)
        sys.exit(1)
    '''), encoding="utf-8")
    wrapper.chmod(0o755)
    fleet.bot_relay._hermes_cli = lambda: str(wrapper)
    fleet.configure(mode="accept", pre=0.2, post=0.0)
    out = fleet.send()
    assert out.get("status") == "sent", out
    proc_id = out["process_id"]
    deadline = time.monotonic() + 20
    while not fleet.registry.get(proc_id).exited and time.monotonic() < deadline:
        time.sleep(0.2)
    assert count.read_text(encoding="utf-8") == "x", "an accepted delivery was re-sent"
    assert len([m for m in fleet.transcript() if "PAYLOAD_F62G" in (m.get("content") or "")]) == 1


def test_target_db_path_windows_fallback(tmp_path, monkeypatch):
    from tools import bot_mode_dm

    monkeypatch.setattr(sys, "platform", "win32")
    root = tmp_path / "AppData" / "Local" / "hermes"
    monkeypatch.setenv("LOCALAPPDATA", str(root.parent))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert bot_mode_dm._target_db_path("default") == root / "state.db"
    assert bot_mode_dm._target_db_path("researcher") == root / "profiles" / "researcher" / "state.db"

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "custom"))
    assert bot_mode_dm._target_db_path("researcher") == tmp_path / "custom" / "profiles" / "researcher" / "state.db"


@pytest.mark.linux_only
@pytest.mark.parametrize("how", ["timeout", "cancelled"])
def test_stopped_delivery_whose_row_landed_is_delivered_untracked(fleet, monkeypatch, how):
    """A delivery stopped at the bound or on an interrupt whose row already landed is delivered_untracked, never an error."""
    import threading

    from tools import bot_mode_dm
    fleet.configure(mode="accept", pre=1.5, post=30.0)

    def _block_accepted_write():
        # The runner's `accepted` write goes through <signal>.tmp; a directory there makes it fail,
        # which is the window between the row landing and the next poll, held open.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            for sig in fleet.dm_dir.glob("*.signal"):
                if '"pending"' in sig.read_text(encoding="utf-8"):
                    Path(f"{sig}.tmp").mkdir()
                    return
            time.sleep(0.02)

    threading.Thread(target=_block_accepted_write, daemon=True).start()
    real_await = bot_mode_dm._await_delivery_ack

    def _await_after_row(proc_id, signal_file, bound):
        deadline = time.monotonic() + 20
        while not fleet.accepted.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.5)
        if how == "cancelled":
            monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: True)
            return real_await(proc_id, signal_file, bound)
        return real_await(proc_id, signal_file, 0)

    monkeypatch.setattr(bot_mode_dm, "_await_delivery_ack", _await_after_row)
    out = fleet.send()
    assert fleet.accepted.exists(), "the target never persisted the row"
    assert out.get("status") == "delivered_untracked", out
    assert "error" not in out, out
    assert fleet.registry.get(out["process_id"]).exited
    assert len([m for m in fleet.transcript() if "PAYLOAD_F62G" in (m.get("content") or "")]) == 1


_READ_FAIL_SHIM = textwrap.dedent('''\
    import os, sys
    _MARKER = {marker!r}

    class _Finder:
        def find_spec(self, name, path=None, target=None):
            if name != "hermes_state":
                return None
            for finder in sys.meta_path:
                if finder is self or not hasattr(finder, "find_spec"):
                    continue
                spec = finder.find_spec(name, path, target)
                if spec is not None and spec.loader is not None:
                    break
            else:
                return None
            exec_module = spec.loader.exec_module

            def _exec(module):
                exec_module(module)
                init = module.SessionDB.__init__

                def __init__(self, *a, **k):
                    if k.get("read_only") and os.path.exists(_MARKER):
                        import sqlite3
                        raise sqlite3.OperationalError("injected: database is locked")
                    init(self, *a, **k)

                module.SessionDB.__init__ = __init__

            spec.loader.exec_module = _exec
            return spec

    sys.meta_path.insert(0, _Finder())
''')


@pytest.mark.linux_only
def test_unreadable_ack_does_not_redeliver(fleet, tmp_path, monkeypatch):
    """A persisted row whose acceptance cannot be read is never retried: one invocation, one row, possibly-delivered error."""
    import pathlib
    marker = tmp_path / "acks-unreadable"
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "sitecustomize.py").write_text(_READ_FAIL_SHIM.format(marker=str(marker)), encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(shim))
    fake = pathlib.Path(fleet.bot_relay._hermes_cli())
    wrapper = fake.with_name("hermes-unreadable-then-503")
    count = fake.with_name("invocations.txt")
    wrapper.write_text(textwrap.dedent(f'''\
        #!{sys.executable}
        import os, subprocess, sys
        with open({str(count)!r}, "a", encoding="utf-8") as f:
            f.write("x")
        if os.path.getsize({str(count)!r}) == 1:
            open({str(marker)!r}, "w", encoding="utf-8").close()
            subprocess.run([{str(fake)!r}, *sys.argv[1:]], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        print("Error code: 503 - server error - overloaded", file=sys.stderr)
        sys.exit(1)
    '''), encoding="utf-8")
    wrapper.chmod(0o755)
    fleet.bot_relay._hermes_cli = lambda: str(wrapper)
    fleet.configure(mode="accept", pre=0.2, post=0.0)
    out = fleet.send()
    assert count.exists(), out
    assert count.read_text(encoding="utf-8") == "x", "an unconfirmed delivery was re-sent"
    assert len([m for m in fleet.transcript() if "PAYLOAD_F62G" in (m.get("content") or "")]) == 1
    assert out.get("status") != "sent", out
    assert out.get("reason") == "delivery_unconfirmed", out
    assert "may already" in out.get("error", ""), out
