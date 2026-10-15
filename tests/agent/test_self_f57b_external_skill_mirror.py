"""SELF-F57.b — hourly external-skill fetch mirrored into every bot.

These are proof/regression guards for the stock skills.external_dirs mirror mechanism:
they pass on an unmodified tree because the capability is native (this is a CONFIGURE
row, not a plugin — there is no plugin to be absent, hence no red->green asymmetry).
Behaviour contract, not enumeration counts; no network; nothing written under ~/.hermes.
"""

import argparse
import os
import textwrap
from pathlib import Path

import tools.skills_tool as skills_tool
from tools.skills_tool import _find_all_skills
from agent.skill_utils import _external_dirs_cache_clear


def _write_skill(root: Path, name: str, description: str) -> Path:
    """Create <root>/<name>/SKILL.md with the minimal frontmatter the scanner accepts.

    (name + description are the only fields discovery consumes — tools/skills_tool.py.)
    """
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\nBody.\n",
        encoding="utf-8",
    )
    return skill_dir


def _make_home(tmp_path: Path, name: str, external_dirs) -> Path:
    """An isolated HERMES_HOME whose on-disk config.yaml lists the given external_dirs.

    Skills discovery reads config.yaml straight off disk (agent/skill_utils.py:402), so a
    hermetic test writes the file rather than constructing a config object. project
    discovery is disabled and the local skills dir is left empty so only external dirs
    contribute; membership is still asserted by unique name, never by count.
    """
    home = tmp_path / name
    (home / "skills").mkdir(parents=True, exist_ok=True)
    (home / "scripts").mkdir(parents=True, exist_ok=True)
    (home / "cron").mkdir(parents=True, exist_ok=True)
    ext_lines = "\n".join(f"    - {Path(p)}" for p in external_dirs)
    (home / "config.yaml").write_text(
        "skills:\n  project_discovery: false\n  external_dirs:\n" + ext_lines + "\n",
        encoding="utf-8",
    )
    return home


def _reset_skill_caches() -> None:
    skills_tool._SKILLS_CACHE.clear()
    _external_dirs_cache_clear()


def test_ac_self_f57_b_1(tmp_path, monkeypatch):
    """A SKILL.md in a configured skills.external_dirs directory is discovered; one in an unconfigured directory is not.

    Proves skills.external_dirs is a real, consulted reader at its exact dotted path (not
    an inert key) and that the config gates discovery (negative control -> non-vacuous).
    """
    shared = tmp_path / "shared-skills"
    shared.mkdir()
    outside = tmp_path / "unconfigured-skills"
    outside.mkdir()
    _write_skill(shared, "self-f57b-inside", "inside external_dirs")
    _write_skill(outside, "self-f57b-outside", "not configured")

    home = _make_home(tmp_path, "bot", [shared])
    monkeypatch.setenv("HERMES_HOME", str(home))
    _reset_skill_caches()

    names = {s["name"] for s in _find_all_skills()}
    assert "self-f57b-inside" in names, "external-dir skill was not discovered"
    assert "self-f57b-outside" not in names, "unconfigured-dir skill leaked into discovery"


def test_ac_self_f57_b_2(tmp_path, monkeypatch):
    """One shared external directory feeds every bot: two profiles both discover a skill placed there once.

    This is the "mirrored into every bot" fan-out: no per-bot copy, one directory read by
    every profile.
    """
    shared = tmp_path / "shared-skills"
    shared.mkdir()
    _write_skill(shared, "self-f57b-shared", "shared skill")

    home_a = _make_home(tmp_path, "bot-a", [shared])
    home_b = _make_home(tmp_path, "bot-b", [shared])

    discovered = {}
    for label, home in (("a", home_a), ("b", home_b)):
        monkeypatch.setenv("HERMES_HOME", str(home))
        _reset_skill_caches()  # _SKILLS_CACHE is module-level, keyed by view not by home
        names = {s["name"] for s in _find_all_skills()}
        discovered[label] = "self-f57b-shared" in names

    assert discovered["a"] and discovered["b"], (
        "one shared external dir did not feed both profiles: " + repr(discovered)
    )


def test_ac_self_f57_b_3(tmp_path, monkeypatch):
    """A skill edited in place in an external dir is picked up within the 30 s index-cache TTL, without a restart.

    An in-place SKILL.md edit bumps only the file mtime, which the scan signature (root +
    immediate-child dir mtimes, tools/skills_tool.py:107-137) cannot see — so the refresh
    is bounded by _SKILLS_CACHE_TTL_SECONDS (30 s, tools/skills_tool.py:102). We drive the
    cache clock (time.monotonic, tools/skills_tool.py:728) to prove the bound deterministically.
    """
    shared = tmp_path / "shared-skills"
    shared.mkdir()
    skill_dir = _write_skill(shared, "self-f57b-ttl", "v1")

    home = _make_home(tmp_path, "bot", [shared])
    monkeypatch.setenv("HERMES_HOME", str(home))
    _reset_skill_caches()

    clock = {"t": 1000.0}
    monkeypatch.setattr(skills_tool.time, "monotonic", lambda: clock["t"])

    # Prime the cache at t=1000: description is "v1".
    primed = {s["name"]: s for s in _find_all_skills()}
    assert primed["self-f57b-ttl"]["description"] == "v1"

    # In-place edit -> "v2". Preserve dir mtimes so the scan signature stays identical
    # (editing a file does not change parent-dir mtime; restore is defensive).
    root_before = shared.stat()
    child_before = skill_dir.stat()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: self-f57b-ttl\ndescription: v2\n---\n\n# self-f57b-ttl\n\nBody.\n",
        encoding="utf-8",
    )
    os.utime(shared, (root_before.st_atime, root_before.st_mtime))
    os.utime(skill_dir, (child_before.st_atime, child_before.st_mtime))

    # Just under the 30 s bound (age 29 s): signature unchanged -> still cached "v1".
    clock["t"] = 1029.0
    within = {s["name"]: s for s in _find_all_skills()}
    assert within["self-f57b-ttl"]["description"] == "v1"

    # At the 30 s bound (age 30 s, not < 30): cache is stale -> rescan surfaces "v2",
    # no restart, no clear. Pins the exact TTL, not a looser 31-40 s window.
    clock["t"] = 1030.0
    after = {s["name"]: s for s in _find_all_skills()}
    assert after["self-f57b-ttl"]["description"] == "v2"


def test_ac_self_f57_b_4(tmp_path, monkeypatch):
    """The documented hourly --no-agent cron job runs its refresh script with the LLM skipped and provider credentials scrubbed by default, mirroring a skill a profile then discovers.

    The documented command parses (schedule positional), persists as a durable interval-60
    no_agent job, runs through run_job()'s no_agent short-circuit (cron/scheduler.py:5513-5530)
    with no agent constructed and provider credentials scrubbed from the subprocess env, and
    the SKILL.md the script writes into the shared external dir becomes discoverable.
    """
    import cron.scheduler as scheduler
    from cron.jobs import create_job, get_job, use_cron_store
    from hermes_cli.subcommands.cron import build_cron_parser

    shared = tmp_path / "shared-skills"
    shared.mkdir()
    home = _make_home(tmp_path, "bot", [shared])
    monkeypatch.setenv("HERMES_HOME", str(home))
    # OPENAI_API_KEY is in the provider blocklist, so the sanitizer strips it from the
    # cron subprocess env by default — the script asserts it never sees it.
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-cron-script")

    # The refresh script stands in for the hourly git pull. It must resolve under the
    # active profile's HERMES_HOME/scripts/ (cron/scheduler.py:4338-4346).
    script = home / "scripts" / "refresh-skills.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import os
            from pathlib import Path
            if os.getenv("OPENAI_API_KEY"):
                raise SystemExit("provider credential leaked into cron script env")
            d = Path({str(shared)!r}) / "self-f57b-fetched"
            d.mkdir(parents=True, exist_ok=True)
            (d / "SKILL.md").write_text(
                "---\\nname: self-f57b-fetched\\ndescription: fetched\\n---\\n\\n# self-f57b-fetched\\n\\nBody.\\n",
                encoding="utf-8",
            )
            print("refreshed")
            """
        ),
        encoding="utf-8",
    )

    # The positional schedule is part of the documented CLI contract.
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_cron_parser(subparsers, cmd_cron=lambda *a, **k: None)
    ns = parser.parse_args(
        ["cron", "create", "every 1h", "--script", "refresh-skills.py", "--no-agent"]
    )
    assert ns.schedule == "every 1h"
    assert ns.script == "refresh-skills.py"
    assert ns.no_agent is True

    with use_cron_store(home):
        job = create_job(
            prompt=None,
            schedule=ns.schedule,
            script=ns.script,
            no_agent=ns.no_agent,
            name="refresh-external-skills",
        )
        reloaded = get_job(job["id"])
        assert reloaded["no_agent"] is True
        assert reloaded["script"] == "refresh-skills.py"
        assert reloaded["schedule"]["kind"] == "interval"
        assert reloaded["schedule"]["minutes"] == 60

        # Constructing an agent is a failure: the no_agent short-circuit must not reach that branch.
        import run_agent

        def _no_agent_allowed(*args, **kwargs):
            raise AssertionError("no_agent cron job must not construct an agent")

        monkeypatch.setattr(run_agent, "AIAgent", _no_agent_allowed, raising=False)

        ok, doc, _final, err = scheduler.run_job(reloaded)
        assert ok, f"no_agent run_job failed: {err}"
        assert "no_agent" in doc

    _reset_skill_caches()
    names = {s["name"] for s in _find_all_skills()}
    assert "self-f57b-fetched" in names, "the refresh script's skill did not mirror into the bot"
