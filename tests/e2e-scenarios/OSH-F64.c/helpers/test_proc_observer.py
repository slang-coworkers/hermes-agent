"""AC-OSH-F64-4 G2 helpers: the decision rule over observer records, and the observer on a real process tree."""
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location("osh_f64c_" + name, HERE / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pty_select = _load("pty_select")
proc_observer = _load("proc_observer")
pty_probe = _load("pty_probe")

NS = "net:[1]"
DASH = {"pid": 10, "starttime": 100, "netns": NS, "home": ".hermes", "resume": None}
FP = (20, 500)


def child(pid=20, start=500, resume="sidA", home="orchestrator", netns=NS, ppid=15, parent_start=None):
    return {"pid": pid, "ppid": ppid, "parent_starttime": parent_start, "starttime": start, "resume": resume,
            "home": home, "netns": netns}


def read(ts, *children):
    return {"ts": ts, "count": len(children), "children": list(children)}


def ev(kind, ts, c):
    return dict(c, event=kind, ts=ts)


def browser(*opened, ready=1, urls=None):
    return ready, list(opened), list(urls if urls is not None else opened)


def gate(mode, baseline, presend, events=(), accepts=1, br=None, armed=None, guard_pids=None, selected=FP,
         expected="sidA", canonical="sidA"):
    return pty_select.decide(
        mode, "orchestrator", expected, canonical, DASH, baseline, presend, list(events), accepts,
        br if br is not None else browser(expected), armed if armed is not None else {20: {"netns": NS}},
        guard_pids if guard_pids is not None else {20}, None if mode == "initial" else list(selected))


def initial(**kw):
    kw.setdefault("events", [ev("birth", 2.0, child())])
    return gate("initial", read(1.0), read(9.0, child()), **kw)


def reattach(**kw):
    kw.setdefault("presend", read(9.0, child()))
    return gate("reattach", read(1.0, child()), **kw)


def test_initial_spawn_accepts_one_open_one_accept_one_new_birth():
    ok, lines, chosen = initial()
    assert ok, lines
    assert chosen == FP


def test_initial_spawn_accepts_a_recorded_spa_rewrite_as_two_opens_one_birth():
    ok, lines, _ = initial(canonical="sidC", accepts=2, br=browser("sidC", "sidA", urls=["sidC", "sidA"]))
    assert ok, lines


def test_same_key_reattach_accepts_one_open_one_accept_zero_births():
    ok, lines, _ = reattach()
    assert ok, lines


def test_away_window_accepts_zero_accepts_with_the_child_still_live():
    ok, lines, _ = gate("away", read(1.0, child()), read(9.0, child()), accepts=0, br=browser())
    assert ok, lines


def test_close_accepts_only_the_selected_child_left_detached():
    assert gate("close", read(1.0, child()), read(9.0, child()), accepts=0, br=browser())[0]
    assert gate("close", read(1.0, child()), read(9.0), accepts=0, br=browser())[0]


def test_rejects_a_different_fingerprint_on_a_later_connect():
    other = child(pid=21, start=510)
    assert not reattach(presend=read(9.0, other), events=[ev("birth", 2.0, other)], armed={21: {"netns": NS}},
                        guard_pids={21})[0]


def test_rejects_an_exp_change_that_leaves_two_live_children():
    new = child(pid=21, start=510, resume="sidB")
    assert not reattach(presend=read(9.0, child(), new), events=[ev("birth", 2.0, new)], expected="sidB",
                        br=browser("sidB"))[0]


def test_rejects_a_hidden_socket_accept():
    assert not reattach(accepts=2)[0]
    assert not gate("away", read(1.0, child()), read(9.0, child()), accepts=1, br=browser())[0]


def test_rejects_a_pre_existing_child_at_the_step_1_baseline():
    assert not gate("initial", read(1.0, child(pid=19, start=400)), read(9.0, child()),
                    events=[ev("birth", 2.0, child())])[0]


def test_rejects_one_open_with_two_accepts():
    assert not initial(accepts=2)[0]


def test_rejects_two_opens_with_identical_urls():
    assert not initial(accepts=2, br=browser("sidA", "sidA"))[0]


def test_rejects_a_rewrite_without_the_page_url_change():
    assert not initial(canonical="sidC", accepts=2, br=browser("sidC", "sidA", urls=["sidC"]))[0]


def test_rejects_an_unobservable_browser_log():
    assert not initial(br=browser("sidA", ready=0))[0]


def test_rejects_pid_reuse_as_a_different_child():
    reused = child(start=777)
    assert not reattach(presend=read(9.0, reused), events=[ev("exit", 2.0, child()), ev("birth", 3.0, reused)])[0]


def test_rejects_a_racing_second_client_with_the_same_resume_id():
    b = child(pid=21, start=510)
    assert not initial(events=[ev("birth", 2.0, child()), ev("birth", 2.5, b)], accepts=2,
                       br=browser("sidA", "sidA"))[0]


@pytest.mark.parametrize("field,value", [("resume", "other"), ("home", "builder"), ("netns", "net:[2]")])
def test_rejects_an_initial_child_that_does_not_match(field, value):
    c = dict(child(), **{field: value})
    assert not gate("initial", read(1.0), read(9.0, c), events=[ev("birth", 2.0, c)])[0]


def test_rejects_an_armed_netns_mismatch_or_missing_guard_records():
    assert not initial(armed={20: {"netns": "net:[2]"}})[0]
    assert not initial(armed={})[0]
    assert not reattach(guard_pids=set())[0]
    assert not gate("away", read(1.0, child()), read(9.0, child()), accepts=0, br=(1, [], []), guard_pids=set())[0]


def test_initial_accepts_a_fresh_child_before_its_first_guard_record():
    ok, lines, _ = initial(guard_pids=set())
    assert ok and any(line.endswith("guard no") for line in lines)


def test_rejects_a_close_that_leaves_another_child():
    assert not gate("close", read(1.0, child()), read(9.0, child(), child(pid=22, start=600)), accepts=0,
                    br=browser())[0]


def test_opens_in_counts_pty_opens_ready_lines_and_page_resumes():
    lines = [
        "[info] OSHWS ready 1.0 /chat",
        "[info] OSHWS created 1.1 ws://h/api/pty?profile=orchestrator&resume=sidC&token=REDACTED&attach=REDACTED",
        "[info] OSHWS open 1.2 ws://h/api/pty?profile=orchestrator&resume=sidC&token=REDACTED&attach=REDACTED",
        "[info] OSHWS url 1.2 /chat resume=sidC",
        "[log] unrelated line",
    ]
    assert pty_select.opens_in(lines) == (1, ["sidC"], ["sidC"])


def test_accepts_between_counts_by_byte_offset_including_the_same_millisecond(tmp_path):
    log = tmp_path / "gui.log"
    line = "2026-10-05 03:00:00,123 INFO hermes_cli.web_server: pty accepted peer=127.0.0.1 mode=loopback cred=token\n"
    log.write_text(line, encoding="utf-8")
    start = pty_select.cursor(str(log))
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(line)
        fh.write("2026-10-05 03:00:00,123 INFO hermes_cli.web_server: pty refused: host_mismatch\n")
    assert pty_select.accepts_between(str(log), start, pty_select.cursor(str(log))) == 1


def test_accepts_between_fails_on_rotation_or_truncation(tmp_path):
    log = tmp_path / "gui.log"
    log.write_text("x" * 100, encoding="utf-8")
    start = pty_select.cursor(str(log))
    with pytest.raises(SystemExit):
        pty_select.accepts_between(str(log), start, dict(start, size=10))
    with pytest.raises(SystemExit):
        pty_select.accepts_between(str(log), start, dict(start, inode=start["inode"] + 1))


def test_preconnect_accepts_a_page_with_no_chat_socket():
    ok, lines, _ = gate("preconnect", read(1.0), read(9.0), accepts=0, br=browser())
    assert ok, lines


def test_preconnect_rejects_a_hidden_socket_or_a_child():
    assert not gate("preconnect", read(1.0), read(9.0), accepts=1, br=browser())[0]
    assert not gate("preconnect", read(1.0), read(9.0, child()), events=[ev("birth", 2.0, child())], accepts=0,
                    br=browser())[0]


def test_close_rejects_hidden_accept():
    assert not gate("close", read(1.0, child()), read(9.0, child()), accepts=1, br=browser())[0]


def test_rewrite_rejects_a_reversed_url_order():
    assert not initial(canonical="sidC", accepts=2, br=browser("sidC", "sidA", urls=["sidA", "sidC"]))[0]


def test_rewrite_rejects_two_document_loads():
    assert not initial(canonical="sidC", accepts=2, br=browser("sidC", "sidA", ready=2, urls=["sidC", "sidA"]))[0]


def test_mixed_socket_page_counts_only_the_pty_open():
    lines = [
        "[info] OSHWS ready 1.0 /chat",
        "[info] OSHWS open 1.2 ws://h/api/pty?profile=orchestrator&resume=sidA&token=REDACTED&attach=REDACTED",
        "[info] OSHWS url 1.2 /chat resume=sidA",
    ]
    # ws_log.js never logs /api/events or /api/ws sockets, so they cannot reach the parser at all
    assert pty_select.opens_in(lines) == (1, ["sidA"], ["sidA"])
    assert gate("initial", read(1.0), read(9.0, child()), events=[ev("birth", 2.0, child())],
                br=pty_select.opens_in(lines))[0]


class _Socket:
    def __init__(self, on_open=None):
        self.on_open = on_open

    def __call__(self):
        if self.on_open:
            self.on_open()
        return self

    def close(self):
        pass


def _probe(births_after, open_ws=None, state_fn=None, child_live=True):
    calls = []

    def reread(min_count, timeout_s):
        calls.append(min_count)
        if min_count == 1 and child_live:
            return 0, read(5.0, child())
        return 0, read(1.0 + len(calls))

    seq = [set(), births_after]
    return pty_probe.probe(open_ws or _Socket(), reread, lambda: seq.pop(0) if len(seq) > 1 else seq[0],
                           state_fn or (lambda profile, resume: {"messages": 1, "latest": resume}),
                           "orchestrator", "sidC", out=lambda *a: None)


def test_probe_accepts_one_new_birth_from_a_zero_baseline():
    assert _probe({FP}) == 0


def test_probe_rejects_late_second_birth():
    assert _probe({FP, (21, 510)}) == 1


def test_probe_rejects_no_birth():
    assert _probe(set()) == 1


@pytest.mark.parametrize("change", ["messages", "latest"])
def test_probe_session_state_change_is_rejected(tmp_path, monkeypatch, change):
    from hermes_state import SessionDB

    profile_dir = tmp_path / "profiles" / "orchestrator"
    profile_dir.mkdir(parents=True)
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda name: profile_dir)
    db = SessionDB(db_path=profile_dir / "state.db")
    db.create_session("sidC", "tui")
    db.append_message("sidC", "user", "hello")
    db.close()

    def mutate():
        db = SessionDB(db_path=profile_dir / "state.db")
        if change == "messages":
            db.append_message("sidC", "assistant", "a probe must never cause this")
        else:
            db.create_session("sidD", "tui", parent_session_id="sidC")
        db.close()

    assert _probe({FP}, state_fn=pty_select.session_state) == 0
    assert _probe({FP}, open_ws=_Socket(on_open=mutate), state_fn=pty_select.session_state) == 1


def test_settled_read_rereads_a_read_taken_before_the_environ_is_set(monkeypatch):
    reads = iter([dict(DASH, home=""), dict(DASH, home="")] + [DASH] * 5)
    monkeypatch.setattr(proc_observer, "read", lambda pid: next(reads))
    monkeypatch.setattr(proc_observer, "SCAN_S", 0)
    assert proc_observer.settled_read(10) == DASH


def test_settled_read_returns_a_denied_read_as_unreadable_after_bounded_tries(monkeypatch):
    denied = dict(DASH, home="unreadable", netns="unreadable")
    calls = []
    monkeypatch.setattr(proc_observer, "read", lambda pid: calls.append(pid) or denied)
    monkeypatch.setattr(proc_observer, "SCAN_S", 0)
    assert proc_observer.settled_read(10, tries=3) == denied
    assert len(calls) == 4


@pytest.mark.linux_only
def test_selftest_passes_when_each_first_read_lands_before_the_environ_is_set(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "h"))
    real, seen = proc_observer.read, set()

    def first_read_in_exec_window(pid):
        rec = real(pid)
        if pid not in seen:
            seen.add(pid)
            return dict(rec, home="")
        return rec

    monkeypatch.setattr(proc_observer, "read", first_read_in_exec_window)
    root = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], stdin=subprocess.DEVNULL)
    try:
        result = proc_observer.selftest(root)
    finally:
        root.kill()
        root.wait()
    assert result["ok"] is True
    assert result["root"]["home"] == "h" and result["probe"]["home"] == "h"


@pytest.mark.linux_only
def test_post_exit_reports_a_surviving_reparented_child(tmp_path):
    survivor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        st = proc_observer.stat_fields(survivor.pid)
        final = proc_observer.post_exit(0, {(survivor.pid, st[1])}, wait_s=0.3)
        assert final["survivors"] == [[survivor.pid, st[1]]]
    finally:
        survivor.kill()
        survivor.wait()
    assert proc_observer.post_exit(0, {(survivor.pid, st[1])}, wait_s=0.3)["survivors"] == []


@pytest.mark.linux_only
def test_observer_records_names_only_births_exits_and_answers_reads(tmp_path):
    mod = tmp_path / "mod" / "tui_gateway"
    mod.mkdir(parents=True)
    (mod / "__init__.py").write_text("", encoding="utf-8")
    (mod / "entry.py").write_text("import time; time.sleep(2)\n", encoding="utf-8")
    root = tmp_path / "root.py"
    root.write_text(
        "import os, subprocess, sys, time\n"
        "env = dict(os.environ, PYTHONPATH=%r, HERMES_HOME=os.environ['HERMES_HOME'] + '/profiles/orchestrator',\n"
        "           HERMES_TUI_RESUME='sidA')\n"
        "time.sleep(0.6)\n"
        "subprocess.run([sys.executable, '-c', 'import subprocess, sys; "
        "subprocess.run([sys.executable, \"-m\", \"tui_gateway.entry\"])'], env=env)\n"
        "time.sleep(0.4)\n" % str(tmp_path / "mod"), encoding="utf-8")
    obs_root = tmp_path / "obs"
    env = dict(os.environ, HERMES_HOME=str(tmp_path / "h"), OSH_F64C_TEST_CANARY="canary-value-not-recorded")
    # cwd outside the repo: `-m tui_gateway.entry` would otherwise import the real gateway from the cwd entry
    # that -m puts first on sys.path, not the stub on PYTHONPATH.
    proc = subprocess.Popen([sys.executable, str(HERE / "proc_observer.py"), "run", str(obs_root), "dashboard",
                             "--", sys.executable, str(root)], env=env, cwd=tmp_path)
    obs = obs_root / "dashboard"
    deadline = time.time() + 15
    while not (obs / "events.jsonl").exists() and time.time() < deadline:
        time.sleep(0.1)
    answer = subprocess.run([sys.executable, str(HERE / "proc_observer.py"), "query", str(obs), "5"],
                            capture_output=True, text=True, check=True)
    assert proc.wait(timeout=30) == 0

    live = json.loads(answer.stdout)
    assert live["count"] == 1 and live["children"][0]["resume"] == "sidA"
    selftest = json.loads((obs / "selftest.json").read_text(encoding="utf-8"))
    assert selftest["ok"] is True and selftest["probe"]["netns"] == selftest["root"]["netns"]
    events = [json.loads(x) for x in (obs / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["event"] for e in events] == ["birth", "exit"]
    assert events[0]["home"] == "orchestrator" and events[0]["netns"] == selftest["root"]["netns"]
    assert json.loads((obs / "exit.json").read_text(encoding="utf-8"))["rc"] == 0
    final = json.loads((obs / "final.json").read_text(encoding="utf-8"))
    assert final["survivors"] == [] and final["unreadable"] == [] and final["recorded"] == 1
    written = "".join(p.read_text(encoding="utf-8") for p in obs.iterdir() if p.is_file())
    assert "canary-value-not-recorded" not in written
    allowed = {"pid", "ppid", "parent_starttime", "starttime", "netns", "home", "resume", "event", "ts"}
    assert all(set(e) <= allowed for e in events + live["children"])


FORK = child(pid=21, start=510, ppid=20, parent_start=500)
REPLAY_NS = "net:[4026535743]"
REPLAY_SID = "20261005_142821_6b2aba"


def _replay_child(pid, start, ppid, parent_start=None):
    return {"home": "orchestrator", "netns": REPLAY_NS, "pid": pid, "ppid": ppid, "parent_starttime": parent_start,
            "resume": REPLAY_SID, "starttime": start}


def test_initial_replay_3346_3412_excludes_the_exited_fork_descendant(tmp_path):
    """(a) The caeb365 Step-1 records: PTY child 3346 live, 3412 (ppid 3346) born and exited before the send."""
    obs, win, guard = tmp_path / "obs", tmp_path / "obs" / "g2" / "s1", tmp_path / "guard"
    win.mkdir(parents=True)
    guard.mkdir()
    gui = tmp_path / "gui.log"
    gui.write_text("2026-10-05 14:31:46,186 INFO hermes_cli.web_server: pty accepted peer=127.0.0.1 mode=loopback "
                   "cred=token\n", encoding="utf-8")
    c0 = pty_select.cursor(str(gui))
    with open(gui, "a", encoding="utf-8") as fh:
        fh.write("2026-10-05 14:36:54,843 INFO hermes_cli.web_server: pty accepted peer=127.0.0.1 mode=loopback "
                 "cred=token\n")
    c1 = pty_select.cursor(str(gui))
    (obs / "root.json").write_text(json.dumps({"home": ".hermes", "netns": REPLAY_NS, "pid": 2049, "ppid": 2029,
                                               "resume": None, "starttime": 11142244}), encoding="utf-8")
    pty, fork, probe = _replay_child(3346, 11185516, 3327), _replay_child(3412, 11187750, 3346, 11185516), \
        _replay_child(2416, 11154446, 2405)
    events = [dict(probe, event="birth", ts=1791210709.4433012), dict(probe, event="exit", ts=1791210710.2721367),
              dict(pty, event="birth", ts=1791211020.1120718), dict(fork, event="birth", ts=1791211042.29143),
              dict(fork, event="exit", ts=1791211042.4399135)]
    (obs / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    (win / "base.json").write_text(json.dumps({"children": [], "count": 0, "cursor": c0, "gui_log": str(gui),
                                               "ts": 1791210997.84573}), encoding="utf-8")
    (win / "send.json").write_text(json.dumps({"children": [pty], "count": 1, "cursor": c1, "gui_log": str(gui),
                                               "ts": 1791211078.2101135}), encoding="utf-8")
    url = "ws://127.0.0.1:29194/api/pty?channel=c&resume=%s&attach=REDACTED&profile=orchestrator&token=REDACTED"
    (win / "console.txt").write_text(
        "[info] OSHWS ready 1791211003.841 /chat\n[info] OSHWS open 1791211016.353 %s\n"
        "[info] OSHWS url 1791211016.353 /chat resume=%s\n" % (url % REPLAY_SID, REPLAY_SID), encoding="utf-8")
    (guard / "3346.armed").write_text(json.dumps({"pid": 3346, "ppid": 3327, "netns": REPLAY_NS,
                                                  "home": "orchestrator"}), encoding="utf-8")
    selected = tmp_path / "selected.json"
    out = subprocess.run([sys.executable, str(HERE / "pty_select.py"), "gate", "initial", str(obs), str(guard),
                          "orchestrator", REPLAY_SID, REPLAY_SID, str(win), str(selected)],
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    lines = out.stdout.splitlines()
    assert lines[0] == "mode initial baseline 0 accepts 1 opens 1 ready 1 births 1 presend 1"
    assert "fork_descendant 3412 starttime 11187750 parent 3346/11185516 born 1791211042.29143 " \
           "exited 1791211042.4399135" in lines
    assert lines[-1] == "g2_ok yes"
    assert json.loads(selected.read_text(encoding="utf-8")) == [3346, 11185516]


def test_second_birth_without_a_matched_parent_still_fails():
    """(b) An exited second birth whose ppid is no recorded fingerprint still counts: births 2."""
    stray = child(pid=21, start=510, ppid=19)
    ok, lines, _ = initial(events=[ev("birth", 2.0, child()), ev("birth", 2.5, stray), ev("exit", 3.0, stray)])
    assert not ok
    assert lines[0].endswith("births 2 presend 1")
    assert not any(line.startswith("fork_descendant") for line in lines)


@pytest.mark.parametrize("case", ["parent_exited_before_birth", "reused_parent_pid", "exit_after_send"])
def test_exited_descendant_of_a_parent_not_live_at_its_birth_is_not_excluded(case):
    """(c) A historical parent, a reused parent PID, or an exit recorded after the send leaves the birth counted."""
    if case == "parent_exited_before_birth":
        old = child(pid=30, start=300)
        fork = child(pid=31, start=310, ppid=30, parent_start=300)
        events = [ev("birth", 0.2, old), ev("exit", 0.5, old), ev("birth", 2.0, child()), ev("birth", 2.5, fork),
                  ev("exit", 3.0, fork)]
    elif case == "reused_parent_pid":
        old = child(pid=30, start=300)
        fork = child(pid=31, start=1001, ppid=30, parent_start=999)
        events = [ev("birth", 0.2, old), ev("birth", 2.0, child()), ev("birth", 2.5, fork), ev("exit", 2.6, old),
                  ev("exit", 3.0, fork)]
    else:
        events = [ev("birth", 2.0, child()), ev("birth", 2.5, FORK), ev("exit", 9.5, FORK)]
    ok, lines, _ = initial(events=events)
    assert not ok
    assert lines[0].endswith("births 2 presend 1")
    assert not any(line.startswith("fork_descendant") for line in lines)


def test_parent_exits_while_child_live():
    """(c2) A parent live at the child's birth qualifies even when it exits before the child does."""
    p = child(pid=22, start=520, ppid=20, parent_start=500)
    b = child(pid=23, start=530, ppid=22, parent_start=520)
    ok, lines, chosen = initial(events=[ev("birth", 1.5, child()), ev("birth", 2.0, p), ev("birth", 3.0, b),
                                        ev("exit", 4.0, p), ev("exit", 5.0, b)])
    assert ok, lines
    assert chosen == FP
    assert lines[0].endswith("births 1 presend 1")
    assert "fork_descendant 23 starttime 530 parent 22/520 born 3.0 exited 5.0" in lines


@pytest.mark.parametrize("mode", ["initial", "reattach", "away", "close"])
def test_live_direct_descendant_fails_every_gate(mode):
    """(d) A matched direct descendant still live at the send is never left out, at any gate."""
    events = [ev("birth", 2.0 if mode == "initial" else 0.5, child()), ev("birth", 2.5, FORK), ev("exit", 3.0, FORK)]
    assert len(pty_select.fork_descendants(events, (1.0, 9.0), {})) == 1
    armed, guards = {20: {"netns": NS}, 21: {"netns": NS}}, {20, 21}
    if mode == "initial":
        ok, lines, _ = gate("initial", read(1.0), read(9.0, child(), FORK), events=events, armed=armed,
                            guard_pids=guards)
    elif mode == "reattach":
        ok, lines, _ = reattach(presend=read(9.0, child(), FORK), events=events, armed=armed, guard_pids=guards)
    else:
        ok, lines, _ = gate(mode, read(1.0, child()), read(9.0, child(), FORK), events=events, accepts=0,
                            br=browser(), armed=armed, guard_pids=guards)
    assert not ok
    assert not any(line.startswith("fork_descendant") for line in lines)


@pytest.mark.linux_only
def test_teardown_counts_a_live_recorded_fork_descendant(tmp_path):
    """(e) A fork the gates could leave out stays in the observer's teardown accounting while it lives."""
    mod = tmp_path / "mod" / "tui_gateway"
    mod.mkdir(parents=True)
    (mod / "__init__.py").write_text("", encoding="utf-8")
    stop = tmp_path / "stop"
    (mod / "entry.py").write_text(
        "import os, time\nos.fork()\nwhile not os.path.exists(os.environ['STOP']):\n    time.sleep(0.05)\n",
        encoding="utf-8")
    # cwd outside the repo, as in the observer test below: -m resolves the stub, not the real gateway.
    stub = subprocess.Popen([sys.executable, "-m", "tui_gateway.entry"], cwd=tmp_path,
                            env=dict(os.environ, PYTHONPATH=str(tmp_path / "mod"), STOP=str(stop)))
    known, recorded = {}, set()
    try:
        deadline = time.time() + 15
        while len(recorded) < 2 and time.time() < deadline:
            proc_observer.scan(os.getpid(), known, recorded, str(tmp_path / "events.jsonl"))
            time.sleep(0.05)
        assert len(recorded) == 2 and any(pid == stub.pid for pid, _ in recorded)
        (fork,) = [fp for fp in recorded if fp[0] != stub.pid]
        births = [json.loads(x) for x in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        stub_fp = [fp for fp in recorded if fp[0] == stub.pid][0]
        assert [(e["ppid"], e["parent_starttime"]) for e in births if e["pid"] == fork[0]] == [stub_fp]
        stub.kill()
        stub.wait()
        assert proc_observer.post_exit(0, recorded, wait_s=0.3)["survivors"] == [list(fork)]
    finally:
        # The fork is reparented once the stub dies, outside the conftest kill guard's subtree: it exits on a file.
        stop.write_text("", encoding="utf-8")
        stub.kill()
        stub.wait()
        assert proc_observer.post_exit(0, recorded, wait_s=10)["survivors"] == []


def _reconcile_dir(tmp_path, posts, excluded=(), window=(99.0, 110.0)):
    """A host-side scenario dir as win_close leaves it: guard/, openshell.log, usage files, pty-select-*.txt."""
    scen = tmp_path / "scenario"
    guard = scen / "guard"
    guard.mkdir(parents=True)
    router = []
    for pid, at in posts:
        recs = [{"kind": "probe", "method": "GET", "decision": "allowed", "status": 200, "sent": at, "ts": at},
                {"kind": "post", "method": "POST", "decision": "allowed", "status": 200, "sent": at + 1, "ts": at + 1}]
        with open(guard / ("%d.jsonl" % pid), "a", encoding="utf-8") as fh:
            for r in recs:
                fh.write(json.dumps(dict(r, pid=pid, home="orchestrator")) + "\n")
        router += ["[%.1f] routing proxy inference request method=GET path=/v1/models" % (at + 0.1),
                   "[%.1f] routing proxy inference request method=POST path=/v1/chat/completions" % (at + 1.1)]
    (scen / "openshell.log").write_text("\n".join(router) + "\n", encoding="utf-8")
    in_window = sum(1 for _, at in posts if window[0] <= at + 1 <= window[1])
    (scen / "usage-before.txt").write_text("usage orchestrator s1 - https://inference.local/v1 0\n", encoding="utf-8")
    (scen / "usage-after.txt").write_text("usage orchestrator s1 - https://inference.local/v1 %d\n" % in_window,
                                          encoding="utf-8")
    (scen / "pty-select-s1.txt").write_text(
        "mode initial baseline 0 accepts 1 opens 1 ready 1 births 1 presend 1\n"
        + "".join("fork_descendant %d starttime 11187750 parent 3346/11185516 born 1.0 exited 1.1\n" % pid
                  for pid in excluded) + "g2_ok yes\n", encoding="utf-8")
    return scen


def _reconcile(scen, window):
    return subprocess.run([sys.executable, str(HERE / "route_reconcile.py"), str(scen / "guard"),
                           str(scen / "openshell.log"), str(scen / "usage-before.txt"), str(scen / "usage-after.txt"),
                           str(window[0]), str(window[1])], capture_output=True, text=True)


def test_excluded_fork_post_rejected(tmp_path):
    """(f) An allowed POST from an excluded fork PID fails G4 even when the selected PID also has allowed POSTs."""
    posts = [(3346, 100.0), (3412, 103.0)]
    out = _reconcile(_reconcile_dir(tmp_path / "x", posts, excluded=[3412]), (99.0, 110.0))
    assert out.returncode == 1, out.stdout
    assert "reconcile_ok no excluded pid 3412" in out.stdout
    assert "excluded_pids 3412" in out.stdout.splitlines()
    control = _reconcile(_reconcile_dir(tmp_path / "c", posts), (99.0, 110.0))
    assert control.returncode == 0, control.stdout
    assert "excluded_pids -" in control.stdout.splitlines()


def test_excluded_fork_post_only_in_the_run_wide_window_fails(tmp_path):
    """E7.4: an excluded PID's POST outside every turn window still fails the run-wide `win_close all`."""
    posts = [(3346, 100.0), (3412, 106.0)]
    turn = _reconcile(_reconcile_dir(tmp_path / "t", posts, excluded=[3412], window=(99.0, 102.5)), (99.0, 102.5))
    assert turn.returncode == 0, turn.stdout
    run_wide = _reconcile(_reconcile_dir(tmp_path / "a", posts, excluded=[3412]), (99.0, 110.0))
    assert run_wide.returncode == 1, run_wide.stdout
    assert "reconcile_ok no excluded pid 3412" in run_wide.stdout
