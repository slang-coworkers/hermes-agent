"""OSH-F64.e direct-mode inference acceptance contracts (ADR §Acceptance criteria)."""
import re
import shutil
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_SRC = REPO_ROOT / "plugins" / "nv-coworker-compose"
DIRECT_SPEC = REPO_ROOT / "tests" / "e2e-scenarios" / "OSH-F64.e" / "spec" / "coworker-types.yaml"
MANAGED_SPEC = REPO_ROOT / "tests" / "e2e-scenarios" / "OSH-F64.b" / "spec" / "coworker-types.yaml"
RUNBOOK = REPO_ROOT / "website" / "docs" / "user-guide" / "fleet-openshell.md"

SERVED_ROLES = ("orchestrator", "architect", "builder", "tester", "reviewer")
MANAGED_HOST = "inference.local"
BROKER_HOPPORT = "172.17.0.1:18777"
FOUR_RULES = {
    ("POST", "/v1/chat/completions"),
    ("POST", "/v1/messages"),
    ("POST", "/v1/responses"),
    ("GET", "/v1/models"),
}
RESERVED_PROVIDERS = ("compatible-endpoint", "github")
GATEWAY_ONLY_FS = ("/opt/hermes", "/proc", "/dev/urandom", "/sandbox", "/dev/pts")


@pytest.fixture()
def loaded(tmp_path, monkeypatch):
    """Load nv-coworker-compose from an isolated HERMES_HOME via the real scanner."""
    hermes_home = tmp_path / "home"
    (hermes_home / "plugins").mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, hermes_home / "plugins" / "nv-coworker-compose")
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["nv-coworker-compose"]}}), encoding="utf-8")
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


def _managed_trigger():
    """The OSH-F64.b managed-route trigger, read from its shipped spec: a direct render must never
    carry it (it has no resolve marker, so the relay would forward it unresolved)."""
    data = yaml.safe_load(MANAGED_SPEC.read_text(encoding="utf-8"))
    return data["egress"]["inference_provider"]["rewrite_placeholder"]


def _direct_block():
    data = yaml.safe_load(DIRECT_SPEC.read_text(encoding="utf-8"))
    block = data["egress"]["inference_provider"]
    assert block.get("mode") == "direct", "precondition: the shipped OSH-F64.e spec is direct mode"
    return data, block


def _served(data):
    return [t for t in (data.get("types") or {}) if t != data.get("default_profile", "default")]


def _render(plugin, spec_path, out_dir):
    plugin.module.compose(str(spec_path), str(out_dir))
    policies, configs, texts = {}, {}, {}
    for p in Path(out_dir).rglob("*"):
        if p.is_file():
            texts[str(p.relative_to(out_dir))] = p.read_text(encoding="utf-8", errors="replace")
    for p in Path(out_dir).rglob("policy-*.yaml"):
        policies[p.stem] = yaml.safe_load(p.read_text(encoding="utf-8"))
    for c in Path(out_dir).rglob("config.yaml"):
        configs[c.parent.name] = yaml.safe_load(c.read_text(encoding="utf-8"))
    return policies, configs, texts


def _gateway_policy(policies):
    return policies.get("policy-gateway") or policies.get("policy-default")


def _endpoints(policy):
    out = []
    for net in (policy.get("network_policies") or {}).values():
        out.extend(net.get("endpoints") or [])
    return out


def _rules(ep):
    return {((r.get("allow") or {}).get("method"), (r.get("allow") or {}).get("path"))
            for r in ep.get("rules") or []}


def _upstream_hostport(block):
    parts = urlsplit(block["upstream_base_url"])
    return parts.hostname, parts.port or 443


def _glob_match(pattern, path):
    regex = "^" + re.escape(pattern).replace(r"\*\*", ".*").replace(r"\*", "[^/]*") + "$"
    return re.match(regex, path) is not None


def openshell_decides(policy, method, host, port, path):
    """APF-equivalent oracle for OpenShell v0.0.72: a request to the managed inference host is
    intercepted BEFORE the policy engine (proxy.rs:614) and is therefore ungoverned by the policy;
    any other host is denied unless an endpoint matches host:port, and a protocol:rest endpoint
    decides each request by its method+path rules (l7/relay.rs:860). Returns 'ungoverned' | 'allow' | 'deny'."""
    if host == MANAGED_HOST and port == 443:
        return "ungoverned"
    for ep in _endpoints(policy):
        if ep.get("host") == host and int(ep.get("port")) == port:
            if ep.get("tls") == "skip" or ep.get("protocol") != "rest":
                return "allow"
            ok = any(m == method and _glob_match(p, path) for m, p in _rules(ep))
            return "allow" if ok else "deny"
    return "deny"


def _compose_bad(loaded, tmp_path, name, mutate, source=DIRECT_SPEC):
    bad_dir = tmp_path / name
    shutil.copytree(source.parent, bad_dir)
    types_path = bad_dir / "coworker-types.yaml"
    cfg = yaml.safe_load(types_path.read_text(encoding="utf-8"))
    mutate(cfg)
    types_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    with pytest.raises(loaded.module.CompositionError):
        loaded.module.compose(str(types_path), str(tmp_path / f"{name}-out"))


def test_ac_osh_f64_e_1(loaded, tmp_path):
    """Under substrate: openshell with egress.inference_provider.mode: direct, the rendered gateway policy carries exactly one model-route endpoint, on the upstream_base_url host:port, as protocol: rest / enforcement: enforce with no tls key and EXACTLY the four OSH-F64.b method+paths; no rendered policy or config names inference.local; the broker 172.17.0.1:18777 stays the only tls: skip endpoint."""
    data, block = _direct_block()
    up_host, up_port = _upstream_hostport(block)
    policies, configs, texts = _render(loaded, DIRECT_SPEC, tmp_path / "out")
    gw = _gateway_policy(policies)
    assert gw is not None, "direct mode must render the gateway (model-call) policy"
    route = [ep for ep in _endpoints(gw) if ep.get("host") == up_host and int(ep.get("port")) == up_port]
    assert len(route) == 1, f"expected exactly one model-route endpoint on {up_host}:{up_port}, got {route}"
    ep = route[0]
    assert ep.get("protocol") == "rest" and ep.get("enforcement") == "enforce", ep
    assert "tls" not in ep, "the model route must be TLS-terminated (credential resolution + L7 rules)"
    assert _rules(ep) == FOUR_RULES, f"model-route rules must be exactly the four paths, got {_rules(ep)}"
    for name, text in texts.items():
        if Path(name).name == "config.yaml" or Path(name).name.startswith("policy-"):
            assert MANAGED_HOST not in text, f"{name}: direct render must not name {MANAGED_HOST}"
    for pname, policy in policies.items():
        raw = {f"{e.get('host')}:{e.get('port')}" for e in _endpoints(policy) if e.get("tls") == "skip"}
        assert raw == {BROKER_HOPPORT}, f"{pname}: the broker must be the one raw tls:skip hop, got {raw}"
        if policy is not gw:
            assert all(e.get("host") != up_host for e in _endpoints(policy)), (
                f"{pname}: a worker policy must not reach the model upstream")
    assert set(_served(data)) <= set(configs), "the render must write every served profile config"


def test_ac_osh_f64_e_2(loaded, tmp_path):
    """Under direct mode the rendered gateway policy DECIDES the path on the new endpoint: an OpenShell-decision oracle over the rendered YAML allows POST /v1/chat/completions, denies POST /v1/completions, POST /v1/embeddings and POST /api/show, and with POST /v1/chat/completions removed from the rendered rules denies that omitted path too; on the OSH-F64.b managed render the same omitted-path request is addressed to inference.local, which the oracle reports as ungoverned."""
    _, block = _direct_block()
    up_host, up_port = _upstream_hostport(block)
    policies, _, _ = _render(loaded, DIRECT_SPEC, tmp_path / "direct")
    gw = _gateway_policy(policies)
    assert openshell_decides(gw, "POST", up_host, up_port, "/v1/chat/completions") == "allow"
    for path in ("/v1/completions", "/v1/embeddings", "/api/show"):
        assert openshell_decides(gw, "POST", up_host, up_port, path) == "deny", path
    variant = yaml.safe_load(yaml.safe_dump(gw))
    for ep in _endpoints(variant):
        if ep.get("host") == up_host:
            ep["rules"] = [r for r in ep["rules"]
                           if (r["allow"]["method"], r["allow"]["path"]) != ("POST", "/v1/chat/completions")]
    assert openshell_decides(variant, "POST", up_host, up_port, "/v1/chat/completions") == "deny", (
        "omitting the path from the RENDER's policy must make APF deny it on the direct route")
    # Contrast (negative control): the OSH-F64.b managed render routes the model call to the
    # policy-bypassing managed host, where removing a rule cannot deny the path.
    m_policies, m_configs, _ = _render(loaded, MANAGED_SPEC, tmp_path / "managed")
    m_base = urlsplit(m_configs["architect"]["providers"][m_configs["architect"]["model"]["provider"]]["base_url"])
    assert openshell_decides(_gateway_policy(m_policies), "POST", m_base.hostname, m_base.port or 443,
                             "/v1/chat/completions") == "ungoverned"


def test_ac_osh_f64_e_3(loaded, tmp_path, monkeypatch):
    """Under direct mode each served profile's AND the gateway default profile's selected provider has base_url = upstream_base_url (/v1 path), api_mode chat_completions, and api_key = one shared opaque reference that ends in the credential_env key name and is not the bare name; the OSH-F64.b managed-route trigger appears in no rendered file; the release resolver returns that reference as the key and X-Hermes-Profile <prefix>-<profile> as the only extra header."""
    spec, block = _direct_block()
    key_name = block["credential_env"]
    trigger = _managed_trigger()
    _, configs, texts = _render(loaded, DIRECT_SPEC, tmp_path / "out")
    for name, text in texts.items():
        assert trigger not in text, f"{name}: the managed-route trigger must not reach a direct render"
    from hermes_cli import runtime_provider

    refs = set()
    for role in (spec["default_profile"], *_served(spec)):
        cfg = configs[role]
        sel = cfg["model"]["provider"]
        prov = cfg["providers"][sel]
        assert prov["base_url"] == block["upstream_base_url"], role
        assert urlsplit(prov["base_url"]).path.rstrip("/") == "/v1", role
        assert prov["api_mode"] == "chat_completions", role
        ref = prov["api_key"]
        assert isinstance(ref, str) and ref.endswith(key_name) and ref != key_name, (
            f"{role}: api_key must be OpenShell's opaque reference for the {key_name} key, not a value")
        assert not any(ch.isspace() for ch in ref), role
        refs.add(ref)
        monkeypatch.setattr(runtime_provider, "load_config", lambda cfg=cfg: cfg)
        resolved = runtime_provider._get_named_custom_provider(sel)
        assert resolved is not None, f"{role}: the release resolver must find providers.{sel}"
        assert resolved["api_key"] == ref, role
        assert resolved.get("extra_headers") == {
            "X-Hermes-Profile": f"{block['attribution_tag_prefix']}-{role}"}, role
    assert len(refs) == 1, "every profile carries the same single credential reference (one gateway credential)"


def _plan(plugin, spec_path, mutate=None):
    mod = plugin.module
    data = yaml.safe_load(Path(spec_path).read_text(encoding="utf-8"))
    if mutate is not None:
        mutate(data)
    return mod.build_provision_plan(data, mod._resolve_substrate(data)), data


def _opt_in(cfg, role, pname, endpoint="registry.example.org:443"):
    cfg.setdefault("providers", {})[pname] = {
        "endpoints": [endpoint], "binaries": ["/usr/bin/curl"], "methods": ["GET"]}
    cfg["types"][role].setdefault("providers", []).append(pname)


def _worker_provider_mentions(plan, gw_name):
    """Every provider name a WORKER is given anywhere in the plan: `--provider` on a worker create
    line, or a `sandbox provider attach <worker> <name>` line."""
    import shlex

    names = []
    for line in plan:
        argv = shlex.split(line)
        if argv[:3] == ["openshell", "sandbox", "create"] and argv[argv.index("--name") + 1] != gw_name:
            names += [argv[i + 1] for i, tok in enumerate(argv) if tok == "--provider"]
        elif argv[:4] == ["openshell", "sandbox", "provider", "attach"] and argv[4] != gw_name:
            names.append(argv[5])
    return names


def test_ac_osh_f64_e_4(loaded):
    """Under direct mode the provision dry-run plan carries exactly one gateway line, openshell sandbox create --name <fleet>-gw --from <gateway_image> --policy policy-gateway.yaml --provider <credential_provider>, and no worker create OR attach line names <credential_provider>; with mode absent the plan is unchanged from OSH-F64.b."""
    import shlex

    plan, data = _plan(loaded, DIRECT_SPEC)
    block = data["egress"]["inference_provider"]
    gw_name = f"{data['project']}-gw"
    creates = [shlex.split(line) for line in plan if line.startswith("openshell sandbox create ")]
    assert set(_served(data)) == {"orchestrator", "architect"}, "the live lane budget is 1 gateway + 2 workers"
    assert len(creates) == len(_served(data)) + 1 == 3, "one gateway create + one create per served worker"
    gw_lines = [argv for argv in creates if argv[argv.index("--name") + 1] == gw_name]
    assert len(gw_lines) == 1, f"expected one gateway create line for {gw_name}, got {gw_lines}"
    argv = gw_lines[0]
    assert argv[argv.index("--from") + 1] == block["gateway_image"]
    assert argv[argv.index("--policy") + 1] == "policy-gateway.yaml"
    providers = [argv[i + 1] for i, tok in enumerate(argv) if tok == "--provider"]
    assert providers == [block["credential_provider"]], providers
    assert block["credential_provider"] not in _worker_provider_mentions(plan, gw_name), (
        "no worker create or attach line may name the inference credential provider")
    opted, _ = _plan(loaded, DIRECT_SPEC, lambda c: _opt_in(c, "architect", "extra-egress"))
    mentions = _worker_provider_mentions(opted, gw_name)
    assert "extra-egress" in mentions, f"control: the worker's own opt-in must appear, got {opted}"
    assert block["credential_provider"] not in mentions, opted
    managed_plan, _ = _plan(loaded, MANAGED_SPEC)
    assert all(f"--name {data['project']}-gw " not in line for line in managed_plan), (
        "managed mode keeps the OSH-F64.b plan: no gateway create line")
    assert all("--provider" not in line for line in managed_plan), managed_plan


def test_ac_osh_f64_e_5(loaded, tmp_path):
    """(derived) mode is spec-gated and fail-closed: with mode absent the OSH-F64.b spec renders every file identically to the same spec with mode: managed; an unknown mode, a direct spec whose upstream_base_url or inference_route names inference.local, whose inference_route differs from the upstream host:port, whose credential_env is not an env-key name, whose credential_provider is a reserved lane provider, in which any worker type's resolved providers opt-in names credential_provider, which carries the managed-route rewrite_placeholder, or which lacks gateway_image, each raises CompositionError; the unknown-mode case is driven from the managed (OSH-F64.b) spec, so it raises only through the mode check."""
    def _render_variant(tag, mode):
        root = tmp_path / tag
        shutil.copytree(MANAGED_SPEC.parent, root / "spec")
        path = root / "spec" / "coworker-types.yaml"
        cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
        if mode is not None:
            cfg["egress"]["inference_provider"]["mode"] = mode
        path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        _, _, texts = _render(loaded, path, root / "out")
        return {k: v.replace(str(root), "<ROOT>") for k, v in texts.items()}

    implicit, explicit = _render_variant("implicit", None), _render_variant("explicit", "managed")
    assert implicit and implicit == explicit, "an absent mode must render exactly as mode: managed (OSH-F64.b)"

    ip = lambda c: c["egress"]["inference_provider"]  # noqa: E731
    # From the MANAGED spec: it renders on base, so only a real mode check makes this raise.
    _compose_bad(loaded, tmp_path, "unknown-mode", lambda c: ip(c).update(mode="bypass"),
                 source=MANAGED_SPEC)
    _compose_bad(loaded, tmp_path, "upstream-managed-host",
                 lambda c: (ip(c).update(upstream_base_url="https://Inference.Local./v1",
                                         base_url="https://Inference.Local./v1"),
                            c["egress"].update(inference_route="inference.local:443")))
    _compose_bad(loaded, tmp_path, "route-managed-host",
                 lambda c: c["egress"].update(inference_route="inference.local:443"))
    _compose_bad(loaded, tmp_path, "route-mismatch",
                 lambda c: c["egress"].update(inference_route="other-upstream.example:443"))
    _compose_bad(loaded, tmp_path, "bad-env-name", lambda c: ip(c).update(credential_env="NOT-A KEY"))
    _compose_bad(loaded, tmp_path, "reserved-env-prefix",
                 lambda c: ip(c).update(credential_env="OPENSHELL_SANDBOX_POLICY"))
    for reserved in RESERVED_PROVIDERS:
        _compose_bad(loaded, tmp_path, f"reserved-{reserved}",
                     lambda c, r=reserved: ip(c).update(credential_provider=r))
    _compose_bad(loaded, tmp_path, "worker-opts-into-credential-provider",
                 lambda c: _opt_in(c, "architect", ip(c)["credential_provider"]))
    assert not list((tmp_path / "worker-opts-into-credential-provider-out").rglob("config.yaml")), (
        "the opt-in guard must fail closed before any distribution is written")
    with pytest.raises(loaded.module.CompositionError):
        _plan(loaded, DIRECT_SPEC, lambda c: _opt_in(c, "architect", ip(c)["credential_provider"]))
    _compose_bad(loaded, tmp_path, "managed-trigger-in-direct",
                 lambda c: ip(c).update(rewrite_placeholder=_managed_trigger()))
    _compose_bad(loaded, tmp_path, "no-gateway-image", lambda c: ip(c).pop("gateway_image"))


def _import_installer():
    import importlib.util

    path = PLUGIN_SRC / "openshell" / "installer.py"
    spec = importlib.util.spec_from_file_location("osh_f64_e_installer", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_ac_osh_f64_e_6(loaded, tmp_path, monkeypatch):
    """(derived, fold-ins) the gated render leaves no empty auxiliary.<task> mapping (N3); delegation.request_overrides keeps its non-route keys while its route/credential/header keys are dropped (N6); the gateway-only filesystem grants appear in no worker policy (S7); the installer's _sandbox_exists runs openshell sandbox list with stdin closed (N5); and fleet-openshell.md no longer says the Phase A set is "not posture-derived" (S6)."""
    import subprocess

    seeded = tmp_path / "seeded"
    shutil.copytree(MANAGED_SPEC.parent, seeded)
    spine_path = seeded / "spines" / "base.yaml"
    spine = yaml.safe_load(spine_path.read_text(encoding="utf-8"))
    spine["config"]["auxiliary"] = {"vision": {"provider": "inherited-aux", "base_url": "https://aux.example/v1"},
                                    "compression": {"provider": "inherited-aux", "model": "kept-model"}}
    spine["config"]["delegation"] = {"model": "kept-delegate-model",
                                     "request_overrides": {"extra_body": {"kept": True,
                                                                          "provider": {"sort": "throughput"}},
                                                           "speed": "fast", "api_key": None,
                                                           "base_url": "https://override.example/v1",
                                                           "extra_headers": {"X-Inherited": "1"}}}
    spine_path.write_text(yaml.safe_dump(spine), encoding="utf-8")
    policies, configs, _ = _render(loaded, seeded / "coworker-types.yaml", tmp_path / "out")
    for role in SERVED_ROLES:
        aux = configs[role].get("auxiliary") or {}
        assert all(v for v in aux.values()), f"{role}: empty auxiliary task mapping survived (N3): {aux}"
        assert aux.get("compression", {}).get("model") == "kept-model", role
        ro = (configs[role].get("delegation") or {}).get("request_overrides") or {}
        assert ro.get("extra_body") == {"kept": True} and ro.get("speed") == "fast", (
            f"{role}: N6 keeps non-route overrides and drops the extra_body.provider routing hint: {ro}")
        for key in ("api_key", "base_url", "extra_headers", "default_headers", "headers", "provider", "api_mode"):
            assert key not in ro, f"{role}: request_overrides.{key} must be dropped (N6)"
    gw = _gateway_policy(policies)
    for pname, policy in policies.items():
        if policy is gw:
            continue
        fs = policy.get("filesystem_policy") or {}
        granted = set(fs.get("read_only") or []) | set(fs.get("read_write") or [])
        leaked = granted & set(GATEWAY_ONLY_FS)
        assert not leaked, f"{pname}: gateway-only filesystem grants leaked to a worker (S7): {leaked}"

    installer = _import_installer()
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        return subprocess.CompletedProcess(argv, 0, stdout="NAME CREATED PHASE\nosh-f64b-architect x Ready\n",
                                           stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert installer._sandbox_exists("osh-f64b-architect") is True
    assert seen["argv"] == ["openshell", "sandbox", "list"]
    assert seen["kwargs"].get("stdin") is subprocess.DEVNULL, "N5: _sandbox_exists must close stdin"

    runbook = RUNBOOK.read_text(encoding="utf-8")
    assert "not posture-derived" not in runbook, "S6: the Phase A plugin set IS posture-derived"
