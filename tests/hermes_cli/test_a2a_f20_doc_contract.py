"""A2A-F20 own-diff negative control (NOT an AC-numbered test).

The deliverable under test is the ``## Reply-home lineage`` section added to
``website/docs/user-guide/bot-mode.md``. Reading the doc page here is the
sanctioned exception to "never read source in tests" — the doc IS the artifact
being verified. On the base tree the section is absent, so every assertion
below fails; on the head tree they all pass. This asymmetry is what makes it a
negative control for a zero-runtime adopt row.
"""

import re
from pathlib import Path

DOC = Path(__file__).resolve().parents[2] / "website" / "docs" / "user-guide" / "bot-mode.md"
HEADING = "## Reply-home lineage"

# The native reply-home seams the section must cite by path (the tag: side of
# each dual citation keeps these exact strings even where main moved the file).
SEAM_PATHS = (
    "tools/bot_mode_dm.py",
    "gateway/run.py",
    "gateway/wake.py",
    "plugins/platforms/a2a/adapter.py",
    "hermes_cli/kanban_db.py",
    "gateway/kanban_watchers.py",
)


def _section() -> str:
    """Slice the ``## Reply-home lineage`` section: from its heading to the next
    level-2 heading (``### `` subsections stay inside). Asserts the exact heading
    first so a base-tree failure is specifically the missing section."""
    text = DOC.read_text(encoding="utf-8")
    assert HEADING in text, f"{HEADING!r} section missing from {DOC}"
    start = text.index(HEADING)
    rest = text[start + len(HEADING):]
    m = re.search(r"\n## ", rest)
    end = start + len(HEADING) + (m.start() if m else len(rest))
    return text[start:end]


def test_reply_home_lineage_section_present():
    assert HEADING in DOC.read_text(encoding="utf-8"), f"{HEADING!r} missing from {DOC}"


def test_seam_citations_present():
    section = _section()
    for path in SEAM_PATHS:
        assert path in section, f"seam citation {path!r} missing from the Reply-home lineage section"


def test_acceptance_criteria_ids_present():
    section = _section()
    for n in range(1, 6):
        assert f"AC-A2A-F20-{n}" in section, f"AC-A2A-F20-{n} missing from the Reply-home lineage section"


def test_dual_tag_main_citations():
    section = _section()
    assert section.count("tag:") >= 3, "section must carry at least 3 tag: citations"
    assert section.count("main:") >= 3, "section must carry at least 3 main: citations"
