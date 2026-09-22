"""Acceptance test for OSH-F63 — OpenShell-native sandbox substrate (P7).

One ``test_ac_osh_f63_<n>`` per ``pytest:`` acceptance criterion (1, 2, 3, 4,
6). AC-OSH-F63-5 is a ``sandbox:`` scenario proven by
tests/e2e-scenarios/OSH-F63/AC-OSH-F63-5.md, not a function here.

Behaviour contracts, not byte snapshots (AGENTS.md forbids change-detector
tests). No network I/O; the veto's readiness oracle ``ensure_task_env`` is
monkeypatched so the production gate is exercised unchanged. HERMES_HOME is
tmp_path-rooted. Shapes mirror tests/plugins/test_fleet_f62_acceptance.py and
tests/plugins/test_nv_fleet_gates_acceptance.py. The openshell spec/fixtures
ship in the same PR under tests/e2e-scenarios/OSH-F63/.
"""

from __future__ import annotations

import os
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

# nv-fleet-gates is loaded (not edited) so AC-3 exercises the production veto.
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


def _extract_allowed_hosts(policy: dict):
    """The allow-listed host:port entries from a parsed openshell policy.

    OSH-F63 render contract (this ADR defines it): the policy carries an
    ``egress.allow`` list of ``"host:port"`` strings. This is the schema the
    render emits and the tester parses.
    """
    egress = (policy or {}).get("egress") or {}
    return [str(x) for x in (egress.get("allow") or [])]


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
        # the render-derived expected_backend is what makes the (unchanged) veto admit ssh
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


def test_ac_osh_f63_2(tmp_path):
    """AC-OSH-F63-2: the render emits one policy-<profile>.yaml per profile
    whose allowed egress set is exactly the OneCLI hop + inference route + ssh
    control path, and never 10256 or a wildcard."""
    home = _bootstrap_home(tmp_path)
    out = tmp_path / "out"
    assert _render(OPENSHELL_SPEC, out, home).returncode == 0

    # The three allowed endpoints are spec data (egress section), read back so
    # the assertion is a behaviour contract, not a hard-coded snapshot.
    spec = yaml.safe_load(OPENSHELL_SPEC.read_text(encoding="utf-8"))
    egress = spec.get("egress", {})
    expected_allowed = {
        egress.get("proxy_addr") or egress.get("onecli_hop"),
        egress.get("inference_route"),
        egress.get("ssh_control_path"),
    }
    expected_allowed.discard(None)
    assert len(expected_allowed) == 3, f"spec egress must name exactly three endpoints: {expected_allowed}"

    # exactly one policy file per profile, named policy-<profile>.yaml — a
    # duplicate in another dir must be caught, not silently collapsed.
    policy_paths = list(out.glob("**/policy-*.yaml"))
    assert len(policy_paths) == len(COWORKER_PROFILES), f"expected {len(COWORKER_PROFILES)} policy files, got {[p.name for p in policy_paths]}"
    assert {p.name for p in policy_paths} == {f"policy-{p}.yaml" for p in COWORKER_PROFILES}, f"policy files != one per profile: {sorted(p.name for p in policy_paths)}"
    policy_files = {p.name: p for p in policy_paths}

    for profile in COWORKER_PROFILES:
        raw = policy_files[f"policy-{profile}.yaml"].read_text(encoding="utf-8")
        allowed = set(_extract_allowed_hosts(yaml.safe_load(raw)))
        assert allowed == expected_allowed, f"{profile}: allowed {allowed} != {expected_allowed}"
        assert not any("*" in a for a in allowed), f"{profile}: wildcard in allowed set {allowed}"
        assert "10256" not in raw, f"{profile}: OneCLI control plane 10256 present in policy"


def _scope(manager, monkeypatch, active: Path, launch: Path):
    """Simulate a multiplex turn's profile identity: patch the veto module's
    active/launch home resolvers (mirrors test_fleet_f62_acceptance.py's
    active!=launch setup). active==launch => own profile; differ => cross."""
    vmod = manager._plugins["nv-fleet-gates"].module
    monkeypatch.setattr(vmod, "get_hermes_home", lambda *a, **k: active, raising=False)
    monkeypatch.setattr(vmod, "get_process_hermes_home", lambda *a, **k: launch, raising=False)


def test_ac_osh_f63_3(tmp_path, monkeypatch):
    """AC-OSH-F63-3: the single pre_tool_call veto admits an own-profile ssh
    terminal call and still blocks mcp_*, local, cross-profile, an unready
    container backend, and an unready/config-absent ssh backend (isolation
    preserved; the veto itself is unchanged)."""
    import tools.terminal_tool as tt

    # Load the veto from an actual RENDERED openshell coworker home (real
    # render-derived expected_backend=ssh, enforce_sandbox, mcp_scope).
    home = _bootstrap_home(tmp_path)
    out = tmp_path / "out-openshell"
    assert _render(OPENSHELL_SPEC, out, home).returncode == 0
    veto_home = _rendered_veto_home(out, "builder", tmp_path, monkeypatch, "osh-veto")
    manager = _load_veto(veto_home)

    assert len(_pre_tool_call_hooks(manager)) == 1, "the fleet must register a single veto hook"

    # own profile (active == launch); readiness oracle ready (mocked, no net).
    _scope(manager, monkeypatch, active=veto_home, launch=veto_home)
    monkeypatch.setattr(tt, "ensure_task_env", lambda *a, **k: object())

    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_SSH_HOST", "fleet-builder")
    monkeypatch.setenv("TERMINAL_SSH_USER", "sandbox")
    assert not _is_block(manager, "terminal", {"command": "hostname"}), "own ssh call must be admitted"

    # the builder fixture declares a stdio mcp server, so the render scopes it
    # stdio: the block must cite the stdio-transport branch, not "not allow-listed".
    msg = _block_message(manager, "mcp__somestdioserver__tool", {})
    assert msg is not None, "stdio mcp tool must be blocked"
    assert "stdio" in msg.lower(), f"mcp block not the stdio-transport branch: {msg!r}"

    monkeypatch.setenv("TERMINAL_ENV", "local")
    assert _is_block(manager, "terminal", {"command": "hostname"}), "local must be blocked"

    # BLOCK (negative control b): resolved ssh with config ABSENT — unset
    # host/user so this specifically covers the config-absent path, and the
    # (unchanged) readiness gate reports not-ready.
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.delenv("TERMINAL_SSH_HOST", raising=False)
    monkeypatch.delenv("TERMINAL_SSH_USER", raising=False)
    monkeypatch.setattr(tt, "ensure_task_env", lambda *a, **k: None)
    assert _is_block(manager, "terminal", {"command": "hostname"}), "config-absent ssh must not fall through"
    monkeypatch.setattr(tt, "ensure_task_env", lambda *a, **k: object())
    monkeypatch.setenv("TERMINAL_SSH_HOST", "fleet-builder")
    monkeypatch.setenv("TERMINAL_SSH_USER", "sandbox")

    _scope(manager, monkeypatch, active=(tmp_path / "other-profile"), launch=veto_home)
    assert _is_block(manager, "terminal", {"command": "hostname"}), "cross-profile call must be refused"

    # BLOCK (negative control a): a CONTAINER backend with liveness unready —
    # the unchanged container path, proven on a RENDERED podman coworker home
    # (real expected_backend=docker).
    pod_out = tmp_path / "out-podman"
    assert _render(PODMAN_SPEC, pod_out, home).returncode == 0
    pod_home = _rendered_veto_home(pod_out, "builder", tmp_path, monkeypatch, "pod-veto")
    dmanager = _load_veto(pod_home)
    _scope(dmanager, monkeypatch, active=pod_home, launch=pod_home)
    monkeypatch.setattr(tt, "ensure_task_env", lambda *a, **k: None)
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    assert _is_block(dmanager, "terminal", {"command": "hostname"}), "unready container must stay blocked"


def test_ac_osh_f63_4(tmp_path):
    """AC-OSH-F63-4: --provision-dry-run prints the create/policy/ssh-config/
    teardown lines for all five profiles, deterministically across two runs.
    (substrate is the spec-file key; --provision-dry-run is the only flag.)"""
    home = _bootstrap_home(tmp_path)
    r1 = _render(OPENSHELL_SPEC, tmp_path / "o1", home, "--provision-dry-run")
    r2 = _render(OPENSHELL_SPEC, tmp_path / "o2", home, "--provision-dry-run")
    assert r1.returncode == 0 and r2.returncode == 0, (r1.stderr, r2.stderr)
    assert r1.stdout == r2.stdout, "provision dry-run not deterministic across runs"

    lines = r1.stdout.splitlines()
    n = len(COWORKER_PROFILES)

    def _one_line_per_profile(cmd: str) -> list:
        hits_all = [ln for ln in lines if cmd in ln]
        assert len(hits_all) == n, f"{cmd!r}: expected exactly {n} lines, got {len(hits_all)}"
        for profile in COWORKER_PROFILES:
            per = [ln for ln in hits_all if profile in ln]
            assert len(per) == 1, f"{cmd!r}: expected exactly one line for {profile}, got {per}"
        return hits_all

    creates = _one_line_per_profile("openshell sandbox create")
    _one_line_per_profile("openshell policy set")
    _one_line_per_profile("openshell sandbox ssh-config")
    _one_line_per_profile("openshell sandbox delete")
    _one_line_per_profile("openshell policy delete")

    for ln in creates:
        assert "--from" in ln and "--policy" in ln, f"create line missing --from/--policy: {ln}"
    # the "no nested podman" requirement: the plan runs no container engine.
    for ln in lines:
        assert not ln.strip().startswith(("podman", "docker")), f"nested container command in plan: {ln}"


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
