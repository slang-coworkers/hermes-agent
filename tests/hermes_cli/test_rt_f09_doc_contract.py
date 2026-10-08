"""RT-F09 adopt-row negative control: the router-seams doc page must exist and cite its seam surface and AC ids (absent on base, present on head)."""

import re
from pathlib import Path

# tests/hermes_cli/test_rt_f09_doc_contract.py -> parents[2] is the repo root
DOC_PAGE = (
    Path(__file__).resolve().parents[2]
    / "website" / "docs" / "developer-guide" / "plugins" / "router-seams.md"
)


def test_rt_f09_doc_contract():
    assert DOC_PAGE.is_file(), f"RT-F09 doc page missing (expected at {DOC_PAGE})"
    text = DOC_PAGE.read_text(encoding="utf-8")

    for hook in (
        "pre_gateway_dispatch",
        "pre_tool_call",
        "on_session_start",
        "on_session_end",
        "on_session_reset",
    ):
        assert hook in text, f"doc must cite the {hook!r} seam"

    assert "gateway/run.py:18141" in text, "doc must cite the pre_gateway_dispatch emit gateway/run.py:18141"
    assert re.search(r"(?:hermes_cli/)?plugins\.py:(?:6635|6831)\b", text), (
        "doc must cite the pre_tool_call production dispatch (plugins.py:6635 or :6831)"
    )

    for n in (1, 2, 3, 4):
        assert f"AC-RT-F09-{n}" in text, f"doc must carry acceptance-criterion id AC-RT-F09-{n}"
