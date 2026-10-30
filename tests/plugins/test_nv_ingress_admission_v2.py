"""AC-ING-F66-2 on v0.22.5's real webhook admission seam (UPG-F69.c erratum E1, D11).

A real ``WebhookAdapter`` serves the rendered ingress route through upstream's real-authority
harness (``tests.gateway.fixtures.webhook_route_authority``), so the delivery reaches nv-ingress's
``pre_gateway_dispatch`` hook through ``gateway/platforms/webhook_ingress.py`` ``admit_producer``.
Only the door's RPC and endpoint discovery are faked. Kept apart from
``test_nv_ingress_acceptance.py``, whose autouse fixture reroutes ``admit_producer``.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_yaml as yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_KEY = "nv-ingress"
PLUGIN_SRC = REPO_ROOT / "plugins" / PLUGIN_KEY
ING_SPEC = REPO_ROOT / "tests" / "e2e-scenarios" / "ING-F66" / "spec" / "openshell" / "coworker-types.yaml"
ORCH = "orchestrator"
REPO = "o/ing-f66-v2"
PR = 41
DELIVERY = "d-v2-real-admission"


def _check_failure() -> dict:
    return {"action": "completed", "repository": {"full_name": REPO},
            "sender": {"login": "octo", "type": "User"},
            "check_run": {"head_sha": "b" * 40, "status": "completed", "conclusion": "failure",
                          "name": "check", "pull_requests": [{"number": PR}],
                          "check_suite": {"head_branch": "feat"}}}


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An isolated DEFAULT home with nv-ingress enabled, its route script rendered, and the
    orchestrator's Bot Chat present; the door is real down to its RPC."""
    from hermes_state import SessionDB

    root = tmp_path / "root"
    (root / "plugins").mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, root / "plugins" / PLUGIN_KEY, ignore=shutil.ignore_patterns("__pycache__"))
    spec = yaml.safe_load(ING_SPEC.read_text(encoding="utf-8-sig"))
    from importlib.util import module_from_spec, spec_from_file_location

    loader = spec_from_file_location("_nv_ingress_hostrender_v2", PLUGIN_SRC / "hostrender.py")
    hostrender = module_from_spec(loader)
    loader.loader.exec_module(hostrender)
    params = hostrender.validate(spec["ingress"])
    settings = {**hostrender.plugin_settings(params, ORCH), "webhook_guard": False,
                "drain_interval_seconds": 3600}
    (root / "config.yaml").write_text(yaml.safe_dump({
        "plugins": {"enabled": [PLUGIN_KEY], "entries": {PLUGIN_KEY: {"settings": settings}}}}), encoding="utf-8")
    (root / "scripts").mkdir()
    shutil.copy(PLUGIN_SRC / "route_scripts" / "ingress_stage.py", root / "scripts" / "ingress_stage.py")
    orch_home = root / "profiles" / ORCH
    orch_home.mkdir(parents=True)
    db = SessionDB(db_path=orch_home / "state.db")
    try:
        db.create_session("orch-bot-chat", "cli")
        db.set_session_title("orch-bot-chat", "Bot Chat")
    finally:
        db.close()
    bundled = tmp_path / "empty-bundled"
    bundled.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")

    import hermes_cli.gateway_runtime as runtime
    import tools.bot_live_delivery as live

    door: list[dict] = []
    ready = SimpleNamespace(state="ready", endpoint=SimpleNamespace(instance_id="lease-1"))
    monkeypatch.setattr(runtime, "discover_gateway_endpoint", lambda h, timeout=5, **_kw: ready)
    monkeypatch.setattr(live, "authority_delivery", lambda h, p: door.append(dict(p)) or {
        "status": "queued", "delivery_id": p["id"]})
    return SimpleNamespace(root=root, params=params, route_block=hostrender.route_block(params), door=door)


@pytest.mark.asyncio
async def test_ac_ing_f66_2_real_admission(home):
    """The edge verifies a GitHub signature with a host-held secret, normalizes to envelope v1 and forwards it; the in-sandbox loopback route (a real release `WebhookAdapter` on 127.0.0.1 with the loopback-gated no-auth route) accepts it with 202 and the bridge stages it (floor a2, hermetic)."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from gateway.config import PlatformConfig
    from gateway.platforms.webhook import WebhookAdapter
    from hermes_cli.plugins import get_plugin_manager
    from tests.gateway.fixtures.webhook_route_authority import mount_authority

    manager = get_plugin_manager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled is True and loaded.error is None, loaded.error
    hooks = manager._hooks["pre_gateway_dispatch"]
    assert len(hooks) == 1, "only nv-ingress hooks pre_gateway_dispatch here"
    seen: list[tuple[str, object]] = []
    registered = hooks[0]

    def recording(event=None, **kwargs):
        verdict = registered(event=event, **kwargs)
        seen.append((str(event.source.chat_id), verdict))
        return verdict

    hooks[0] = recording
    try:
        envelope = _submodules(loaded)["envelope"]
        body = envelope.normalize("github", "check_run", {"X-GitHub-Event": "check_run",
                                                         "X-GitHub-Delivery": DELIVERY}, _check_failure())
        body["event_type"] = envelope.TRANSPORT_EVENT
        route = home.params["route"]
        adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={
            "host": "127.0.0.1", "port": home.params["port"], "routes": {route: home.route_block}}))
        app = web.Application()
        mount_authority(app, adapter)
        app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
        async with TestClient(TestServer(app)) as client:
            authority = adapter._message_handler.__self__.session_authority
            resp = await client.post(f"/webhooks/{route}", data=json.dumps(body).encode(), headers={
                "Content-Type": "application/json", "X-GitHub-Delivery": DELIVERY})
            assert resp.status == 202, await resp.text()
            assert len(seen) == 1 and seen[0][0].startswith("webhook:v2:"), seen
            assert isinstance(seen[0][1], dict) and seen[0][1].get("action") == "skip", seen
            assert not authority.db._read_all("SELECT 1 FROM session_admissions WHERE request_id = ?", (DELIVERY,))
        ledger = _submodules(loaded)["ledger"]
        conn = ledger.connect()
        try:
            staged = list(conn.execute("SELECT delivery_id FROM outbox WHERE delivery_id = ?", (DELIVERY,)))
        finally:
            conn.close()
        assert staged == [(DELIVERY,)]
        loaded.module.drain_once()
        to_orch = [r for r in home.door if r["profile"] == ORCH]
        assert len(to_orch) == 1 and f"ingress-delivery: github/{DELIVERY}/{PR}" in to_orch[0]["message"]
    finally:
        hooks[0] = registered
        manager.unload()


def _submodules(loaded) -> dict:
    import sys

    pkg = loaded.module.__name__
    return {name: sys.modules[f"{pkg}.{name}"] for name in ("envelope", "ledger")}
