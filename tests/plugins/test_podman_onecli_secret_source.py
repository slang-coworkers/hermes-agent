"""Round-6 (FLEET-F62) hermetic units for podman-onecli (C1 secret_source + C1b oneclient).

Both lanes are proven functionally on the real substrate by AC-FLEET-F62-6 /
AC-CRED-F28-2 (live), but these behaviour-contract units give them a hermetic
floor: they load the plugin through the real discovery path and drive the
registered SecretSource / the oneclient transport seam directly. No network,
nothing written under ~/.hermes. These are NOT acceptance-criterion ids (the 14
are frozen); they backstop C1(i) and C1b.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import yaml

from hermes_cli.plugins import PluginManager

PLUGIN_KEY = "podman-onecli"
REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_SRC = REPO_ROOT / "plugins" / PLUGIN_KEY

EXPECTED_PROXY = "172.17.0.1:10255"
_PROXY_ALIASES = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")


def _load(tmp_path, monkeypatch):
    """Discover + load podman-onecli from an isolated HERMES_HOME with an empty
    bundled dir, the base + keyless origin configured so register(ctx)->configure
    runs. Returns the loaded oneclient module."""
    home = tmp_path / "home"
    (home / "plugins").mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, home / "plugins" / PLUGIN_KEY,
                    ignore=shutil.ignore_patterns("__pycache__"))
    (home / "config.yaml").write_text(yaml.safe_dump({
        "plugins": {"enabled": [PLUGIN_KEY], "entries": {PLUGIN_KEY: {"settings": {
            "gateway_api_base_url": "http://172.17.0.1:10256",
            "insecure_no_auth_origins": ["172.17.0.1:10256"],
            "api_key_env": "ONECLI_API_KEY",
        }}}},
    }), encoding="utf-8")
    bundled = tmp_path / "empty_bundled"
    bundled.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    monkeypatch.delenv("ONECLI_API_KEY", raising=False)
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled and loaded.error is None
    oneclient = getattr(loaded.module, "oneclient", None) or sys.modules.get("oneclient")
    assert oneclient is not None
    return manager, loaded, oneclient


class _FakeResp:
    def __init__(self, code, body=b"{}"):
        self._code, self._body = code, body

    def getcode(self):
        return self._code

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _capturing_opener(responses):
    """_OPENER.open stub recording (method, url, body) and replying from `responses`."""
    calls = []
    seq = list(responses)

    def _open(req, timeout=30):
        body = req.data.decode() if getattr(req, "data", None) else ""
        calls.append((req.get_method(), req.full_url, body))
        code, payload = seq.pop(0) if seq else (200, b"{}")
        return _FakeResp(code, payload)

    return _open, calls


# ── C1(i): the SecretSource exposes the token-bearing proxy value under all four spellings ──

def test_secret_source_exposes_all_four_proxy_spellings(tmp_path, monkeypatch):
    """OneCLI may return the proxy URL under only a subset of the four spellings; the
    SecretSource must mirror the token-bearing value onto all four so the render's
    name-only docker_forward_env resolves each to a token at exec (curl prefers the
    lowercase spelling)."""
    manager, loaded, oneclient = _load(tmp_path, monkeypatch)
    source = loaded.module.secret_source.OneCLISecretSource()

    token_url = f"http://x:aoc_architect@{EXPECTED_PROXY}"
    # OneCLI returns ONLY the uppercase HTTPS_PROXY here — the lowercase / HTTP_PROXY
    # spellings must be synthesized by the source, not left missing.
    monkeypatch.setattr(oneclient, "get_container_config", lambda *, agent: {
        "env": {
            "HTTPS_PROXY": token_url,
            "NO_PROXY": "127.0.0.1,localhost,::1",
            "SSL_CERT_FILE": "/etc/ssl/certs/hermes-egress-ca.crt",
            "ANTHROPIC_API_KEY": "onecli-injects-at-request-time",
        },
    })
    profile = tmp_path / "profiles" / "architect"
    profile.mkdir(parents=True)
    result = source.fetch({}, profile)

    assert result.ok
    for name in _PROXY_ALIASES:
        assert result.secrets.get(name) == token_url, (
            f"{name} must carry the token-bearing proxy URL, got {result.secrets.get(name)!r}"
        )


def test_secret_source_no_proxy_synthesizes_nothing(tmp_path, monkeypatch):
    """A keyless / no-proxy container-config leaves no proxy spelling in the secret scope —
    the source never invents a proxy value where OneCLI provided none."""
    manager, loaded, oneclient = _load(tmp_path, monkeypatch)
    source = loaded.module.secret_source.OneCLISecretSource()

    monkeypatch.setattr(oneclient, "get_container_config", lambda *, agent: {
        "env": {"SSL_CERT_FILE": "/etc/ssl/certs/hermes-egress-ca.crt"},
    })
    profile = tmp_path / "profiles" / "architect"
    profile.mkdir(parents=True)
    result = source.fetch({}, profile)

    assert result.ok
    for name in _PROXY_ALIASES:
        assert name not in result.secrets, f"no proxy value in env => {name} must be absent"


# ── C1b: set_secrets resolves identifier->uuid (GET) then PUTs secretIds (strict order) ──

def test_set_secrets_strict_get_then_put_grant_and_revoke(tmp_path, monkeypatch):
    """set_secrets makes EXACTLY [GET /api/agents, PUT /api/agents/<uuid>/secrets] — grant
    body is the exact secretIds list, revoke body is an empty list — and never a
    POST/PUT to /api/agents/<identifier>/secrets (the 404-ing base behaviour)."""
    manager, loaded, oneclient = _load(tmp_path, monkeypatch)
    uuid = "4d1da6ff-4af8-44f3-8831-07b11bfd004f"
    agents_body = json.dumps([{"identifier": "architect", "id": uuid}]).encode()
    secret_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    grant_open, grant_calls = _capturing_opener([(200, agents_body), (200, b"{}")])
    monkeypatch.setattr(oneclient._OPENER, "open", grant_open)
    oneclient.set_secrets(identifier="architect", secrets=[secret_id])
    assert [m for m, _u, _b in grant_calls] == ["GET", "PUT"], "exactly GET then PUT"
    assert grant_calls[0][1].rstrip("/").endswith("/api/agents")
    assert f"/api/agents/{uuid}/secrets" in grant_calls[1][1]
    assert json.loads(grant_calls[1][2]).get("secretIds") == [secret_id]
    assert all("/architect/secrets" not in u for _m, u, _b in grant_calls), \
        "never key the secrets endpoint by identifier"

    revoke_open, revoke_calls = _capturing_opener([(200, agents_body), (200, b"{}")])
    monkeypatch.setattr(oneclient._OPENER, "open", revoke_open)
    oneclient.set_secrets(identifier="architect", secrets=[])
    assert [m for m, _u, _b in revoke_calls] == ["GET", "PUT"]
    assert json.loads(revoke_calls[1][2]).get("secretIds") == []
