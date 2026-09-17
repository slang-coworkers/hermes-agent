"""Acceptance test for SELF-F57 — skill engine + zero-downtime refresh (Adopt).

Adopt-track row: the capability already ships in stock Hermes v2026.8.31, so
this test *passes on the stock tree* — it proves Hermes does it, and is NOT a
plugin red→green test. It drives the shipped functions directly (no
PluginManager). Non-vacuity is carried by in-test negative controls: an
unconfigured dir is never scanned (AC-1); a stale/unlisted file must vanish/not
land and user bytes + a sibling profile must survive (AC-3/AC-4).

Hermetic: disk + config only, no network/git. AC-1/AC-2 read the real
``skills.external_dirs`` config; AC-3/AC-4/AC-5 install and update a profile from
a LOCAL-directory distribution source (``_stage_source`` takes a local dir with
no git), all under a tmp HERMES_HOME.

Behaviour contracts cited (tag v2026.8.31 @29112bef):
  tools/skills_tool.py:101 _SKILLS_CACHE, :102 _SKILLS_CACHE_TTL_SECONDS,
    :107 _skills_scan_signature, :687 _find_all_skills
  agent/skill_utils.py:533 get_external_skills_dirs (config skills.external_dirs)
  hermes_cli/profile_distribution.py:563 _copy_dist_payload, :662
    install_distribution, :705 update_distribution, :101 USER_OWNED_EXCLUDE
"""

import pytest

import agent.skill_utils as su
import tools.skills_tool as st


def _write_skill(base, name, description="a skill"):
    """External-dir layout: a skill directory directly under `base`."""
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n",
        encoding="utf-8",
    )
    return d


def _set_external_dirs(hermes_home, dirs):
    (hermes_home / "config.yaml").write_text(
        "skills:\n  external_dirs:\n"
        + "".join(f"    - {d}\n" for d in dirs),
        encoding="utf-8",
    )
    su._external_dirs_cache_clear()


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "skills").mkdir(parents=True)
    (home / "config.yaml").write_text("skills:\n  external_dirs: []\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    su._external_dirs_cache_clear()
    return home


@pytest.fixture(autouse=True)
def _fresh_skills_cache(monkeypatch, hermes_home):
    st._SKILLS_CACHE.clear()
    monkeypatch.setattr(st, "_skills_dir", lambda: hermes_home / "skills")
    monkeypatch.setattr(st, "_get_disabled_skill_names", lambda: set())
    monkeypatch.setattr(
        "agent.skill_utils.get_project_skills_dirs", lambda: [], raising=False
    )
    yield
    st._SKILLS_CACHE.clear()
    su._external_dirs_cache_clear()


def _names(skills):
    return {s["name"] for s in skills}


def test_ac_self_f57_1(tmp_path, hermes_home):
    """A new skill added to a scanned skills directory is returned by the next
    scan with no cache clear and no restart; a skill in an unconfigured dir is
    never returned."""
    scanned = tmp_path / "external-skills"
    scanned.mkdir()
    unscanned = tmp_path / "not-configured"
    unscanned.mkdir()
    _write_skill(scanned, "skill-one")
    _write_skill(unscanned, "skill-negative")
    _set_external_dirs(hermes_home, [scanned])

    first = st._find_all_skills()
    assert "skill-one" in _names(first)
    assert "skill-negative" not in _names(first)

    # Adding a skill *directory* bumps the scan-root signature, so the live
    # index picks it up on the next call without any _SKILLS_CACHE.clear().
    _write_skill(scanned, "skill-two")
    second = st._find_all_skills()
    assert {"skill-one", "skill-two"} <= _names(second)
    assert "skill-negative" not in _names(second)


def test_ac_self_f57_2(tmp_path, hermes_home, monkeypatch):
    """An in-place SKILL.md content edit is served from cache until the index
    TTL elapses, then reflected on the next scan — bounded, no restart."""
    scanned = tmp_path / "external-skills"
    scanned.mkdir()
    skill_md = _write_skill(scanned, "skill-one", description="old-desc") / "SKILL.md"
    _set_external_dirs(hermes_home, [scanned])

    ttl = st._SKILLS_CACHE_TTL_SECONDS
    assert 0 < ttl <= 30.0
    clock = {"t": 1000.0}
    monkeypatch.setattr(st.time, "monotonic", lambda: clock["t"])

    def _desc(skills):
        return [s for s in skills if s["name"] == "skill-one"][0]["description"]

    assert _desc(st._find_all_skills()) == "old-desc"

    # In-place content edit bumps only the file mtime, which is not part of the
    # directory signature — so the change is bounded by the TTL, not immediate.
    skill_md.write_text(
        "---\nname: skill-one\ndescription: new-desc\n---\n# skill-one\n",
        encoding="utf-8",
    )

    clock["t"] = 1000.0 + ttl / 2
    assert _desc(st._find_all_skills()) == "old-desc"

    clock["t"] = 1000.0 + ttl
    assert _desc(st._find_all_skills()) == "new-desc"


# --- Profile-distribution update behavior ----------------------------------

from hermes_cli.profile_distribution import (  # noqa: E402
    install_distribution,
    read_manifest,
    update_distribution,
)


def _write_dist(root, *, name="testdist", version, soul, mcp, config, skill_desc, cron,
                owned=("SOUL.md", "config.yaml", "mcp.json", "skills", "cron")):
    """A local-directory distribution. README.md is deliberately unlisted: with an
    explicit `owned` allowlist it is never shipped; with `owned=None` (manifest
    omits distribution_owned) the legacy copy-all-minus-USER_OWNED_EXCLUDE branch
    ships it."""
    (root / "skills" / "dist-skill").mkdir(parents=True, exist_ok=True)
    (root / "cron").mkdir(parents=True, exist_ok=True)
    manifest = f"name: {name}\nversion: {version}\ndescription: test\n"
    if owned is not None:
        manifest += "distribution_owned:\n" + "".join(f"  - {p}\n" for p in owned)
    (root / "distribution.yaml").write_text(manifest, encoding="utf-8")
    (root / "SOUL.md").write_text(soul, encoding="utf-8")
    (root / "mcp.json").write_text(mcp, encoding="utf-8")
    (root / "config.yaml").write_text(config, encoding="utf-8")
    (root / "skills" / "dist-skill" / "SKILL.md").write_text(
        f"---\nname: dist-skill\ndescription: {skill_desc}\n---\n# dist-skill\n",
        encoding="utf-8",
    )
    (root / "cron" / "job.json").write_text(cron, encoding="utf-8")
    (root / "README.md").write_text("readme\n", encoding="utf-8")


def test_ac_self_f57_3(tmp_path, hermes_home):
    """A distribution update replaces distribution-owned files (SOUL.md,
    mcp.json, skills/, cron/) with the new distribution's content."""
    dist = tmp_path / "dist"
    _write_dist(dist, version="1.0.0", soul="soul-v1", mcp='{"m": 1}',
                config="model: dist-v1", skill_desc="skill-v1", cron='{"c": 1}')
    profile = install_distribution(str(dist), name="testdist").target_dir

    assert not (profile / "README.md").exists()
    # A stale owned entry in the profile must vanish when the dir is replaced.
    _write_skill(profile / "skills", "stale-skill")

    _write_dist(dist, version="2.0.0", soul="soul-v2", mcp='{"m": 2}',
                config="model: dist-v2", skill_desc="skill-v2", cron='{"c": 2}')
    update_distribution("testdist", force_config=False)

    assert (profile / "SOUL.md").read_text(encoding="utf-8") == "soul-v2"
    assert (profile / "mcp.json").read_text(encoding="utf-8") == '{"m": 2}'
    assert "skill-v2" in (profile / "skills" / "dist-skill" / "SKILL.md").read_text(encoding="utf-8")
    assert not (profile / "skills" / "stale-skill").exists()
    assert (profile / "cron" / "job.json").read_text(encoding="utf-8") == '{"c": 2}'
    assert not (profile / "README.md").exists()
    # The version is recorded by rewriting distribution.yaml — always written,
    # outside the allowlist by design.
    assert (profile / "distribution.yaml").exists()
    assert read_manifest(profile).version == "2.0.0"


def test_ac_self_f57_4(tmp_path, hermes_home):
    """The same update leaves user-owned data (.env, state.db, memories/,
    sessions/) byte-for-byte untouched and does not disturb a sibling profile."""
    dist = tmp_path / "dist"
    _write_dist(dist, version="1.0.0", soul="soul-v1", mcp='{"m": 1}',
                config="model: dist-v1", skill_desc="skill-v1", cron='{"c": 1}')
    profile = install_distribution(str(dist), name="testdist").target_dir

    (profile / ".env").write_text("USER_SECRET=keep-me\n", encoding="utf-8")
    (profile / "auth.json").write_text('{"token": "keep"}\n', encoding="utf-8")
    (profile / "state.db").write_text("user-db-bytes\n", encoding="utf-8")
    (profile / "memories" / "note.md").write_text("remember this\n", encoding="utf-8")
    (profile / "sessions" / "s1.json").write_text('{"turns": 3}\n', encoding="utf-8")

    sib_dist = tmp_path / "sib"
    _write_dist(sib_dist, name="sibling", version="1.0.0", soul="sibling-soul",
                mcp='{"m": 9}', config="model: sib", skill_desc="s", cron='{"c": 9}')
    sibling = install_distribution(str(sib_dist), name="sibling").target_dir
    sib_soul_before = (sibling / "SOUL.md").read_text(encoding="utf-8")

    _write_dist(dist, version="2.0.0", soul="soul-v2", mcp='{"m": 2}',
                config="model: dist-v2", skill_desc="skill-v2", cron='{"c": 2}')
    update_distribution("testdist", force_config=False)

    assert (profile / ".env").read_text(encoding="utf-8") == "USER_SECRET=keep-me\n"
    assert (profile / "auth.json").read_text(encoding="utf-8") == '{"token": "keep"}\n'
    assert (profile / "state.db").read_text(encoding="utf-8") == "user-db-bytes\n"
    assert (profile / "memories" / "note.md").read_text(encoding="utf-8") == "remember this\n"
    assert (profile / "sessions" / "s1.json").read_text(encoding="utf-8") == '{"turns": 3}\n'
    assert (sibling / "SOUL.md").read_text(encoding="utf-8") == sib_soul_before


def test_ac_self_f57_5(tmp_path, hermes_home):
    """config.yaml is preserved on update by default and replaced only when the
    distribution config is forced (--force-config → preserve_config=False)."""
    dist = tmp_path / "dist"
    _write_dist(dist, version="1.0.0", soul="soul-v1", mcp='{"m": 1}',
                config="model: dist-v1", skill_desc="skill-v1", cron='{"c": 1}')
    profile = install_distribution(str(dist), name="testdist").target_dir

    (profile / "config.yaml").write_text("model: user-override\n", encoding="utf-8")
    _write_dist(dist, version="2.0.0", soul="soul-v2", mcp='{"m": 2}',
                config="model: dist-v2\n", skill_desc="skill-v2", cron='{"c": 2}')

    update_distribution("testdist", force_config=False)
    assert (profile / "config.yaml").read_text(encoding="utf-8") == "model: user-override\n"

    update_distribution("testdist", force_config=True)
    assert (profile / "config.yaml").read_text(encoding="utf-8") == "model: dist-v2\n"


def test_ac_self_f57_6(tmp_path, hermes_home):
    """A distribution that omits `distribution_owned` preserves user-owned data
    via USER_OWNED_EXCLUDE (the default-preservation branch) while shipping
    unlisted non-user-owned payload files."""
    dist = tmp_path / "dist"
    _write_dist(dist, owned=None, version="1.0.0", soul="soul-v1", mcp='{"m": 1}',
                config="model: dist-v1", skill_desc="skill-v1", cron='{"c": 1}')
    profile = install_distribution(str(dist), name="testdist").target_dir

    # No allowlist: the legacy branch copies every staged entry except
    # USER_OWNED_EXCLUDE, so the unlisted README.md DOES land (contrast AC-3).
    assert (profile / "README.md").exists()

    (profile / ".env").write_text("USER_SECRET=keep-me\n", encoding="utf-8")
    (profile / "state.db").write_text("user-db-bytes\n", encoding="utf-8")
    (profile / "memories" / "note.md").write_text("remember this\n", encoding="utf-8")

    _write_dist(dist, owned=None, version="2.0.0", soul="soul-v2", mcp='{"m": 2}',
                config="model: dist-v2", skill_desc="skill-v2", cron='{"c": 2}')
    # Staged v2 carries CONFLICTING user-owned entries so the exclusion is
    # load-bearing: without USER_OWNED_EXCLUDE these would overwrite user data.
    (dist / ".env").write_text("DIST_SECRET=overwrite\n", encoding="utf-8")
    (dist / "state.db").write_text("distribution-db\n", encoding="utf-8")
    (dist / "memories").mkdir(exist_ok=True)
    (dist / "memories" / "note.md").write_text("forget this\n", encoding="utf-8")
    (dist / "README.md").write_text("readme-v2\n", encoding="utf-8")
    update_distribution("testdist", force_config=False)

    assert (profile / ".env").read_text(encoding="utf-8") == "USER_SECRET=keep-me\n"
    assert (profile / "state.db").read_text(encoding="utf-8") == "user-db-bytes\n"
    assert (profile / "memories" / "note.md").read_text(encoding="utf-8") == "remember this\n"
    assert (profile / "SOUL.md").read_text(encoding="utf-8") == "soul-v2"
    # The unlisted payload is refreshed by the update (not just install).
    assert (profile / "README.md").read_text(encoding="utf-8") == "readme-v2\n"
