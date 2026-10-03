"""AC-OSH-F64-4 G2: find the one dashboard PTY child serving a given Bot Chat (from /proc; no procps).

Usage:
  $PY pty_select.py expect <profile> <canonical-id>
  python3 pty_select.py select <dashboard-pid> <profile> <expected-resume-id>

`expect` computes the id the PTY route will put in HERMES_TUI_RESUME, the route's own way: before it
spawns the child the route swaps the requested id for its newest descendant by started_at (release
hermes_cli/web_server.py:16623-16634, _session_latest_descendant :12249, :12314-12327), which can
differ from the listing's resolved_id. It opens <profile>'s state.db read-only and prints
"expected_id <id>". Run it with the Hermes venv's $PY (it imports hermes_state and the dashboard's
web_server), immediately before each connect and again before every reconnect.

`select` keeps the `-m tui_gateway.entry` processes that descend from <dashboard-pid> (ppid walk over
/proc/<pid>/stat), whose environ HERMES_TUI_RESUME equals <expected-resume-id> and whose HERMES_HOME
basename equals <profile>. Only those two environ values are parsed; nothing else from the environ
is read out or printed. Prints "pty_child <pid> resume <id> netns <ns> dashboard_netns <ns>" for each
match and "pty_matches <n>". Exits 0 only when exactly one PID matches.
"""
import os
import sys


def ppid(pid):
    try:
        with open("/proc/%s/stat" % pid, "rb") as fh:
            stat = fh.read().decode(errors="replace")
    except OSError:
        return None
    # comm may contain spaces or ')' — the fields after the last ')' are fixed.
    fields = stat.rsplit(")", 1)[-1].split()
    return fields[1] if len(fields) > 1 else None


def descends_from(pid, ancestor):
    seen = set()
    while pid and pid not in seen and pid != "0":
        if pid == ancestor:
            return True
        seen.add(pid)
        pid = ppid(pid)
    return False


def environ_values(pid, names):
    try:
        with open("/proc/%s/environ" % pid, "rb") as fh:
            raw = fh.read()
    except OSError:
        return {}
    out = {}
    for item in raw.split(b"\0"):
        key, sep, value = item.partition(b"=")
        if sep and key.decode(errors="replace") in names:
            out[key.decode()] = value.decode(errors="replace")
    return out


def netns(pid):
    try:
        return os.readlink("/proc/%s/ns/net" % pid)
    except OSError:
        return "unreadable"


def expected_id(profile, canonical_id):
    from pathlib import Path

    from hermes_cli.profiles import get_profile_dir
    from hermes_cli.web_server import _session_latest_descendant
    from hermes_state import SessionDB

    db = SessionDB(db_path=Path(get_profile_dir(profile)) / "state.db", read_only=True)
    try:
        return _session_latest_descendant(canonical_id, db)[0] or canonical_id
    finally:
        db.close()


def select(dashboard, profile, expected):
    matches = []
    for pid in sorted((p for p in os.listdir("/proc") if p.isdigit()), key=int):
        try:
            with open("/proc/" + pid + "/cmdline", "rb") as fh:
                argv = [a.decode(errors="replace") for a in fh.read().split(b"\0") if a]
        except OSError:
            continue
        if len(argv) < 3 or argv[1:3] != ["-m", "tui_gateway.entry"] or pid == dashboard:
            continue
        if not descends_from(pid, dashboard):
            continue
        env = environ_values(pid, ("HERMES_TUI_RESUME", "HERMES_HOME"))
        home = os.path.basename((env.get("HERMES_HOME") or "").rstrip("/"))
        if env.get("HERMES_TUI_RESUME") == expected and home == profile:
            matches.append(pid)
    for pid in matches:
        print("pty_child", pid, "resume", expected, "netns", netns(pid), "dashboard_netns", netns(dashboard))
    print("pty_matches", len(matches))
    return 0 if len(matches) == 1 else 1


def main():
    mode, args = sys.argv[1], sys.argv[2:]
    if mode == "expect":
        print("expected_id", expected_id(*args[:2]))
        return 0
    if mode == "select":
        return select(*args[:3])
    print("unknown mode", mode, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
