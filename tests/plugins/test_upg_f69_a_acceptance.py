"""UPG-F69.a acceptance: the process watcher ends after its unverified-exit notice.

ADR ``reports/upg-f69-a.md``. A watched background session that has exited, but whose result is never published, gets
exactly one "outcome unknown" notice; after it the watcher ends instead of polling for the life of the process.
The registry here keeps the session registered and unpublished, the state the registry's own failure edges leave.
"""

import asyncio
import subprocess
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from gateway.run_notifications import _UNPUBLISHED_EXIT_NOTICE_TICKS as N

REPO = Path(__file__).resolve().parents[2]
F69_MERGE = "d2802d78b74faf6bc1e04bc2e3e0fa80cfbd8473"  # UPG-F69 (#87) squash, the base of this row
SESSION_ID = "proc_upg_f69a"
OVERRUN = 8  # registry reads past the notice before a watcher counts as still polling


class _StillPolling(Exception):
    pass


class _Registry:
    """Keeps one exited session registered and never consumed; sets its publish event on read ``publish_on``."""

    def __init__(self, session, publish_on=None):
        self.session, self.publish_on, self.reads = session, publish_on, 0

    def get(self, session_id):
        self.reads += 1
        if self.reads == self.publish_on:
            self.session._completion_event.set()
        if self.reads > N + OVERRUN:
            raise _StillPolling(self.reads)
        return self.session if session_id == SESSION_ID else None

    def is_completion_consumed(self, session_id):
        return False


def _exited_session():
    return SimpleNamespace(output_buffer="done\n", exited=True, exit_code=0, command="echo done", started_at=None,
                           _completion_event=threading.Event())


def _watcher(notify_on_complete):
    return {"session_id": SESSION_ID, "check_interval": 0, "platform": "telegram", "chat_id": "123",
            "session_key": "agent:main:telegram:dm:123", "notify_on_complete": notify_on_complete}


async def _drive(monkeypatch, home, notify_on_complete, watcher_fn, publish_on=None):
    import gateway.run as gateway_run
    import tools.process_registry as pr_module

    (home / "config.yaml").write_text("display:\n  background_process_notifications: concise\n", encoding="utf-8")
    monkeypatch.setattr(gateway_run, "_hermes_home", home)
    registry = _Registry(_exited_session(), publish_on)
    monkeypatch.setattr(pr_module, "process_registry", registry)
    runner = GatewayRunner(GatewayConfig())
    send_reads = []
    adapter = SimpleNamespace(send=AsyncMock(side_effect=lambda *a, **k: send_reads.append(registry.reads)),
                              handle_message=AsyncMock())
    runner.adapters[Platform.TELEGRAM] = adapter
    runner._enqueue_process_completion_notification = AsyncMock(return_value=True)
    try:
        await asyncio.wait_for(watcher_fn(runner, _watcher(notify_on_complete)), timeout=30)
        ended = True
    except _StillPolling:
        ended = False
    return SimpleNamespace(
        ended=ended, reads=registry.reads, sent=[c.args[1] for c in adapter.send.await_args_list],
        reads_after_send=registry.reads - send_reads[0] if send_reads else None,
        completions=runner._enqueue_process_completion_notification.await_count,
        published=registry.session._completion_event.is_set(),
        final=runner._format_process_final_message(SESSION_ID, registry.session, "concise"),
    )


def _notice():
    from agent.i18n import t

    return t("gateway.background.unverified_exit", session_id=SESSION_ID)


def _merged_watcher(monkeypatch):
    """``_run_process_watcher`` exactly as UPG-F69 merged it, executed from the merge commit's blob."""
    blob = subprocess.run(["git", "-C", str(REPO), "show", f"{F69_MERGE}:gateway/run_notifications.py"],
                          capture_output=True, text=True, encoding="utf-8", check=False)
    if blob.returncode != 0:
        pytest.fail(f"the negative control needs UPG-F69's merge {F69_MERGE} in this checkout's history "
                    f"(CI checks out with fetch-depth: 0): {blob.stderr.strip()}")
    module = types.ModuleType("upg_f69a_merged_run_notifications")
    monkeypatch.setitem(sys.modules, module.__name__, module)  # its dataclass resolves annotations through it
    exec(compile(blob.stdout, f"{F69_MERGE}:gateway/run_notifications.py", "exec"), module.__dict__)
    return module.GatewayNotificationsMixin._run_process_watcher


@pytest.mark.asyncio
@pytest.mark.parametrize("notify_on_complete", [True, False], ids=["agent_notify", "chat_only"])
async def test_ac_upg_f69_a_1(monkeypatch, tmp_path, notify_on_complete):
    """A watched session that has exited and is never published gets exactly one unverified-exit notice, and its watcher then ends by itself, with no registry read after the notice, while the session is still registered."""
    out = await _drive(monkeypatch, tmp_path, notify_on_complete, GatewayRunner._run_process_watcher)

    assert out.ended, f"the watcher was still polling {out.reads_after_send} registry reads after its notice"
    assert out.sent == [_notice()], out.sent
    assert out.reads_after_send == 0 and out.reads == N, (out.reads, out.reads_after_send)
    assert out.completions == 0 and not out.published


@pytest.mark.asyncio
@pytest.mark.parametrize("publish_on", [1, N], ids=["first_read", "notice_read"])
@pytest.mark.parametrize("notify_on_complete", [True, False], ids=["agent_notify", "chat_only"])
async def test_ac_upg_f69_a_2(monkeypatch, tmp_path, notify_on_complete, publish_on):
    """(derived) A session whose result is published inside the notice window, up to and including the read on which the notice would fire, still gets its one completion and no unverified-exit notice, and its watcher ends."""
    out = await _drive(monkeypatch, tmp_path, notify_on_complete, GatewayRunner._run_process_watcher, publish_on)

    assert out.ended and out.reads == publish_on, (out.ended, out.reads)
    assert _notice() not in out.sent, out.sent
    if notify_on_complete:
        assert out.completions == 1 and out.sent == [], (out.completions, out.sent)
    else:
        assert out.completions == 0 and out.sent == [out.final], (out.completions, out.sent)


@pytest.mark.asyncio
@pytest.mark.parametrize("notify_on_complete", [True, False], ids=["agent_notify", "chat_only"])
async def test_ac_upg_f69_a_3(monkeypatch, tmp_path, notify_on_complete):
    """Negative control: the watcher as merged in UPG-F69 (d2802d78), driven through criterion 1's scenario, sends the same one notice but does not end; it is still polling the registry OVERRUN reads after the notice, so criterion 1 fails on F69's merged head."""
    out = await _drive(monkeypatch, tmp_path, notify_on_complete, _merged_watcher(monkeypatch))

    assert out.sent == [_notice()], out.sent
    assert not out.ended, "the merged watcher ended after its notice: criterion 1 no longer discriminates"
    assert out.reads_after_send >= OVERRUN and out.completions == 0, (out.reads_after_send, out.completions)
