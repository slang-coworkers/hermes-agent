"""Print "<id> <hidden> <title>" for one session of the orchestrator's store (stdlib sqlite3; no CLI needed).

Usage: python3 bot_chat_row.py <session-id>
"""
import os
import sqlite3
import sys

db = sqlite3.connect(os.environ["HERMES_HOME"] + "/profiles/orchestrator/state.db")
row = db.execute("select id, hidden, title from sessions where id = ?", (sys.argv[1],)).fetchone()
if row:
    print(*row)
else:
    print("no-such-session", sys.argv[1])
