"""AC-OSH-F64-4 G2 observer: run one fleet process as this process's child and record what G2 compares.

Usage:
  $PY proc_observer.py run <obs-dir> <name> -- <argv...>
  $PY proc_observer.py query <obs-dir> [timeout_s] [min_count] [save-as] [gui.log]
  $PY proc_observer.py record <obs-root> <name>...

In osh-f64c-gw a fresh `openshell sandbox exec` cannot read another exec tree's /proc/<pid>/environ or
ns/net, but an ancestor in the same tree can. So `run` spawns argv unchanged (inherited env, same netns),
stays its parent and writes, under <obs-dir>/<name>/:
- root.json: the child's pid, starttime (/proc/<pid>/stat field 22), netns, HERMES_HOME basename and
  HERMES_TUI_RESUME, read once it has started;
- selftest.json: root.json's read plus the same read of a probe process this observer forks under the
  root's env, so the read path is proven on a process other than the root ("ok" false on any miss);
- events.jsonl: a "birth" line for every new descendant `<python> -m tui_gateway.entry` fingerprint
  (pid, starttime) seen by a scan every SCAN_S seconds, with its ppid, netns and the two names, and an
  "exit" line when that fingerprint is gone;
- resp-<n>.json: the answer to a req-<n> file (`query`), a synchronous scan of the live descendants;
- exit.json: the child's return code, written when it exits;
- final.json: after the exit, whether any fingerprint it ever recorded is still alive (by /proc/<pid>/stat
  alone, so a reparented survivor still counts), polled for up to POST_EXIT_S seconds.
Only HERMES_TUI_RESUME and the HERMES_HOME basename are ever written from an environ; a read the observer
cannot make is "unreadable", never guessed. `query` runs in any exec: it only writes req-<n> and waits
for resp-<n>, and exits non-zero when no answer comes. `record` prints one netns.txt line per named start from
its own root.json and fails on a missing, unreadable or exited one.
"""
import json
import os
import subprocess
import sys
import time

SCAN_S = 0.1
POST_EXIT_S = 30.0
NAMES = ("HERMES_TUI_RESUME", "HERMES_HOME")


def stat_fields(pid):
    try:
        with open("/proc/%d/stat" % pid, "rb") as fh:
            stat = fh.read().decode(errors="replace")
    except OSError:
        return None
    # comm may contain spaces or ')' — the fields after the last ')' are fixed (field 3 onwards).
    rest = stat.rsplit(")", 1)[-1].split()
    try:
        return int(rest[1]), int(rest[19])
    except (IndexError, ValueError):
        return None


def cmdline(pid):
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as fh:
            return [a.decode(errors="replace") for a in fh.read().split(b"\0") if a]
    except OSError:
        return None


def environ_names(pid):
    try:
        with open("/proc/%d/environ" % pid, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    out = {}
    for item in raw.split(b"\0"):
        key, sep, value = item.partition(b"=")
        if sep and key.decode(errors="replace") in NAMES:
            out[key.decode()] = value.decode(errors="replace")
    return out


def netns(pid):
    try:
        return os.readlink("/proc/%d/ns/net" % pid)
    except OSError:
        return "unreadable"


def read(pid):
    """The fields G2 compares for one process; any field it cannot read is "unreadable"."""
    st = stat_fields(pid)
    env = environ_names(pid)
    if env is None:
        home = resume = "unreadable"
    else:
        home = os.path.basename((env.get("HERMES_HOME") or "").rstrip("/"))
        resume = env.get("HERMES_TUI_RESUME")
    return {"pid": pid, "ppid": st[0] if st else "unreadable", "starttime": st[1] if st else "unreadable",
            "netns": netns(pid), "home": home, "resume": resume}


def readable(rec):
    """Required fields for a root or probe read; HERMES_TUI_RESUME may be absent outside a PTY child."""
    return (rec["home"] not in ("", "unreadable") and rec["netns"] != "unreadable"
            and rec["starttime"] != "unreadable")


def settled_read(pid, tries=50):
    """Avoid a false unreadable result while exec populates /proc/<pid>/environ.

    /proc/<pid>/stat can appear first; a persistently denied read remains unreadable after bounded retries.
    """
    rec = read(pid)
    for _ in range(tries):
        if readable(rec):
            break
        time.sleep(SCAN_S)
        rec = read(pid)
    return rec


def descendants(root):
    """(pid, starttime) -> ppid for every live `<python> -m tui_gateway.entry` under root (ppid walk)."""
    parent = {}
    for name in os.listdir("/proc"):
        if name.isdigit():
            st = stat_fields(int(name))
            if st:
                parent[int(name)] = st
    out = {}
    for pid, (ppid, start) in parent.items():
        argv = cmdline(pid)
        if pid == root or not argv or argv[1:3] != ["-m", "tui_gateway.entry"]:
            continue
        cur, seen = ppid, set()
        while cur and cur not in seen and cur in parent:
            if cur == root:
                out[(pid, start)] = ppid
                break
            seen.add(cur)
            cur = parent[cur][0]
    return out


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, sort_keys=True)
    os.replace(tmp, path)


def selftest(child):
    """Read the root, then a probe forked under the root's env, through the same read path."""
    root = settled_read(child.pid)
    probe_proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                  env=os.environ.copy(), stdin=subprocess.DEVNULL)
    try:
        probe = settled_read(probe_proc.pid)
    finally:
        probe_proc.kill()
        probe_proc.wait()
    ok = readable(root) and probe is not None and readable(probe) and probe["netns"] == root["netns"]
    return {"ok": ok, "root": root, "probe": probe}


def scan(root_pid, known, recorded, events):
    """One descendant scan: log a birth for each new fingerprint and an exit for each gone one."""
    now = descendants(root_pid)
    lines = []
    for fp in sorted(set(now) - set(known)):
        rec = read(fp[0])
        known[fp] = rec
        recorded.add(fp)
        lines.append(dict(rec, event="birth", ts=time.time(), starttime=fp[1]))
    for fp in sorted(set(known) - set(now)):
        lines.append(dict(known.pop(fp), event="exit", ts=time.time(), starttime=fp[1]))
    if lines:
        with open(events, "a", encoding="utf-8") as fh:
            for line in lines:
                fh.write(json.dumps(line, sort_keys=True) + "\n")
    return now


def answer_requests(obs, root_pid, known, recorded, events):
    """Answer each req-<n> with a fresh scan and a fresh read of every live child.

    The scan runs first, so any child in an answer already has its birth line in events.jsonl.
    """
    for name in sorted(os.listdir(obs)):
        if name.startswith("req-") and name.endswith(".json"):
            n = name[len("req-"):-len(".json")]
            now = scan(root_pid, known, recorded, events)
            live = [dict(read(fp[0]), starttime=fp[1]) for fp in sorted(now)]
            write_json(os.path.join(obs, "resp-%s.json" % n),
                       {"ts": time.time(), "count": len(live), "children": live})
            os.unlink(os.path.join(obs, name))


def run(obs_root, name, argv):
    obs = os.path.join(obs_root, name)
    os.makedirs(obs, exist_ok=True)
    child = subprocess.Popen(argv)
    time.sleep(SCAN_S)
    write_json(os.path.join(obs, "root.json"), settled_read(child.pid))
    write_json(os.path.join(obs, "selftest.json"), selftest(child))
    known, recorded = {}, set()
    events = os.path.join(obs, "events.jsonl")
    while child.poll() is None:
        scan(child.pid, known, recorded, events)
        answer_requests(obs, child.pid, known, recorded, events)
        time.sleep(SCAN_S)
    write_json(os.path.join(obs, "exit.json"), {"rc": child.returncode, "ts": time.time()})
    write_json(os.path.join(obs, "final.json"), post_exit(child.returncode, recorded))
    return child.returncode


def post_exit(rc, recorded, wait_s=POST_EXIT_S):
    """After the child exits, wait for every fingerprint ever recorded to be gone, by /proc/<pid>/stat alone.

    No ancestry walk: a child the dashboard left behind is reparented and would drop out of descendants().
    """
    deadline = time.time() + wait_s
    while True:
        survivors, unreadable = [], []
        for pid, start in sorted(recorded):
            st = stat_fields(pid)
            if st is None:
                if os.path.exists("/proc/%d" % pid):
                    unreadable.append([pid, start])
            elif st[1] == start:
                survivors.append([pid, start])
        if not survivors and not unreadable or time.time() >= deadline:
            return {"rc": rc, "ts": time.time(), "recorded": len(recorded), "survivors": survivors,
                    "unreadable": unreadable}
        time.sleep(SCAN_S)


def query(obs, timeout_s=10.0, min_count=0, save_as=None, gui_log=None):
    """Ask the observer for a live re-read; with min_count, repeat until that many children are live.

    save_as keeps the answer as <obs>/<save_as> too (a path inside <obs>), for a later `pty_select.py gate`
    in another exec.
    gui_log adds that file's {inode, size} cursor, taken right after the answer, so a gate can count the
    dashboard's `pty accepted` lines between two re-reads by byte offset rather than by timestamp.
    """
    deadline = time.time() + float(timeout_s)
    last = None
    while time.time() < deadline:
        n = "%d-%d" % (os.getpid(), time.time_ns())
        write_json(os.path.join(obs, "req-%s.json" % n), {"ts": time.time()})
        resp = os.path.join(obs, "resp-%s.json" % n)
        while time.time() < deadline and not os.path.exists(resp):
            time.sleep(SCAN_S)
        if not os.path.exists(resp):
            break
        with open(resp, encoding="utf-8") as fh:
            last = json.load(fh)
        if last["count"] >= int(min_count):
            if gui_log:
                try:
                    st = os.stat(gui_log)
                except OSError:
                    print(json.dumps({"error": "gui.log unreadable", "obs": obs}, sort_keys=True))
                    return 1
                last["gui_log"], last["cursor"] = gui_log, {"inode": st.st_ino, "size": st.st_size}
            if save_as:
                target = os.path.normpath(os.path.join(obs, save_as))
                if os.path.commonpath([target, os.path.abspath(obs)]) != os.path.abspath(obs):
                    print(json.dumps({"error": "save-as outside the observer dir", "obs": obs}, sort_keys=True))
                    return 1
                os.makedirs(os.path.dirname(target), exist_ok=True)
                write_json(target, last)
            print(json.dumps(last, sort_keys=True))
            return 0
    print(json.dumps({"error": "no answer from the observer" if last is None else "fewer children than asked",
                      "obs": obs, "last": last}, sort_keys=True))
    return 1


def record(obs_root, names):
    """netns.txt lines from the observers' own reads; any missing, unreadable or exited start fails."""
    ok = True
    for name in names:
        obs = os.path.join(obs_root, name)
        try:
            with open(os.path.join(obs, "root.json"), encoding="utf-8") as fh:
                root = json.load(fh)
            with open(os.path.join(obs, "selftest.json"), encoding="utf-8") as fh:
                st = json.load(fh)
        except (OSError, ValueError):
            print(name, "missing")
            ok = False
            continue
        exited = os.path.exists(os.path.join(obs, "exit.json"))
        good = readable(root) and st.get("ok") is True and not exited
        ok = ok and good
        print(name, root["pid"], root["starttime"], root["netns"], "selftest", "ok" if st.get("ok") else "miss",
              "exited" if exited else "running")
    print("record_ok", "yes" if ok else "no")
    return 0 if ok else 1


def main():
    mode, args = sys.argv[1], sys.argv[2:]
    if mode == "run":
        return run(args[0], args[1], args[args.index("--") + 1:])
    if mode == "query":
        return query(*args[:5])
    if mode == "record":
        return record(args[0], args[1:])
    print("unknown mode", mode, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
