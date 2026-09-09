"""Transport + canonical-Bot-Chat behavior tests for nv-coworker-compose.

Cover the JSON-RPC envelope normalization, both groups.state response shapes,
the string->member-object wire adaptation, the WebSocket requester's
ready-frame consumption and id correlation, the in-process-vs-standalone
dispatch routing, onboarding honesty (ok:false on a failed required step), the
tool handlers' never-raise contract, and the canonical Bot Chat lifecycle. The
plugin loads through the real discovery path (same shape as
test_plugin_api_compat.py).
"""

import asyncio
import inspect
import json
from pathlib import Path
import shutil

import pytest
import yaml

from hermes_cli.plugins import PluginManager

PLUGIN_KEY = "nv-coworker-compose"
PLUGIN_SRC = Path(__file__).resolve().parents[2] / "plugins" / PLUGIN_KEY
FIXTURE_SPEC = Path(__file__).parent / "fixtures" / "loop-f35" / "coworker-types.yaml"


def _load(tmp_path, monkeypatch):
    home = tmp_path / "hermes-root" / "hermes-home"
    plugins_dir = home / "plugins"
    plugins_dir.mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, plugins_dir / PLUGIN_KEY)
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": [PLUGIN_KEY]}}), encoding="utf-8"
    )
    empty_bundled = tmp_path / "bundled-plugins"
    empty_bundled.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(empty_bundled))
    manager = PluginManager()
    manager.discover_and_load()
    return manager._plugins[PLUGIN_KEY].module


def _onboard_ok_envelope(method):
    """A realistic per-method success envelope that drives a full onboard to
    ok:True. Onboarding confirms each step, so a blanket ``{"ok": True}`` is not
    enough: session.list must be empty and session.create must return an id for
    the canonical Bot Chat to be confirmed, and groups.state must report an
    absent room for the standing room to be created."""
    if method == "profiles.list":
        return {"result": {"profiles": []}}
    if method == "session.list":
        return {"result": {"sessions": []}}
    if method == "session.create":
        return {"result": {"session_id": "sess-1"}}
    if method == "groups.state":
        return {"result": {"exists": False}}
    return {"result": {"ok": True}}


def test_unwrap_rpc_envelope(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    assert mod._unwrap_rpc_envelope({"jsonrpc": "2.0", "id": 1, "result": {"x": 1}}) == {"x": 1}
    assert mod._unwrap_rpc_envelope({"ok": True}) == {"ok": True}  # bare-fake passthrough
    assert mod._unwrap_rpc_envelope(None) is None
    with pytest.raises(mod.RpcError):
        mod._unwrap_rpc_envelope({"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "no"}})


def test_room_exists_both_shapes(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    assert mod._room_exists({"exists": True}) is True          # frozen fake shape
    assert mod._room_exists({"room": {"id": "r"}}) is True      # production shape
    assert mod._room_exists({"exists": False}) is False
    assert mod._room_exists({"ok": True}) is False
    assert mod._room_exists(None) is False


def test_onboard_no_url_dispatches_in_process_without_preflight(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "_install", lambda *a, **k: None, raising=True)

    def _boom(url):
        raise AssertionError("preflight must not run on the in-process path")

    monkeypatch.setattr(mod, "_gateway_preflight", _boom, raising=True)

    seen = []

    def _handle(request):
        seen.append(request["method"])
        return _onboard_ok_envelope(request["method"])

    monkeypatch.setattr(mod, "_gateway_handle_request", _handle, raising=True)

    result = mod.onboard_coworker(str(FIXTURE_SPEC), settings={})
    assert result.get("ok") is True
    # In-process path reached the gateway handler for the real onboarding methods.
    assert "profiles.configure" in seen
    assert "groups.create" in seen


def test_onboard_standalone_routes_through_ws_requester(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "_install", lambda *a, **k: None, raising=True)
    monkeypatch.setattr(mod, "_gateway_preflight", lambda url: (True, "ok"), raising=True)

    def _no_in_process(request):
        raise AssertionError("standalone path must not use in-process dispatch")

    monkeypatch.setattr(mod, "_gateway_handle_request", _no_in_process, raising=True)

    ws_methods = []
    opened = []

    def _fake_requester(request):
        ws_methods.append(request["method"])
        return _onboard_ok_envelope(request["method"])

    def _fake_open(url):
        opened.append(url)
        return _fake_requester

    monkeypatch.setattr(mod, "_open_ws_requester", _fake_open, raising=True)

    result = mod.onboard_coworker(str(FIXTURE_SPEC), settings={"gateway_url": "ws://127.0.0.1:9/api/ws?token=t"})
    assert result.get("ok") is True
    assert opened == ["ws://127.0.0.1:9/api/ws?token=t"]  # requester opened once, reused
    assert "profiles.configure" in ws_methods and "groups.create" in ws_methods


def test_ensure_canonical_bot_chat_production_and_bare(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)

    # Production-shaped responses: create returns a session_id; title must be
    # called with exactly that id to materialise the durable row.
    calls = []

    def _prod_rpc(method, params):
        calls.append((method, params))
        if method == "session.list":
            return {"sessions": []}
        if method == "session.create":
            return {"session_id": "sess-42"}
        return {"ok": True}

    monkeypatch.setattr(mod, "_dispatch_rpc", _prod_rpc, raising=True)
    assert mod._ensure_canonical_bot_chat("reviewer") is True
    titles = [p for m, p in calls if m == "session.title"]
    assert len(titles) == 1 and titles[0].get("session_id") == "sess-42"

    # Permissive bare response (no session_id): must not crash, reports unconfirmed.
    def _bare_rpc(method, params):
        return {"ok": True}

    monkeypatch.setattr(mod, "_dispatch_rpc", _bare_rpc, raising=True)
    assert mod._ensure_canonical_bot_chat("fixer") is False


class _FakeConn:
    """A scripted WebSocket connection: recv() returns queued frames in order."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.sent = []
        self.closed = False

    def recv(self, timeout=None):
        return self._frames.pop(0)

    def send(self, payload):
        self.sent.append(payload)

    def close(self):
        self.closed = True


def test_ws_requester_consumes_ready_and_correlates_ids(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    ready = {"jsonrpc": "2.0", "method": "gateway.ready", "params": {}}
    unsolicited = {"jsonrpc": "2.0", "method": "room.message", "params": {"x": 1}}  # no id
    matched = {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}
    conn = _FakeConn([json.dumps(ready), json.dumps(unsolicited), json.dumps(matched)])
    monkeypatch.setattr(mod, "_ws_connect", lambda url: conn, raising=True)

    requester = mod._open_ws_requester("ws://127.0.0.1:9/api/ws?token=t")
    # gateway.ready was consumed at open, not treated as a response.
    envelope = requester({"jsonrpc": "2.0", "id": 7, "method": "m", "params": {}})
    assert envelope == matched
    # Only the request was sent; the id-less broadcast was skipped, id 7 correlated.
    assert len(conn.sent) == 1
    assert json.loads(conn.sent[0])["id"] == 7


def test_ticket_reuses_preflight_socket(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "_install", lambda *a, **k: None, raising=True)

    used = []

    def _requester(request):
        used.append(request["method"])
        return _onboard_ok_envelope(request["method"])

    def _fake_preflight(url):
        # A real preflight opens the socket and retains its requester so the
        # single-use ticket is not spent twice.
        mod._pending_ws_requester = _requester
        return (True, "ok")

    monkeypatch.setattr(mod, "_gateway_preflight", _fake_preflight, raising=True)

    def _must_not_open(url):
        raise AssertionError("preflight socket must be reused, not reopened")

    monkeypatch.setattr(mod, "_open_ws_requester", _must_not_open, raising=True)

    result = mod.onboard_coworker(
        str(FIXTURE_SPEC), settings={"gateway_url": "ws://127.0.0.1:9/api/ws?ticket=t"}
    )
    assert result.get("ok") is True
    assert "profiles.configure" in used and "groups.create" in used


def test_dispatch_maps_members_and_normalizes_absent_room(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    seen = []

    def _handle(request):
        seen.append(request)
        if request["method"] == "groups.state":
            return {"jsonrpc": "2.0", "id": request["id"],
                    "error": {"code": mod._ROOM_NOT_FOUND_CODE, "message": "no room"}}
        return {"jsonrpc": "2.0", "id": request["id"], "result": {"ok": True}}

    monkeypatch.setattr(mod, "_gateway_handle_request", _handle, raising=True)

    # groups.create: profile-name strings are adapted to member objects on the wire.
    mod._dispatch_rpc("groups.create",
                      {"room_id": "r", "name": "R", "members": ["orchestrator", "reviewer"]})
    create_req = [r for r in seen if r["method"] == "groups.create"][0]
    wire_members = create_req["params"]["members"]
    assert all(isinstance(m, dict) for m in wire_members)
    assert {m["profile"] for m in wire_members} == {"orchestrator", "reviewer"}

    # groups.state absent-room (error 4114) is normalized, not raised.
    state = mod._dispatch_rpc("groups.state", {"room_id": "missing"})
    assert state == {"exists": False}
    assert mod._room_exists(state) is False


def test_dispatch_reraises_non_absent_room_errors(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)

    def _handle(request):
        return {"jsonrpc": "2.0", "id": request["id"],
                "error": {"code": -32000, "message": "boom"}}

    monkeypatch.setattr(mod, "_gateway_handle_request", _handle, raising=True)
    with pytest.raises(mod.RpcError):
        mod._dispatch_rpc("groups.state", {"room_id": "x"})


def test_tool_handlers_return_json_never_raise(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(mod, "onboard_coworker", _boom, raising=True)
    parsed = json.loads(mod._tool_onboard_coworker({"spec": "x.yaml"}))
    assert parsed["ok"] is False and "kaboom" in parsed["error"]

    # Missing required arg → structured error, not an exception.
    missing = json.loads(mod._tool_onboard_coworker({}))
    assert missing["ok"] is False and "spec" in missing["error"]

    monkeypatch.setattr(mod, "_dispatch_rpc", _boom, raising=True)
    created = json.loads(mod._tool_create_agent({"name": "bot"}))
    assert created["ok"] is False


def test_configure_bot_meta_retries_on_cas_conflict(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    calls = {"configure": 0}

    def _rpc(method, params):
        if method == "profiles.list":
            return {"profiles": [{"name": "reviewer", "ui_meta_revisions": {"hermes-bots": 5}}]}
        if method == "profiles.configure":
            calls["configure"] += 1
            if calls["configure"] == 1:
                raise mod.RpcError({"code": 4113, "message": "revision conflict"})
            return {"applied": {"ui_meta": True}, "ui_meta_revisions": {"hermes-bots": 6}}
        return {"ok": True}

    monkeypatch.setattr(mod, "_dispatch_rpc", _rpc, raising=True)
    revisions = {"reviewer": {"hermes-bots": 0}}
    assert mod._configure_bot_meta("reviewer", {"title": "Reviewer"}, revisions) is True
    assert calls["configure"] == 2
    # The retry re-read the current revision from the gateway, not the stale 0.
    assert revisions["reviewer"]["hermes-bots"] == 6


def test_configure_bot_meta_gives_up_after_retry(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)

    def _rpc(method, params):
        if method == "profiles.list":
            return {"profiles": []}
        if method == "profiles.configure":
            raise mod.RpcError({"code": 4113, "message": "conflict"})
        return {"ok": True}

    monkeypatch.setattr(mod, "_dispatch_rpc", _rpc, raising=True)
    assert mod._configure_bot_meta("reviewer", {"title": "R"}, {}) is False


def test_run_onboard_reports_failure_on_install_error(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)

    def _bad_install(rendered_dir, name):
        raise RuntimeError(f"disk full for {name}")

    monkeypatch.setattr(mod, "_install", _bad_install, raising=True)

    def _rpc(method, params):
        if method == "profiles.list":
            return {"profiles": []}
        if method == "session.list":
            return {"sessions": [{"title": mod.BOT_CHAT_TITLE}]}
        if method == "groups.state":
            return {"exists": True}
        return {"ok": True}

    monkeypatch.setattr(mod, "_dispatch_rpc", _rpc, raising=True)
    result = mod._run_onboard(str(FIXTURE_SPEC))
    assert result["ok"] is False
    assert any("install" in w for w in result["warnings"])


def test_slash_handler_is_async_and_offloads(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    assert inspect.iscoroutinefunction(mod._slash_onboard_coworker)

    monkeypatch.setattr(mod, "onboard_coworker",
                        lambda spec, settings: {"ok": True, "spec": spec}, raising=True)
    out = asyncio.run(mod._slash_onboard_coworker("some.yaml"))
    assert json.loads(out) == {"ok": True, "spec": "some.yaml"}

    # Empty args → usage string, no onboard dispatch.
    usage = asyncio.run(mod._slash_onboard_coworker(""))
    assert "usage" in usage


def test_compose_rejects_path_traversal_spec(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    spec_dir = tmp_path / "hostile"
    spec_dir.mkdir()
    spec = spec_dir / "coworker-types.yaml"
    spec.write_text(
        yaml.safe_dump({"spines": {}, "types": {"../escape": {"identity": "x"}}}),
        encoding="utf-8",
    )
    with pytest.raises(mod.CompositionError):
        mod.compose(str(spec), str(tmp_path / "out"))
