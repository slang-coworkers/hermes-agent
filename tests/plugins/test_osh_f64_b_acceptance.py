"""OSH-F64.b OpenShell inference acceptance contracts."""
import re
import shutil
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_SRC = REPO_ROOT / "plugins" / "nv-coworker-compose"
SPEC = REPO_ROOT / "tests" / "e2e-scenarios" / "OSH-F64.b" / "spec" / "coworker-types.yaml"
# A non-openshell (podman substrate) spec — the FLEET-F62 container spec — proving the
# OSH-F64.b render is openshell-GATED and does not leak into the container path.
PODMAN_SPEC = REPO_ROOT / "tests" / "e2e-scenarios" / "FLEET-F62" / "spec" / "podman" / "coworker-types.yaml"

SERVED_ROLES = ("orchestrator", "architect", "builder", "tester", "reviewer")
INFERENCE_HOST = "inference.local"
ONECLI_HOP = "172.17.0.1:18255"
BROKER_HOPPORT = "172.17.0.1:18777"
FORBIDDEN_HOPS = {"172.17.0.1:10256", "172.17.0.1:22"}
ALLOWED_INFERENCE = {
    ("POST", "/v1/chat/completions"),
    ("POST", "/v1/messages"),
    ("POST", "/v1/responses"),
    ("GET", "/v1/models"),
}
# The pinned OpenShell CLI rejects these endpoint keys; the proxy bearer rewrite needs no
# on-endpoint marker, so the credential rewrite is declared config-side (AC-4), not here.
INVALID_ENDPOINT_FIELDS = ("provider", "credential", "credential_rewrite")
# The named config-side provider block (D3) and the REST base path the SDK posts under.
INFERENCE_PROVIDER_KEY = "compatible-endpoint"
INFERENCE_BASE_PATH = "/v1"
# Two SEPARATE roles (ADR §Design D3):
#   ROUTING     — the single FIXED literal that fires the OpenShell L7 rewrite, identical across profiles.
#   ATTRIBUTION — a DISTINCT per-profile non-secret tag in a DEDICATED field, never the api_key/credential slot.
# The real credential the rewrite reads (COMPATIBLE_API_KEY) lives OpenShell-side and must appear in NO rendered config.
ROUTING_PLACEHOLDER = "sk-OPENSHELL-PROXY-REWRITE"
REAL_CRED_KEY = "COMPATIBLE_API_KEY"
# Attribution uses a dedicated non-credential field; accept the supported tag keys.
ATTRIBUTION_TAG_KEYS = ("attribution_tag", "profile_tag", "tag", "label")


@pytest.fixture()
def loaded(tmp_path, monkeypatch):
    """Load nv-coworker-compose from an isolated HERMES_HOME via the real scanner."""
    hermes_home = tmp_path / "home"
    (hermes_home / "plugins").mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, hermes_home / "plugins" / "nv-coworker-compose")
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["nv-coworker-compose"]}})
    )
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")

    from hermes_cli.plugins import PluginManager

    manager = PluginManager()
    manager.discover_and_load()
    plugin = manager._plugins["nv-coworker-compose"]
    assert plugin.enabled is True
    assert plugin.error is None
    assert plugin.module is not None
    assert "coworker" in manager._cli_commands
    return plugin


def _render(plugin, spec_path, out_dir):
    """Invoke the fork compose render entrypoint."""
    plugin.module.compose(str(spec_path), str(out_dir))
    policies, configs = {}, {}
    for p in Path(out_dir).rglob("policy-*.yaml"):
        policies[p.stem] = yaml.safe_load(p.read_text(encoding="utf-8"))
    for c in Path(out_dir).rglob("config.yaml"):
        configs[c.parent.name] = yaml.safe_load(c.read_text(encoding="utf-8"))
    return policies, configs


def _gateway_policy(policies):
    """The rendered gateway/default APF policy (D2), whichever name the render uses."""
    return policies.get("policy-gateway") or policies.get("policy-default")


def _endpoints(policy):
    """All egress endpoints across the policy's network_policies."""
    out = []
    for net in (policy.get("network_policies") or {}).values():
        out.extend(net.get("endpoints") or [])
    return out


def _hostport(ep):
    host = ep.get("host")
    port = ep.get("port")
    return f"{host}:{port}" if port is not None else str(host)


def _is_raw(ep):
    return ep.get("tls") == "skip"


def _method_paths(ep):
    pairs = set()
    for rule in ep.get("rules") or []:
        allow = rule.get("allow") or {}
        pairs.add((allow.get("method"), allow.get("path")))
    return pairs


def _binaries(policy):
    """Executable-egress allow-list paths, top-level and per-network (path str or {path:...})."""
    raw = list(policy.get("binaries") or [])
    for net in (policy.get("network_policies") or {}).values():
        raw.extend(net.get("binaries") or [])
    return [b.get("path") if isinstance(b, dict) else b for b in raw]


def _inference_provider(cfg):
    """The inference model-provider block of a rendered profile config."""
    providers = cfg.get("providers")
    candidates = []
    if isinstance(providers, dict):
        candidates = list(providers.values())
    elif isinstance(providers, list):
        candidates = providers
    for prov in candidates:
        if isinstance(prov, dict) and urlsplit(str(prov.get("base_url", ""))).hostname == INFERENCE_HOST:
            return prov
    return None


def _model_selects_inference(cfg, prov):
    """The rendered config EXPLICITLY selects the inference provider via model.provider.

    An absent model.provider is rejected: Hermes may then resolve an env/automatic provider
    (runtime_provider.py:647), so a lone provider block does not prove selection.
    """
    providers = cfg.get("providers")
    sel = (cfg.get("model") or {}).get("provider")
    return isinstance(sel, str) and bool(sel) and isinstance(providers, dict) and providers.get(sel) is prov


def _attribution_tag(prov, cfg):
    """The per-profile NON-SECRET attribution tag — a DEDICATED field, never the api_key/credential slot.

    Routing (api_key = the fixed rewrite trigger) and attribution (this tag) are separate roles (ADR D3).
    """
    for key in ATTRIBUTION_TAG_KEYS:
        val = prov.get(key)
        if isinstance(val, str) and val:
            return val
        if isinstance(val, list) and val:
            return ",".join(str(v) for v in val)
    for key in ATTRIBUTION_TAG_KEYS:
        val = cfg.get(key)
        if isinstance(val, str) and val:
            return val
    return None


def test_ac_osh_f64_b_1(loaded, tmp_path):
    """The rendered gateway + worker policies contain no 172.17.0.1:18255 and no tls:skip except the broker 18777."""
    policies, _ = _render(loaded, SPEC, tmp_path / "out")
    assert {f"policy-{role}" for role in SERVED_ROLES} <= policies.keys(), (
        f"expected a policy per served role; got {sorted(policies)}"
    )
    assert len({"policy-gateway", "policy-default"} & policies.keys()) == 1, (
        f"expected exactly one rendered gateway/default policy; got {sorted(policies)}"
    )
    raw_hostports = set()
    for name, policy in policies.items():
        for ep in _endpoints(policy):
            hp = _hostport(ep)
            assert hp != ONECLI_HOP, f"{name}: OneCLI hop {ONECLI_HOP} must be gone"
            assert hp not in FORBIDDEN_HOPS, f"{name}: forbidden hop {hp} present"
            if _is_raw(ep):
                raw_hostports.add(hp)
    assert raw_hostports == {BROKER_HOPPORT}, (
        f"the only tls:skip/raw endpoint must be the broker {BROKER_HOPPORT}; got {raw_hostports}"
    )


def test_ac_osh_f64_b_2(loaded, tmp_path):
    """Inference renders as a protocol:rest/enforce endpoint with exactly the four allowed paths, no tls:skip, and no substrate-invalid on-endpoint credential field (the rewrite is config-side — AC-4), reachable by the Hermes model-call executable."""
    policies, _ = _render(loaded, SPEC, tmp_path / "out")
    gateway = _gateway_policy(policies)
    assert gateway is not None, "OSH-F64.b must render a gateway policy (D2)"
    inf = [ep for ep in _endpoints(gateway) if ep.get("host") == INFERENCE_HOST]
    assert len(inf) == 1, f"exactly one inference endpoint expected, got {len(inf)}"
    ep = inf[0]
    assert ep.get("protocol") == "rest"
    assert ep.get("enforcement") == "enforce"
    assert ep.get("tls") != "skip", "inference endpoint must NOT be tls:skip"
    assert _method_paths(ep) == ALLOWED_INFERENCE, (
        f"inference allowed set must be exactly {ALLOWED_INFERENCE}; got {_method_paths(ep)}"
    )
    for invalid in INVALID_ENDPOINT_FIELDS:
        assert invalid not in ep, f"inference endpoint must carry no '{invalid}' field"
    # request_body_credential_rewrite is a CLI-ACCEPTED field, but the deployed rewrite needs
    # no on-endpoint marker (proxy fires on the placeholder bearer), so the render omits it too.
    assert "request_body_credential_rewrite" not in ep
    # the model call is an in-process Hermes Python request, not curl: the gateway binary
    # allow-list must permit that exact executable, else the allowed endpoint is unreachable
    bins = _binaries(gateway)
    assert "/opt/hermes/.venv/bin/python" in bins, (
        f"gateway policy must permit the Hermes model-call venv interpreter, not curl-only; got {bins}"
    )


def test_ac_osh_f64_b_3(loaded, tmp_path):
    """Under openshell the render emits none of the OSH-F64 §D7/§D7.1 OneCLI chain-dial config and does not enable OneCLI for model calls (superseded)."""
    _, configs = _render(loaded, SPEC, tmp_path / "out")
    assert configs, "no rendered configs found"
    for name, cfg in configs.items():
        onecli = ((cfg.get("secrets") or {}).get("onecli")) or {}
        assert "override_existing" not in onecli, f"{name}: §D7 secrets.onecli.override_existing present"
        assert onecli.get("enabled") in (None, False), f"{name}: OneCLI must not be enabled for model calls"
        enabled_plugins = ((cfg.get("plugins") or {}).get("enabled") or [])
        assert "podman-onecli" not in enabled_plugins, f"{name}: podman-onecli must be inert under openshell"
        settings = (((cfg.get("plugins") or {}).get("entries") or {})
                    .get("podman-onecli") or {}).get("settings") or {}
        assert "proxy_rewrite" not in settings, f"{name}: §D7 proxy_rewrite present"
        assert "ca_bundle" not in settings, f"{name}: §D7 ca_bundle present"
        for role, ids in (settings.get("profile_secret_sets") or {}).items():
            assert ids == [], f"{name}: §D7.1 profile_secret_sets[{role}] populated: {ids}"
    # non-openshell control: the container (podman) render is UNAFFECTED — OSH-F64.b's
    # openshell-only additions (a rendered APF policy + the https://inference.local placeholder
    # provider) must NOT leak into the container path.
    podman_policies, podman_configs = _render(loaded, PODMAN_SPEC, tmp_path / "podman")
    assert not podman_policies, f"container render must emit NO openshell policy-*.yaml; got {sorted(podman_policies)}"
    for role in SERVED_ROLES:
        pcfg = podman_configs.get(role)
        assert pcfg is not None, f"missing container render for role {role}"
        assert _inference_provider(pcfg) is None, (
            f"{role}: the OSH-F64.b openshell inference provider (https://{INFERENCE_HOST}) leaked into the container render"
        )
        # the container path keeps its FLEET-F62 OneCLI posture untouched — proves the render
        # is openshell-gated, not that it silently disabled the container path's own credentials
        assert pcfg.get("providers"), f"{role}: container render lost its providers block"
        assert ((pcfg.get("secrets") or {}).get("onecli") or {}).get("enabled") is True, (
            f"{role}: container render must keep secrets.onecli.enabled True (FLEET-F62 baseline)"
        )
        assert "podman-onecli" in ((pcfg.get("plugins") or {}).get("enabled") or []), (
            f"{role}: container render must keep podman-onecli enabled"
        )


def test_ac_osh_f64_b_4(loaded, tmp_path):
    """Each served profile's config separates ROUTING (one fixed sk-OPENSHELL-PROXY-REWRITE api_key, identical across profiles) from ATTRIBUTION (a distinct per-profile non-secret tag, never the api_key slot); COMPATIBLE_API_KEY never appears, and the container render is unaffected."""
    _, configs = _render(loaded, SPEC, tmp_path / "out")
    api_keys, tags = {}, {}
    for role in SERVED_ROLES:
        cfg = configs.get(role)
        assert cfg is not None, f"missing rendered config for served role {role}"
        prov = _inference_provider(cfg)
        assert prov is not None, f"{role}: no inference provider pointing at {INFERENCE_HOST}"
        url = urlsplit(str(prov.get("base_url", "")))
        assert (url.scheme, url.hostname) == ("https", INFERENCE_HOST), (
            f"{role}: inference base_url must be https://{INFERENCE_HOST} (Hermes rejects a schemeless "
            f"provider URL, config.py:1648); got {prov.get('base_url')!r}"
        )
        assert _model_selects_inference(cfg, prov), (
            f"{role}: the rendered model config must SELECT the inference provider, not merely define it"
        )
        providers = cfg.get("providers") or {}
        assert isinstance(providers, dict) and isinstance(providers.get(INFERENCE_PROVIDER_KEY), dict), (
            f"{role}: the inference provider must be the named '{INFERENCE_PROVIDER_KEY}' config block"
        )
        assert providers[INFERENCE_PROVIDER_KEY] is prov, (
            f"{role}: the selected inference provider must be the '{INFERENCE_PROVIDER_KEY}' block"
        )
        assert url.path.rstrip("/") == INFERENCE_BASE_PATH, (
            f"{role}: inference base_url path must be {INFERENCE_BASE_PATH!r} "
            f"(the SDK posts {{base_url}}/chat/completions); got {prov.get('base_url')!r}"
        )
        key = prov.get("api_key")
        assert key == ROUTING_PLACEHOLDER, (
            f"{role}: inference api_key must be the fixed routing trigger {ROUTING_PLACEHOLDER!r}; got {key!r}"
        )
        assert "key_env" not in prov, f"{role}: routing uses the inline placeholder, not key_env to a real secret"
        api_keys[role] = key
        tag = _attribution_tag(prov, cfg)
        assert tag, f"{role}: no per-profile attribution tag rendered (dedicated field, not the api_key slot)"
        assert tag != key, f"{role}: the attribution tag must not be the api_key/credential slot"
        assert REAL_CRED_KEY not in tag, f"{role}: the attribution tag must be non-secret, not the real credential"
        tags[role] = tag
        # the real credential lives OpenShell-side — it must appear in NO rendered config
        assert REAL_CRED_KEY not in yaml.safe_dump(cfg), (
            f"{role}: the real credential {REAL_CRED_KEY} must not appear in a rendered config"
        )
    assert set(api_keys.values()) == {ROUTING_PLACEHOLDER}, (
        f"the routing api_key must be one fixed literal shared by all profiles; got {api_keys}"
    )
    # attribution tags are DISTINCT per profile (the observable AC-6 must resolve end-to-end)
    assert len(set(tags.values())) == len(tags), (
        f"per-profile attribution tags must be distinct: {tags}"
    )
    _, podman_configs = _render(loaded, PODMAN_SPEC, tmp_path / "podman")
    for role in SERVED_ROLES:
        pcfg = podman_configs.get(role)
        assert pcfg is not None, f"missing container render for role {role}"
        assert ROUTING_PLACEHOLDER not in yaml.safe_dump(pcfg), (
            f"{role}: the OSH-F64.b routing placeholder leaked into the container render"
        )


def test_ac_osh_f64_b_5(loaded):
    """fleet-openshell.md gains the openshell-provider runbook + COST-F30 metering note + OSH-F64 §D7/FLEET/ISO/CRED supersession, and drops the §D7 chain-dial tunnel sections (onecli-chain/socat/loopback-relay) — while PRESERVING F63's base data-plane 172.17.0.1:18255 hop, which OSH-F64.b does not supersede (test_ac_osh_f63_6)."""
    doc = (REPO_ROOT / "website" / "docs" / "user-guide" / "fleet-openshell.md").read_text(encoding="utf-8")
    low = doc.lower()
    for term in ("openshell provider", "inference.local", "credential rewrite",
                 "llm_execution", "codex_app_server"):
        assert term in low, f"AC-5: runbook missing required term {term!r}"
    assert ("supersede" in low and "d7" in low), "missing the 'supersedes OSH-F64 §D7' note"
    for term in ("fleet-f62", "iso-f14", "cred-f28"):
        assert term in low, f"AC-5: missing the OneCLI-identity supersession note for {term!r}"
    # 18255 is permitted as a bare substring: 172.17.0.1:18255 is F63's base
    # data-plane egress hop (test_ac_osh_f63_6 requires it as a protocol:rest
    # endpoint; §D4 spec-gating keeps the bare case byte-unchanged). What AC-5
    # forbids is the SUPERSEDED §D7 inference chain-dial: the three terms below,
    # the OneCLI per-profile grant instructions, and — distinguished by SHAPE
    # not substring — any raw tls:skip CONNECT example of the 18255 hop (the
    # APF-opaque tunnel), while F63's protocol:rest 18255 example stays permitted.
    # (The inference-drops-18255 guarantee itself is AC-1's rendered-policy check
    # plus AC-4 provider selection and the AC-6/7 live turn, never a doc substring.)
    for term in ("onecli-chain", "socat", "loopback-relay"):
        assert term not in low, f"AC-5: obsolete §D7 chain-dial term {term!r} must be dropped"
    assert "onecli-onboard --profile" not in low, (
        "AC-5: obsolete OneCLI per-profile grant instructions remain"
    )
    for block in re.findall(r"```ya?ml[^\n]*\n(.*?)```", doc, flags=re.I | re.S):
        example = yaml.safe_load(block)
        if not isinstance(example, dict):
            continue
        endpoint_groups = [example.get("endpoints") or []]
        endpoint_groups.extend(
            network.get("endpoints") or []
            for network in (example.get("network_policies") or {}).values()
        )
        assert not any(
            _hostport(ep) == ONECLI_HOP and _is_raw(ep)
            for endpoints in endpoint_groups
            for ep in endpoints
            if isinstance(ep, dict)
        ), "AC-5: obsolete §D7 raw CONNECT (tls:skip 18255) example remains"


# --- builder proof obligations (not AC ids — extra coverage) ---

def test_osh_f64_b_forbidlist_guard(loaded, tmp_path):
    """OSH-F63's control-plane forbid-list still REJECTS a spec whose egress names a forbidden
    host — the OSH-F64.b render removes no guard (builder proof obligation; AC-1's
    absence-of-endpoint check does not by itself prove the guard still raises)."""
    bad_spec_dir = tmp_path / "badspec"
    shutil.copytree(SPEC.parent, bad_spec_dir)
    types_path = bad_spec_dir / "coworker-types.yaml"
    cfg = yaml.safe_load(types_path.read_text(encoding="utf-8"))
    cfg["egress"]["proxy_addr"] = "172.17.0.1:10256"  # the OneCLI control plane — forbidden
    types_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    with pytest.raises(loaded.module.CompositionError):
        loaded.module.compose(str(types_path), str(tmp_path / "out"))


def test_osh_f64_b_base_url_v1(loaded, tmp_path):
    """Each served profile's inference base_url carries the /v1 path, so the OpenAI SDK's
    {base_url}/chat/completions reaches the policy-allowed /v1/chat/completions (not /chat/completions)."""
    _, configs = _render(loaded, SPEC, tmp_path / "out")
    for role in SERVED_ROLES:
        prov = _inference_provider(configs.get(role) or {})
        assert prov is not None, f"{role}: no inference provider pointing at {INFERENCE_HOST}"
        path = urlsplit(str(prov.get("base_url", ""))).path.rstrip("/")
        assert path == "/v1", f"{role}: inference base_url must carry a /v1 path; got {prov.get('base_url')!r}"


def test_osh_f64_b_profile_header(loaded, tmp_path):
    """Each served profile's inference provider carries a distinct per-profile X-Hermes-Profile
    attribution header equal to the top-level attribution_tag — the live AC-6 attribution carrier
    (both profile children share the one gateway sandbox, so a per-request header, not per-sandbox
    identity, distinguishes them)."""
    _, configs = _render(loaded, SPEC, tmp_path / "out")
    headers = {}
    for role in SERVED_ROLES:
        cfg = configs.get(role) or {}
        prov = _inference_provider(cfg)
        assert prov is not None, f"{role}: no inference provider"
        hdr = (prov.get("extra_headers") or {}).get("X-Hermes-Profile")
        assert hdr, f"{role}: no X-Hermes-Profile attribution header rendered"
        assert hdr == cfg.get("attribution_tag"), (
            f"{role}: header {hdr!r} must equal top-level attribution_tag {cfg.get('attribution_tag')!r}"
        )
        headers[role] = hdr
    assert len(set(headers.values())) == len(headers), f"per-profile headers must be distinct: {headers}"


def _compose_bad_spec(loaded, tmp_path, name, mutate):
    """Copy the OSH-F64.b spec tree, apply ``mutate`` to its coworker-types.yaml egress, and
    assert ``compose`` fail-closes with ``CompositionError`` — the shape AC-1's absence check
    cannot by itself establish (that a guard still RAISES)."""
    bad_dir = tmp_path / name
    shutil.copytree(SPEC.parent, bad_dir)
    types_path = bad_dir / "coworker-types.yaml"
    cfg = yaml.safe_load(types_path.read_text(encoding="utf-8"))
    mutate(cfg)
    types_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    with pytest.raises(loaded.module.CompositionError):
        loaded.module.compose(str(types_path), str(tmp_path / f"{name}-out"))


def _import_installer():
    """Load the openshell installer directly (stdlib-only, no relative imports) to exercise its
    gateway-default edit — the real install-time authority, separate from the render."""
    import importlib.util
    path = PLUGIN_SRC / "openshell" / "installer.py"
    spec = importlib.util.spec_from_file_location("osh_f64_b_installer", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_osh_f64_b_rejects_raw_bypass(loaded, tmp_path):
    """A gated spec that would collapse the single-authority invariants is refused: inference
    must stay a distinct protocol:rest endpoint and the broker must be the only raw tls:skip hop
    (and never the dropped 18255 OneCLI hop)."""
    # inference_route == broker_addr → inference would render raw (the raw branch wins), losing
    # the four-path enforcement.
    _compose_bad_spec(loaded, tmp_path, "inf-eq-broker",
                      lambda c: c["egress"].update(broker_addr=c["egress"]["inference_route"]))
    # broker_addr == the dropped proxy_addr (the 18255 OneCLI hop) → the superseded raw tunnel
    # would reappear as the broker.
    _compose_bad_spec(loaded, tmp_path, "broker-eq-proxy",
                      lambda c: c["egress"].update(broker_addr=c["egress"]["proxy_addr"]))
    # base_url host != inference_route → the policy allows one host while the config dials another
    # (unreachable).
    _compose_bad_spec(loaded, tmp_path, "base-url-mismatch",
                      lambda c: c["egress"]["inference_provider"].update(base_url="https://evil.local/v1"))

    # The broker is the ONLY permitted raw tls:skip hop (172.17.0.1:18777). With proxy_addr OMITTED
    # the proxy==broker check cannot cover a broker set elsewhere, and the base host-guard forbids
    # neither 18255 nor an arbitrary 18256 — so the broker is pinned to 18777 (AC-OSH-F64.b-1: the
    # only raw hop is 18777). The dropped 18255 OneCLI hop and any other raw port are both rejected.
    def _drop_proxy_broker_18255(c):
        c["egress"].pop("proxy_addr", None)
        c["egress"]["broker_addr"] = "172.17.0.1:18255"
    _compose_bad_spec(loaded, tmp_path, "noproxy-broker-18255", _drop_proxy_broker_18255)

    def _drop_proxy_broker_18256(c):
        c["egress"].pop("proxy_addr", None)
        c["egress"]["broker_addr"] = "172.17.0.1:18256"
    _compose_bad_spec(loaded, tmp_path, "noproxy-broker-18256", _drop_proxy_broker_18256)

    # The inference route itself cannot be the dropped 18255 hop (inference is a distinct OpenShell
    # protocol:rest endpoint, never the superseded OneCLI tunnel); base_url is moved with it so the
    # base==route reachability check does not mask the hop guard.
    def _route_18255(c):
        c["egress"]["inference_route"] = "172.17.0.1:18255"
        c["egress"]["inference_provider"]["base_url"] = "https://172.17.0.1:18255/v1"
    _compose_bad_spec(loaded, tmp_path, "route-18255", _route_18255)

    # The docker-bridge alias host.docker.internal resolves to the 172.17.0.1 bridge IP (podman has
    # no mapping, so the render folds it), so a route spelled with the alias is the same dropped
    # 18255 hop and must be rejected the same way — the guard canonicalizes the alias, not just the
    # literal IP.
    def _route_alias_18255(c):
        c["egress"]["inference_route"] = "Host.Docker.Internal:18255"
        c["egress"]["inference_provider"]["base_url"] = "https://host.docker.internal:18255/v1"
    _compose_bad_spec(loaded, tmp_path, "route-alias-18255", _route_alias_18255)


def test_osh_f64_b_provider_catalog_cannot_reintroduce_inference_or_onecli(loaded, tmp_path):
    """A FLEET-F62.c type-opted provider must not re-add the dropped 18255 OneCLI hop or the
    inference route to a worker policy under the single-authority posture — inference is a
    gateway-only endpoint and 18255 is dropped, and the base host-guard forbids neither
    (AC-OSH-F64.b-1). compose must fail closed before any distribution is written."""
    def _opt_provider(c, endpoint):
        c.setdefault("providers", {})["extra-egress"] = {
            "endpoints": [endpoint],
            "binaries": ["/usr/bin/curl"],
            "methods": ["POST"],
        }
        c["types"]["architect"]["providers"] = ["extra-egress"]
    _compose_bad_spec(loaded, tmp_path, "prov-onecli-18255",
                      lambda c: _opt_provider(c, "172.17.0.1:18255"))
    _compose_bad_spec(loaded, tmp_path, "prov-inference-route",
                      lambda c: _opt_provider(c, c["egress"]["inference_route"]))
    # a trailing-root-dot FQDN is the same host — it must not slip the inference route past the guard
    _compose_bad_spec(loaded, tmp_path, "prov-inference-fqdn",
                      lambda c: _opt_provider(c, "inference.local.:443"))


def test_osh_f64_b_invalid_provider_contract(loaded, tmp_path):
    """The gated inference_provider block fail-closes on an api_mode outside the endpoint-backed,
    header-preserving allow-list — a header-stripping mode (which would silently drop the
    X-Hermes-Profile attribution header) OR codex_app_server (which runs Codex's own subprocess
    model/tool loop off the policy-allowed REST route) — a rewrite_placeholder other than the fixed
    routing trigger, or a base_url embedding credentials in userinfo."""
    _compose_bad_spec(loaded, tmp_path, "bad-api-mode",
                      lambda c: c["egress"]["inference_provider"].update(api_mode="anthropic_messages"))
    # codex_app_server is a valid Hermes api_mode (agent_init.py:723) but it bypasses the REST
    # endpoint and the attribution header, so the single-authority validator must reject it.
    _compose_bad_spec(loaded, tmp_path, "codex-app-server",
                      lambda c: c["egress"]["inference_provider"].update(api_mode="codex_app_server"))
    _compose_bad_spec(loaded, tmp_path, "bad-placeholder",
                      lambda c: c["egress"]["inference_provider"].update(rewrite_placeholder="sk-WRONG"))
    _compose_bad_spec(loaded, tmp_path, "userinfo-base-url",
                      lambda c: c["egress"]["inference_provider"].update(
                          base_url="https://user:pass@inference.local/v1"))


def test_osh_f64_b_inherited_provider_credential(loaded, tmp_path):
    """The render fully OWNS the inference provider block: an inherited credential-bearing field
    on the spine provider (an Authorization header, a key_env) is NOT carried into the rendered
    placeholder-only config — only the X-Hermes-Profile attribution header survives."""
    bad_dir = tmp_path / "inherited-cred"
    shutil.copytree(SPEC.parent, bad_dir)
    spine_path = bad_dir / "spines" / "base.yaml"
    spine = yaml.safe_load(spine_path.read_text(encoding="utf-8"))
    prov = spine["config"]["providers"]["compatible-endpoint"]
    prov["extra_headers"] = {"Authorization": "Bearer sk-INHERITED-REAL-SECRET"}
    prov["key_env"] = "SOME_REAL_KEY_ENV"
    spine_path.write_text(yaml.safe_dump(spine), encoding="utf-8")
    _, configs = _render(loaded, bad_dir / "coworker-types.yaml", tmp_path / "out")
    for role in SERVED_ROLES:
        cfg = configs.get(role) or {}
        prov = _inference_provider(cfg)
        assert prov is not None, f"{role}: no inference provider"
        headers = prov.get("extra_headers") or {}
        assert set(headers) == {"X-Hermes-Profile"}, (
            f"{role}: provider carries non-attribution headers (inherited leak): {headers}"
        )
        assert "key_env" not in prov, f"{role}: inherited key_env leaked into the provider"
        assert "sk-INHERITED-REAL-SECRET" not in yaml.safe_dump(cfg), (
            f"{role}: an inherited credential leaked into the rendered config"
        )


def test_osh_f64_b_inherited_model_and_custom_providers_dropped(loaded, tmp_path):
    """The gated render drops every inherited credential/route channel, not just the
    `providers.<name>` entry: inherited `model.*` auth fields, a competing same-URL `providers`
    entry, and a legacy same-URL `custom_providers` entry must not survive into any rendered
    config (served roles AND the default/gateway), and the release resolver's effective
    extra_headers for the inference base_url must be ONLY the per-profile X-Hermes-Profile tag.
    The resolver lists `custom_providers` before `providers` (config.py:1889-1897), so an
    inherited same-URL entry would otherwise win the base_url match and displace attribution with
    its own credential — the AC-6 attribution path."""
    bad_dir = tmp_path / "inherited-model-cp"
    shutil.copytree(SPEC.parent, bad_dir)
    spine_path = bad_dir / "spines" / "base.yaml"
    spine = yaml.safe_load(spine_path.read_text(encoding="utf-8"))
    cfg_block = spine["config"]
    cfg_block["model"].update({
        "api_key": "sk-INHERITED-MODEL-KEY",
        "api": "https://inference.local/v1",
        "base_url": "https://inference.local/v1",
        "api_base": "https://inherited-model-apibase.example/v1",
        "api_mode": "chat_completions",
        "key_env": "INHERITED_MODEL_KEY_ENV",
        "api_key_env": "INHERITED_MODEL_API_KEY_ENV",
        "default_headers": {"Authorization": "Bearer INHERITED_MODEL_HEADER"},
        "extra_headers": {"Authorization": "Bearer INHERITED_MODEL_EXTRA"},
    })
    # Root-level route keys the release promotes into the model at load (_normalize_root_model_keys).
    cfg_block["provider"] = "inherited-root-provider"
    cfg_block["base_url"] = "https://inherited-root-baseurl.example/v1"
    cfg_block["api_base"] = "https://inherited-root-apibase.example/v1"
    # A delegation block is a second provider route: _resolve_delegation_credentials returns an
    # inherited delegation.api_key as the subagent key, and request_overrides can add per-request headers.
    cfg_block["delegation"] = {
        "provider": "custom",
        "base_url": "https://inherited-delegation.example/v1",
        "api_key": "sk-INHERITED-DELEGATION-KEY",
        "api_mode": "chat_completions",
        "request_overrides": {"extra_headers": {"Authorization": "Bearer INHERITED_DELEGATION_HEADER"}},
        "model": "anthropic/claude-sonnet-4",
    }
    cfg_block["providers"]["rogue-same-url"] = {
        "base_url": "https://inference.local/v1",
        "api_mode": "chat_completions",
        "api_key": "sk-INHERITED-PROV2-KEY",
    }
    cfg_block["custom_providers"] = [{
        "name": "legacy-compat",
        "base_url": "https://inference.local/v1",
        "api_mode": "chat_completions",
        "api_key": "sk-INHERITED-CP-KEY",
        "extra_headers": {"Authorization": "Bearer INHERITED_LEGACY_HEADER"},
    }]
    # Inherited §D7 OneCLI chain-dial state a spine might carry: the gated render must scrub it
    # (podman-onecli is inert under single authority) so a gated fleet can't re-arm the tunnel.
    cfg_block["secrets"] = {"onecli": {"enabled": True, "override_existing": True}}
    cfg_block["plugins"]["enabled"] = [*cfg_block["plugins"]["enabled"], "podman-onecli"]
    cfg_block["plugins"].setdefault("entries", {})["podman-onecli"] = {
        "settings": {"proxy_rewrite": "127.0.0.1:18255", "ca_bundle": "/etc/osh-lane/ca.pem",
                     "profile_secret_sets": {"architect": ["Anthropic-Dev"]}},
    }
    # A fallback chain may name its own provider and carry an inline credential; an auxiliary task
    # pinned off `auto` calls a second provider with its own auth. The gated render must drop both
    # so the single-authority endpoint is the sole provider.
    cfg_block["fallback_model"] = [{
        "provider": "openrouter",
        "model": "anthropic/claude-sonnet-4",
        "api_key": "sk-INHERITED-FALLBACK-KEY",
        "base_url": "https://openrouter.ai/api/v1",
    }]
    cfg_block["fallback_providers"] = [{
        "provider": "openai",
        "model": "gpt-4o",
        "api_key": "sk-INHERITED-FBPROV-KEY",
    }]
    cfg_block["auxiliary"] = {
        "vision": {
            "provider": "openai",
            "base_url": "https://api.openai.com/v1",
            "api_mode": "chat_completions",
            "api_key": "sk-INHERITED-AUX-KEY",
            "key_env": "SOME_AUX_KEY_ENV",
            "model": "gpt-4o",
        },
    }
    spine_path.write_text(yaml.safe_dump(spine), encoding="utf-8")
    _, configs = _render(loaded, bad_dir / "coworker-types.yaml", tmp_path / "out")
    from hermes_cli.config import get_compatible_custom_providers
    inherited = ("sk-INHERITED-MODEL-KEY", "INHERITED_MODEL_HEADER", "INHERITED_MODEL_EXTRA",
                 "sk-INHERITED-PROV2-KEY", "sk-INHERITED-CP-KEY", "INHERITED_LEGACY_HEADER",
                 "sk-INHERITED-FALLBACK-KEY", "sk-INHERITED-FBPROV-KEY", "sk-INHERITED-AUX-KEY",
                 "SOME_AUX_KEY_ENV",
                 "INHERITED_MODEL_KEY_ENV", "INHERITED_MODEL_API_KEY_ENV",
                 "inherited-model-apibase.example", "inherited-root-provider",
                 "inherited-root-baseurl.example", "inherited-root-apibase.example",
                 "sk-INHERITED-DELEGATION-KEY", "INHERITED_DELEGATION_HEADER",
                 "inherited-delegation.example")
    for role in (*SERVED_ROLES, "default"):  # the default/gateway profile also renders inference
        cfg = configs.get(role) or {}
        dumped = yaml.safe_dump(cfg)
        for leaked in inherited:
            assert leaked not in dumped, f"{role}: inherited credential {leaked!r} leaked into rendered config"
        assert "custom_providers" not in cfg, f"{role}: legacy custom_providers survived the gated render"
        assert "rogue-same-url" not in (cfg.get("providers") or {}), (
            f"{role}: a competing same-URL providers entry survived the gated render"
        )
        model = cfg.get("model") or {}
        for field in ("api_key", "api", "api_base", "base_url", "api_mode",
                      "key_env", "api_key_env", "default_headers", "extra_headers"):
            assert field not in model, f"{role}: inherited model.{field} survived the gated render"
        for field in ("provider", "base_url", "api_base"):
            assert field not in cfg, f"{role}: inherited root {field} survived the gated render"
        delegation = cfg.get("delegation") or {}
        for field in ("provider", "base_url", "api_key", "api_mode", "request_overrides"):
            assert field not in delegation, f"{role}: inherited delegation.{field} survived the gated render"
        # the fallback chain (a second provider route) is dropped entirely
        assert "fallback_model" not in cfg, f"{role}: inherited fallback_model survived the gated render"
        assert "fallback_providers" not in cfg, f"{role}: inherited fallback_providers survived"
        # auxiliary tasks keep no off-route/auth field (provider → auto, model-name may stay)
        for task_name, task_cfg in (cfg.get("auxiliary") or {}).items():
            if not isinstance(task_cfg, dict):
                continue
            for field in ("provider", "base_url", "api_mode", "api_key", "key_env",
                          "api_key_env", "fallback_chain"):
                assert field not in task_cfg, (
                    f"{role}: auxiliary.{task_name}.{field} survived the gated render"
                )
        assert "onecli" not in (cfg.get("secrets") or {}), f"{role}: inherited secrets.onecli survived"
        assert "podman-onecli" not in ((cfg.get("plugins") or {}).get("enabled") or []), (
            f"{role}: podman-onecli still enabled under the single-authority posture"
        )
        assert "podman-onecli" not in ((cfg.get("plugins") or {}).get("entries") or {}), (
            f"{role}: inherited podman-onecli settings survived"
        )
        compat = get_compatible_custom_providers(cfg)
        matches = [e for e in compat
                   if str(e.get("base_url", "")).strip().rstrip("/").lower() == "https://inference.local/v1"]
        assert matches, f"{role}: no resolver entry for the inference base_url"
        for entry in matches:
            eh = entry.get("extra_headers") or {}
            assert set(eh) == {"X-Hermes-Profile"}, (
                f"{role}: resolver extra_headers for inference base_url must be only X-Hermes-Profile, got {eh}"
            )
        # The release delegation resolver must not surface an inherited subagent key. With neither
        # provider nor base_url on the scrubbed block, _resolve_delegation_credentials takes the
        # parent-inherit branch and returns api_key None — the child inherits the single-authority
        # route rather than the seeded delegation.api_key.
        from tools.delegate_tool import _resolve_delegation_credentials

        class _StubParent:  # the parent-inherit branch only reads getattr(parent, "request_overrides", None)
            pass

        resolved = _resolve_delegation_credentials(cfg.get("delegation") or {}, _StubParent())
        assert resolved.get("api_key") in (None, ""), (
            f"{role}: delegation resolver surfaced an inherited key {resolved.get('api_key')!r}"
        )
        assert resolved.get("api_key") != "sk-INHERITED-DELEGATION-KEY", (
            f"{role}: delegation resolver returned the seeded inherited delegation key"
        )


def test_osh_f64_b_minimal_gated_spec(loaded, tmp_path):
    """A gated spec renders without egress.proxy_addr (accepted-but-dropped under the single-
    authority posture) and without egress.binaries, and the gateway policy still permits the
    in-process model-call interpreter so the policy-allowed REST endpoint is reachable."""
    bad_dir = tmp_path / "minimal-gated"
    shutil.copytree(SPEC.parent, bad_dir)
    types_path = bad_dir / "coworker-types.yaml"
    cfg = yaml.safe_load(types_path.read_text(encoding="utf-8"))
    cfg["egress"].pop("proxy_addr", None)
    cfg["egress"].pop("binaries", None)
    types_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    policies, _ = _render(loaded, types_path, tmp_path / "out")
    gateway = _gateway_policy(policies)
    assert gateway is not None, "minimal gated spec must still render a gateway policy"
    assert "/opt/hermes/.venv/bin/python" in _binaries(gateway), (
        "the gateway policy must permit the model-call interpreter even when egress.binaries is omitted"
    )


def test_osh_f64_b_installer_suppresses_grants():
    """The openshell installer's gateway-default edit (_desired_default) must NOT write the §D7.1
    profile_secret_sets grant map under the single-authority posture (egress.inference_provider
    present) — else the render's suppression (AC-3) is undone at install time. The legacy path
    (no inference_provider) still populates it, proving the gate is the discriminator."""
    installer = _import_installer()
    gated = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    assert "inference_provider" in gated["egress"], "precondition: gated spec carries inference_provider"
    assert gated["egress"].get("onecli_secret_ids"), "precondition: onecli_secret_ids retained to prove suppression"
    settings = installer._desired_default({}, gated, [], {}).get("plugins.entries.podman-onecli.settings") or {}
    assert "profile_secret_sets" not in settings, (
        "OSH-F64.b (§D4): the installer must not write profile_secret_sets when egress.inference_provider is present"
    )
    legacy = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    legacy["egress"].pop("inference_provider")
    legacy_settings = installer._desired_default({}, legacy, [], {}).get("plugins.entries.podman-onecli.settings") or {}
    assert legacy_settings.get("profile_secret_sets"), (
        "control: without inference_provider the legacy §D7.1 grant map must still populate"
    )


def test_osh_f64_b_installer_gated_upgrade_deletes_inherited_onecli(monkeypatch, tmp_path):
    """A single-authority install of a home that was PREVIOUSLY on the legacy §D7 path must
    DELETE the inherited OneCLI state from the default config — not merely stop adding it. An
    upgraded home carries `podman-onecli` in plugins.enabled, a `secrets.onecli` block and a
    `plugins.entries.podman-onecli` settings entry; after the gated default-config diff is applied,
    none of those survive and the plugin is not re-enabled by the enabled-union. A second diff is
    empty (idempotence)."""
    installer = _import_installer()
    monkeypatch.setattr(installer, "_require_home", lambda: str(tmp_path))
    gated_spec = str(SPEC)
    # A home as the legacy OSH-F64 (non-gated) install would have left it.
    existing = {
        "plugins": {
            "enabled": ["nv-coworker-compose", "nv-fleet-gates", "podman-onecli", "operator-plugin"],
            "entries": {
                "podman-onecli": {"settings": {"proxy_rewrite": "127.0.0.1:18255",
                                               "ca_bundle": "/etc/osh-lane/ca.pem"}},
                "operator-plugin": {"settings": {"keep": "me"}},
            },
        },
        "secrets": {"onecli": {"enabled": True, "override_existing": True}},
        "operator_top_level": {"survives": True},
    }
    _, diff, backup_required = installer.default_config_diff(existing, gated_spec)
    assert backup_required, "a config-changing gated upgrade must take a backup"
    assert diff.get("plugins.entries.podman-onecli") is installer._DELETE, (
        "the gated upgrade diff must mark plugins.entries.podman-onecli for deletion"
    )
    assert diff.get("secrets.onecli") is installer._DELETE, (
        "the gated upgrade diff must mark secrets.onecli for deletion"
    )
    upgraded = installer.apply_config_diff(existing, diff)
    enabled = ((upgraded.get("plugins") or {}).get("enabled")) or []
    assert "podman-onecli" not in enabled, "podman-onecli must not survive the enabled-union on the gated path"
    assert "secrets" not in upgraded or "onecli" not in (upgraded.get("secrets") or {}), (
        "the inherited secrets.onecli block must be removed by the gated upgrade"
    )
    assert "podman-onecli" not in ((upgraded.get("plugins") or {}).get("entries") or {}), (
        "the inherited podman-onecli settings entry must be removed by the gated upgrade"
    )
    # Preservation: an unrelated operator plugin, entry and top-level key all survive.
    assert "operator-plugin" in enabled, "an unrelated operator-enabled plugin must survive"
    assert (upgraded["plugins"]["entries"].get("operator-plugin") or {}).get("settings") == {"keep": "me"}, (
        "an unrelated operator plugin entry must survive the gated upgrade"
    )
    assert upgraded.get("operator_top_level") == {"survives": True}, "an unrelated top-level key must survive"
    # Idempotence: re-diffing the upgraded home plans no further edit.
    _, second_diff, _ = installer.default_config_diff(upgraded, gated_spec, backup_present=True)
    assert second_diff == {}, f"a second gated diff must be empty (idempotence), got {second_diff}"
