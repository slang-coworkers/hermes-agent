"""GOV-F26 own-diff negative control (RT-F09): the fleet-admin runbook must exist
and cite its seam surface and AC ids. Not AC-numbered, so the AC id <-> test-function
join stays 1:1."""

from pathlib import Path

# tests/hermes_cli/test_gov_f26_doc_contract.py -> parents[2] is the repo root
DOC_PAGE = (
    Path(__file__).resolve().parents[2]
    / "website" / "docs" / "user-guide" / "fleet-admin-scope.md"
)


def test_gov_f26_doc_contract():
    assert DOC_PAGE.is_file(), f"GOV-F26 doc page missing (expected at {DOC_PAGE})"
    text = DOC_PAGE.read_text(encoding="utf-8")

    for seam in ("_fleet_admin_block", "admin_tools", "profile_roles"):
        assert seam in text, f"runbook must cite the {seam!r} seam surface"

    assert "plugins/nv-fleet-gates/__init__.py" in text, (
        "runbook must cite the covering plugin source"
    )

    for n in (1, 2, 3):
        assert f"AC-GOV-F26-{n}" in text, f"runbook must carry acceptance-criterion id AC-GOV-F26-{n}"
