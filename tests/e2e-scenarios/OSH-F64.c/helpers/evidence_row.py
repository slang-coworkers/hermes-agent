"""Pair one AC-OSH-F64-4 screenshot with its state.db row (stdlib sqlite3; the image has no sqlite3 CLI).

Usage: python3 evidence_row.py <profile> <session-id> <role> [tool_name] [needle]

<session-id> may be the canonical Bot Chat or its compression tip. The session line
"session <id> hidden=<0|1> title=<title>" names the canonical row: the nearest session up the
parent_session_id chain that is hidden and titled "Bot Chat" (the supplied id itself when it is).
Then the newest message in <session-id> with the given role (and tool_name, and content containing
needle, when given) prints as "row <msg-id> <role> <tool_name> <content prefix>", or "row none".
"""
import os
import sqlite3
import sys

profile, session_id, role = sys.argv[1:4]
tool_name = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] else None
needle = sys.argv[5] if len(sys.argv) > 5 else None

home = os.environ["HERMES_HOME"]
db_path = os.path.join(home, "state.db") if profile == "default" else os.path.join(home, "profiles", profile, "state.db")
db = sqlite3.connect(db_path)

canonical = None
current = session_id
for _ in range(32):
    found = db.execute(
        "select id, hidden, title, parent_session_id from sessions where id = ?", (current,)
    ).fetchone()
    if found is None:
        break
    if found[1] and found[2] == "Bot Chat":
        canonical = found[:3]
        break
    if not found[3]:
        break
    current = found[3]
if canonical:
    print("session %s hidden=%s title=%s" % canonical)
else:
    print("session none", session_id)

sql = "select id, role, tool_name, content from messages where session_id = ? and role = ?"
args = [session_id, role]
if tool_name:
    sql += " and tool_name = ?"
    args.append(tool_name)
if needle:
    sql += " and content like ?"
    args.append("%" + needle + "%")
row = db.execute(sql + " order by id desc limit 1", args).fetchone()
if row:
    print("row", row[0], row[1], row[2], (row[3] or "").replace("\n", " ")[:200])
else:
    print("row none")
