"""Behavior tests for MSG-DELIV: PATH-independent message_agent CLI resolution.

The same-machine message_agent delivery must resolve the hermes CLI to an
absolute path via _hermes_cli(), so a delivery subprocess launched under a
service-context PATH (no venv bin on PATH) can still find it instead of the
bare "hermes" that raises FileNotFoundError in the background runner.
"""

import json
import os
import shlex
import shutil
import sys
import textwrap
from pathlib import Path

import pytest

from tools import bot_mode_dm, bot_mode_probe


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    bot_mode_probe._reset_cache_for_tests()
    yield
    bot_mode_probe._reset_cache_for_tests()


def _managed_home(tmp_path, *, teammates=("researcher",), peers=()):
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
    if peers:
        lines = ["bot_peers:"]
        for peer in peers:
            lines += [f"  {peer}:", f"    url: http://{peer}.lan:8377"]
        (home / "config.yaml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return home


class _FakeDB:
    def __init__(self, home, title):
        self.db_path = str(home / "state.db")
        self._title = title

    def get_session_title(self, _sid):
        return self._title


class _FakeAgent:
    def __init__(self, home, title="Bot Chat"):
        self._session_db = _FakeDB(home, title)
        self.session_id = "sess-1"
        self._session_title_hint = None
        self._bot_mode_protocol = True
        self.tools = []
        self.valid_tool_names = set()


def _capture_spawn(monkeypatch):
    calls = []

    def fake_terminal_tool(command, **kwargs):
        calls.append({"command": command, **kwargs})
        return json.dumps(
            {"output": "Background process started", "session_id": "proc_test1234"}
        )

    import tools.terminal_tool as terminal_tool_module

    monkeypatch.setattr(terminal_tool_module, "terminal_tool", fake_terminal_tool)
    return calls


def _runner_parts(command):
    parts = shlex.split(command)
    marker = parts.index("--run-delivery")
    return parts[marker + 1], parts[marker + 2], parts[marker + 3:]


def _service_context_cli(tmp_path, monkeypatch):
    """Point _hermes_cli() at an executable sibling, with an empty PATH.

    With sys.executable inside a fake venv bin, _hermes_cli() returns the
    absolute bin/hermes sibling. PATH is then set to a real but empty dir, so a
    bare "hermes" is unresolvable (shutil.which returns None) while the absolute
    sibling still resolves — the service-context condition being reproduced.
    """
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    sibling = bin_dir / ("hermes.exe" if sys.platform == "win32" else "hermes")
    sibling.touch()
    sibling.chmod(0o755)
    monkeypatch.setattr("sys.executable", str(bin_dir / "python"))
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    return sibling


def _assert_resolves_without_path(transport_argv, sibling):
    prog = transport_argv[0]
    assert os.path.isabs(prog), (
        f"message_agent delivery argv[0] must be an absolute path so a "
        f"service-context PATH spawn can find it; got bare {prog!r}"
    )
    # Compare as Path, not str: _delivery_command forward-slashes the argv on
    # Windows, so the captured token and the sibling differ only by separator.
    assert Path(prog) == sibling, (
        "argv[0] must be the _hermes_cli() sibling resolution, not a bare name"
    )
    assert os.access(prog, os.X_OK)
    assert shutil.which(prog) == prog
    assert shutil.which("hermes") is None


def test_ac_msg_deliv_1(tmp_path, monkeypatch):
    """Under a service-context PATH, a same-machine local-teammate message_agent
    delivery resolves the hermes CLI to the absolute _hermes_cli() path so the
    delivery subprocess can find its CLI instead of dying with FileNotFoundError.
    """
    sibling = _service_context_cli(tmp_path, monkeypatch)
    calls = _capture_spawn(monkeypatch)
    home = _managed_home(tmp_path, teammates=("researcher",))
    agent = _FakeAgent(home, title="Bot Chat")

    result = json.loads(
        bot_mode_dm.message_agent_tool(
            target="@researcher", message="ping", agent=agent
        )
    )
    assert result["status"] == "sent"
    assert len(calls) == 1

    _mode, _dm_file, transport_argv = _runner_parts(calls[0]["command"])
    assert transport_argv[1:3] == ["-p", "researcher"]
    assert "chat" in transport_argv
    _assert_resolves_without_path(transport_argv, sibling)


def test_ac_msg_deliv_2(tmp_path, monkeypatch):
    """Under a service-context PATH, a same-machine peer message_agent delivery
    resolves the hermes CLI to the absolute _hermes_cli() path so the peer-DM
    delivery subprocess can find its CLI instead of dying with FileNotFoundError.
    """
    sibling = _service_context_cli(tmp_path, monkeypatch)
    calls = _capture_spawn(monkeypatch)
    home = _managed_home(tmp_path, teammates=("researcher",), peers=("spark",))
    agent = _FakeAgent(home, title="Bot Chat")

    result = json.loads(
        bot_mode_dm.message_agent_tool(target="spark", message="ping", agent=agent)
    )
    assert result["status"] == "sent"
    assert len(calls) == 1

    _mode, _dm_file, transport_argv = _runner_parts(calls[0]["command"])
    assert "peer" in transport_argv and "dm" in transport_argv
    _assert_resolves_without_path(transport_argv, sibling)
