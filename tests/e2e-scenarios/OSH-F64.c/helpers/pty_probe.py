"""AC-OSH-F64-4 G2 descendant self-test: one /api/pty WebSocket without `attach`, read by the dashboard's observer.

Usage: $PY pty_probe.py <port> <profile> <resume-id> <session-token> <dashboard-obs-dir>

Without `attach` the dashboard takes the legacy 1:1 path, which closes the PTY child when the socket closes
(release hermes_cli/web_server.py:17535-17546, _legacy_pump :16141-16175). The probe sends no keystroke. It asks
the observer for a live re-read (none may be live before it) until one child is live, requires that child to be
the only new birth in the observer's events.jsonl (counted after the close, so a late second birth fails),
prints its fields, closes the socket and
asks again until none is. Before and after, it reads the probed session's message count and latest descendant
(pty_select.py `state`, read-only): the probe is safe only if they are unchanged. Prints "probe_child_netns
<ns>", "probe_child_home <basename>", "probe_after_count <n>", "probe_session_unchanged yes|no",
"probe_baseline_count <n>", "probe_new_births <n>" and exits 0 only when, from a zero baseline, exactly one new
child was born, with readable names, none is left after the close and the session is unchanged. The setup-window G4 (0 POSTs) is the other half of the safety evidence.
The token is passed on the URL, as the SPA does in loopback mode; it is never printed.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

from websockets.sync.client import connect

HERE = Path(__file__).resolve().parent
OBSERVER = HERE / "proc_observer.py"
sys.path.insert(0, str(HERE))
from pty_select import session_state  # noqa: E402


def reread(obs, min_count, timeout_s):
    out = subprocess.run([sys.executable, str(OBSERVER), "query", obs, str(timeout_s), str(min_count)],
                         capture_output=True, text=True, check=False)
    return out.returncode, json.loads(out.stdout or "{}")


def births(obs):
    try:
        with open(Path(obs) / "events.jsonl", encoding="utf-8") as fh:
            return {(e["pid"], e["starttime"]) for e in map(json.loads, fh) if e["event"] == "birth"}
    except FileNotFoundError:
        return set()


def probe(open_ws, reread_fn, births_fn, state_fn, profile, resume, out=print):
    """The probe's decision over injected I/O (the socket, the observer, the session store); returns 0 or 1."""
    before = state_fn(profile, resume)
    _, base = reread_fn(0, 10)
    seen = births_fn()
    ws = open_ws()
    try:
        rc, live = reread_fn(1, 60)
        children = live.get("children") or (live.get("last") or {}).get("children") or []
    finally:
        ws.close()
    for c in children:
        out("probe_child", c["pid"], "starttime", c["starttime"])
        out("probe_child_netns", c["netns"])
        out("probe_child_home", c["home"])
    deadline = time.time() + 60
    after = None
    while time.time() < deadline:
        _, now = reread_fn(0, 10)
        after = now.get("count")
        if after == 0:
            break
        time.sleep(0.5)
    born = births_fn() - seen
    unchanged = state_fn(profile, resume) == before
    one = {(children[0]["pid"], children[0]["starttime"])} if len(children) == 1 else None
    out("probe_after_count", after if after is not None else "unread")
    out("probe_session_unchanged", "yes" if unchanged else "no")
    out("probe_baseline_count", base.get("count", "unread"))
    out("probe_new_births", len(born))
    ok = (rc == 0 and base.get("count") == 0 and one is not None and born == one
          and children[0]["home"] == profile and children[0]["netns"] != "unreadable" and after == 0 and unchanged)
    return 0 if ok else 1


def main():
    port, profile, resume, token, obs = sys.argv[1:6]
    url = "ws://127.0.0.1:%s/api/pty?profile=%s&resume=%s&token=%s" % (port, profile, resume, token)
    return probe(lambda: connect(url, proxy=None, open_timeout=30),
                 lambda min_count, timeout_s: reread(obs, min_count, timeout_s),
                 lambda: births(obs), session_state, profile, resume)


if __name__ == "__main__":
    sys.exit(main())
