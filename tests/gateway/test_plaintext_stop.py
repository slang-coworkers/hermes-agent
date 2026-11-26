"""A Slack message that is only "stop" becomes /stop at adapter entry (cancels instead of steering the run)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.base import coerce_plaintext_gateway_command
from gateway.platforms.event import MessageEvent, MessageType


def _ev(text, platform=Platform.SLACK, mtype=MessageType.TEXT, media=None, chat_type="group"):
    return MessageEvent(text=text, message_type=mtype, media_urls=list(media or []),
                        source=SimpleNamespace(platform=platform, chat_type=chat_type, chat_id="C1"))


@pytest.mark.parametrize("text", ["stop", "Stop", "STOP", "stop.", "Stop!", " stop ", "stop?", "stop…",
                                  "<@U0BOT1> stop", "<@U0BOT1|bot>, Stop.", "stop！"])
def test_a_bare_slack_stop_becomes_slash_stop(text):
    ev = _ev(text)
    coerce_plaintext_gateway_command(ev)
    assert ev.text == "/stop" and ev.get_command() == "stop"


@pytest.mark.parametrize("text", ["please stop", "stop the build", "don't stop", "stopped", "nonstop",
                                  "stop and check", "s top", "ſtop"])
def test_other_text_is_untouched(text):
    ev = _ev(text)
    coerce_plaintext_gateway_command(ev)
    assert ev.text == text


def test_only_slack_plain_text_without_files():
    for ev in (_ev("stop", platform=Platform.TELEGRAM), _ev("stop", mtype=MessageType.PHOTO),
               _ev("stop", media=["https://files.example.com/x.png"])):
        coerce_plaintext_gateway_command(ev)
        assert ev.text == "stop"
