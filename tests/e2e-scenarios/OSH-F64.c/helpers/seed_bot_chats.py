"""Seed each served profile's hidden canonical "Bot Chat" with an operator + an assistant line.

Run inside the osh-f64c-gw sandbox with the Hermes venv's python (it imports hermes_state).
Prints "<profile> <session-id>" per profile.
"""
import os
from pathlib import Path

from hermes_state import SessionDB

root = Path(os.environ["HERMES_HOME"])
for name in ("default", "orchestrator", "builder"):
    home = root if name == "default" else root / "profiles" / name
    db = SessionDB(db_path=home / "state.db")
    try:
        row = db.get_session_by_title(SessionDB.CANONICAL_BOT_CHAT_TITLE)
        sid = row["id"] if row else "osh-f64c-botchat-" + name
        if row is None:
            db.create_session(sid, source="tui")
            db.set_session_title(sid, SessionDB.CANONICAL_BOT_CHAT_TITLE)
        db.set_session_hidden(sid, True)
        db.append_message(sid, "user", "osh-f64c operator line for " + name)
        db.append_message(sid, "assistant", "osh-f64c assistant line from " + name)
        print(name, sid)
    finally:
        db.close()
