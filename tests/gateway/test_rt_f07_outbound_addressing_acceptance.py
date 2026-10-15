"""Hermetic acceptance test for RT-F07 — Cold-DM cache / outbound addressing (ADOPT).

Proves stock Hermes at v2026.8.31 already provides NanoClaw's cold-DM cache /
outbound addressing:
- AC-1 one send-target resolver over the target grammar (``resolve_send_target``):
  Slack ``user:``/``user_name:`` resolution-required markers + conversation-id
  passthrough, and a direct-addressable email returned verbatim.
- AC-2 the ``platform:#channel`` form resolved through ``resolve_send_target`` ->
  the on-disk ``channel_directory`` discovery cache; missing file -> empty.
- AC-3 the Slack per-``(team,user)`` cold-DM open-and-cache
  (``SlackAdapter._ensure_dm_conversation``).
- AC-4 the per-platform ``HomeChannel`` fallback: parsed with provenance, served
  by ``GatewayConfig.get_home_channel``, and actually consumed by the ``hermes
  send`` path (positive: chat_id routes to ``_send_to_platform``; negative: the
  documented no-home error, with the getter proven called).
- AC-5 the NanoClaw-parity doc section cites the surface + consumers in tag:/main:.
- AC-6 the ``send_message`` tool re-resolves a Slack user target per call
  (conversations.open per call), sharing no cache with the adapter.

No plugin: this is an adopt/regression-pin row. AC-1..4/6 pass on the stock tree
(that passing IS the proof); AC-5 carries the fail-before/pass-after (the parity
heading is absent on stock). No network, nothing written under ~/.hermes.
"""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.channel_directory import load_directory, resolve_channel_name
from gateway.config import GatewayConfig, HomeChannel, Platform, PlatformConfig
from tools.send_message_tool import resolve_send_target


def test_ac_rt_f07_1():
    """resolve_send_target resolves an outbound target address purely: @alice -> user_name:alice, U01234567 -> user:U01234567, a conversation id passes through, and a direct-addressable email is returned verbatim."""
    cases = [
        ("slack", "@alice", "user_name:alice"),
        ("slack", "U01234567", "user:U01234567"),
        ("slack", "C0B0QV5434G", "C0B0QV5434G"),
        ("email", "ops@example.com", "ops@example.com"),
    ]
    # Patch the directory lookup so no channel_directory.json under ~/.hermes is
    # read; the branches under test are pure string grammar (_parse_target_ref).
    with patch("gateway.channel_directory.resolve_channel_name", return_value=None):
        for platform, ref, expected in cases:
            chat_id, _thread_id, err = resolve_send_target(platform, ref)
            assert err is None, f"{platform}:{ref!r} unexpectedly errored: {err!r}"
            assert chat_id == expected, f"{platform}:{ref!r} -> {chat_id!r}, expected {expected!r}"


def test_ac_rt_f07_2(tmp_path):
    """The platform:#channel form resolves through resolve_send_target into the on-disk channel_directory discovery cache (engineering and #engineering -> its id), and a missing directory file yields an empty directory."""
    directory = tmp_path / "channel_directory.json"
    directory.write_text(
        json.dumps(
            {
                "updated_at": "2026-01-01T00:00:00",
                "platforms": {"slack": [{"id": "C0ENG", "name": "engineering", "type": "channel"}]},
            }
        ),
        encoding="utf-8",
    )
    aliases = tmp_path / "none.json"  # missing on purpose -> no ~/.hermes alias read
    with patch("gateway.channel_directory.DIRECTORY_PATH", directory), patch(
        "gateway.channel_directory.CHANNEL_ALIASES_PATH", aliases
    ):
        # Directly against the cache resolver ...
        assert resolve_channel_name("slack", "engineering") == "C0ENG"
        assert resolve_channel_name("slack", "#engineering") == "C0ENG"
        # ... and through the public send-target resolver, which delegates a
        # human-friendly name to the directory (send_message_tool.py:728-730).
        chat_id, _t, err = resolve_send_target("slack", "engineering")
        assert err is None and chat_id == "C0ENG"
        chat_id, _t, err = resolve_send_target("slack", "#engineering")
        assert err is None and chat_id == "C0ENG"

    missing = tmp_path / "nope.json"
    with patch("gateway.channel_directory.DIRECTORY_PATH", missing), patch(
        "gateway.channel_directory.CHANNEL_ALIASES_PATH", aliases
    ):
        assert load_directory()["platforms"] == {}


def test_ac_rt_f07_3():
    """On Slack's live adapter the cold-DM cache opens a DM for a bare user id once per (team,user) on a cold miss, serves the cached D... id on a repeat without re-opening, opens again for a different team, and passes a conversation id through with no open."""
    # Function-local import: if the Slack SDK extra is absent when this file is
    # run in isolation, only this criterion errors (the pure ones still run).
    from plugins.platforms.slack.adapter import SlackAdapter

    adapter = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-fake"))
    adapter._app = MagicMock()
    adapter._app.client = AsyncMock()
    adapter._app.client.conversations_open = AsyncMock(
        return_value={"ok": True, "channel": {"id": "D999"}}
    )

    async def _flow():
        t1_first = await adapter._ensure_dm_conversation("U123ABCDEF", team_id="T1")
        t1_second = await adapter._ensure_dm_conversation("U123ABCDEF", team_id="T1")
        t2 = await adapter._ensure_dm_conversation("U123ABCDEF", team_id="T2")
        passthrough = await adapter._ensure_dm_conversation("C0B0QV5434G")
        return t1_first, t1_second, t2, passthrough

    t1_first, t1_second, t2, passthrough = asyncio.run(_flow())

    assert t1_first == "D999"
    assert t1_second == "D999"
    assert t2 == "D999"
    assert passthrough == "C0B0QV5434G"
    # One open per (team,user): (T1,U123ABCDEF) once + (T2,U123ABCDEF) once; the
    # T1 repeat and the conversation-id passthrough add none.
    assert adapter._app.client.conversations_open.await_count == 2

    # The public SlackAdapter.send() (used by the kanban notifier and cron
    # delivery) wires to the cache: a refactor dropping the _ensure_dm_conversation
    # call at adapter.py:2935 would silently break cold-DM delivery. Prove the
    # wiring at that seam; send()'s downstream post path (slash/ephemeral/post) is
    # out of scope and not driven — the try/except tolerates it, since the cache
    # call happens first.
    adapter2 = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-fake"))
    adapter2._app = MagicMock()
    adapter2._app.client = AsyncMock()
    with patch.object(
        adapter2, "_ensure_dm_conversation", AsyncMock(return_value="D999")
    ) as ensure_spy:
        try:
            asyncio.run(adapter2.send("U123ABCDEF", "hi"))
        except Exception:
            pass
    ensure_spy.assert_called()


def test_ac_rt_f07_4():
    """Hermes parses a per-platform HomeChannel fallback preserving user_id/scope_id provenance, GatewayConfig.get_home_channel returns it, and the `hermes send` path consumes it: a bare-platform send routes the home chat_id to _send_to_platform, and with no home channel it returns the documented error (the getter proven called)."""
    hc = HomeChannel.from_dict(
        {
            "platform": "slack",
            "chat_id": "C0HOME",
            "name": "Home",
            "user_id": "U0OWNER",
            "scope_id": "T0TEAM",
        }
    )
    assert hc.platform is Platform.SLACK
    assert hc.chat_id == "C0HOME"
    assert hc.user_id == "U0OWNER"
    assert hc.scope_id == "T0TEAM"

    pc = PlatformConfig.from_dict(
        {"enabled": True, "home_channel": {"platform": "slack", "chat_id": "C0HOME", "name": "Home"}}
    )
    assert isinstance(pc.home_channel, HomeChannel)
    assert pc.home_channel.chat_id == "C0HOME"

    gw = GatewayConfig(platforms={Platform.SLACK: pc})
    assert gw.get_home_channel(Platform.SLACK) is pc.home_channel

    from tools.send_message_tool import send_message_tool

    # Positive: a bare "slack" target (no explicit chat) falls back to the home
    # channel, and the home chat_id routes to the send. _send_to_platform is
    # mocked so nothing leaves the process; mirror_to_session is mocked so no
    # session state is written. The getter is wrapped to prove _handle_send
    # actually calls it (a hardcoded None could not pass this).
    send_mock = AsyncMock(return_value={"success": True})
    with patch.object(
        gw, "get_home_channel", wraps=gw.get_home_channel
    ) as gh_spy, patch(
        "tools.send_message_tool.prepare_send_message_platforms"
    ), patch(
        "tools.interrupt.is_interrupted", return_value=False
    ), patch(
        "gateway.config.load_gateway_config", return_value=gw
    ), patch(
        "tools.send_message_tool._send_to_platform", send_mock
    ), patch(
        "gateway.mirror.mirror_to_session", MagicMock(return_value=False)
    ):
        out = send_message_tool({"action": "send", "target": "slack", "message": "hello"})
    gh_spy.assert_called_with(Platform.SLACK)
    assert send_mock.await_args is not None, "the send path never reached _send_to_platform"
    assert send_mock.await_args.args[2] == "C0HOME"
    assert "C0HOME" in out

    # Negative: no home channel and no explicit target -> the documented error,
    # returned before any network send (send_message_tool.py:459-467).
    gw_no_home = GatewayConfig(
        platforms={Platform.SLACK: PlatformConfig(enabled=True, token="xoxb-fake")}
    )
    with patch("tools.send_message_tool.prepare_send_message_platforms"), patch(
        "tools.interrupt.is_interrupted", return_value=False
    ), patch("gateway.config.load_gateway_config", return_value=gw_no_home):
        out = send_message_tool({"action": "send", "target": "slack", "message": "hello"})
    assert "No home channel set for slack" in out


def test_ac_rt_f07_5():
    """The NanoClaw-parity doc section maps the cold-DM cache / outbound addressing behaviour onto the Hermes surface and its cron/kanban/hermes-send consumers, and cites them (resolve_send_target, _ensure_dm_conversation, _resolve_slack_user_target, HomeChannel, channel_directory, send_cmd.py, cron/scheduler.py, kanban) in tag:/main: form."""
    doc = (
        Path(__file__).parents[2]
        / "website/docs/developer-guide/gateway-internals.md"
    ).read_text(encoding="utf-8")
    heading = "## NanoClaw parity: cold-DM cache & outbound addressing"
    idx = doc.find(heading)
    assert idx != -1, f"parity section heading missing from gateway-internals.md: {heading!r}"
    section = doc[idx:]
    # Bound the slice at the next top-level heading so an unrelated later section
    # cannot satisfy a citation needle on this section's behalf.
    nxt = section.find("\n## ", len(heading))
    if nxt != -1:
        section = section[:nxt]
    for needle in (
        # core surface
        "tools/send_message_tool.py",
        "resolve_send_target",
        "_parse_target_ref",
        "plugins/platforms/slack/adapter.py",
        "_ensure_dm_conversation",
        "_resolve_slack_user_target",
        "gateway/channel_directory.py",
        "resolve_channel_name",
        "gateway/config.py",
        "HomeChannel",
        "get_home_channel",
        # consumers (the requirement's "used by")
        "hermes_cli/send_cmd.py",
        "cmd_send",
        "cron/scheduler.py",
        "_get_config_home_channel",
        "_resolve_single_delivery_target",
        "gateway/kanban_watchers.py",
        "_kanban_notifier_watcher",
        "plugins/kanban/dashboard/plugin_api.py",
        "_configured_home_channels",
        "subscribe_home",
        "SlackAdapter.send",
        # citation markers
        "tag:",
        "main:",
    ):
        assert needle in section, f"parity section must cite {needle!r}"


def test_ac_rt_f07_6():
    """The send_message tool re-resolves a Slack user target via _resolve_slack_user_target on every call (Slack conversations.open per call), sharing no cache with the adapter."""
    from tools.send_message_tool import _resolve_slack_user_target, send_message_tool

    # Stub aiohttp so the REAL _resolve_slack_user_target runs offline: it does
    # `async with aiohttp.ClientSession(...) as session:` then
    # `async with session.post(f"{base}/conversations.open", ...) as resp:` then
    # `await resp.json()` (send_message_tool.py:1927-1976). A user:<id> target
    # goes straight to conversations.open (no users.list).
    def _make_session():
        resp = MagicMock()
        resp.json = AsyncMock(return_value={"ok": True, "channel": {"id": "D999"}})
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=None)
        session = MagicMock()
        session.post = MagicMock(return_value=resp)
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        return session

    session = _make_session()
    resolve_spy = AsyncMock(wraps=_resolve_slack_user_target)
    send_mock = AsyncMock(return_value={"success": True})
    gw = GatewayConfig(platforms={Platform.SLACK: PlatformConfig(enabled=True, token="xoxb-fake")})
    with patch("aiohttp.ClientSession", return_value=session), patch(
        "tools.send_message_tool.prepare_send_message_platforms"
    ), patch("tools.interrupt.is_interrupted", return_value=False), patch(
        "gateway.config.load_gateway_config", return_value=gw
    ), patch(
        "tools.send_message_tool._resolve_slack_user_target", resolve_spy
    ), patch(
        "tools.send_message_tool._send_to_platform", send_mock
    ), patch("gateway.mirror.mirror_to_session", MagicMock(return_value=False)):
        send_message_tool({"action": "send", "target": "slack:U01234567", "message": "hi"})
        send_message_tool({"action": "send", "target": "slack:U01234567", "message": "hi again"})

    assert resolve_spy.await_count == 2
    conv_open_posts = sum(
        str(call.args[0]).endswith("/conversations.open")
        for call in session.post.call_args_list
    )
    assert conv_open_posts == 2
