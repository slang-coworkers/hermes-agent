"""Acceptance test for OSH-F63 — OpenShell-native sandbox substrate (P7).

One ``test_ac_osh_f63_<n>`` per ``pytest:`` acceptance criterion (1, 2, 3, 4,
6). AC-OSH-F63-5 is a ``sandbox:`` scenario proven by
tests/e2e-scenarios/OSH-F63/AC-OSH-F63-5.md, not a function here.

Behaviour contracts, not byte snapshots (AGENTS.md forbids change-detector
tests). No network I/O: for the ready/unready readiness cases the oracle
``ensure_task_env`` is monkeypatched, and for the one causal config-absent
case the REAL oracle is left in place and proven to return ``None`` — on
missing ssh_host/ssh_user ``_create_environment`` raises ValueError at its ssh
guard (terminal_tool.py:2122-2124) BEFORE any SSHEnvironment is constructed,
so no ssh subprocess and no connection ever run; ``ensure_task_env`` catches
it and returns None (terminal_tool.py:2336-2341). HERMES_HOME is
tmp_path-rooted. Shapes mirror
tests/plugins/test_fleet_f62_acceptance.py and
tests/plugins/test_nv_fleet_gates_acceptance.py. The openshell spec/fixtures
ship in the same PR under tests/e2e-scenarios/OSH-F63/.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGINS_SRC = REPO_ROOT / "plugins"
SCENARIO_DIR = REPO_ROOT / "tests" / "e2e-scenarios" / "OSH-F63"
OPENSHELL_SPEC = SCENARIO_DIR / "spec" / "openshell" / "coworker-types.yaml"
PODMAN_SPEC = (
    REPO_ROOT / "tests" / "e2e-scenarios" / "FLEET-F62" / "spec" / "podman" / "coworker-types.yaml"
)
DOC_PAGE = REPO_ROOT / "website" / "docs" / "user-guide" / "fleet-openshell.md"

# Both fleet plugins load so AC-3 exercises the production veto end to end
# (nv-coworker-compose render + nv-fleet-gates predicates).
FLEET_PLUGINS = ("nv-coworker-compose", "nv-fleet-gates")
COWORKER_PROFILES = ("orchestrator", "architect", "builder", "tester", "reviewer")


def _bootstrap_home(tmp_path: Path) -> Path:
    """An isolated HERMES_HOME with the fleet plugins enabled so ``hermes
    coworker compose`` can run. Mirrors test_plugin_api_compat.py: copy each
    plugin dir into <home>/plugins/<name>, enable them in config.yaml; the
    empty bundled dir + HERMES_ENABLE_PROJECT_PLUGINS=0 are set by _render so
    only these plugins load.
    """
    home = tmp_path / "hermes-home"
    (home / "plugins").mkdir(parents=True, exist_ok=True)
    for name in FLEET_PLUGINS:
        shutil.copytree(PLUGINS_SRC / name, home / "plugins" / name)
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": list(FLEET_PLUGINS)}}),
        encoding="utf-8",
    )
    return home


def _render(spec: Path, out: Path, home: Path, *extra_args: str) -> subprocess.CompletedProcess:
    """Run ``hermes coworker compose <spec> --out <out> [extra]`` under the
    isolated home with an empty bundled-plugins dir (so only the copied
    plugins load) and project plugins disabled."""
    empty_bundled = home.parent / "empty-bundled"
    empty_bundled.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update(
        HOME=str(home.parent),  # isolate from the real home, as the FLEET-F62 harness does
        HERMES_HOME=str(home),
        HERMES_BUNDLED_PLUGINS=str(empty_bundled),
        HERMES_ENABLE_PROJECT_PLUGINS="0",
    )
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "coworker", "compose", str(spec), "--out", str(out), *extra_args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )


def _all_profile_dirs(out: Path):
    """Every rendered profile dir (has a config.yaml), including default."""
    return sorted(p.parent for p in out.glob("*/config.yaml"))


def _profile_config(out: Path, profile: str) -> dict:
    matches = list(out.glob(f"*{profile}*/config.yaml"))
    assert matches, f"no rendered config for profile {profile!r} under {out}"
    return yaml.safe_load(matches[0].read_text(encoding="utf-8"))


def _dotted(d: dict, path: str):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _pre_tool_call_hooks(manager) -> list:
    if hasattr(manager, "list_registered_hooks"):
        return list(manager.list_registered_hooks("pre_tool_call"))
    return list(getattr(manager, "_hooks", {}).get("pre_tool_call", []))


def _is_block(manager, tool_name: str, args: dict) -> bool:
    results = manager.invoke_hook(
        "pre_tool_call",
        tool_name=tool_name,
        args=args,
        task_id="t",
        session_id="s",
        tool_call_id="c",
        turn_id="u",
        api_request_id="r",
        middleware_trace=None,
    )
    for r in results if isinstance(results, list) else [results]:
        if isinstance(r, dict) and r.get("action") == "block":
            return True
    return False


def _load_veto(home: Path):
    """Load the fleet veto from a rendered coworker home (import model_tools
    to trigger plugin discovery, then a fresh PluginManager)."""
    import model_tools

    from hermes_cli.plugins import PluginManager

    model_tools.discover_builtin_tools()
    manager = PluginManager()
    manager.discover_and_load()
    return manager


def _rendered_veto_home(out: Path, profile: str, tmp_path: Path, monkeypatch, label: str) -> Path:
    """Install a RENDERED coworker distribution into a loadable home and point
    the env at it, so AC-3 loads the veto from the actual rendered config
    (real render-derived expected_backend, enforce_sandbox, mcp_scope,
    profile_roles) — the criterion's "HERMES_HOME = a rendered coworker home".
    Copies the rendered profile config.yaml + the plugin code + the managed
    fragment (the FLEET-F62 _install_fleet / _load_profile_plugins shape).
    """
    src = next(iter(out.glob(f"*{profile}*/")))
    # home basename must equal the rendered profile name: the veto's
    # _current_profile() derives identity from the home basename to select the
    # rendered mcp_scope / profile_roles entry.
    home = tmp_path / label / src.name
    (home / "plugins").mkdir(parents=True, exist_ok=True)
    for name in FLEET_PLUGINS:
        shutil.copytree(PLUGINS_SRC / name, home / "plugins" / name)
    shutil.copy(src / "config.yaml", home / "config.yaml")
    managed = next(iter(out.glob("**/managed*/config.yaml")), None)
    if managed is not None:
        (home / "managed").mkdir(exist_ok=True)
        shutil.copy(managed, home / "managed" / "config.yaml")
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(home / "managed"))
    empty_bundled = tmp_path / f"empty-bundled-{label}"
    empty_bundled.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(empty_bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    return home


def _block_message(manager, tool_name: str, args: dict):
    """First block directive's message, or None."""
    results = manager.invoke_hook(
        "pre_tool_call",
        tool_name=tool_name,
        args=args,
        task_id="t",
        session_id="s",
        tool_call_id="c",
        turn_id="u",
        api_request_id="r",
        middleware_trace=None,
    )
    for r in results if isinstance(results, list) else [results]:
        if isinstance(r, dict) and r.get("action") == "block":
            return r.get("message") or ""
    return None


# --- criteria --------------------------------------------------------------


def test_ac_osh_f63_1(tmp_path):
    """AC-OSH-F63-1: substrate=openshell renders per-profile terminal.backend
    ssh with distinct ssh_host/ssh_key paths and no docker_* keys; podman
    still renders docker (regression)."""
    home = _bootstrap_home(tmp_path)
    out = tmp_path / "out-openshell"
    proc = _render(OPENSHELL_SPEC, out, home)
    assert proc.returncode == 0, proc.stderr

    hosts, keys, key_parents = set(), set(), set()
    for profile in COWORKER_PROFILES:
        cfg = _profile_config(out, profile)
        assert _dotted(cfg, "terminal.backend") == "ssh", profile
        # the render-derived expected_backend is what the veto's backend-name check reads to admit ssh
        assert _dotted(cfg, "plugins.entries.nv-fleet-gates.settings.expected_backend") == "ssh", profile
        host = _dotted(cfg, "terminal.ssh_host")
        key = str(_dotted(cfg, "terminal.ssh_key") or "")
        assert host, f"{profile}: no ssh_host"
        assert _dotted(cfg, "terminal.ssh_user"), f"{profile}: no ssh_user"
        assert _dotted(cfg, "terminal.ssh_port"), f"{profile}: no ssh_port"
        assert key, f"{profile}: no ssh_key"
        # ssh_key is a PATH (never key material), absolute, co-located under one
        # gateway-home keys dir (render contract, ADR D1.2): distinct leaf per
        # profile, shared parent = the gateway $HERMES_HOME keys dir.
        assert "BEGIN" not in key and "PRIVATE KEY" not in key, f"{profile}: key material in config"
        assert Path(key).is_absolute(), f"{profile}: ssh_key not an absolute path: {key!r}"
        # contained under the gateway $HERMES_HOME (render-time get_hermes_home()
        # == the render's HERMES_HOME == `home`): a leaked path outside it fails.
        assert Path(key).resolve().is_relative_to(home.resolve()), f"{profile}: ssh_key {key!r} not under gateway HERMES_HOME {home}"
        key_parents.add(str(Path(key).parent))
        term = _dotted(cfg, "terminal") or {}
        assert not any(k.startswith("docker_") for k in term), f"{profile}: docker_* present under ssh"
        hosts.add(host)
        keys.add(key)
    assert len(hosts) == len(COWORKER_PROFILES), f"ssh_host not distinct per profile: {hosts}"
    assert len(keys) == len(COWORKER_PROFILES), f"ssh_key not distinct per profile: {keys}"
    assert len(key_parents) == 1, f"ssh_key paths not co-located under one gateway keys dir: {key_parents}"

    # local appears in NO profile's terminal.backend (all profiles incl default).
    for prof_dir in _all_profile_dirs(out):
        cfg = yaml.safe_load((prof_dir / "config.yaml").read_text(encoding="utf-8"))
        assert _dotted(cfg, "terminal.backend") != "local", f"{prof_dir.name}: backend is local"

    # Regression: podman (the FLEET-F62 spec) still renders docker — with the
    # substrate key EXPLICIT and OMITTED (default must be podman).
    pod_out = tmp_path / "out-podman"
    pod = _render(PODMAN_SPEC, pod_out, home)
    assert pod.returncode == 0, pod.stderr
    pod_cfg = _profile_config(pod_out, "builder")
    assert _dotted(pod_cfg, "terminal.backend") == "docker"
    pterm = _dotted(pod_cfg, "terminal") or {}
    assert any(k.startswith("docker_") for k in pterm), "podman regression: docker_* keys gone"

    # substrate OMITTED -> default podman render (copy the spec dir so relative
    # spine paths still resolve, strip the top-level `substrate` key).
    nosub_dir = tmp_path / "podman-nosub"
    shutil.copytree(PODMAN_SPEC.parent, nosub_dir)
    nosub_spec = nosub_dir / PODMAN_SPEC.name
    data = yaml.safe_load(nosub_spec.read_text(encoding="utf-8"))
    data.pop("substrate", None)
    nosub_spec.write_text(yaml.safe_dump(data), encoding="utf-8")
    nosub_out = tmp_path / "out-podman-nosub"
    assert _render(nosub_spec, nosub_out, home).returncode == 0
    assert _dotted(_profile_config(nosub_out, "builder"), "terminal.backend") == "docker", "default substrate is not podman"

    # sandbox_scope: session is RECOGNISED but fail-closed in OSH-F63 (the
    # session provider is deferred, ADR §D1.5) — the render must refuse it and
    # write no distribution, not silently mis-render as profile.
    sess_dir = tmp_path / "openshell-session"
    shutil.copytree(OPENSHELL_SPEC.parent, sess_dir)
    sess_spec = sess_dir / OPENSHELL_SPEC.name
    sdata = yaml.safe_load(sess_spec.read_text(encoding="utf-8"))
    sdata["sandbox_scope"] = "session"
    sess_spec.write_text(yaml.safe_dump(sdata), encoding="utf-8")
    sess_out = tmp_path / "out-session"
    sess = _render(sess_spec, sess_out, home)
    assert sess.returncode != 0, "sandbox_scope: session must fail closed, not render"
    refusal = (sess.stdout + sess.stderr).lower()
    # recognized-but-deferred, not a generic "unknown value": name the provider path.
    assert "session" in refusal and "defer" in refusal and "provider" in refusal, f"session refusal must name the deferred provider path, got: {refusal!r}"
    assert not list(sess_out.glob("*/config.yaml")), "no distribution may be written on the session refusal"


# OpenShell's `sandbox create --policy` rejects any top-level key outside this
# set (`unknown field <k>, expected one of ...`), so a generated policy that is
# not exactly this five-section shape is rejected before any runtime enforcement.
_GRAMMAR_TOP_KEYS = {"version", "filesystem_policy", "landlock", "process", "network_policies"}
_WILDCARD_HOSTS = {"0.0.0.0/0", "::/0", "*", "0.0.0.0", "::"}


def _hostport(value) -> tuple:
    """Normalise a spec egress target to ``(host, port)`` with port a string.
    Accepts a ``{host, port}`` mapping or a scalar ``host:port`` / bare ``host``
    (port ``None`` only for a bare host — the grammar requires a port on every
    endpoint, so a target reaching the render without one is a fixture defect
    the caller surfaces)."""
    if isinstance(value, dict):
        port = value.get("port")
        return str(value.get("host")), (str(port) if port is not None else None)
    text = str(value)
    head, sep, tail = text.rpartition(":")
    if sep and tail.isdigit():
        return head, tail
    return text, None


def _assert_grammar_shape(profile: str, policy: dict) -> list:
    """Validate the required documented grammar structure (§D1.3) and return
    the raw list of endpoint records — NOT de-duplicated, so a duplicated
    endpoint stays visible to the caller's count check. Every named policy is
    ``{name, binaries:[{path}], endpoints:[{host, port, protocol, enforcement,
    rules:[{allow:{method, path}}]}]}``."""
    fsp = policy.get("filesystem_policy")
    assert isinstance(fsp, dict), f"{profile}: filesystem_policy must be a mapping"
    assert fsp.get("include_workdir") is True, f"{profile}: filesystem_policy.include_workdir must be true, got {fsp.get('include_workdir')!r}"
    assert "read_only" in fsp and isinstance(fsp["read_only"], list), f"{profile}: filesystem_policy.read_only must be a present list"
    assert "read_write" in fsp and isinstance(fsp["read_write"], list), f"{profile}: filesystem_policy.read_write must be a present list"
    landlock = policy.get("landlock")
    assert isinstance(landlock, dict) and landlock.get("compatibility"), f"{profile}: landlock.compatibility missing"
    proc = policy.get("process")
    assert isinstance(proc, dict), f"{profile}: process must be a mapping"
    assert proc.get("run_as_user") == "sandbox", f"{profile}: process.run_as_user must be 'sandbox', got {proc.get('run_as_user')!r}"
    assert proc.get("run_as_group") == "sandbox", f"{profile}: process.run_as_group must be 'sandbox', got {proc.get('run_as_group')!r}"

    nps = policy.get("network_policies")
    assert isinstance(nps, dict) and nps, f"{profile}: network_policies must be a non-empty mapping"
    endpoints = []
    for name, np in nps.items():
        assert isinstance(np, dict), f"{profile}: network_policy {name!r} must be a mapping"
        assert np.get("name") == name, f"{profile}: network_policy {name!r} name field {np.get('name')!r} != map key"
        bins = np.get("binaries")
        assert isinstance(bins, list) and bins, f"{profile}: network_policy {name!r} needs a non-empty binaries[]"
        for b in bins:
            assert isinstance(b, dict) and b.get("path"), f"{profile}: {name} binary {b!r} needs a path"
        eps = np.get("endpoints")
        assert isinstance(eps, list) and eps, f"{profile}: network_policy {name!r} needs a non-empty endpoints[]"
        for ep in eps:
            assert isinstance(ep, dict), f"{profile}: {name}: each endpoint must be a mapping"
            assert ep.get("protocol") == "rest", f"{profile}: {name}: endpoint {ep!r} protocol must be 'rest'"
            assert ep.get("enforcement") == "enforce", f"{profile}: {name}: endpoint {ep!r} enforcement must be 'enforce'"
            assert ep.get("host"), f"{profile}: {name}: endpoint missing host"
            assert ep.get("port") is not None, f"{profile}: {name}: endpoint {ep.get('host')!r} missing port"
            rules = ep.get("rules")
            assert isinstance(rules, list) and rules, f"{profile}: {name}: endpoint {ep.get('host')} needs a non-empty rules[]"
            for rule in rules:
                allow = rule.get("allow") if isinstance(rule, dict) else None
                assert isinstance(allow, dict) and allow.get("method") and allow.get("path"), f"{profile}: {name}: rule {rule!r} needs allow.method + allow.path"
            endpoints.append(ep)
    return endpoints


def test_ac_osh_f63_2(tmp_path):
    """AC-OSH-F63-2: the render emits one policy-<profile>.yaml per profile
    conforming to the required OpenShell policy grammar (version/filesystem_policy/
    landlock/process/network_policies, no top-level egress/allow), whose
    network_policies endpoints are exactly the three allowed targets (OneCLI
    hop, inference route, ssh control path) — each protocol: rest, enforcement:
    enforce, with an allow rule — and 172.17.0.1:10256 and wildcard hosts
    absent."""
    home = _bootstrap_home(tmp_path)
    out = tmp_path / "out"
    assert _render(OPENSHELL_SPEC, out, home).returncode == 0

    # The three allowed endpoints are spec data (egress section), read back so
    # the assertion is a behaviour contract, not a hard-coded snapshot. Two of
    # them MAY share a host on different ports — the contract is three host:port
    # records, not three distinct hosts.
    spec = yaml.safe_load(OPENSHELL_SPEC.read_text(encoding="utf-8"))
    egress = spec.get("egress", {})
    required = [
        egress.get("proxy_addr") or egress.get("onecli_hop"),
        egress.get("inference_route"),
        egress.get("ssh_control_path"),
    ]
    required = [r for r in required if r]
    assert len(required) == 3, f"spec egress must name exactly three endpoints: {required}"
    required_hp = {_hostport(r) for r in required}
    assert len(required_hp) == 3, f"the three egress targets must be distinct host:port records: {required_hp}"
    for host, port in required_hp:
        assert port is not None, f"egress target {host!r} must name a port (grammar requires host+port): {required}"

    # exactly one policy file per profile, named policy-<profile>.yaml — a
    # duplicate in another dir must be caught, not silently collapsed.
    policy_paths = list(out.glob("**/policy-*.yaml"))
    assert len(policy_paths) == len(COWORKER_PROFILES), f"expected {len(COWORKER_PROFILES)} policy files, got {[p.name for p in policy_paths]}"
    assert {p.name for p in policy_paths} == {f"policy-{p}.yaml" for p in COWORKER_PROFILES}, f"policy files != one per profile: {sorted(p.name for p in policy_paths)}"
    policy_files = {p.name: p for p in policy_paths}

    # AC-2 is a STRUCTURAL contract on the landed OpenShell grammar (§D1.3): the
    # full five-section shape (every named policy well-formed), and the endpoints
    # == exactly the three allowed targets, each enforce/rest with an allow rule.
    # The allow-vs-deny SEMANTICS (only these three admitted, all else denied) are
    # proven on-box by AC-OSH-F63-5's `openshell sandbox create --policy`/`prove`.
    for profile in COWORKER_PROFILES:
        policy = yaml.safe_load(policy_files[f"policy-{profile}.yaml"].read_text(encoding="utf-8"))
        assert isinstance(policy, dict), f"{profile}: policy is not a YAML mapping"
        assert policy.get("version") == 1, f"{profile}: policy version must be 1, got {policy.get('version')!r}"
        assert "egress" not in policy, f"{profile}: placeholder top-level 'egress' key present (openshell create --policy rejects it)"
        assert "allow" not in policy, f"{profile}: placeholder top-level 'allow' key present"
        assert set(policy) == _GRAMMAR_TOP_KEYS, f"{profile}: top-level keys {sorted(policy)} != grammar {sorted(_GRAMMAR_TOP_KEYS)}"

        endpoints = _assert_grammar_shape(profile, policy)
        # raw list (NOT a set) so a duplicated endpoint fails the count.
        assert len(endpoints) == 3, f"{profile}: expected exactly 3 endpoint records, got {len(endpoints)}: {endpoints}"
        emitted_hp = {(str(ep.get("host")), str(ep.get("port"))) for ep in endpoints}
        assert emitted_hp == required_hp, f"{profile}: endpoints {sorted(emitted_hp)} != allowed targets {sorted(required_hp)}"

        assert ("172.17.0.1", "10256") not in emitted_hp, f"{profile}: OneCLI control plane 172.17.0.1:10256 present as an endpoint"
        emitted_hosts = {h for h, _ in emitted_hp}
        assert not (emitted_hosts & _WILDCARD_HOSTS), f"{profile}: wildcard host present: {emitted_hosts & _WILDCARD_HOSTS}"


def _scope(manager, monkeypatch, active: Path, launch: Path):
    """Simulate a multiplex turn's profile identity: patch the veto module's
    active/launch home resolvers (mirrors test_fleet_f62_acceptance.py's
    active!=launch setup). active==launch => own profile; differ => cross."""
    vmod = manager._plugins["nv-fleet-gates"].module
    monkeypatch.setattr(vmod, "get_hermes_home", lambda *a, **k: active, raising=False)
    monkeypatch.setattr(vmod, "get_process_hermes_home", lambda *a, **k: launch, raising=False)


def _managed_expected_ssh_host(veto_home: Path) -> dict:
    """The ``expected_ssh_host`` map the render pins into the MANAGED fragment
    (worker-unforgeable, mirroring ``profile_roles`` — read straight from
    managed scope, never ctx.get_config). Read from the copied managed config so
    the test proves the map exists and is populated per profile."""
    mpath = veto_home / "managed" / "config.yaml"
    if not mpath.exists():
        return {}
    data = yaml.safe_load(mpath.read_text(encoding="utf-8")) or {}
    settings = _dotted(data, "plugins.entries.nv-fleet-gates.settings") or {}
    m = settings.get("expected_ssh_host")
    return m if isinstance(m, dict) else {}


class _SSHConstructReached(BaseException):
    """A BaseException (not Exception) sentinel: ensure_task_env's except
    Exception (terminal_tool.py:2336) would SWALLOW an Exception and mask the
    failure, so the sentinel must escape that boundary to fail the test."""


def test_ac_osh_f63_3(tmp_path, monkeypatch):
    """AC-OSH-F63-3: the single pre_tool_call veto binds the ssh terminal to the
    profile's OWN sandbox host — admitting the own-host call and blocking a
    sibling host, an absent host, an absent/malformed managed map, a
    dead-but-configured backend, an unready container, mcp_*, local, and
    cross-profile — with host binding ordered BEFORE the readiness gate."""
    import tools.terminal_tool as tt

    home = _bootstrap_home(tmp_path)
    out = tmp_path / "out-openshell"
    assert _render(OPENSHELL_SPEC, out, home).returncode == 0

    own_host = _dotted(_profile_config(out, "builder"), "terminal.ssh_host")
    sibling_host = _dotted(_profile_config(out, "tester"), "terminal.ssh_host")
    assert own_host and sibling_host and own_host != sibling_host, (
        f"render must give distinct per-profile hosts: own={own_host!r} sibling={sibling_host!r}"
    )

    veto_home = _rendered_veto_home(out, "builder", tmp_path, monkeypatch, "osh-veto")
    manager = _load_veto(veto_home)
    assert len(_pre_tool_call_hooks(manager)) == 1, "the fleet must register a single veto hook"

    # The render pins expected_ssh_host into the MANAGED fragment
    # (worker-unforgeable) for EVERY coworker profile, each to its OWN rendered
    # host — the value the veto's host-binding predicate compares against.
    managed = _managed_expected_ssh_host(veto_home)
    for prof in COWORKER_PROFILES:
        pdir = next(iter(out.glob(f"*{prof}*/")))
        phost = _dotted(yaml.safe_load((pdir / "config.yaml").read_text(encoding="utf-8")), "terminal.ssh_host")
        assert managed.get(pdir.name) == phost, (
            f"managed expected_ssh_host[{pdir.name}] must be {phost!r}, got {managed!r}"
        )
    assert managed.get(veto_home.name) == own_host

    _scope(manager, monkeypatch, active=veto_home, launch=veto_home)
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_SSH_USER", "sandbox")

    real_ensure = tt.ensure_task_env  # for the causal config-absent case below

    # A call-counting readiness oracle: readiness stays SUCCESSFUL for these
    # cases so the host predicate is the sole decider, and the counter proves the
    # ORDERING — readiness must not run for a sibling/absent host (else a
    # reachable sibling would be connected + cached before the veto blocks).
    ready_calls = {"n": 0}

    def _ready_spy(*a, **k):
        ready_calls["n"] += 1
        return object()

    monkeypatch.setattr(tt, "ensure_task_env", _ready_spy)

    monkeypatch.setenv("TERMINAL_SSH_HOST", own_host)
    ready_calls["n"] = 0
    assert not _is_block(manager, "terminal", {"command": "hostname"}), "own-host ssh must be admitted"
    assert ready_calls["n"] >= 1, "readiness must run once the host matches"

    # a stdio mcp server is scoped stdio by the render; _sandbox_block admits
    # (host ok, ready) so the block comes from _mcp_scope_block and cites stdio.
    msg = _block_message(manager, "mcp__somestdioserver__tool", {})
    assert msg is not None, "stdio mcp tool must be blocked"
    assert "stdio" in msg.lower(), f"mcp block not the stdio-transport branch: {msg!r}"

    # sibling host (a REAL sibling's rendered host): blocked, and readiness must
    # NOT be reached (the host predicate gates before any connect/cache).
    monkeypatch.setenv("TERMINAL_SSH_HOST", sibling_host)
    ready_calls["n"] = 0
    msg = _block_message(manager, "terminal", {"command": "hostname"})
    assert msg is not None and "host" in msg.lower(), f"sibling-host must block on the host mismatch: {msg!r}"
    assert ready_calls["n"] == 0, "readiness ran for a sibling host — a reachable sibling would be connected+cached"

    monkeypatch.delenv("TERMINAL_SSH_HOST", raising=False)
    ready_calls["n"] = 0
    msg = _block_message(manager, "terminal", {"command": "hostname"})
    assert msg is not None and "host" in msg.lower(), f"absent-host must block on the missing host: {msg!r}"
    assert ready_calls["n"] == 0, "readiness ran for an absent host"

    monkeypatch.setenv("TERMINAL_ENV", "local")
    assert _is_block(manager, "terminal", {"command": "hostname"}), "local must be blocked"

    # dead-but-configured: own host set (host predicate passes), oracle reports
    # not-ready -> block. Isolates the oracle from the host predicate.
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_SSH_HOST", own_host)
    monkeypatch.setattr(tt, "ensure_task_env", lambda *a, **k: None)
    assert _is_block(manager, "terminal", {"command": "hostname"}), (
        "dead-but-configured ssh (oracle None) must be blocked"
    )

    monkeypatch.setattr(tt, "ensure_task_env", lambda *a, **k: object())
    _scope(manager, monkeypatch, active=(tmp_path / "other-profile"), launch=veto_home)
    assert _is_block(manager, "terminal", {"command": "hostname"}), "cross-profile call must be refused"

    # Use a rendered podman profile to exercise the non-SSH readiness path.
    pod_out = tmp_path / "out-podman"
    assert _render(PODMAN_SPEC, pod_out, home).returncode == 0
    pod_home = _rendered_veto_home(pod_out, "builder", tmp_path, monkeypatch, "pod-veto")
    dmanager = _load_veto(pod_home)
    _scope(dmanager, monkeypatch, active=pod_home, launch=pod_home)
    monkeypatch.setattr(tt, "ensure_task_env", lambda *a, **k: None)
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    assert _is_block(dmanager, "terminal", {"command": "hostname"}), "unready container must stay blocked"

    # expected_ssh_host is read LIVE inside the predicate, not captured at
    # register(): mutate the ALREADY-LOADED manager's managed file, invalidate the
    # cache, and the verdict must flip to block — a capture-at-register impl would
    # keep admitting. A missing or non-dict map fails CLOSED (block) with the hook
    # still registered, and readiness is never reached (the map check precedes it).
    from hermes_cli import managed_scope

    mpath = veto_home / "managed" / "config.yaml"
    good_managed = mpath.read_text(encoding="utf-8")
    _scope(manager, monkeypatch, active=veto_home, launch=veto_home)
    # the container case above repointed HERMES_HOME/HERMES_MANAGED_DIR at the
    # podman home; restore the osh-veto home so managed_scope reads the fragment
    # we mutate below (else these cases read the podman managed map — which pins
    # no expected_ssh_host — and pass without proving the live read).
    monkeypatch.setenv("HERMES_HOME", str(veto_home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(veto_home / "managed"))
    managed_scope.invalidate_managed_cache()
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_SSH_HOST", own_host)
    monkeypatch.setattr(tt, "ensure_task_env", _ready_spy)
    for corrupt in (
        lambda s: s.pop("expected_ssh_host", None),                  # missing map
        lambda s: s.__setitem__("expected_ssh_host", "not-a-dict"),  # malformed map
    ):
        data = yaml.safe_load(mpath.read_text(encoding="utf-8"))
        corrupt(_dotted(data, "plugins.entries.nv-fleet-gates.settings"))
        mpath.write_text(yaml.safe_dump(data), encoding="utf-8")
        managed_scope.invalidate_managed_cache()
        ready_calls["n"] = 0
        assert _is_block(manager, "terminal", {"command": "hostname"}), "own-host must block under a corrupt managed map (live read, fail-closed)"
        assert len(_pre_tool_call_hooks(manager)) == 1, "veto must stay registered under a corrupt managed map (not fail-open)"
        assert ready_calls["n"] == 0, "readiness ran despite a corrupt managed map — the map check must precede it"
        mpath.write_text(good_managed, encoding="utf-8")
        managed_scope.invalidate_managed_cache()

    # a worker cannot forge a host for a profile ABSENT from the managed map by
    # writing expected_ssh_host into its LOCAL config.yaml. An unmanaged profile
    # name ("rogue") is what discriminates a ctx.get_config deep-merged read
    # (which would surface the local entry and admit the sibling) from the
    # required managed-only read (the managed map has no rogue entry, so the call
    # is refused). A managed profile would not discriminate: the managed entry
    # wins the merge regardless of which read the veto uses.
    lconf = yaml.safe_load((veto_home / "config.yaml").read_text(encoding="utf-8")) or {}
    lconf.setdefault("plugins", {}).setdefault("entries", {}).setdefault(
        "nv-fleet-gates", {}
    ).setdefault("settings", {})["expected_ssh_host"] = {"rogue": sibling_host}
    (veto_home / "config.yaml").write_text(yaml.safe_dump(lconf), encoding="utf-8")
    managed_scope.invalidate_managed_cache()
    lmanager = _load_veto(veto_home)
    rogue_home = veto_home.parent / "rogue"  # basename -> _current_profile() == "rogue"
    _scope(lmanager, monkeypatch, active=rogue_home, launch=rogue_home)
    monkeypatch.setattr(tt, "ensure_task_env", _ready_spy)
    monkeypatch.setenv("TERMINAL_SSH_HOST", sibling_host)
    ready_calls["n"] = 0
    assert _is_block(lmanager, "terminal", {"command": "hostname"}), "a worker-local expected_ssh_host for an unmanaged profile must not authorize a sibling host (managed map governs)"
    assert ready_calls["n"] == 0, "readiness ran despite the host not matching the managed map"

    # causal config-absent: the REAL oracle returns None on missing config. A
    # minimal home whose config.yaml carries only terminal.backend: ssh (no
    # ssh_host/ssh_user) so the one-shot config->env bridge cannot restore them;
    # _create_environment then raises at its ssh guard (terminal_tool.py:2122-2124)
    # BEFORE constructing SSHEnvironment, caught -> None (2336-2341), zero network.
    # The _SSHEnvironment sentinel (BaseException) makes reaching the constructor
    # fail the test, so a poisoned config cannot yield a false pass.
    min_home = tmp_path / "ssh-only-home"
    min_home.mkdir()
    (min_home / "config.yaml").write_text(
        yaml.safe_dump({"terminal": {"backend": "ssh"}}), encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(min_home))
    monkeypatch.delenv("TERMINAL_SSH_HOST", raising=False)
    monkeypatch.delenv("TERMINAL_SSH_USER", raising=False)
    monkeypatch.setattr(tt, "_terminal_config_bridge_attempted", False, raising=False)
    monkeypatch.setattr(tt, "_active_environments", {}, raising=False)

    def _no_construct(*a, **k):
        raise _SSHConstructReached("SSHEnvironment constructed on config-absent path")

    monkeypatch.setattr(tt, "_SSHEnvironment", _no_construct, raising=False)
    assert real_ensure() is None, "real ensure_task_env must return None on missing ssh config (no connect)"


def test_ac_osh_f63_4(tmp_path):
    """AC-OSH-F63-4: --provision-dry-run prints, deterministically, a plan where
    each profile's create/policy/ssh-config/teardown commands are JOINED by the
    same sandbox name + policy file and setup precedes teardown — so a plan that
    creates one name, configures another, and points Hermes at a third cannot
    pass. (substrate is the spec-file key; --provision-dry-run is the only flag.)"""
    home = _bootstrap_home(tmp_path)
    r1 = _render(OPENSHELL_SPEC, tmp_path / "o1", home, "--provision-dry-run")
    r2 = _render(OPENSHELL_SPEC, tmp_path / "o2", home, "--provision-dry-run")
    assert r1.returncode == 0 and r2.returncode == 0, (r1.stderr, r2.stderr)
    assert r1.stdout == r2.stdout, "provision dry-run not deterministic across runs"

    tokens = [shlex.split(ln) for ln in r1.stdout.splitlines() if ln.strip()]

    def _arg(toks, flag):
        if flag in toks and toks.index(flag) + 1 < len(toks):
            return toks[toks.index(flag) + 1]
        return None

    def _find(subcmd, needle):
        hits = [(i, t) for i, t in enumerate(tokens) if t[: len(subcmd)] == subcmd and needle in t]
        assert len(hits) == 1, f"{' '.join(subcmd)} referencing {needle!r}: expected exactly one line, got {[h[1] for h in hits]}"
        return hits[0]

    # no nested container engine anywhere in the plan (the P7 "no nested podman").
    for t in tokens:
        assert t and t[0] == "openshell", f"provision plan runs a non-openshell command: {t}"

    for profile in COWORKER_PROFILES:
        policy = f"policy-{profile}.yaml"
        ci, create = _find(["openshell", "sandbox", "create"], policy)
        name = _arg(create, "--name")
        # the create --name must equal the profile's RENDERED terminal.ssh_host
        # (the sandbox alias Hermes ssh's to) — so a plan cannot create one name
        # while the render points Hermes at another host containing the profile.
        rendered_host = _dotted(_profile_config(tmp_path / "o1", profile), "terminal.ssh_host")
        assert name and name == rendered_host, f"{profile}: create --name {name!r} != rendered ssh_host {rendered_host!r}"
        assert "--from" in create, f"{profile}: create missing --from: {create}"
        # ssh-config and delete must target the SAME sandbox name the create used.
        si, _ = _find(["openshell", "sandbox", "ssh-config"], name)
        di, _ = _find(["openshell", "sandbox", "delete"], name)
        psi, _ = _find(["openshell", "policy", "set"], policy)
        pdi, _ = _find(["openshell", "policy", "delete"], policy)
        assert max(ci, si, psi) < min(di, pdi), (
            f"{profile}: a teardown command precedes setup (create={ci} ssh-config={si} policy-set={psi}; sandbox-delete={di} policy-delete={pdi})"
        )

    # exactly one of each command per profile.
    for subcmd in (
        ["openshell", "sandbox", "create"], ["openshell", "policy", "set"],
        ["openshell", "sandbox", "ssh-config"], ["openshell", "sandbox", "delete"],
        ["openshell", "policy", "delete"],
    ):
        cnt = sum(1 for t in tokens if t[: len(subcmd)] == subcmd)
        assert cnt == len(COWORKER_PROFILES), f"{' '.join(subcmd)}: expected {len(COWORKER_PROFILES)} lines, got {cnt}"


def test_ac_osh_f63_6():
    """AC-OSH-F63-6: website/docs/user-guide/fleet-openshell.md exists and
    covers architecture, why-not-nested-podman, both ssh routes + chosen,
    route-(a) admissibility, prerequisites, provisioning plan, teardown."""
    assert DOC_PAGE.exists(), f"{DOC_PAGE} missing"
    text = DOC_PAGE.read_text(encoding="utf-8").lower()
    required = [
        "sandbox per profile",   # architecture
        "nested podman",         # why-not
        "route (a)",             # both ssh routes named explicitly
        "route (b)",
        "chosen route",          # WHICH route is chosen (and why)
        "admissib",              # route-(a) admissibility rule
        "sibling",               # sibling-sandbox denial is the isolation gate
        "openshell_endpoint",    # the gateway endpoint the policy must deny to workers
        "operator gateway credentials",  # named subject of the prohibition
        "worker sandbox",
        "must never",            # the gateway-admin credential prohibition, explicitly
        "prerequisite",          # operator prerequisites
        "provision",             # provisioning plan
        "teardown",              # teardown
        "sandbox_scope",         # the scope contract
        "session",               # the deferred session scope + its provider home
        "default",               # the documented default (profile)
        "defer",                 # session is deferred / fail-closed
    ]
    missing = [k for k in required if k not in text]
    assert not missing, f"fleet-openshell.md missing required topics: {missing}"


# --- Fail-closed regressions (not AC ids) ---------------------------
def test_openshell_rejects_container_mount_fields(tmp_path):
    """Unit (not an AC id): the openshell substrate fails CLOSED — not a silent
    no-op — when a spec sets a container-only mount field the remote-ssh substrate
    cannot honour (workspace_root / shared_learnings_root / install_surfaces)."""
    home = _bootstrap_home(tmp_path)
    for field, value in (
        ("workspace_root", "/data/osh-fleet"),
        ("shared_learnings_root", "/opt/osh-shared-learnings"),
        ("install_surfaces", ["/opt/osh-tools"]),
    ):
        spec_dir = tmp_path / f"openshell-{field}"
        shutil.copytree(OPENSHELL_SPEC.parent, spec_dir)
        spec = spec_dir / OPENSHELL_SPEC.name
        data = yaml.safe_load(spec.read_text(encoding="utf-8"))
        data[field] = value
        spec.write_text(yaml.safe_dump(data), encoding="utf-8")
        out = tmp_path / f"out-{field}"
        proc = _render(spec, out, home)
        assert proc.returncode != 0, f"{field}: openshell must reject a container-mount field, not ignore it"
        msg = (proc.stdout + proc.stderr).lower()
        assert field in msg and "openshell" in msg, f"{field}: refusal must name the field + substrate, got: {msg!r}"
        assert not list(out.glob("*/config.yaml")), f"{field}: no distribution may be written on the refusal"


def test_openshell_rejects_docker_terminal_keys(tmp_path):
    """Unit (not an AC id): the openshell substrate refuses an inherited/spec
    terminal.docker_* key — the remote-ssh backend has no local container, so such a
    key can never be honoured; failing closed keeps the no-docker_* render invariant
    true even for a hostile spec (rather than silently serializing a dead key)."""
    home = _bootstrap_home(tmp_path)
    spec_dir = tmp_path / "openshell-dockerkey"
    shutil.copytree(OPENSHELL_SPEC.parent, spec_dir)
    spec = spec_dir / OPENSHELL_SPEC.name
    data = yaml.safe_load(spec.read_text(encoding="utf-8"))
    data.setdefault("default_config", {})["terminal"] = {"docker_image": "leaked/image:tag"}
    spec.write_text(yaml.safe_dump(data), encoding="utf-8")
    out = tmp_path / "out-dockerkey"
    proc = _render(spec, out, home)
    assert proc.returncode != 0, "openshell must reject an inherited terminal.docker_* key"
    msg = (proc.stdout + proc.stderr).lower()
    assert "docker_" in msg and "openshell" in msg, f"refusal must name the docker key + substrate, got: {msg!r}"
    assert not list(out.glob("*/config.yaml")), "no distribution may be written on the docker-key refusal"


def test_openshell_egress_rejects_control_char_image(tmp_path):
    """Unit (not an AC id): a control character in the spec's egress.sandbox_image is
    refused — it is interpolated into the provisioning plan, so a newline could inject
    an extra (container-engine) command line."""
    home = _bootstrap_home(tmp_path)
    spec_dir = tmp_path / "openshell-badimage"
    shutil.copytree(OPENSHELL_SPEC.parent, spec_dir)
    spec = spec_dir / OPENSHELL_SPEC.name
    data = yaml.safe_load(spec.read_text(encoding="utf-8"))
    data["egress"]["sandbox_image"] = "safe-image\npodman run attacker/image"
    spec.write_text(yaml.safe_dump(data), encoding="utf-8")
    out = tmp_path / "out-badimage"
    proc = _render(spec, out, home)
    assert proc.returncode != 0, "a control-char sandbox_image must be rejected"
    assert "control character" in (proc.stdout + proc.stderr).lower()
    assert not list(out.glob("*/config.yaml")), "no distribution may be written on the bad-image refusal"
