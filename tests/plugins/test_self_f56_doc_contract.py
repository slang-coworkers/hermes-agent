"""Verify the required SELF-F56 onboarding documentation page."""

from pathlib import Path

DOC_PAGE = (
    Path(__file__).resolve().parents[2]
    / "website" / "docs" / "user-guide"
    / "onboarding-projects-and-coworkers.md"
)


def test_self_f56_doc_contract():
    assert DOC_PAGE.is_file(), f"SELF-F56 doc page missing (expected at {DOC_PAGE})"
    text = DOC_PAGE.read_text(encoding="utf-8")

    for surface in ("create_profile", "list_profiles", "install_distribution"):
        assert surface in text, f"doc must map the stock coworker-onboarding surface {surface!r}"

    assert "build_init_prompt_for_cwd" in text or "/init" in text, "doc must map the stock /init repo scan"
    assert "projects_cmd" in text or "hermes project create" in text, "doc must map hermes project create"

    for surface in ("nv-coworker-compose", "onboard_coworker", "onboard_project"):
        assert surface in text, f"doc must map the fleet onboarding surface {surface!r}"

    for surface in ("hermes-bots", "profiles.configure", "groups.create"):
        assert surface in text, f"doc must map the onboarding dispatch {surface!r}"

    assert "tag:" in text and "main:" in text, "doc must carry stock tag:/main: citations"
    assert "baseline:" in text, "doc must carry the baseline: citation for the fork carrier"

    for n in (2, 3, 4, 5, 6, 7):
        assert f"AC-SELF-F56-{n}" in text, f"doc must carry acceptance-criterion id AC-SELF-F56-{n}"
