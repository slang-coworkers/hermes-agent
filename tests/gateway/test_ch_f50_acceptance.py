"""Adopt-track acceptance coverage for CH-F50 interactive operations on stock Hermes.

Proves the four NanoClaw interactive operations (cards, questions, reactions,
files) already work: native buttons on a button-capable adapter, a numbered-text
/ `/approve` fallback on adapters that cannot render buttons, and interactive
structured choices on the desktop panel (backed by the headless tui_gateway
server that `hermes serve` runs). Questions (`clarify`) and the desktop
`react_to_message` tool are agent-callable; approval cards are raised
automatically by the approval gate; messaging reactions and file sends run
through the `send_message` transport engine, which is deliberately not a model
tool (these tests drive that engine directly, the way cron delivery, the
`hermes send` CLI, and the MCP server do). One test function per
acceptance-criterion id (test_ac_ch_f50_1 .. _8). Behaviour contracts only — no
network; SessionDB and HERMES_HOME are sandboxed to tmp_path by conftest.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from gateway.platforms.base import BasePlatformAdapter, SendResult


class _FallbackAdapter(BasePlatformAdapter):
    """Adapter double that inherits the base interactive fallbacks."""

    name = "fallback"

    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"chat_id": chat_id}

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append({"chat_id": chat_id, "content": content, "metadata": metadata})
        return SendResult(success=True, message_id="m1")


def _relay_adapter():
    """Construct a prompt-capable Relay adapter over an SDK-free stub."""
    from gateway.config import PlatformConfig
    from gateway.relay.adapter import RelayAdapter
    from gateway.relay.descriptor import CONTRACT_VERSION, CapabilityDescriptor
    from tests.gateway.relay.stub_connector import StubConnector

    desc = CapabilityDescriptor(
        contract_version=CONTRACT_VERSION,
        platform="telegram",
        label="Telegram",
        max_message_length=4096,
        supports_draft_streaming=False,
        supports_edit=True,
        supports_threads=True,
        markdown_dialect="markdown_v2",
        len_unit="utf16",
        supported_ops=(
            "send",
            "edit",
            "typing",
            "get_chat_info",
            "send_media",
            "prompt",
            "react",
        ),
    )
    stub = StubConnector(desc)
    adapter = RelayAdapter(PlatformConfig(), desc, transport=stub)
    return adapter, stub


@pytest.mark.asyncio
async def test_ac_ch_f50_1():
    """A question with choices on a fallback adapter renders as a numbered text list."""
    adapter = _FallbackAdapter()
    await adapter.send_clarify(
        chat_id="c1",
        question="Pick a fruit",
        choices=["apple", "banana"],
        clarify_id="clar-1",
        session_key="sess:1",
    )
    text = adapter.sent[-1]["content"]
    assert "1." in text and "apple" in text
    assert "2." in text and "banana" in text
    assert "number" in text.lower()


@pytest.mark.asyncio
async def test_ac_ch_f50_2():
    """A question with choices on a button adapter renders one option per choice plus Other."""
    adapter, stub = _relay_adapter()
    choices = ["Alpha", "Beta", "Gamma", "Delta"]
    result = await adapter.send_clarify("c1", "Pick one", choices, "clar-2", "sess:1")
    assert result.success is True
    action = stub.sent[-1]
    assert action["op"] == "prompt"
    assert action["prompt_kind"] == "clarify"
    ids = [o["id"] for o in action["options"]]
    assert ids == ["c0", "c1", "c2", "c3", "other"]
    assert [o["label"] for o in action["options"][:4]] == choices
    assert action["prompt_id"] in adapter._pending_prompts


@pytest.mark.asyncio
async def test_ac_ch_f50_3():
    """A dangerous-command approval on a button messaging adapter offers once/session/always/deny."""
    adapter, stub = _relay_adapter()
    result = await adapter.send_exec_approval(
        "c1", "rm -rf /tmp/x", "sess:1", description="deletes files"
    )
    assert result.success is True
    action = stub.sent[-1]
    assert action["op"] == "prompt"
    assert action["prompt_kind"] == "approval"
    ids = [o["id"] for o in action["options"]]
    assert ids == ["once", "session", "always", "deny"]
    assert "rm -rf /tmp/x" in action["content"]
    assert "deletes files" in action["content"]


def test_ac_ch_f50_4():
    """The same approval on a messaging adapter without buttons degrades to a /approve text card."""
    from gateway.run import _format_exec_approval_fallback

    text = _format_exec_approval_fallback(
        "rm -rf /tmp/x",
        "deletes files",
        "/",
        allow_permanent=True,
        allow_session=True,
        smart_denied=False,
    )
    assert "rm -rf /tmp/x" in text
    assert "Reply `/approve`" in text
    assert "`/approve session`" in text
    assert "`/approve always`" in text
    assert "`/deny`" in text


def test_ac_ch_f50_5():
    """The desktop-panel approval payload (tui_gateway backend) carries interactive structured choices."""
    # Importing tui_gateway.server kicks off a background update check that shells
    # out to `git fetch`; stub it so this stays hermetic (no network on import).
    with patch("hermes_cli.banner.prefetch_update_check"):
        from tui_gateway.server import _approval_request_payload

    payload = _approval_request_payload(
        {"command": "echo hi", "allow_session": True, "allow_permanent": True}
    )
    assert payload["choices"] == ["once", "session", "always", "deny"]
    denied = _approval_request_payload({"smart_denied": True})
    assert denied["choices"] == ["once", "deny"]
    no_permanent = _approval_request_payload(
        {"command": "echo hi", "allow_session": True, "allow_permanent": False}
    )
    assert no_permanent["choices"] == ["once", "session", "deny"]
    no_session = _approval_request_payload({"allow_session": False})
    assert no_session["choices"] == ["once", "deny"]


def test_ac_ch_f50_6():
    """The send_message transport engine dispatches an emoji to a messaging adapter's add_reaction via action=react (send_message is not a model tool)."""
    import tools.send_message_tool as smt
    from gateway.config import Platform

    class _FakeReactAdapter:
        def __init__(self):
            self.calls = []

        async def add_reaction(self, chat_id, emoji, message_id=None):
            self.calls.append(("add", chat_id, emoji, message_id))
            return {"success": True, "emoji": emoji}

    adapter = _FakeReactAdapter()
    runner = SimpleNamespace(adapters={Platform("photon"): adapter})
    with patch("gateway.run._gateway_runner_ref", lambda: runner):
        result = json.loads(
            smt.send_message_tool(
                {"action": "react", "target": "photon:+15551234567", "emoji": "❤️"}
            )
        )
    assert result["success"] is True
    assert adapter.calls == [("add", "+15551234567", "❤️", None)]


@pytest.mark.asyncio
async def test_ac_ch_f50_7(tmp_path):
    """A file on an adapter with native document support is delivered via the adapter's send_document."""
    from tools.send_message_tool import _send_live_adapter_media

    media = tmp_path / "report.pdf"
    media.write_text("x", encoding="utf-8")

    class _DocAdapter:
        def __init__(self):
            self.calls = []

        async def send(self, chat_id, content, metadata=None):
            return SendResult(success=True, message_id="m")

        async def send_document(self, chat_id, media_path, **kwargs):
            self.calls.append((chat_id, media_path))
            return SendResult(success=True, message_id="d")

    adapter = _DocAdapter()
    result = await _send_live_adapter_media(
        adapter, "c1", "", [(str(media), False)], force_document=True
    )
    assert adapter.calls == [("c1", str(media))]
    assert not (isinstance(result, dict) and result.get("error"))


@pytest.mark.asyncio
async def test_ac_ch_f50_8(tmp_path):
    """A file on an adapter that only inherits send_document returns a sanitized no-host-path error."""
    from tools.send_message_tool import _send_live_adapter_media

    media = tmp_path / "secret.bin"
    media.write_text("x", encoding="utf-8")

    adapter = _FallbackAdapter()
    result = await _send_live_adapter_media(
        adapter, "c1", "", [(str(media), False)], force_document=True
    )
    assert isinstance(result, dict) and "error" in result
    assert "does not implement native" in result["error"]
    assert str(media) not in result["error"]
    # The inherited base send_document text-notice is never sent to the chat.
    assert adapter.sent == []
