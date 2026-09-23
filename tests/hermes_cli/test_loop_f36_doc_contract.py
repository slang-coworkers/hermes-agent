"""Own-diff negative control for the LOOP-F36 doc section (ADOPT-verify).

The section does not exist on the base tree, so the content assertions fail there
and pass once it exists. Not AC-numbered, keeping the criterion<->test join
one-to-one; reading the doc here tests the doc, not core behaviour.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
DOC = _REPO / "website" / "docs" / "user-guide" / "features" / "skills.md"
HEADING = "## Workflows and overlays (durable multi-step loops): the NanoClaw model, mapped (LOOP-F36)"

# Criteria minted by this adopt row (AC-LOOP-F36-1..3 belong to the compose plugin).
AC_IDS = ["AC-LOOP-F36-4", "AC-LOOP-F36-5", "AC-LOOP-F36-6"]

# Required references to coverage supplied by the compose plugin.
INHERITED = [
    "AC-LOOP-F36-1",
    "AC-LOOP-F36-2",
    "AC-LOOP-F36-3",
    "nv-coworker-compose",
    "applies-to",
]

# Mapped code seams the section must name.
SEAM_PATHS = [
    "agent/skill_commands.py",
    "agent/skill_bundles.py",
    "agent/plan_prompt.py",
    "hermes_cli/commands.py",
]

# Residual gap that must never silently shrink while the contract stays green.
GAP_TERMS = [
    "/implement",
    "investigate/review/research",
    "LOOP-F36.a",
    "CONFIGURE",
    "BUILD",
]

# Tag anchors are exact; main anchors must use the same source path.
DUAL_CITES = [
    ("resolve_skill_command_key", "agent/skill_commands.py:645"),
    ("build_skill_invocation_message", "agent/skill_commands.py:664"),
    ("build_bundle_invocation_message", "agent/skill_bundles.py:253"),
    ("build_plan_prompt", "agent/plan_prompt.py:78"),
    ("resolve_command", "hermes_cli/commands.py:463"),
]


def _section() -> str:
    assert DOC.is_file(), f"host doc page missing: {DOC}"
    lines = DOC.read_text(encoding="utf-8").splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.strip() == HEADING), None)
    if start is None:
        return ""
    end = next((j for j in range(start + 1, len(lines))
                if lines[j].startswith("## ")), len(lines))
    return "\n".join(lines[start:end])


def test_doc_section_present():
    """The LOOP-F36 section exists in skills.md."""
    assert _section().strip(), f"section {HEADING!r} not found in {DOC}"


def test_doc_lists_new_ac_ids():
    """The section enumerates this row's new ids (AC-LOOP-F36-4..6)."""
    section = _section()
    assert not [ac for ac in AC_IDS if ac not in section]


def test_doc_references_inherited_coverage():
    """The section names the inherited render coverage (AC-1..3, nv-coworker-compose, applies-to)."""
    section = _section()
    assert not [t for t in INHERITED if t not in section]


def test_doc_names_every_seam():
    """The section names each mapped code seam the acceptance test drives."""
    section = _section()
    assert not [p for p in SEAM_PATHS if p not in section]


def test_doc_discloses_the_residual_gap():
    """The residual disclosure (multi-mode /plan, /implement, LOOP-F36.a) is present."""
    section = _section()
    assert not [t for t in GAP_TERMS if t not in section]


@pytest.mark.parametrize("symbol, tag_anchor", DUAL_CITES)
def test_doc_dual_cites_symbol_tag_and_main_form(symbol, tag_anchor):
    """Each required symbol is on a row with its exact tag: anchor and a same-path main: anchor."""
    tag_path = tag_anchor.rsplit(":", 1)[0]
    main_re = re.compile(rf"main: `{re.escape(tag_path)}:\d+`")
    rows = [ln for ln in _section().splitlines()
            if symbol in ln and f"tag: `{tag_anchor}`" in ln]
    assert rows, f"missing dual-cite row for {symbol} with tag: `{tag_anchor}`"
    assert any(main_re.search(ln) for ln in rows), (
        f"row for {symbol} lacks a main: `{tag_path}:<line>` anchor")


def test_dual_cite_label_parity():
    """Every backtick-cite row carries BOTH tag: and main:; counts match the required set."""
    lines = _section().splitlines()
    offenders = [ln for ln in lines if ("tag: `" in ln) != ("main: `" in ln)]
    assert not offenders, f"rows with a lone tag:/main: cite: {offenders}"
    tag_rows = [ln for ln in lines if "tag: `" in ln]
    main_rows = [ln for ln in lines if "main: `" in ln]
    assert len(tag_rows) == len(main_rows) >= len(DUAL_CITES)
