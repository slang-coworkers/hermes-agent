"""OSH-F64 real-run transport: the standalone install-openshell path binds the gateway.

A standalone ``hermes coworker install-openshell`` process has no in-process hosted-room
service, so its room ``groups.state``/``groups.create`` calls must route over the
authenticated WebSocket requester (the ``_standalone_ctx`` binding ``onboard_coworker``
uses), never the in-process ``_gateway_handle_request``. Loads the plugin from an isolated
HERMES_HOME via the real discovery path and drives ``_apply_openshell`` with a recording
requester and a fake installer, so no subprocess and no live gateway are needed.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

PLUGIN_KEY = "nv-coworker-compose"


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "plugins" / PLUGIN_KEY / "plugin.yaml").exists():
            return parent
    pytest.fail("could not locate repo root")


def _load_module(tmp_path, monkeypatch):
    root = _repo_root()
    home = tmp_path / "hermes_home"
    (home / "plugins").mkdir(parents=True)
    shutil.copytree(root / "plugins" / PLUGIN_KEY, home / "plugins" / PLUGIN_KEY)
    bundled = tmp_path / "empty_bundled"
    bundled.mkdir()
    (home / "config.yaml").write_text(
        "plugins:\n  enabled: [nv-coworker-compose]\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    from hermes_cli.plugins import PluginManager

    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled is True and loaded.error is None and loaded.module is not None
    return loaded.module


class _Recorder:
    """A stand-in WS requester: records each dispatched method and answers the two the
    plan makes — an absent room for groups.state, an ok for groups.create."""

    def __init__(self):
        self.methods = []

    def __call__(self, request):
        self.methods.append(request["method"])
        rid = request.get("id")
        if request["method"] == "groups.state":
            return {"jsonrpc": "2.0", "id": rid, "result": {"exists": False}}
        return {"jsonrpc": "2.0", "id": rid, "result": {"ok": True}}


def test_apply_openshell_routes_rooms_over_the_ws_requester(tmp_path, monkeypatch):
    mod = _load_module(tmp_path, monkeypatch)
    recorder = _Recorder()

    def _fake_preflight(url):
        mod._pending_ws_requester.set(recorder)
        return True, "ok"

    def _forbidden_inprocess(request):
        raise AssertionError("standalone install-openshell must not use the in-process handler")

    monkeypatch.setattr(mod, "_gateway_preflight", _fake_preflight)
    monkeypatch.setattr(mod, "_gateway_handle_request", _forbidden_inprocess)

    class _FakeInstaller:
        def apply(self, spec, ref, *, room_creator, room_exists):
            assert room_exists("fleet-review") is False
            room_creator("fleet-review", "FLEET-F62 Review", ["orchestrator", "reviewer"])

    mod._apply_openshell(_FakeInstaller(), "spec.yaml", "a" * 40,
                         "ws://127.0.0.1:9/api/ws?token=x")

    assert "groups.state" in recorder.methods, "room existence must be probed over the requester"
    assert "groups.create" in recorder.methods, "room creation must be dispatched over the requester"


def test_apply_openshell_refuses_when_preflight_fails(tmp_path, monkeypatch):
    """A failed preflight refuses before installer.apply runs (zero mutation)."""
    mod = _load_module(tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "_gateway_preflight", lambda url: (False, "auth_rejected"))

    class _NeverApply:
        def apply(self, *a, **k):
            raise AssertionError("apply must not run when the gateway preflight fails")

    with pytest.raises(RuntimeError):
        mod._apply_openshell(_NeverApply(), "spec.yaml", "a" * 40, "ws://127.0.0.1:9/api/ws?token=x")
