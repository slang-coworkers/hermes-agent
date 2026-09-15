"""Acceptance tests for MEM-F43 — shared learnings and learnings wiki.

MEM-F43 is a CONFIGURE (adopt-Hermes-native) row: the fleet's learnings live in one git
"learnings clone" whose skills/ subtree is surfaced into every coworker profile via
Hermes' stock ``skills.external_dirs``, and whose contents are synthesised by the bundled
``llm-wiki`` skill under an orchestrator-only ``WIKI_PATH`` cron. The render/activation
extends the existing in-surface ``nv-coworker-compose`` plugin.

AC-1..6 mirror tests/agent/test_external_skills.py; AC-7 (render) and AC-8 (activation)
mirror tests/plugins/test_mem_f44_retention_acceptance.py. Assertions read RAW rendered
config.yaml (yaml.safe_load), never load_config() — a deep-merged effective config would
report a default-valued key present even if never written. The test asserts behaviour,
never source text.
"""

import json
import shutil
from pathlib import Path

import pytest
import yaml


# ── Shared helpers ──────────────────────────────────────────────────────────

def _make_skill(skills_dir: Path, name: str, *, description: str, body: str) -> Path:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n{body}\n",
        encoding="utf-8",
    )
    return skill_dir


def _write_config(home: Path, external_dirs) -> None:
    (home / "config.yaml").write_text(
        yaml.safe_dump({"skills": {"external_dirs": [str(d) for d in external_dirs]}}),
        encoding="utf-8",
    )


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    (h / "skills").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(h))
    from agent.skill_utils import _external_dirs_cache_clear
    _external_dirs_cache_clear()
    return h


def _bind_local_skills(monkeypatch, home: Path) -> Path:
    # tests/agent/test_external_skills.py idiom: point the module SKILLS_DIR at the temp
    # local dir so the hermetic scan resolves to it.
    local = home / "skills"
    monkeypatch.setattr("tools.skills_tool.SKILLS_DIR", local, raising=False)
    monkeypatch.setattr("tools.skill_manager_tool.SKILLS_DIR", local, raising=False)
    return local



def test_ac_mem_f43_1(home, monkeypatch):
    """AC-MEM-F43-1: A shared-learnings skill placed in one directory that a profile lists in `skills.external_dirs` (and has no local copy of) is discoverable across that profile's surfaces: it appears in the skills catalog (`_find_all_skills`) and as a `/skill-name` slash command (`scan_skill_commands`), and `skill_view` returns its body."""
    ext = home.parent / "learnings-clone" / "skills"
    _make_skill(ext, "fleet-postmortem",
                description="Fleet postmortem learning",
                body="UNIQUE_BODY_SENTINEL_ppm about writing postmortems.")
    _write_config(home, [ext])
    _bind_local_skills(monkeypatch, home)

    from agent.skill_utils import _external_dirs_cache_clear
    _external_dirs_cache_clear()

    from tools.skills_tool import _find_all_skills, skill_view
    from agent.skill_commands import scan_skill_commands

    names = [s["name"] for s in _find_all_skills()]
    assert "fleet-postmortem" in names, "external skill absent from the discovery catalog"

    commands = scan_skill_commands()
    assert "/fleet-postmortem" in commands, "external skill absent from the slash-command set"

    viewed = json.loads(skill_view("fleet-postmortem"))
    assert viewed["success"] is True
    assert "UNIQUE_BODY_SENTINEL_ppm" in viewed["content"], "skill_view did not return the body"



def test_ac_mem_f43_2(home, monkeypatch):
    """AC-MEM-F43-2: The shared skill's one-line index entry (name + description) is rendered into the skills **system prompt**, while its **body** stays out of the prompt (loaded on demand) — i.e. "appears in the index, not injected"."""
    ext = home.parent / "learnings-clone" / "skills"
    _make_skill(ext, "fleet-postmortem",
                description="Fleet postmortem learning",
                body="UNIQUE_BODY_SENTINEL_ppm about writing postmortems.")
    _write_config(home, [ext])
    local = _bind_local_skills(monkeypatch, home)

    from agent.skill_utils import _external_dirs_cache_clear
    _external_dirs_cache_clear()

    from agent.prompt_builder import build_skills_system_prompt
    prompt = build_skills_system_prompt(skills_dir_override=local)

    assert "fleet-postmortem" in prompt, "index entry (name) absent from the skills system prompt"
    assert "Fleet postmortem learning" in prompt, "index entry (description) absent from the prompt"
    assert "UNIQUE_BODY_SENTINEL_ppm" not in prompt, "skill body was injected into the system prompt"



def test_ac_mem_f43_3(home, monkeypatch):
    """AC-MEM-F43-3: A newly-created skill resolves to the profile-local `$HERMES_HOME/skills` tree, never into a configured `external_dirs` entry, while that external dir remains a discovery source — so a non-promoting bot's newly-authored learnings never land in the shared clone via the create path."""
    ext = home.parent / "learnings-clone" / "skills"
    _make_skill(ext, "fleet-postmortem", description="d", body="b")
    _write_config(home, [ext])
    local = _bind_local_skills(monkeypatch, home)

    from agent.skill_utils import _external_dirs_cache_clear, get_external_skills_dirs
    _external_dirs_cache_clear()

    from tools.skill_manager_tool import _resolve_skill_dir
    target = _resolve_skill_dir("brand-new-skill")
    assert str(target).startswith(str(local)), "new skill did not resolve profile-local"
    assert str(ext) not in str(target), "new skill resolved into an external_dirs entry"

    assert ext.resolve() in get_external_skills_dirs()



def test_ac_mem_f43_4(tmp_path, monkeypatch):
    """AC-MEM-F43-4: One shared external directory feeds multiple profiles with no per-bot copy: two profiles with distinct `HERMES_HOME`, each listing the SAME external dir, both discover the identical skill and neither holds a local copy of it."""
    shared = tmp_path / "learnings-clone" / "skills"
    _make_skill(shared, "fleet-postmortem", description="d", body="b")

    def _catalog_for(home_name):
        h = tmp_path / home_name
        (h / "skills").mkdir(parents=True)
        _write_config(h, [shared])
        monkeypatch.setenv("HERMES_HOME", str(h))
        monkeypatch.setattr("tools.skills_tool.SKILLS_DIR", h / "skills", raising=False)
        from agent.skill_utils import _external_dirs_cache_clear
        _external_dirs_cache_clear()
        from tools.skills_tool import _find_all_skills
        names = [s["name"] for s in _find_all_skills()]
        local_copy = (h / "skills" / "fleet-postmortem").exists()
        return names, local_copy

    names_a, local_a = _catalog_for("profile-a")
    names_b, local_b = _catalog_for("profile-b")

    assert "fleet-postmortem" in names_a and "fleet-postmortem" in names_b
    assert not local_a and not local_b, "a profile held a per-bot local copy of the shared skill"



def test_ac_mem_f43_5(tmp_path, monkeypatch):
    """AC-MEM-F43-5: Cross-fleet isolation: two profiles configured with DIFFERENT `external_dirs` entries discover disjoint external skills (fleet A's skill is absent from fleet B's catalog)."""
    fleet_a = tmp_path / "clone-a" / "skills"
    fleet_b = tmp_path / "clone-b" / "skills"
    _make_skill(fleet_a, "alpha-only", description="d", body="b")
    _make_skill(fleet_b, "beta-only", description="d", body="b")

    def _catalog_for(home_name, ext):
        h = tmp_path / home_name
        (h / "skills").mkdir(parents=True)
        _write_config(h, [ext])
        monkeypatch.setenv("HERMES_HOME", str(h))
        monkeypatch.setattr("tools.skills_tool.SKILLS_DIR", h / "skills", raising=False)
        from agent.skill_utils import _external_dirs_cache_clear
        _external_dirs_cache_clear()
        from tools.skills_tool import _find_all_skills
        return [s["name"] for s in _find_all_skills()]

    names_a = _catalog_for("profile-a", fleet_a)
    names_b = _catalog_for("profile-b", fleet_b)

    assert "alpha-only" in names_a and "beta-only" not in names_a
    assert "beta-only" in names_b and "alpha-only" not in names_b



def test_ac_mem_f43_6(home, monkeypatch):
    """AC-MEM-F43-6: External-dir skills are NOT natively write-protected against foreground edits: a `skill_manage` patch of an existing external skill writes it in place in the external dir — establishing that shared-clone write-protection is delegated to filesystem permissions + the LOOP-F37 veto, not to `external_dirs`/config. (derived)"""
    ext = home.parent / "learnings-clone" / "skills"
    skill_dir = _make_skill(ext, "shared-runbook",
                            description="d", body="OLD_MARKER step to replace.")
    _write_config(home, [ext])
    _bind_local_skills(monkeypatch, home)

    from agent.skill_utils import _external_dirs_cache_clear
    _external_dirs_cache_clear()

    from tools.skill_manager_tool import skill_manage
    result = json.loads(skill_manage(
        action="patch", name="shared-runbook",
        old_string="OLD_MARKER", new_string="NEW_MARKER",
    ))
    assert result.get("success") is True, f"patch failed: {result}"

    on_disk = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    assert "NEW_MARKER" in on_disk and "OLD_MARKER" not in on_disk, (
        "external skill was NOT edited in place — external_dirs unexpectedly write-protected"
    )



PLUGIN_KEY = "nv-coworker-compose"
PLUGIN_SRC = Path(__file__).resolve().parents[2] / "plugins" / PLUGIN_KEY
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "mem-f43"
CLONE = "/data/shared-learnings"          # host learnings clone (fixture shared_learnings_root)
WIKI_MOUNT = "/mnt/shared-learnings"  # container mount path (OUTSIDE /workspace — a ":/workspace"
                                      # substring would trip workspace_explicitly_mounted, docker.py:998)
PRE_EXISTING_EXTERNAL = "/data/some-other-skills"   # the base spine's own external_dirs entry


def _load_compose(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
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

    from hermes_cli.plugins import PluginManager
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled is True and loaded.error is None and loaded.module is not None
    return loaded


def _raw_config(profile_dir):
    return yaml.safe_load((Path(profile_dir) / "config.yaml").read_text(encoding="utf-8"))


def _dig(cfg, *keys):
    node = cfg
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return None
        node = node[k]
    return node


def _default_profile_name(rendered):
    for name, pdir in rendered.items():
        if _dig(_raw_config(pdir), "gateway", "multiplex_profiles") is True:
            return name
    raise AssertionError("no rendered profile carries gateway.multiplex_profiles: true")


def test_ac_mem_f43_7(tmp_path, monkeypatch):
    """AC-MEM-F43-7: The `nv-coworker-compose` render enforces, on every coworker (type) profile (the multiplexer/DEFAULT profile is the gateway root, out of the coworker-learnings scope): `<clone>/skills` is a member of `skills.external_dirs` (inserted when the spec omits it, preserving pre-existing entries when it conflicts); `WIKI_PATH` is in `terminal.env_passthrough`; and the clone is mounted via `terminal.docker_volumes` at a stable container path — read-write for the orchestrator, read-only for the other coworkers (the defence-in-depth mount)."""
    loaded = _load_compose(tmp_path, monkeypatch)
    want_ext = f"{CLONE}/skills"
    mount_rw = f"{CLONE}:{WIKI_MOUNT}"
    mount_ro = f"{CLONE}:{WIKI_MOUNT}:ro"
    # The mount target must stay outside /workspace, else docker.py:998 (":/workspace" in vol)
    # suppresses the auto cwd->/workspace bind for the coworker.
    assert ":/workspace" not in mount_rw
    for i, spec_name in enumerate(("coworker-types.yaml", "coworker-types-omitted.yaml")):
        rendered = loaded.module.compose(str(FIXTURE_DIR / spec_name), str(tmp_path / f"out-{i}"))
        default_name = _default_profile_name(rendered)
        coworkers = [n for n in rendered if n != default_name]
        assert coworkers, f"{spec_name}: no coworker rendered"
        for name in coworkers:
            cfg = _raw_config(rendered[name])
            dirs = [str(d) for d in (_dig(cfg, "skills", "external_dirs") or [])]
            assert want_ext in dirs, f"{spec_name}:{name} external_dirs missing {want_ext}: {dirs}"
            if spec_name == "coworker-types.yaml":
                assert PRE_EXISTING_EXTERNAL in dirs, (
                    f"{spec_name}:{name} render dropped the pre-existing external dir: {dirs}"
                )
            passthrough = _dig(cfg, "terminal", "env_passthrough") or []
            assert "WIKI_PATH" in passthrough, (
                f"{spec_name}:{name} terminal.env_passthrough missing WIKI_PATH: {passthrough}"
            )
            vols = [str(v) for v in (_dig(cfg, "terminal", "docker_volumes") or [])]
            if name == "orchestrator":
                assert mount_rw in vols, f"orchestrator missing rw clone mount {mount_rw}: {vols}"
            else:
                assert mount_ro in vols, f"{name} missing ro clone mount {mount_ro}: {vols}"


def test_ac_mem_f43_8(tmp_path, monkeypatch):
    """AC-MEM-F43-8: Effective (activated, not staged) wiki fold: `_run_onboard` invokes the activation over the installed profile homes (after install), and after it runs the ORCHESTRATOR profile's cron store holds exactly ONE enabled `llm-wiki` fold **agent** job (`no_agent is False`, `skills` includes `llm-wiki`, recurring `schedule.kind in {cron, interval}`, self-contained prompt naming the wiki mount, no synthetic cron `env` field), the orchestrator's `.env` carries `WIKI_PATH=<container mount path>` (upserted, other keys preserved), and after `sync_skills` the `llm-wiki` skill is loadable there via `skill_view` with the env_passthrough↔.env join resolving `WIKI_PATH` to the mount; every worker profile and the DEFAULT profile hold NO wiki job and NO `WIKI_PATH`; re-running activation is idempotent (same one job, stable `id`, `.env` not duplicated)."""
    loaded = _load_compose(tmp_path, monkeypatch)

    # Use rendered configs for direct activation; installed-path wiring is checked separately.
    rendered = loaded.module.compose(str(FIXTURE_DIR / "coworker-types.yaml"), str(tmp_path / "out"))
    default_name = _default_profile_name(rendered)
    orchestrator = "orchestrator"
    assert orchestrator in rendered and orchestrator != default_name
    homes = {name: Path(pdir) for name, pdir in rendered.items()}
    workers = [n for n in homes if n not in (orchestrator, default_name)]
    assert workers, "fixture must render at least one worker profile"

    (homes[orchestrator] / ".env").write_text("KEEP_ME=1\n", encoding="utf-8")

    from cron.jobs import use_cron_store, load_jobs
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    def _activate():
        loaded.module.activate_shared_learnings(
            profile_homes={k: str(v) for k, v in homes.items()},
            orchestrator=orchestrator, clone=CLONE,
        )

    def _wiki_jobs(profile_home):
        with use_cron_store(str(profile_home)):
            return [j for j in load_jobs() if "llm-wiki" in (j.get("skills") or [])]

    _activate()

    env_text = (homes[orchestrator] / ".env").read_text(encoding="utf-8")
    assert "KEEP_ME=1" in env_text, "activation clobbered pre-existing .env content"
    assert f"WIKI_PATH={WIKI_MOUNT}" in env_text, "orchestrator .env missing WIKI_PATH=<container mount>"

    orch_jobs = _wiki_jobs(homes[orchestrator])
    assert len(orch_jobs) == 1, f"orchestrator must hold exactly one wiki-fold job: {orch_jobs}"
    job = orch_jobs[0]
    assert job.get("enabled") is True
    assert job.get("no_agent") is False, "wiki fold must be an agent job (skills=[llm-wiki])"
    assert WIKI_MOUNT in (job.get("prompt") or ""), "fold prompt does not name the wiki mount"
    assert (job.get("schedule") or {}).get("kind") in {"cron", "interval"}, "wiki fold must be recurring"
    assert "env" not in job, "cron jobs carry no env field; WIKI_PATH belongs in .env"
    first_id = job.get("id")
    assert isinstance(first_id, str) and first_id, "cron job must have a non-empty string id"

    for name in workers + [default_name]:
        assert not _wiki_jobs(homes[name]), f"{name} profile unexpectedly holds a wiki job"
        env_path = homes[name] / ".env"
        assert not env_path.exists() or "WIKI_PATH=" not in env_path.read_text(encoding="utf-8"), (
            f"{name} profile unexpectedly received WIKI_PATH"
        )

    # llm-wiki must be loadable in the orchestrator home, and the env_passthrough allowlist
    # (render) must join the .env value (activation) so the sandbox resolves WIKI_PATH=<mount>.
    token = set_hermes_home_override(str(homes[orchestrator]))
    try:
        monkeypatch.setattr("tools.skills_tool.SKILLS_DIR", homes[orchestrator] / "skills",
                            raising=False)
        from tools.skills_tool import skill_view
        assert json.loads(skill_view("llm-wiki")).get("success") is True, (
            "llm-wiki not loadable in the orchestrator home"
        )

        import tools.env_passthrough as ep
        ep._config_passthrough = None  # drop the cached config allowlist so this home is read
        from agent.secret_scope import set_secret_scope, build_profile_secret_scope
        set_secret_scope(build_profile_secret_scope(homes[orchestrator]))
        try:
            assert "WIKI_PATH" in ep.get_all_passthrough(), "WIKI_PATH not allowlisted for the sandbox"
            assert ep.resolve_passthrough_value("WIKI_PATH") == WIKI_MOUNT, (
                "WIKI_PATH does not resolve to the container mount through the profile secret scope"
            )
        finally:
            set_secret_scope(None)
            ep._config_passthrough = None
    finally:
        reset_hermes_home_override(token)

    _activate()
    again = _wiki_jobs(homes[orchestrator])
    assert len(again) == 1 and again[0].get("id") == first_id, "activation is not idempotent"
    assert (homes[orchestrator] / ".env").read_text(encoding="utf-8").count("WIKI_PATH=") == 1, (
        "activation duplicated WIKI_PATH in .env"
    )

    # Isolate onboarding from host/RPC effects while verifying activation receives installed homes.
    from hermes_cli.profiles import get_profile_dir
    order, calls = [], []

    def _record_activation(profile_homes, orchestrator, clone):
        order.append("activate")
        calls.append((dict(profile_homes), orchestrator, clone))

    monkeypatch.setattr(loaded.module, "_install", lambda *a, **k: order.append("install"),
                        raising=False)
    monkeypatch.setattr(loaded.module, "_profile_revisions", lambda *a, **k: {}, raising=False)
    monkeypatch.setattr(loaded.module, "_configure_bot_meta", lambda *a, **k: True, raising=False)
    monkeypatch.setattr(loaded.module, "_ensure_canonical_bot_chat", lambda *a, **k: True, raising=False)
    monkeypatch.setattr(loaded.module, "_dispatch_rpc", lambda *a, **k: {}, raising=False)
    monkeypatch.setattr(loaded.module, "_room_exists", lambda *a, **k: True, raising=False)
    monkeypatch.setattr(loaded.module, "activate_shared_learnings", _record_activation, raising=False)
    result = loaded.module._run_onboard(str(FIXTURE_DIR / "coworker-types.yaml"))

    assert result == {"ok": True}, f"_run_onboard reported failures: {result}"
    assert len(calls) == 1, "_run_onboard did not invoke activate_shared_learnings exactly once"
    got_homes, got_orch, got_clone = calls[0]
    assert got_orch == orchestrator and got_clone == CLONE
    # Activation must receive EVERY installed profile home, and each must be the INSTALLED
    # profile dir — not compose()'s TemporaryDirectory (destroyed when _run_onboard exits,
    # which would lose the activated .env / skill / cron). Checking only the orchestrator
    # would let an implementation pass ephemeral or partial homes for workers/DEFAULT while
    # the direct-activation isolation guarantee (above) silently ceases to describe production.
    expected_homes = set(workers) | {orchestrator, default_name}
    assert set(got_homes) == expected_homes, (
        f"activation must receive every installed profile home: got {set(got_homes)}, want {expected_homes}"
    )
    for name in expected_homes:
        assert Path(got_homes[name]).resolve() == get_profile_dir(name).resolve(), (
            f"activation received a non-installed (ephemeral) home for {name}"
        )
    activate_idx = order.index("activate")
    assert "install" in order[:activate_idx], "activation ran before any install"
    assert "install" not in order[activate_idx + 1:], "an install ran after activation"
