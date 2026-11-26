"""display.platforms.<platform>.tool_progress_show_args: false keeps tool arguments out of progress lines."""

from __future__ import annotations

import queue
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.display_config import resolve_display_setting
from gateway.run_turn_progress import GatewayTurnProgressMixin


class _P(GatewayTurnProgressMixin):
    def __init__(self, show_args, mode="new"):
        cfg = {"display": {"platforms": {"slack": {"tool_progress_show_args": show_args}}}}
        self._ctx = SimpleNamespace(source=SimpleNamespace(platform=Platform.SLACK), user_config=cfg,
                                    resolve_display_setting=resolve_display_setting, progress_mode=mode,
                                    last_was_terminal_block=[None], progress_queue=queue.Queue())
        self._runner = SimpleNamespace(_delivery_adapter_for=lambda src: None)


ARGS = {"command": "cat /home/someone/private/config.yaml"}


def test_default_is_true():
    assert resolve_display_setting({}, "slack", "tool_progress_show_args", True) is True


@pytest.mark.parametrize("mode", ["new", "all"])
def test_verb_only_progress_never_shows_the_command_or_path(mode):
    line = _P(False, mode)._progress_build_message("terminal", "cat /home/someone/private", ARGS)
    assert line and "/home/someone" not in line and "cat " not in line and "config.yaml" not in line


def test_verbose_mode_queues_the_verb_only_line():
    p = _P(False, "verbose")
    assert p._progress_build_message("terminal", "cat /home/someone/x", ARGS) is None
    queued = p._ctx.progress_queue.get_nowait()
    assert "/home/someone" not in queued and "cat " not in queued


def test_show_args_true_keeps_the_preview():
    line = _P(True)._progress_build_message("web_search", "release notes", {"query": "release notes"})
    assert "release notes" in line
