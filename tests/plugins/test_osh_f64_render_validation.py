"""Unit tests for the OSH-F64 OpenShell-0.0.72 render extension on the v1 grammar.

Behaviour contract on ``_validate_openshell_egress`` / ``_validate_openshell_read_only`` /
``_validate_openshell_binaries`` / ``_openshell_policy_document``: on the v1
``network_policies`` render the base allow-set is the two endpoints {proxy_addr,
inference_route} (ssh is INBOUND via the proxy socket, never an egress target); the optional
``broker_addr`` is appended as a THIRD endpoint and gets the same wildcard / control-plane
(``:10256``) / ssh (``:22``) refusal as every entry; the optional ``filesystem_read_only``
fail-closes on a present ``null`` / non-list / empty / control-char / relative / ``..`` /
wildcard entry and, when valid, is APPENDED to ``filesystem_policy.read_only``; the optional
``binaries`` fail-closes on the same malformed values EXCEPT a ``*`` glob is permitted (a
binary path is resolved by the sandbox, not an egress boundary), and is APPENDED to
``network_policies.worker-egress.binaries``. An ABSENT optional key yields the base v1 policy
(OSH-F63 4c1ef384 two-endpoint back-compat). No network; imports the plugin's compose module directly.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pytest
import yaml

PLUGIN_KEY = "nv-coworker-compose"
# v1 base egress: NO ssh_control_path (the v1 render forbids outbound :22; ssh is inbound).
BASE_EGRESS = {
    "proxy_addr": "172.17.0.1:18255",
    "inference_route": "inference-api.nvidia.com:443",
    "sandbox_image": "localhost/hermes-openshell-sandbox:pinned",
}
PROXY = "172.17.0.1:18255"
INFERENCE = "inference-api.nvidia.com:443"
BROKER = "172.17.0.1:18777"


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "plugins" / PLUGIN_KEY / "plugin.yaml").exists():
            return parent
    pytest.fail("could not locate repo root")


def _compose():
    path = _repo_root() / "plugins" / PLUGIN_KEY / "compose.py"
    spec = importlib.util.spec_from_file_location("osh_f64_compose_unit", path)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: compose.py's frozen @dataclass resolves cls.__module__ via
    # sys.modules during class creation, which is None for an unregistered synthetic module.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _endpoint(host: str, port: int) -> dict:
    return {"host": host, "port": port, "protocol": "rest", "enforcement": "enforce",
            "rules": [{"allow": {"method": "POST", "path": "/**"}}]}


def _raw_endpoint(host: str, port: int) -> dict:
    # A proxy CONNECT-tunnel hop: raw passthrough, no protocol/enforcement/rules.
    return {"host": host, "port": port, "tls": "skip"}


# --- allow-set validation -------------------------------------------------------------

def test_broker_addr_appended_as_third_endpoint():
    """A declared broker_addr is the 3rd allow entry, after proxy/inference, in order, and
    the two proxy CONNECT-tunnel hops (proxy_addr + broker_addr) are the raw_hops set — the
    endpoints that render as raw tls:skip."""
    c = _compose()
    params = c._validate_openshell_egress({**BASE_EGRESS, "broker_addr": BROKER})
    assert params["allow"] == [PROXY, INFERENCE, BROKER]
    assert params["raw_hops"] == [PROXY, BROKER]


def test_absent_optional_fields_is_the_two_endpoint_backcompat():
    """No broker_addr / filesystem_read_only / binaries -> the base two-endpoint v1 allow,
    None for both optional render inputs, and an EMPTY raw_hops so the render toggle stays
    off and both endpoints keep protocol:rest (72042ac / OSH-F63 4c1ef384 back-compat)."""
    c = _compose()
    params = c._validate_openshell_egress(dict(BASE_EGRESS))
    assert params["allow"] == [PROXY, INFERENCE]
    assert params["filesystem_read_only"] is None
    assert params["binaries"] is None
    assert params["raw_hops"] == []


def test_broker_addr_control_plane_10256_is_refused():
    """The broker gets the same :10256 control-plane refusal as every allow entry."""
    c = _compose()
    with pytest.raises(c.CompositionError):
        c._validate_openshell_egress({**BASE_EGRESS, "broker_addr": "172.17.0.1:10256"})


def test_broker_addr_ssh_22_is_refused():
    """Outbound :22 is forbidden on v1 (inbound-ssh host:port), broker included."""
    c = _compose()
    with pytest.raises(c.CompositionError):
        c._validate_openshell_egress({**BASE_EGRESS, "broker_addr": "172.17.0.1:22"})


def test_broker_addr_wildcard_is_refused():
    """A wildcard broker is refused like any wildcard allow host."""
    c = _compose()
    with pytest.raises(c.CompositionError):
        c._validate_openshell_egress({**BASE_EGRESS, "broker_addr": "*:18777"})


# --- filesystem_read_only validation --------------------------------------------------

def test_filesystem_read_only_valid_is_returned():
    """A well-formed absolute-path lane is returned for the filesystem_policy render."""
    c = _compose()
    params = c._validate_openshell_egress({**BASE_EGRESS, "filesystem_read_only": ["/opt/osh-lane"]})
    assert params["filesystem_read_only"] == ["/opt/osh-lane"]


@pytest.mark.parametrize("bad", [
    None,                    # present-but-null (distinct from absent)
    [],                      # empty list
    "/opt/osh-lane",         # non-list (a bare string)
    ["/opt/osh-lane\t"],     # tab control char
    ["/opt/osh-lane\x01"],   # C0 control char
    ["/opt/osh-lane\x7f"],   # DEL
    ["opt/osh-lane"],        # relative path
    ["/opt/../etc"],         # .. traversal
    ["/opt/*"],              # wildcard (a lane is an egress boundary — no glob)
    [""],                    # empty entry
])
def test_filesystem_read_only_malformed_is_refused(bad):
    """A PRESENT filesystem_read_only fail-closes on any malformed value/entry."""
    c = _compose()
    with pytest.raises(c.CompositionError):
        c._validate_openshell_egress({**BASE_EGRESS, "filesystem_read_only": bad})


# --- binaries validation (a ``*`` glob is permitted, unlike a lane/host) --------------

def test_binaries_valid_returned_with_glob_allowed():
    """Absolute binary paths are returned unchanged, and a ``*`` glob is permitted."""
    c = _compose()
    binaries = ["/usr/bin/python3*", "/opt/hermes/.venv/bin/python"]
    params = c._validate_openshell_egress({**BASE_EGRESS, "binaries": binaries})
    assert params["binaries"] == binaries


@pytest.mark.parametrize("bad", [
    None,                    # present-but-null (distinct from absent)
    [],                      # empty list
    "/usr/bin/python3",      # non-list (a bare string)
    ["/usr/bin/py\t"],       # tab control char
    ["/usr/bin/py\x01"],     # C0 control char
    ["usr/bin/python3"],     # relative path
    ["/usr/../etc/x"],       # .. traversal
    [""],                    # empty entry
])
def test_binaries_malformed_is_refused(bad):
    """A PRESENT binaries fail-closes on any malformed value/entry (but NOT on a ``*``)."""
    c = _compose()
    with pytest.raises(c.CompositionError):
        c._validate_openshell_egress({**BASE_EGRESS, "binaries": bad})


# --- full policy-document assembly -----------------------------------------------------

def test_policy_document_full_v1_with_all_extensions():
    """_openshell_policy_document appends broker (3rd endpoint), the read-only lane and the
    binaries to the exact v1 five-section document; with the delta present the two proxy
    CONNECT-tunnel hops (18255 proxy_addr + 18777 broker_addr) render as raw tls:skip while
    inference-api:443 keeps protocol:rest (it is a real HTTP-API endpoint, not a tunnel)."""
    c = _compose()
    doc = c._openshell_policy_document(
        [PROXY, INFERENCE, BROKER],
        filesystem_read_only=["/opt/osh-lane"],
        binaries=["/usr/bin/python3*", "/opt/hermes/.venv/bin/python"],
        raw_hops=[PROXY, BROKER],
    )
    assert doc == {
        "version": 1,
        "filesystem_policy": {
            "include_workdir": True,
            "read_only": ["/usr", "/bin", "/lib", "/etc", "/opt/osh-lane"],
            "read_write": ["/tmp"],
        },
        "landlock": {"compatibility": "best_effort"},
        "process": {"run_as_user": "sandbox", "run_as_group": "sandbox"},
        "network_policies": {"worker-egress": {
            "name": "worker-egress",
            "binaries": [{"path": "/usr/bin/curl"}, {"path": "/usr/bin/python3*"},
                         {"path": "/opt/hermes/.venv/bin/python"}],
            "endpoints": [_raw_endpoint("172.17.0.1", 18255), _endpoint("inference-api.nvidia.com", 443),
                          _raw_endpoint("172.17.0.1", 18777)],
        }},
    }


def test_policy_document_base_v1_without_extensions():
    """With no optional inputs the document is the OSH-F63 4c1ef384 base v1 shape: two
    endpoints BOTH protocol:rest (no raw_hops -> toggle off), /usr/bin/curl only, and the
    base read_only lanes (no /opt/osh-lane, no third endpoint)."""
    c = _compose()
    doc = c._openshell_policy_document([PROXY, INFERENCE])
    assert doc["filesystem_policy"]["read_only"] == ["/usr", "/bin", "/lib", "/etc"]
    assert doc["network_policies"]["worker-egress"]["binaries"] == [{"path": "/usr/bin/curl"}]
    assert doc["network_policies"]["worker-egress"]["endpoints"] == [
        _endpoint("172.17.0.1", 18255), _endpoint("inference-api.nvidia.com", 443)]


def test_policy_document_raw_hops_default_off_keeps_rest():
    """The render toggle is keyed strictly on raw_hops: even with the broker present in the
    allow-set, an absent raw_hops leaves ALL three endpoints protocol:rest (back-compat
    safety — tls:skip is never inferred from the allow-set alone)."""
    c = _compose()
    doc = c._openshell_policy_document([PROXY, INFERENCE, BROKER])
    assert doc["network_policies"]["worker-egress"]["endpoints"] == [
        _endpoint("172.17.0.1", 18255), _endpoint("inference-api.nvidia.com", 443),
        _endpoint("172.17.0.1", 18777)]


# --- OSH-F64 §D7 chain-dial: preserve_existing fail-closed guard ----------------------

CHAIN_ADDR = "127.0.0.1:18255"
CA_BUNDLE = "/etc/osh-lane/ca-bundle.pem"


@pytest.mark.parametrize("preserved", ["https_proxy", "HTTPS_PROXY", " HTTPS_PROXY ", "SSL_CERT_FILE", "CURL_CA_BUNDLE"])
def test_openshell_chain_dial_rejects_preserved_proxy_ca(preserved):
    """A secrets.preserve_existing entry naming a forced proxy/CA var is refused fail-closed:
    it would win over secrets.onecli.override_existing and keep the inherited denied proxy,
    defeating the chain-dial (OSH-F64 §D7)."""
    c = _compose()
    config = {"secrets": {"preserve_existing": [preserved]}}
    with pytest.raises(c.CompositionError):
        c._enforce_openshell_chain_dial(config, "builder", CHAIN_ADDR, CA_BUNDLE)


def test_openshell_chain_dial_allows_benign_preserve_and_sets_fields():
    """A preserve_existing carve-out that does NOT name a proxy/CA var is allowed, and the
    render sets the four chain-dial fields (enabled + override_existing + the two settings)."""
    c = _compose()
    config = {"secrets": {"preserve_existing": ["ANTHROPIC_API_KEY"]}}
    c._enforce_openshell_chain_dial(config, "builder", CHAIN_ADDR, CA_BUNDLE)
    assert config["secrets"]["onecli"]["enabled"] is True
    assert config["secrets"]["onecli"]["override_existing"] is True
    settings = config["plugins"]["entries"]["podman-onecli"]["settings"]
    assert settings["proxy_rewrite"] == CHAIN_ADDR
    assert settings["ca_bundle"] == CA_BUNDLE
    assert config["secrets"]["preserve_existing"] == ["ANTHROPIC_API_KEY"]


# --- OSH-F64 §D7.1: onecli_secret_ids validation (reject the grant-revocation footgun) --

@pytest.mark.parametrize("bad", [[], [""], "Anthropic-Dev", [123], [" "], ["a\n"], None])
def test_openshell_onecli_secret_ids_rejects_malformed(bad):
    """A PRESENT egress.onecli_secret_ids fail-closes unless it is a non-empty list of
    non-empty strings — [] is rejected because onecli-onboard on an empty grant does
    set_secrets([]), REVOKING the identity's secret → 401 (OSH-F64 §D7.1)."""
    c = _compose()
    with pytest.raises(c.CompositionError):
        c._validate_openshell_egress({**BASE_EGRESS, "onecli_secret_ids": bad})


def test_openshell_onecli_secret_ids_absent_is_none():
    """An ABSENT onecli_secret_ids means no populate (the committed spine [] passes through)."""
    c = _compose()
    assert c._validate_openshell_egress(dict(BASE_EGRESS))["onecli_secret_ids"] is None


def test_openshell_onecli_secret_ids_valid_returned():
    """A valid list is returned (stripped) for the default profile_secret_sets populate."""
    c = _compose()
    params = c._validate_openshell_egress({**BASE_EGRESS, "onecli_secret_ids": ["Anthropic-Dev"]})
    assert params["onecli_secret_ids"] == ["Anthropic-Dev"]


def test_container_onecli_secret_ids_rejects_empty(tmp_path):
    """The reject-[] guard holds on the CONTAINER substrate too (via compose(), not only the
    openshell validator that a container spec never reaches) — a container spec cannot silently
    ship an empty grant list. A VALID container value composes fine and is ignored (frozen
    test_ac_osh_f64_11 part 2 covers that positive)."""
    c = _compose()
    source = _repo_root() / "tests" / "e2e-scenarios" / "FLEET-F62" / "spec" / "podman"
    spec_dir = tmp_path / "spec"
    shutil.copytree(source, spec_dir)
    spec_path = spec_dir / "coworker-types.yaml"
    data = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    data["egress"]["onecli_secret_ids"] = []
    spec_path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(c.CompositionError, match="onecli_secret_ids"):
        c.compose(str(spec_path), str(tmp_path / "out"))
