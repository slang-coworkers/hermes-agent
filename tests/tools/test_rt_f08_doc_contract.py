"""RT-F08 adopt-row negative control: the cross-session-context doc page must exist and cite its three surfaces (in tag:/main: form) and the AC ids (absent on base, present on head)."""

from pathlib import Path

# tests/tools/test_rt_f08_doc_contract.py -> parents[2] is the repo root
DOC_PAGE = (
    Path(__file__).resolve().parents[2]
    / "website" / "docs" / "user-guide" / "cross-session-context.md"
)


def test_rt_f08_doc_contract():
    assert DOC_PAGE.is_file(), f"RT-F08 doc page missing (expected at {DOC_PAGE})"
    text = DOC_PAGE.read_text(encoding="utf-8")

    # Each of the three mapped surfaces is cited.
    for cite in (
        # pull (session_search + its DB backing)
        "session_search", "search_messages", "hermes_state_search.py:1417", "tools/session_search_tool.py",
        # room transcript
        "hosted_rooms", "bot-mode.md",
        # kanban card
        "kanban_db", "kanban.md",
        # the no-fan-out trap
        "gateway/mirror.py",
    ):
        assert cite in text, f"doc must cite the mapped surface {cite!r}"

    # The dispatch mandates the tag:/main: citation form for evidence-backed cites.
    for cite in (
        "tag: website/docs/user-guide/sessions.md:11 | main: website/docs/user-guide/sessions.md:11",
        "tag: website/docs/user-guide/bot-mode.md:80 | main: website/docs/user-guide/bot-mode.md:91",
        "tag: gateway/mirror.py:115 | main: gateway/mirror.py:65",
        "tag: gateway/mirror.py:52 | main: gateway/mirror.py:25",
        "tag: tools/session_search_tool.py:1145 | main: tools/session_search_tool.py:562",
    ):
        assert cite in text, f"doc must carry the tag:/main: citation {cite!r}"

    # The acceptance-criterion ids (id-join with tester/reviewer/merge gate).
    for n in (1, 2, 3):
        assert f"AC-RT-F08-{n}" in text, f"doc must carry acceptance-criterion id AC-RT-F08-{n}"
