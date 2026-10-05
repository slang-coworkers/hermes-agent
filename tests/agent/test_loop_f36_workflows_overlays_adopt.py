"""LOOP-F36 (ADOPT-verify) — stock Hermes provides the runtime substrate for workflows and overlays.

Acceptance contracts for AC-LOOP-F36-4..6. Each passes on the stock v2026.8.31
tree (the adopt claim) and carries a differential control so a one-line
regression flips it.
"""

from pathlib import Path

import pytest

import tools.skills_tool as skills_tool_module
from agent import skill_bundles
from agent.skill_bundles import build_bundle_invocation_message, scan_bundles
from agent.skill_commands import (
    build_skill_invocation_message,
    resolve_skill_command_key,
    scan_skill_commands,
)


def _make_skill(skills_dir: Path, name: str, body: str = "Do the thing.") -> Path:
    """Create a minimal skill directory with SKILL.md."""
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Description for {name}\n---\n\n# {name}\n\n{body}\n"
    )
    return skill_dir


def _make_bundle_yaml(
    bundles_dir: Path, slug: str, skills: list[str],
    description: str = "", instruction: str = "", name: str | None = None,
) -> Path:
    """Write a bundle YAML file."""
    bundles_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"name: {name if name is not None else slug}"]
    if description:
        lines.append(f"description: {description}")
    lines.append("skills:")
    for s in skills:
        lines.append(f"  - {s}")
    if instruction:
        lines.append("instruction: |")
        for ln in instruction.splitlines():
            lines.append(f"  {ln}")
    path = bundles_dir / f"{slug}.yaml"
    path.write_text("\n".join(lines) + "\n")
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated bundles dir + skills dir."""
    bundles_dir = tmp_path / "skill-bundles"
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    monkeypatch.setenv("HERMES_BUNDLES_DIR", str(bundles_dir))
    monkeypatch.setattr(skills_tool_module, "SKILLS_DIR", skills_dir)
    skill_bundles._bundles_cache = {}
    skill_bundles._bundles_cache_mtime = None
    return bundles_dir, skills_dir


def test_ac_loop_f36_4(env):
    """A skill is a reusable procedural workflow body invoked as a /slash command shared by the CLI and the gateway: a known skill slug resolves and loads its full body into the turn, and an unknown slug resolves to nothing."""
    _bundles_dir, skills_dir = env
    _make_skill(skills_dir, "loop-f36-flow",
                body="STEP ONE inspect the tree\nSTEP TWO write the plan")
    scan_skill_commands()

    assert resolve_skill_command_key("loop-f36-flow") == "/loop-f36-flow"
    msg = build_skill_invocation_message("/loop-f36-flow")
    assert msg is not None
    assert "STEP ONE inspect the tree" in msg
    assert "STEP TWO write the plan" in msg

    # Differential control: a slug that was never created resolves to nothing.
    # A name-blind resolver/loader (a plausible mutant) would return a body here.
    assert resolve_skill_command_key("loop-f36-absent") is None
    assert build_skill_invocation_message("/loop-f36-absent") is None


def test_ac_loop_f36_5(env):
    """A skill bundle composes several skill workflow bodies under one alias and prepends a shared author instruction — the overlay — above them; a bundle carrying no instruction composes the same members with no overlay line."""
    bundles_dir, skills_dir = env
    _make_skill(skills_dir, "loop-f36-a", body="BODY A CONTENT")
    _make_skill(skills_dir, "loop-f36-b", body="BODY B CONTENT")
    _make_bundle_yaml(bundles_dir, "loop-f36-team",
                      skills=["loop-f36-a", "loop-f36-b"],
                      instruction="Apply the LOOP-F36 review overlay.")
    # Same members as the team bundle, minus the instruction — isolates the
    # overlay from the member set for the differential below.
    _make_bundle_yaml(bundles_dir, "loop-f36-plain",
                      skills=["loop-f36-a", "loop-f36-b"])
    scan_bundles()

    result = build_bundle_invocation_message("/loop-f36-team")
    assert result is not None
    msg, loaded, missing = result
    assert loaded == ["loop-f36-a", "loop-f36-b"]
    assert missing == []
    assert "BODY A CONTENT" in msg and "BODY B CONTENT" in msg
    # The overlay is prepended ABOVE the composed bodies, in member order — an
    # append mutant or a member-reorder mutant flips this ordering (index() also
    # asserts the overlay line is present).
    assert (msg.index("Bundle instruction: Apply the LOOP-F36 review overlay.")
            < msg.index("BODY A CONTENT") < msg.index("BODY B CONTENT"))

    # Differential control: the same-member bundle with no instruction composes
    # the SAME members with NO overlay line — a mutant that drops a member only
    # when the instruction is absent flips loaded2; one that always prepends the
    # overlay flips the "not in" assertion.
    plain = build_bundle_invocation_message("/loop-f36-plain")
    assert plain is not None
    msg2, loaded2, _missing2 = plain
    assert loaded2 == ["loop-f36-a", "loop-f36-b"]
    assert "BODY A CONTENT" in msg2 and "BODY B CONTENT" in msg2
    assert "Bundle instruction:" not in msg2


def test_ac_loop_f36_6():
    """/plan is a first-class built-in shared by the CLI and gateway (not a skill that can fall off a capped command menu) whose generated turn is planning-only — no code, no mutating commands — and instructs saving a markdown plan under .hermes/plans/; an unknown command resolves to nothing."""
    from agent.plan_prompt import build_plan_prompt
    from hermes_cli.commands import (
        GATEWAY_KNOWN_COMMANDS,
        resolve_command,
        telegram_bot_commands,
    )

    cmd = resolve_command("plan")
    assert cmd is not None
    assert cmd.name == "plan"
    assert cmd.cli_only is False
    assert ".hermes/plans/" in cmd.description
    assert "without executing" in cmd.description
    assert "plan" in GATEWAY_KNOWN_COMMANDS
    assert "plan" in {n for n, _ in telegram_bot_commands(include_plugins=False)}

    # build_plan_prompt returns the generated turn's instructions (it performs no
    # write), so assert the planning-only contract + durable save target it mandates
    # rather than an observed file.
    prompt = build_plan_prompt("port LOOP-F36")
    assert "PLAN MODE" in prompt
    assert "Do not implement code" in prompt
    assert "Do not run mutating terminal commands" in prompt
    assert ".hermes/plans/" in prompt

    # Differential control: a command that was never registered resolves to
    # nothing — a resolver returning a truthy default would flip this.
    assert resolve_command("loop-f36-not-a-command") is None
