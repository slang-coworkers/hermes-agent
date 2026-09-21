"""RT-F08 — Cross-session context: hermetic acceptance test.

Proves stock Hermes at v2026.8.31 provides cross-session context through three
native core surfaces (no plugin), one test per surface:

- AC-RT-F08-1 room transcript: a Bot Mode room is one shared ordered event log
  every member reads, so a member's turn is visible to another member, while
  members share the `Group: <room-id>` session title (gateway.hosted_rooms).
- AC-RT-F08-2 kanban card: a durable hand-off row survives a DB close/reopen and
  is read back intact by an independent reader (hermes_cli.kanban_db).
- AC-RT-F08-3 session_search pull: a query from one session surfaces content in a
  different session and excludes the searcher's own live session.

Adopt-row asymmetry: these pass on the stock pinned tree (Hermes already does
it); they are regression guards, NOT the negative control (see the standalone
tests/tools/test_rt_f08_doc_contract.py). Behavior contracts only — no model
lists, no version literals, no reading source files, no network, nothing under
~/.hermes (the autouse conftest sandbox pins HERMES_HOME to tmp_path).
"""

import json

from gateway import hosted_rooms
from hermes_cli import kanban_db as kb
from hermes_state import SessionDB
from tools.session_search_tool import session_search
from tui_gateway.hosted_room_driver import room_session_title

ROOM_ID = "room-1"
AUTHORITY = "gwtest"

# Distinct FTS/room tokens so hits are unambiguous.
A_TOKEN = "aardvarkalpha"
B_TOKEN = "penguinbeta"


def test_ac_rt_f08_1(tmp_path):
    """AC-RT-F08-1: the shared room transcript carries every member's turn for other members to read."""
    db = tmp_path / "state.db"
    hosted_rooms.create_room(
        db,
        room_id=ROOM_ID,
        name="Release room",
        members=[
            {"member_id": "hermes", "profile": "default", "handle": "hermes"},
            {"member_id": "ops", "profile": "ops", "handle": "ops"},
        ],
        authority_gateway_id=AUTHORITY,
    )
    for event_id, member, token in (("e1", "hermes", A_TOKEN), ("e2", "ops", B_TOKEN)):
        hosted_rooms.append_event(
            db,
            room_id=ROOM_ID,
            event_id=event_id,
            kind="message.member",
            actor={"kind": "member", "id": member},
            payload={"text": f"{token} turn", "member_id": member},
            authority_gateway_id=AUTHORITY,
            authority_epoch=1,
        )

    events = hosted_rooms.read_events(db, room_id=ROOM_ID)["events"]
    by_member = {
        e["payload"].get("member_id"): e["payload"].get("text")
        for e in events
        if e["kind"] == "message.member"
    }
    # Both members' turns live in the ONE shared log any member reads.
    assert by_member.get("hermes") == f"{A_TOKEN} turn"
    assert by_member.get("ops") == f"{B_TOKEN} turn"

    # Members address the room through the shared room-scoped session title.
    assert room_session_title(ROOM_ID) == f"Group: {ROOM_ID}"


def test_ac_rt_f08_2(tmp_path):
    """AC-RT-F08-2: a durable kanban hand-off card survives close/reopen and is read back by an independent reader."""
    db = tmp_path / "kanban.db"
    handoff = f"{A_TOKEN} hand off to reviewer: PR head ready for round-1 review"

    with kb.connect_closing(db) as writer:
        task_id = kb.create_task(
            writer,
            title="RT-F08 review hand-off",
            body=handoff,
            assignee="reviewer",
            created_by="builder",
        )

    # A fresh, independent connection — writer above is truly closed
    # (connect_closing, unlike sqlite3's transaction CM, closes the FD), so this
    # is a genuine close/reopen boundary: the shared board another profile reads.
    with kb.connect_closing(db) as reader:
        task = kb.get_task(reader, task_id)
        missing = kb.get_task(reader, "does-not-exist")

    assert task is not None
    assert task.title == "RT-F08 review hand-off"
    assert task.body == handoff
    assert task.assignee == "reviewer"
    assert task.created_by == "builder"
    # Differential: a real keyed lookup, not an always-return.
    assert missing is None


def test_ac_rt_f08_3(tmp_path):
    """AC-RT-F08-3: session_search performs a cross-session pull that excludes the searcher's own current session."""
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(session_id="s_a", source="cli")
        db.append_message("s_a", role="user", content=f"deployment note {A_TOKEN}")
        db.create_session(session_id="s_b", source="cli")
        db.append_message("s_b", role="user", content=f"config note {B_TOKEN}")

        # Working "in" session B, pull a token that lives only in session A.
        payload = json.loads(
            session_search(query=A_TOKEN, db=db, current_session_id="s_b")
        )
        assert payload["success"] is True
        assert payload["mode"] == "discover"
        assert "s_a" in {r["session_id"] for r in payload["results"]}

        # Differential: B's own token from B excludes B's current live lineage —
        # the pull genuinely crosses sessions, it is not a self-dump.
        payload_b = json.loads(
            session_search(query=B_TOKEN, db=db, current_session_id="s_b")
        )
        assert "s_b" not in {r["session_id"] for r in payload_b["results"]}
    finally:
        db.close()
