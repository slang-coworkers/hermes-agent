"""Contract test for the ISO-F10 documentation section (ADOPT-verify negative control).

This is the fail-on-base / pass-on-head merge-gate control: the section does not
exist on the base tree (so _section() is empty and the assertions FAIL), and it
exists once the builder adds it (so they PASS). NOT AC-numbered.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
DOC = _REPO / "website" / "docs" / "user-guide" / "docker.md"
HEADING = "## Per-session container isolation: the NanoClaw model, mapped (ISO-F10)"

AC_IDS = ["AC-ISO-F10-3", "AC-ISO-F10-4", "AC-ISO-F10-5", "AC-ISO-F10-6"]

# The full P8 gap scope that must never silently shrink while the contract stays green.
GAP_TERMS = [
    "inbound.db",
    "outbound.db",
    "exclusive host/container writer ownership",
    "even/odd sequence-number protocol",
    "in-container session runner",
    "runner-owned heartbeat",
    "P8",
    "upstream ask",
    "CORE-CHANGE",
]

# (row locator, exact tag anchor, exact main anchor) — the dual cites from
# gap-matrix-evidence § ISO-F10. Each must sit on one row bound to its own label,
# carrying BOTH the tag: and main: backtick-wrapped anchors.
DUAL_CITES = [
    ("container_persistent: false",
     "website/docs/user-guide/configuration.md:282", "website/docs/user-guide/configuration.md:284"),
    ("_session_isolation_enabled", "tools/terminal_tool.py:1372", "tools/terminal_tool.py:394"),
    ("_resolve_container_task_id", "tools/terminal_tool.py:1482", "tools/terminal_tool.py:399"),
    ("_session_scoped", "tools/environments/docker.py:929", "tools/environments/docker.py:495"),
    ("_docker_persistent_profile_scoped", "tools/terminal_tool.py:1408", "tools/terminal_tool.py:380"),
    ("one shared long-lived container",
     "website/docs/user-guide/docker.md:578", "website/docs/user-guide/docker.md:575"),
    ("create_environment",
     "website/docs/developer-guide/terminal-environment-plugin.md:19",
     "website/docs/developer-guide/terminal-environment-plugin.md:19"),
    ("TerminalEnvironmentProvider", "agent/terminal_env_provider.py:72", "agent/terminal_env_provider.py:20"),
    ("session_isolated_when_nonpersistent",
     "website/docs/developer-guide/terminal-environment-plugin.md:28",
     "website/docs/developer-guide/terminal-environment-plugin.md:28"),
    ("find_docker", "tools/environments/docker.py:309", "tools/environments/docker.py:209"),
]


def _section() -> str:
    assert DOC.is_file(), f"doc page missing: {DOC}"
    lines = DOC.read_text(encoding="utf-8").splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.strip() == HEADING), None)
    if start is None:
        return ""
    end = next((j for j in range(start + 1, len(lines))
                if lines[j].startswith("## ")), len(lines))
    return "\n".join(lines[start:end])


def test_doc_section_present():
    """The ISO-F10 section exists in docker.md."""
    assert _section().strip(), f"section {HEADING!r} not found in {DOC}"


def test_doc_lists_all_ac_ids():
    """The section enumerates every AC-ISO-F10 id (3..6)."""
    section = _section()
    assert not [ac for ac in AC_IDS if ac not in section]


def test_doc_discloses_the_p8_gap():
    """The CORE-CHANGE / P8 gap disclosure (two-DB mailbox, seq parity, heartbeat) is present."""
    section = _section()
    assert not [t for t in GAP_TERMS if t not in section]


@pytest.mark.parametrize("locator, tag_anchor, main_anchor", DUAL_CITES)
def test_doc_dual_cites_exact_anchors(locator, tag_anchor, main_anchor):
    """Each mapped surface is on a row carrying its exact tag: and main: anchors."""
    rows = [ln for ln in _section().splitlines() if locator in ln]
    assert rows, f"surface {locator!r} not documented in the ISO-F10 section"
    assert any(f"tag: `{tag_anchor}`" in ln and f"main: `{main_anchor}`" in ln
               for ln in rows), (
        f"surface {locator!r} lacks its dual-cite tag: `{tag_anchor}` / main: `{main_anchor}`")


def test_every_citation_row_carries_both_labels():
    """Any line bearing tag: OR main: must bear BOTH — a lone label fails the control."""
    offenders = [ln for ln in _section().splitlines() if ("tag:" in ln) != ("main:" in ln)]
    assert not offenders, f"rows with a lone tag:/main: label: {offenders}"
