"""LOOP-F36.a (PORT-as-plugin) — multi-mode /plan + /implement as workflow-body skills.

Acceptance contracts AC-LOOP-F36.a-1..6, all in this file. The plugin
`nv-workflow-modes` ships the four residual workflow bodies (plan-investigate,
plan-review, plan-research, implement) and a `workflow-modes` CLI installer that
copies them into the scanned skills tree; each then auto-slashes and seeds a turn
with its body. AC-1..3 and AC-6 fail on the stock v2026.8.31 tree (plugin absent
→ explicit pytest.fail); AC-5 fails on base (doc section absent); AC-4 is the
ADOPT-boundary criterion, green on stock by design. test_ac_loop_f36_a_5 doubles
as the own-diff doc negative control.

Mirrors tests/hermes_cli/test_plugin_api_compat.py (isolated-HERMES_HOME load) and
tests/agent/test_skill_commands.py (scan_skill_commands + collision guard).
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import pytest
import yaml

import tools.skills_tool as skills_tool_module
from agent.skill_commands import (
    build_skill_invocation_message,
    get_skill_commands,
    resolve_skill_command_key,
    scan_skill_commands,
)
from hermes_cli.commands import resolve_command
from hermes_cli.plugins import PluginManager

_REPO = Path(__file__).resolve().parents[2]
PLUGIN_KEY = "nv-workflow-modes"
PLUGIN_SRC = _REPO / "plugins" / PLUGIN_KEY

# Each residual mode's shipped SKILL.md must be a mode-specific, multi-step
# workflow body (ADR §Design contract) — not a stub with an arbitrary marker.
# MODE_CONTRACTS lists the semantic tokens a faithful body for that mode must
# carry (case-insensitive); every mode body must also expose >= MIN_PHASES
# ordinal step markers. This forces real, distinguishable workflows.
MIN_PHASES = 3
MODE_CONTRACTS = {
    "plan-investigate": ("investigate", "read-only", "evidence", "report"),
    "plan-review": ("review", "read-only", "findings", "verdict"),
    "plan-research": ("research", "sources", "synthes", "uncertaint"),
    "implement": ("implement", "edit", "test", "validat"),
}
RESIDUAL_MODES = tuple(MODE_CONTRACTS)


def _phase_count(body: str) -> int:
    """Count genuine ordinal workflow phases: a numbered step with content, or a
    heading that is explicitly `Step N` / `Phase N`. A plain heading is NOT a
    phase — otherwise a title plus two arbitrary sections would look multi-step."""
    import re
    return len(re.findall(
        r"(?im)^[ \t]*(?:\d+[.)][ \t]+\S|#{1,4}[ \t]+(?:step|phase)[ \t]+\d+\b)",
        body,
    ))


def _require_plugin() -> None:
    """Stock-tree red: the whole port is absent until the plugin ships."""
    if not PLUGIN_SRC.exists():
        pytest.fail(f"covering-surface plugin missing (stock-tree red): {PLUGIN_SRC}")


def _load(tmp_path, monkeypatch, name="home"):
    """Discover+load nv-workflow-modes from an isolated HERMES_HOME."""
    _require_plugin()
    hermes_home = tmp_path / name
    (hermes_home / "plugins").mkdir(parents=True)
    bundled = tmp_path / f"bundled_empty_{name}"
    bundled.mkdir()
    shutil.copytree(PLUGIN_SRC, hermes_home / "plugins" / PLUGIN_KEY)
    cfg = {"plugins": {"enabled": [PLUGIN_KEY]}}
    (hermes_home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.delenv("HERMES_ENABLE_PROJECT_PLUGINS", raising=False)
    manager = PluginManager()
    manager.discover_and_load()
    return manager, hermes_home


def _run_installer(manager, *argv):
    """Drive the workflow-modes CLI subcommand through its registered setup_fn/handler_fn."""
    entry = manager._cli_commands["workflow-modes"]
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="cmd")
    entry["setup_fn"](sub.add_parser("workflow-modes"))
    ns = parser.parse_args(["workflow-modes", *argv])
    handler = entry.get("handler_fn") or getattr(ns, "func", None)
    assert handler is not None, "workflow-modes registered no handler"
    return handler(ns)


def test_ac_loop_f36_a_1(tmp_path, monkeypatch):
    """The nv-workflow-modes plugin loads from an isolated HERMES_HOME (enabled, no error, module present) and registers its workflow-modes CLI installer subcommand."""
    manager, _home = _load(tmp_path, monkeypatch)
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled is True
    assert loaded.error is None
    assert loaded.module is not None
    assert "workflow-modes" in manager._cli_commands


def test_ac_loop_f36_a_2(tmp_path, monkeypatch):
    """After install, each of the four residual modes auto-resolves as a /slash command, loads its full workflow body into the turn, and appears in the skill-command catalog; an unknown slug resolves to nothing."""
    manager, hermes_home = _load(tmp_path, monkeypatch)
    skills_dir = hermes_home / "skills"
    _run_installer(manager, "install")
    assert skills_dir.exists(), "installer did not create the skills tree"
    # The scanner reads tools.skills_tool.SKILLS_DIR; point it at the install dest.
    monkeypatch.setattr(skills_tool_module, "SKILLS_DIR", skills_dir)
    scan_skill_commands()
    catalog = get_skill_commands()

    # Guard the phase counter itself: plain headings are not phases, so a body
    # that is only a title + sections must count zero (else ≥3 below is vacuous).
    assert _phase_count("# Title\n## Notes\n### Output") == 0
    assert _phase_count("1.\nbody\n2)\nbody\n3.\nbody") == 0

    for mode, required in MODE_CONTRACTS.items():
        assert (skills_dir / mode / "SKILL.md").is_file(), f"installer missing {mode}"
        key = resolve_skill_command_key(mode)
        assert key == f"/{mode}", f"{mode} did not auto-slash (got {key!r})"
        assert key in catalog, f"/{mode} absent from the skill-command catalog"
        msg = build_skill_invocation_message(key)
        assert msg is not None, f"/{mode} did not build an invocation message"
        low = msg.lower()
        missing = [t for t in required if t not in low]
        assert not missing, f"/{mode} body omits required workflow tokens {missing}"
        assert _phase_count(msg) >= MIN_PHASES, f"/{mode} body is not a multi-step workflow"

    # Differential control: a slug that was never installed resolves to nothing.
    assert resolve_skill_command_key("plan-absent") is None
    assert build_skill_invocation_message("/plan-absent") is None


def _installed(tmp_path, monkeypatch):
    """Load the plugin, run its installer, point the scanner at the dest, scan."""
    manager, hermes_home = _load(tmp_path, monkeypatch)
    skills_dir = hermes_home / "skills"
    _run_installer(manager, "install")
    monkeypatch.setattr(skills_tool_module, "SKILLS_DIR", skills_dir)
    scan_skill_commands()
    return skills_dir


def test_ac_loop_f36_a_3(tmp_path, monkeypatch):
    """Invoking a mode skill with trailing text carries it into the body as the skill user_instruction (the mode is fixed by the slug); a skill named `plan` does NOT shadow the stock /plan builtin (core-command collision guard)."""
    skills_dir = _installed(tmp_path, monkeypatch)

    msg = build_skill_invocation_message("/plan-investigate", "MODEARG_TOKEN_XYZ")
    assert msg is not None and "MODEARG_TOKEN_XYZ" in msg

    # A `plan` skill must not claim /plan (the builtin wins) — the guard that forces
    # the /plan-<mode> alias divergence; a non-colliding name stays claimable.
    plan_skill = skills_dir / "plan"
    plan_skill.mkdir()
    (plan_skill / "SKILL.md").write_text(
        "---\nname: plan\ndescription: Shadow attempt.\n---\n\nShadow body.\n",
        encoding="utf-8",
    )
    scan_skill_commands()
    assert resolve_skill_command_key("plan") is None, "a plan skill shadowed the builtin"
    assert resolve_skill_command_key("plan-investigate") == "/plan-investigate"


def test_ac_loop_f36_a_4():
    """Residual boundary is honest: stock /plan is planning-only, stock /review exists as the independent-subagent capability (not a plan-review workflow body), and no /implement builtin exists on stock."""
    plan = resolve_command("plan")
    assert plan is not None and plan.name == "plan"
    assert ".hermes/plans/" in plan.description
    assert "without executing" in plan.description
    review = resolve_command("review")
    assert review is not None
    assert "independent subagent" in review.description.lower()
    assert resolve_command("implement") is None


# Doc page shipped by this plugin; the ## Equivalence section is absent on base,
# so the content assertions below fail there and pass once it exists (own-diff
# negative control). Reading the doc here tests the doc artifact, not core logic.
_DOC = _REPO / "website" / "docs" / "developer-guide" / "plugins" / "nv-workflow-modes.md"
_EQUIV_HEADING = "## Equivalence (NanoClaw plan-modes + /implement, mapped)"


def _equiv_section() -> str:
    if not _DOC.is_file():
        return ""
    lines = _DOC.read_text(encoding="utf-8").splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.strip() == _EQUIV_HEADING), None)
    if start is None:
        return ""
    end = next((j for j in range(start + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
    return "\n".join(lines[start:end])


def test_ac_loop_f36_a_5():
    """The doc page maps all four NanoClaw plan-modes + /implement onto the Hermes surface with an explicit ## Equivalence caveat (distinct /slash names with the mode in the slug and the topic passed as user_instruction, not the literal /plan <mode> token) and dual tag:/main: citations of the mapped seams."""
    import re

    assert _DOC.is_file(), f"plugin doc page missing: {_DOC}"
    section = _equiv_section()
    assert section.strip(), f"section {_EQUIV_HEADING!r} not found in {_DOC}"
    low = section.lower()

    for claim in (
        "/plan-investigate", "/plan-review", "/plan-research", "/implement",
        "nanoclaw", "user instruction",
    ):
        assert claim in low, f"equivalence section does not name: {claim!r}"
    assert "not the literal" in low or "does not reproduce" in low, "divergence not stated"
    assert "review" in low and ("independent" in low or "subagent" in low or "not equivalent" in low)
    assert re.search(r"(?im)^.*/implement.*(?:no stock equivalent|no .*builtin|none).*$", section), \
        "equivalence section does not map /implement as absent from stock"
    _seam = r"(?P<p>agent/skill_commands\.py|hermes_cli/commands\.py|agent/plan_prompt\.py)"
    assert re.search(rf"tag:\s*`{_seam}:\d+`[^\n]*\|\s*main:\s*`(?P=p):\d+`", section), \
        "equivalence section lacks a same-path tag:/main: dual citation of a mapped seam"


def _ok(rc) -> bool:
    """Installer success convention: prints output, returns None or 0."""
    return rc is None or rc == 0


def test_ac_loop_f36_a_6(tmp_path, monkeypatch, capsys):
    """The installer is profile-scoped, idempotent, and never clobbers user data: a first install copies+marks the four bodies and reports the reload step; a repeat install succeeds and preserves installed contents; a user-owned (unmarked) skill dir is refused atomically (its content preserved, no other mode written) unless --force replaces it."""
    manager, home = _load(tmp_path, monkeypatch)
    skills_dir = home / "skills"

    rc1 = _run_installer(manager, "install")
    assert _ok(rc1), f"first install failed (rc={rc1!r})"
    out = capsys.readouterr().out.lower()
    assert "/reload-skills" in out or "restart" in out, "install did not report the reload step"
    for mode in RESIDUAL_MODES:
        assert (skills_dir / mode / "SKILL.md").is_file(), f"missing {mode}"
        marker = skills_dir / mode / ".installed-by"
        assert marker.is_file() and PLUGIN_KEY in marker.read_text(encoding="utf-8"), \
            f"{mode} lacks the {PLUGIN_KEY} ownership marker"
    snapshot = {m: (skills_dir / m / "SKILL.md").read_text(encoding="utf-8")
                for m in RESIDUAL_MODES}

    rc2 = _run_installer(manager, "install")
    assert _ok(rc2), f"repeat install did not succeed over its own marked dirs (rc={rc2!r})"
    for mode in RESIDUAL_MODES:
        assert (skills_dir / mode / "SKILL.md").read_text(encoding="utf-8") == snapshot[mode]

    manager2, home2 = _load(tmp_path, monkeypatch, name="home2")
    skills2 = home2 / "skills"
    user_body = "---\nname: implement\ndescription: user owned.\n---\n\nDO NOT CLOBBER.\n"
    (skills2 / "implement").mkdir(parents=True)
    (skills2 / "implement" / "SKILL.md").write_text(user_body, encoding="utf-8")
    capsys.readouterr()
    rc3 = _run_installer(manager2, "install")
    assert not _ok(rc3), "install did not refuse a user-owned skill dir"
    assert (skills2 / "implement" / "SKILL.md").read_text(encoding="utf-8") == user_body
    for mode in ("plan-investigate", "plan-review", "plan-research"):
        assert not (skills2 / mode).exists(), f"refusal was not atomic — {mode} was written"

    rc4 = _run_installer(manager2, "install", "--force")
    assert _ok(rc4), f"--force install failed (rc={rc4!r})"
    forced = (skills2 / "implement" / "SKILL.md").read_text(encoding="utf-8")
    assert forced != user_body and "implement" in forced.lower()
    for mode in RESIDUAL_MODES:
        assert (skills2 / mode / ".installed-by").is_file(), f"{mode} unmarked after --force"
