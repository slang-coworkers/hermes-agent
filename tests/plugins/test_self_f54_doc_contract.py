"""Contract test for the SELF-F54 documentation page.

Reading the page is the intended mechanism for a doc deliverable's contract.
"""

from pathlib import Path

# tests/plugins/test_self_f54_doc_contract.py -> parents[2] is the repo root
DOC_PAGE = (
    Path(__file__).resolve().parents[2]
    / "website" / "docs" / "user-guide"
    / "self-modification-and-spawning-coworkers.md"
)


def test_self_f54_doc_contract():
    assert DOC_PAGE.is_file(), f"SELF-F54 doc page missing (expected at {DOC_PAGE})"
    text = DOC_PAGE.read_text(encoding="utf-8")

    # Agent-spawned-agents half: the stock primitive + the merged carrier surface.
    for surface in (
        "create_profile",
        "list_profiles",
        "nv-coworker-compose",
        "nv-fleet-gates",
        "create_agent",
    ):
        assert surface in text, f"doc must map the agent-spawn surface {surface!r}"

    # register_tool + check_fn (the orchestrator-gating mechanism).
    assert "check_fn" in text, "doc must cite the check_fn gating mechanism"
    assert "register_tool" in text, "doc must cite ctx.register_tool"

    # Self-modification tier-1 safety surfaces (the gap-matrix.md:70 three).
    for surface in (
        "write_file_tool",
        "protected_instruction_files",
        "write_approval",
        "stage_write",
        "self_repo_guard",
    ):
        assert surface in text, f"doc must map the self-mod safety surface {surface!r}"

    # Package install is contained by the sandbox, not approval-gated (not a gap).
    assert "sandbox" in text.lower(), "doc must state package-install sandbox containment"

    # A NanoClaw->Hermes mapping runbook carries dual tag:/main: citations, so a
    # keyword-only stub page cannot satisfy the contract.
    assert "tag:" in text and "main:" in text, "doc must carry tag:/main: citations"

    # Every acceptance-criterion id this requirement carries (agent-spawn 1-3 +
    # self-mod 4-5) must appear in the runbook mapping.
    for n in (1, 2, 3, 4, 5):
        assert f"AC-SELF-F54-{n}" in text, f"doc must carry acceptance-criterion id AC-SELF-F54-{n}"
