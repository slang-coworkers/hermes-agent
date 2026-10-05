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


def child(pid=20, start=500, resume="sidA", home="orchestrator", netns=NS):
    return {"pid": pid, "starttime": start, "resume": resume, "home": home, "netns": netns}


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
    allowed = {"pid", "ppid", "starttime", "netns", "home", "resume", "event", "ts"}
    assert all(set(e) <= allowed for e in events + live["children"])
