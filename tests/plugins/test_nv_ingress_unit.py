"""Companion behaviour tests for nv-ingress (ING-F66), beside the acceptance file.

These cover the render and runtime guarantees the acceptance criteria rely on
but do not assert directly:

- no global webhook secret survives the ingress render;
- the routing labels reach the plugin settings;
- `hermes ingress render-host` writes exactly what compose writes;
- the periodic drain never blocks the gateway's event loop;
- the no-auth mode is refused off the ingress route;
- `hermes ingress status` keeps its documented shape.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_SRC = REPO_ROOT / "plugins" / "nv-ingress"
ING_SPEC = REPO_ROOT / "tests" / "e2e-scenarios" / "ING-F66" / "spec" / "openshell" / "coworker-types.yaml"
FLEET_PLUGINS = ("nv-coworker-compose", "nv-fleet-gates", "nv-ingress")


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home" / ".hermes"
    (home / "plugins").mkdir(parents=True)
    for key in FLEET_PLUGINS:
        shutil.copytree(REPO_ROOT / "plugins" / key, home / "plugins" / key)
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": list(FLEET_PLUGINS)}}),
                                      encoding="utf-8")
    (tmp_path / "bundled").mkdir(exist_ok=True)
    return home


def _hermes(tmp_path: Path, *argv: str) -> subprocess.CompletedProcess:
    home = _home(tmp_path) if not (tmp_path / "home").exists() else tmp_path / "home" / ".hermes"
    env = dict(os.environ, HOME=str(home.parent), HERMES_HOME=str(home),
               HERMES_BUNDLED_PLUGINS=str(tmp_path / "bundled"), HERMES_ENABLE_PROJECT_PLUGINS="0")
    return subprocess.run([sys.executable, "-m", "hermes_cli.main", *argv], capture_output=True, text=True,
                          encoding="utf-8", env=env, cwd=str(REPO_ROOT), timeout=300)


def _spec_copy(tmp_path: Path, mutate) -> Path:
    dest = tmp_path / "spec"
    shutil.copytree(ING_SPEC.parent, dest)
    path = dest / ING_SPEC.name
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _default_cfg(out: Path) -> dict:
    return yaml.safe_load((out / "default" / "config.yaml").read_text(encoding="utf-8"))


def test_ingress_render_drops_a_global_webhook_secret(tmp_path):
    material = secrets.token_hex(24)

    def mutate(data):
        data["default_config"]["platforms"] = {"webhook": {"extra": {"secret": material, "rate_limit": 12}}}

    out = tmp_path / "out"
    proc = _hermes(tmp_path, "coworker", "compose", str(_spec_copy(tmp_path, mutate)), "--out", str(out))
    assert proc.returncode == 0, proc.stderr
    extra = _default_cfg(out)["platforms"]["webhook"]["extra"]
    assert "secret" not in extra and extra["rate_limit"] == 12
    assert all(material.encode() not in p.read_bytes() for p in out.rglob("*") if p.is_file())


def test_ingress_render_writes_the_routing_labels_into_the_plugin_settings(tmp_path):
    out = tmp_path / "out"
    proc = _hermes(tmp_path, "coworker", "compose", str(ING_SPEC), "--out", str(out))
    assert proc.returncode == 0, proc.stderr
    settings = _default_cfg(out)["plugins"]["entries"]["nv-ingress"]["settings"]
    spec = yaml.safe_load(ING_SPEC.read_text(encoding="utf-8"))
    assert settings["issue_labels"] == spec["ingress"]["issue_labels"]
    assert settings["orchestrator_profile"] == spec["orchestrator_profile"]


def test_render_host_matches_the_compose_host_artefacts(tmp_path):
    composed, standalone = tmp_path / "composed", tmp_path / "standalone"
    assert _hermes(tmp_path, "coworker", "compose", str(ING_SPEC), "--out", str(composed)).returncode == 0
    proc = _hermes(tmp_path, "ingress", "render-host", str(ING_SPEC), "--out", str(standalone))
    assert proc.returncode == 0, proc.stderr + proc.stdout
    for sub in ("host", "hooks"):
        a = sorted(p.relative_to(composed) for p in (composed / sub).rglob("*") if p.is_file())
        b = sorted(p.relative_to(standalone) for p in (standalone / sub).rglob("*") if p.is_file())
        assert a == b and a, sub
        for rel in a:
            assert (composed / rel).read_bytes() == (standalone / rel).read_bytes(), rel


def test_no_auth_mode_is_refused_off_the_ingress_route(monkeypatch):
    path = REPO_ROOT / "plugins" / "nv-coworker-compose" / "compose.py"
    modspec = importlib.util.spec_from_file_location("_compose_under_test", path)
    mod = importlib.util.module_from_spec(modspec)
    # dataclass creation resolves the defining module through sys.modules.
    monkeypatch.setitem(sys.modules, modspec.name, mod)
    modspec.loader.exec_module(mod)
    cfg = {"platforms": {"webhook": {"extra": {"host": "127.0.0.1", "routes": {
        "other": {"secret": "INSECURE_NO_AUTH", "profile": "default"}}}}}}
    with pytest.raises(mod.CompositionError):
        mod._validate_webhook_routes(cfg, {"default"}, ingress_route="ingress")
    cfg["platforms"]["webhook"]["extra"] = {"host": "0.0.0.0", "routes": {
        "ingress": {"secret": "INSECURE_NO_AUTH", "profile": "default"}}}
    with pytest.raises(mod.CompositionError):
        mod._validate_webhook_routes(cfg, {"default"}, ingress_route="ingress")
    cfg["platforms"]["webhook"]["extra"]["host"] = "127.0.0.1"
    mod._validate_webhook_routes(cfg, {"default"}, ingress_route="ingress")


def test_periodic_drain_never_blocks_the_event_loop(tmp_path, monkeypatch):
    from hermes_cli.plugins import PluginManager

    root = tmp_path / "root"
    (root / "plugins").mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, root / "plugins" / "nv-ingress")
    (root / "config.yaml").write_text(yaml.safe_dump({"plugins": {
        "enabled": ["nv-ingress"],
        "entries": {"nv-ingress": {"settings": {"drain_interval_seconds": 1}}}}}), encoding="utf-8")
    (tmp_path / "bundled").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(tmp_path / "bundled"))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")

    started, release = threading.Event(), threading.Event()

    async def scenario():
        manager = PluginManager()
        manager.discover_and_load()
        module = manager._plugins["nv-ingress"].module
        monkeypatch.setattr(module.bridge, "drain_once",
                            lambda _settings: (started.set(), release.wait(10), 0)[-1])
        module._LOOP_TASK_STARTED = False
        module._ensure_loop_task()
        assert await asyncio.to_thread(started.wait, 10), "the periodic drain never ran"
        beats = 0
        t0 = time.monotonic()
        while time.monotonic() - t0 < 0.5:
            await asyncio.sleep(0.05)
            beats += 1
        release.set()
        return beats

    beats = asyncio.run(scenario())
    assert beats >= 5, f"the event loop stalled while a drain pass was blocked ({beats} beats)"


def test_status_has_the_documented_shape(tmp_path, monkeypatch, capsys):
    from hermes_cli.plugins import PluginManager

    root = tmp_path / "root"
    (root / "plugins").mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, root / "plugins" / "nv-ingress")
    (root / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": ["nv-ingress"]}}), encoding="utf-8")
    (tmp_path / "bundled").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(tmp_path / "bundled"))
    manager = PluginManager()
    manager.discover_and_load()
    capsys.readouterr()
    handler = manager._cli_commands["ingress"]["handler_fn"]
    assert handler(SimpleNamespace(ingress_command="status", json=True)) == 0
    status = json.loads(capsys.readouterr().out)
    assert set(status) == {"pending", "stuck", "runaway", "refusals", "unclaimable", "webhook_guard"}
    assert status["webhook_guard"] == {"breach": False, "routes": [], "reason": None}


def _loaded_plugin(tmp_path: Path, monkeypatch, settings: dict | None = None):
    from hermes_cli.plugins import PluginManager

    root = tmp_path / "root"
    (root / "plugins").mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, root / "plugins" / "nv-ingress")
    entry = {"settings": settings} if settings else {}
    (root / "config.yaml").write_text(yaml.safe_dump({"plugins": {
        "enabled": ["nv-ingress"], "entries": {"nv-ingress": entry}}}), encoding="utf-8")
    (tmp_path / "bundled").mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(tmp_path / "bundled"))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    manager = PluginManager()
    manager.discover_and_load()
    return root, manager._plugins["nv-ingress"].module


def test_lease_is_not_taken_from_a_live_holder_past_its_deadline(tmp_path, monkeypatch):
    _root, module = _loaded_plugin(tmp_path, monkeypatch)
    ledger = module.ledger
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        import psutil

        live = f"{sleeper.pid}:{psutil.Process(sleeper.pid).create_time()}:drain"
        assert ledger.acquire_lease(live, -1.0), "the first holder takes the lease"
        assert not ledger.acquire_lease("1:0:other", 60.0), "a live holder's lease must not be stolen"
    finally:
        sleeper.kill()
        sleeper.wait()
    assert ledger.acquire_lease("1:0:other", 60.0), "a dead holder's lease is taken"


def test_ci_event_matching_two_claimed_prs_waits_instead_of_guessing(tmp_path, monkeypatch):
    _root, module = _loaded_plugin(tmp_path, monkeypatch)
    ledger, bridge = module.ledger, module.bridge
    conn = ledger.connect()
    try:
        for pr in (3, 4):
            conn.execute("INSERT INTO pr_owner (repo, pr, owner_profile, session_id, thread_id, claimed_at)"
                         " VALUES ('o/r', ?, 'fixer', ?, 't', 0)", (pr, f"s{pr}"))
            conn.execute("INSERT INTO pr_heads (repo, pr, head_sha, head_branch, open, updated_at)"
                         " VALUES ('o/r', ?, ?, 'shared', 1, 0)", (pr, f"sha{pr}"))
        conn.commit()
        env = {"repo": "o/r", "head_branch": "shared", "head_sha": "unknown"}
        assert bridge._associate(conn, env) == ("pending", None)
        assert bridge._associate(conn, {**env, "head_sha": "sha4"}) == ("owned", 4)
    finally:
        conn.close()


def test_guard_rebuilds_routes_from_the_file_not_a_stale_cache(tmp_path, monkeypatch):
    root, module = _loaded_plugin(tmp_path, monkeypatch, {"webhook_guard": True})
    from gateway.config import PlatformConfig
    from gateway.platforms.webhook import _INSECURE_NO_AUTH

    subs = root / "webhook_subscriptions.json"
    material = secrets.token_hex(24)
    subs.write_text(json.dumps({"agent-made": {"secret": material, "events": ["push"]}}), encoding="utf-8")
    adapter = module.webhook_guard.GuardedWebhookAdapter(PlatformConfig(enabled=True, extra={
        "host": "127.0.0.1", "port": 0, "routes": {"ingress": {"secret": _INSECURE_NO_AUTH}}}))
    adapter._reload_dynamic_routes()
    assert all(r.get("enabled") is False for r in adapter._routes.values())

    # A same-mtime rewrite leaves the release's mtime-gated cache holding the old route.
    stamp = subs.stat().st_mtime
    subs.write_text(json.dumps({"agent-safe": {"secret": _INSECURE_NO_AUTH, "events": ["push"]}}),
                    encoding="utf-8")
    os.utime(subs, (stamp, stamp))
    adapter._reload_dynamic_routes()
    assert set(adapter._routes) == {"ingress", "agent-safe"}
    assert all(r.get("enabled") is not False for r in adapter._routes.values())
    assert material not in json.dumps(adapter._routes)
