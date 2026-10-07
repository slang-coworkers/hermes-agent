"""Companion behaviour tests for nv-ingress (ING-F66), beside the acceptance file.

These cover the render and runtime guarantees the acceptance criteria rely on
but do not assert directly:

- no global webhook secret survives the ingress render;
- the routing labels reach the plugin settings;
- `hermes ingress render-host` writes exactly what compose writes;
- the drain runs only on its driver thread, never on the loading thread or the
  gateway's event loop, and only in the process holding the gateway runtime lock;
- an orchestrator-bound prompt still names the action, the CI result and the link;
- the no-auth mode is refused off the ingress route;
- `hermes ingress status` keeps its documented shape;
- the edge scope is rendered only from a spec block, and a malformed block is refused;
- an edge that cannot record its scope state refuses the delivery instead of
  spooling an unregistered PR or losing a count, and an edge never reuses scope
  state written under other scope rules.
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


_MANAGERS: list = []


@pytest.fixture(autouse=True)
def _unload_managers():
    """Every load starts a drain driver thread; unloading stops it (D5)."""
    yield
    while _MANAGERS:
        _MANAGERS.pop().unload()


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


def test_edge_scope_is_rendered_only_from_the_spec_block(tmp_path):
    def drop_scope(data):
        data["ingress"].pop("scope")

    out = tmp_path / "out"
    proc = _hermes(tmp_path, "coworker", "compose", str(_spec_copy(tmp_path, drop_scope)), "--out", str(out))
    assert proc.returncode == 0, proc.stderr
    assert "scope" not in json.loads((out / "host" / "edge.yaml").read_text(encoding="utf-8"))

    def bad_scope(data):
        data["ingress"]["scope"] = {"repo": "o/r", "ref_prefix": "x-"}

    bad_out = tmp_path / "bad-out"
    shutil.rmtree(tmp_path / "spec")
    proc = _hermes(tmp_path, "coworker", "compose", str(_spec_copy(tmp_path, bad_scope)), "--out", str(bad_out))
    assert proc.returncode != 0
    assert "ingress.scope" in proc.stderr + proc.stdout
    assert not (bad_out / "host" / "edge.yaml").exists()


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


def _drain_threads() -> list:
    return [t for t in threading.enumerate() if t.name == "nv-ingress-drain" and t.is_alive()]


def test_drain_runs_only_on_the_driver_thread_and_never_blocks_the_loop(tmp_path, monkeypatch):
    from gateway import status as gateway_status
    from gateway.config import Platform

    passes: list[str] = []
    started, release = threading.Event(), threading.Event()

    def _slow_pass(_settings, _load_id, _stop=None):
        passes.append(threading.current_thread().name)
        started.set()
        release.wait(10)
        return 0

    assert gateway_status.acquire_gateway_runtime_lock()
    try:
        # A loop-less loading thread, as the gateway's background plugin discovery is.
        loaded: dict = {}
        loader = threading.Thread(target=lambda: loaded.update(
            zip(("root", "module"), _loaded_plugin(tmp_path, monkeypatch, {"drain_interval_seconds": 1}))))
        loader.start()
        loader.join(30)
        module = loaded["module"]
        monkeypatch.setattr(module.bridge, "drain_once", _slow_pass)
        event = SimpleNamespace(source=SimpleNamespace(platform=Platform.WEBHOOK, chat_type="webhook",
                                                       chat_id="webhook:ingress:d-1"))

        async def scenario():
            t0 = time.monotonic()
            verdict = module._on_pre_gateway_dispatch(event=event)
            hook_seconds = time.monotonic() - t0
            assert await asyncio.to_thread(started.wait, 10), "the driver never ran a pass"
            beats = 0
            t0 = time.monotonic()
            while time.monotonic() - t0 < 2.0:
                await asyncio.sleep(0.05)
                beats += 1
            return verdict, hook_seconds, beats

        verdict, hook_seconds, beats = asyncio.run(scenario())
        # A pass run inline would hold the hook (and the loop) for the 10 s release bound.
        assert verdict["action"] == "skip" and hook_seconds < 2.0, "the hook must only kick the driver"
        assert beats >= 5, f"the event loop stalled while a drain pass was blocked ({beats} beats)"
        assert set(passes) == {"nv-ingress-drain"}, passes
    finally:
        release.set()
        gateway_status.release_gateway_runtime_lock()


def test_driver_idles_without_the_runtime_lock_and_stops_on_unload(tmp_path, monkeypatch):
    from gateway import status as gateway_status

    consults: list[bool] = []
    monkeypatch.setattr(gateway_status, "owns_gateway_runtime_lock", lambda: consults.append(False) or False)
    _root, module = _loaded_plugin(tmp_path, monkeypatch, {"drain_interval_seconds": 1})
    passes: list[str] = []
    monkeypatch.setattr(module.bridge, "drain_once", lambda *a, **k: passes.append("pass") or 0)
    module._kick()
    # The driver asks for the lock before every pass, so three answers mean three skipped passes.
    deadline = time.monotonic() + 30
    while len(consults) < 3 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(consults) >= 3, "the driver never consulted the gateway runtime lock"
    assert passes == [], "a process without the gateway runtime lock ran a drain pass"
    assert _drain_threads(), "the load started no driver thread"
    for manager in list(_MANAGERS):
        manager.unload()
    _MANAGERS.clear()
    assert not _drain_threads(), "unload left the driver thread running"


def test_a_second_load_in_one_process_cannot_take_a_live_pass_lease(tmp_path, monkeypatch):
    _root, module = _loaded_plugin(tmp_path, monkeypatch)
    ledger, bridge = module.ledger, module.bridge
    first, second = bridge._holder("load-a"), bridge._holder("load-b")
    assert first != second
    assert ledger.acquire_lease(first, -1.0)
    assert not ledger.acquire_lease(second, 60.0), "a reloaded plugin took the lease of a pass in flight"
    ledger.release_lease(first)
    assert ledger.acquire_lease(second, 60.0)


def test_orchestrator_prompt_keeps_the_action_ci_result_and_link(tmp_path, monkeypatch):
    _root, module = _loaded_plugin(tmp_path, monkeypatch)
    envelope, deliver = module.envelope, module.deliver
    probe, sha = "probe" + secrets.token_hex(6), "c" * 40
    url = "https://github.com/o/r/runs/" + secrets.token_hex(4)
    gh_headers = {envelope.GITHUB_EVENT_HEADER: "check_run", envelope.GITHUB_DELIVERY_HEADER: "d-b"}
    gh = envelope.normalize("github", "check_run", gh_headers, {
        "action": "completed", "repository": {"full_name": "o/r"}, "sender": {"login": "octo"},
        "check_run": {"head_sha": sha, "conclusion": "failure", "name": probe, "html_url": url,
                      "pull_requests": [{"number": 9}], "check_suite": {"head_branch": "b"}}})
    orch = deliver.prompt(gh, 9, to_orchestrator=True)
    for fact in ("o/r#9", "check_run", "completed", "concluded failure", sha, url, "ingress-delivery: github/d-b/9"):
        assert fact in orch, f"{fact!r} missing from the orchestrator prompt"
    assert probe not in orch
    assert probe in deliver.prompt(gh, 9, to_orchestrator=False), "the owner prompt keeps the check name"

    gl_url = "https://gitlab.example/o/r/-/issues/4"
    gl_headers = {envelope.GITLAB_EVENT_HEADER: "Issue Hook", envelope.GITLAB_ID_HEADER: "gl-b"}
    gl = envelope.normalize("gitlab", "Issue Hook", gl_headers, {
        "object_kind": "issue", "project": {"path_with_namespace": "o/r"}, "user": {"username": "octo"},
        "labels": [{"title": "fleet"}], "object_attributes": {"iid": 4, "action": "open", "title": probe,
                                                              "url": gl_url}})
    issue = deliver.prompt(gl, None, to_orchestrator=True)
    for fact in ("o/r issue #4", "opened", gl_url, "ingress-delivery: gitlab/gl-b"):
        assert fact in issue, f"{fact!r} missing from the orchestrator issue prompt"
    assert probe not in issue


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
    _MANAGERS.append(manager)
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
    _MANAGERS.append(manager)
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


def test_render_names_the_orchestrator_in_every_coworker_and_gates_remap_on_it(tmp_path, monkeypatch):
    def mutate(data):
        data["types"]["lead"] = data["types"].pop("orchestrator")
        data["orchestrator_profile"] = "lead"
        for room in (data.get("rooms") or {}).values():
            room["members"] = ["lead" if m == "orchestrator" else m for m in room.get("members") or []]

    out = tmp_path / "out"
    proc = _hermes(tmp_path, "coworker", "compose", str(_spec_copy(tmp_path, mutate)), "--out", str(out))
    assert proc.returncode == 0, proc.stderr
    for role in ("lead", "triager", "fixer", "reviewer", "approver"):
        cfg = yaml.safe_load((out / role / "config.yaml").read_text(encoding="utf-8"))
        assert cfg["plugins"]["entries"]["nv-ingress"]["settings"]["orchestrator_profile"] == "lead", role

    _root, module = _loaded_plugin(tmp_path / "gate", monkeypatch, {"orchestrator_profile": "lead"})
    monkeypatch.setattr(module, "_active_profile", lambda: "lead")
    assert module._on_pre_tool_call(tool_name="ingress_remap", args={"repo": "o/r", "pr": 1, "to": "fixer"})["action"] == "approve"
    monkeypatch.setattr(module, "_active_profile", lambda: "orchestrator")
    assert module._on_pre_tool_call(tool_name="ingress_remap", args={"repo": "o/r", "pr": 1, "to": "fixer"})["action"] == "block"


def _edge_module():
    edge_dir = PLUGIN_SRC / "edge" / "__init__.py"
    spec = importlib.util.spec_from_file_location("_nv_ingress_edge_under_test", edge_dir)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _offline_edge(tmp_path: Path):
    edge_mod = _edge_module()
    key_file = tmp_path / "gh-key"
    key = secrets.token_bytes(32)
    key_file.write_bytes(key)
    cfg = {"forward_url": "http://127.0.0.1:9/webhooks/ingress", "spool_dir": str(tmp_path / "spool"),
           "platforms": {"github": {"secret_file": str(key_file)}}}
    return edge_mod, edge_mod.Edge(cfg), key


def _signed_github_headers(edge_mod, key: bytes, body: bytes, delivery: str) -> dict:
    import hashlib
    import hmac

    env = edge_mod.envelope
    return {env.GITHUB_EVENT_HEADER: "issues", env.GITHUB_DELIVERY_HEADER: delivery,
            edge_mod.GITHUB_SIGNATURE_HEADER: "sha256=" + hmac.new(key, body, hashlib.sha256).hexdigest()}


def test_concurrent_same_id_posts_each_spool_a_complete_entry(tmp_path):
    edge_mod, edge, key = _offline_edge(tmp_path)
    body = json.dumps({"action": "opened", "repository": {"full_name": "o/r"},
                       "issue": {"number": 3, "labels": [{"name": "fleet"}]}}).encode()
    headers = _signed_github_headers(edge_mod, key, body, "same-id")
    results: list = []
    start = threading.Barrier(8)

    def post():
        start.wait(5)
        results.append(edge.receive("github", headers, body)[0])

    threads = [threading.Thread(target=post) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert results == [202] * 8
    spooled = list((tmp_path / "spool").glob("*.json"))
    assert len(spooled) == 1
    assert json.loads(spooled[0].read_bytes())["envelope"]["delivery_id"] == "same-id"
    assert not [p for p in (tmp_path / "spool").iterdir() if p.name.startswith(".")], "no temp file left behind"


def test_corrupt_spool_entry_is_dead_lettered_and_logged(tmp_path, caplog):
    _edge_mod, edge, _key = _offline_edge(tmp_path)
    bad = tmp_path / "spool" / "github-broken.json"
    bad.write_bytes(b'{"envelope": ')
    with caplog.at_level("ERROR", logger="nv_ingress.edge"):
        edge._forward_one(bad, time.time())
    assert not bad.exists()
    assert (tmp_path / "spool" / "dead-letter" / "github-broken.json").exists()
    assert any("github-broken.json" in r.getMessage() and "dead-letter" in r.getMessage() for r in caplog.records)


def test_a_redelivery_racing_a_corrupt_entry_is_kept_for_forwarding(tmp_path, monkeypatch):
    edge_mod, edge, key = _offline_edge(tmp_path)
    body = json.dumps({"action": "opened", "repository": {"full_name": "o/r"},
                       "issue": {"number": 3, "labels": [{"name": "fleet"}]}}).encode()
    path = tmp_path / "spool" / edge_mod.spool_name("github", "same-id")
    path.write_bytes(b'{"envelope": ')
    real_loads = edge_mod.json.loads

    def _loads_then_redeliver(raw, *a, **k):
        if raw == b'{"envelope": ':
            # The platform redelivers the same id after the forwarder read the corrupt bytes.
            assert edge.receive("github", _signed_github_headers(edge_mod, key, body, "same-id"), body)[0] == 202
        return real_loads(raw, *a, **k)

    monkeypatch.setattr(edge_mod.json, "loads", _loads_then_redeliver)
    edge._forward_one(path, time.time())
    monkeypatch.setattr(edge_mod.json, "loads", real_loads)
    assert path.exists(), "the accepted redelivery was moved away with the corrupt entry"
    assert json.loads(path.read_bytes())["envelope"]["delivery_id"] == "same-id"
    assert not (tmp_path / "spool" / "dead-letter" / path.name).exists()


def test_forwarder_survives_a_transient_spool_scan_error(tmp_path, monkeypatch):
    edge_mod, edge, key = _offline_edge(tmp_path)
    body = json.dumps({"action": "opened", "repository": {"full_name": "o/r"},
                       "issue": {"number": 3, "labels": [{"name": "fleet"}]}}).encode()
    assert edge.receive("github", _signed_github_headers(edge_mod, key, body, "scan-err"), body)[0] == 202
    real_glob, failed = edge_mod.Path.glob, []

    def _glob_once_failing(self, pattern):
        if self == edge.spool and not failed:
            failed.append(True)
            raise OSError(5, "transient I/O error")
        return real_glob(self, pattern)

    forwarded = threading.Event()
    monkeypatch.setattr(edge_mod.Path, "glob", _glob_once_failing)
    monkeypatch.setattr(edge, "_post", lambda env: forwarded.set() or True)
    edge.start()
    try:
        assert forwarded.wait(30), "the forwarder died on one spool-scan error and never forwarded"
        assert failed, "the injected scan error never fired"
        assert edge._forwarder.is_alive()
    finally:
        edge.stop()
        edge._forwarder.join(10)


def test_an_unwritable_scope_state_refuses_the_delivery_without_spooling(tmp_path, monkeypatch):
    edge_mod = _edge_module()
    key_file = tmp_path / "gh-key"
    key = secrets.token_bytes(32)
    key_file.write_bytes(key)
    edge = edge_mod.Edge({"forward_url": "http://127.0.0.1:9/webhooks/ingress",
                          "spool_dir": str(tmp_path / "state" / "spool"),
                          "platforms": {"github": {"secret_file": str(key_file)}},
                          "scope": {"repo": "o/r", "ref_prefix": "ing-", "label_prefix": "ing-"}})
    body = json.dumps({"action": "opened", "repository": {"full_name": "o/r"},
                       "pull_request": {"number": 5, "head": {"sha": "a" * 40, "ref": "ing-fix"}}}).encode()
    headers = {**_signed_github_headers(edge_mod, key, body, "scope-io"),
               edge_mod.envelope.GITHUB_EVENT_HEADER: "pull_request"}
    real_write = edge.scope._write

    def _failing_write(path, data):
        raise OSError(28, "no space left on device")

    monkeypatch.setattr(edge.scope, "_write", _failing_write)
    code, _reply = edge.receive("github", headers, body)
    assert code >= 500, "a delivery whose PR registration was not recorded must be redelivered"
    assert not list(edge.spool.glob("*.json")), "nothing may be spooled ahead of its registration"
    assert edge.scope.prs == {}, "memory must not run ahead of the scope file"

    monkeypatch.setattr(edge.scope, "_write", real_write)
    assert edge.receive("github", headers, body) == (202, {"status": "accepted", "delivery_id": "scope-io"})
    state = json.loads((tmp_path / "state" / "scope.json").read_bytes())
    assert state["prs"]["5"]["head_branch"] == "ing-fix"


def test_scope_state_written_under_other_rules_is_refused_at_start(tmp_path):
    edge_mod = _edge_module()
    key_file = tmp_path / "gh-key"
    key = secrets.token_bytes(32)
    key_file.write_bytes(key)
    cfg = {"forward_url": "http://127.0.0.1:9/webhooks/ingress", "spool_dir": str(tmp_path / "state" / "spool"),
           "platforms": {"github": {"secret_file": str(key_file)}},
           "scope": {"repo": "o/r", "ref_prefix": "ing-", "label_prefix": "ing-"}}
    body = json.dumps({"action": "opened", "repository": {"full_name": "o/r"},
                       "pull_request": {"number": 5, "head": {"sha": "a" * 40, "ref": "ing-fix"}}}).encode()
    headers = {**_signed_github_headers(edge_mod, key, body, "scope-rules"),
               edge_mod.envelope.GITHUB_EVENT_HEADER: "pull_request"}
    assert edge_mod.Edge(cfg).receive("github", headers, body)[0] == 202
    assert edge_mod.Edge(cfg).scope.prs["5"]["head_branch"] == "ing-fix", "same rules reuse the registrations"
    with pytest.raises(ValueError, match="different scope"):
        edge_mod.Edge({**cfg, "scope": {"repo": "other/r", "ref_prefix": "ing-", "label_prefix": "ing-"}})
    state_file = tmp_path / "state" / "scope.json"
    unbound = json.loads(state_file.read_bytes())
    unbound.pop("rules")
    state_file.write_bytes(json.dumps(unbound).encode())
    with pytest.raises(ValueError, match="different scope"):
        edge_mod.Edge(cfg)


# --------------------------------------------------------------------------- coalesced owner drain (E13)

E13_REPO, E13_OWNER, E13_ORCH = "o/r", "fixer", "orchestrator"


def _seed(root: Path, session_id: str, *, parent=None, markers=(), compacted=False) -> None:
    """Seed a fixer session with the release SessionDB, as the acceptance file does."""
    import sqlite3

    from hermes_state import SessionDB

    home = root / "profiles" / E13_OWNER
    home.mkdir(parents=True, exist_ok=True)
    db = SessionDB(db_path=home / "state.db")
    try:
        if db.get_session(session_id) is None:
            db.create_session(session_id, "cli", parent_session_id=parent)
        for text in markers:
            db.append_message(session_id, "user", text)
        if compacted:
            conn = sqlite3.connect(home / "state.db")
            try:
                conn.execute("UPDATE messages SET active = 0, compacted = 1 WHERE session_id = ?", (session_id,))
                conn.commit()
            finally:
                conn.close()
        if parent:
            db.end_session(parent, "compression")
    finally:
        db.close()


def _fleet(tmp_path: Path, monkeypatch, **settings) -> SimpleNamespace:
    root, module = _loaded_plugin(tmp_path, monkeypatch, {
        "orchestrator_profile": E13_ORCH, "issue_labels": ["fleet"], "per_pr_hourly_budget": 30, **settings})
    (root / "profiles" / E13_ORCH).mkdir(parents=True, exist_ok=True)
    fleet = SimpleNamespace(root=root, module=module, turns=[])
    _record_turns(fleet, monkeypatch)
    return fleet


def _record_turns(fleet, monkeypatch, rc: int = 0, side_effect=None) -> None:
    def _run_cli(argv, prompt_path, **_kw):
        fleet.turns.append({"argv": list(argv), "prompt": Path(prompt_path).read_text(encoding="utf-8")})
        if side_effect is not None:
            side_effect()
        return rc

    monkeypatch.setattr(fleet.module.deliver, "run_cli", _run_cli)


def _own(fleet, pr: int, session: str) -> None:
    conn = fleet.module.ledger.connect()
    try:
        conn.execute("INSERT INTO pr_owner (repo, pr, owner_profile, session_id, thread_id, claimed_at)"
                     " VALUES (?, ?, ?, ?, ?, 0) ON CONFLICT(repo, pr) DO UPDATE SET session_id = excluded.session_id",
                     (E13_REPO, pr, E13_OWNER, session, session))
        conn.commit()
    finally:
        conn.close()


def _event(fleet, delivery: str, *, pr: int = 7, event: str = "check_run", body: str = "please look") -> None:
    envelope = fleet.module.envelope
    base = {"repository": {"full_name": E13_REPO}, "sender": {"login": "octo"}}
    if event == "issue_comment":
        payload = {**base, "action": "created", "issue": {"number": pr, "pull_request": {"url": "u"}},
                   "comment": {"body": body}}
    elif event == "issues":
        payload = {**base, "action": "opened", "issue": {"number": pr, "title": "t", "labels": [{"name": "fleet"}]}}
    else:
        payload = {**base, "action": "completed", "check_run": {
            "head_sha": "a" * 40, "status": "completed", "conclusion": "failure", "name": "check",
            "pull_requests": [{"number": pr}], "check_suite": {"head_branch": "feat"}}}
    headers = {envelope.GITHUB_EVENT_HEADER: event, envelope.GITHUB_DELIVERY_HEADER: delivery}
    out = fleet.module.ledger.stage(envelope.normalize("github", event, headers, payload), {"issue_labels": ["fleet"]})
    assert out["stage"] == "new"


def _mark(delivery: str, pr: int | None = 7) -> str:
    return f"ingress-delivery: github/{delivery}" + (f"/{pr}" if pr is not None else "")


def _markers(turn: dict) -> list[str]:
    return [ln for ln in turn["prompt"].splitlines() if ln.startswith("ingress-delivery: ")]


def _ledger_rows(fleet, sql: str, args=()) -> list:
    conn = fleet.module.ledger.connect()
    try:
        return list(conn.execute(sql, args))
    finally:
        conn.close()


def _state(fleet, delivery: str) -> str:
    return _ledger_rows(fleet, "SELECT state FROM deliveries WHERE outbox_delivery = ?", (delivery,))[0][0]


def _budget(fleet, pr: int = 7) -> int:
    return _ledger_rows(fleet, "SELECT COALESCE(SUM(count), 0) FROM budget WHERE repo = ? AND pr = ?",
                        (E13_REPO, pr))[0][0]


def _resumed(turn: dict) -> str | None:
    argv = turn["argv"]
    return argv[argv.index("--resume") + 1] if "--resume" in argv else None


def test_a_burst_for_one_pr_and_session_is_one_turn_with_every_marker(tmp_path, monkeypatch):
    fleet = _fleet(tmp_path, monkeypatch)
    _seed(fleet.root, "S")
    _own(fleet, 7, "S")
    ids = [f"d-burst-{i:02d}" for i in range(25)]
    for d in ids:
        _event(fleet, d)
    fleet.module.drain_once()
    assert len(fleet.turns) == 1, "a 25-event burst for one PR and session must be one resumed turn"
    assert _resumed(fleet.turns[0]) == "S"
    assert _markers(fleet.turns[0]) == [_mark(d) for d in ids], "every row's marker, alone on its line, in id order"
    assert {_state(fleet, d) for d in ids} == {"done"}


def test_group_caps_split_by_rows_and_by_prompt_bytes(tmp_path, monkeypatch):
    fleet = _fleet(tmp_path, monkeypatch, max_group_rows=4)
    _seed(fleet.root, "S")
    _own(fleet, 7, "S")
    rows = [f"d-cap-{i}" for i in range(7)]
    for d in rows:
        _event(fleet, d)
    fleet.module.drain_once()
    assert [_markers(t) for t in fleet.turns] == [[_mark(d) for d in rows[:4]]]
    assert [_state(fleet, d) for d in rows[4:]] == ["pending"] * 3, "rows past the row cap wait for the next pass"
    fleet.module.drain_once()
    assert [_markers(t) for t in fleet.turns[1:]] == [[_mark(d) for d in rows[4:]]]

    fleet.turns.clear()
    bridge, deliver = fleet.module.bridge, fleet.module.deliver
    big = [f"d-big-{i}" for i in range(4)]
    for d in big[:3]:
        _event(fleet, d, event="issue_comment", body="x" * 1900)
    envs = [json.loads(raw) for (raw,) in _ledger_rows(
        fleet, "SELECT envelope FROM outbox WHERE delivery_id LIKE 'd-big-%' ORDER BY id")]
    sizes = {len(deliver.prompt(env, 7, to_orchestrator=False).encode("utf-8")) for env in envs}
    assert len(sizes) == 1
    one = sizes.pop()
    settings = {"orchestrator_profile": E13_ORCH, "per_pr_hourly_budget": 30, "max_group_rows": 25,
                "max_group_prompt_bytes": one + 20}
    for n in range(3):
        bridge.drain_once(settings, "e13-byte-cap")
        assert [_markers(t) for t in fleet.turns] == [[_mark(d)] for d in big[:n + 1]], \
            "the byte cap, not the row cap, splits these rows into one per pass"
    _event(fleet, big[3], event="issue_comment", body="x" * 1900)
    bridge.drain_once({**settings, "max_group_prompt_bytes": one - 1}, "e13-byte-cap")
    (raw,) = _ledger_rows(fleet, "SELECT envelope FROM outbox WHERE delivery_id = ?", (big[3],))[0]
    assert fleet.turns[-1]["prompt"] == deliver.prompt(json.loads(raw), 7, to_orchestrator=False), \
        "a single row over the byte cap is delivered alone, unchanged"
    assert _state(fleet, big[3]) == "done"


def test_groups_never_combine_prs_and_bot_chat_rows_go_alone(tmp_path, monkeypatch):
    fleet = _fleet(tmp_path, monkeypatch)
    _seed(fleet.root, "S")
    _own(fleet, 7, "S")
    _own(fleet, 8, "S")
    for d, pr in (("d-p7a", 7), ("d-p8a", 8), ("d-p7b", 7), ("d-p8b", 8)):
        _event(fleet, d, pr=pr)
    _event(fleet, "d-issue", pr=55, event="issues")
    fleet.module.drain_once()
    assert [_markers(t) for t in fleet.turns] == [
        [_mark("d-p7a"), _mark("d-p7b")], [_mark("d-p8a", 8), _mark("d-p8b", 8)], [_mark("d-issue", None)]]
    assert [_resumed(t) for t in fleet.turns[:2]] == ["S", "S"]
    issue = fleet.turns[2]["argv"]
    assert "--resume" not in issue and issue[issue.index("-p") + 1] == E13_ORCH, "a Bot Chat row is its own turn"


def test_rows_stored_against_a_root_and_its_tip_share_one_turn(tmp_path, monkeypatch):
    fleet = _fleet(tmp_path, monkeypatch)
    _seed(fleet.root, "S-root")
    _seed(fleet.root, "S-tip", parent="S-root")
    _own(fleet, 7, "S-root")
    _event(fleet, "d-on-root")
    fleet.module.bridge.plan_and_resolve(E13_ORCH)
    _own(fleet, 7, "S-tip")
    _event(fleet, "d-on-tip")
    fleet.module.drain_once()
    assert len(fleet.turns) == 1 and _resumed(fleet.turns[0]) == "S-tip"
    assert _markers(fleet.turns[0]) == [_mark("d-on-root"), _mark("d-on-tip")]
    assert _ledger_rows(fleet, "SELECT outbox_delivery, target_session FROM deliveries ORDER BY id") == [
        ("d-on-root", "S-root"), ("d-on-tip", "S-tip")], "a stored target session is never rewritten"


def test_rows_already_committed_are_reconciled_before_the_turn(tmp_path, monkeypatch):
    fleet = _fleet(tmp_path, monkeypatch)
    _seed(fleet.root, "S-root", markers=[_mark("d-done")], compacted=True)
    _seed(fleet.root, "S-tip", parent="S-root")
    _own(fleet, 7, "S-root")
    _event(fleet, "d-done")
    _event(fleet, "d-new")
    fleet.module.drain_once()
    assert [_markers(t) for t in fleet.turns] == [[_mark("d-new")]], "a committed row never rides the group turn"
    assert (_state(fleet, "d-done"), _state(fleet, "d-new")) == ("done", "done")
    assert _budget(fleet) == 2, "a row reconciled by its marker still counts once toward its PR's hour"

    fleet.turns.clear()
    _seed(fleet.root, "S-tip", markers=[_mark("d-all-1"), _mark("d-all-2")])
    _event(fleet, "d-all-1")
    _event(fleet, "d-all-2")
    fleet.module.drain_once()
    assert fleet.turns == [], "a unit whose rows are all committed runs no turn"
    assert (_state(fleet, "d-all-1"), _state(fleet, "d-all-2")) == ("done", "done")
    assert _budget(fleet) == 4


def test_a_failed_group_turn_keeps_only_the_uncommitted_rows_pending(tmp_path, monkeypatch):
    fleet = _fleet(tmp_path, monkeypatch)
    _seed(fleet.root, "S")
    _own(fleet, 7, "S")
    for d in ("d-a", "d-b", "d-c"):
        _event(fleet, d)
    _record_turns(fleet, monkeypatch, rc=1,
                  side_effect=lambda: _seed(fleet.root, "S", markers=[_mark("d-a"), _mark("d-b")]))
    before = time.time()
    fleet.module.drain_once()
    assert len(fleet.turns) == 1
    assert (_state(fleet, "d-a"), _state(fleet, "d-b")) == ("done", "done"), "rows whose markers landed are done"
    assert _budget(fleet) == 2, "each reconciled row counts once"
    (row,) = _ledger_rows(fleet, "SELECT state, attempts, next_attempt_at, target_profile, target_session"
                                 " FROM deliveries WHERE outbox_delivery = 'd-c'")
    assert row[:2] == ("pending", 1) and row[2] > before and row[3:] == (E13_OWNER, "S")

    conn = fleet.module.ledger.connect()
    try:
        conn.execute("UPDATE deliveries SET next_attempt_at = 0 WHERE outbox_delivery = 'd-c'")
        conn.commit()
    finally:
        conn.close()
    fleet.turns.clear()
    _record_turns(fleet, monkeypatch)
    fleet.module.drain_once()
    assert [_markers(t) for t in fleet.turns] == [[_mark("d-c")]], "the next pass carries only the rest"
    assert _budget(fleet) == 3


def test_a_group_staged_before_a_crash_is_one_turn_after_the_next_load(tmp_path, monkeypatch):
    from hermes_cli.plugins import PluginManager

    fleet = _fleet(tmp_path, monkeypatch)
    _seed(fleet.root, "S")
    _own(fleet, 7, "S")
    _event(fleet, "d-pre-1")
    _event(fleet, "d-pre-2")
    assert fleet.turns == []
    name = fleet.module.__name__
    for manager in list(_MANAGERS):
        manager.unload()
    _MANAGERS.clear()
    for mod in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
        monkeypatch.delitem(sys.modules, mod, raising=False)
    manager = PluginManager()
    manager.discover_and_load()
    _MANAGERS.append(manager)
    fleet.module = manager._plugins["nv-ingress"].module
    _record_turns(fleet, monkeypatch)
    fleet.module.drain_once()
    assert [_markers(t) for t in fleet.turns] == [[_mark("d-pre-1"), _mark("d-pre-2")]]


def test_a_group_turn_counts_every_row_and_flags_the_hour_once(tmp_path, monkeypatch, caplog):
    import logging

    fleet = _fleet(tmp_path, monkeypatch, per_pr_hourly_budget=3)
    _seed(fleet.root, "S")
    _own(fleet, 7, "S")
    with caplog.at_level(logging.WARNING):
        for i in range(5):
            _event(fleet, f"d-over-{i}")
        fleet.module.drain_once()
        assert len(fleet.turns) == 1 and len(_markers(fleet.turns[0])) == 5
        assert _budget(fleet) == 5
        for i in range(2):
            _event(fleet, f"d-more-{i}")
        fleet.module.drain_once()
    assert _budget(fleet) == 7, "a budget never holds back owner delivery"
    assert _ledger_rows(fleet, "SELECT repo, pr FROM runaway") == [(E13_REPO, 7)]
    warned = [r for r in caplog.records if "runaway" in r.getMessage().lower() and f"{E13_REPO}#7" in r.getMessage()]
    assert len(warned) == 1, "the breach is logged once per PR and hour"
    assert all("--resume" in t["argv"] for t in fleet.turns), "the orchestrator receives nothing for a budget breach"


def test_a_group_of_one_is_the_single_row_prompt(tmp_path, monkeypatch):
    fleet = _fleet(tmp_path, monkeypatch)
    _seed(fleet.root, "S")
    _own(fleet, 7, "S")
    _event(fleet, "d-solo")
    fleet.module.drain_once()
    (raw,) = _ledger_rows(fleet, "SELECT envelope FROM outbox WHERE delivery_id = 'd-solo'")[0]
    assert [t["prompt"] for t in fleet.turns] == [fleet.module.deliver.prompt(json.loads(raw), 7,
                                                                             to_orchestrator=False)]
