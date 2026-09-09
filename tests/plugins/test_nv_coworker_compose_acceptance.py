"""Behavior-contract acceptance tests for the nv-coworker-compose plugin (LOOP-F35)."""

import argparse
from pathlib import Path
import shutil

import pytest
import yaml

from hermes_cli.plugins import PluginManager

PLUGIN_KEY = "nv-coworker-compose"
PLUGIN_SRC = Path(__file__).resolve().parents[2] / "plugins" / PLUGIN_KEY
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "loop-f35"
FIXTURE_SPEC = FIXTURE_DIR / "coworker-types.yaml"
EXPECTED_FILE = FIXTURE_DIR / "expected.yaml"

# DEFAULT_DIST_OWNED (hermes_cli/profile_distribution.py:88-95) plus the two the render must add.
FULL_DIST_OWNED = {
    "SOUL.md", "config.yaml", "mcp.json", "skills", "cron", "distribution.yaml",
    "skill-bundles", ".env.template",
}


def _expected():
    return yaml.safe_load(EXPECTED_FILE.read_text(encoding="utf-8"))


def _dig(mapping, dotted):
    node = mapping
    for key in dotted.split("."):
        if not isinstance(node, dict) or key not in node:
            return (False, None)
        node = node[key]
    return (True, node)


def _write_home(tmp_path, monkeypatch, profile_name=None):
    root = tmp_path / "hermes-root"
    home = root / "profiles" / profile_name if profile_name else root / "hermes-home"
    plugins_dir = home / "plugins"
    plugins_dir.mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, plugins_dir / PLUGIN_KEY)
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": [PLUGIN_KEY]}}), encoding="utf-8"
    )
    empty_bundled = tmp_path / "bundled-plugins"
    empty_bundled.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(empty_bundled))
    return home


def _load(tmp_path, monkeypatch, profile_name=None):
    _write_home(tmp_path, monkeypatch, profile_name=profile_name)
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled is True
    assert loaded.error is None
    assert loaded.module is not None
    return manager, loaded


def _render(loaded, out_root):
    return loaded.module.compose(str(FIXTURE_SPEC), str(out_root))


def _config(rendered_dir):
    return yaml.safe_load((Path(rendered_dir) / "config.yaml").read_text(encoding="utf-8"))


def _tree_state(root):
    root = Path(root)
    if not root.exists():
        return {}
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _coworker_profiles(exp):
    return [p for p in exp["profiles"] if p != exp["default_profile"]]


def test_ac_loop_f35_1(tmp_path, monkeypatch):
    """AC-LOOP-F35-1: hermes coworker compose renders, per coworker type in the spec, a profile-distribution SOURCE tree containing distribution.yaml, SOUL.md, config.yaml, skills/, cron/, mcp.json, .env.template (never .env.EXAMPLE, which the installer creates)"""
    _, loaded = _load(tmp_path, monkeypatch)
    exp = _expected()
    rendered = _render(loaded, tmp_path / "out")
    assert set(rendered) == set(exp["profiles"]), "rendered profile set != expected"
    required = {"distribution.yaml", "SOUL.md", "config.yaml", "mcp.json", ".env.template"}
    for name, profile_dir in rendered.items():
        profile_dir = Path(profile_dir)
        present = {p.name for p in profile_dir.iterdir()}
        assert required <= present, f"{name} missing {required - present}"
        assert ".env.EXAMPLE" not in present, f"{name}: .env.EXAMPLE is install-time, not rendered"
        assert (profile_dir / "skills").is_dir()
        assert (profile_dir / "cron").is_dir()
        dist = yaml.safe_load((profile_dir / "distribution.yaml").read_text(encoding="utf-8"))
        owned = set(dist.get("distribution_owned") or [])
        assert FULL_DIST_OWNED <= owned, f"{name}: distribution_owned missing {FULL_DIST_OWNED - owned}"


def test_ac_loop_f35_2(tmp_path, monkeypatch):
    """AC-LOOP-F35-2: Every fleet-invariant key required by batch 1b is present in each rendered coworker config.yaml with the value the fixture spec dictates, so a missing key or wrong value fails the render assertion"""
    _, loaded = _load(tmp_path, monkeypatch)
    exp = _expected()
    rendered = _render(loaded, tmp_path / "out")
    for name in _coworker_profiles(exp):
        cfg = _config(rendered[name])
        for dotted, want in exp["coworker_config"].items():
            found, got = _dig(cfg, dotted)
            assert found, f"{name}: missing config key {dotted}"
            assert got == want, f"{name}: {dotted} == {got!r}, expected {want!r}"
        assert "create_dir" not in cfg.get("skills", {}), "skills.create_dir is a phantom key"


def test_ac_loop_f35_3(tmp_path, monkeypatch):
    """AC-LOOP-F35-3: The rendered DEFAULT multiplexer profile config carries gateway.multiplex_profiles true, multiplex_profile_allowlist naming the roster, sessions.auto_prune false"""
    _, loaded = _load(tmp_path, monkeypatch)
    exp = _expected()
    rendered = _render(loaded, tmp_path / "out")
    cfg = _config(rendered[exp["default_profile"]])
    for dotted, want in exp["default_config"].items():
        found, got = _dig(cfg, dotted)
        assert found, f"DEFAULT profile missing {dotted}"
        assert got == want, f"DEFAULT {dotted} == {got!r}, expected {want!r}"


def test_ac_loop_f35_4(tmp_path, monkeypatch):
    """AC-LOOP-F35-4: The extends chain resolves so a child type's identity is the leaf value while its invariants, context, skills, workflows, overlays, trait bindings are the de-duplicated union of the chain, reflected in the rendered artifacts"""
    _, loaded = _load(tmp_path, monkeypatch)
    exp = _expected()["extends"]
    rendered = _render(loaded, tmp_path / "out")
    child = Path(rendered[exp["child_type"]])
    soul = (child / "SOUL.md").read_text(encoding="utf-8")
    cfg = _config(child)
    assert exp["expected_identity"] in soul, "leaf identity not rendered into child SOUL.md"
    assert exp["shadowed_parent_identity"] not in soul, "parent identity survived the leaf-wins override"
    for marker in exp["expected_context_contains"]:
        assert soul.count(marker) == 1, f"context {marker!r} not de-duplicated to exactly once"
    for inv in exp["expected_invariants_contains"]:
        assert soul.count(inv) == 1, f"invariant {inv!r} not de-duplicated to exactly once"
    skill_dirs = [p for p in (child / "skills").iterdir() if p.is_dir()]
    names = [p.name for p in skill_dirs]
    assert len(names) == len(set(names)), "skills not deduped"
    assert set(exp["expected_skills_superset"]) <= set(names), "skills union incomplete"
    assert set(exp["expected_workflows"]) <= set(names), "workflow union incomplete"
    bodies = " ".join((p / "SKILL.md").read_text(encoding="utf-8") for p in skill_dirs if (p / "SKILL.md").exists())
    for ov in exp["expected_overlays"]:
        assert bodies.count(ov) == 1, f"overlay {ov!r} not de-duplicated to exactly once"
    tb_key, tb_val = exp["expected_trait_binding"]
    found, got = _dig(cfg, tb_key)
    assert found and got == tb_val, f"trait binding {tb_key} not applied ({got!r})"


@pytest.mark.parametrize(
    "gateway_url, probe, expect_reason",
    [
        ("ws://127.0.0.1:1/api/ws?token=t", "refuse", "connection_refused"),  # unreachable
        ("ws://127.0.0.1:1/api/ws", None, "no_credential"),                   # missing cred (local reject)
        ("ws://127.0.0.1:1/api/ws?token=bogus", "auth_reject", "auth_rejected"),  # invalid cred (auth boundary)
        ("ws://127.0.0.1:1/api/ws?internal=x", None, "forbidden_internal"),   # forbidden (local reject)
    ],
)
def test_ac_loop_f35_6(tmp_path, monkeypatch, gateway_url, probe, expect_reason):
    """AC-LOOP-F35-6: hermes onboard coworker refuses after a read-only preflight, leaving zero profile mutations, for an unreachable gateway, a missing credential, an invalid credential, or a forbidden internal-credential URL"""
    home = _write_home(tmp_path, monkeypatch)
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_KEY]

    # Credential-form boundary: ?internal and form-invalid (no-credential) URLs are rejected
    # LOCALLY before any transport; a valid ?token URL passes the form check.
    assert loaded.module.validate_gateway_url("ws://h/api/ws?internal=x")[0] is False
    assert loaded.module.validate_gateway_url("ws://h/api/ws")[0] is False
    assert loaded.module.validate_gateway_url("ws://h/api/ws?token=t")[0] is True

    # Fake the transport BENEATH the real _gateway_preflight so the credential-recognition
    # code (form check + response mapping) is the code under test, not a mock of its verdict.
    def _fake_probe(url):
        if probe == "refuse":
            raise ConnectionError("refused")
        if probe == "auth_reject":
            return {"authed": False, "reason": "auth_rejected"}
        return {"authed": True}

    if probe is not None:
        monkeypatch.setattr(loaded.module, "_ws_probe", _fake_probe, raising=True)
    ok, reason = loaded.module._gateway_preflight(gateway_url)  # REAL preflight
    assert ok is False and reason == expect_reason, f"preflight mapped {gateway_url} to ({ok},{reason})"

    profiles_root = Path(home) / "profiles"
    watched = [Path(home) / "profile.yaml", Path(home) / "config.yaml"]

    def _snapshot():
        return (
            _tree_state(profiles_root),
            {str(p): (p.read_bytes() if p.exists() else None) for p in watched},
        )

    before = _snapshot()
    result = loaded.module.onboard_coworker(str(FIXTURE_SPEC), settings={"gateway_url": gateway_url})
    assert result.get("ok") is False
    assert result.get("error"), "a refusal must carry an error reason"
    if expect_reason == "auth_rejected":
        assert "auth" in result["error"].lower(), "auth rejection not distinctly reported"
    assert _snapshot() == before, f"onboard mutated profile-owned state despite a refused preflight ({gateway_url})"


def test_ac_loop_f35_7(tmp_path, monkeypatch):
    """AC-LOOP-F35-7: The plugin registers its full declared surface as one flat kind:standalone manifest, so its manifest_version is 1, its kind is standalone, each of the three CLI verbs parses, the two onboard slash commands are registered, the three orchestrator-gated gateway tools onboard_coworker, onboard_project, create_agent are registered, no runtime hook or middleware or system-prompt section is registered"""
    from tools.registry import registry

    manager, loaded = _load(tmp_path, monkeypatch)
    exp = _expected()
    assert loaded.manifest.name == PLUGIN_KEY
    assert (loaded.manifest.key or loaded.manifest.name) == PLUGIN_KEY
    assert loaded.manifest.manifest_version == 1
    assert loaded.manifest.kind == "standalone"
    assert not loaded.hooks_registered, "render is invoked, not event-driven — no hooks"
    assert not loaded.middleware_registered, "no middleware"
    sections = getattr(manager, "_system_prompt_sections", {}) or {}
    section_vals = sections.values() if isinstance(sections, dict) else sections
    assert not any(getattr(s, "plugin", None) == PLUGIN_KEY for s in section_vals), "no runtime prompt injection"
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="cmd")
    for verb in exp["cli_verbs"]:
        assert verb in manager._cli_commands, f"CLI verb {verb} not registered"
        manager._cli_commands[verb]["setup_fn"](sub.add_parser(verb))
    for argv in exp["cli_subcommands"]:
        assert parser.parse_args(argv) is not None, f"failed to parse {argv}"
    for slash in exp["slash_commands"]:
        assert slash in loaded.commands_registered, f"slash /{slash} not registered"
    for tool in exp["tools_orchestrator_only"]:
        assert registry.get_entry(tool, scope=manager.scope_key) is not None, f"tool {tool} not registered"


def test_ac_loop_f35_8(tmp_path, monkeypatch):
    """AC-LOOP-F35-8: Each webhook route declared in the spec round-trips verbatim into the DEFAULT profile config at platforms.webhook.extra.routes, keyed by route name, carrying its own secret plus profile binding"""
    _, loaded = _load(tmp_path, monkeypatch)
    exp = _expected()
    routes = exp["webhook_routes"]
    assert isinstance(routes, dict) and routes, "fixture must declare a non-empty webhook route mapping"
    rendered = _render(loaded, tmp_path / "out")
    cfg = _config(rendered[exp["default_profile"]])
    found, rendered_routes = _dig(cfg, exp["webhook_config_path"])
    assert found and isinstance(rendered_routes, dict), f"no route mapping at {exp['webhook_config_path']}"
    for name, route in routes.items():
        assert rendered_routes.get(name) == route, f"webhook route {name} not rendered verbatim"


def test_ac_loop_f35_9(tmp_path, monkeypatch):
    """AC-LOOP-F35-9: Installing a rendered distribution produces a profile whose .env.EXAMPLE exists while .env.template is gone"""
    from hermes_cli.profile_distribution import install_distribution

    _, loaded = _load(tmp_path, monkeypatch)
    exp = _expected()
    rendered = _render(loaded, tmp_path / "out")
    name = _coworker_profiles(exp)[0]
    plan = install_distribution(str(rendered[name]), name="loop-f35-installed", force=True)
    target = Path(plan.target_dir)
    assert (target / ".env.EXAMPLE").exists(), "installed profile lacks .env.EXAMPLE"
    assert not (target / ".env.template").exists(), ".env.template must not survive install"


def test_ac_loop_f36_1(tmp_path, monkeypatch):
    """AC-LOOP-F36-1: Each workflow body renders to skills/workflow/SKILL.md in every coworker profile that declares it"""
    _, loaded = _load(tmp_path, monkeypatch)
    exp = _expected()
    workflow = exp["workflow"]["name"]
    marker = exp["workflow"]["body_marker"]
    rendered = _render(loaded, tmp_path / "out")
    for name in exp["workflow"]["declared_by"]:
        skill_md = Path(rendered[name]) / "skills" / workflow / "SKILL.md"
        assert skill_md.is_file(), f"{name}: skills/{workflow}/SKILL.md not rendered"
        assert marker in skill_md.read_text(encoding="utf-8"), f"{name}: workflow body marker missing"


def test_ac_loop_f36_2(tmp_path, monkeypatch):
    """AC-LOOP-F36-2: Each coworker type gets a skill-bundles/type.yaml alias whose instruction prepend anchors the type's workflow entry"""
    _, loaded = _load(tmp_path, monkeypatch)
    exp = _expected()
    rendered = _render(loaded, tmp_path / "out")
    for btype, want in exp["bundles"].items():
        bundle = Path(rendered[btype]) / "skill-bundles" / (btype + ".yaml")
        assert bundle.is_file(), f"{btype}: skill-bundles alias not rendered"
        data = yaml.safe_load(bundle.read_text(encoding="utf-8"))
        assert set(want["skills"]) <= set(data.get("skills") or []), f"{btype}: bundle missing workflow skills"
        assert data.get("instruction") == want["instruction"], f"{btype}: bundle instruction != expected anchor"


def test_ac_loop_f36_3(tmp_path, monkeypatch):
    """AC-LOOP-F36-3: Overlays are spliced by applies-to against the type at render time, so the shipped SKILL.md contains the overlay text only for the matching type, never for a non-matching type"""
    _, loaded = _load(tmp_path, monkeypatch)
    exp = _expected()
    marker = exp["overlay"]["marker"]
    workflow = exp["workflow"]["name"]
    rendered = _render(loaded, tmp_path / "out")
    hit = (Path(rendered[exp["overlay"]["applies_to"]]) / "skills" / workflow / "SKILL.md").read_text(encoding="utf-8")
    miss = (Path(rendered[exp["overlay"]["not_applies_to"]]) / "skills" / workflow / "SKILL.md").read_text(encoding="utf-8")
    assert marker in hit, "overlay not spliced into the matching type at render time"
    assert marker not in miss, "overlay leaked into a non-matching type"


def test_ac_self_f54_1(tmp_path, monkeypatch):
    """AC-SELF-F54-1: Each orchestrator-gated gateway tool's check_fn returns True only for the orchestrator profile, present in the orchestrator's tool schema, absent from a worker profile's schema"""
    from tools.registry import registry

    exp = _expected()
    tools = exp["tools_orchestrator_only"]

    orch_mgr, _ = _load(tmp_path / "orch", monkeypatch, profile_name=exp["orchestrator_profile"])
    orch_names = {d["function"]["name"] for d in registry.get_definitions(set(tools))}
    for tool in tools:
        entry = registry.get_entry(tool, scope=orch_mgr.scope_key)
        assert entry is not None and entry.check_fn is not None, f"{tool} missing check_fn"
        assert entry.check_fn() is True, f"{tool} check_fn False for orchestrator"
        assert tool in orch_names, f"{tool} absent from orchestrator schema"

    worker_mgr, _ = _load(tmp_path / "worker", monkeypatch, profile_name=exp["worker_profile"])
    worker_names = {d["function"]["name"] for d in registry.get_definitions(set(tools))}
    for tool in tools:
        entry = registry.get_entry(tool, scope=worker_mgr.scope_key)
        assert entry is not None and entry.check_fn is not None
        assert entry.check_fn() is False, f"{tool} check_fn True for a worker profile"
        assert tool not in worker_names, f"{tool} present in a worker's schema"


def test_ac_self_f54_2(tmp_path, monkeypatch):
    """AC-SELF-F54-2: The create_agent tool handler dispatches a single profiles.create call carrying the requested profile fields"""
    from tools.registry import registry

    manager, loaded = _load(tmp_path, monkeypatch)
    exp = _expected()
    calls = []
    monkeypatch.setattr(
        loaded.module, "_dispatch_rpc",
        lambda method, params: calls.append((method, dict(params))) or {"ok": True},
        raising=True,
    )
    entry = registry.get_entry("create_agent", scope=manager.scope_key)
    entry.handler(dict(exp["create_agent_fields"]))
    creates = [c for c in calls if c[0] == "profiles.create"]
    assert len(creates) == 1, f"expected exactly one profiles.create dispatch, got {len(creates)}"
    for key, val in exp["create_agent_fields"].items():
        assert creates[0][1].get(key) == val, f"profiles.create missing {key}={val!r}"


def test_ac_self_f56_2(tmp_path, monkeypatch):
    """AC-SELF-F56-2: onboard coworker dispatches one groups.create per declared standing room carrying its member profile set, a second onboard re-dispatching the same room ids without a duplicate create"""
    _, loaded = _load(tmp_path, monkeypatch)
    exp = _expected()
    created_rooms = set()
    calls = []

    def _rpc(method, params):
        calls.append((method, dict(params)))
        if method == "groups.create":
            created_rooms.add(params.get("room_id") or params.get("name"))
            return {"ok": True}
        if method == "groups.state":
            rid = params.get("room_id") or params.get("name")
            return {"exists": rid in created_rooms, "room_id": rid, "disbanded": False}
        return {"ok": True, "applied": {"ui_meta": True}}

    monkeypatch.setattr(loaded.module, "_gateway_preflight", lambda url: (True, "ok"), raising=True)
    monkeypatch.setattr(loaded.module, "_dispatch_rpc", _rpc, raising=True)

    settings = {"gateway_url": "ws://127.0.0.1:9/api/ws?token=t"}
    loaded.module.onboard_coworker(str(FIXTURE_SPEC), settings=settings)
    for room, members in exp["rooms"].items():
        matching = [c for c in calls if c[0] == "groups.create" and (c[1].get("room_id") == room or c[1].get("name") == room)]
        assert len(matching) == 1, f"expected one groups.create for room {room}, got {len(matching)}"
        assert set(matching[0][1].get("members") or []) == set(members), f"room {room} members mismatch"

    loaded.module.onboard_coworker(str(FIXTURE_SPEC), settings=settings)  # second onboard: idempotent
    assert any(c[0] == "groups.state" for c in calls), "idempotency must consult groups.state"
    for room in exp["rooms"]:
        total = [c for c in calls if c[0] == "groups.create" and (c[1].get("room_id") == room or c[1].get("name") == room)]
        assert len(total) == 1, f"room {room} created twice — not idempotent"


def test_ac_self_f56_3(tmp_path, monkeypatch):
    """AC-SELF-F56-3: onboard coworker dispatches one profiles.configure per profile carrying a ui_meta hermes-bots blob whose title, description, shape match the spec, with the current ui_meta_expected_revisions, never a raw profile.yaml write, leaving a competing ui_meta key intact"""
    home = _write_home(tmp_path, monkeypatch)
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_KEY]
    exp = _expected()
    coworkers = _coworker_profiles(exp)
    # Model live server state: a competing ui_meta key + per-key revisions the plugin
    # must read (via profiles.list), preserve, and CAS against — never a raw file write.
    server = {p: {"ui_meta": {"custom-badge": {"emoji": "star"}},
                  "ui_meta_revisions": {"custom-badge": 7, "hermes-bots": 3}} for p in coworkers}
    configures = {}

    def _rpc(method, params):
        if method == "profiles.list":
            return {"profiles": [{"name": p, "ui_meta": server[p]["ui_meta"],
                                  "ui_meta_revisions": server[p]["ui_meta_revisions"]} for p in coworkers]}
        if method == "profiles.configure":
            name = params["name"]
            configures.setdefault(name, []).append(dict(params))
            exp_rev = params.get("ui_meta_expected_revisions") or {}
            cur = server[name]["ui_meta_revisions"]
            # Enforce CAS: expected revision must match the current one, per key.
            for key, rev in exp_rev.items():
                assert cur.get(key) == rev, f"{name}: stale CAS for {key} ({rev} != {cur.get(key)})"
            server[name]["ui_meta"].update(params.get("ui_meta") or {})       # key-wise merge
            for key in (params.get("ui_meta") or {}):
                cur[key] = cur.get(key, 0) + 1                                # revision advances
            return {"applied": {"ui_meta": True}, "ui_meta_revisions": cur}
        return {"ok": True}

    monkeypatch.setattr(loaded.module, "_gateway_preflight", lambda url: (True, "ok"), raising=True)
    monkeypatch.setattr(loaded.module, "_dispatch_rpc", _rpc, raising=True)

    loaded.module.onboard_coworker(str(FIXTURE_SPEC), settings={"gateway_url": "ws://127.0.0.1:9/api/ws?token=t"})
    for p in coworkers:
        assert len(configures.get(p, [])) == 1, f"{p}: expected exactly one profiles.configure"
        params = configures[p][0]
        assert params.get("ui_meta_expected_revisions") == {"hermes-bots": 3}, f"{p}: wrong/absent CAS precondition"
        assert set(params["ui_meta"].keys()) == {"hermes-bots"}, f"{p}: must write only its own key"
        # The written BotMeta must carry the spec's title/description/shape — not just the key,
        # else an empty/wrong blob would still pass (the desktop tier only consumes these values).
        written = params["ui_meta"]["hermes-bots"]
        for field, value in exp["bot_meta"][p].items():
            assert written.get(field) == value, (
                f"{p}: BotMeta {field} mismatch ({written.get(field)!r} != {value!r})"
            )
        assert "custom-badge" in server[p]["ui_meta"], f"{p}: competing ui_meta key was clobbered"
        assert server[p]["ui_meta_revisions"]["hermes-bots"] == 4, f"{p}: hermes-bots revision did not advance"
        # No out-of-band raw write: the installed profile.yaml (if any) carries no ui_meta.hermes-bots.
        pfile = Path(home) / "profiles" / p / "profile.yaml"
        if pfile.exists():
            disk = yaml.safe_load(pfile.read_text(encoding="utf-8")) or {}
            assert "hermes-bots" not in (disk.get("ui_meta") or {}), f"{p}: ui_meta.hermes-bots written raw to profile.yaml"


def test_ac_self_f56_4(tmp_path, monkeypatch):
    """AC-SELF-F56-4: hermes onboard project against a local path or a URL emits a project distribution scaffold containing distribution.yaml, a SOUL.md spine, skills/capability/SKILL.md, mcp.json, while a bounded scan keeps an excluded path out of the scaffold"""
    _, loaded = _load(tmp_path, monkeypatch)
    exp = _expected()
    excluded = exp["project_scan"]["excluded_marker"]
    max_files = exp["project_scan"]["max_files"]
    markers = ["SCAN-MARK-1", "SCAN-MARK-2", "SCAN-MARK-3"]  # > max_files eligible inputs

    repo = tmp_path / "sample-repo"
    (repo / "src").mkdir(parents=True)
    for i, mark in enumerate(markers):
        (repo / f"mod_{i}.py").write_text(f"# {mark}\n", encoding="utf-8")
    (repo / (excluded + ".bin")).write_bytes(b"\x00" * 1024)  # binary the scan must skip

    # URL source resolves through the same code via a clone seam pointed at the local repo.
    monkeypatch.setattr(loaded.module, "_clone_repo", lambda url: str(repo), raising=True)

    for source in (str(repo), "https://example.com/demo/sample-repo.git"):
        out = tmp_path / ("dist-" + ("local" if source == str(repo) else "url"))
        result = loaded.module.onboard_project(
            source, settings={"scan": "offline", "out": str(out), "max_files": max_files}
        )
        assert result.get("ok") is True, f"onboard_project failed for {source}"
        dist_dir = Path(result["distribution"])
        assert (dist_dir / "distribution.yaml").is_file()
        assert (dist_dir / "SOUL.md").is_file()
        assert (dist_dir / "mcp.json").is_file()
        assert list((dist_dir / "skills").glob("*/SKILL.md")), "no skills/capability/SKILL.md emitted"
        scaffold_text = " ".join(p.read_text(encoding="utf-8", errors="ignore") for p in dist_dir.rglob("*") if p.is_file())
        present = [m for m in markers if m in scaffold_text]
        assert len(present) == max_files, f"bounded scan surfaced {len(present)} of {len(markers)} files, expected {max_files}"
        assert excluded not in scaffold_text, "excluded binary leaked into the scaffold"
