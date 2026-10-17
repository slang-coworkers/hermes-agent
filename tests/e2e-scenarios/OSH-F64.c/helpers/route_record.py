"""AC-OSH-F64-4 route record: every model call this scenario made, and whether it stayed on the managed route.

Usage: python3 route_record.py <since-epoch-seconds> <expected-billing-base-url>

Reads every session_model_usage row in $HERMES_HOME/state.db (the gateway/default store) and in every
$HERMES_HOME/profiles/*/state.db, for every session started at or after <since> (delivery children and
aux tasks included, not only the canonical Bot Chats). Prints one "usage <store> <session_id> <task>
<billing_base_url> <api_call_count>" line per row, then "route_ok yes|no" and "controls_ok yes|no".
route_ok is "no" when any row with api_call_count > 0 billed a base URL other than <expected>.
controls_ok is "no" unless the orchestrator store and the builder store each hold such a row.
Exits 0 only when both are "yes". Prints names and counts only, never a header value or a key.
"""
import glob
import os
import sqlite3
import sys

since = float(sys.argv[1])
expected = sys.argv[2].rstrip("/")
home = os.environ["HERMES_HOME"]
stores = {"default": os.path.join(home, "state.db")}
for path in sorted(glob.glob(os.path.join(home, "profiles", "*", "state.db"))):
    stores[os.path.basename(os.path.dirname(path))] = path

off_route = 0
called = set()
for name, path in stores.items():
    if not os.path.exists(path):
        print("store", name, "absent")
        continue
    db = sqlite3.connect(path)
    # A store no model turn has written yet may predate the usage table; it holds no calls.
    if not db.execute(
        "select 1 from sqlite_master where type = 'table' and name = 'session_model_usage'"
    ).fetchone():
        print("store", name, "has no session_model_usage table")
        continue
    rows = db.execute(
        "select u.session_id, u.task, u.billing_base_url, u.api_call_count "
        "from session_model_usage u join sessions s on s.id = u.session_id "
        "where s.started_at >= ? order by s.started_at, u.session_id",
        (since,),
    ).fetchall()
    for session_id, task, base_url, count in rows:
        print("usage", name, session_id, task or "-", base_url or "-", count)
        if count > 0:
            called.add(name)
            if (base_url or "").rstrip("/") != expected:
                off_route += 1
route_ok = off_route == 0
controls_ok = {"orchestrator", "builder"} <= called
print("route_ok", "yes" if route_ok else "no", "off_route_rows", off_route)
print("controls_ok", "yes" if controls_ok else "no", "stores_with_calls", ",".join(sorted(called)) or "-")
sys.exit(0 if route_ok and controls_ok else 1)
