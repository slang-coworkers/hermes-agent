"""Unit floor for FLEET-F62.e's openshell/trust.py (builder tests beside the acceptance test).

trust.py carries its own copy of the release NO_PROXY matchers (it must stay import-light for the
standalone installer), so these tests pin its agreement with gateway.platforms.base on every form
the ADR lists, plus the install preflight's dotenv parsing and its never-echo-a-value message rule.
"""

import importlib.util
import os
from pathlib import Path

import pytest

TRUST_PY = Path(__file__).resolve().parents[2] / "plugins" / "nv-coworker-compose" / "openshell" / "trust.py"
BRIDGE_PROBES = (("172.17.0.1", 18777), ("172.17.0.1", 18255), ("172.17.0.1", None), ("host.docker.internal", None))
FORMS = ["localhost", "127.0.0.1", "::1", "10.200.0.1", ".svc.cluster.local", "172.17.0.1", "172.17.0.1:18777",
         "172.17.0.1:10256", "[172.17.0.1]:18777", "host.docker.internal", "HOST.DOCKER.INTERNAL.",
         "host.docker.internal:10256", "172.17.0.0/16", "172.16.0.0/12", "0.0.0.0/0", "*", ".172.17.0.1",
         "*.docker.internal", ".docker.internal", "docker.internal", "10.0.0.0/8", "example.com"]


@pytest.fixture(scope="module")
def trust():
    spec = importlib.util.spec_from_file_location("fleet_f62e_trust_unit", TRUST_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _release_bypasses(entry, monkeypatch):
    from gateway.platforms import base

    monkeypatch.setenv("NO_PROXY", entry)
    monkeypatch.delenv("no_proxy", raising=False)
    hops = [f"{h}:{p}" if p else h for h, p in BRIDGE_PROBES]
    return (any(base.should_bypass_proxy([hop]) for hop in hops)
            or any(base.is_host_excluded_by_no_proxy(h, entry) for h, _ in BRIDGE_PROBES))


@pytest.mark.parametrize("entry", FORMS)
def test_bridge_entry_agrees_with_release_matchers(trust, monkeypatch, entry):
    host = entry.strip("[").split("]")[0].rsplit(":", 1)[0] if entry.count(":") == 1 or entry.startswith("[") else entry
    names_bridge = host.lower().rstrip(".") in ("172.17.0.1", "host.docker.internal")
    assert trust.is_bridge_entry(entry) is (names_bridge or _release_bypasses(entry, monkeypatch))


def test_strip_keeps_order_and_falls_back_to_loopback(trust):
    assert trust.strip_bridge_no_proxy("localhost,172.17.0.1, 10.200.0.1") == "localhost,10.200.0.1"
    assert trust.strip_bridge_no_proxy("localhost,127.0.0.1") == "localhost,127.0.0.1"
    assert trust.strip_bridge_no_proxy("*,172.17.0.1") == trust.LOOPBACK_NO_PROXY


def test_claim_uses_the_other_spelling_when_one_is_unset(trust):
    claim = trust.claim_no_proxy({"no_proxy": "localhost,172.17.0.1"})
    assert claim == {"NO_PROXY": "localhost", "no_proxy": "localhost"}


def test_derive_bundle_refuses_before_writing(trust, tmp_path):
    live, empty, dest = tmp_path / "live.pem", tmp_path / "empty.pem", tmp_path / "out" / "ca.pem"
    live.write_text("LIVE", encoding="utf-8")
    empty.write_text("", encoding="utf-8")
    with pytest.raises(trust.TrustError):
        trust.derive_bundle(str(live), [str(empty)], dest)
    assert not dest.exists()
    trust.derive_bundle(str(live), [], dest)
    assert dest.read_text(encoding="utf-8") == "LIVE\n"
    assert sorted(p.name for p in dest.parent.iterdir()) == ["ca.pem"]


@pytest.mark.parametrize("bad", [None, [], {"bundle": "/a", "merge": []}, {"bundle": "/a/./b?"}, {"bundle": "/a\x7f"}])
def test_parse_trust_refusals_name_the_key(trust, bad):
    with pytest.raises(trust.TrustError, match=r"egress\.openshell_trust"):
        trust.parse_trust(bad)


@pytest.mark.parametrize("line,keyed,expect", [
    ('NO_PROXY="localhost,172.17.0.1" # lane shim', False, ["NO_PROXY"]),
    ("export no_proxy='host.docker.internal'", False, ["no_proxy"]),
    ("SSL_CERT_FILE=", True, ["SSL_CERT_FILE"]),
    ("SSL_CERT_FILE=/copied.pem", False, []),
    ("NO_PROXY=127.0.0.1,localhost,::1", True, []),
])
def test_env_file_clashes_parse_like_the_runtime(trust, tmp_path, line, keyed, expect):
    env = tmp_path / ".env"
    env.write_text(line + "\n", encoding="utf-8")
    assert trust.env_file_clashes(env, keyed=keyed) == expect


def test_preflight_names_file_and_variable_never_value(trust, tmp_path):
    (tmp_path / ".env").write_text("CURL_CA_BUNDLE=/persisted/copy.pem\n", encoding="utf-8")
    cfg = {"NO_PROXY": "localhost,172.17.0.1:10256", "secrets": {"preserve_existing": [" DENO_CERT "]}}
    with pytest.raises(trust.TrustError) as exc:
        trust.preflight_install(cfg, tmp_path, None, keyed=True)
    text = str(exc.value)
    for name in ("CURL_CA_BUNDLE", "NO_PROXY", "DENO_CERT", ".env", "config.yaml"):
        assert name in text
    assert "/persisted/copy.pem" not in text and "10256" not in text


def test_keyless_preflight_ignores_ca_names(trust, tmp_path):
    (tmp_path / ".env").write_text("SSL_CERT_FILE=/x.pem\n", encoding="utf-8")
    trust.preflight_install({"secrets": {"preserve_existing": ["SSL_CERT_FILE"]}}, tmp_path, None, keyed=False)


def test_strip_inherited_only_on_fleet_homes(trust):
    env = {"NO_PROXY": "localhost,172.17.0.1"}
    assert trust.strip_inherited_no_proxy(env, {"terminal": {"backend": "ssh"}}) == []
    assert env["NO_PROXY"] == "localhost,172.17.0.1"
    marked = {"plugins": {"entries": {"nv-coworker-compose": {"settings": {"openshell_fleet": True}}}}}
    assert trust.strip_inherited_no_proxy(env, marked) == ["NO_PROXY"]
    assert env["NO_PROXY"] == "localhost"


def test_source_fetch_never_raises_on_hostile_config(tmp_path, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "localhost,172.17.0.1")
    from importlib import import_module

    pkg = TRUST_PY.parent
    spec = importlib.util.spec_from_file_location(
        "fleet_f62e_unit_pkg", pkg / "__init__.py", submodule_search_locations=[str(pkg)])
    mod = importlib.util.module_from_spec(spec)
    import sys

    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    source = import_module(f"{spec.name}.trust_source").OpenShellTrustSource()
    for cfg in ({}, {"bundle": 7}, {"bundle": str(tmp_path / "nope")}, {"bundle": "/", "merge": "x"}):
        result = source.fetch(cfg, tmp_path)
        assert result.ok and result.secrets["NO_PROXY"] == "localhost"
        assert not set(result.secrets) & {"SSL_CERT_FILE", "CURL_CA_BUNDLE"}
        assert result.warnings
    assert os.environ["NO_PROXY"] == "localhost,172.17.0.1", "fetch must not mutate the process env"
