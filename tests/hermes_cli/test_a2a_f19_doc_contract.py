"""Contract tests for the A2A-F19 documentation section."""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
DOC = _REPO / "website" / "docs" / "user-guide" / "features" / "hooks.md"
HEADING = "## Gating agent-to-agent sends: per-edge human approval"

# This row's delivered pytest join set (5 ids). The carrier's live id 3
# (the LOOP-F37 operator-hold scenario) is carrier-owned and NOT tabled by this
# adopt row, so the doc enumerates these five and documents the live behaviour
# descriptively — see the ADR's ## Carried criteria section.
AC_IDS = ["AC-A2A-F19-1", "AC-A2A-F19-2",
          "AC-A2A-F19-4", "AC-A2A-F19-5", "AC-A2A-F19-6"]

# (row locator, exact tag anchor, exact main anchor) — the anchors are the full
# backtick-wrapped strings from gap-matrix-evidence § A2A-F19; each must sit on one
# dual-cited row bound to its own label.
DUAL_CITES = [
    ("pre_tool_call",
     "website/docs/user-guide/features/hooks.md:529", "website/docs/user-guide/features/hooks.md:529"),
    ("Single source of truth", "tools/approval.py:1", "tools/approval.py:1"),
    ("register_approval_transport", "hermes_cli/plugins.py:1740", "hermes_cli/plugins.py:423"),
    ("request_tool_approval", "tools/approval.py:4166", "tools/approval.py:914"),
    ("_await_gateway_decision", "tools/approval.py:4549", "tools/approval_gateway_wait.py:105"),
    ("ApprovalRequest", "hermes_cli/approval_transport.py:43", "hermes_cli/approval_transport.py:41"),
    ("hermes -p", "tools/bot_mode_dm.py:687-688", "tools/bot_mode_dm.py:446"),
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
    """The A2A-F19 section exists in hooks.md."""
    assert _section().strip(), f"section {HEADING!r} not found in {DOC}"


def test_doc_lists_all_ac_ids():
    """The section enumerates this row's five delivered AC-A2A-F19 join ids."""
    section = _section()
    assert not [ac for ac in AC_IDS if ac not in section]


@pytest.mark.parametrize("locator, tag_anchor, main_anchor", DUAL_CITES)
def test_doc_dual_cites_exact_anchors(locator, tag_anchor, main_anchor):
    """Each ledger surface is on a row carrying its exact tag: and main: anchors."""
    rows = [ln for ln in _section().splitlines() if locator in ln]
    assert rows, f"surface {locator!r} not documented in the A2A-F19 section"
    assert any(f"tag: `{tag_anchor}`" in ln and f"main: `{main_anchor}`" in ln
               for ln in rows), (
        f"surface {locator!r} lacks its dual-cite tag: `{tag_anchor}` / main: `{main_anchor}`")


def test_every_citation_row_carries_both_labels():
    """Any line bearing tag: OR main: must bear BOTH — a lone label fails the control."""
    offenders = [ln for ln in _section().splitlines() if ("tag:" in ln) != ("main:" in ln)]
    assert not offenders, f"rows with a lone tag:/main: label: {offenders}"
