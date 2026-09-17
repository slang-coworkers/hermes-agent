"""Acceptance tests for RT-F05 admission and unknown-sender policy."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for var in (
        "TELEGRAM_ALLOWED_USERS",
        "TELEGRAM_ALLOW_ALL_USERS",
        "TELEGRAM_GROUP_ALLOWED_USERS",
        "TELEGRAM_GROUP_ALLOWED_CHATS",
        "WHATSAPP_ALLOWED_USERS",
        "WHATSAPP_ALLOW_ALL_USERS",
        "WHATSAPP_CLOUD_ALLOW_ALL_USERS",
        "GATEWAY_ALLOW_ALL_USERS",
        "GATEWAY_ALLOWED_USERS",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / ".hermes").mkdir(parents=True, exist_ok=True)
    from gateway.pairing import PairingStore

    return PairingStore()


def _make_source(
    user_id="stranger", chat_type="dm", platform=Platform.TELEGRAM, profile=None
):
    return SessionSource(
        platform=platform,
        chat_id="123",
        chat_type=chat_type,
        user_id=user_id,
        user_name="SomeHuman",
        is_bot=False,
        profile=profile,
    )


def _authz_runner(*, pairing_store=None, config=None):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.pairing_store = (
        pairing_store
        if pairing_store is not None
        else SimpleNamespace(is_approved=lambda *_a, **_kw: False)
    )
    runner.config = config if config is not None else GatewayConfig()
    runner.adapters = {}
    return runner


def _handle_runner(platform, config, *, pairing_store=None):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = config
    adapter = SimpleNamespace(send=AsyncMock())
    runner.adapters = {platform: adapter}
    if pairing_store is not None:
        runner.pairing_store = pairing_store
    else:
        runner.pairing_store = MagicMock()
        runner.pairing_store.is_approved.return_value = False
        runner.pairing_store._is_rate_limited.return_value = False
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._update_prompts = {}
    runner.hooks = SimpleNamespace(dispatch=AsyncMock(return_value=None))
    runner._sessions = {}
    return runner, adapter


def _make_event(
    platform, user_id, chat_id, *, chat_type="dm", profile=None, text="hello"
):
    return MessageEvent(
        text=text,
        message_id="m1",
        source=SessionSource(
            platform=platform,
            user_id=user_id,
            chat_id=chat_id,
            user_name="tester",
            chat_type=chat_type,
            profile=profile,
        ),
    )


def test_ac_rt_f05_1(monkeypatch):
    """Strict: an unknown DM sender not on the allowlist and holding no pairing grant is denied, and the resolved unauthorized-DM behaviour is 'ignore'."""
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "owner1,owner2")
    runner = _authz_runner()
    assert runner._is_user_authorized(_make_source("stranger")) is False
    assert runner._is_user_authorized(_make_source("owner1")) is True
    assert runner._get_unauthorized_dm_behavior(Platform.TELEGRAM) == "ignore"
    monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
    assert _authz_runner()._get_unauthorized_dm_behavior(Platform.TELEGRAM) == "pair"


def test_ac_rt_f05_2(store, monkeypatch):
    """Request-approval: under 'pair' an unknown DM sender is minted a pending pairing code and is not yet authorized; 'pair' is the default without an allowlist and requires an explicit per-platform override alongside one."""
    code = store.generate_code("telegram", "newuser", "Alice")
    assert code is not None
    assert any(p["user_id"] == "newuser" for p in store.list_pending("telegram"))
    assert store.is_approved("telegram", "newuser") is False
    assert _authz_runner()._get_unauthorized_dm_behavior(Platform.TELEGRAM) == "pair"
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "owner1")
    override_cfg = GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(
                extra={"unauthorized_dm_behavior": "pair"}
            )
        }
    )
    assert (
        _authz_runner(config=override_cfg)._get_unauthorized_dm_behavior(
            Platform.TELEGRAM
        )
        == "pair"
    )


def test_ac_rt_f05_3(tmp_path, monkeypatch):
    """Approval unions the grant into the authorized set for that profile: after approve the sender is authorized in the approving profile even when absent from the env allowlist, and stays denied in another profile."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / ".hermes").mkdir(parents=True, exist_ok=True)
    from gateway.pairing import PairingStore

    alpha = PairingStore(profile="alpha")
    beta = PairingStore(profile="beta")
    result = alpha.approve_code(
        "telegram", alpha.generate_code("telegram", "newuser", "Alice")
    )
    assert result is not None and result["user_id"] == "newuser"
    assert alpha.is_approved("telegram", "newuser") is True
    assert beta.is_approved("telegram", "newuser") is False

    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "owner1")
    runner = _authz_runner()
    runner.pairing_stores = {"alpha": alpha, "beta": beta}
    assert runner._is_user_authorized(_make_source("newuser", profile="alpha")) is True
    assert runner._is_user_authorized(_make_source("newuser", profile="beta")) is False
    assert (
        runner._is_user_authorized(_make_source("interloper", profile="alpha")) is False
    )


def test_ac_rt_f05_4(monkeypatch):
    """Public: the allow-all flag authorizes an unknown DM sender with no approval; an open/unconfigured gateway (no allowlist, no flag) does not, and the per-platform and global flags differ in precedence."""
    assert _authz_runner()._is_user_authorized(_make_source("anyone")) is False
    monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "owner1")
    monkeypatch.setenv("TELEGRAM_ALLOW_ALL_USERS", "true")
    assert _authz_runner()._is_user_authorized(_make_source("anyone")) is True
    monkeypatch.delenv("TELEGRAM_ALLOW_ALL_USERS", raising=False)
    monkeypatch.setenv("GATEWAY_ALLOW_ALL_USERS", "true")
    assert _authz_runner()._is_user_authorized(_make_source("anyone")) is False
    monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
    assert _authz_runner()._is_user_authorized(_make_source("anyone")) is True


def test_ac_rt_f05_5():
    """Panel: the dashboard route POST /api/pairing/approve grants the selected pending pairing so that sender becomes approved and no other does."""
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.fail(
            "AC-RT-F05-5 needs the web extra (fastapi/starlette); run with --extra all"
        )
    import hermes_state
    from hermes_constants import get_hermes_home
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN
    from gateway.pairing import PairingStore

    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    hermes_state.DEFAULT_DB_PATH = get_hermes_home() / "state.db"

    store = PairingStore()
    assert store.generate_code("telegram", "user1", "Alice") is not None
    assert store.generate_code("telegram", "user2", "Bob") is not None
    pending = {
        p["user_id"]: p["request_id"]
        for p in client.get("/api/pairing").json()["pending"]
    }

    r = client.post(
        "/api/pairing/approve",
        json={"platform": "telegram", "request_id": pending["user1"]},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["user"]["user_id"] == "user1"
    assert store.is_approved("telegram", "user1") is True
    assert store.is_approved("telegram", "user2") is False


@pytest.mark.asyncio
async def test_ac_rt_f05_6():
    """Accepted difference: an unauthorized GROUP message is silently ignored — no pairing code minted, no reply sent."""
    config = GatewayConfig(platforms={Platform.WHATSAPP: PlatformConfig(enabled=True)})
    runner, adapter = _handle_runner(Platform.WHATSAPP, config)
    runner.pairing_store.generate_code.return_value = "ABC12DEF"
    result = await runner._handle_message(
        _make_event(
            Platform.WHATSAPP,
            "15551234567@s.whatsapp.net",
            "group-1",
            chat_type="group",
        )
    )
    assert result is None
    runner.pairing_store.generate_code.assert_not_called()
    adapter.send.assert_not_awaited()

    runner2, adapter2 = _handle_runner(Platform.WHATSAPP, config)
    runner2.pairing_store.generate_code.return_value = "ABC12DEF"
    result2 = await runner2._handle_message(
        _make_event(
            Platform.WHATSAPP,
            "15551234567@s.whatsapp.net",
            "15551234567@s.whatsapp.net",
            chat_type="dm",
        )
    )
    assert result2 is None
    runner2.pairing_store.generate_code.assert_called_once()
    adapter2.send.assert_awaited_once()
    assert "ABC12DEF" in adapter2.send.await_args.args[1]


@pytest.mark.asyncio
async def test_ac_rt_f05_7(store):
    """Accepted difference: the triggering message is never captured for replay — routing the marked DM drops it and its body is retained neither in the pending record nor, after approval, in the approved record or the approval return."""
    config = GatewayConfig(platforms={Platform.WHATSAPP: PlatformConfig(enabled=True)})
    runner, adapter = _handle_runner(Platform.WHATSAPP, config, pairing_store=store)
    marker = "REPLAY-MARKER-9f3c2a"
    result = await runner._handle_message(
        _make_event(
            Platform.WHATSAPP,
            "15551234567@s.whatsapp.net",
            "15551234567@s.whatsapp.net",
            chat_type="dm",
            text=marker,
        )
    )
    assert result is None
    assert marker not in store._pending_path("whatsapp").read_text(encoding="utf-8")

    request_id = store.list_pending("whatsapp")[0]["request_id"]
    approved = store.approve_request("whatsapp", request_id)
    assert approved is not None
    assert marker not in repr(approved)
    assert marker not in store._approved_path("whatsapp").read_text(encoding="utf-8")
