"""Acceptance test for FLEET-F62 — Fleet assembly (P6): five sandboxed profiles on ONE gateway.

One ``test_ac_fleet_f62_<n>`` per ``pytest:`` acceptance criterion (AC-FLEET-F62-1..4). The
``sandbox:``/``live:``/``ui:``/``desktop:`` ids (5-10) and the four carried ids
(AC-CRED-F28-2, AC-ISO-F14-1/-2/-5) are proven by the ``tests/e2e-scenarios/FLEET-F62/`` files /
the Playwright spec, not here. ``test_podman_onecli_optional_api_key_bridge_base`` ships the
base-URL-guard regression proof (its permanent home is tests/plugins/test_podman_onecli_acceptance.py).

Isolation follows tests/hermes_cli/test_plugin_api_compat.py: an EMPTY HERMES_BUNDLED_PLUGINS dir; the
composed plugin code is copied into the relevant home's plugins/ for discovery, but enablement comes
from the RENDER (config.yaml plugins.enabled), asserted rather than injected, so a render that fails to
enable a plugin is caught. Behaviour contracts, not change-detector snapshots: no model lists, no
version literals, no enumeration counts, no reading of source files, no network, nothing under
~/.hermes. AC-FLEET-F62-1 asserts the rendered fleet's STRUCTURE and required keys (a behaviour contract
on the render), not a whole-tree byte snapshot.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_PLUGINS = REPO_ROOT / "plugins"
COMPOSED = ("nv-coworker-compose", "nv-fleet-gates", "podman-onecli")
SCEN_DIR = REPO_ROOT / "tests" / "e2e-scenarios" / "FLEET-F62"
ROLES = ("orchestrator", "architect", "builder", "tester", "reviewer")
ALL_DISTS = ("default",) + ROLES
# the fleet-admin tools the veto must deny to every worker: the onboarding/create tools the render lists in
# settings.admin_tools, unioned by nv-fleet-gates with its vendored {cronjob_manage}
FLEET_ADMIN_TOOLS = ("cronjob_manage", "onboard_coworker", "onboard_project", "create_agent")

# The sandbox substrate is a DATA column: the spec's `substrate` value is what the render switches its backend
# emission on, and every substrate-specific expectation is declared in that column's own fixtures/<sub>/meta.json
# as {backend, checks} — a list of {op, key, ...} run through the backend-neutral _run_check ops. The
# parametrize set is DISCOVERED from the committed fixture dirs, so a second column is a new spec/<sub>/ +
# fixtures/<sub>/ tree with its meta.json declaring its own checks, and NOTHING in this file changes.
pytestmark = pytest.mark.linux_only  # generate_systemd_unit + reconcile_profile_gateways are the Linux CI target


def _spec_for(substrate: str) -> Path:
    return SCEN_DIR / "spec" / substrate / "coworker-types.yaml"


def _fixtures_for(substrate: str) -> Path:
    return SCEN_DIR / "fixtures" / substrate


def _golden_managed_for(substrate: str) -> Path:
    return _fixtures_for(substrate) / "managed" / "config.yaml"


def _meta_for(substrate: str) -> dict:
    return json.loads((_fixtures_for(substrate) / "meta.json").read_text(encoding="utf-8"))


def _run_check(cfg: dict, chk: dict, home: Path, name: str) -> None:
    """Execute one declarative substrate check from meta.json. The ops are backend-neutral primitives; every
    substrate-specific literal (which config key, which value, mount separator, egress marker) lives in the
    fixture, so a new column declares its own checks (ssh keys, an openshell-policy marker, ...) with no
    change here."""
    key = chk["key"]
    val = _dig(cfg, key)
    op = chk["op"]
    if op == "equals":
        assert val == chk["value"], f"{name}: {key} must equal {chk['value']!r}, got {val!r}"
    elif op == "is_bool":
        assert isinstance(val, bool), f"{name}: {key} must be rendered as an explicit bool, got {val!r}"
    elif op == "nonempty_list":
        assert isinstance(val, list) and val, f"{name}: {key} must be a nonempty list, got {val!r}"
    elif op == "list_item_contains":
        assert any(chk["substring"] in str(v) for v in (val or [])), f"{name}: no {key} item contains {chk['substring']!r}"
    elif op == "list_field_outside_home":
        for item in (val or []):
            src = str(item).split(chk["sep"], 1)[0]
            assert not _is_inside(src, home), f"{name}: {key} source {src!r} is inside HERMES_HOME"
    elif op == "mapping_any_entry_contains":
        # at least one of the allowlisted entry keys must carry the marker value. The allowlist is EXACT keys
        # (positive proxy vars only) so NO_PROXY — the bypass list — can never satisfy a proxy-hop check; both
        # the allowlist and the expected value come from the fixture
        assert isinstance(val, dict), f"{name}: {key} must be a mapping, got {val!r}"
        hits = [e for e in chk["entries"] if e in val and chk["value_contains"] in str(val[e])]
        assert hits, f"{name}: none of {chk['entries']} in {key} carries a value containing {chk['value_contains']!r}"
    else:
        raise AssertionError(f"{name}: unknown meta.json check op {op!r}")


def _discover_substrates() -> tuple:
    fx = SCEN_DIR / "fixtures"
    found = tuple(sorted(p.name for p in fx.iterdir() if (p / "meta.json").exists())) if fx.is_dir() else ()
    return found or ("podman",)  # parametrize needs at least one value


SUBSTRATES = _discover_substrates()


def _require_fixtures(substrate: str) -> None:
    required = (_spec_for(substrate), _golden_managed_for(substrate), _fixtures_for(substrate) / "meta.json")
    missing = [p for p in required if not p.exists()]
    if missing:
        pytest.fail(f"FLEET-F62 {substrate} spec/fixtures not committed yet: " + ", ".join(str(m) for m in missing))


def _read_yaml(path: Path) -> dict:
    import yaml

    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _dig(cfg: dict, dotted: str):
    node = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _copy_plugin_code(home: Path) -> None:
    """Make the composed plugin code discoverable from home/plugins/ WITHOUT touching config.yaml —
    enablement is the render's job and is asserted, never injected."""
    (home / "plugins").mkdir(parents=True, exist_ok=True)
    for name in COMPOSED:
        dst = home / "plugins" / name
        if not dst.exists():
            shutil.copytree(SRC_PLUGINS / name, dst, ignore=shutil.ignore_patterns("__pycache__"))


def _cli(*args: str, home: Path, bundled: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["HERMES_HOME"] = str(home)
    env["HERMES_BUNDLED_PLUGINS"] = str(bundled)
    env["HERMES_ENABLE_PROJECT_PLUGINS"] = "0"
    return subprocess.run(  # noqa: S603 - fixed argv, test-controlled paths
        [sys.executable, "-m", "hermes_cli.main", *args],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
    )


def _render_fleet(substrate: str, out_dir: Path, tmp_path: Path, bundled: Path) -> Path:
    """Render via ``hermes coworker compose`` from a dedicated bootstrap home that enables ONLY the compose
    CLI, so the render's own enablement of the fleet plugins is not masked by the harness."""
    boot = tmp_path / "bootstrap"
    boot.mkdir()
    _copy_plugin_code(boot)
    import yaml

    (boot / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": ["nv-coworker-compose"]}}), encoding="utf-8")
    proc = _cli("coworker", "compose", str(_spec_for(substrate)), "--out", str(out_dir), home=boot, bundled=bundled)
    if proc.returncode != 0:
        pytest.fail(f"`coworker compose` failed (rc={proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}")
    return out_dir


def _install_fleet(render_dir: Path, home: Path, bundled: Path) -> Path:
    """Install the six rendered distributions. The default distribution IS the base (launch) profile — its
    rendered files (config.yaml, SOUL.md, .env.template→.env.EXAMPLE) land in the root home; the five roles
    install via `hermes profile install`. Returns the managed dir holding the rendered managed fragment."""
    (home / "profiles").mkdir(parents=True, exist_ok=True)
    shutil.copy2(render_dir / "default" / "config.yaml", home / "config.yaml")
    if (render_dir / "default" / "SOUL.md").exists():
        shutil.copy2(render_dir / "default" / "SOUL.md", home / "SOUL.md")
    if (render_dir / "default" / ".env.template").exists():
        shutil.copy2(render_dir / "default" / ".env.template", home / ".env.EXAMPLE")
    _copy_plugin_code(home)
    for role in ROLES:
        proc = _cli("profile", "install", str(render_dir / role), "--name", role, "-y", home=home, bundled=bundled)
        if proc.returncode != 0:
            pytest.fail(f"profile install {role} failed: {proc.stderr.strip() or proc.stdout.strip()}")
        _copy_plugin_code(home / "profiles" / role)
    managed = home / "managed"
    managed.mkdir(exist_ok=True)
    shutil.copy2(render_dir / "managed" / "config.yaml", managed / "config.yaml")
    return managed


def _build_rendered(substrate: str, tmp_path: Path, monkeypatch) -> dict:
    """Render + install the fleet for one substrate column. The parameterized criteria (tests 1, 2, 3) call
    this directly with their substrate param; the ``rendered`` fixture pins the podman column for the
    substrate-independent supervision criterion (test 4)."""
    _require_fixtures(substrate)
    home = tmp_path / "home"
    home.mkdir()
    bundled = tmp_path / "empty-bundled"
    bundled.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    render_dir = _render_fleet(substrate, tmp_path / "render", tmp_path, bundled)
    managed = _install_fleet(render_dir, home, bundled)
    return {"home": home, "render": render_dir, "bundled": bundled, "managed": managed, "substrate": substrate}


@pytest.fixture()
def rendered(tmp_path, monkeypatch):
    # test 4 (supervision) is substrate-independent; it runs on the podman column. tests 1, 2, 3 parameterize
    # over SUBSTRATES and call _build_rendered themselves.
    return _build_rendered("podman", tmp_path, monkeypatch)


def _is_inside(host_src: str, root: Path) -> bool:
    try:
        return Path(host_src).resolve().is_relative_to(root.resolve())
    except (ValueError, OSError):
        return False


@pytest.mark.parametrize("substrate", SUBSTRATES)
def test_ac_fleet_f62_1(substrate, tmp_path, monkeypatch):
    """AC-FLEET-F62-1: compose renders exactly six distributions + a managed fragment; the DEFAULT multiplexes
    exactly the five with a force-written >=30s watchdog and auto_prune off, each config enables the column's
    required plugins, profiles_to_serve yields default + those five, and no coworker binds a platform port."""
    from hermes_cli.profiles import profiles_to_serve

    rendered = _build_rendered(substrate, tmp_path, monkeypatch)
    render_dir = rendered["render"]
    meta = _meta_for(substrate)
    # structure (a behaviour contract on the render, not a byte snapshot): exactly the six distributions + the
    # managed fragment; each distribution is installable (distribution.yaml at its root) and carries a config.yaml
    assert sorted(p.name for p in render_dir.iterdir() if p.is_dir()) == sorted(ALL_DISTS + ("managed",))
    for name in ALL_DISTS:
        assert (render_dir / name / "distribution.yaml").is_file(), f"{name} render missing distribution.yaml"
        assert (render_dir / name / "config.yaml").is_file(), f"{name} render missing config.yaml"
    assert (render_dir / "managed" / "config.yaml").is_file(), "managed fragment missing config.yaml"

    default_cfg = _read_yaml(render_dir / "default" / "config.yaml")
    assert _dig(default_cfg, "gateway.multiplex_profiles") is True
    assert sorted(_dig(default_cfg, "gateway.multiplex_profile_allowlist") or []) == sorted(ROLES)
    assert _dig(default_cfg, "sessions.auto_prune") is False
    assert _dig(default_cfg, "checkpoints.auto_prune") is False

    # the >=30s watchdog is a force-written render INVARIANT, not a pass-through: the committed spec sets a
    # below-floor value (1) in its default_config, and compose reads default_config, yet the render floors it
    spec_wd = _dig(_read_yaml(_spec_for(substrate)), "default_config.gateway.systemd_watchdog_seconds")
    assert spec_wd == 1, "committed spec must set default_config.gateway.systemd_watchdog_seconds=1 (below floor) to prove the force-writer"
    rendered_wd = _dig(default_cfg, "gateway.systemd_watchdog_seconds")
    assert isinstance(rendered_wd, int) and rendered_wd >= 30

    # each rendered config enables the column's declared plugin set (podman: all three; a substrate that does
    # not use the wrapper omits podman-onecli) — declared in meta.json so a new column needs no test edit
    required = set(meta["enabled_plugins"])
    for name in ALL_DISTS:
        enabled = set(_dig(_read_yaml(render_dir / name / "config.yaml"), "plugins.enabled") or [])
        assert required <= enabled, f"{name} config must enable {sorted(required)}, got {sorted(enabled)}"

    served = {name for name, _dir in profiles_to_serve(multiplex=True, profile_allowlist=list(ROLES))}
    assert served == {"default"} | set(ROLES)

    from gateway.config import platform_binds_port  # production listener-ownership predicate

    for name in ROLES:
        cfg = _read_yaml(render_dir / name / "config.yaml")
        platforms = _dig(cfg, "gateway.platforms") or _dig(cfg, "platforms") or {}
        for platform_name, block in (platforms.items() if isinstance(platforms, dict) else []):
            if isinstance(block, dict) and block.get("enabled"):
                assert not platform_binds_port(platform_name, block.get("extra")), f"{name} enables port-binding platform {platform_name}"


@pytest.mark.parametrize("substrate", SUBSTRATES)
def test_ac_fleet_f62_2(substrate, tmp_path, monkeypatch):
    """AC-FLEET-F62-2: every coworker renders the substrate's terminal.backend + a matching expected_backend,
    the nv-fleet-gates enforce_sandbox/edges_db_path settings and the admin_tools fleet-admin list, profile_roles
    in the managed fragment, HERMES_DUMP_REQUESTS true, and no `local` backend on any profile incl. DEFAULT;
    the substrate's own hardening (image, policed mounts incl. the shared-learnings mount all outside
    HERMES_HOME, mount-CWD + explicit persistence, the egress block, credential enablement) via meta.json checks."""
    rendered = _build_rendered(substrate, tmp_path, monkeypatch)
    render_dir = rendered["render"]
    home = rendered["home"]
    meta = _meta_for(substrate)
    backend = meta["backend"]
    managed = _read_yaml(render_dir / "managed" / "config.yaml")
    roles_map = _dig(managed, "plugins.entries.nv-fleet-gates.settings.profile_roles") or {}
    assert roles_map.get("orchestrator") == "orchestrator"
    assert all(roles_map.get(r) == "worker" for r in ("architect", "builder", "tester", "reviewer"))

    for name in ALL_DISTS:
        rendered_backend = _dig(_read_yaml(render_dir / name / "config.yaml"), "terminal.backend")
        assert rendered_backend == backend, f"{name} backend must be {backend}, got {rendered_backend!r}"
        assert rendered_backend != "local"

    for name in ROLES:
        cfg = _read_yaml(render_dir / name / "config.yaml")
        settings = _dig(cfg, "plugins.entries.nv-fleet-gates.settings") or {}
        assert settings.get("enforce_sandbox") is True
        assert settings.get("expected_backend") == backend  # render-derived from the substrate, matches terminal.backend
        edges = settings.get("edges_db_path")
        assert edges and Path(edges).is_absolute() and not _is_inside(edges, home)
        env_template = render_dir / name / ".env.template"
        assert env_template.exists() and "HERMES_DUMP_REQUESTS=true" in env_template.read_text(encoding="utf-8")
        # the veto's fleet-admin set must include the onboarding/create tools (unioned with the vendored
        # {cronjob_manage}); the render declares them so AC-3 can prove per-tool denial
        assert set(FLEET_ADMIN_TOOLS) <= set(settings.get("admin_tools") or []) | {"cronjob_manage"}, \
            f"{name} nv-fleet-gates.settings.admin_tools must list the onboarding/create fleet-admin tools"
        # the substrate's own hardening contract (image, mounts, egress, and any credential enablement),
        # declared in its meta.json and run through backend-neutral ops, so a second column asserts its shape
        # (ssh keys, an openshell-policy marker) with no test edit
        for chk in meta["checks"]:
            _run_check(cfg, chk, home, name)


def _load_profile_plugins(profile_home: Path, monkeypatch, bundled: Path, managed: Path):
    """Discover+load a profile's enabled plugin set with the managed fragment in scope, so nv-fleet-gates
    reads profile_roles (conftest scrubs HERMES_MANAGED_DIR — it is re-set per home here)."""
    from hermes_cli.plugins import PluginManager

    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    try:
        from hermes_cli import managed_scope

        for fn in ("invalidate_managed_cache", "reset_managed_cache", "clear_cache"):
            if callable(getattr(managed_scope, fn, None)):
                getattr(managed_scope, fn)()
                break
    except ImportError:
        pass
    # register the core tools so nv-fleet-gates' restricted-set backstop finds a complete set and defers to
    # the specific fleet-admin/wired-only/sandbox predicates (an absent restricted set fails closed on every call)
    import model_tools

    model_tools.discover_builtin_tools()
    manager = PluginManager()
    manager.discover_and_load()
    return manager


def _pre_tool_call_hooks(manager) -> list:
    if hasattr(manager, "list_registered_hooks"):
        return list(manager.list_registered_hooks("pre_tool_call"))
    return list(getattr(manager, "_hooks", {}).get("pre_tool_call", []))


def _block_message(manager, tool_name, args):
    results = manager.invoke_hook(
        "pre_tool_call", tool_name=tool_name, args=args, task_id="t", session_id="s",
        tool_call_id="c", turn_id="u", api_request_id="r", middleware_trace=None,
    )
    for r in results if isinstance(results, list) else [results]:
        if isinstance(r, dict) and r.get("action") == "block":
            return r.get("message")
    return None


@pytest.mark.parametrize("substrate", SUBSTRATES)
def test_ac_fleet_f62_3(substrate, tmp_path, monkeypatch):
    """AC-FLEET-F62-3: each profile home loads exactly one pre_tool_call hook (nv-fleet-gates); under a worker
    home the veto DENIES every fleet-admin tool, an unwired message_agent, and a backend-mismatched terminal
    call, while the orchestrator is allowed them; and on a multiplexed (active!=launch) turn a sandbox-consuming
    call is cross-profile refused while a wired message_agent is exempt and an unwired one is still blocked."""
    rendered = _build_rendered(substrate, tmp_path, monkeypatch)
    home, bundled, managed = rendered["home"], rendered["bundled"], rendered["managed"]
    backend = _meta_for(substrate)["backend"]
    homes = {"default": home, **{r: home / "profiles" / r for r in ROLES}}
    for name, phome in homes.items():
        mgr = _load_profile_plugins(phome, monkeypatch, bundled, managed)
        assert len(_pre_tool_call_hooks(mgr)) == 1, f"{name}: expected exactly one pre_tool_call hook"

    # a ready task env so _sandbox_block does not refuse on "task env not ready" for the non-sandbox cases
    import tools.terminal_tool as tt

    monkeypatch.setattr(tt, "ensure_task_env", lambda *a, **k: object())

    mgr_b = _load_profile_plugins(homes["builder"], monkeypatch, bundled, managed)
    monkeypatch.setenv("TERMINAL_ENV", "local")  # a resolved backend that mismatches the expected one
    msg = _block_message(mgr_b, "terminal", {"command": "echo hi"})
    assert msg and "sandbox: refused" in msg and "'local'" in msg and f"'{backend}'" in msg
    monkeypatch.setenv("TERMINAL_ENV", backend)  # sandbox predicate satisfied; isolate the fleet-admin/wired-only blocks
    # the veto DENIES each fleet-admin tool for a worker (not merely hides it from the schema — nv-fleet-gates
    # has no schema-hiding path); the render must list onboard_*/create_agent in settings.admin_tools, which
    # unions with the vendored {cronjob_manage}, and AC-FLEET-F62-2 asserts that render
    for tool in FLEET_ADMIN_TOOLS:
        msg = _block_message(mgr_b, tool, {})
        assert msg and "fleet-admin" in msg and "orchestrator-only" in msg and "builder" in msg, f"builder must be fleet-admin denied {tool}"
    msg = _block_message(mgr_b, "message_agent", {"target": "orchestrator", "message": "hi"})
    assert msg and msg.startswith("wired-only:") and "builder is not wired to orchestrator" in msg

    mgr_o = _load_profile_plugins(homes["orchestrator"], monkeypatch, bundled, managed)
    monkeypatch.setenv("TERMINAL_ENV", backend)
    for tool in FLEET_ADMIN_TOOLS:
        assert _block_message(mgr_o, tool, {}) is None, f"orchestrator must be allowed {tool}"

    # multiplexed axis: the gateway loads the veto under the LAUNCH profile, so a served secondary's turn has
    # active != launch. A sandbox-consuming tool is then cross-profile-refused; message_agent is exempt because
    # its delivery is host-local and consumes no sandbox, so coordination still flows along the wired edges.
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    mgr_launch = _load_profile_plugins(home, monkeypatch, bundled, managed)  # process HERMES_HOME = launch (default)
    edges_db = _dig(_read_yaml(home / "config.yaml"), "plugins.entries.nv-fleet-gates.settings.edges_db_path")
    sys.path.insert(0, str(home / "plugins" / "nv-fleet-gates"))
    import edges as fg_edges

    fg_edges.add_edge(edges_db, "architect", "builder", gated=False)

    # Prove the exemption bypasses the ENTIRE sandbox predicate, not merely the cross-profile branch: a backend
    # mismatch (TERMINAL_ENV=local vs docker) AND a task-env probe that raises if consulted would each block or
    # error a non-exempt tool; message_agent must reach neither. The cross-profile branch runs first, so the
    # terminal refusal below never consults either — that is what makes them a clean bypass witness.
    def _task_env_must_not_run(*a, **k):
        raise AssertionError("ensure_task_env must not be consulted for the exempt message_agent tool")

    monkeypatch.setattr(tt, "ensure_task_env", _task_env_must_not_run)
    monkeypatch.setenv("TERMINAL_ENV", "local")
    token = set_hermes_home_override(str(home / "profiles" / "architect"))
    try:
        msg = _block_message(mgr_launch, "terminal", {"command": "ls"})
        assert msg and "cross-profile refused" in msg and "architect" in msg, "a non-launch sandbox call must be cross-profile refused"
        assert _block_message(mgr_launch, "message_agent", {"target": "builder", "message": "hi"}) is None, \
            "message_agent to a WIRED target must be exempt from the whole sandbox predicate (else the fleet cannot coordinate)"
        msg = _block_message(mgr_launch, "message_agent", {"target": "tester", "message": "hi"})
        assert msg and msg.startswith("wired-only:"), "an UNWIRED message_agent must still be wiring-blocked"
    finally:
        reset_hermes_home_override(token)


def test_ac_fleet_f62_4(rendered, tmp_path, monkeypatch):
    """AC-FLEET-F62-4: generate_systemd_unit for the DEFAULT home emits Type=notify + NotifyAccess=main +
    WatchdogSec + the restart policy + HERMES_HOME env + `gateway run` with no --profile; under multiplex
    reconcile_profile_gateways(dry_run=True) starts the default and only registers each coworker."""
    from hermes_cli import container_boot as cb
    from hermes_cli import gateway as gw

    default_home = rendered["home"]
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    unit = gw.generate_systemd_unit()
    for needle in (
        "Type=notify", "NotifyAccess=main",
        "Restart=always", "RestartSec=5",
        "RestartForceExitStatus=75", "RestartPreventExitStatus=78",
        f'Environment="HERMES_HOME={default_home}"',
    ):
        assert needle in unit, f"systemd unit missing {needle!r}"
    # Bind the generated unit to the enforced configuration value.
    wd = _dig(_read_yaml(default_home / "config.yaml"), "gateway.systemd_watchdog_seconds")
    assert isinstance(wd, int) and wd >= 30, f"DEFAULT config must carry a >=30 watchdog (the force-writer), got {wd!r}"
    assert f"WatchdogSec={wd}s" in unit, "the unit's WatchdogSec must reflect the rendered >=30 value"
    execstart = next(ln for ln in unit.splitlines() if ln.startswith("ExecStart="))
    assert execstart.rstrip().endswith("gateway run") and "--profile" not in execstart

    scandir = tmp_path / "gw-state"
    scandir.mkdir()
    monkeypatch.setenv("GATEWAY_MULTIPLEX_PROFILES", "1")
    # a coworker whose own state says running must still only register, never start, under multiplex
    (default_home / "profiles" / "builder" / "gateway_state.json").write_text(
        json.dumps({"desired_state": "running"}), encoding="utf-8"
    )
    (default_home / "gateway_state.json").write_text(json.dumps({"desired_state": "running"}), encoding="utf-8")
    actions = {
        getattr(a, "profile", None) or getattr(a, "name", None): a
        for a in cb.reconcile_profile_gateways(hermes_home=default_home, scandir=scandir, dry_run=True)
    }
    assert "default" in actions and actions["default"].action == "started"
    for role in ROLES:
        act = actions.get(role)
        assert act is not None, f"{role} must be registered"
        assert act.action == "registered", f"{role} must be registered, never started (got {act.action!r})"


def test_render_rejects_unknown_substrate(tmp_path, monkeypatch):
    """Unknown substrates fail before writing any distribution."""
    import yaml

    _require_fixtures("podman")
    bundled = tmp_path / "empty-bundled"
    bundled.mkdir()
    boot = tmp_path / "bootstrap"
    boot.mkdir()
    _copy_plugin_code(boot)
    (boot / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": ["nv-coworker-compose"]}}), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")

    spec = yaml.safe_load(_spec_for("podman").read_text(encoding="utf-8"))
    spec["substrate"] = "openshell-not-yet-implemented"
    bad_spec = tmp_path / "bad-substrate.yaml"
    bad_spec.write_text(yaml.safe_dump(spec), encoding="utf-8")
    out = tmp_path / "render-bad"
    proc = _cli("coworker", "compose", str(bad_spec), "--out", str(out), home=boot, bundled=bundled)
    assert proc.returncode != 0, "render must fail closed on an unknown substrate"
    combined = (proc.stderr + proc.stdout).lower()
    assert "substrate" in combined and "openshell-not-yet-implemented" in combined
    written = [p.name for p in out.iterdir() if p.is_dir()] if out.exists() else []
    assert not written, f"no distribution may be written on a fail-closed render, found {written}"


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
    """Load podman-onecli through the real discovery path with the bridge base + allowlist in config.yaml,
    so register(ctx) → ctx.get_config('insecure_no_auth_origins') → oneclient.configure(...) actually runs
    (proving the plumbing, not just the guard function). Returns the configured oneclient module."""
    import yaml

    from hermes_cli.plugins import PluginManager

    home = tmp_path / "poh"
    (home / "plugins").mkdir(parents=True)
    shutil.copytree(SRC_PLUGINS / "podman-onecli", home / "plugins" / "podman-onecli",
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
    """The podman-onecli base-URL guard, configured through register(ctx)→configure(), permits http:// only
    to an allowlisted EXACT origin when no bootstrap key is set, still protects a present key, and fails
    closed on 401/403. Permanent home: tests/plugins/test_podman_onecli_acceptance.py. The exact-origin bound
    (not host-only) is deliberate: get_container_config returns the aoc_ proxy token as userinfo, so a
    host-only allowlist would leak it on any other port of the same host."""
    oneclient = _load_podman_onecli(tmp_path, monkeypatch, ["172.17.0.1:10256"])

    oneclient._require_secure_base("http://172.17.0.1:10256")
    captured = {}

    def _capture(req, timeout=30):
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        return _FakeResp(200)

    monkeypatch.setattr(oneclient._OPENER, "open", _capture)
    oneclient.get_container_config(agent="architect")
    assert "authorization" not in captured["headers"], "no key set ⇒ no Authorization header"

    with pytest.raises(oneclient.OneCLIError):
        oneclient._require_secure_base("http://172.17.0.1:9999")
    with pytest.raises(oneclient.OneCLIError):
        oneclient._require_secure_base("http://10.0.0.5:10256")

    monkeypatch.setenv("ONECLI_API_KEY", "aoc_secret")
    with pytest.raises(oneclient.OneCLIError):
        oneclient._require_secure_base("http://172.17.0.1:10256")
    monkeypatch.delenv("ONECLI_API_KEY", raising=False)

    monkeypatch.setattr(oneclient._OPENER, "open", lambda req, timeout=30: _FakeResp(fail_status))
    with pytest.raises(oneclient.OneCLIError):
        oneclient.get_container_config(agent="architect")
