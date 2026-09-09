"""Transport + canonical-Bot-Chat unit tests for nv-coworker-compose.

Behavior contracts for the seams the frozen acceptance test does not exercise
directly: the JSON-RPC envelope normalization, the two groups.state response
shapes, the in-process-vs-standalone dispatch routing, and the defensive
canonical Bot Chat lifecycle. Loads the plugin through the real discovery path
(same shape as test_plugin_api_compat.py) so seams are monkeypatched on the
actual loaded module.
"""

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
        return {"result": {"ok": True}}

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
        return {"result": {"ok": True}}

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
