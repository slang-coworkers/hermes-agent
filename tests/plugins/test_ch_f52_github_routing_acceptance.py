"""Behavior-contract acceptance tests for CH-F52 — GitHub webhook receiver and issue/PR routing.

The canonical GitHub ingress route set is authored into the DEFAULT (multiplexer) profile's
default_config and rendered by nv-coworker-compose into platforms.webhook.extra.routes; the built-in
WebhookAdapter routes each signed delivery to its bound profile. This suite proves (a) the rendered
route contract against the ADR-pinned CANONICAL_ROUTES and (b) end-to-end routing by feeding the
RENDERED routes into the core adapter, so a config key that no reader consumes cannot pass. No new
plugin/render code exists; the canonical route fixture is the CONFIGURE deliverable under test. It is
a conformance test for that new fixture on already-capable core (the runbook is reviewed separately).
"""

import asyncio
import hashlib
import hmac
import json
import logging
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter
from hermes_cli.plugins import PluginManager

PLUGIN_KEY = "nv-coworker-compose"
PLUGIN_SRC = Path(__file__).resolve().parents[2] / "plugins" / PLUGIN_KEY
FIXTURE_SPEC = Path(__file__).parent / "fixtures" / "ch-f52" / "coworker-types.yaml"

DEFAULT_PROFILE = "default"
WEBHOOK_ROUTES_PATH = "platforms.webhook.extra.routes"
SERVED = ["default", "orchestrator", "reviewer", "fixer"]
MENTION_TOKEN = "@slang-orchestrator"

# The ADR-pinned canonical O1 route contract (ch-f52.md ## Design). This constant is the source of
# truth: the rendered config is asserted against it, so a consistently-wrong fixture cannot pass. `sentinel` is a per-route-unique repository.full_name that the behaviour tests
# post and then require in the dispatched event text (proves the route prompt templated the payload).
CANONICAL_ROUTES = {
    "gh-issues": {"profile": "orchestrator", "events": ["issues"],
                  "action_admits": ["opened", "reopened"], "sentinel": "slang-coworkers/repo-issues"},
    "gh-issue-comment": {"profile": "orchestrator", "events": ["issue_comment"],
                         "action_admits": ["created"], "sentinel": "slang-coworkers/repo-comment"},
    "gh-pull-request": {"profile": "reviewer", "events": ["pull_request"],
                        "action_admits": ["opened", "reopened", "synchronize", "ready_for_review"],
                        "sentinel": "slang-coworkers/repo-pr"},
    "gh-pr-review": {"profile": "fixer", "events": ["pull_request_review"],
                     "action_admits": ["submitted"], "sentinel": "slang-coworkers/repo-review"},
    "gh-check-suite": {"profile": "fixer", "events": ["check_suite"],
                       "action_admits": ["completed"], "sentinel": "slang-coworkers/repo-check"},
}


# ── Render helpers ──
def _dig(mapping, dotted):
    node = mapping
    for key in dotted.split("."):
        if not isinstance(node, dict) or key not in node:
            return (False, None)
        node = node[key]
    return (True, node)


def _write_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-root" / "hermes-home"
    plugins_dir = home / "plugins"
    plugins_dir.mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, plugins_dir / PLUGIN_KEY)
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": [PLUGIN_KEY]}}), encoding="utf-8"
    )
    empty_bundled = tmp_path / "bundled-plugins"
    empty_bundled.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(empty_bundled))
    return home


def _load(tmp_path, monkeypatch):
    _write_home(tmp_path, monkeypatch)
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled is True
    assert loaded.error is None
    assert loaded.module is not None
    return loaded


def _config(profile_dir):
    return yaml.safe_load((Path(profile_dir) / "config.yaml").read_text(encoding="utf-8"))


def _rendered_default_routes(tmp_path, monkeypatch):
    """Render the fixture; return (rendered_profiles, default_route_mapping)."""
    loaded = _load(tmp_path, monkeypatch)
    rendered = loaded.module.compose(str(FIXTURE_SPEC), str(tmp_path / "out"))
    cfg = _config(rendered[DEFAULT_PROFILE])
    webhook = _dig(cfg, "platforms.webhook")[1]
    # An undeployable rendered config (webhook disabled) must not pass as routed ingress.
    assert isinstance(webhook, dict) and webhook.get("enabled") is True
    found, routes = _dig(cfg, WEBHOOK_ROUTES_PATH)
    assert found and isinstance(routes, dict) and routes
    return rendered, routes


def _action_filter(route):
    filt = route.get("filters")
    specs = filt if isinstance(filt, list) else [filt] if filt else []
    return next(s for s in specs if isinstance(s, dict) and s.get("field") == "action")


def _assert_route_contract(routes, name):
    spec = CANONICAL_ROUTES[name]
    route = routes[name]
    assert route["profile"] == spec["profile"]
    assert route["events"] == spec["events"]
    assert set(_action_filter(route)["in"]) == set(spec["action_admits"])
    assert isinstance(route["secret"], str) and route["secret"]
    assert isinstance(route["prompt"], str) and route["prompt"].strip()
    # The prompt must template the payload, not hard-code a value: the behaviour
    # tests post repository.full_name and require it in the dispatched text.
    assert "{repository.full_name}" in route["prompt"]
    assert isinstance(route["skills"], list) and route["skills"]
    return route


# ── Webhook harness helpers ──
def _github_sig(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _multiplex_app(monkeypatch, tmp_path, routes: dict):
    """Wire a WebhookAdapter over `routes` with the /p/<profile>/ multiplex prefix and capture
    dispatched agent runs. Returns (app, captured)."""
    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "port": 0, "routes": routes}))

    captured = []

    async def _capture(event):
        captured.append(event)

    adapter.handle_message = _capture

    runner = MagicMock()
    runner.config.multiplex_profiles = True
    adapter.gateway_runner = runner
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex, profile_allowlist=None: [
            (name, tmp_path / "profiles" / name) for name in SERVED
        ],
        raising=False,
    )

    app = web.Application(client_max_size=adapter._max_body_bytes)
    app.router.add_post("/p/{profile}/webhooks/{route_name}", adapter._handle_webhook)
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return app, captured


async def _await_dispatch(captured, n=1, timeout=1.0):
    """Bounded deterministic wait for the fire-and-forget background dispatch (webhook.py:974)."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while len(captured) < n and loop.time() < deadline:
        await asyncio.sleep(0.01)
    return captured


async def _post(cli, path, body: bytes, event, secret=None, delivery="d-1", signed=True):
    headers = {"Content-Type": "application/json", "X-GitHub-Event": event, "X-GitHub-Delivery": delivery}
    if signed and secret is not None:
        headers["X-Hub-Signature-256"] = _github_sig(body, secret)
    return await cli.post(path, data=body, headers=headers)


# ─────────────────────────── AC-CH-F52-1 ───────────────────────────
def test_ac_ch_f52_1(tmp_path, monkeypatch):
    """The rendered DEFAULT-profile config declares an issues route bound profile: orchestrator,
    filtered on events:[issues] and a payload action filter admitting only opened/reopened, with its
    own non-empty secret, a non-empty prompt and a non-empty skills list."""
    _, routes = _rendered_default_routes(tmp_path, monkeypatch)
    route = _assert_route_contract(routes, "gh-issues")
    admits = _action_filter(route)["in"]
    assert "closed" not in admits and "edited" not in admits


# ─────────────────────────── AC-CH-F52-2 ───────────────────────────
@pytest.mark.parametrize("name", ["gh-pull-request", "gh-pr-review", "gh-check-suite"])
def test_ac_ch_f52_2(tmp_path, monkeypatch, name):
    """The rendered DEFAULT-profile config declares pull-request -> reviewer and review + check ->
    fixer, each with its own non-empty secret, its exact events + action filter, a non-empty prompt
    and a non-empty skills list."""
    _, routes = _rendered_default_routes(tmp_path, monkeypatch)
    _assert_route_contract(routes, name)


# ─────────────────────────── AC-CH-F52-3 ───────────────────────────
def test_ac_ch_f52_3(tmp_path, monkeypatch):
    """No canonical O1 ingress route uses deliver: github_comment — attributable post-backs are the
    owning bot's own gh; github_comment is reserved for unattributed notices."""
    _, routes = _rendered_default_routes(tmp_path, monkeypatch)
    offenders = [name for name, r in routes.items() if r.get("deliver") == "github_comment"]
    assert offenders == []


# ─────────────────────────── AC-CH-F52-4 ───────────────────────────
def test_ac_ch_f52_4(tmp_path, monkeypatch):
    """The DEFAULT ingress is complete and isolated: the rendered route-name set equals exactly the
    five canonical routes, their secrets are pairwise distinct, the DEFAULT webhook platform is
    enabled, and no coworker profile declares a webhook route or enables the webhook platform."""
    rendered, routes = _rendered_default_routes(tmp_path, monkeypatch)
    assert set(routes) == set(CANONICAL_ROUTES)
    secrets = [r["secret"] for r in routes.values()]
    assert len(set(secrets)) == len(secrets)
    for name, pdir in rendered.items():
        if name == DEFAULT_PROFILE:
            continue
        webhook = (_config(pdir).get("platforms") or {}).get("webhook")
        if isinstance(webhook, dict):
            assert not webhook.get("enabled")
            assert not (webhook.get("extra") or {}).get("routes")


# ─────────────────────────── AC-CH-F52-5 ───────────────────────────
@pytest.mark.asyncio
async def test_ac_ch_f52_5(tmp_path, monkeypatch):
    """A signed GitHub issues delivery with action: opened POSTed to /p/orchestrator/webhooks/<route>
    dispatches exactly once (a duplicate delivery id yields 200 duplicate, no second dispatch) to a
    run bound to orchestrator, with the prompt rendered from the payload."""
    _, routes = _rendered_default_routes(tmp_path, monkeypatch)
    name = "gh-issues"
    route = routes[name]
    sentinel = CANONICAL_ROUTES[name]["sentinel"]
    app, captured = _multiplex_app(monkeypatch, tmp_path, routes)
    payload = {"action": "opened", "issue": {"number": 42, "title": "Boom"},
               "repository": {"full_name": sentinel}}
    body = json.dumps(payload).encode()
    async with TestClient(TestServer(app)) as cli:
        first = await _post(cli, f"/p/orchestrator/webhooks/{name}", body, "issues", route["secret"], delivery="gh-1")
        assert first.status == 202
        dup = await _post(cli, f"/p/orchestrator/webhooks/{name}", body, "issues", route["secret"], delivery="gh-1")
        assert dup.status == 200 and (await dup.json()).get("status") == "duplicate"
        await _await_dispatch(captured, 1)
    assert len(captured) == 1
    assert captured[0].source.profile == "orchestrator"
    assert sentinel in captured[0].text


# ─────────────────────────── AC-CH-F52-6 ───────────────────────────
@pytest.mark.asyncio
@pytest.mark.parametrize("name,event,action", [
    ("gh-pull-request", "pull_request", "opened"),
    ("gh-pr-review", "pull_request_review", "submitted"),
    ("gh-check-suite", "check_suite", "completed"),
])
async def test_ac_ch_f52_6(tmp_path, monkeypatch, name, event, action):
    """A signed delivery for each PR/review/check route POSTed to its bound /p/<profile>/ endpoint
    dispatches exactly one run to the expected profile (pull_request -> reviewer; review, check ->
    fixer) with the route prompt rendered over the payload."""
    _, routes = _rendered_default_routes(tmp_path, monkeypatch)
    route = routes[name]
    profile = CANONICAL_ROUTES[name]["profile"]
    sentinel = CANONICAL_ROUTES[name]["sentinel"]
    app, captured = _multiplex_app(monkeypatch, tmp_path, routes)
    payload = {"action": action, "repository": {"full_name": sentinel},
               "pull_request": {"number": 7, "title": "PR"}, "check_suite": {"conclusion": "failure"}}
    body = json.dumps(payload).encode()
    async with TestClient(TestServer(app)) as cli:
        resp = await _post(cli, f"/p/{profile}/webhooks/{name}", body, event, route["secret"])
        assert resp.status == 202
        await _await_dispatch(captured, 1)
    assert len(captured) == 1
    assert captured[0].source.profile == profile
    assert sentinel in captured[0].text


# ─────────────────────────── AC-CH-F52-7 ───────────────────────────
@pytest.mark.asyncio
async def test_ac_ch_f52_7(tmp_path, monkeypatch, caplog):
    """The ingress fails closed: an invalid signature returns 401, an absent signature returns 401,
    and a route with no effective secret returns 403; none dispatches, each rejection is logged."""
    _, routes = _rendered_default_routes(tmp_path, monkeypatch)
    name = "gh-issues"
    route = routes[name]
    body = json.dumps({"action": "opened", "issue": {"number": 1, "title": "x"}}).encode()

    caplog.clear()
    app, captured = _multiplex_app(monkeypatch, tmp_path, routes)
    with caplog.at_level(logging.WARNING):
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                f"/p/orchestrator/webhooks/{name}", data=body,
                headers={"Content-Type": "application/json", "X-GitHub-Event": "issues",
                         "X-GitHub-Delivery": "d-bad", "X-Hub-Signature-256": "sha256=deadbeef"},
            )
            assert resp.status == 401
            await asyncio.sleep(0.05)
    assert captured == []
    assert any("Invalid signature for route" in r.getMessage() for r in caplog.records)

    caplog.clear()
    app_b, captured_b = _multiplex_app(monkeypatch, tmp_path, routes)
    with caplog.at_level(logging.WARNING):
        async with TestClient(TestServer(app_b)) as cli:
            resp = await _post(cli, f"/p/orchestrator/webhooks/{name}", body, "issues", delivery="d-ns", signed=False)
            assert resp.status == 401
            await asyncio.sleep(0.05)
    assert captured_b == []
    assert any("Invalid signature for route" in r.getMessage() for r in caplog.records)

    caplog.clear()
    no_secret_routes = {name: {**route, "secret": ""}}
    app_c, captured_c = _multiplex_app(monkeypatch, tmp_path, no_secret_routes)
    with caplog.at_level(logging.ERROR):
        async with TestClient(TestServer(app_c)) as cli:
            resp = await cli.post(
                f"/p/orchestrator/webhooks/{name}", data=body,
                headers={"Content-Type": "application/json", "X-GitHub-Event": "issues",
                         "X-GitHub-Delivery": "d-nosec"},
            )
            assert resp.status == 403
            await asyncio.sleep(0.05)
    assert captured_c == []
    assert any("has no HMAC secret" in r.getMessage() for r in caplog.records)


# ─────────────────────────── AC-CH-F52-8 ───────────────────────────
@pytest.mark.asyncio
async def test_ac_ch_f52_8(tmp_path, monkeypatch):
    """A delivery outside the issues route's scope is not dispatched: a non-matching X-GitHub-Event
    (ping) is ignored, a non-admitted action (closed) is ignored, a request to a different profile
    prefix (/p/reviewer/) is rejected 404, and the bare /webhooks/<route> path is rejected 404 —
    none produces a dispatch."""
    _, routes = _rendered_default_routes(tmp_path, monkeypatch)
    name = "gh-issues"
    secret = routes[name]["secret"]

    app, captured = _multiplex_app(monkeypatch, tmp_path, routes)
    async with TestClient(TestServer(app)) as cli:
        body = json.dumps({"action": "opened"}).encode()
        resp = await _post(cli, f"/p/orchestrator/webhooks/{name}", body, "ping", secret, delivery="d-ev")
        assert resp.status == 200 and (await resp.json()).get("status") == "ignored"
        await asyncio.sleep(0.05)
    assert captured == []

    app_b, captured_b = _multiplex_app(monkeypatch, tmp_path, routes)
    async with TestClient(TestServer(app_b)) as cli:
        body = json.dumps({"action": "closed", "issue": {"number": 9, "title": "z"}}).encode()
        resp = await _post(cli, f"/p/orchestrator/webhooks/{name}", body, "issues", secret, delivery="d-act")
        assert resp.status == 200 and (await resp.json()).get("status") == "ignored"
        await asyncio.sleep(0.05)
    assert captured_b == []

    app_c, captured_c = _multiplex_app(monkeypatch, tmp_path, routes)
    async with TestClient(TestServer(app_c)) as cli:
        body = json.dumps({"action": "opened", "issue": {"number": 9, "title": "z"}}).encode()
        resp = await _post(cli, f"/p/reviewer/webhooks/{name}", body, "issues", secret, delivery="d-prof")
        assert resp.status == 404
        await asyncio.sleep(0.05)
    assert captured_c == []

    app_d, captured_d = _multiplex_app(monkeypatch, tmp_path, routes)
    async with TestClient(TestServer(app_d)) as cli:
        body = json.dumps({"action": "opened", "issue": {"number": 9, "title": "z"}}).encode()
        resp = await _post(cli, f"/webhooks/{name}", body, "issues", secret, delivery="d-bare")
        assert resp.status == 404
        await asyncio.sleep(0.05)
    assert captured_d == []


# ─────────────────────────── AC-CH-F52-9 ───────────────────────────
@pytest.mark.asyncio
async def test_ac_ch_f52_9(tmp_path, monkeypatch):
    """The mention gate works: the rendered issue-comment route carries a declarative mention filter,
    an issue_comment whose body lacks the bot mention is ignored (no dispatch), and one whose body
    contains the mention is dispatched to the bound profile."""
    _, routes = _rendered_default_routes(tmp_path, monkeypatch)
    name = "gh-issue-comment"
    route = _assert_route_contract(routes, name)
    profile = CANONICAL_ROUTES[name]["profile"]
    sentinel = CANONICAL_ROUTES[name]["sentinel"]
    filt = route["filters"]
    specs = filt if isinstance(filt, list) else [filt]
    mention_spec = next(s for s in specs if s.get("field") == "comment.body")
    assert MENTION_TOKEN in (mention_spec.get("contains") or mention_spec.get("regex") or "")

    app, captured = _multiplex_app(monkeypatch, tmp_path, routes)
    async with TestClient(TestServer(app)) as cli:
        body = json.dumps({"action": "created", "comment": {"body": "no ping here"},
                           "issue": {"number": 5, "title": "t"}, "repository": {"full_name": sentinel}}).encode()
        resp = await _post(cli, f"/p/{profile}/webhooks/{name}", body, "issue_comment", route["secret"], delivery="c-1")
        assert resp.status == 200 and (await resp.json()).get("status") == "ignored"
        await asyncio.sleep(0.05)
    assert captured == []

    app_b, captured_b = _multiplex_app(monkeypatch, tmp_path, routes)
    async with TestClient(TestServer(app_b)) as cli:
        body = json.dumps({"action": "created", "comment": {"body": f"hey {MENTION_TOKEN} please look"},
                           "issue": {"number": 5, "title": "t"}, "repository": {"full_name": sentinel}}).encode()
        resp = await _post(cli, f"/p/{profile}/webhooks/{name}", body, "issue_comment", route["secret"], delivery="c-2")
        assert resp.status == 202
        await _await_dispatch(captured_b, 1)
    assert len(captured_b) == 1
    assert captured_b[0].source.profile == profile
    assert sentinel in captured_b[0].text
