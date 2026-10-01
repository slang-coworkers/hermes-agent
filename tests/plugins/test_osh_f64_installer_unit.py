"""Unit tests for the OSH-F64 installer's safety and idempotence behaviour.

Behaviour contracts on the pure planner and its executor — deep-merge preserves unrelated
keys, backups are not re-taken, drift forces a reinstall, a fully-installed home plans
nothing, the default backup precedes plugin installs, room creation is dispatched through
an injected creator, and paths fail closed when unset. No network, no snapshot of source;
``HERMES_HOME`` is tmp_path-rooted.
"""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest

PLUGIN_KEY = "nv-coworker-compose"
COWORKERS = ("orchestrator", "architect", "builder", "tester", "reviewer")
SHA = "a" * 40


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "plugins" / PLUGIN_KEY / "plugin.yaml").exists():
            return parent
    pytest.fail("could not locate repo root")


def _installer():
    path = _repo_root() / "plugins" / PLUGIN_KEY / "openshell" / "installer.py"
    spec = importlib.util.spec_from_file_location("osh_f64_installer_unit", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _spec_path() -> Path:
    return _repo_root() / "tests" / "e2e-scenarios" / "OSH-F64" / "spec" / "coworker-types.yaml"


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(tmp_path / "managed"))


def test_default_diff_preserves_unrelated_keys():
    """apply_config_diff deep-merges; an operator's unrelated default key survives."""
    inst = _installer()
    existing = {"gateway": {"multiplex_profiles": False, "keep_me": 7}, "unrelated": {"x": 1}}
    _, diff, backup_required = inst.default_config_diff(existing, str(_spec_path()))
    assert diff and backup_required is True
    merged = inst.apply_config_diff(existing, diff)
    assert merged["unrelated"] == {"x": 1}
    assert merged["gateway"]["keep_me"] == 7
    assert merged["gateway"]["multiplex_profiles"] is True


def test_managed_diff_preserves_unrelated_machine_policy():
    """managed_config_diff deep-merges the rendered fragment; UNRELATED managed keys the
    render does not touch (here an operator's own security/approval policy) are preserved."""
    inst = _installer()
    existing = {
        "security": {"approval": {"transport": "builtin"}},
        "plugins": {"entries": {"nv-fleet-gates": {"settings": {"operator_only": True}}}},
    }
    rendered = {"plugins": {"entries": {"nv-fleet-gates": {"settings": {"profile_roles": {"builder": "worker"}}}}}}
    merged = inst.managed_config_diff(existing, rendered)
    assert merged["security"]["approval"]["transport"] == "builtin"
    settings = merged["plugins"]["entries"]["nv-fleet-gates"]["settings"]
    assert settings["operator_only"] is True
    assert settings["profile_roles"] == {"builder": "worker"}


def test_backup_present_suppresses_second_backup():
    """A re-run with the backup already present does not require a second backup."""
    inst = _installer()
    existing = {"gateway": {"multiplex_profiles": False}}
    _, _, first = inst.default_config_diff(existing, str(_spec_path()))
    assert first is True
    _, _, second = inst.default_config_diff(existing, str(_spec_path()), backup_present=True)
    assert second is False


def _full_state(inst):
    fresh = {"gateway": {"multiplex_profiles": False}}
    _, diff, _ = inst.default_config_diff(fresh, str(_spec_path()))
    installed = inst.apply_config_diff(fresh, diff)
    import yaml
    spine = yaml.safe_load((_spec_path().parent / "spines" / "base.yaml").read_text(encoding="utf-8")) or {}
    enabled = list(((spine.get("config") or {}).get("plugins") or {}).get("enabled", []))
    return {
        "default_config": installed,
        "plugins": {n: {"ref": SHA, "enabled": True} for n in enabled},
        "profile_plugins": {r: {n: {"ref": SHA, "enabled": True} for n in enabled} for r in COWORKERS},
        "profiles": list(COWORKERS),
        "managed_installed": True,
        "rooms": "all",
        "wires": "all",
    }


def _cmds(inst, state):
    out = []
    for step in inst.build_plan(state, str(_spec_path()), SHA):
        c = step.command
        out.append(" ".join(c) if isinstance(c, (list, tuple)) else str(c))
    return out


def test_fully_installed_plans_no_mutation():
    inst = _installer()
    import re
    mutating = re.compile(r"(plugins install|plugins enable|profile install|--force|gateway restart|backup|wire add|write|edit)")
    assert not [c for c in _cmds(inst, _full_state(inst)) if mutating.search(c)]


def test_drifted_profile_forces_reinstall():
    """A present-but-drifted profile (via profile_digests) is planned with --force."""
    inst = _installer()
    state = _full_state(inst)
    state["profile_digests"] = {"builder": {"current": "old", "desired": "new"}}
    cmds = _cmds(inst, state)
    assert any(c.startswith("hermes profile install") and "builder" in c and "--force" in c for c in cmds)


def test_disabled_plugin_enabled_not_reinstalled():
    inst = _installer()
    state = _full_state(inst)
    state["plugins"]["nv-fleet-gates"]["enabled"] = False
    cmds = _cmds(inst, state)
    assert "hermes plugins enable nv-fleet-gates" in cmds
    assert not any(c.startswith("hermes plugins install") and "nv-fleet-gates" in c for c in cmds)


def test_enabled_plugins_resolves_from_spine():
    """enabled_plugins reads the canonical set from the SPINE (no top-level mirror); a
    divergent top-level mirror, if one were ever re-added, fails closed."""
    inst = _installer()
    import yaml
    data = yaml.safe_load(_spec_path().read_text(encoding="utf-8")) or {}
    assert "enabled" not in (data.get("plugins") or {}), "spec must carry no top-level plugins.enabled mirror"
    ok = inst.enabled_plugins(data, str(_spec_path()))
    assert set(ok) == {"nv-coworker-compose", "nv-fleet-gates", "podman-onecli"}
    drifted = copy.deepcopy(data)
    drifted["plugins"] = {"enabled": ["nv-coworker-compose"]}
    with pytest.raises(ValueError):
        inst.enabled_plugins(drifted, str(_spec_path()))


def _spine_enabled():
    import yaml
    spine = yaml.safe_load((_spec_path().parent / "spines" / "base.yaml").read_text(encoding="utf-8")) or {}
    return list(((spine.get("config") or {}).get("plugins") or {}).get("enabled", []))


def test_phase_a_backs_up_before_first_plugin_command(monkeypatch):
    """phase_a snapshots the ORIGINAL config before any `plugins install --enable`
    rewrites it, not after."""
    import os
    inst = _installer()
    home = Path(os.environ["HERMES_HOME"]); (home / "plugins").mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        "plugins:\n  enabled: [nv-coworker-compose]\ngateway:\n  multiplex_profiles: false\n", encoding="utf-8")
    bak = home / ("config.yaml" + inst._BACKUP_SUFFIX)
    seen = []
    monkeypatch.setattr(inst, "_run", lambda cmd: seen.append((list(cmd), bak.exists())))
    inst.phase_a(str(_spec_path()), SHA)
    assert bak.exists(), "phase_a must back up the original config"
    plugin_calls = [(c, had) for c, had in seen if c[1:3] == ["plugins", "install"]]
    assert plugin_calls, "phase_a must run bootstrap plugin installs"
    assert all(had for _, had in plugin_calls), "backup must precede every plugin install"


def test_collect_home_state_reads_shared_metadata_revision():
    """Installed refs come from the shared `<home>/plugins/.install-metadata.json`
    (keyed by name, field `revision`), so a re-run is not seen as universally stale."""
    import os, json, yaml
    inst = _installer()
    home = Path(os.environ["HERMES_HOME"]); (home / "plugins").mkdir(parents=True, exist_ok=True)
    enabled = _spine_enabled()
    for name in enabled:
        (home / "plugins" / name).mkdir()
    (home / "plugins" / ".install-metadata.json").write_text(
        json.dumps({n: {"revision": SHA, "pinned": True, "source": "x"} for n in enabled}), encoding="utf-8")
    (home / "config.yaml").write_text("plugins:\n  enabled: [%s]\n" % ", ".join(enabled), encoding="utf-8")
    spec = yaml.safe_load(_spec_path().read_text(encoding="utf-8"))
    state = inst.collect_home_state(spec, str(_spec_path()))
    for name in enabled:
        assert state["plugins"][name] == {"ref": SHA, "enabled": True}


def test_default_diff_seeds_fleet_gates_edges_db_path():
    """The in-place default edit seeds the spine's nv-fleet-gates settings so the
    default home carries edges_db_path (`hermes wire add` reads it with no default)."""
    inst = _installer()
    _, diff, _ = inst.default_config_diff({"gateway": {"multiplex_profiles": False}}, str(_spec_path()))
    key = "plugins.entries.nv-fleet-gates.settings"
    assert key in diff and diff[key].get("edges_db_path"), "default edit must carry nv-fleet-gates.edges_db_path"


def test_managed_installed_requires_ssh_host_and_approvals():
    """A managed fragment is 'installed' only with profile_roles AND expected_ssh_host AND
    the machine-wide GOV-F23 approval floor AND the §D6 plugins.scan_on_install: false guard
    (F64-P2). A FLEET-F62 fragment (profile_roles only), an openshell fragment still missing the
    approval keys, and one still missing the scan guard each leave managed_installed False so the
    write backfills them — neither the ssh veto, the approval floor, nor the install-scan guard is
    left fail-open (a re-run without the guard hits the DANGEROUS verdict --force cannot override)."""
    import os, yaml
    inst = _installer()
    managed = Path(os.environ["HERMES_MANAGED_DIR"]); managed.mkdir(parents=True, exist_ok=True)
    Path(os.environ["HERMES_HOME"], "plugins").mkdir(parents=True, exist_ok=True)
    spec = yaml.safe_load(_spec_path().read_text(encoding="utf-8"))

    def _installed(cfg):
        (managed / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
        return inst.collect_home_state(spec, str(_spec_path()))["managed_installed"]

    base = {"plugins": {"entries": {"nv-fleet-gates": {"settings": {"profile_roles": {"builder": "worker"}}}}}}
    assert _installed(base) is False                               # no expected_ssh_host
    base["plugins"]["entries"]["nv-fleet-gates"]["settings"]["expected_ssh_host"] = {"builder": "openshell-osh-f64-builder"}
    assert _installed(base) is False                               # roles+host but no approval floor
    base["approvals"] = {"mode": "smart"}
    base["security"] = {"approval": {"transport": "builtin"}}
    assert _installed(base) is False                               # roles+host+approvals but no §D6 scan guard (F64-P2)
    base["plugins"]["scan_on_install"] = False
    assert _installed(base) is True                                # roles + host + approval floor + scan guard


def test_room_steps_carry_payload_and_execute_via_creator():
    """Each room is a structured step the executor dispatches through the injected
    groups.create creator; with no creator it fails closed (never a silent skip)."""
    import os, yaml
    inst = _installer()
    home = Path(os.environ["HERMES_HOME"]); (home / "plugins").mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("gateway:\n  multiplex_profiles: false\n", encoding="utf-8")
    spec = yaml.safe_load(_spec_path().read_text(encoding="utf-8"))
    steps = inst.build_plan(inst.collect_home_state(spec, str(_spec_path())), str(_spec_path()), SHA)
    room_steps = [s for s in steps if s.tag == "rooms"]
    spec_rooms = spec.get("rooms") or {}
    assert len(room_steps) == len(spec_rooms)
    for s in room_steps:
        assert s.data["room_id"] in spec_rooms
        assert s.data["name"] == spec_rooms[s.data["room_id"]]["name"]
        assert s.data["members"] == spec_rooms[s.data["room_id"]]["members"]
    calls = []
    inst._execute_step(room_steps[0], {"home": str(home), "room_creator": lambda rid, n, m: calls.append(rid)})
    assert calls == [room_steps[0].data["room_id"]]
    with pytest.raises(ValueError):
        inst._execute_step(room_steps[0], {"home": str(home)})


def test_managed_write_fails_closed_without_managed_dir():
    """With no managed dir resolved, the managed write refuses (never targets cwd)."""
    import os
    inst = _installer()
    home = Path(os.environ["HERMES_HOME"]); home.mkdir(parents=True, exist_ok=True)
    step = inst.Step("# write managed", "managed_write")
    with pytest.raises(ValueError):
        inst._execute_step(step, {"home": str(home), "managed_dir": "", "out": str(home)})


def test_default_diff_preserves_unrelated_plugin_settings():
    """The default edit deep-merges spine settings; an unrelated operator key on the same
    entry survives (never a wholesale replace)."""
    inst = _installer()
    existing = {"gateway": {"multiplex_profiles": False},
                "plugins": {"entries": {"nv-fleet-gates": {"settings": {"operator_only": True}}}}}
    _, diff, _ = inst.default_config_diff(existing, str(_spec_path()))
    merged = inst.apply_config_diff(existing, diff)
    settings = merged["plugins"]["entries"]["nv-fleet-gates"]["settings"]
    assert settings["operator_only"] is True   # unrelated operator key preserved
    assert settings.get("edges_db_path")        # spine setting merged in


def test_default_diff_preserves_operator_grant_in_secret_sets():
    """The openshell grant apply OVERRIDES the served roles' profile_secret_sets but PRESERVES an
    operator-managed identity already in that map (render-parallel merge, not a wholesale replace);
    a re-run over the applied default is idempotent (§D7.1 ruling 2726 (a))."""
    inst = _installer()
    existing = {"gateway": {"multiplex_profiles": False},
                "plugins": {"entries": {"podman-onecli": {"settings": {
                    "profile_secret_sets": {"operator-sidecar": ["Ops-Only"]}}}}}}
    _, diff, _ = inst.default_config_diff(existing, str(_spec_path()))
    merged = inst.apply_config_diff(existing, diff)
    pss = merged["plugins"]["entries"]["podman-onecli"]["settings"]["profile_secret_sets"]
    assert pss["operator-sidecar"] == ["Ops-Only"]
    assert pss["orchestrator"] == ["Anthropic-Dev"]
    assert inst.default_config_diff(merged, str(_spec_path()))[1] == {}


def test_force_install_snapshots_plugin_before_reinstall():
    """A different-ref plugin is snapshotted immediately before its --force reinstall."""
    inst = _installer()
    enabled = _spine_enabled()
    state = {"default_config": {}, "plugins": {n: {"ref": "b" * 40, "enabled": True} for n in enabled},
             "profile_plugins": {}, "profiles": [], "managed_installed": False, "rooms": [], "wires": []}
    tags = [s.tag for s in inst.build_plan(state, str(_spec_path()), SHA)]
    force_idxs = [i for i, t in enumerate(tags) if t == "plugin_force"]
    assert force_idxs, "different-ref plugins must plan a force reinstall"
    for i in force_idxs:
        assert tags[i - 1] == "plugin_snapshot", "each --force reinstall must be immediately preceded by a snapshot"


def test_managed_only_change_is_backed_up():
    """When only the managed fragment needs writing (default already installed), the plan
    still snapshots first — the managed config is never overwritten without a backup."""
    import os
    inst = _installer()
    Path(os.environ["HERMES_HOME"], "plugins").mkdir(parents=True, exist_ok=True)
    managed = Path(os.environ["HERMES_MANAGED_DIR"]); managed.mkdir(parents=True, exist_ok=True)
    (managed / "config.yaml").write_text("plugins: {}\n", encoding="utf-8")
    enabled = _spine_enabled()
    _, diff, _ = inst.default_config_diff({"gateway": {"multiplex_profiles": False}}, str(_spec_path()))
    installed_default = inst.apply_config_diff({"gateway": {"multiplex_profiles": False}}, diff)
    state = {"default_config": installed_default,
             "plugins": {n: {"ref": SHA, "enabled": True} for n in enabled},
             "profile_plugins": {r: {n: {"ref": SHA, "enabled": True} for n in enabled} for r in COWORKERS},
             "profiles": list(COWORKERS), "managed_installed": False, "rooms": "all", "wires": "all",
             "backup_present": False}
    tags = [s.tag for s in inst.build_plan(state, str(_spec_path()), SHA)]
    assert "backup" in tags, "a managed-only change must still plan a backup"
    assert "managed_write" in tags


def test_installed_home_with_present_rooms_plans_no_room_or_restart():
    """A fully-installed home whose rooms already exist (rooms='all', the value apply()
    sets when room_exists reports every room present) plans neither a room step nor a
    gateway restart."""
    inst = _installer()
    enabled = _spine_enabled()
    _, diff, _ = inst.default_config_diff({"gateway": {"multiplex_profiles": False}}, str(_spec_path()))
    installed_default = inst.apply_config_diff({"gateway": {"multiplex_profiles": False}}, diff)
    state = {"default_config": installed_default,
             "plugins": {n: {"ref": SHA, "enabled": True} for n in enabled},
             "profile_plugins": {r: {n: {"ref": SHA, "enabled": True} for n in enabled} for r in COWORKERS},
             "profiles": list(COWORKERS), "managed_installed": True, "rooms": "all", "wires": "all",
             "backup_present": True}
    tags = [s.tag for s in inst.build_plan(state, str(_spec_path()), SHA)]
    assert "rooms" not in tags and "restart" not in tags


def test_non_openshell_spec_refused_before_any_install(tmp_path):
    """A spec whose substrate is not openshell is refused at the entry points, before any
    state collection — so a wrong spec never installs/enables plugins and only then fails."""
    inst = _installer()
    bad = tmp_path / "podman-spec.yaml"
    bad.write_text("substrate: podman\ntypes: {}\n", encoding="utf-8")
    for entry in (inst.plan_text, inst.phase_a, inst.apply):
        with pytest.raises(ValueError):
            entry(str(bad), SHA)


def test_default_apply_refuses_to_clobber_operator_scalar():
    """apply_config_diff fails closed rather than silently replacing an operator's
    non-mapping value at a key the OSH default edit descends into (never data loss)."""
    inst = _installer()
    existing = {"gateway": "disabled"}
    _, diff, _ = inst.default_config_diff(existing, str(_spec_path()))
    with pytest.raises(ValueError):
        inst.apply_config_diff(existing, diff)
    assert existing["gateway"] == "disabled"


def test_default_apply_refuses_to_clobber_operator_null():
    """An explicit YAML null at an intermediate key is an operator-owned value too:
    apply_config_diff fails closed rather than resurrecting it as a mapping."""
    inst = _installer()
    existing = {"gateway": None}
    _, diff, _ = inst.default_config_diff(existing, str(_spec_path()))
    with pytest.raises(ValueError):
        inst.apply_config_diff(existing, diff)


def test_phase_a_snapshots_bootstrap_plugin_before_force(tmp_path, monkeypatch):
    """A drifted bootstrap plugin is snapshotted (default-home dir) before its --force
    reinstall in Phase A — the rollback snapshot is not filtered out, even when HERMES_HOME
    itself sits under a directory named `profiles` (structural check, not a substring)."""
    import json
    inst = _installer()
    home = tmp_path / "profiles" / "gateway"   # a valid home whose path contains /profiles/
    monkeypatch.setenv("HERMES_HOME", str(home))
    (home / "plugins").mkdir(parents=True, exist_ok=True)
    enabled = _spine_enabled()
    for name in enabled:
        (home / "plugins" / name).mkdir()
    (home / "plugins" / ".install-metadata.json").write_text(
        json.dumps({n: {"revision": "b" * 40, "pinned": True} for n in enabled}), encoding="utf-8")
    (home / "config.yaml").write_text("plugins:\n  enabled: [%s]\n" % ", ".join(enabled), encoding="utf-8")
    ran = []
    monkeypatch.setattr(inst, "_execute_step", lambda step, ctx: ran.append((step.tag, getattr(step, "data", None))))
    inst.phase_a(str(_spec_path()), SHA)
    tags = [t for t, _ in ran]
    assert "plugin_snapshot" in tags and "plugin_force" in tags
    assert tags.index("plugin_snapshot") < tags.index("plugin_force"), "snapshot must precede the bootstrap force"
    snap = next(d for t, d in ran if t == "plugin_snapshot")
    assert (snap or {}).get("plugin_dir", "").startswith(str(home / "plugins")), \
        "Phase A runs the default-home snapshot"


@pytest.mark.linux_only
def test_atomic_write_preserves_existing_config_mode(tmp_path):
    """_atomic_write_yaml keeps the existing config's permission bits — an operator's 0600
    config is never silently widened to the temp file's umask default on rewrite."""
    import os
    import stat
    import yaml
    inst = _installer()
    cfg = tmp_path / "config.yaml"
    cfg.write_text("gateway:\n  multiplex_profiles: false\n", encoding="utf-8")
    os.chmod(cfg, 0o600)
    inst._atomic_write_yaml(cfg, {"gateway": {"multiplex_profiles": True}})
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    assert yaml.safe_load(cfg.read_text(encoding="utf-8"))["gateway"]["multiplex_profiles"] is True


def test_managed_diff_installs_approvals_and_security():
    """managed_config_diff installs the FULL rendered managed fragment — the GOV-F23
    approvals/security floor AND the nv-fleet-gates settings — with the rendered fleet policy
    winning on conflict, while an operator's UNRELATED managed key is preserved."""
    inst = _installer()
    existing = {"logging": {"level": "debug"},   # unrelated operator key — must survive
                "approvals": {"mode": "off"}}     # operator's weaker floor — the fleet overrides
    rendered = {
        "approvals": {"mode": "smart", "timeout": 300, "cron_mode": "deny"},
        "security": {"approval": {"transport": "builtin", "transport_fallback": "deny"}},
        "plugins": {"entries": {"nv-fleet-gates": {"settings": {"profile_roles": {"builder": "worker"}}}}},
    }
    merged = inst.managed_config_diff(existing, rendered)
    assert merged["approvals"]["mode"] == "smart"              # fleet floor wins over operator 'off'
    assert merged["approvals"]["cron_mode"] == "deny"
    assert merged["security"]["approval"]["transport"] == "builtin"
    assert merged["plugins"]["entries"]["nv-fleet-gates"]["settings"]["profile_roles"] == {"builder": "worker"}
    assert merged["logging"] == {"level": "debug"}            # unrelated operator key preserved


@pytest.mark.linux_only
def test_atomic_write_preserves_symlink_target(tmp_path):
    """_atomic_write_yaml writes THROUGH a symlinked config to its target — the symlink is
    never detached into a plain file, and the target receives the updated YAML."""
    import os
    import yaml
    inst = _installer()
    real = tmp_path / "real-config.yaml"
    real.write_text("gateway:\n  multiplex_profiles: false\n", encoding="utf-8")
    link = tmp_path / "config.yaml"
    os.symlink(real, link)
    inst._atomic_write_yaml(link, {"gateway": {"multiplex_profiles": True}})
    assert link.is_symlink()                                   # symlink preserved, not replaced
    assert os.path.realpath(link) == str(real)                 # still points at the target
    assert yaml.safe_load(real.read_text(encoding="utf-8"))["gateway"]["multiplex_profiles"] is True


def test_scan_guard_backup_records_original_absence_not_generated_file():
    """§D6 rollback integrity (F64-R1): when the managed config is ABSENT before install, the
    backup records an .absent marker (not a .bak), scan_guard_write then creates the file, and the
    Phase-B backup re-evaluation does NOT snapshot the installer's OWN scan-guard file — so rollback
    deletes it rather than restoring plugins.scan_on_install: false. An EXISTING managed config is
    backed up verbatim before the guard mutates it."""
    import os
    import yaml
    inst = _installer()
    Path(os.environ["HERMES_HOME"], "plugins").mkdir(parents=True, exist_ok=True)
    managed = Path(os.environ["HERMES_MANAGED_DIR"]); managed.mkdir(parents=True, exist_ok=True)
    mcfg = managed / "config.yaml"
    bak = managed / ("config.yaml" + inst._BACKUP_SUFFIX)
    absent = managed / ("config.yaml" + inst._ABSENT_SUFFIX)
    ctx = {"home": os.environ["HERMES_HOME"], "managed_dir": os.environ["HERMES_MANAGED_DIR"]}

    # ABSENT before install -> backup records the .absent marker, never a .bak.
    inst._execute_step(inst.Step("# backup", "backup"), ctx)
    assert absent.exists() and not bak.exists(), \
        "an absent managed config must be recorded by an .absent marker, not a .bak"
    # scan_guard_write then CREATES the managed config carrying the guard.
    inst._execute_step(inst.Step("# scan guard", "scan_guard_write"), ctx)
    assert (yaml.safe_load(mcfg.read_text(encoding="utf-8")) or {}).get("plugins", {}).get("scan_on_install") is False
    # Phase-B backup re-evaluation must NOT snapshot that installer-created file (the .absent marker
    # already recorded the original state) — otherwise rollback would restore scan_on_install: false.
    inst._execute_step(inst.Step("# backup", "backup"), ctx)
    assert not bak.exists(), "Phase-B backup must not snapshot the installer-created scan-guard file"

    # EXISTING before install -> backed up verbatim before the guard mutates it.
    absent.unlink()
    mcfg.write_text("plugins:\n  scan_on_install: true\nkeep: me\n", encoding="utf-8")
    inst._execute_step(inst.Step("# backup", "backup"), ctx)
    assert bak.exists(), "an existing managed config must be backed up before mutation"
    original = yaml.safe_load(bak.read_text(encoding="utf-8")) or {}
    assert original.get("keep") == "me" and original["plugins"]["scan_on_install"] is True, \
        "the .bak must capture the ORIGINAL managed content, not a generated scan-guard file"


def test_rollback_doc_deletes_absent_marker():
    """§D6 rollback integrity (F64-R1 round 2): the rollback runbook must delete the
    `.osh-f64.absent` marker alongside the managed config it created, so a stale marker cannot make
    a later operator-created managed config look originally-absent (which would skip its backup and
    leave it deletable on the next rollback)."""
    import re
    root = _repo_root()
    doc = (root / "website" / "docs" / "user-guide" / "fleet-openshell.md").read_text(encoding="utf-8")
    lower = doc.lower()
    start = lower.find("### rollback")
    assert start != -1, "runbook must carry a Rollback section"
    nxt = lower.find("\n### ", start + 1)
    section = lower[start:(nxt if nxt != -1 else len(lower))]
    assert "osh-f64.absent" in section, "rollback runbook must name the .osh-f64.absent marker"
    assert re.search(r"delete[\s\S]{0,160}osh-f64\.absent", section), \
        "rollback runbook must delete the .osh-f64.absent marker so no stale marker misleads a later install"


@pytest.mark.linux_only
def test_install_into_sandbox_dry_run_forwards_policy_root(tmp_path, monkeypatch):
    """§D3: the shell entrypoint forwards --policy-root to `installer.py plan`, so a --dry-run
    transcript rebases every `openshell sandbox create --policy` to <root>/render/<role>/
    policy-<role>.yaml; without --policy-root the gateway-internal render path is kept."""
    import os
    import shlex
    import subprocess
    import sys
    root = _repo_root()
    home = tmp_path / "home"; (home / "plugins").mkdir(parents=True)  # _require_home refuses the platform default
    (home / "config.yaml").write_text("plugins:\n  enabled: [nv-coworker-compose]\n", encoding="utf-8")
    script = root / "plugins" / PLUGIN_KEY / "openshell" / "install-into-sandbox.sh"
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["HERMES_PYTHON"] = sys.executable  # deterministic interpreter selection (has PyYAML under the runner)
    host_root = "/tmp/osh-f64-policy-root"

    def _create_policies(extra):
        proc = subprocess.run(
            ["bash", str(script), str(_spec_path()), "--ref", SHA, "--dry-run", *extra],
            capture_output=True, text=True, env=env, timeout=120,
        )
        assert proc.returncode == 0, f"dry-run failed: {proc.stderr}"
        out = {}
        for ln in proc.stdout.splitlines():
            v = shlex.split(ln.strip()) if ln.strip() else []
            if v[:3] == ["openshell", "sandbox", "create"]:
                out[v[v.index("--name") + 1]] = v[v.index("--policy") + 1]
        return out

    rooted = _create_policies(["--policy-root", host_root])
    assert rooted, "dry-run must plan `openshell sandbox create` steps"
    for name, policy in rooted.items():
        role = name[len("osh-f64-"):]
        assert policy == f"{host_root}/render/{role}/policy-{role}.yaml", \
            f"{name}: --policy-root must rebase --policy to the host mirror path (got {policy})"

    default = _create_policies([])
    for name, policy in default.items():
        role = name[len("osh-f64-"):]
        assert policy.endswith(f"/render/{role}/policy-{role}.yaml") and host_root not in policy, \
            f"{name}: without --policy-root the gateway-internal render path is kept (got {policy})"


def test_sandbox_list_failure_does_not_create(tmp_path, monkeypatch):
    """A failed `openshell sandbox list` halts apply (raises) rather than guessing "absent"
    and creating over a possibly-live worker sandbox — the safe direction for _sandbox_exists."""
    import subprocess
    inst = _installer()
    create_steps = [s for s in inst.build_plan({"profiles": []}, str(_spec_path()), SHA)
                    if s.tag == "provision_create"]
    assert create_steps, "build_plan must plan provision_create steps"
    created: list = []

    def _fake_run(cmd, *args, **kwargs):
        cmd = list(cmd)
        if cmd[:3] == ["openshell", "sandbox", "list"]:
            raise subprocess.CalledProcessError(1, cmd)
        if cmd[:3] == ["openshell", "sandbox", "create"]:
            created.append(cmd)

        class _CP:
            returncode = 0

        return _CP()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        inst._execute_step(create_steps[0], {"home": str(tmp_path)})
    assert not created, "no sandbox create may be issued when the existence check fails"


def test_sandbox_exists_reads_list_name_column(monkeypatch):
    """The real _sandbox_exists parser reads the NAME (first) column of `openshell sandbox
    list`: a listed sandbox is present, an unlisted one absent."""
    import subprocess
    inst = _installer()
    listing = (
        "NAME              CREATED              PHASE\n"
        "osh-f64-gw        2026-09-29 11:54:52  Ready\n"
        "osh-f64-builder   2026-09-29 11:55:10  Ready\n"
    )

    def _fake_run(cmd, *args, **kwargs):
        assert list(cmd) == ["openshell", "sandbox", "list"]

        class _CP:
            returncode = 0
            stdout = listing

        return _CP()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert inst._sandbox_exists("osh-f64-builder") is True
    assert inst._sandbox_exists("osh-f64-tester") is False
