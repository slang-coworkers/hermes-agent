"""A2A-F20 reply-home lineage: stock Hermes routes a hand-off reply back to its
originating session/edge through three native correlations.

- Bot Chat reply-home: message_agent dispatches a background, notify-on-complete
  delivery pinned to the target's canonical "Bot Chat", carrying the origin
  task id so the completion notification threads back to the sender.
- Kanban durable hand-off: a decomposed child inherits the root's notify
  subscription, a terminal child event is delivered to that originating
  destination, and the dependency-gated root is promoted todo->ready.
- a2a per-context/task correlation: a successful reply resolves the oldest
  pending future for its contextId (FIFO); a terminal outcome resolves the
  exact task by message_id.
"""

import asyncio
import json
import shlex
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools import bot_mode_dm, bot_mode_probe


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    bot_mode_probe._reset_cache_for_tests()
    yield
    bot_mode_probe._reset_cache_for_tests()


def _managed_home(tmp_path, *, teammates=("researcher",)) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir(exist_ok=True)
    for name in teammates:
        d = home / "profiles" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "profile.yaml").write_text(
            textwrap.dedent(
                """\
                description: teammate for tests
                ui_meta:
                  hermes-bots:
                    shape: cloud
                """
            ),
            encoding="utf-8",
        )
    return home


class _FakeDB:
    def __init__(self, home: Path, title: str):
        self.db_path = str(home / "state.db")
        self._title = title

    def get_session_title(self, _sid):
        return self._title


class _FakeAgent:
    def __init__(self, home: Path, title: str = "Bot Chat"):
        self._session_db = _FakeDB(home, title)
        self.session_id = "sess-1"
        self._session_title_hint = None
        self._bot_mode_protocol = True
        self.tools: list = []
        self.valid_tool_names: set = set()


def _capture_spawn(monkeypatch, tmp_path):
    calls = []

    def fake_terminal_tool(command, **kwargs):
        calls.append({"command": command, **kwargs})
        return json.dumps({"output": "started", "session_id": "proc_test1234"})

    import tools.terminal_tool as terminal_tool_module

    monkeypatch.setattr(terminal_tool_module, "terminal_tool", fake_terminal_tool)
    # keep DM temp files inside pytest's tmp_path (auto-cleaned) instead of the
    # shared /tmp/hermes-dm dir — the faked terminal_tool never consumes them.
    monkeypatch.setattr(bot_mode_dm, "_dm_dir", lambda: tmp_path)
    return calls


def _runner_parts(command):
    parts = shlex.split(command)
    marker = parts.index("--run-delivery")
    return parts[marker + 1], parts[marker + 2], parts[marker + 3:]


def test_ac_a2a_f20_1(tmp_path, monkeypatch):
    """message_agent arms reply-home as a background, notify-on-complete,
    fire-and-forget delivery carrying the origin task id (the reply returns
    later as a completion notification, not synchronously)."""
    calls = _capture_spawn(monkeypatch, tmp_path)
    home = _managed_home(tmp_path)
    agent = _FakeAgent(home, title="Bot Chat")
    origin_task = "a2a-origin-7f91"

    result = json.loads(
        bot_mode_dm.message_agent_tool(
            target="@researcher", message="status?", task_id=origin_task, agent=agent
        )
    )

    assert result["status"] == "sent"
    assert len(calls) == 1
    call = calls[0]
    # background + notify_on_complete = the reply lands as a completion
    # notification on the sender's next turn; task_id is the correlation key
    # that threads it home. A mutant dropping any of the three breaks reply-home.
    assert call["background"] is True
    assert call["notify_on_complete"] is True
    assert call["task_id"] == origin_task


def test_ac_a2a_f20_2(tmp_path, monkeypatch):
    """The target answers only in its single canonical "Bot Chat" session, per
    invocation; the message body is delivered verbatim off the command line."""
    calls = _capture_spawn(monkeypatch, tmp_path)
    home = _managed_home(tmp_path)
    agent = _FakeAgent(home, title="Bot Chat")

    body = 'give me the "PAYLOAD_SENTINEL_7A91" numbers $(not shell)'
    bot_mode_dm.message_agent_tool(target="@researcher", message=body, agent=agent)

    command = calls[0]["command"]
    _mode, dm_file, transport_argv = _runner_parts(command)
    ci = transport_argv.index("-c")
    assert transport_argv[ci + 1] == "Bot Chat"
    # the body must travel verbatim inside the temp file, never on the command
    # line; the full body is the suffix after the attribution prefix — a mutant
    # that drops OR truncates the body flips the content check (rstrip tolerates
    # a trailing newline only).
    assert "PAYLOAD_SENTINEL_7A91" not in command
    content = Path(dm_file).read_text(encoding="utf-8")
    assert content.rstrip().endswith(body)


def _build_runner(monkeypatch, tmp_path):
    from gateway.config import GatewayConfig, Platform
    from gateway.run import GatewayRunner
    import gateway.run as gateway_run

    (tmp_path / "config.yaml").write_text(
        "display:\n  background_process_notifications: all\n", encoding="utf-8"
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    runner = GatewayRunner(GatewayConfig())
    adapter = SimpleNamespace(send=AsyncMock(), handle_message=AsyncMock())
    runner.adapters[Platform.TELEGRAM] = adapter
    return runner, adapter


def test_ac_a2a_f20_3(tmp_path, monkeypatch):
    """A background completion notification is routed back into the sender's
    ORIGINATING session, selected by the queued event's session_key — not the
    first stored origin, the foreground session, or the target/worker session."""
    from gateway.config import Platform
    from gateway.session import SessionSource

    runner, adapter = _build_runner(monkeypatch, tmp_path)

    caller_key = "agent:main:telegram:group:-100:42"
    decoy_key = "agent:main:telegram:group:-999:99"
    caller_origin = SessionSource(
        platform=Platform.TELEGRAM, chat_id="-100", chat_type="group",
        thread_id="42", user_id="123", user_name="Caller",
    )
    decoy_origin = SessionSource(
        platform=Platform.TELEGRAM, chat_id="-999", chat_type="group",
        thread_id="99", user_id="666", user_name="Decoy",
    )
    # decoy inserted FIRST so a "pick the first stored origin" mutant chooses it.
    runner.session_store._entries[decoy_key] = SimpleNamespace(origin=decoy_origin)
    runner.session_store._entries[caller_key] = SimpleNamespace(origin=caller_origin)

    # evt carries the caller's session_key but the DECOY's raw routing fields,
    # so a mutant that reads routing off the event instead of the keyed origin
    # would surface the decoy.
    evt = {
        "session_id": "proc_watch",
        "session_key": caller_key,
        "platform": "telegram",
        "chat_id": "-999",
        "thread_id": "99",
    }
    asyncio.run(runner._inject_watch_notification("[SYSTEM: reply-home]", evt))

    adapter.handle_message.assert_awaited_once()
    synth = adapter.handle_message.await_args.args[0]
    assert synth.internal is True
    assert synth.source.chat_id == "-100"
    assert synth.source.thread_id == "42"
    assert synth.source.user_id == "123"


def test_ac_a2a_f20_4():
    """On the cross-gateway a2a path each reply is correlated to its own
    originating request per context/task with no cross-context bleed: a
    successful reply resolves the oldest pending future for its contextId
    (FIFO); a terminal outcome resolves the exact task by message_id."""
    from plugins.platforms.a2a.adapter import A2AAdapter
    from plugins.platforms.a2a import protocol
    from gateway.config import PlatformConfig
    from gateway.platforms.base import ProcessingOutcome

    adapter = A2AAdapter(PlatformConfig(enabled=True))
    a1 = adapter._add_pending("t-A1", "ctx-A")
    a2 = adapter._add_pending("t-A2", "ctx-A")
    b1 = adapter._add_pending("t-B1", "ctx-B")
    b2 = adapter._add_pending("t-B2", "ctx-B")
    try:
        asyncio.run(adapter.send("ctx-A", "reply-A1", metadata={"notify": True}))
        assert a1.result(timeout=0) == (protocol.STATE_COMPLETED, "reply-A1")
        assert not a2.done()               # a newest-first mutant resolves a2
        assert not b1.done() and not b2.done()   # a cross-context mutant resolves ctx-B

        asyncio.run(
            adapter.on_processing_complete(
                SimpleNamespace(message_id="t-B2"), ProcessingOutcome.FAILURE
            )
        )
        assert b2.result(timeout=0)[0] == protocol.STATE_FAILED
        assert not b1.done()               # a context-wide mutant resolves b1
        assert not a2.done()
    finally:
        for tid in ("t-A1", "t-A2", "t-B1", "t-B2"):
            adapter._pop_pending(tid)


def test_ac_a2a_f20_5(tmp_path, monkeypatch):
    """Kanban preserves the originating channel for durable hand-offs: each
    decomposed child inherits the root's notify subscription, the root is
    dependency-gated (promoted todo->ready only when ALL children finish), and
    a terminal child event is delivered to the originating destination."""
    from hermes_cli import kanban_db as kb
    from gateway.config import Platform
    from gateway.run import GatewayRunner

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    with kb.connect() as conn:
        root = kb.create_task(conn, title="ship a feature", triage=True)
        kb.add_notify_sub(
            conn, task_id=root, platform="telegram", chat_id="chat1",
            thread_id="topic1", user_id="user1", notifier_profile="default",
        )
    # two INDEPENDENT children so the root is gated on BOTH completing.
    with kb.connect() as conn:
        child_ids = kb.decompose_triage_task(
            conn, root, root_assignee="orchestrator",
            children=[
                {"title": "work A", "assignee": "engineer", "parents": []},
                {"title": "work B", "assignee": "engineer", "parents": []},
            ],
            author="decomposer",
        )
    assert child_ids and len(child_ids) == 2

    with kb.connect() as conn:
        for cid in child_ids:
            subs = kb.list_notify_subs(conn, cid)
            assert any(s["chat_id"] == "chat1" for s in subs)
        assert kb.get_task(conn, root).status == "todo"

    with kb.connect() as conn:
        assert kb.complete_task(conn, child_ids[0], result="A done") is True
    with kb.connect() as conn:
        # one child done is not enough — an any-child promotion mutant flips here
        assert kb.get_task(conn, root).status == "todo"

    with kb.connect() as conn:
        assert kb.complete_task(conn, child_ids[1], result="B done") is True
    with kb.connect() as conn:
        assert kb.get_task(conn, root).status == "ready"

    runner = object.__new__(GatewayRunner)
    runner._owns_kanban_dispatcher_lock = lambda: True
    runner._running = True
    runner._kanban_sub_fail_counts = {}
    sent_chat_ids: list = []

    async def _send(chat_id, msg, metadata=None):
        sent_chat_ids.append(chat_id)

    fake_adapter = MagicMock()
    fake_adapter.send = AsyncMock(side_effect=_send)
    runner.adapters = {Platform.TELEGRAM: fake_adapter}

    tick = {"n": 0}
    _orig_sleep = asyncio.sleep

    async def _fast_sleep(_):
        await _orig_sleep(0)
        tick["n"] += 1
        if tick["n"] >= 3:
            runner._running = False

    async def _run_tick():
        with patch("gateway.run.asyncio.sleep", side_effect=_fast_sleep), \
             patch("gateway.wake.deliver_wake", new=AsyncMock()):
            await asyncio.wait_for(
                runner._kanban_notifier_watcher(interval=1), timeout=10.0
            )

    asyncio.run(_run_tick())
    # a mutant that lost the inherited subscription delivers nothing to chat1.
    assert "chat1" in sent_chat_ids
