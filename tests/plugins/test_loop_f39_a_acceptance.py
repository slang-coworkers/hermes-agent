"""LOOP-F39.a acceptance: the AC-LOOP-F39-6 escalation is one recorded owner-input act, not a marker position.

Ships as tests/plugins/test_loop_f39_a_acceptance.py. One test per `pytest:` row of the ADR's `## Acceptance criteria`
(reports/loop-f39.a.md), in id order; docstring line 1 is the criterion. AC-1/AC-2 run the shipped FLEET-F62.a oracle
(a read-only stdlib script) as a subprocess over synthetic stores shaped like one live run, the way
test_upg_f67_acceptance.py does; AC-3 drives the real kanban_block handler on an isolated board. No network, no model,
nothing under the real home.
"""
import copy
import json
import math
import re
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[2]
ORACLE = REPO / "tests" / "e2e-scenarios" / "FLEET-F62.a" / "oracle.py"
JOB, EXEC = "job-1", "e1"
NONCE = "f39-1793055854"
TOK_A, TOK_B = f"F39A-{NONCE}", f"F39B-{NONCE}"
CARD_A, CARD_B = f"f39a-{NONCE}", f"f39b-{NONCE}"
MARK_B = f"F39-ESC:{TOK_B}"
CLAIMED = datetime(2026, 11, 12, 14, 0, 0, 250000, tzinfo=UTC)
FINISHED = CLAIMED + timedelta(minutes=3)
LO, HI = math.floor(CLAIMED.timestamp()), math.ceil(FINISHED.timestamp())
BLOCKED_AT = LO - 25 * 3600
CRON_INBOUND = f'[Cronjob "supervise-issues" output] Nudge worker: {TOK_A} post the unblock step. {MARK_B} escalated.'
LEAD_REPLY = f"{MARK_B} card {CARD_B} blocked 25h, blocker: awaiting worker input. Nudge sent to worker for {TOK_A}."
WORKER_REPLY = "Message from 🤖 worker (@worker): (no reply)"


def _call(cid, name, **args):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def _block_rows(sid, task=CARD_B, kind="needs_input", cid="k-1", ok=True):
    """An assistant kanban_block call and its correlated tool result, as the handler returns them."""
    call = _call(cid, "kanban_block", task_id=task, kind=kind, reason=f"F39-ESC:{TOK_B} stuck 25h, worker silent")
    result = ({"ok": True, "task_id": task, "run_id": None, "status": "blocked", "block_kind": kind} if ok
              else {"ok": False, "error": f"could not block {task} (unknown id or not in running/ready)"})
    return [(sid, "assistant", "", [call], None, None), (sid, "tool", json.dumps(result), None, "kanban_block", cid)]


def _good():
    """One passing run: the sweep (cron session C) escalates card B; the cron-delivered Bot Chat turn (S) nudges card
    A; the worker's relayed reply is the next user row in S, which closes that turn."""
    nudge = _call("call-1", "message_agent", target="worker", message=f"{TOK_A} [supervise-issues nudge] post the step")
    return {
        "orch_sessions": {"C": "supervise-issues cron run", "S": "Bot Chat", "O": "Other chat"},
        "cron": [("C", "user", "Run the supervise-issues sweep over the fleet's in-flight work.", None, None, None),
                 *_block_rows("C"), ("C", "assistant", CRON_INBOUND, None, None, None)],
        "turn": [("S", "user", CRON_INBOUND, None, None, None),
                 ("S", "assistant", "", [nudge], None, None),
                 ("S", "tool", json.dumps({"status": "sent", "delivery_id": "dlv-1"}), None, "message_agent", "call-1"),
                 ("S", "assistant", LEAD_REPLY, None, None, None)],
        "after": [("S", "user", WORKER_REPLY, None, None, None)],
        "admissions": [(f"cron:{JOB}:{EXEC}", "cron-owner", "C")],
        "worker_rows": [("W", "user", f"Message from 🤖 orchestrator (@orchestrator): {TOK_A} [supervise-issues nudge] "
                                      "post the step", None, None, None)],
        "comments_b": [("orchestrator", f"[supervise-issues nudge] ping {k} — worker, {TOK_B}") for k in (1, 2)],
        "post_events": [(CARD_B, "blocked", {"kind": "needs_input", "reason": f"F39-ESC:{TOK_B} stuck 25h",
                                             "classified_in_place": True}, LO + 60)],
        "baseline": {},
    }


def _state_db(path, sessions, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT)")
        c.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,"
                  " tool_calls TEXT, tool_name TEXT, tool_call_id TEXT)")
        c.execute("CREATE TABLE session_admissions (seq INTEGER PRIMARY KEY, request_id TEXT, principal_id TEXT,"
                  " target_session_id TEXT)")
        c.executemany("INSERT INTO sessions VALUES (?, ?)", sessions.items())
        c.executemany(
            "INSERT INTO messages (session_id, role, content, tool_calls, tool_name, tool_call_id) VALUES (?,?,?,?,?,?)",
            [(s, r, t, json.dumps(tc) if tc is not None else None, tn, tid) for (s, r, t, tc, tn, tid) in rows])


def _run(root, spec):
    """Write ``spec`` as the run's stores under ``root``, run the oracle; return (exit code, line-1 metrics, reasons)."""
    _state_db(root / "profiles" / "orchestrator" / "state.db", spec["orch_sessions"],
              spec["cron"] + spec["turn"] + spec["after"])
    with sqlite3.connect(root / "profiles" / "orchestrator" / "state.db") as c:
        c.executemany("INSERT INTO session_admissions (request_id, principal_id, target_session_id) VALUES (?,?,?)",
                      spec["admissions"])
    _state_db(root / "profiles" / "worker" / "state.db", {"W": "Bot Chat"}, spec["worker_rows"])
    (root / "profiles" / "orchestrator" / "cron").mkdir()
    with sqlite3.connect(root / "profiles" / "orchestrator" / "cron" / "executions.db") as c:
        c.execute("CREATE TABLE executions (id TEXT PRIMARY KEY, job_id TEXT, status TEXT, source TEXT,"
                  " claimed_at TEXT, finished_at TEXT)")
        c.execute("INSERT INTO executions VALUES (?, ?, 'completed', 'builtin', ?, ?)",
                  (EXEC, JOB, CLAIMED.isoformat(), FINISHED.isoformat()))
    kanban = root / "kanban.db"
    with sqlite3.connect(kanban) as c:
        c.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, assignee TEXT, block_kind TEXT)")
        c.execute("CREATE TABLE task_comments (id INTEGER PRIMARY KEY, task_id TEXT, author TEXT, body TEXT)")
        c.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT,"
                  " created_at INTEGER)")
        c.executemany("INSERT INTO tasks VALUES (?, 'blocked', 'worker', NULL)", [(CARD_A,), (CARD_B,)])
        pre = [(CARD_A, "blocked", {"reason": "awaiting worker input"}, BLOCKED_AT),
               (CARD_B, "blocked", {"reason": "awaiting worker input"}, BLOCKED_AT)]
        pre += [(CARD_B, "commented", {"author": a, "len": len(b)}, BLOCKED_AT + 60 * k)
                for k, (a, b) in enumerate(spec["comments_b"], 1)]
        ins = "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)"
        pre_ids = {t: [] for t in (CARD_A, CARD_B)}
        for (t, k, p, ts) in pre:
            pre_ids[t].append(c.execute(ins, (t, k, json.dumps(p), ts)).lastrowid)
        b_nudges = [c.execute("INSERT INTO task_comments (task_id, author, body) VALUES (?, ?, ?)",
                              (CARD_B, a, b)).lastrowid for (a, b) in spec["comments_b"]]
        c.execute("INSERT INTO task_comments (task_id, author, body) VALUES (?, 'orchestrator', ?)",
                  (CARD_A, f"[supervise-issues nudge] {TOK_A}: worker, post the unblock step"))
        for (t, k, p, ts) in spec["post_events"]:
            c.execute(ins, (t, k, json.dumps(p), ts))
            if p.get("kind"):
                c.execute("UPDATE tasks SET block_kind = ? WHERE id = ?", (p["kind"], t))
        if "card_b_block_kind_now" in spec:
            c.execute("UPDATE tasks SET block_kind = ? WHERE id = ?", (spec["card_b_block_kind_now"], CARD_B))
        for (author, body) in spec.get("late_comments_b", []):
            c.execute("INSERT INTO task_comments (task_id, author, body) VALUES (?, ?, ?)", (CARD_B, author, body))
    nudge_ids = [i for i, (a, b) in zip(b_nudges, spec["comments_b"]) if b.startswith("[supervise-issues nudge]")]
    bl = {"base_orch": 0, "base_worker": 0, "exec_base": [], "card_a_nudge_ids": [], "card_b_nudge_ids": nudge_ids,
          "card_a_status": "blocked", "card_a_assignee": "worker", "card_a_worker_comments": 0,
          "card_b_status": "blocked", "card_b_assignee": "worker", "card_b_worker_comments": 0,
          "card_b_block_kind": None, "card_a_event_ids": pre_ids[CARD_A], "card_b_event_ids": pre_ids[CARD_B]}
    bl.update(spec["baseline"])
    (root / "baselines.json").write_text(json.dumps(bl), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(ORACLE), "--home", str(root), "--job-id", JOB, "--nonce", NONCE,
         "--kanban-db", str(kanban), "--baselines", str(root / "baselines.json")],
        capture_output=True, text=True, timeout=120, check=False)
    lines = proc.stdout.splitlines()
    metrics = dict(re.findall(r"(\w+)=(\S+)", lines[0])) if lines else {}
    reasons = next((ln for ln in lines if ln.startswith("FAIL reasons: ")), "")
    return proc.returncode, metrics, reasons + proc.stderr


def _variant(**changes):
    spec = copy.deepcopy(_good())
    for key, value in changes.items():
        spec[key] = value(spec) if callable(value) else value
    return spec


def _without_act(spec):
    return [r for r in spec["cron"] if r[4] != "kanban_block" and not (r[3] and "kanban_block" in json.dumps(r[3]))]


def _reply(text):
    return lambda s: s["turn"][:3] + [("S", "assistant", text, None, None, None)]


def _third_nudge(spec):
    call = _call("call-2", "message_agent", target="worker", message=f"{TOK_B} [supervise-issues nudge] third ping")
    ack = json.dumps({"status": "sent", "delivery_id": "dlv-2"})
    return spec["turn"][:3] + [("S", "assistant", "", [call], None, None),
                               ("S", "tool", ack, None, "message_agent", "call-2"), spec["turn"][3]]


def test_ac_loop_f39_a_1(tmp_path):
    """The re-specified oracle PASSes a recorded-good store. It FAILs that store when exactly one escalation correlation is broken: no act, wrong author, a bot destination, the wrong card, no prior non-response, or outside the cron turn. A valid act never FAILs on reply wording, marker position or a repeated failed call."""
    no_act = lambda s: _without_act(s)
    valid = {
        "act in the cron session": _variant(),
        "reply carries no marker": _variant(turn=_reply("Sweep done: one nudge sent, one card escalated.")),
        "marker on the reply's last line": _variant(turn=_reply(f"Sweep done.\nOne nudge sent.\n{MARK_B}")),
        "a refused repeat call": _variant(
            turn=lambda s: s["turn"][:3] + _block_rows("S", cid="k-2", ok=False) + s["turn"][3:]),
        "event on the window's lower edge": _variant(post_events=lambda s: [(*s["post_events"][0][:3], LO)]),
        "event on the window's upper edge": _variant(post_events=lambda s: [(*s["post_events"][0][:3], HI)]),
    }
    for i, (name, spec) in enumerate(valid.items()):
        rc, m, why = _run(tmp_path / f"ok-{i}", spec)
        assert rc == 0 and m.get("escalations") == "1", f"valid escalation FAILed [{name}]: {m} {why}"

    def act_on(sid="C", task=CARD_B, kind="needs_input", ok=True):
        return lambda s: no_act(s) + _block_rows(sid, task=task, kind=kind, ok=ok)

    def event(task=CARD_B, kind="needs_input", ts=LO + 60):
        return [(task, "blocked", {"kind": kind, "reason": "stuck", "classified_in_place": True}, ts)]

    broken = {
        "no act (the reply still leads with the marker)": (_variant(cron=no_act, post_events=[]), "escalations", "0"),
        "only a refused act": (_variant(cron=act_on(ok=False), post_events=[]), "escalations", "0"),
        "wrong author: the act is in the worker's store": (
            _variant(cron=no_act, worker_rows=lambda s: s["worker_rows"] + _block_rows("W")), "escalations", "0"),
        "bot destination: a third nudge instead": (
            _variant(cron=no_act, post_events=[], turn=_third_nudge), "card_b_calls", "1"),
        "bot destination: a non-owner block kind": (
            _variant(cron=act_on(kind="transient"), post_events=event(kind="transient")), "escalations", "0"),
        "wrong card": (_variant(cron=act_on(task=CARD_A), post_events=event(task=CARD_A)), "card_a_escalations", "1"),
        "no prior non-response: one nudge only": (
            _variant(comments_b=lambda s: s["comments_b"][:1]), None, "card_b_nudge_ids"),
        "no prior non-response: the assignee answered": (
            _variant(late_comments_b=[("worker", "on it")]), None, "worker-authored"),
        "no prior non-response: already escalated before the fire": (
            _variant(baseline={"card_b_block_kind": "needs_input"}), None, "card_b_block_kind"),
        "uncorrelated: the ok result sits in another session": (_variant(
            cron=lambda s: [(r[0] if r[4] != "kanban_block" else "O", *r[1:]) for r in s["cron"]]), "escalations", "0"),
        "not human-facing any more: card B's needs_input was cleared": (
            _variant(card_b_block_kind_now=None), None, "block_kind=None"),
        "outside the cron turn: an unrelated session": (_variant(cron=act_on(sid="O")), "escalations", "0"),
        "outside the cron turn: the cron-delivered Bot Chat turn": (_variant(
            cron=no_act, turn=lambda s: s["turn"][:1] + _block_rows("S") + s["turn"][1:]), "escalations", "0"),
        "outside the cron turn: another execution's admission": (
            _variant(admissions=[(f"cron:{JOB}:e0", "cron-owner", "C")]), "escalations", "0"),
        "outside the cron turn: not a cron-owner admission": (
            _variant(admissions=[(f"cron:{JOB}:{EXEC}", "someone-else", "C")]), "escalations", "0"),
        "outside the cron turn: a later Bot Chat turn": (
            _variant(cron=no_act, after=lambda s: s["after"] + _block_rows("S")), "escalations", "0"),
        "outside the cron turn: event before the window": (_variant(post_events=event(ts=LO - 1)), "escalations", "0"),
        "outside the cron turn: event after the window": (_variant(post_events=event(ts=HI + 1)), "escalations", "0"),
    }
    for i, (name, (spec, metric, expect)) in enumerate(broken.items()):
        rc, m, why = _run(tmp_path / f"bad-{i}", spec)
        assert rc == 1, f"broken correlation PASSed [{name}]: {m}"
        if metric is None:
            assert expect in why, f"[{name}] expected a red '{expect}' reason, got: {why}"
        else:
            assert m.get(metric) == expect, f"[{name}] expected {metric}={expect}: {m} {why}"


def test_ac_loop_f39_a_2(tmp_path):
    """Negative control: a store that the old byte-prefix rule rejects (F68 shape, `F39-ESC:` at char 757 of the reply) but that carries the recorded act PASSes the new oracle. The same store with the act removed FAILs, and the escalation clause is its only red reason."""
    lead = "Escalation token: "
    head = ("Sweep complete; card A nudged through message_agent, delivery queued. " * 12)[: 757 - len(lead)]
    f68_reply = f"{head}{lead}{MARK_B}. Card {CARD_B} has been blocked 25h with two unanswered nudges."
    assert f68_reply.index(MARK_B) == 757 and not f68_reply.startswith(MARK_B)

    rc, m, why = _run(tmp_path / "with-act", _variant(turn=_reply(f68_reply)))
    assert rc == 0 and m.get("escalations") == "1", f"F68-shape escalation FAILed: {m} {why}"

    rc, m, why = _run(tmp_path / "act-removed", _variant(turn=_reply(f68_reply), cron=_without_act, post_events=[]))
    assert rc == 1 and m.get("escalations") == "0", f"store without the act PASSed: {m}"
    reasons = why.splitlines()[0]
    assert reasons.startswith("FAIL reasons: ") and reasons.count(";") == 0 and "escalations" in reasons, reasons


def test_ac_loop_f39_a_3(tmp_path, monkeypatch):
    """(derived) On a card seeded the way the live scenario seeds card B, the real `kanban_block` handler, called from a non-worker session with kind `needs_input`, records exactly one owner-input `blocked` event, returns the result shape the oracle parses, and leaves status, assignee and comments unchanged. A second call is refused. The dashboard's Kanban drawer API serves that event to the human, payload `"kind":"needs_input"`, under the card's token."""
    import hermes_cli.kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_HOME", "HERMES_KANBAN_DB"):
        monkeypatch.delenv(var, raising=False)   # an orchestrator turn, not a dispatcher-owned worker
    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")
    kb.init_db()
    import tools.kanban_tools as kt

    with kbc.connect() as conn:   # the scenario's §Setup 5 seed, column for column
        for cid, tok in ((CARD_A, TOK_A), (CARD_B, TOK_B)):
            conn.execute("INSERT INTO tasks (id,title,status,assignee,created_by,created_at) VALUES (?,?,?,?,?,?)",
                         (cid, f"Silent chain {tok}", "blocked", "worker", "test", BLOCKED_AT))
            conn.execute("INSERT INTO task_events (task_id,kind,payload,created_at) VALUES (?,?,?,?)",
                         (cid, "blocked", json.dumps({"reason": "awaiting worker input"}), BLOCKED_AT))
        for k in (1, 2):
            conn.execute("INSERT INTO task_comments (task_id,author,body,created_at) VALUES (?,?,?,?)",
                         (CARD_B, "orchestrator", f"[supervise-issues nudge] ping {k} — worker, {TOK_B}",
                          BLOCKED_AT + k * 60))
        conn.commit()
        before = conn.execute("SELECT MAX(id) FROM task_events").fetchone()[0]

    args = {"task_id": CARD_B, "kind": "needs_input", "reason": f"F39-ESC:{TOK_B} stuck 25h; worker silent"}
    out = json.loads(kt._handle_block(dict(args)))
    assert out.get("ok") is True and out.get("task_id") == CARD_B and out.get("block_kind") == "needs_input", out
    again = kt._handle_block(dict(args))
    try:
        again_parsed = json.loads(again)
    except ValueError:
        again_parsed = {}
    assert again_parsed.get("ok") is not True, f"a second escalation of the same card was accepted: {again}"

    with kbc.connect() as conn:
        rows = [tuple(r) for r in conn.execute(
            "SELECT task_id, kind, payload FROM task_events WHERE id > ? ORDER BY id", (before,))]
        card_b, card_a = kb.get_task(conn, CARD_B), kb.get_task(conn, CARD_A)
        comments = kb.list_comments(conn, CARD_B)
    assert [(t, k) for (t, k, _p) in rows] == [(CARD_B, "blocked")], rows
    payload = json.loads(rows[0][2])
    assert payload.get("kind") == "needs_input", payload
    assert (card_b.status, card_b.assignee, card_b.block_kind) == ("blocked", "worker", "needs_input")
    assert len(comments) == 2 and card_a.block_kind is None

    import importlib.util
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    api = importlib.util.spec_from_file_location(
        "loop_f39_a_kanban_plugin_api", REPO / "plugins" / "kanban" / "dashboard" / "plugin_api.py")
    mod = importlib.util.module_from_spec(api)
    sys.modules[api.name] = mod
    api.loader.exec_module(mod)
    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/kanban")
    drawer = TestClient(app).get(f"/api/plugins/kanban/tasks/{CARD_B}")
    assert drawer.status_code == 200, drawer.text
    body = drawer.json()
    assert TOK_B in body["task"]["title"], body["task"]
    shown = [e for e in body["events"] if e["id"] > before]
    assert [(e["kind"], (e.get("payload") or {}).get("kind")) for e in shown] == [("blocked", "needs_input")], shown
    assert '"kind":"needs_input"' in json.dumps(shown[0]["payload"], separators=(",", ":"))

    from gateway.kanban_watchers_notifier import diagnostic_event
    assert diagnostic_event(SimpleNamespace(kind="blocked", payload=payload)) is False   # an owner decision
    assert diagnostic_event(SimpleNamespace(kind="blocked", payload={**payload, "kind": "transient"})) is True
