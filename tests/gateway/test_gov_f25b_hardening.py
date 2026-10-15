"""GOV-F25.b — failing-on-base acceptance tests for the three GOV-F25 core hardening fixes.

One test per pytest acceptance criterion; the AC-id -> node mapping is mechanical:
AC-GOV-F25.b-<n> -> test_ac_gov_f25_b_<n>.

Behaviour contracts, not snapshots: no model lists / version literals / enum counts;
no source files read; no network beyond loopback aiohttp test servers; HERMES_HOME is
sandboxed by tests/conftest.py.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

OWNER_SESS = "gov-f25b-owner-sess-0"
OWNER_PROFILE = "gov-f25b-owner"


# --------------------------------------------------------------------------- #
# Durable-notifier harness (mirrors tests/gateway/test_plugin_message_injection.py;
# the delivery dict shape, incl. old_cursor/cursor, mirrors kanban_watchers.py:499).
# --------------------------------------------------------------------------- #
def _mk_task_and_durable_sub(platform="api_server", chat_id=OWNER_SESS,
                             retry_policy="durable"):
    import hermes_cli.kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_notify as kbn

    conn = kbc.connect()
    try:
        task = kb.create_task(
            conn, title="gov-f25b/repo#41", assignee=OWNER_PROFILE,
            idempotency_key="gh-pr-gov-f25b-41",
        )
        task_id = task if isinstance(task, str) else getattr(task, "id", task)
        kbn.add_notify_sub(
            conn, task_id=task_id, platform=platform, chat_id=chat_id,
            notifier_profile=OWNER_PROFILE, delivery_mode="wake",
            retry_policy=retry_policy,
        )
    finally:
        conn.close()
    return task_id


def _publish(task_id, message, key):
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_notify as kbn

    conn = kbc.connect()
    try:
        kbn.publish_task_notification(
            conn, task_id, message, metadata={"idempotency_key": key},
        )
    finally:
        conn.close()


def _peek_delivery(task_id):
    """The delivery view a drainer receives: unseen events with the pre-delivery
    cursor snapshot (old_cursor), the range end (cursor), and no claim taken."""
    import hermes_cli.kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_notify as kbn

    conn = kbc.connect()
    try:
        sub = kbn.list_notify_subs(conn, task_id=task_id)[0]
        cur, events = kbn.unseen_events_for_sub(
            conn, task_id=sub["task_id"], platform=sub["platform"],
            chat_id=sub["chat_id"], thread_id=sub.get("thread_id") or "", kinds=None,
        )
        task = kb.get_task(conn, task_id)
    finally:
        conn.close()
    return {"sub": sub, "old_cursor": int(sub.get("last_event_id") or 0),
            "cursor": cur, "events": events, "task": task, "board": None,
            "durable": True}


def _serve_owner_profile(monkeypatch):
    """Multiplex home serving the route-only secondary owner, whose own state.db
    holds the owner session — the tag's ownership proof for a stateless api_server
    destination (``kanban_watchers_notifier._adapter_for_subscription``)."""
    from pathlib import Path

    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB

    root = Path(get_hermes_home())
    owner = root / "profiles" / OWNER_PROFILE
    owner.mkdir(parents=True, exist_ok=True)
    (owner / "config.yaml").write_text("{}\n", encoding="utf-8")
    db = SessionDB(owner / "state.db")
    try:
        db.create_session(OWNER_SESS, source="webui", profile_name=OWNER_PROFILE)
    finally:
        db.close()
    monkeypatch.setattr("hermes_constants.get_default_hermes_root", lambda: root)
    return owner


def _durable_runner():
    from gateway.config import Platform
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    # The shared api_server adapter is the only api_server credential; a route-only
    # secondary owner is served through it once its own store proves the session.
    runner.adapters = {Platform.API_SERVER: SimpleNamespace(
        _host="127.0.0.1", _port=8642, _api_key="k", _model_name="hermes-agent",
        supports_async_delivery=False,  # api_server is non-push → self-post branch
    )}
    runner._profile_adapters = {OWNER_PROFILE: {}}
    runner._profile_failed_platforms = {}
    runner._primary_profile_name = "default"
    runner._kanban_notifier_profile = "default"
    runner.config = SimpleNamespace(multiplex_profiles=True, profile_routes=[])
    runner._kanban_sub_fail_counts = {}
    runner._kanban_durable_backoff = {}
    return runner


# --------------------------------------------------------------------------- #
# AC-GOV-F25.b-1 — atomic durable claim: exactly-once under concurrent drainers
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_ac_gov_f25_b_1(monkeypatch):
    """On the durable path, two concurrent drainers observing the same not-yet-acked
    notification event deliver it exactly once (one deliver_wake), and the cursor
    advances once."""
    _serve_owner_profile(monkeypatch)
    task_id = _mk_task_and_durable_sub()
    _publish(task_id, "review submitted on o/r#41 @beefcafe", "o/r#41:D1")

    calls = []
    a_in_delivery = asyncio.Event()
    release_a = asyncio.Event()

    async def _deliver(adapter, *, text, session_id, profile=None,
                       idempotency_key=None, require_persist_ack=None):
        calls.append(idempotency_key)
        if len(calls) == 1:
            # Hold the first drainer inside delivery — its claim taken, cursor not yet
            # advanced — while the competing drainer attempts the same event.
            a_in_delivery.set()
            await asyncio.wait_for(release_a.wait(), timeout=5)

    monkeypatch.setattr("gateway.wake.deliver_wake", _deliver)

    # Both drainers observe the one unseen event at the same pre-delivery cursor.
    d_a = _peek_delivery(task_id)
    d_b = _peek_delivery(task_id)
    assert len(d_a["events"]) == 1 and len(d_b["events"]) == 1

    task_a = asyncio.create_task(
        _durable_runner()._deliver_durable_notifications(d_a))
    await asyncio.wait_for(a_in_delivery.wait(), timeout=5)
    # Drainer B competes for the same event while A still holds it undelivered.
    await _durable_runner()._deliver_durable_notifications(d_b)
    release_a.set()
    await asyncio.wait_for(task_a, timeout=5)

    assert calls == ["o/r#41:D1"], (
        f"exactly one delivery expected across two concurrent drainers; got {calls}"
    )
    assert _peek_delivery(task_id)["events"] == []


# --------------------------------------------------------------------------- #
# AC-GOV-F25.b-2 — cache_if opt-in: persist-gating only when the caller asks,
# and the durable caller (wake.py) is the one that opts in.
# --------------------------------------------------------------------------- #
def _chat_app():
    """A minimal aiohttp app exposing just the chat/completions handler, the shape of
    tests/gateway/test_api_server.py:_create_app."""
    from aiohttp import web

    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import (
        APIServerAdapter,
        cors_middleware,
        security_headers_middleware,
    )

    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    mws = [mw for mw in (cors_middleware, security_headers_middleware) if mw is not None]
    app = web.Application(middlewares=mws)
    app["api_server_adapter"] = adapter
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    return adapter, app


def _unpersisted_run_agent(counter):
    async def _mock_run_agent(**kwargs):
        counter["n"] += 1
        return (
            {"final_response": "ok", "turn_persisted": False,
             "messages": [], "api_calls": 1},
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )
    return _mock_run_agent


async def _wake_request_headers(*, require_persist_ack):
    """Drive gateway.wake.deliver_wake against a loopback server and return the request
    header value it sent for X-Hermes-Require-Persist (None if absent)."""
    from aiohttp import web

    from gateway.wake import deliver_wake

    seen = {}

    async def handler(request):
        seen["require_persist"] = request.headers.get("X-Hermes-Require-Persist")
        return web.json_response(
            {"choices": [{"message": {"content": "ok"}}]},
            headers={"X-Hermes-Turn-Persisted": "true"},
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        adapter = SimpleNamespace(_host="127.0.0.1", _port=port, _api_key="k",
                                  _model_name="hermes-agent",
                                  supports_async_delivery=False)
        await deliver_wake(adapter, text="x", session_id="sid",
                           require_persist_ack=require_persist_ack)
    finally:
        await runner.cleanup()
    return seen.get("require_persist")


@pytest.mark.asyncio
async def test_ac_gov_f25_b_2():
    """After an unpersisted 200, a same-Idempotency-Key retry is served from cache when
    the request omits X-Hermes-Require-Persist (upstream default caching), and re-runs
    only when the request opts in with X-Hermes-Require-Persist:1 (which the durable
    wake caller sends)."""
    from aiohttp.test_utils import TestClient, TestServer

    body = {"model": "hermes-agent",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False}

    # (A) No opt-in header: the same-key retry after an unpersisted 200 is served from
    # cache — the agent runs exactly once.
    adapter, app = _chat_app()
    counter_a = {"n": 0}
    async with TestClient(TestServer(app)) as cli:
        with patch.object(adapter, "_run_agent",
                          side_effect=_unpersisted_run_agent(counter_a)):
            r1 = await cli.post("/v1/chat/completions", json=body,
                                headers={"Idempotency-Key": "k-nohdr"})
            assert r1.status == 200
            r2 = await cli.post("/v1/chat/completions", json=body,
                                headers={"Idempotency-Key": "k-nohdr"})
            assert r2.status == 200
    assert counter_a["n"] == 1, (
        f"no-header retry must be served from cache (upstream default); ran "
        f"{counter_a['n']}x"
    )

    # (B) Opt-in header: the unpersisted turn is not cached, so the same-key retry
    # re-runs — the agent runs twice.
    adapter, app = _chat_app()
    counter_b = {"n": 0}
    async with TestClient(TestServer(app)) as cli:
        with patch.object(adapter, "_run_agent",
                          side_effect=_unpersisted_run_agent(counter_b)):
            hdrs = {"Idempotency-Key": "k-hdr", "X-Hermes-Require-Persist": "1"}
            r1 = await cli.post("/v1/chat/completions", json=body, headers=hdrs)
            assert r1.status == 200
            r2 = await cli.post("/v1/chat/completions", json=body, headers=hdrs)
            assert r2.status == 200
    assert counter_b["n"] == 2, (
        f"opt-in unpersisted turn must not be cached, so the retry re-runs; ran "
        f"{counter_b['n']}x"
    )

    # (C) The opt-in is actually wired: the durable caller sends the header, a default
    # caller does not.
    assert await _wake_request_headers(require_persist_ack=True) == "1"
    assert await _wake_request_headers(require_persist_ack=False) is None


# --------------------------------------------------------------------------- #
# AC-GOV-F25.b-3 — durable policy restricted to a confirmable transport
# --------------------------------------------------------------------------- #
def test_ac_gov_f25_b_3():
    """add_notify_sub refuses retry_policy='durable' on a push-capable
    (non-api_server) platform with a clear error, and accepts it on api_server."""
    import hermes_cli.kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_notify as kbn

    conn = kbc.connect()
    try:
        task = kb.create_task(
            conn, title="gov-f25b/repo#1", assignee=OWNER_PROFILE,
            idempotency_key="gh-pr-gov-f25b-1",
        )
        task_id = task if isinstance(task, str) else getattr(task, "id", task)

        # api_server is the sole confirmable transport -> durable accepted.
        kbn.add_notify_sub(
            conn, task_id=task_id, platform="api_server", chat_id=OWNER_SESS,
            notifier_profile=OWNER_PROFILE, delivery_mode="wake",
            retry_policy="durable",
        )
        subs = [s for s in kbn.list_notify_subs(conn, task_id=task_id)
                if s["platform"] == "api_server"]
        assert subs and subs[0]["retry_policy"] == "durable"

        # A push-capable transport cannot confirm persistence -> durable is refused
        # with a clear, surfaced error.
        with pytest.raises(ValueError) as exc:
            kbn.add_notify_sub(
                conn, task_id=task_id, platform="telegram", chat_id="tg-chat-1",
                notifier_profile=OWNER_PROFILE, delivery_mode="wake",
                retry_policy="durable",
            )
        msg = str(exc.value).lower()
        assert "durable" in msg and "api_server" in msg, (
            f"the refusal must name the policy and the confirmable transport; got "
            f"{exc.value!r}"
        )
    finally:
        conn.close()
