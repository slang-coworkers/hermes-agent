"""AC-OSH-F64-4 G2: find the one dashboard PTY child serving a given Bot Chat (from /proc; no procps).

Usage:
  $PY pty_select.py expect <profile> <canonical-id>
  $PY pty_select.py state <profile> <canonical-id>
  python3 pty_select.py select <dashboard-pid> <profile> <expected-resume-id>
  python3 pty_select.py gate <mode> <obs-dir> <guard-dir> <profile> <expected-id> <canonical-id> <window-dir> <selected.json>

`expect` computes the id the PTY route will put in HERMES_TUI_RESUME, the route's own way: before it
spawns the child the route swaps the requested id for its newest descendant by started_at (release
hermes_cli/web_server.py:16623-16634, _session_latest_descendant :12249, :12314-12327), which can
differ from the listing's resolved_id. It opens <profile>'s state.db read-only and prints
"expected_id <id>". Run it with the Hermes venv's $PY (it imports hermes_state and the dashboard's
web_server), immediately before each connect and again before every reconnect.

`state` prints the canonical session's message count and latest descendant as JSON (read-only), the
Setup-5 probe's before/after safety read.

`select` keeps the `-m tui_gateway.entry` processes that descend from <dashboard-pid> (ppid walk over
/proc/<pid>/stat), whose environ HERMES_TUI_RESUME equals <expected-resume-id> and whose HERMES_HOME
basename equals <profile>. Only those two environ values are parsed; nothing else from the environ
is read out or printed. Prints "pty_child <pid> resume <id> netns <ns> dashboard_netns <ns>" for each
match and "pty_matches <n>". Exits 0 only when exactly one PID matches.

`gate` is G2 for a sandbox where a fresh exec cannot read another exec tree's /proc (osh-f64c-gw), over one
window dir holding base.json and send.json (two proc_observer.py `query` answers with gui.log cursors, taken
right before the navigation and right before the send) and console.txt (the ws_log.js lines `agent-browser
console` printed in that window). It also reads the dashboard observer's root.json and events.jsonl and the G3
guard's <pid>.armed / <pid>.jsonl. <mode> is preconnect | initial | reattach | away | close; `initial` writes the chosen
(pid, starttime) to <selected.json> and every later mode but `preconnect` reads it. The decision is `decide()`, a pure function.
"""
import json
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


def session_state(profile, canonical_id):
    """Bound-2 probe safety read: the canonical session's message count and its latest descendant."""
    import sqlite3
    from pathlib import Path

    from hermes_cli.profiles import get_profile_dir

    db = sqlite3.connect("file:%s?mode=ro" % (Path(get_profile_dir(profile)) / "state.db"), uri=True)
    try:
        count = db.execute("select count(*) from messages where session_id = ?", (canonical_id,)).fetchone()[0]
    finally:
        db.close()
    return {"messages": count, "latest": expected_id(profile, canonical_id)}


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


def fp(rec):
    return (rec["pid"], rec["starttime"])


def opens_in(lines):
    """Parse ws_log.js console lines into (ready, opens, urls): opens = [url resume], urls = [page resume]."""
    ready, opens, urls = 0, [], []
    for line in lines:
        parts = line.split("OSHWS ", 1)[-1].split() if "OSHWS " in line else []
        if not parts:
            continue
        if parts[0] == "ready":
            ready += 1
        elif parts[0] == "open" and len(parts) >= 3:
            q = parts[2].split("?", 1)[1] if "?" in parts[2] else ""
            opens.append(dict(kv.split("=", 1) for kv in q.split("&") if "=" in kv).get("resume", "-"))
        elif parts[0] == "url" and len(parts) >= 4:
            urls.append(parts[3].split("=", 1)[-1])
    return ready, opens, urls


def decide(mode, profile, expected, canonical, dashboard, baseline, presend, events, accepts, browser,
           armed, guard_pids, selected=None):
    """G2 for one window [C0, send] (D3 bounds 1 and 3 as amended by D3a); returns (ok, lines).

    Inputs are records other writers made. baseline / presend: two observer `query` answers (right before the
    navigation, right before the send). events: the observer's births/exits between them. accepts: the
    dashboard's gui.log `pty accepted` lines between two byte offsets. browser: (ready, opens, urls) from
    ws_log.js lines between two console cursors. armed / guard_pids: the G3 guard's records. selected: the
    fingerprint the step-1 `initial` window chose (pid, starttime), for every later window.

    Every mode: accepts == browser opens (a hidden or competing client fails), and at least one `ready` line
    (an unobservable browser log fails) except `close`, which loads no page. Per mode:
    - preconnect (the /sessions page before the first chat): no child at either end, no open, no birth.
    - initial: no child at the baseline; one open, or two where the first carries `canonical` != expected,
      the second carries `expected`, and the page `url` lines of that one document load (one `ready`) go
      from `canonical` to `expected` in that order;
      exactly one birth, new, with `expected` and `profile`; the pre-send live set is that child alone.
    - reattach: the baseline live set is `selected` alone; one open; zero births; the pre-send live set is
      `selected` alone with the same resume, home and netns.
    - away (a page other than /chat): zero accepts; the live set at the end is `selected` alone.
    - close: no accept without an open, and the live set at the end has nothing but `selected`.
    The chosen child's netns must equal the dashboard's and its .armed netns, and, in every mode but initial, it
    must have guard records.
    """
    ready, opens, urls = browser
    base = {fp(c): c for c in baseline["children"]}
    live = {fp(c): c for c in presend["children"]}
    births = [e for e in events if e["event"] == "birth"]
    dns = dashboard.get("netns", "unreadable")
    lines = ["mode %s baseline %d accepts %d opens %d ready %d births %d presend %d" % (
        mode, len(base), accepts, len(opens), ready, len(births), len(live))]
    ok = dns != "unreadable" and ready >= 1 and accepts == len(opens)
    chosen = None
    if mode == "initial":
        rewrite = (len(opens) == 2 and ready == 1 and canonical != expected
                   and opens == [canonical, expected] and urls == [canonical, expected])
        ok = ok and not base and (len(opens) == 1 or rewrite) and len(births) == 1
        if births:
            chosen = births[0]
            ok = ok and (chosen["resume"] == expected and bool(expected) and chosen["home"] == profile
                         and fp(chosen) not in base and set(live) == {fp(chosen)})
        lines.append("rewrite %s" % ("yes" if rewrite else "no"))
    elif mode == "reattach":
        sel = tuple(selected) if selected else None
        ok = ok and sel is not None and set(base) == {sel} and len(opens) == 1 and not births and set(live) == {sel}
        if ok:
            chosen = live[sel]
            ok = all(chosen[k] == base[sel][k] for k in ("resume", "home", "netns")) and chosen["resume"] == expected
    elif mode == "away":
        sel = tuple(selected) if selected else None
        ok = dns != "unreadable" and ready >= 1 and accepts == 0 and not opens and sel is not None and set(live) == {sel}
        chosen = live.get(sel) if ok else None
    elif mode == "preconnect":
        ok = ok and not base and not opens and not births and not live
    elif mode == "close":
        sel = tuple(selected) if selected else None
        ok = dns != "unreadable" and accepts == len(opens) and sel is not None and set(live) <= {sel}
        chosen = live.get(sel)
    else:
        ok = False
    if chosen is not None:
        a = armed.get(chosen["pid"]) or {}
        armed_ns = a.get("netns") or "absent"
        has_guard = chosen["pid"] in guard_pids
        # A fresh PTY child may have no G3 request record before its first turn; the post-turn away gate requires one.
        ok = ok and chosen["netns"] == dns and armed_ns == dns and (has_guard or mode == "initial")
        lines.append("pty_child %s starttime %s resume %s home %s netns %s dashboard_netns %s armed_netns %s guard %s" % (
            chosen["pid"], chosen["starttime"], chosen["resume"], chosen["home"], chosen["netns"], dns, armed_ns,
            "yes" if has_guard else "no"))
    lines.append("g2_ok %s" % ("yes" if ok else "no"))
    return ok, lines, (fp(chosen) if chosen is not None else None)


def accepts_between(gui_log, start, end):
    """Complete `pty accepted` lines between two byte offsets of gui.log ({inode, size} cursors)."""
    if start["inode"] != end["inode"] or end["size"] < start["size"]:
        raise SystemExit("gui.log rotated or truncated inside the window: FAIL(env)")
    with open(gui_log, "rb") as fh:
        if os.fstat(fh.fileno()).st_ino != start["inode"]:
            raise SystemExit("gui.log rotated before the gate read it: FAIL(env)")
        fh.seek(start["size"])
        chunk = fh.read(end["size"] - start["size"])
    return sum(1 for line in chunk.decode("utf-8", errors="replace").splitlines() if " pty accepted peer=" in line)


def guard_record(guard_dir, pid):
    """True when <pid>.jsonl holds at least one parsed G3 record written by that PID."""
    try:
        with open(os.path.join(guard_dir, "%d.jsonl" % pid), encoding="utf-8") as fh:
            for raw in fh:
                try:
                    if json.loads(raw).get("pid") == pid:
                        return True
                except ValueError:
                    continue
    except OSError:
        pass
    return False


def cursor(gui_log):
    st = os.stat(gui_log)
    return {"inode": st.st_ino, "size": st.st_size}


def gate(mode, obs, guard_dir, profile, expected, canonical, window_dir, selected_path):
    """`decide()` over one window dir: base.json / send.json (re-reads + gui.log cursors), console.txt."""
    def load(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    dashboard = load(os.path.join(obs, "root.json"))
    baseline, presend = load(os.path.join(window_dir, "base.json")), load(os.path.join(window_dir, "send.json"))
    events = []
    with open(os.path.join(obs, "events.jsonl"), encoding="utf-8") if os.path.exists(os.path.join(obs, "events.jsonl")) else open(os.devnull, encoding="utf-8") as fh:
        for raw in fh:
            rec = json.loads(raw)
            if baseline["ts"] < rec["ts"] <= presend["ts"]:
                events.append(rec)
    with open(os.path.join(window_dir, "console.txt"), encoding="utf-8") as fh:
        browser = opens_in(fh.read().splitlines())
    accepts = accepts_between(baseline["gui_log"], baseline["cursor"], presend["cursor"])
    pids = {c["pid"] for c in presend["children"]} | {e["pid"] for e in events}
    armed = {}
    for pid in pids:
        try:
            armed[pid] = load(os.path.join(guard_dir, "%d.armed" % pid))
        except (OSError, ValueError):
            pass
    guard_pids = {pid for pid in pids if guard_record(guard_dir, pid)}
    selected = load(selected_path) if mode not in ("initial", "preconnect") else None
    ok, lines, chosen = decide(mode, profile, expected, canonical, dashboard, baseline, presend, events, accepts,
                               browser, armed, guard_pids, selected)
    if ok and mode == "initial":
        with open(selected_path, "w", encoding="utf-8") as fh:
            json.dump(list(chosen), fh)
    print("\n".join(lines))
    return 0 if ok else 1


def main():
    mode, args = sys.argv[1], sys.argv[2:]
    if mode == "expect":
        print("expected_id", expected_id(*args[:2]))
        return 0
    if mode == "state":
        print(json.dumps(session_state(*args[:2]), sort_keys=True))
        return 0
    if mode == "select":
        return select(*args[:3])
    if mode == "gate":
        return gate(*args[:8])
    print("unknown mode", mode, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
