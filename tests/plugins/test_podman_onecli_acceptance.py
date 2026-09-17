"""Acceptance test for the podman-onecli plugin (CRED-F28).

Loads the plugin through the real discovery path from an isolated HERMES_HOME
with an empty bundled dir (tests/hermes_cli/test_plugin_api_compat.py shape),
asserts the registration each criterion depends on, then drives the behaviour
through the registered entries. A register(ctx): pass plugin fails every test.

Pytest criteria only; AC-CRED-F28-2 is sandbox: and ships as
tests/e2e-scenarios/CRED-F28/AC-CRED-F28-2.md.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import yaml

from agent.secret_sources.base import ErrorKind
from agent.secret_sources.registry import get_source
from hermes_cli.plugins import PluginManager

PLUGIN_KEY = "podman-onecli"
REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_SRC = REPO_ROOT / "plugins" / PLUGIN_KEY
WRAPPER = PLUGIN_SRC / "bin" / "podman-onecli-wrap"

SANDBOX_CA = "/etc/ssl/certs/hermes-egress-ca.crt"
EXPECTED_PROXY = "172.17.0.1:10255"
PROVIDER_PLACEHOLDER = "onecli-injects-at-egress"
PROXY_URL_KEYS = {"HTTPS_PROXY", "HTTP_PROXY"}
EXPECTED_SECRET_KEYS = {
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "ANTHROPIC_API_KEY",
}
PROVIDER_CANARY = "sk-real-provider-canary-DO-NOT-LEAK"
CONTROL_PLANE_CANARY = "aoc_control_plane_canary_DO_NOT_LEAK"


class _FakeTransport:
    """Stubbed OneCLI HTTP transport: (method, path[, json]) -> (status, body).

    The REAL oneclient.ensure_agent / set_secrets / get_container_config run on
    top of this, so their status-code interpretation (POST /api/agents 409 ->
    created:false, GET /v1/container-config 404 -> None) is what is under test —
    not a re-implementation in the stub. Records the logical call sequence.
    """

    def __init__(self):
        self.calls = []       # list[(kind, identifier)]
        self.existing = set()  # identifiers already provisioned (re-POST -> 409)
        self.secrets = {}      # identifier -> last secrets list

    def __call__(self, method, path, *, json=None, **_):
        if method == "POST" and path == "/api/agents":
            ident = json["identifier"]
            self.calls.append(("ensure_agent", ident))
            if ident in self.existing:
                return 409, {"error": "agent already exists"}
            self.existing.add(ident)
            return 201, {"identifier": ident}
        if method == "POST" and path.startswith("/api/agents/") and path.endswith("/secrets"):
            ident = path[len("/api/agents/"):-len("/secrets")]
            self.calls.append(("set_secrets", ident))
            self.secrets[ident] = list((json or {}).get("secrets", []))
            return 200, {}
        if method == "GET" and path.startswith("/v1/container-config"):
            ident = parse_qs(urlparse(path).query)["agent"][0]
            self.calls.append(("get_container_config", ident))
            if ident not in self.existing:
                return 404, None
            token = f"aoc_{ident}"
            return 200, {
                "env": {
                    "HTTPS_PROXY": f"http://x:{token}@{EXPECTED_PROXY}",
                    "HTTP_PROXY": f"http://x:{token}@{EXPECTED_PROXY}",
                    "NO_PROXY": "127.0.0.1,localhost,::1",
                    "SSL_CERT_FILE": SANDBOX_CA,
                    "REQUESTS_CA_BUNDLE": SANDBOX_CA,
                    "ANTHROPIC_API_KEY": PROVIDER_PLACEHOLDER,
                },
                "caCertificateContainerPath": SANDBOX_CA,
            }
        raise AssertionError(f"unexpected OneCLI call {method} {path}")


def _load(tmp_path, monkeypatch):
    hermes_home = tmp_path / "home"
    (hermes_home / "plugins").mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, hermes_home / "plugins" / PLUGIN_KEY)
    # secrets.onecli.enabled is left unset, so SecretSource.is_enabled() is
    # False and discovery does NOT pull the source (which would need a live
    # transport). Tests install a stub transport and drive fetch()/ensure_agent.
    (hermes_home / "config.yaml").write_text(
        "plugins:\n"
        f"  enabled: [{PLUGIN_KEY}]\n"
        "  entries:\n"
        f"    {PLUGIN_KEY}:\n"
        "      settings:\n"
        "        profile_secret_sets:\n"
        "          cred-f28-bot-a: []\n",
        encoding="utf-8",
    )
    empty_bundled = tmp_path / "empty_bundled"
    empty_bundled.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(empty_bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled is True
    assert loaded.error is None
    assert loaded.module is not None
    return manager, loaded, hermes_home


@pytest.mark.linux_only
def test_ac_cred_f28_1(tmp_path, monkeypatch):
    """AC-CRED-F28-1: The podman wrapper fails closed AND allowlists the two probes: it refuses a long-lived sandbox launch (`run -d`/`create` … `sleep infinity`) whose env lacks the expected OneCLI proxy address or whose mounts lack the read-only CA (exit ≠ 0, logged, real podman not invoked), while passing through the cgroup probe (`run --rm … sleep 0`, no `-v`) and the storage-opt probe (`create --storage-opt size=1m hello-world`) unchanged and preserving stdout byte-for-byte."""
    manager, loaded, _ = _load(tmp_path, monkeypatch)
    assert "on_session_start" in loaded.hooks_registered
    assert WRAPPER.is_file() and os.access(WRAPPER, os.X_OK)

    stub = tmp_path / "bin" / "podman-stub"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$STUB_REC"\necho deadbeefcafe\n', encoding="utf-8")
    stub.chmod(0o755)
    rec = tmp_path / "rec.txt"
    log = tmp_path / "wrapper.log"
    ca_host = tmp_path / "ca.crt"
    ca_host.write_text("--CA--", encoding="utf-8")
    ca_ro = f"{ca_host}:{SANDBOX_CA}:ro"

    # Rendered egress env names delivered to the sandbox by name-only -e (docker.py:1313-1322).
    proxy_names = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
    ca_names = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
                "NODE_EXTRA_CA_CERTS", "HERMES_CA_BUNDLE", "DENO_CERT")
    required_env = proxy_names + ca_names
    good_proxy = f"http://x:aoc_bota@{EXPECTED_PROXY}"

    def env_for(proxy):
        env = {
            **os.environ,
            "PODMAN_ONECLI_PODMAN": str(stub),
            "PODMAN_ONECLI_EXPECTED_PROXY": EXPECTED_PROXY,
            "PODMAN_ONECLI_EXPECTED_CA": SANDBOX_CA,
            "PODMAN_ONECLI_LOG": str(log),
            "STUB_REC": str(rec),
        }
        for k in required_env:
            env.pop(k, None)
        if proxy is not None:
            for k in proxy_names:
                env[k] = proxy
            for k in ca_names:
                env[k] = SANDBOX_CA
        return env

    def launch(verb, names, mount):
        argv = [verb, "-d"] if verb == "run" else [verb]
        argv += ["--name", "s"]
        for n in names:
            argv += ["-e", n]
        if mount:
            argv += ["-v", mount]
        return argv + ["img", "sleep", "infinity"]

    def run_wrap(argv, proxy=good_proxy):
        rec.write_text("", encoding="utf-8")
        log.write_text("", encoding="utf-8")
        return subprocess.run([str(WRAPPER), *argv], env=env_for(proxy),
                              capture_output=True, text=True, timeout=30)

    r = run_wrap(launch("run", required_env, ca_ro), proxy=None)
    assert r.returncode != 0 and rec.read_text() == "" and "refuse" in log.read_text().lower()

    r = run_wrap(launch("run", [n for n in required_env if n != "HTTPS_PROXY"], ca_ro))
    assert r.returncode != 0 and rec.read_text() == ""

    r = run_wrap(launch("run", required_env, None))
    assert r.returncode != 0 and rec.read_text() == ""

    r = run_wrap(launch("run", required_env, f"{ca_host}:{SANDBOX_CA}:rw"))
    assert r.returncode != 0 and rec.read_text() == ""

    r = run_wrap(launch("run", required_env, ca_ro), proxy="http://x:aoc_bota@10.0.0.9:1")
    assert r.returncode != 0 and rec.read_text() == ""
    refusal_log = log.read_text()
    assert "aoc_bota" not in refusal_log and "http://x:aoc_bota@" not in refusal_log

    r = run_wrap(launch("create", [], ca_ro), proxy=None)
    assert r.returncode != 0 and rec.read_text() == ""

    probe = ["run", "--rm", "--cpus", "0.5", "--memory", "64m", "--pids-limit", "32", "img", "sleep", "0"]
    r = run_wrap(probe)
    assert r.returncode == 0 and rec.read_text() == " ".join(probe) + "\n" and r.stdout == "deadbeefcafe\n"

    sprobe = ["create", "--storage-opt", "size=1m", "hello-world"]
    r = run_wrap(sprobe)
    assert r.returncode == 0 and rec.read_text() == " ".join(sprobe) + "\n" and r.stdout == "deadbeefcafe\n"

    r = run_wrap(launch("run", required_env, ca_ro))
    assert r.returncode == 0 and r.stdout == "deadbeefcafe\n"
    forwarded = rec.read_text()
    assert "sleep infinity" in forwarded and "nv.hermes-ppid" in forwarded


def test_ac_cred_f28_3(tmp_path, monkeypatch):
    """AC-CRED-F28-3: No real PROVIDER credential appears in any resolved secret, sandbox, or rendered file: the SecretSource resolves only OneCLI proxy coordinates (proxy URLs carrying the `aoc_` agent token as userinfo, CA-trust vars, CA path) plus a placeholder provider key. The host-only OneCLI control-plane key `ONECLI_API_KEY` is exempt — it authenticates the onboard/render step to OneCLI and never enters a sandbox or a rendered profile."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", PROVIDER_CANARY)
    monkeypatch.setenv("ONECLI_API_KEY", CONTROL_PLANE_CANARY)
    manager, loaded, hermes_home = _load(tmp_path, monkeypatch)
    fake = _FakeTransport()
    fake.existing.add("cred-f28-bot-a")  # agent already provisioned in OneCLI
    loaded.module.oneclient.set_transport(fake)

    source = get_source("onecli", scope=manager.scope_key)
    assert source is not None
    assert source.name == "onecli" and source.shape == "mapped"

    profile = tmp_path / "profiles" / "cred-f28-bot-a"
    profile.mkdir(parents=True)
    result = source.fetch({}, profile)
    assert result.ok
    assert set(result.secrets) == EXPECTED_SECRET_KEYS
    assert result.secrets["ANTHROPIC_API_KEY"] == PROVIDER_PLACEHOLDER

    for key, value in result.secrets.items():
        if key in PROXY_URL_KEYS:
            # The aoc_ agent token is the attribution coordinate; allowed only as proxy userinfo.
            assert value.startswith("http://x:aoc_") and value.endswith(f"@{EXPECTED_PROXY}")
        else:
            assert "aoc_" not in value
    joined = "\n".join(result.secrets.values())
    assert PROVIDER_CANARY not in joined
    assert CONTROL_PLANE_CANARY not in joined

    for path in hermes_home.rglob("*"):
        if path.is_file():
            blob = path.read_bytes()
            assert PROVIDER_CANARY.encode() not in blob
            assert CONTROL_PLANE_CANARY.encode() not in blob


def test_ac_cred_f28_4(tmp_path, monkeypatch):
    """AC-CRED-F28-4: The plugin uses only the public plugin surface — it registers exactly `on_session_start` (no other hook, and specifically NO `pre_tool_call` veto), the `onecli` SecretSource, and the `onecli-onboard` CLI command, and registers no `TerminalEnvironmentProvider`."""
    manager, loaded, _ = _load(tmp_path, monkeypatch)
    assert set(loaded.hooks_registered) == {"on_session_start"}
    assert "pre_tool_call" not in loaded.hooks_registered
    assert get_source("onecli", scope=manager.scope_key) is not None
    assert "onecli-onboard" in manager._cli_commands
    assert not any(
        r.active and r.plugin_key == PLUGIN_KEY and r.kind == "terminal_environment_provider"
        for r in manager._registration_order
    )


def test_ac_cred_f28_5(tmp_path, monkeypatch):
    """AC-CRED-F28-5: Onboarding calls `ensureAgent(identifier=<profile>)` BEFORE `getContainerConfig`, and a 409 (agent already exists) is tolerated (created:false, no error) — so a never-run profile does not fail its first render."""
    manager, loaded, _ = _load(tmp_path, monkeypatch)
    oc = loaded.module.oneclient
    fake = _FakeTransport()
    oc.set_transport(fake)
    source = get_source("onecli", scope=manager.scope_key)

    profile = tmp_path / "profiles" / "cred-f28-bot-a"
    profile.mkdir(parents=True)

    # Never-run profile: no OneCLI identity yet -> NOT_CONFIGURED, not an error.
    pre = source.fetch({}, profile)
    assert pre.ok is False and pre.error_kind == ErrorKind.NOT_CONFIGURED

    entry = manager._cli_commands["onecli-onboard"]
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="_cmd")
    entry["setup_fn"](subs.add_parser("onecli-onboard"))
    args = parser.parse_args(["onecli-onboard", "--profile", str(profile)])

    # Two onboard passes through the registered handler: the first POST /api/agents
    # returns 201 (create), the second returns 409 (already exists). Both complete
    # without error and call ensure_agent BEFORE get_container_config.
    for _ in range(2):
        fake.calls.clear()
        entry["handler_fn"](args)
        kinds = [kind for kind, _ in fake.calls]
        assert kinds == ["ensure_agent", "set_secrets", "get_container_config"]
        assert fake.secrets["cred-f28-bot-a"] == []

    # The 409 tolerance is the REAL client's mapping, not the stub's: a repeat
    # POST (agent exists) -> {"created": False} with no exception; a fresh id -> 201.
    assert oc.ensure_agent(identifier="cred-f28-bot-a") == {"created": False}
    assert oc.ensure_agent(identifier="cred-f28-bot-b") == {"created": True}

    post = source.fetch({}, profile)
    assert post.ok


# The base-URL guard's keyless exact-origin allowance: an unauthenticated OneCLI
# bridge control plane is reachable over http:// only from an allowlisted exact origin
# WHEN no bootstrap key is set; a present key, userinfo, or a 401/403 still fails closed.


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


def _load_podman_onecli(tmp_path, monkeypatch, origins):
    """Load podman-onecli through the real discovery path with the bridge base +
    allowlist in config.yaml, so register(ctx) -> ctx.get_config -> configure(...)
    actually runs (proving the plumbing, not just the guard function)."""
    home = tmp_path / "poh"
    (home / "plugins").mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, home / "plugins" / "podman-onecli",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (home / "config.yaml").write_text(yaml.safe_dump({
        "plugins": {"enabled": ["podman-onecli"], "entries": {"podman-onecli": {"settings": {
            "gateway_api_base_url": "http://172.17.0.1:10256",
            "insecure_no_auth_origins": origins,
            "api_key_env": "ONECLI_API_KEY",
        }}}},
    }), encoding="utf-8")
    bundled = tmp_path / "poh-bundled"
    bundled.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    monkeypatch.delenv("ONECLI_API_KEY", raising=False)
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins["podman-onecli"]
    assert loaded.enabled and loaded.error is None
    oneclient = getattr(loaded.module, "oneclient", None) or sys.modules.get("oneclient")
    assert oneclient is not None, "podman-onecli __init__ must expose its oneclient module"
    return oneclient


@pytest.mark.parametrize("fail_status", [401, 403])
def test_podman_onecli_optional_api_key_bridge_base(tmp_path, monkeypatch, fail_status):
    """The podman-onecli base-URL guard, configured through register(ctx)->configure(),
    permits http:// only to an allowlisted EXACT origin when no bootstrap key is set,
    still protects a present key, rejects userinfo, and fails closed on 401/403. The
    exact-origin bound (not host-only) is deliberate: get_container_config returns the
    aoc_ proxy token as userinfo, so a host-only allowlist would leak it on any other
    port of the same host."""
    oneclient = _load_podman_onecli(tmp_path, monkeypatch, ["172.17.0.1:10256"])

    oneclient._require_secure_base("http://172.17.0.1:10256")
    captured = {}

    def _capture(req, timeout=30):
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        return _FakeResp(200)

    monkeypatch.setattr(oneclient._OPENER, "open", _capture)
    oneclient.get_container_config(agent="architect")
    assert "authorization" not in captured["headers"], "no key set => no Authorization header"

    with pytest.raises(oneclient.OneCLIError):
        oneclient._require_secure_base("http://172.17.0.1:9999")
    with pytest.raises(oneclient.OneCLIError):
        oneclient._require_secure_base("http://10.0.0.5:10256")
    # userinfo is refused outright on ANY scheme, before every allow branch (a
    # user:pass@ authority carries a cleartext secret regardless of scheme)
    with pytest.raises(oneclient.OneCLIError):
        oneclient._require_secure_base("http://user:pass@172.17.0.1:10256")
    with pytest.raises(oneclient.OneCLIError):
        oneclient._require_secure_base("https://user:pass@example.com")
    # https is always allowed; a loopback http base is still allowed (unchanged)
    oneclient._require_secure_base("https://172.17.0.1:10256")
    oneclient._require_secure_base("http://127.0.0.1:10256")

    monkeypatch.setenv("ONECLI_API_KEY", "aoc_secret")
    with pytest.raises(oneclient.OneCLIError):
        oneclient._require_secure_base("http://172.17.0.1:10256")
    monkeypatch.delenv("ONECLI_API_KEY", raising=False)

    monkeypatch.setattr(oneclient._OPENER, "open", lambda req, timeout=30: _FakeResp(fail_status))
    with pytest.raises(oneclient.OneCLIError):
        oneclient.get_container_config(agent="architect")
