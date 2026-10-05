"""Acceptance tests for ING-F66 — the inbound gateway (plugin ``nv-ingress``).

Ships to ``tests/plugins/test_nv_ingress_acceptance.py``. One ``test_ac_ing_f66_<n>``
per ``pytest:`` row of the ADR's ``## Acceptance criteria`` (AC-1..18, AC-22, AC-23, AC-25, AC-26),
in ADR order; AC-19/AC-20/AC-24 (live) and AC-21 (ui) are scenario files under
``tests/e2e-scenarios/ING-F66/``. Shape: ``tests/hermes_cli/test_plugin_api_compat.py``
— isolated HERMES_HOME, empty bundled dir, real ``PluginManager().discover_and_load()``.

Every signing secret is generated per run and never committed. On the stock
v2026.8.31 tree (and on the fork base d0402936) ``plugins/nv-ingress`` does not
exist, so the fixture fails before any behavioural assert.

Plugin seams the builder must provide (named in ADR §Design, imported here):
  envelope.FIELDS / normalize(source, event, headers, payload) -> dict
  edge.make_server(config) -> (server, thread)    [stdlib, host side]
  deliver.run_cli(argv, prompt_path) -> int       [single subprocess seam]
  ledger.connect() -> sqlite3.Connection          [the DEFAULT-home fleet ledger]
  ledger.resolve_owner(repo, pr) -> row | None    [the single owner lookup]
  ledger tables: pr_owner, pr_heads, outbox, issue_seen, claim_refusals(repo, pr, holder,
    claimant, claimant_session, reason), deliveries(outbox_delivery, pr, target_profile,
    target_session, state)
  the `webhook` platform registered when settings.webhook_guard is true [D14 guard adapter]
  `hermes ingress status --json` -> {pending, stuck, runaway, refusals, unclaimable, webhook_guard}
  module.drain_once() -> int                      [one drain pass]
  edge config `scope` {repo, ref_prefix, label_prefix} -> 202 {"status": "out_of_scope"} for
    out-of-scope events, `GET /scope` -> {"counts": {<event>: n}}; registrations in
    <parent of spool_dir>/scope.json                                     [ADR D2 Scope, r7]
  the drain driver: one daemon thread per load, passes only while this process owns the
    gateway runtime lock -- it calls gateway.status.owns_gateway_runtime_lock() (looked up on
    the module at each pass) before every pass -- stopped by PluginManager.unload()  [ADR D5]
  skills/ci-gate/scripts/checks_gate.py evaluate(runs, pinned_sha, current_head, waivers);
    CLI <repo> <pr> <sha> --read provider|anonymous
"""
from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from gateway.config import Platform, PlatformConfig
from hermes_cli.plugins import PluginManager
from tools.registry import invalidate_check_fn_cache, registry

PLUGIN_KEY = "nv-ingress"
REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_SRC = REPO_ROOT / "plugins" / PLUGIN_KEY
ING_SPEC = REPO_ROOT / "tests" / "e2e-scenarios" / "ING-F66" / "spec" / "openshell" / "coworker-types.yaml"
DOCS_PAGE = REPO_ROOT / "website" / "docs" / "user-guide" / "fleet-ingress.md"
FLEET_PLUGINS = ("nv-coworker-compose", "nv-fleet-gates", PLUGIN_KEY)
ROUTE = "ingress"
ORCH = "orchestrator"
OWNER = "fixer"
OTHER = "reviewer"
REPO = "o/ing-f66"
LABEL = "fleet"
CI_EVENTS = {"check_run", "check_suite", "workflow_run"}
# Header NAMES as the release adapter reads them (gateway/platforms/webhook.py:753-754, :847, :1121-1132).
GH_SIG_HEADER = "X-Hub-Signature-256"
GL_EVENT_HEADER = "X-Gitlab-Event"
GL_TOKEN_HEADER = "X-Gitlab-Token"
# GitLab signing-token delivery headers (GitLab webhook docs, §Delivery headers / §Signing tokens).
GL_ID_HEADER = "webhook-id"
GL_TS_HEADER = "webhook-timestamp"
GL_SIG_HEADER = "webhook-signature"
TRANSPORT_EVENT = "ingress.v1"
PLATFORM_HEADERS = {h.lower() for h in ("X-GitHub-Event", "X-GitHub-Delivery", GH_SIG_HEADER, GL_EVENT_HEADER,
                                         GL_TOKEN_HEADER, GL_ID_HEADER, GL_TS_HEADER, GL_SIG_HEADER,
                                         "svix-id", "svix-timestamp", "svix-signature")}
OWNED_EVENT_CASES = [
    ("check_run", "completed", "failure"),
    ("check_run", "completed", "success"),
    ("check_suite", "completed", "failure"),
    ("check_suite", "completed", "success"),
    ("workflow_run", "completed", "failure"),
    ("workflow_run", "completed", "success"),
    ("pull_request", "synchronize", None),
    ("pull_request", "closed", None),
    ("pull_request", "reopened", None),
    ("pull_request_review", "submitted", None),
    ("pull_request_review_comment", "created", None),
    ("issue_comment", "created", None),
]


# --------------------------------------------------------------------------- helpers

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _load(rel: str, name: str):
    path = PLUGIN_SRC / rel
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _gh_sign(secret: bytes, body: bytes) -> str:
    # The GitHub scheme the release adapter verifies (gateway/platforms/webhook.py:1121-1127).
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def _gh_payload(event: str, action: str, *, pr: int = 7, head: str = "a" * 40, branch: str = "feat",
                conclusion=None, labels=(), sender="octo", sender_type="User",
                issue: int = 11, pr_numbers=None, repo_name: str = REPO) -> dict:
    repo = {"full_name": repo_name}
    who = {"login": sender, "type": sender_type}
    nums = [{"number": n} for n in (pr_numbers if pr_numbers is not None else [pr])]
    pr_obj = {"number": pr, "head": {"sha": head, "ref": branch}}
    if event == "issues":
        return {"action": action, "repository": repo, "sender": who,
                "issue": {"number": issue, "title": "t", "labels": [{"name": n} for n in labels]},
                "label": {"name": labels[0]} if (action == "labeled" and labels) else None}
    if event == "check_run":
        return {"action": action, "repository": repo, "sender": who,
                "check_run": {"head_sha": head, "status": "completed", "conclusion": conclusion,
                              "name": "check", "pull_requests": nums,
                              "check_suite": {"head_branch": branch}}}
    if event == "check_suite":
        return {"action": action, "repository": repo, "sender": who,
                "check_suite": {"head_sha": head, "head_branch": branch, "status": "completed",
                                "conclusion": conclusion, "name": "check", "pull_requests": nums}}
    if event == "workflow_run":
        return {"action": action, "repository": repo, "sender": who,
                "workflow_run": {"head_sha": head, "head_branch": branch, "conclusion": conclusion,
                                 "name": "ci", "pull_requests": nums}}
    if event == "issue_comment":
        return {"action": action, "repository": repo, "sender": who,
                "issue": {"number": pr, "pull_request": {"url": "u"}},
                "comment": {"body": "please look", "user": who}}
    if event == "pull_request_review":
        return {"action": action, "repository": repo, "sender": who, "pull_request": pr_obj,
                "review": {"state": "changes_requested", "body": "fix"}}
    if event == "pull_request_review_comment":
        return {"action": action, "repository": repo, "sender": who, "pull_request": pr_obj,
                "comment": {"body": "nit", "user": who}}
    return {"action": action, "repository": repo, "sender": who,
            "pull_request": {**pr_obj, "merged": False}}


def _gh_headers(event: str, delivery: str) -> dict:
    return {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery}


_PR_CREATE = " ".join(("gh", "pr", "create"))


def _seed_session(home: Path, session_id: str, *, title=None, thread_id=None,
                  parent=None, messages=(), compacted=False) -> None:
    """Seed a session in ``<home>/state.db`` with the release SessionDB (hermes_state.py:4807)."""
    from hermes_state import SessionDB

    home.mkdir(parents=True, exist_ok=True)
    db = SessionDB(db_path=home / "state.db")
    try:
        db.create_session(session_id, "cli", thread_id=thread_id, parent_session_id=parent)
        if title:
            db.set_session_title(session_id, title)
        for text in messages:
            db.append_message(session_id, "user", text)
        if compacted:
            conn = sqlite3.connect(home / "state.db")
            try:  # the in-place compaction row shape (hermes_state.py:12488-12510)
                conn.execute("UPDATE messages SET active = 0, compacted = 1 WHERE session_id = ?",
                             (session_id,))
                conn.commit()
            finally:
                conn.close()
        if parent:
            db.end_session(parent, "compression")
    finally:
        db.close()


@pytest.fixture
def ingress(tmp_path, monkeypatch):
    """nv-ingress loaded through the real discovery path from an isolated home."""
    root = tmp_path / "root"
    (root / "plugins").mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, root / "plugins" / PLUGIN_KEY)
    settings = {"route": ROUTE, "orchestrator_profile": ORCH, "issue_labels": [LABEL],
                "per_pr_hourly_budget": 3, "self_logins": ["fleet-bot"], "drain_interval_seconds": 1,
                "webhook_guard": True}
    (root / "config.yaml").write_text(yaml.safe_dump({
        "plugins": {"enabled": [PLUGIN_KEY], "entries": {PLUGIN_KEY: {"settings": settings}}},
    }), encoding="utf-8")
    for prof in (ORCH, OWNER, OTHER):
        (root / "profiles" / prof).mkdir(parents=True)
    (root / "scripts").mkdir()
    shutil.copy(PLUGIN_SRC / "route_scripts" / "ingress_stage.py", root / "scripts" / "ingress_stage.py")
    bundled = tmp_path / "empty-bundled"
    bundled.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")

    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled is True and loaded.error is None and loaded.module is not None
    for hook in ("pre_gateway_dispatch", "post_tool_call", "pre_tool_call", "on_kanban_dispatch_tick"):
        assert hook in loaded.hooks_registered
    assert {"ingress_remap", "ingress_claim_replacement"} <= set(loaded.tools_registered)
    assert "ingress" in {n for n, e in manager._cli_commands.items() if e.get("plugin_key") == PLUGIN_KEY}
    from gateway.platform_registry import platform_registry

    entry = platform_registry.get("webhook")
    assert entry is not None and entry.source == "plugin" and entry.plugin_name == PLUGIN_KEY

    pkg = loaded.module.__name__
    deliver = sys.modules[pkg + ".deliver"]
    calls: list[dict] = []

    def _fake_run_cli(argv, prompt_path, **_kw):
        calls.append({"argv": list(argv), "prompt": Path(prompt_path).read_text(encoding="utf-8")})
        return 0

    monkeypatch.setattr(deliver, "run_cli", _fake_run_cli)
    yield SimpleNamespace(manager=manager, loaded=loaded, module=loaded.module, root=root,
                          calls=calls, deliver=deliver, ledger=sys.modules[pkg + ".ledger"],
                          envelope=sys.modules[pkg + ".envelope"], tmp=tmp_path)
    # Unloading stops this load's drain driver thread (D5), so no driver outlives its test.
    manager.unload()


def _set_profile(monkeypatch, name: str) -> None:
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: name)
    invalidate_check_fn_cache()


def _rows(ing, sql: str, args=()) -> list:
    conn = ing.ledger.connect()
    try:
        return list(conn.execute(sql, args))
    finally:
        conn.close()


def _stage(ing, event: str, delivery: str, payload: dict, source: str = "github") -> dict:
    """Run the REAL route script exactly as the release adapter does
    (gateway/platforms/webhook_filters.py:228-300: payload JSON on stdin, JSON out)."""
    from gateway.platforms.webhook_filters import WebhookRouteProcessor

    headers = (_gh_headers(event, delivery) if source == "github"
               else {GL_EVENT_HEADER: event, GL_ID_HEADER: delivery})
    env_payload = ing.envelope.normalize(source, event, headers, payload)
    keep, out = WebhookRouteProcessor(script_timeout_seconds=30).run_route_script(
        "ingress_stage.py", env_payload)
    assert keep is True and isinstance(out, dict), "a valid envelope is never dropped by staging"
    return out


def _dispatch_event(ing, out: dict, delivery: str, route: str = ROUTE) -> list:
    """Fire pre_gateway_dispatch as the gateway does for a webhook event (run.py:18131-18172)."""
    event = SimpleNamespace(
        text="", message_id=delivery, raw_message=out,
        source=SimpleNamespace(platform=Platform.WEBHOOK, chat_type="webhook",
                               chat_id=f"webhook:{route}:{delivery}"))
    return ing.manager.invoke_hook("pre_gateway_dispatch", event=event, gateway=None,
                                   session_store=None, future_additive_field=True)


def _ingest(ing, event: str, delivery: str, payload: dict, source: str = "github") -> dict:
    out = _stage(ing, event, delivery, payload, source)
    results = _dispatch_event(ing, out, delivery)
    assert any(isinstance(r, dict) and r.get("action") == "skip" for r in results), \
        "the ingress route must never mint a one-shot webhook session"
    ing.module.drain_once()
    return out


def _claim(ing, monkeypatch, profile: str, session_id: str, pr: int, *, extra: str = "") -> None:
    _set_profile(monkeypatch, profile)
    ing.manager.invoke_hook(
        "post_tool_call", tool_name="terminal",
        args={"command": f"{_PR_CREATE} --draft --fill {extra}".strip()},
        result=f"https://github.com/{REPO}/pull/{pr}\n", status="ok",
        task_id=session_id, session_id=session_id, tool_call_id="tc", turn_id="t",
        duration_ms=1)


def _owner_row(ing, pr: int):
    rows = _rows(ing, "SELECT owner_profile, session_id, thread_id FROM pr_owner WHERE repo = ? AND pr = ?",
                 (REPO, pr))
    return rows[0] if rows else None


def _deliveries_to(ing, profile: str) -> list[dict]:
    return [c for c in ing.calls if c["argv"][c["argv"].index("-p") + 1] == profile]


# --------------------------------------------------------------------------- edge harness

class _Relay:
    """Stand-in for OpenShell's forward: a loopback TCP relay that counts connections."""

    def __init__(self, target_port: int):
        self.port = _free_port()
        self.target = target_port
        self.connections = 0
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.listen(8)
        threading.Thread(target=self._serve, daemon=True).start()

    def _pipe(self, a, b):
        try:
            while True:
                data = a.recv(65536)
                if not data:
                    break
                b.sendall(data)
        except OSError:
            pass
        finally:
            for s in (a, b):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def _serve(self):
        while True:
            try:
                client, _ = self._sock.accept()
            except OSError:
                return
            self.connections += 1
            upstream = socket.create_connection(("127.0.0.1", self.target))
            threading.Thread(target=self._pipe, args=(client, upstream), daemon=True).start()
            threading.Thread(target=self._pipe, args=(upstream, client), daemon=True).start()

    def close(self):
        self._sock.close()


def _guarded(extra: dict):
    """The `webhook` adapter the gateway would create. Plugin platforms are looked up first
    (gateway/run.py:17370-17392), so this is nv-ingress's guard subclass when it is registered."""
    from gateway.platform_registry import platform_registry

    return platform_registry.create_adapter("webhook", PlatformConfig(enabled=True, extra=extra))


def _serve_adapter(adapter):
    """Run the adapter's real ``connect()`` (route validation, dynamic-route load, bind) on a loop thread."""
    import asyncio

    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    assert asyncio.run_coroutine_threadsafe(adapter.connect(), loop).result(30) is True
    return loop


def _stop_adapter(adapter, loop) -> None:
    import asyncio

    asyncio.run_coroutine_threadsafe(adapter.disconnect(), loop).result(30)
    loop.call_soon_threadsafe(loop.stop)


def _rendered_ingress(tmp_path) -> tuple[dict, dict]:
    """The ING-F66 render's DEFAULT webhook `extra` and its ingress route, as the gateway would load them."""
    out = tmp_path / "render-ingress"
    if not (out / "host" / "edge.yaml").exists():
        _render(tmp_path, out)
    extra = (_webhook(out).get("extra") or {})
    return extra, (extra.get("routes") or {})[ROUTE]


def _start_route(ing, port: int, route: dict):
    """The nv-ingress `webhook` adapter on loopback, serving the rendered ingress route."""
    adapter = _guarded({"host": "127.0.0.1", "port": port, "routes": {ROUTE: route}})
    assert adapter is not None
    hooked, seen_headers = [], []
    real_handle = adapter._handle_webhook

    async def _handle(event):  # what the gateway runner would do: fire the bridge hook
        hooked.append(_dispatch_event(ing, event.raw_message, event.message_id))

    async def _recording_handle(request):
        seen_headers.append({k.lower() for k in request.headers})
        return await real_handle(request)

    adapter.handle_message = _handle
    adapter._handle_webhook = _recording_handle  # bound into the app by connect() below
    loop = _serve_adapter(adapter)
    return SimpleNamespace(adapter=adapter, hooked=hooked, seen_headers=seen_headers, loop=loop, port=port)


def _status(ing, capsys) -> dict:
    """`hermes ingress status --json`, through the plugin's registered CLI handler."""
    capsys.readouterr()
    cli = ing.manager._cli_commands["ingress"]["handler_fn"]
    assert cli(SimpleNamespace(ingress_command="status", json=True)) in (0, None)
    return json.loads(capsys.readouterr().out)


def _gl_signing_token() -> bytes:
    # The signing-token shape GitLab documents (`whsec_` + base64 key material); generated per run.
    import base64

    return b"whsec_" + base64.b64encode(secrets.token_bytes(32))


def _start_edge(ing, forward_port: int, *, spool_dir: Path | None = None, secrets_by_platform=None, scope=None):
    """The REAL host edge on an ephemeral loopback port; signing material is generated now."""
    secrets_by_platform = secrets_by_platform or {"github": secrets.token_bytes(32),
                                                  "gitlab": _gl_signing_token()}
    platforms = {}
    for name, material in secrets_by_platform.items():
        path = ing.tmp / "host-secrets" / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(material)
        os.chmod(path, 0o600)
        platforms[name] = {"secret_file": str(path)}
    edge = _load("edge/__init__.py", "nv_ingress_edge_under_test")
    port = _free_port()
    cfg = {"listen": f"127.0.0.1:{port}",
           "forward_url": f"http://127.0.0.1:{forward_port}/webhooks/{ROUTE}",
           "spool_dir": str(spool_dir or (ing.tmp / "edge-spool")),
           "events": sorted(ing.module.ROUTED_EVENTS), "platforms": platforms}
    if scope is not None:
        cfg["scope"] = dict(scope)
    server, _thread = edge.make_server(cfg)
    return SimpleNamespace(module=edge, server=server, port=port, secrets=secrets_by_platform, cfg=cfg)


def _stop_edge(edge) -> None:
    edge.server.shutdown()
    edge.server.server_close()


def _post(port: int, path: str, body: bytes, headers: dict) -> int:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _send_github(edge, event: str, delivery: str, payload: dict) -> int:
    body = json.dumps(payload).encode()
    return _post(edge.port, "/github", body, {**_gh_headers(event, delivery),
                                              GH_SIG_HEADER: _gh_sign(edge.secrets["github"], body)})


def _gl_sign(token: bytes, msg_id: str, ts: str, body: bytes) -> str:
    # The Standard Webhooks scheme GitLab documents for signing tokens (GitLab webhook docs,
    # §Signing tokens), computed here only to produce run-time test signatures.
    import base64

    key = base64.b64decode(token.decode().removeprefix("whsec_"))
    digest = hmac.new(key, msg_id.encode() + b"." + ts.encode() + b"." + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


def _send_gitlab(edge, event: str, payload: dict, delivery: str | None, *, token: bytes | None = None,
                 ts: str | None = None, signed: bool = True) -> int:
    body = json.dumps(payload).encode()
    ts = ts or str(int(time.time()))
    headers = {GL_EVENT_HEADER: event, GL_TS_HEADER: ts}
    if delivery is not None:
        headers[GL_ID_HEADER] = delivery
    if signed:
        headers[GL_SIG_HEADER] = _gl_sign(token or edge.secrets["gitlab"], delivery or "", ts, body)
    return _post(edge.port, "/gitlab", body, headers)


@pytest.fixture
def edge_path(ingress, tmp_path):
    """edge → stand-in forward (loopback relay) → the rendered ingress route on the nv-ingress adapter → bridge."""
    _extra, rendered_route = _rendered_ingress(tmp_path)
    route_port = _free_port()
    route = _start_route(ingress, route_port, rendered_route)
    relay = _Relay(route_port)
    edge = _start_edge(ingress, relay.port)
    yield SimpleNamespace(route=route, relay=relay, edge=edge, route_port=route_port, rendered_route=rendered_route)
    _stop_edge(edge)
    relay.close()
    _stop_adapter(route.adapter, route.loop)


def _wait(pred, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.1)
    return False


def _settle(ing, path, n_hooked: int) -> None:
    assert _wait(lambda: len(path.route.hooked) >= n_hooked), "forwarded envelope never reached the route"
    assert _wait(lambda: not list(Path(path.edge.cfg["spool_dir"]).glob("*.json"))), \
        "an acknowledged forward must leave the edge spool empty"
    ing.module.drain_once()


# --------------------------------------------------------------------------- criteria

def test_ac_ing_f66_1(ingress, edge_path):
    """A new labelled issue reaches the orchestrator's canonical Bot Chat exactly once when its `opened` and `labeled` deliveries arrive under two distinct delivery ids and one of them is redelivered (floor a)."""
    opened = _gh_payload("issues", "opened", labels=(LABEL,))
    labeled = _gh_payload("issues", "labeled", labels=(LABEL,))
    for i, (delivery, payload) in enumerate((("d-open", opened), ("d-label", labeled), ("d-label", labeled))):
        assert _send_github(edge_path.edge, "issues", delivery, payload) == 202
        _settle(ingress, edge_path, min(i + 1, 2))
    assert edge_path.relay.connections >= 2
    stages = dict(_rows(ingress, "SELECT delivery_id, stage FROM outbox"))
    assert stages == {"d-open": "new", "d-label": "suppressed"}
    to_orch = _deliveries_to(ingress, ORCH)
    assert len(to_orch) == 1 and len(ingress.calls) == 1
    argv = to_orch[0]["argv"]
    assert argv[argv.index("-c") + 1] == "Bot Chat" and "--resume" not in argv
    assert "ingress-delivery: github/d-open" in to_orch[0]["prompt"]


def test_ac_ing_f66_2(ingress, edge_path):
    """The edge verifies a GitHub signature with a host-held secret, normalizes to envelope v1 and forwards it; the in-sandbox loopback route (a real release `WebhookAdapter` on 127.0.0.1 with the loopback-gated no-auth route) accepts it with 202 and the bridge stages it (floor a2, hermetic)."""
    assert edge_path.rendered_route["events"] == [TRANSPORT_EVENT]
    payload = _gh_payload("check_run", "completed", conclusion="failure")
    assert _send_github(edge_path.edge, "check_run", "d-edge-1", payload) == 202
    _settle(ingress, edge_path, 1)
    assert edge_path.relay.connections >= 1
    rows = _rows(ingress, "SELECT source, delivery_id, event, repo, kind, head_sha FROM outbox")
    assert rows == [("github", "d-edge-1", "check_run", REPO, "ci", "a" * 40)]
    hooked = edge_path.route.hooked[0]
    assert any(isinstance(r, dict) and r.get("action") == "skip" for r in hooked)
    forwarded = edge_path.route.seen_headers[0]
    assert not forwarded & PLATFORM_HEADERS, f"the forward passed platform headers through: {forwarded & PLATFORM_HEADERS}"


def test_ac_ing_f66_3(ingress, edge_path):
    """A GitLab-shaped issue event and a GitLab-shaped pipeline event, verified by the GitLab scheme, normalize to the same envelope v1 field set and kinds as their GitHub counterparts (floor a2, GitLab-ready)."""
    env = ingress.envelope
    gl_issue = {"object_kind": "issue", "project": {"path_with_namespace": REPO},
                "user": {"username": "octo"},
                "object_attributes": {"iid": 11, "action": "open", "title": "t"},
                "labels": [{"title": LABEL}]}
    gl_pipe = {"object_kind": "pipeline", "project": {"path_with_namespace": REPO},
               "user": {"username": "octo"},
               "object_attributes": {"status": "failed", "sha": "b" * 40, "name": "ci", "id": 901},
               "merge_request": {"iid": 7}}
    assert _send_gitlab(edge_path.edge, "Issue Hook", gl_issue, "gl-msg-1") == 202
    assert _send_gitlab(edge_path.edge, "Pipeline Hook", gl_pipe, "gl-msg-2") == 202
    assert _send_gitlab(edge_path.edge, "Pipeline Hook", gl_pipe, "gl-msg-2") == 202  # GitLab retry
    assert _send_gitlab(edge_path.edge, "Pipeline Hook", gl_pipe, "gl-msg-3") == 202
    assert _send_gitlab(edge_path.edge, "Issue Hook", gl_issue, "gl-msg-4", token=_gl_signing_token()) == 401
    assert _send_gitlab(edge_path.edge, "Pipeline Hook", gl_pipe, None) == 401
    _settle(ingress, edge_path, 3)
    staged = sorted(_rows(ingress, "SELECT delivery_id, kind, source, repo FROM outbox"))
    assert staged == [("gl-msg-1", "issue", "gitlab", REPO), ("gl-msg-2", "ci", "gitlab", REPO),
                      ("gl-msg-3", "ci", "gitlab", REPO)], "one row per GitLab delivery id, retries collapse"
    assert len(edge_path.route.hooked) == 3, "only the three GitLab deliveries reach the route; the id-less one never does"
    gh_issue = env.normalize("github", "issues", _gh_headers("issues", "g1"),
                             _gh_payload("issues", "opened", labels=(LABEL,)))
    gh_ci = env.normalize("github", "workflow_run", _gh_headers("workflow_run", "g2"),
                          _gh_payload("workflow_run", "completed", conclusion="failure"))
    a = env.normalize("gitlab", "Issue Hook", {GL_EVENT_HEADER: "Issue Hook", GL_ID_HEADER: "n1"}, gl_issue)
    b = env.normalize("gitlab", "Pipeline Hook", {GL_EVENT_HEADER: "Pipeline Hook", GL_ID_HEADER: "n2"}, gl_pipe)
    assert set(a) == set(gh_issue) == set(b) == set(gh_ci) == set(env.FIELDS)
    assert (a["delivery_id"], b["delivery_id"]) == ("n1", "n2")
    assert (a["kind"], a["number"], a["repo"], a["labels"]) == ("issue", 11, REPO, [LABEL])
    assert (b["kind"], b["pr_numbers"], b["conclusion"], b["head_sha"]) == ("ci", [7], "failure", "b" * 40)
    assert (gh_ci["kind"], gh_ci["pr_numbers"], gh_ci["conclusion"]) == ("ci", [7], "failure")
    assert (a["event"], a["platform_event"]) == ("issues", "Issue Hook")
    assert (b["event"], b["platform_event"]) == ("workflow_run", "Pipeline Hook")
    assert gh_issue["event"] == gh_issue["platform_event"] == "issues"


def test_ac_ing_f66_4(ingress):
    """An unsigned event and an event with a bad signature are rejected at the edge (non-2xx) and nothing is forwarded (floor b)."""
    relay = _Relay(_free_port())
    edge = _start_edge(ingress, relay.port)
    body = json.dumps(_gh_payload("issues", "opened", labels=(LABEL,))).encode()
    unsigned = _post(edge.port, "/github", body, _gh_headers("issues", "d-bad-1"))
    wrong = _post(edge.port, "/github", body, {**_gh_headers("issues", "d-bad-2"),
                                               GH_SIG_HEADER: _gh_sign(secrets.token_bytes(32), body)})
    gl_issue = {"object_kind": "issue", "project": {"path_with_namespace": REPO},
                "object_attributes": {"iid": 11, "action": "open"}}
    gl_body = json.dumps(gl_issue).encode()
    now = str(int(time.time()))
    gl_unsigned = _post(edge.port, "/gitlab", gl_body, {GL_EVENT_HEADER: "Issue Hook", GL_ID_HEADER: "gl-bad-1",
                                                        GL_TS_HEADER: now})
    gl_wrong = _send_gitlab(edge, "Issue Hook", gl_issue, "gl-bad-2", token=_gl_signing_token())
    gl_stale = _send_gitlab(edge, "Issue Hook", gl_issue, "gl-bad-3", ts=str(int(time.time()) - 3600))
    # Token-only: the plain token header carrying the very signing material, and no signature.
    gl_token_only = _post(edge.port, "/gitlab", gl_body, {GL_EVENT_HEADER: "Issue Hook", GL_ID_HEADER: "gl-bad-4",
                                                          GL_TS_HEADER: now,
                                                          GL_TOKEN_HEADER: edge.secrets["gitlab"].decode()})
    assert (unsigned, wrong, gl_unsigned, gl_wrong, gl_stale, gl_token_only) == (401,) * 6
    assert relay.connections == 0
    assert not list(Path(edge.cfg["spool_dir"]).glob("*.json"))
    _stop_edge(edge)
    relay.close()


def test_ac_ing_f66_5(ingress, monkeypatch):
    """Opening a PR in a coworker session records one ownership row (repo, PR number, owner profile, session id, thread id) in the single DEFAULT-home ledger and the first claim wins; a later same-profile re-claim is accepted only from the claimed session itself or from a verified compression continuation of it on the claimed thread, and then refreshes the row's session id to that lineage's tip session; a re-claim from an unrelated session of the same profile, or from a foreign profile, is refused with no mutation and records the refusal naming the holder; the thread id is the session's recorded thread, never derived from the PR number (floor c)."""
    owner_home = ingress.root / "profiles" / OWNER
    _seed_session(owner_home, "S-fix", title="Bot Chat", thread_id="thr-canonical-42")
    _seed_session(ingress.root / "profiles" / OTHER, "S-rev", title="Bot Chat", thread_id="thr-canonical-42")
    _claim(ingress, monkeypatch, OWNER, "S-fix", 7)
    assert _owner_row(ingress, 7) == (OWNER, "S-fix", "thr-canonical-42")
    assert not list((owner_home / "plugin-data").glob("**/*.db")), "the ledger is the DEFAULT home's"
    _claim(ingress, monkeypatch, OWNER, "S-fix", 7)
    assert _owner_row(ingress, 7) == (OWNER, "S-fix", "thr-canonical-42")
    _seed_session(owner_home, "S-fix-2", parent="S-fix", thread_id="thr-canonical-42")  # compression continuation
    _claim(ingress, monkeypatch, OWNER, "S-fix-2", 7)
    assert _owner_row(ingress, 7) == (OWNER, "S-fix-2", "thr-canonical-42")
    _seed_session(owner_home, "S-unrelated", thread_id="thr-canonical-42")  # same profile and thread, no lineage
    _claim(ingress, monkeypatch, OWNER, "S-unrelated", 7)
    assert _owner_row(ingress, 7) == (OWNER, "S-fix-2", "thr-canonical-42")
    _claim(ingress, monkeypatch, OTHER, "S-rev", 7)
    assert _owner_row(ingress, 7) == (OWNER, "S-fix-2", "thr-canonical-42")
    refused = _rows(ingress, "SELECT holder, claimant, claimant_session FROM claim_refusals "
                             "WHERE repo = ? AND pr = ? ORDER BY rowid", (REPO, 7))
    assert refused == [(OWNER, OWNER, "S-unrelated"), (OWNER, OTHER, "S-rev")]
    _claim(ingress, monkeypatch, OWNER, "", 8)
    assert _owner_row(ingress, 8) is None


def _replace(monkeypatch, profile: str, session_id: str, old_pr: int, new_pr: int) -> dict:
    _set_profile(monkeypatch, profile)
    return json.loads(registry.dispatch("ingress_claim_replacement",
                                        {"repo": REPO, "old_pr": old_pr, "new_pr": new_pr},
                                        session_id=session_id))


def _superseded(ing, pr: int):
    return _rows(ing, "SELECT superseded_by FROM pr_owner WHERE repo = ? AND pr = ?", (REPO, pr))[0][0]


def test_ac_ing_f66_6(ingress, monkeypatch):
    """A replacement PR inherits the old PR's owner profile and thread id, with that lineage's tip session as its session id, only when it is claimed from the old row's session itself or from a verified compression continuation of it on the old row's thread; a replacement claimed from an unrelated session of the same profile, or from a foreign profile, is refused with no mutation of either row (floor c, derived)."""
    owner_home = ingress.root / "profiles" / OWNER
    _seed_session(owner_home, "S-fix", thread_id="thr-xyz")
    _seed_session(ingress.root / "profiles" / OTHER, "S-rev", thread_id="thr-xyz")
    _claim(ingress, monkeypatch, OWNER, "S-fix", 7)
    _claim(ingress, monkeypatch, OWNER, "S-fix", 9, extra="--body 'Supersedes #7'")
    assert _owner_row(ingress, 9) == (OWNER, "S-fix", "thr-xyz")
    assert _superseded(ingress, 7) == 9
    _seed_session(owner_home, "S-fix-2", parent="S-fix", thread_id="thr-xyz")  # compression continuation
    assert _replace(monkeypatch, OWNER, "S-fix-2", 9, 10)["status"] != "refused"
    assert _owner_row(ingress, 10) == (OWNER, "S-fix-2", "thr-xyz")
    assert _superseded(ingress, 9) == 10
    _seed_session(owner_home, "S-unrelated", thread_id="thr-xyz")
    for profile, sid, new_pr in ((OWNER, "S-unrelated", 11), (OTHER, "S-rev", 12)):
        assert _replace(monkeypatch, profile, sid, 10, new_pr)["status"] == "refused", (profile, sid)
        assert _owner_row(ingress, new_pr) is None
        assert _owner_row(ingress, 10) == (OWNER, "S-fix-2", "thr-xyz") and _superseded(ingress, 10) is None


@pytest.mark.parametrize("event,action,conclusion", OWNED_EVENT_CASES)
def test_ac_ing_f66_7(ingress, monkeypatch, event, action, conclusion):
    """Every listed PR/review/CI event for an owned PR — check_run, check_suite, workflow_run (failure and completion), pull_request synchronize/closed/reopened, pull_request_review, pull_request_review_comment, issue_comment on the PR — is delivered by resuming the owner's EXISTING session id (its compression tip), never by creating a session, and none of them reaches the orchestrator — not even when the resume fails, in which case it stays pending for the same owner and session (floor d)."""
    owner_home = ingress.root / "profiles" / OWNER
    _seed_session(owner_home, "S-root", thread_id="thr-1")
    _seed_session(owner_home, "S-tip", parent="S-root")  # S-root compressed into S-tip
    _claim(ingress, monkeypatch, OWNER, "S-root", 7)
    _ingest(ingress, event, f"d-{event}-{action}", _gh_payload(event, action, conclusion=conclusion))
    owner_calls = _deliveries_to(ingress, OWNER)
    assert len(owner_calls) == 1, f"{event}/{action} not delivered exactly once to the owner"
    argv = owner_calls[0]["argv"]
    assert argv[argv.index("--resume") + 1] == "S-tip"
    assert "--create-if-missing" not in argv and "-c" not in argv
    assert _deliveries_to(ingress, ORCH) == []
    assert f"ingress-delivery: github/d-{event}-{action}/7" in owner_calls[0]["prompt"]
    monkeypatch.setattr(ingress.deliver, "run_cli", lambda argv, prompt_path, **_k: (
        ingress.calls.append({"argv": list(argv), "prompt": Path(prompt_path).read_text(encoding="utf-8")}) or 1))
    ingress.calls.clear()
    _ingest(ingress, event, f"d-fail-{event}-{action}", _gh_payload(event, action, conclusion=conclusion))
    for _ in range(4):
        ingress.module.drain_once()
    assert _deliveries_to(ingress, ORCH) == [], "a failed owner resume must never retarget to the orchestrator"
    assert {c["argv"][c["argv"].index("--resume") + 1] for c in ingress.calls} == {"S-tip"}
    assert _rows(ingress, "SELECT target_profile, target_session, state FROM deliveries WHERE outbox_delivery = ?",
                 (f"d-fail-{event}-{action}",)) == [(OWNER, "S-root", "pending")]


def test_ac_ing_f66_8(ingress, monkeypatch, capsys):
    """A CI event that names several PRs fans out one delivery per owned PR; a CI event with no PR numbers is delivered to the owner of the open claimed PR whose recorded head SHA or head branch matches it; until one matches it stays pending (listed in `hermes ingress status`, no time-out, never sent to the orchestrator) and is delivered once a later PR event records the head, including for a claimed PR whose opened delivery was never received; it goes to the orchestrator only when shown to belong to no claimed PR: its repo has no open claimed PR, or every open claimed PR there has a recorded head branch and none matches the event's (floor d, derived)."""
    home = ingress.root / "profiles" / OWNER
    for sid in ("S-a", "S-b", "S-c"):
        _seed_session(home, sid)
    _claim(ingress, monkeypatch, OWNER, "S-a", 7)
    _claim(ingress, monkeypatch, OWNER, "S-b", 8)
    _ingest(ingress, "check_suite", "d-fan", _gh_payload("check_suite", "completed",
                                                         conclusion="failure", pr_numbers=[7, 8]))
    resumed = sorted(c["argv"][c["argv"].index("--resume") + 1] for c in _deliveries_to(ingress, OWNER))
    assert resumed == ["S-a", "S-b"]
    ingress.calls.clear()

    # PR 9 is claimed but its opened delivery never arrived, so nothing has recorded its head.
    _claim(ingress, monkeypatch, OWNER, "S-c", 9)
    _ingest(ingress, "check_run", "d-early", _gh_payload("check_run", "completed", conclusion="failure",
                                                         head="c" * 40, branch="fix-9", pr_numbers=[]))
    for _ in range(3):
        ingress.module.drain_once()
    assert ingress.calls == [], "an unidentified CI event of a repo with a claimed PR is held, not routed"
    pending = {p["delivery_id"]: p for p in _status(ingress, capsys)["pending"]}
    assert pending["d-early"]["state"] == "awaiting_association"
    assert pending["d-early"]["head_sha"] == "c" * 40
    _ingest(ingress, "pull_request_review", "d-review-9",
            _gh_payload("pull_request_review", "submitted", pr=9, head="c" * 40, branch="fix-9"))
    ingress.module.drain_once()
    early = [c for c in _deliveries_to(ingress, OWNER) if "github/d-early/" in c["prompt"]]
    assert len(early) == 1 and early[0]["argv"][early[0]["argv"].index("--resume") + 1] == "S-c"
    assert "d-early" not in {p["delivery_id"] for p in _status(ingress, capsys)["pending"]}
    ingress.calls.clear()

    _ingest(ingress, "check_run", "d-match", _gh_payload("check_run", "completed", conclusion="failure",
                                                         head="c" * 40, branch="fix-9", pr_numbers=[]))
    matched = _deliveries_to(ingress, OWNER)
    assert len(matched) == 1 and matched[0]["argv"][matched[0]["argv"].index("--resume") + 1] == "S-c"
    assert _deliveries_to(ingress, ORCH) == [], "no event of a claimed PR may reach the orchestrator"
    ingress.calls.clear()

    # Every open claimed PR in REPO now has a recorded head branch; a default-branch run matches none.
    _ingest(ingress, "workflow_run", "d-main", _gh_payload("workflow_run", "completed", conclusion="failure",
                                                           head="d" * 40, branch="main", pr_numbers=[]))
    _ingest(ingress, "workflow_run", "d-other-repo", _gh_payload(
        "workflow_run", "completed", conclusion="failure", head="e" * 40, branch="feat", pr_numbers=[],
        repo_name="o/ing-f66-unclaimed"))
    orch = _deliveries_to(ingress, ORCH)
    assert sorted(d for d in ("d-main", "d-other-repo") for c in orch if f"github/{d}" in c["prompt"]) \
        == ["d-main", "d-other-repo"]
    assert len(orch) == 2 and _deliveries_to(ingress, OWNER) == []


@pytest.mark.parametrize("event,action,conclusion", [
    ("check_run", "completed", "failure"), ("check_suite", "completed", "failure"),
    ("workflow_run", "completed", "failure"), ("pull_request", "synchronize", None),
    ("pull_request_review", "submitted", None), ("pull_request_review_comment", "created", None),
    ("issue_comment", "created", None),
])
def test_ac_ing_f66_9(ingress, monkeypatch, event, action, conclusion):
    """A PR/review/CI event for a PR with no owner is delivered to the orchestrator's Bot Chat and is never dropped, including when the ledger lookup errors (floor e)."""
    _ingest(ingress, event, f"d-unmapped-{event}", _gh_payload(event, action, conclusion=conclusion, pr=31))
    orch = _deliveries_to(ingress, ORCH)
    assert len(orch) == 1 and orch[0]["argv"][orch[0]["argv"].index("-c") + 1] == "Bot Chat"
    assert f"d-unmapped-{event}" in orch[0]["prompt"]
    ingress.calls.clear()

    def _boom(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ingress.ledger, "resolve_owner", _boom)
    _ingest(ingress, event, f"d-locked-{event}", _gh_payload(event, action, conclusion=conclusion, pr=32))
    assert ingress.calls == []
    assert _rows(ingress, "SELECT state FROM deliveries WHERE outbox_delivery = ?",
                 (f"d-locked-{event}",)) == [("pending",)]


def test_ac_ing_f66_10(ingress, monkeypatch, capsys):
    """Remapping an owner (CLI verb and model tool) is orchestrator-only AND passes the human-approval gate before any mutation; denial, timeout or no-human context refuses with no ledger change (floor f)."""
    import hermes_cli.plugins as hp
    import tools.approval as approval

    for prof, sid in ((OWNER, "S-fix"), (OTHER, "S-rev")):
        _seed_session(ingress.root / "profiles" / prof, sid)
    _claim(ingress, monkeypatch, OWNER, "S-fix", 7)
    monkeypatch.setattr(hp, "get_plugin_manager", lambda: ingress.manager)
    asked: list[str] = []
    outcome = {"v": "deny"}

    def _gate(tool_name, reason, *, rule_key="", approval_callback=None):
        asked.append(rule_key)
        if outcome["v"] == "approve":
            return {"approved": True, "message": None}
        return {"approved": False, "message": {"deny": "denied", "timeout": "timed out",
                                               "nohuman": "no human present"}[outcome["v"]]}

    monkeypatch.setattr(approval, "request_tool_approval", _gate)
    cli = ingress.manager._cli_commands["ingress"]["handler_fn"]
    args = SimpleNamespace(ingress_command="remap", repo=REPO, pr=7, to=OTHER, session="S-rev", json=True)
    tool_args = {"repo": REPO, "pr": 7, "to": OTHER, "session": "S-rev"}

    def _tool_path():
        # Model-tool path as the agent loop runs it: the pre_tool_call gate first
        # (resolve_pre_tool_block, hermes_cli/plugins.py:6736-6767), the handler only if unblocked.
        blocked = hp.resolve_pre_tool_block("ingress_remap", tool_args, session_id="S-orch")
        if blocked:
            return {"status": "blocked", "message": blocked}
        return json.loads(registry.dispatch("ingress_remap", tool_args, session_id="S-orch"))

    _set_profile(monkeypatch, OWNER)
    assert cli(args) not in (0, None)
    assert _tool_path().get("status") in ("refused", "blocked")
    assert asked == [] and _owner_row(ingress, 7)[0] == OWNER

    _set_profile(monkeypatch, ORCH)
    for gate_result in ("deny", "timeout", "nohuman"):
        outcome["v"] = gate_result
        n = len(asked)
        assert cli(args) not in (0, None), f"CLI remap must refuse on {gate_result}"
        assert _tool_path().get("status") != "remapped", f"tool remap must refuse on {gate_result}"
        assert len(asked) == n + 2, "both entry points must consult the approval gate"
        assert _owner_row(ingress, 7)[:2] == (OWNER, "S-fix"), f"{gate_result} mutated the ledger"
    assert set(asked) == {f"ingress_remap:{REPO}#7->{OTHER}"}

    outcome["v"] = "approve"
    assert _tool_path().get("status") == "remapped"
    assert _owner_row(ingress, 7)[:2] == (OTHER, "S-rev")
    args_back = SimpleNamespace(ingress_command="remap", repo=REPO, pr=7, to=OWNER, session="S-fix", json=True)
    assert cli(args_back) in (0, None)
    assert _owner_row(ingress, 7)[:2] == (OWNER, "S-fix")
    capsys.readouterr()


# --------------------------------------------------------------------------- render helpers

def _render_home(tmp_path) -> Path:
    home = tmp_path / "render-home" / ".hermes"
    (home / "plugins").mkdir(parents=True, exist_ok=True)
    for key in FLEET_PLUGINS:
        shutil.copytree(REPO_ROOT / "plugins" / key, home / "plugins" / key, dirs_exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": list(FLEET_PLUGINS)}}),
                                      encoding="utf-8")
    (tmp_path / "render-bundled").mkdir(exist_ok=True)
    return home


def _render(tmp_path, out: Path, provision_dry_run: bool = False) -> subprocess.CompletedProcess:
    """``hermes coworker compose`` on the ING-F66 spec copy, isolated (FLEET-F62.c helper shape)."""
    home = _render_home(tmp_path)
    env = dict(os.environ, HOME=str(home.parent), HERMES_HOME=str(home),
               HERMES_BUNDLED_PLUGINS=str(tmp_path / "render-bundled"),
               HERMES_ENABLE_PROJECT_PLUGINS="0")
    proc = subprocess.run([sys.executable, "-m", "hermes_cli.main", "coworker", "compose",
                           str(ING_SPEC), "--out", str(out),
                           *(["--provision-dry-run"] if provision_dry_run else [])],
                          capture_output=True, text=True, env=env, cwd=str(REPO_ROOT))
    assert proc.returncode == 0, proc.stderr
    return proc


def _cfg(out: Path, profile: str) -> dict:
    hits = [p for p in out.glob("*/config.yaml") if p.parent.name == profile or p.parent.name.endswith(profile)]
    assert hits, f"no rendered profile dir for {profile}"
    return yaml.safe_load(hits[0].read_text(encoding="utf-8"))


def _webhook(out: Path) -> dict:
    return ((_cfg(out, "default").get("platforms") or {}).get("webhook") or {})


def _files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts]


_CURL_CREDENTIAL_OPTIONS = {"-u", "--user", "-n", "--netrc", "--netrc-file", "--netrc-optional", "-b", "--cookie",
                            "-c", "--cookie-jar", "-K", "--config", "-E", "--cert", "--key", "--oauth2-bearer",
                            "--aws-sigv4", "--negotiate", "--ntlm", "--digest", "--basic", "--anyauth",
                            "--proxy-user", "-U", "--delegation"}
_CURL_HEADER_NAMES = {"accept", "x-github-api-version", "user-agent"}
_CURL_WRITE_OPTIONS = {"-d", "--data", "--data-raw", "--data-binary", "--data-urlencode", "--json", "-F",
                       "--form", "-T", "--upload-file"}


def _fake_cli(bindir: Path, name: str, log: Path, fixtures: Path) -> None:
    """A stand-in GitHub CLI or curl: records argv (one JSON list per line) and serves fixture JSON by URL."""
    fake = bindir / name
    fake.write_text(
        "#!" + sys.executable + "\n"
        "import json, sys\n"
        f"open({str(log)!r}, 'a').write(json.dumps([{name!r}] + sys.argv[1:]) + '\\n')\n"
        f"fx = json.load(open({str(fixtures)!r}))\n"
        "args = ' '.join(sys.argv[1:])\n"
        "if 'check-runs' in args: print(json.dumps(fx['check-runs']))\n"
        "elif '/status' in args or 'statuses' in args: print(json.dumps(fx['status']))\n"
        "elif 'headRefOid' in args or '/pulls/' in args: print(json.dumps(fx['head']))\n"
        "else: sys.exit(1)\n", encoding="utf-8")
    fake.chmod(0o755)


def test_ac_ing_f66_11(ingress, tmp_path, monkeypatch):
    """The checks gate the reviewer and approver run refuses when the legacy commit status is empty and a check run at the reviewed head failed; it refuses unwaived neutral/skipped/pending/missing runs, a moved head, and an unavailable read; it passes only all-success or explicitly waived runs (floor g, E2E-17)."""
    gate_path = PLUGIN_SRC / "skills" / "ci-gate" / "scripts" / "checks_gate.py"
    gate = _load("skills/ci-gate/scripts/checks_gate.py", "nv_ingress_checks_gate_under_test")
    sha = "e" * 40

    def run(name, conclusion, status="completed"):
        return {"name": name, "status": status, "conclusion": conclusion, "head_sha": sha}

    ev = gate.evaluate
    assert ev([run("a", "success"), run("b", "success")], sha, sha, [])["pass"] is True
    for bad in ("failure", "neutral", "skipped", "cancelled", "timed_out", "action_required", "stale"):
        assert ev([run("a", "success"), run("b", bad)], sha, sha, [])["pass"] is False, bad
        assert ev([run("a", "success"), run("b", bad)], sha, sha, ["b"])["pass"] is True, bad
    assert ev([run("a", None, status="in_progress")], sha, sha, [])["pass"] is False
    assert ev([], sha, sha, [])["pass"] is False
    assert ev([run("a", "success")], sha, "f" * 40, [])["pass"] is False

    # Both read modes against fakes on PATH: legacy status EMPTY, check-runs RED.
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    fixtures = tmp_path / "fixtures.json"
    fixtures.write_text(json.dumps({
        "check-runs": {"total_count": 1, "check_runs": [run("check", "failure")]},
        "status": {"state": "pending", "statuses": [], "total_count": 0},
        "head": {"headRefOid": sha, "head": {"sha": sha}}}), encoding="utf-8")
    env = dict(os.environ, PATH=f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    for mode in ("provider", "anonymous"):
        log = tmp_path / f"argv-{mode}.log"
        for name in ("gh", "curl"):
            _fake_cli(bindir, name, log, fixtures)
        proc = subprocess.run([sys.executable, str(gate_path), REPO, "7", sha, "--read", mode],
                              capture_output=True, text=True, env=env, timeout=60)
        assert proc.returncode == 3, mode + ": " + proc.stdout + proc.stderr
        verdict = json.loads(proc.stdout.strip().splitlines()[-1])
        assert verdict["pass"] is False and "check" in json.dumps(verdict["refused"]), mode
        calls = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()]
        flat = " ".join(" ".join(c) for c in calls)
        assert "check-runs" in flat and "/status" not in flat and "statuses" not in flat, mode
        if mode == "anonymous":
            assert calls and {c[0] for c in calls} == {"curl"}, "the credential-free read never uses the GitHub CLI"
            for argv in (c[1:] for c in calls):
                assert argv[0] == "-q", "curl must ignore any config file"
                assert not _CURL_CREDENTIAL_OPTIONS & {a.split("=", 1)[0] for a in argv}, argv
                names = [argv[i + 1].split(":", 1)[0].strip().lower()
                         for i, a in enumerate(argv[:-1]) if a in ("-H", "--header")]
                assert set(names) <= _CURL_HEADER_NAMES, f"header outside the allowlist: {names}"
                assert not _CURL_WRITE_OPTIONS & {a.split("=", 1)[0] for a in argv}, argv
                verbs = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a in ("-X", "--request")]
                assert all(v.upper() == "GET" for v in verbs), verbs
    (bindir / "curl").write_text("#!" + sys.executable + "\nimport sys\nsys.exit(4)\n", encoding="utf-8")
    proc = subprocess.run([sys.executable, str(gate_path), REPO, "7", sha, "--read", "anonymous"],
                          capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 3 and "L-CHK" in proc.stdout


def test_ac_ing_f66_12(ingress, tmp_path):
    """The rendered hook-subscription artefact lists check_run, check_suite and workflow_run plus every event the bridge routes, and the edge's accepted-event list equals it (floor h, scope 3)."""
    out = tmp_path / "out"
    _render(tmp_path, out)
    events = json.loads((out / "hooks" / "github-events.json").read_text(encoding="utf-8"))
    assert CI_EVENTS <= set(events)
    assert set(events) == set(ingress.module.ROUTED_EVENTS)
    edge_cfg = yaml.safe_load((out / "host" / "edge.yaml").read_text(encoding="utf-8"))
    assert sorted(edge_cfg["events"]) == sorted(events)


LEGACY_SPECS = {
    "FLEET-F62.c": REPO_ROOT / "tests" / "e2e-scenarios" / "FLEET-F62.c" / "spec" / "openshell" / "coworker-types.yaml",
    "LOOP-F40": REPO_ROOT / "tests" / "plugins" / "fixtures" / "ch-f52" / "coworker-types.yaml",
}


_DEFAULT_ACTION = {"pull_request": "opened", "check_suite": "completed", "check_run": "completed",
                   "workflow_run": "completed", "issue_comment": "created", "pull_request_review": "submitted",
                   "pull_request_review_comment": "created", "issues": "opened"}


def _qualifying_action(route: dict, event: str) -> str:
    """An action the route's own `action in [...]` filter admits, so the request reaches its script."""
    for spec in route.get("filters") or []:
        if isinstance(spec, dict) and spec.get("field") == "action" and spec.get("in"):
            return sorted(spec["in"])[0]
    return _DEFAULT_ACTION.get(event, "opened")


def _legacy_route_parity(tmp_path, monkeypatch) -> None:
    """Guard-active companion of the FLEET-F62.c / LOOP-F40 gates (D11, D14): each legacy render's
    DEFAULT webhook routes, served by the built-in adapter and by the guard adapter from identical
    fresh homes, answer the same signed and unsigned requests alike and run the same route scripts."""
    from gateway.platform_registry import platform_registry
    from gateway.platforms.webhook import WebhookAdapter
    from gateway.platforms.webhook_filters import WebhookRouteProcessor

    guard_factory = platform_registry.get("webhook").adapter_factory
    script_runs: list[tuple] = []
    real_run_script = WebhookRouteProcessor.run_route_script
    active = {"label": None}

    def _recording_run_script(self, script, payload, *a, **k):
        result = real_run_script(self, script, payload, *a, **k)
        script_runs.append((active["label"], script, bool(result[0])))
        return result

    monkeypatch.setattr(WebhookRouteProcessor, "run_route_script", _recording_run_script)
    for row, spec in LEGACY_SPECS.items():
        out = tmp_path / f"legacy-{row}"
        home = _render_home(tmp_path / f"legacy-home-{row}")
        env = dict(os.environ, HOME=str(home.parent), HERMES_HOME=str(home),
                   HERMES_BUNDLED_PLUGINS=str(tmp_path / f"legacy-home-{row}" / "render-bundled"),
                   HERMES_ENABLE_PROJECT_PLUGINS="0")
        proc = subprocess.run([sys.executable, "-m", "hermes_cli.main", "coworker", "compose", str(spec),
                               "--out", str(out)], capture_output=True, text=True, env=env, cwd=str(REPO_ROOT))
        assert proc.returncode == 0, f"{row}: {proc.stderr}"
        default_dir = next(p.parent for p in out.glob("*/config.yaml") if p.parent.name.endswith("default"))
        extra = (((yaml.safe_load((default_dir / "config.yaml").read_text(encoding="utf-8")) or {})
                  .get("platforms") or {}).get("webhook") or {}).get("extra") or {}
        routes = extra.get("routes") or {}
        assert routes, f"{row}: legacy render has no webhook routes to replay"
        key = secrets.token_hex(32)
        # Both adapters get the same copy: run-time signing material, enabled, served on the
        # bare route path (profile binding is URL-prefix routing, identical in both classes).
        served = {name: {k: v for k, v in dict(r, secret=key, enabled=True).items() if k != "profile"}
                  for name, r in routes.items()}
        outcomes = {}
        for label, make in (("builtin", lambda e: WebhookAdapter(PlatformConfig(enabled=True, extra=e))),
                            ("guard", lambda e: guard_factory(PlatformConfig(enabled=True, extra=e)))):
            run_home = tmp_path / f"legacy-run-{row}-{label}"
            shutil.copytree(default_dir / "scripts", run_home / "scripts", dirs_exist_ok=True)
            monkeypatch.setenv("HERMES_HOME", str(run_home))
            active["label"] = (row, label)
            port = _free_port()
            adapter = make({"host": "127.0.0.1", "port": port, "routes": served})
            assert (type(adapter) is WebhookAdapter) == (label == "builtin"), label
            seen = []

            async def _record(event, _seen=seen):
                _seen.append(event.text)

            adapter.handle_message = _record
            loop = _serve_adapter(adapter)
            try:
                rows = []
                for name, r in sorted(served.items()):
                    for event in sorted(r.get("events") or ["pull_request"]):
                        raw = json.dumps(_gh_payload(event, _qualifying_action(r, event))).encode()
                        hdr = {"X-GitHub-Event": event, "X-GitHub-Delivery": f"{name}-{event}"}
                        rows.append((name, event, _post_raw(port, name, raw,
                                                            {**hdr, GH_SIG_HEADER: _gh_sign(key.encode(), raw)})))
                        rows.append((name, event, _post_raw(port, name, raw,
                                                            {**hdr, "X-GitHub-Delivery": f"{name}-{event}-u"})))
                assert _wait(lambda: len(seen) >= sum(1 for _n, _e, c in rows if c == 202), timeout=30)
                outcomes[label] = (rows, sorted(seen))
            finally:
                _stop_adapter(adapter, loop)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "root"))
        assert outcomes["guard"] == outcomes["builtin"], f"{row}: guard and built-in diverge on legacy routes"
        scripts = {r.get("script") for r in served.values() if r.get("script")}
        for label in ("builtin", "guard"):
            ran = [(name, keep) for (lbl, name, keep) in script_runs if lbl == (row, label)]
            assert {name for name, _keep in ran} == scripts, f"{row}/{label}: route scripts not executed: {ran}"
        assert sorted(x[1:] for x in script_runs if x[0] == (row, "guard")) == \
            sorted(x[1:] for x in script_runs if x[0] == (row, "builtin")), f"{row}: script outcomes diverge"
    assert {"pr_ci_gate.py", "check_gate.py"} <= {name for (_l, name, _k) in script_runs}, \
        "the LOOP-F40 route scripts must run under both adapters"


def _post_raw(port: int, route: str, body: bytes, headers: dict) -> int:
    return _post(port, f"/webhooks/{route}", body, headers)


def test_ac_ing_f66_13(ingress, tmp_path, monkeypatch, capsys):
    """The rendered in-sandbox ingress route is secret-free and loopback-only: under `ingress:` the static routes are replaced (`replace_routes` true or defaulted; `false` is refused by the render), so no route in the rendered DEFAULT config carries a platform-secret reference; the webhook host is a loopback literal and the ingress route uses the release's loopback-gated no-auth mode, which the release adapter refuses to start on a widened host; and a secret-bearing dynamic route in the DEFAULT home's subscriptions file, present at start or created after it, makes nv-ingress fail closed: the condition shows in `hermes ingress status` and every webhook route, the ingress route and the dynamic route alike, is refused from the first request after start (file present at start) or from the next request (file created later) until the file is removed (floor i; trust point 1)."""
    import asyncio

    from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH, _is_loopback_host

    out = tmp_path / "out"
    _render(tmp_path, out)
    wh = _webhook(out)
    extra = wh.get("extra") or {}
    assert wh.get("enabled") is True and _is_loopback_host(extra.get("host"))
    routes = extra.get("routes") or {}
    assert set(routes) == {ROUTE}, "under ingress: the static routes are replaced by the one ingress route"
    assert routes[ROUTE]["secret"] == _INSECURE_NO_AUTH and routes[ROUTE].get("script") == "ingress_stage.py"
    assert routes[ROUTE]["events"] == [TRANSPORT_EVENT], "the route admits only the forward's transport event type"
    assert "${env:" not in json.dumps(wh), "the DEFAULT webhook block references an env-held secret"
    default_cfg = _cfg(out, "default")
    assert (((default_cfg.get("plugins") or {}).get("entries") or {}).get(PLUGIN_KEY, {})
            .get("settings") or {}).get("webhook_guard") is True
    spec_dir = tmp_path / "spec-no-replace"
    shutil.copytree(ING_SPEC.parent, spec_dir)
    spec = yaml.safe_load((spec_dir / ING_SPEC.name).read_text(encoding="utf-8"))
    spec["ingress"]["replace_routes"] = False
    (spec_dir / ING_SPEC.name).write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    home = _render_home(tmp_path)
    env = dict(os.environ, HOME=str(home.parent), HERMES_HOME=str(home),
               HERMES_BUNDLED_PLUGINS=str(tmp_path / "render-bundled"), HERMES_ENABLE_PROJECT_PLUGINS="0")
    refused = subprocess.run([sys.executable, "-m", "hermes_cli.main", "coworker", "compose",
                              str(spec_dir / ING_SPEC.name), "--out", str(tmp_path / "out-bad")],
                             capture_output=True, text=True, env=env, cwd=str(REPO_ROOT))
    assert refused.returncode != 0 and "replace_routes" in (refused.stderr + refused.stdout)
    widened = dict(extra, host="0.0.0.0")
    with pytest.raises(ValueError):
        asyncio.run(WebhookAdapter(PlatformConfig(enabled=True, extra=widened)).connect())

    # The guard: nv-ingress's `webhook` adapter (D14) taken from the release platform registry.
    subs = ingress.root / "webhook_subscriptions.json"
    material = secrets.token_hex(24)
    dyn = {"agent-made": {"secret": material, "events": ["push"], "prompt": "x"}}
    body = json.dumps(ingress.envelope.normalize("github", "issues", _gh_headers("issues", "d-probe-env"),
                                                 _gh_payload("issues", "opened", labels=(LABEL,)))).encode()
    body = json.dumps({**json.loads(body), "event_type": TRANSPORT_EVENT}).encode()
    probe = {"X-Request-ID": "d-probe"}

    def _refused_both(port):
        return (_post_raw(port, ROUTE, body, probe), _post_raw(port, "agent-made", body, probe))

    for when in ("present-at-start", "created-after-start"):
        if when == "present-at-start":
            subs.write_text(json.dumps(dyn), encoding="utf-8")
        port = _free_port()
        adapter = _guarded({"host": "127.0.0.1", "port": port, "routes": {ROUTE: routes[ROUTE]}})
        assert isinstance(adapter, WebhookAdapter) and type(adapter) is not WebhookAdapter
        adapter.handle_message = lambda _e: asyncio.sleep(0)
        loop = _serve_adapter(adapter)
        try:
            if when == "created-after-start":
                assert _post_raw(port, ROUTE, body, {**probe, "X-Request-ID": "d-pre"}) == 202
                subs.write_text(json.dumps(dyn), encoding="utf-8")
            codes = _refused_both(port)
            assert codes == (403, 403), f"{when}: both routes must be refused as disabled, got {codes}"
            guard = _status(ingress, capsys)["webhook_guard"]
            assert guard["breach"] is True and "agent-made" in guard["routes"], when
            assert material not in json.dumps(guard), "status must name the route, never its secret"
            subs.unlink()
            assert _post_raw(port, ROUTE, body, {**probe, "X-Request-ID": f"d-after-{when}"}) == 202
            assert _status(ingress, capsys)["webhook_guard"]["breach"] is False
        finally:
            _stop_adapter(adapter, loop)

    # Parity: a non-ingress secret-bearing static route verifies signatures as the built-in does.
    key = secrets.token_bytes(32)
    signed = {"push-hook": {"secret": key.hex(), "events": ["push"], "prompt": "x"}}
    results = {}
    for label, make in (("builtin", lambda e: WebhookAdapter(PlatformConfig(enabled=True, extra=e))),
                        ("guard", _guarded)):
        port = _free_port()
        adapter = make({"host": "127.0.0.1", "port": port, "routes": signed})
        adapter.handle_message = lambda _e: asyncio.sleep(0)
        loop = _serve_adapter(adapter)
        try:
            raw = json.dumps({"ref": "refs/heads/x"}).encode()
            base = {"X-GitHub-Event": "push"}
            results[label] = (
                _post_raw(port, "push-hook", raw, {**base, "X-GitHub-Delivery": f"{label}-ok",
                                                   GH_SIG_HEADER: _gh_sign(key.hex().encode(), raw)}),
                _post_raw(port, "push-hook", raw, {**base, "X-GitHub-Delivery": f"{label}-bad",
                                                   GH_SIG_HEADER: _gh_sign(secrets.token_bytes(32), raw)}),
                _post_raw(port, "push-hook", raw, {**base, "X-GitHub-Delivery": f"{label}-none"}))
        finally:
            _stop_adapter(adapter, loop)
    assert results["guard"] == results["builtin"] and results["builtin"][0] == 202 \
        and results["builtin"][1] == results["builtin"][2] == 401
    _legacy_route_parity(tmp_path, monkeypatch)

    from gateway.platform_registry import platform_registry

    off = tmp_path / "guard-off"
    shutil.copytree(PLUGIN_SRC, off / "plugins" / PLUGIN_KEY)
    (off / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": [PLUGIN_KEY]}}), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(off))
    off_manager = PluginManager()
    off_manager.discover_and_load()
    try:
        assert off_manager._plugins[PLUGIN_KEY].enabled is True
        assert platform_registry.get("webhook") is None, "without webhook_guard the built-in adapter must stay in use"
    finally:
        off_manager.unload()


def test_ac_ing_f66_14(ingress, tmp_path, monkeypatch):
    """No model-driven tool in the gateway sandbox can reach the loopback route: the rendered fleet keeps sandbox enforcement on with the ssh backend for every profile, no profile config or env template enables any of the three private-URL switches, and the release URL guard refuses the ingress loopback URL under the rendered config; the running gateway process's own env override is checked live by AC-20 step 1 (trust point 3, derived)."""
    import tools.url_safety as url_safety

    out = tmp_path / "out"
    _render(tmp_path, out)
    profiles = [p.parent for p in out.glob("*/config.yaml") if p.parent.name != "managed"]
    assert profiles
    for pdir in profiles:
        cfg = yaml.safe_load((pdir / "config.yaml").read_text(encoding="utf-8")) or {}
        gates = (((cfg.get("plugins") or {}).get("entries") or {}).get("nv-fleet-gates") or {}).get("settings") or {}
        assert gates.get("enforce_sandbox") is True and gates.get("expected_backend") == "ssh", pdir.name
        assert (cfg.get("terminal") or {}).get("backend") == "ssh", pdir.name
        assert (cfg.get("security") or {}).get("allow_private_urls") is not True, pdir.name
        assert (cfg.get("browser") or {}).get("allow_private_urls") is not True, pdir.name
        for env_file in pdir.glob(".env*"):
            assert "ALLOW_PRIVATE_URLS" not in env_file.read_text(encoding="utf-8"), env_file
    port = int((_webhook(out).get("extra") or {})["port"])
    monkeypatch.delenv("HERMES_ALLOW_PRIVATE_URLS", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(next(p for p in profiles if p.name.endswith("default"))))
    url_safety._reset_allow_private_cache()
    assert url_safety.is_safe_url(f"http://127.0.0.1:{port}/webhooks/{ROUTE}") is False
    url_safety._reset_allow_private_cache()


def test_ac_ing_f66_15(ingress, tmp_path):
    """The rendered host artefacts carry no secret value: the edge's host config names a secret FILE path per platform and no value; the systemd units carry no `Environment=` secret; the ingress section of every rendered file passes a structural no-literal-secret scan (floor i; constraint "no secret values")."""
    # The render's real host-side input: the ING-F66 spec copy's `ingress.platforms.<p>.secret_file`
    # paths. Point a temp copy of the spec at files holding run-time material, render it, and
    # require the material bytes to be absent from every rendered byte while the path is present.
    spec_dir = tmp_path / "spec"
    shutil.copytree(ING_SPEC.parent, spec_dir)
    spec_path = spec_dir / ING_SPEC.name
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    materials = {}
    for platform in spec["ingress"]["platforms"]:
        f = tmp_path / "host-secrets" / platform
        f.parent.mkdir(exist_ok=True)
        materials[platform] = secrets.token_hex(24)
        f.write_text(materials[platform], encoding="utf-8")
        spec["ingress"]["platforms"][platform]["secret_file"] = str(f)
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    out = tmp_path / "out"
    home = _render_home(tmp_path)
    env = dict(os.environ, HOME=str(home.parent), HERMES_HOME=str(home),
               HERMES_BUNDLED_PLUGINS=str(tmp_path / "render-bundled"), HERMES_ENABLE_PROJECT_PLUGINS="0")
    proc = subprocess.run([sys.executable, "-m", "hermes_cli.main", "coworker", "compose", str(spec_path),
                           "--out", str(out)], capture_output=True, text=True, env=env, cwd=str(REPO_ROOT))
    assert proc.returncode == 0, proc.stderr
    rendered = _files(out)
    assert rendered
    for p in rendered:
        data = p.read_bytes()
        for platform, material in materials.items():
            assert material.encode() not in data, f"{p}: {platform} secret material leaked into the render"
    edge_cfg = yaml.safe_load((out / "host" / "edge.yaml").read_text(encoding="utf-8"))
    assert set(edge_cfg["platforms"]) == set(materials)
    for name, entry in edge_cfg["platforms"].items():
        assert set(entry) == {"secret_file"}, f"{name}: only a secret FILE path may be named"
        assert entry["secret_file"] == spec["ingress"]["platforms"][name]["secret_file"]
    for unit in (out / "host").glob("*.service"):
        lines = unit.read_text(encoding="utf-8").splitlines()
        env_lines = [ln for ln in lines if ln.strip().startswith(("Environment=", "EnvironmentFile="))]
        assert all(ln.strip().startswith("Environment=PYTHONPATH=") for ln in env_lines), \
            f"{unit.name}: the only env a unit may carry is the module search path"


def test_ac_ing_f66_16(ingress, monkeypatch):
    """A staged delivery survives a gateway restart: a row staged before a simulated crash (no delivery attempted) is delivered by the drain on the next plugin load with no new inbound; a row whose target turn already committed (marker present in a compacted ancestor of the session) is marked done without a second turn; an edge spool entry not acknowledged is re-forwarded after the edge restarts (floor j, hermetic half)."""
    owner_home = ingress.root / "profiles" / OWNER
    _seed_session(owner_home, "S-root", messages=["ingress-delivery: github/d-done/7"], compacted=True)
    _seed_session(owner_home, "S-tip", parent="S-root")
    _claim(ingress, monkeypatch, OWNER, "S-root", 7)
    for d in ("d-crash", "d-done"):
        out = _stage(ingress, "check_run", d, _gh_payload("check_run", "completed", conclusion="failure"))
        assert out["stage"] == "new"
    assert ingress.calls == []

    # deliver.run_cli's only subprocess call is the CLI turn, so turns are captured here.
    from gateway import status as gateway_status

    started: list[str] = []
    after: list[dict] = []
    release = threading.Event()
    real_run = subprocess.run

    def _capture(argv, *a, **k):
        if isinstance(argv, (list, tuple)) and "chat" in argv and "--query-file" in argv:
            prompt = Path(argv[list(argv).index("--query-file") + 1]).read_text(encoding="utf-8")
            started.append(prompt)
            release.wait(60)
            after.append({"argv": list(argv), "prompt": prompt})
            return subprocess.CompletedProcess(argv, 0, "", "")
        return real_run(argv, *a, **k)

    def _fresh_load() -> PluginManager:
        for mod in [m for m in sys.modules if m.startswith(ingress.module.__name__)]:
            monkeypatch.delitem(sys.modules, mod, raising=False)
        mgr = PluginManager()
        loads.append(mgr)
        mgr.discover_and_load()
        return mgr

    ingress.manager.unload()
    monkeypatch.setattr(subprocess, "run", _capture)
    loads: list[PluginManager] = []
    try:
        # A DEFAULT-home process that is not the gateway (the CLI verb, the dashboard) loads the
        # plugin too; its driver must never run a turn. Counting the driver's lock checks makes
        # "no turn" a claim about three skipped passes, not about a quiet runner.
        lock_checks: list[bool] = []
        real_owns = gateway_status.owns_gateway_runtime_lock
        monkeypatch.setattr(gateway_status, "owns_gateway_runtime_lock",
                            lambda: lock_checks.append(real_owns()) or lock_checks[-1])
        no_lock = _fresh_load()
        assert _wait(lambda: len(lock_checks) >= 3, timeout=30), "the driver never consulted the runtime lock"
        assert not any(lock_checks) and started == [], "a load without the gateway runtime lock ran a delivery"
        no_lock.unload()
        assert gateway_status.acquire_gateway_runtime_lock()
        manager = _fresh_load()
        # A captured turn cannot finish before `release`, so a delivery run inside register()
        # would have held the load until the 60 s bound and be recorded in `after` by now.
        assert after == [], "plugin load ran a delivery inline instead of on the drain driver"
        assert manager._plugins[PLUGIN_KEY].enabled is True
        assert _wait(lambda: [p for p in started if "github/d-crash/" in p], timeout=15), \
            "the pre-crash row was not picked up by the plugin's own drain driver"

        # Reload while that turn is still in flight: the old pass keeps its lease past the
        # unload join, so every lease attempt of the new load must fail until the turn ends.
        manager.unload()
        manager = _fresh_load()
        new_ledger = sys.modules[manager._plugins[PLUGIN_KEY].module.__name__ + ".ledger"]
        real_acquire, attempts = new_ledger.acquire_lease, []

        def _record(*a, **k):
            got = real_acquire(*a, **k)
            attempts.append(got)
            return got

        monkeypatch.setattr(new_ledger, "acquire_lease", _record)
        assert _wait(lambda: attempts, timeout=15), "the reloaded driver never tried to drain"
        assert not any(attempts), "the reloaded load took the lease of a pass still in flight"
        release.set()
        out = _stage(ingress, "check_run", "d-after", _gh_payload("check_run", "completed", conclusion="failure"))
        assert out["stage"] == "new"
        assert _wait(lambda: [c for c in after if "github/d-after/" in c["prompt"]], timeout=15), \
            "the reloaded driver never ran a pass"
        assert [p for p in started if "github/d-crash/" in p] == [started[0]], "one turn per marker"
        assert not [p for p in started if "github/d-done/" in p], "a committed turn was delivered twice"
        assert _rows(ingress, "SELECT state FROM deliveries WHERE outbox_delivery = 'd-done'") == [("done",)]
        gateway_status.release_gateway_runtime_lock()

        # The fresh load stays registered (its `webhook` guard platform serves the route), and
        # chat turns stay captured, so no real CLI turn can run from here on.
        route_port = _free_port()
        spool = ingress.tmp / "edge-spool-restart"
        edge = _start_edge(ingress, route_port, spool_dir=spool)  # nothing listens on route_port yet
        assert _send_github(edge, "issues", "d-spool",
                            _gh_payload("issues", "opened", labels=(LABEL,), issue=55)) == 202
        assert _wait(lambda: list(spool.glob("*.json")))
        _stop_edge(edge)
        route = _start_route(ingress, route_port, _rendered_ingress(ingress.tmp)[1])
        edge2 = _start_edge(ingress, route_port, spool_dir=spool, secrets_by_platform=edge.secrets)
        assert _wait(lambda: not list(spool.glob("*.json")), timeout=90), "the spool was never re-forwarded"
        assert _rows(ingress, "SELECT delivery_id FROM outbox WHERE delivery_id = 'd-spool'") == [("d-spool",)]
        _stop_edge(edge2)
        _stop_adapter(route.adapter, route.loop)
    finally:
        release.set()
        gateway_status.release_gateway_runtime_lock()
        for mgr in loads:
            mgr.unload()
        monkeypatch.setattr(subprocess, "run", real_run)


def _unit(path: Path) -> dict:
    section, data = None, {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif "=" in line and section:
            k, v = line.split("=", 1)
            data.setdefault(section, {}).setdefault(k.strip(), []).append(v.strip())
    return data


def test_ac_ing_f66_17(ingress, tmp_path):
    """The rendered host units restart themselves: the edge, forward and gateway-boot units are rendered with an always-restart policy and a default-target install, the edge and gateway-boot units run the plugin's host modules, the forward unit binds the host loopback address and targets the gateway sandbox's loopback ingress port, and the gateway-boot decision starts the gateway only when the in-sandbox health check fails and no start is in flight (floor j + trust point 2, hermetic half)."""
    out = tmp_path / "out"
    _render(tmp_path, out)
    port = str((_webhook(out).get("extra") or {})["port"])
    units = {p.name: _unit(p) for p in (out / "host").glob("*.service")}
    assert {"nv-ingress-edge.service", "nv-ingress-forward.service", "nv-ingress-gateway-boot.service"} <= set(units)
    for name, module in (("nv-ingress-edge.service", "edge"), ("nv-ingress-gateway-boot.service", "edge.gateway_boot")):
        exec_argv = " ".join(units[name]["Service"]["ExecStart"]).split()
        assert exec_argv[1:3] == ["-m", module], f"{name}: must run the plugin host module {module}"
        assert "--config" in exec_argv
        env = " ".join(units[name]["Service"].get("Environment", []))
        assert env.startswith("PYTHONPATH=") and env.rstrip("/").endswith("plugins/nv-ingress")
    for name, u in units.items():
        assert u["Service"]["Restart"] == ["always"], name
        assert "default.target" in u["Install"]["WantedBy"], name
    argv = " ".join(units["nv-ingress-forward.service"]["Service"]["ExecStart"]).split()
    assert argv[1:3] == ["forward", "service"], "the transport is OpenShell's forward service"
    flags = {argv[i]: argv[i + 1] for i in range(len(argv) - 1) if argv[i].startswith("--")}
    assert flags.get("--target-host") == "127.0.0.1" and flags.get("--target-port") == port
    assert flags.get("--local") == f"127.0.0.1:{port}", "the host side must bind loopback only"
    assert not {"-d", "--background"} & set(argv), "the forward must run in the foreground under systemd"
    boot = _load("edge/gateway_boot.py", "nv_ingress_gateway_boot_under_test")
    assert boot.decide(gateway_healthy=True, start_in_flight=False) != "start", \
        "forward down but gateway healthy (in-sandbox check) must cause zero start calls"
    assert boot.decide(gateway_healthy=False, start_in_flight=False) == "start"
    assert boot.decide(gateway_healthy=False, start_in_flight=True) != "start", "starts are serialized"


def test_ac_ing_f66_18(ingress):
    """The docs page documents the Slack route as NemoClaw's channel path (outbound socket, no inbound endpoint, not built), the polling backstop, the envelope v1 contract with its GitLab mapping, the trust model and the operator lane steps (floor k)."""
    text = DOCS_PAGE.read_text(encoding="utf-8")
    sections = {}
    current = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = ""
        elif current:
            sections[current] += line + "\n"
    for heading in ("Inbound path", "Envelope v1", "GitLab mapping", "Trust model", "Operator lane",
                    "Slack route", "Polling backstop", "Restart behaviour"):
        assert heading in sections, f"docs page lacks ## {heading}"
    for key in ingress.envelope.FIELDS:
        assert f"`{key}`" in sections["Envelope v1"], f"envelope field {key} undocumented"
    gitlab_mapping = sections["GitLab mapping"].lower()
    assert all(term in gitlab_mapping for term in ("19.0+", "signing token", "configured")), \
        "the GitLab mapping must state the 19.0+ signing-token prerequisite"
    slack = sections["Slack route"].lower()
    assert "outbound" in slack and "inbound" in slack and "not built" in slack
    for lane in ("L-HOOK", "L-EDGE", "L-FWD", "L-VAL", "L-RST-SB", "L-RST-SVC", "L-CHK"):
        assert lane in sections["Operator lane"], lane


def test_ac_ing_f66_22(ingress, tmp_path):
    """The rendered reviewer and approver profiles each carry the `ci-gate` skill with its script; the reviewer's rendered `plan` workflow runs the gate in provider-read mode and the approver's rendered `approve` workflow runs it in credential-free mode, each before any approve verdict; the approver attaches no provider and carries no credential reference, and under `ingress:` its rendered policy adds credential-free GET-only egress to the GitHub API host for the worker image's curl (floor g, binding half; E2E-9 for the ING-F66 spec copy)."""
    out = tmp_path / "out"
    proc = _render(tmp_path, out, provision_dry_run=True)

    def pdir(role):
        return next(p.parent for p in out.glob("*/config.yaml") if p.parent.name.endswith(role))

    for role, workflow, mode in (("reviewer", "plan", "provider"), ("approver", "approve", "anonymous")):
        d = pdir(role)
        assert (d / "skills" / "ci-gate" / "SKILL.md").is_file(), role
        assert (d / "skills" / "ci-gate" / "scripts" / "checks_gate.py").is_file(), role
        body = (d / "skills" / workflow / "SKILL.md").read_text(encoding="utf-8")
        gate_at = body.find("ci-gate")
        assert gate_at != -1, f"{role}: {workflow} workflow has no checks-gate step"
        assert f"--read {mode}" in body[gate_at:], f"{role}: the gate step must use the {mode} read"
        assert "approve" in body[gate_at:].lower(), f"{role}: the gate must precede the approve verdict"
    attach = [ln for ln in proc.stdout.splitlines() if "provider attach" in ln and "approver" in ln]
    assert attach == [], "the approver attaches no provider (E2E-9: no credentials)"
    approver_cfg = (pdir("approver") / "config.yaml").read_text(encoding="utf-8")
    assert "${env:" not in approver_cfg, "the approver config must carry no credential reference"
    policy = yaml.safe_load(next(pdir("approver").glob("policy-*.yaml")).read_text(encoding="utf-8"))
    github, binaries = [], []

    def _walk(node):
        if isinstance(node, dict):
            if str(node.get("host", "")).endswith("github.com"):
                github.append(node)
            if "binaries" in node:
                binaries.extend(str((b or {}).get("path", "")) for b in node.get("binaries") or [])
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(policy)
    assert [(e["host"], int(e["port"])) for e in github] == [("api.github.com", 443)], \
        "the approver's only GitHub egress is the API host"
    methods = {str(((r or {}).get("allow") or {}).get("method", "")).upper() for r in github[0].get("rules") or []}
    assert methods == {"GET"}, f"approver must be read-only toward GitHub, got {sorted(methods)}"
    assert "/usr/bin/curl" in binaries and not any(b.endswith("/gh") for b in binaries), binaries


def test_ac_ing_f66_23(ingress, monkeypatch, capsys, caplog):
    """Owned events authored by the fleet's own GitHub identity or a Bot sender are still delivered to the owner, marked as self-authored in the prompt; when one PR exceeds the per-PR hourly delivery budget every event is still delivered to the owner, the over-budget condition is listed in `hermes ingress status` and logged once per PR and hour by the plugin logger, and the orchestrator receives nothing for it (derived — loop visibility without narrowing owner routing; addendum 2)."""
    import logging

    _seed_session(ingress.root / "profiles" / OWNER, "S-fix")
    _claim(ingress, monkeypatch, OWNER, "S-fix", 7)
    out = _ingest(ingress, "issue_comment", "d-self", _gh_payload("issue_comment", "created", sender="fleet-bot"))
    assert out["self_authored"] is True
    _ingest(ingress, "pull_request_review", "d-bot", _gh_payload(
        "pull_request_review", "submitted", sender="x[bot]", sender_type="Bot"))
    owner = _deliveries_to(ingress, OWNER)
    assert len(owner) == 2 and all("self-authored" in c["prompt"].lower() for c in owner)
    ingress.calls.clear()
    with caplog.at_level(logging.WARNING):
        for i in range(5):  # per_pr_hourly_budget = 3 in the fixture; 2 already spent this hour
            _ingest(ingress, "check_run", f"d-ci-{i}", _gh_payload("check_run", "completed", conclusion="failure"))
    assert len(_deliveries_to(ingress, OWNER)) == 5, "a budget must never hold back owner delivery"
    assert _deliveries_to(ingress, ORCH) == [], "the orchestrator only receives events for PRs with no owner"
    runaway = _status(ingress, capsys)["runaway"]
    assert [(r["repo"], r["pr"]) for r in runaway] == [(REPO, 7)]
    warned = [r for r in caplog.records if r.levelno >= logging.WARNING and "runaway" in r.getMessage().lower()
              and f"{REPO}#7" in r.getMessage()]
    assert len(warned) == 1, "the budget breach is logged once per PR and hour"



# (event, action, conclusion, the payload field carrying third-party free text, the object holding the PR title)
_S2_CASES = [
    ("pull_request", "synchronize", None, ("pull_request", "title"), "pull_request"),
    ("issue_comment", "created", None, ("comment", "body"), "issue"),
    ("pull_request_review", "submitted", None, ("review", "body"), "pull_request"),
    ("pull_request_review_comment", "created", None, ("comment", "body"), "pull_request"),
    ("check_run", "completed", "failure", ("check_run", "name"), None),
    ("check_suite", "completed", "failure", ("check_suite", "name"), None),
    ("workflow_run", "completed", "failure", ("workflow_run", "name"), None),
]


def _gl_s2(kind: str, pr: int):
    probe = "s2gl" + secrets.token_hex(8)
    base = {"project": {"path_with_namespace": REPO}, "user": {"username": "octo"}}
    if kind == "Issue Hook":
        return {**base, "object_kind": "issue", "labels": [{"title": LABEL}],
                "object_attributes": {"iid": 70 + pr, "action": "open", "title": probe}}, probe
    return {**base, "object_kind": "pipeline", "merge_request": {"iid": pr},
            "object_attributes": {"status": "failed", "sha": "b" * 40, "name": probe, "id": 900 + pr}}, probe


def _s2_payload(event, action, conclusion, field, title_obj, pr):
    probes = {field: "s2probe" + secrets.token_hex(8)}
    if title_obj:
        probes.setdefault((title_obj, "title"), "s2title" + secrets.token_hex(8))
    payload = _gh_payload(event, action, conclusion=conclusion, pr=pr)
    for (obj, key), value in probes.items():
        payload[obj][key] = value
    return payload, list(probes.values())


def test_ac_ing_f66_25(ingress, monkeypatch):
    """A delivery bound for the orchestrator's Bot Chat carries no third-party free text: for an unowned PR update, PR comment, review, review comment and CI event, and for a labelled issue, the prompt names the repo and carries the delivery marker but none of the comment or review body, the issue or PR title or the check name; the owner's delivery of the same PR, comment, review and CI events for a claimed PR still carries the title, the body text and the check name (derived — review S2, trust point 5)."""
    for i, (event, action, conclusion, field, title_obj) in enumerate(_S2_CASES):
        payload, probes = _s2_payload(event, action, conclusion, field, title_obj, 41 + i)
        ingress.calls.clear()
        _ingest(ingress, event, f"d-s2-unowned-{i}", payload)
        orch = _deliveries_to(ingress, ORCH)
        assert len(orch) == 1 and f"github/d-s2-unowned-{i}/{41 + i}" in orch[0]["prompt"]
        assert REPO in orch[0]["prompt"]
        assert not [p for p in probes if p in orch[0]["prompt"]], \
            f"{event}: third-party text reached the orchestrator's prompt"

    title = "s2title" + secrets.token_hex(8)
    issue = _gh_payload("issues", "opened", labels=(LABEL,), issue=61)
    issue["issue"]["title"] = title
    ingress.calls.clear()
    _ingest(ingress, "issues", "d-s2-issue", issue)
    orch = _deliveries_to(ingress, ORCH)
    assert len(orch) == 1 and "github/d-s2-issue" in orch[0]["prompt"]
    assert title not in orch[0]["prompt"], "an issue title reached the orchestrator's prompt"

    for i, kind in enumerate(("Issue Hook", "Pipeline Hook")):
        payload, probe = _gl_s2(kind, 51 + i)
        ingress.calls.clear()
        _ingest(ingress, kind, f"gl-s2-unowned-{i}", payload, source="gitlab")
        orch = _deliveries_to(ingress, ORCH)
        assert len(orch) == 1 and f"gitlab/gl-s2-unowned-{i}" in orch[0]["prompt"]
        assert probe not in orch[0]["prompt"], f"GitLab {kind}: third-party text reached the orchestrator's prompt"

    _seed_session(ingress.root / "profiles" / OWNER, "S-s2")
    _claim(ingress, monkeypatch, OWNER, "S-s2", 7)
    for i, (event, action, conclusion, field, title_obj) in enumerate(_S2_CASES):
        payload, probes = _s2_payload(event, action, conclusion, field, title_obj, 7)
        ingress.calls.clear()
        _ingest(ingress, event, f"d-s2-owned-{i}", payload)
        owner = _deliveries_to(ingress, OWNER)
        assert len(owner) == 1 and all(p in owner[0]["prompt"] for p in probes), \
            f"{event}: the owner must still see the title, body and check name"
        assert _deliveries_to(ingress, ORCH) == []
    payload, probe = _gl_s2("Pipeline Hook", 7)
    ingress.calls.clear()
    _ingest(ingress, "Pipeline Hook", "gl-s2-owned", payload, source="gitlab")
    owner = _deliveries_to(ingress, OWNER)
    assert len(owner) == 1 and probe in owner[0]["prompt"], "the owner must still see the GitLab job name"


def _scope_payload(event: str, delivery_kind: str, *, pr: int, branch: str, head: str, labels=(), nums=None):
    if event == "issues":
        return _gh_payload("issues", "opened", labels=labels, issue=pr)
    if event == "issue_comment":
        payload = _gh_payload("issue_comment", "created", pr=pr)
        if delivery_kind == "plain-issue":
            payload["issue"].pop("pull_request")
        payload["issue"]["labels"] = [{"name": n} for n in labels]
        return payload
    return _gh_payload(event, "completed" if event in CI_EVENTS else "opened", pr=pr, head=head, branch=branch,
                       conclusion="failure" if event in CI_EVENTS else None, pr_numbers=nums)


def _get_json(port: int, path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as resp:
        return json.loads(resp.read())


def _post_status(port: int, path: str, body: bytes, headers: dict) -> tuple[int, dict]:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {}


def _send_scoped(edge, event: str, delivery: str, payload: dict) -> tuple[int, dict]:
    body = json.dumps(payload).encode()
    return _post_status(edge.port, "/github", body, {**_gh_headers(event, delivery),
                                                     GH_SIG_HEADER: _gh_sign(edge.secrets["github"], body)})


def test_ac_ing_f66_26(ingress, edge_path, tmp_path, caplog):
    """With an `ingress.scope` block (repo, ref prefix, label prefix) the edge forwards only in-scope events: a PR of another repo, an unrelated PR, an unrelated comment, an unrelated CI event, a numberless CI event of an unregistered head and an issue without a prefix label are each answered 202 `out_of_scope`, counted per event type and logged with their delivery id, and are never spooled, forwarded or delivered (zero orchestrator deliveries); a scoped PR, a comment on it, a comment on a prefix-labelled issue, a numberless CI event of its head, a numberless CI event on a prefix branch and a prefix-labelled issue are forwarded; a mixed CI event on a non-prefix head is forwarded with only the registered PR numbers; a CI event on a prefix head branch naming a not-yet-registered PR whose own head ref has the prefix in the scoped repo is forwarded with that number and registers it; a PR associated only by its base branch is not admitted; the PR registration and the counts survive an edge restart; the rendered host config carries the spec's scope block; with no `scope` block every event is forwarded as before (derived — operator msg 356 item 1)."""
    import logging

    out = tmp_path / "scope-render"
    _render(tmp_path, out)
    spec = yaml.safe_load(ING_SPEC.read_text(encoding="utf-8"))
    rendered = yaml.safe_load((out / "host" / "edge.yaml").read_text(encoding="utf-8"))
    assert rendered["scope"] == spec["ingress"]["scope"] == {
        "repo": "slang-coworkers/nanoclaw", "ref_prefix": "ing-f66-", "label_prefix": "ing-f66-"}
    assert rendered["gateway"]["port"] == spec["ingress"]["port"] == 18644

    def _ci_with_prs(event: str, *, branch: str, head: str, prs) -> dict:
        """A CI payload whose `pull_requests[]` entries carry their own head/base refs and repos, as GitHub sends them."""
        payload = _gh_payload(event, "completed", head=head, branch=branch, conclusion="failure", pr_numbers=[])
        payload[event]["pull_requests"] = [
            {"number": n, "head": {"ref": h_ref, "sha": head, "repo": {"id": 1, "name": h_repo.split("/")[-1],
                                                                       "url": f"https://api.github.com/repos/{h_repo}"}},
             "base": {"ref": b_ref, "sha": "0" * 40, "repo": {"id": 1, "name": REPO.split("/")[-1],
                                                              "url": f"https://api.github.com/repos/{REPO}"}}}
            for n, h_ref, h_repo, b_ref in prs]
        return payload

    scope = {"repo": REPO, "ref_prefix": "ing-f66-", "label_prefix": "ing-f66-"}
    spool = tmp_path / "scope-state" / "spool"
    edge = _start_edge(ingress, edge_path.relay.port, spool_dir=spool, scope=scope)
    in_head, out_head = "c" * 40, "d" * 40
    out_cases = [
        ("pull_request", "s-out-pr", _scope_payload("pull_request", "pr", pr=90, branch="feature-x", head=out_head)),
        ("issue_comment", "s-out-comment", _scope_payload("issue_comment", "pr", pr=90, branch="", head="")),
        ("check_run", "s-out-ci", _scope_payload("check_run", "ci", pr=90, branch="feature-x", head=out_head)),
        ("check_suite", "s-out-ci-nonum", _scope_payload("check_suite", "ci", pr=0, branch="feature-x",
                                                          head=out_head, nums=[])),
        ("issues", "s-out-issue", _scope_payload("issues", "issue", pr=91, branch="", head="", labels=(LABEL,))),
    ]
    foreign = _scope_payload("pull_request", "pr", pr=93, branch="ing-f66-scratch-1-fix", head="e" * 40)
    foreign["repository"]["full_name"] = "other/repo"
    out_cases.append(("pull_request", "s-out-foreign-repo", foreign))
    with caplog.at_level(logging.INFO):
        for event, delivery, payload in out_cases:
            code, reply = _send_scoped(edge, event, delivery, payload)
            assert (code, reply.get("status")) == (202, "out_of_scope"), f"{delivery}: {code} {reply}"
            assert not list(spool.glob(f"*{delivery}*")), f"{delivery} was spooled"
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert all(d in logged for _e, d, _p in out_cases), "every out-of-scope delivery id is logged"
    counts = _get_json(edge.port, "/scope")["counts"]
    assert counts == {"pull_request": 2, "issue_comment": 1, "check_run": 1, "check_suite": 1, "issues": 1}

    in_cases = [
        ("pull_request", "s-in-pr", _scope_payload("pull_request", "pr", pr=7, branch="ing-f66-scratch-1-fix",
                                                   head=in_head)),
        ("issue_comment", "s-in-comment", _scope_payload("issue_comment", "pr", pr=7, branch="", head="")),
        ("issue_comment", "s-in-labelled", _scope_payload("issue_comment", "plain-issue", pr=95, branch="", head="",
                                                          labels=("ing-f66-fleet",))),
        ("check_suite", "s-in-ci-nonum", _scope_payload("check_suite", "ci", pr=0, branch="other", head=in_head,
                                                        nums=[])),
        ("check_run", "s-in-ci-mixed", _scope_payload("check_run", "ci", pr=7, branch="feature-x",
                                                      head=out_head, nums=[7, 90])),
        ("check_run", "s-in-ci-preregister", _ci_with_prs(
            "check_run", branch="ing-f66-scratch-2-fix", head="f" * 40,
            prs=[(96, "ing-f66-scratch-2-fix", REPO, "ing-f66-scratch-2-base"),
                 (97, "feature-z", REPO, "ing-f66-scratch-2-base"),
                 (98, "ing-f66-scratch-2-fix", "other/repo", "ing-f66-scratch-2-base")])),
        ("issues", "s-in-issue", _scope_payload("issues", "issue", pr=92, branch="", head="",
                                                labels=("ing-f66-fleet",))),
        ("workflow_run", "s-in-ci-prefix", _scope_payload("workflow_run", "ci", pr=0, branch="ing-f66-other",
                                                          head="e" * 40, nums=[])),
    ]
    for event, delivery, payload in in_cases:
        code, reply = _send_scoped(edge, event, delivery, payload)
        assert (code, reply.get("status")) == (202, "accepted"), f"{delivery}: {code} {reply}"
    assert _wait(lambda: len(edge_path.route.hooked) >= len(in_cases)), "in-scope events were not forwarded"
    assert _wait(lambda: not list(spool.glob("*.json")))
    ingress.module.drain_once()
    staged = {r[0]: r for r in _rows(ingress, "SELECT delivery_id, envelope FROM outbox")}
    assert set(staged) == {d for _e, d, _p in in_cases}, "only in-scope events reach the ledger"
    assert json.loads(staged["s-in-ci-mixed"][1])["pr_numbers"] == [7], "a mixed CI event keeps registered PRs only"
    assert json.loads(staged["s-in-ci-preregister"][1])["pr_numbers"] == [96], \
        "a prefix-head CI event registers its own not-yet-registered prefix PR"
    code, reply = _send_scoped(edge, "check_suite", "s-in-after-preregister",
                               _scope_payload("check_suite", "ci", pr=96, branch="feature-y", head="9" * 40, nums=[96]))
    assert (code, reply.get("status")) == (202, "accepted"), "a later event naming the registered PR is in scope"
    code, reply = _send_scoped(edge, "check_run", "s-ci-base-only", _ci_with_prs(
        "check_run", branch="feature-z", head="8" * 40,
        prs=[(97, "feature-z", REPO, "ing-f66-scratch-3-base")]))
    assert (code, reply.get("status")) == (202, "out_of_scope"), "a PR associated only by its base branch is not admitted"
    code, reply = _send_scoped(edge, "check_suite", "s-ci-base-only-later",
                               _scope_payload("check_suite", "ci", pr=97, branch="feature-z", head="8" * 40, nums=[97]))
    assert (code, reply.get("status")) == (202, "out_of_scope"), "a base-only PR must not have been registered"
    for n, pr_head, pr_repo in ((97, "feature-z", REPO), (98, "ing-f66-scratch-2-fix", "other/repo")):
        code, reply = _send_scoped(edge, "check_run", f"s-ci-not-admitted-{n}", _ci_with_prs(
            "check_run", branch="ing-f66-scratch-4-fix", head="7" * 40,
            prs=[(n, pr_head, pr_repo, "ing-f66-scratch-4-base")]))
        assert (code, reply.get("status")) == (202, "out_of_scope"), f"PR {n} must be neither admitted nor registered"
    assert _wait(lambda: not list(spool.glob("*.json")))
    assert _wait(lambda: len(edge_path.route.hooked) >= len(in_cases) + 1)
    assert len(edge_path.route.hooked) == len(in_cases) + 1, "only s-in-after-preregister was forwarded after the in-scope set"
    counts = _get_json(edge.port, "/scope")["counts"]
    to_orch = _deliveries_to(ingress, ORCH)
    assert not [c for c in to_orch for _e, d, _p in out_cases if f"github/{d}" in c["prompt"]]

    _stop_edge(edge)
    edge = _start_edge(ingress, edge_path.relay.port, spool_dir=spool, scope=scope,
                       secrets_by_platform=edge.secrets)
    assert _get_json(edge.port, "/scope")["counts"] == counts, "the out-of-scope counts survive an edge restart"
    code, reply = _send_scoped(edge, "check_run", "s-in-after-restart",
                               _scope_payload("check_run", "ci", pr=7, branch="other", head=in_head, nums=[7]))
    assert (code, reply.get("status")) == (202, "accepted"), "the PR registration must survive an edge restart"
    assert _wait(lambda: not list(spool.glob("*.json"))), "the after-restart event was never forwarded"
    _stop_edge(edge)

    open_edge = _start_edge(ingress, edge_path.relay.port, spool_dir=tmp_path / "no-scope" / "spool")
    before = len(edge_path.route.hooked)
    try:
        for event, delivery, payload in out_cases:
            code, reply = _send_scoped(open_edge, event, delivery + "-open", payload)
            assert (code, reply.get("status")) == (202, "accepted"), f"no scope must forward {delivery}"
        assert _wait(lambda: len(edge_path.route.hooked) >= before + len(out_cases)), \
            "without a scope block every listed event is forwarded"
    finally:
        _stop_edge(open_edge)
