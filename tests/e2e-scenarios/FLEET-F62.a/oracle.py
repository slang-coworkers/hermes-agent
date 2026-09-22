#!/usr/bin/env python3
"""Strict, exactly-one oracle for AC-LOOP-F39-6 (FLEET-F62.a).

Ships as tests/e2e-scenarios/FLEET-F62.a/oracle.py. Invoked from the scenario ## Steps AFTER the single
force-due fire has settled. Read-only (stdlib sqlite3, read-only URIs — the gateway may still be live);
never writes. Exits 0 and prints one `PASS <metrics>` line, else exits 1 and prints `FAIL <metrics>` plus a
`FAIL reasons:` line naming every failed metric.

Proves, each exactly once and bound to the ONE cron-delivered Bot Chat turn (the assistant rows between the
cron inbound and the next user inbound in that same session), with the false-pass paths closed:
 (1) the rendered cron fired through the scheduler — exactly one NEW execution for the job, status completed;
 (2) deliver:bot-chat delivered into the orchestrator's own 'Bot Chat' — exactly one cron inbound;
 (a) the cron-delivered turn issued exactly one message_agent nudge to `worker` carrying card A's token —
     and exactly one message_agent call exists globally (no untagged extra nudge) — correlated to exactly
     one result whose parsed JSON is {"status":"sent",...};
 (b) that nudge landed as exactly one role='user' inbound in worker's exact 'Bot Chat' with the
     server-stamped `Message from 🤖 orchestrator (@orchestrator): ` envelope;
 (c) card B (its two prior unanswered supervisor nudges recorded in the baseline) drew exactly one
     human-facing F39-ESC escalation in that same turn and ZERO third nudge — no message_agent call, no
     worker inbound, and its on-card [supervise-issues nudge] task_comments set is unchanged from baseline
     (exactly two), card B still assigned to `worker`, zero worker-authored comments;
 plus card A gained exactly one on-card nudge comment and sent_nudges == required_nudges == 1.
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path


def _connect_ro(path: Path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True) if path.exists() else None


def _rows(conn, sql, params=()):
    return conn.execute(sql, params).fetchall() if conn is not None else []


def _one(conn, sql, params=(), default=0):
    r = _rows(conn, sql, params)
    return r[0][0] if r and r[0] and r[0][0] is not None else default


def _iter_message_agent_calls(conn, base_id):
    """Yield (row_id, session_id, tool_call_id, target, message) for every message_agent entry in every
    post-baseline assistant tool_calls array of the orchestrator's exact 'Bot Chat' session (one row may
    hold many calls)."""
    sql = ("SELECT m.id, m.session_id, m.tool_calls FROM messages m JOIN sessions s ON s.id=m.session_id "
           "WHERE m.id>? AND m.role='assistant' AND s.title='Bot Chat' AND m.tool_calls IS NOT NULL")
    for row_id, sid, tc_json in _rows(conn, sql, (base_id,)):
        try:
            calls = json.loads(tc_json)
        except (TypeError, ValueError):
            continue
        for call in calls if isinstance(calls, list) else []:
            fn = (call or {}).get("function") or {}
            if fn.get("name") != "message_agent":
                continue
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            args = args or {}
            target = str(args.get("target") or args.get("to") or "")
            message = str(args.get("message") or args.get("text") or "")
            yield row_id, sid, call.get("id"), target, message


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True, help="testbed HERMES_HOME root")
    ap.add_argument("--job-id", required=True)
    ap.add_argument("--nonce", required=True)
    ap.add_argument("--kanban-db", required=True,
                    help="resolved kanban DB path (respects the testbed HERMES_KANBAN_HOME/HERMES_KANBAN_DB)")
    ap.add_argument("--baselines", required=True,
                    help="JSON from the baseline step: base_orch, base_worker, exec_base, card_a_nudge_ids, "
                         "card_b_nudge_ids, card_b_status, card_b_assignee, card_b_worker_comments")
    a = ap.parse_args()

    home = Path(a.home)
    bl = json.loads(Path(a.baselines).read_text())
    base_orch, base_worker = int(bl["base_orch"]), int(bl["base_worker"])
    exec_base = set(bl.get("exec_base") or [])
    base_a_ids = set(bl.get("card_a_nudge_ids") or [])
    base_b_ids = set(bl.get("card_b_nudge_ids") or [])
    base_b_assignee = bl.get("card_b_assignee")
    base_b_status = bl.get("card_b_status")
    base_b_worker_comments = bl.get("card_b_worker_comments")
    tok_a, tok_b = f"F39A-{a.nonce}", f"F39B-{a.nonce}"
    card_a, card_b = f"f39a-{a.nonce}", f"f39b-{a.nonce}"
    BIG = 1 << 62

    orch = _connect_ro(home / "profiles" / "orchestrator" / "state.db")
    worker = _connect_ro(home / "profiles" / "worker" / "state.db")
    execs = _connect_ro(home / "profiles" / "orchestrator" / "cron" / "executions.db")
    kanban = _connect_ro(Path(a.kanban_db))
    fails, m = [], {}

    # 1. cron fired THROUGH THE SCHEDULER: exactly one NEW execution, status completed AND source builtin
    #    (the tick uses source='builtin' at scheduler.py:8142; a direct `hermes cron run` uses 'direct'
    #    at :7179, so requiring 'builtin' rejects a direct in-process run).
    new_exec = [(rid, st, src) for (rid, st, src) in _rows(
        execs, "SELECT id,status,source FROM executions WHERE job_id=?", (a.job_id,)) if rid not in exec_base]
    m["cron"] = len(new_exec)
    if len(new_exec) != 1:
        fails.append(f"cron(new_executions)={len(new_exec)} != 1")
    else:
        if new_exec[0][1] != "completed":
            fails.append(f"cron_status={new_exec[0][1]!r} != 'completed'")
        if new_exec[0][2] != "builtin":
            fails.append(f"cron_source={new_exec[0][2]!r} != 'builtin' (must fire via the scheduler tick)")

    # 2. deliver:bot-chat inbound in the orchestrator's own 'Bot Chat' — exactly one; bounds the turn.
    cron_rows = _rows(orch,
        "SELECT m.id, m.session_id FROM messages m JOIN sessions s ON s.id=m.session_id "
        "WHERE m.id>? AND m.role='user' AND s.title='Bot Chat' "
        "AND m.content LIKE '[Cronjob \"supervise-issues\" output%'", (base_orch,))
    m["cron_inbound"] = len(cron_rows)
    turn_sid, turn_lo, turn_hi = None, None, None
    if len(cron_rows) != 1:
        fails.append(f"cron_inbound={len(cron_rows)} != 1")
    else:
        turn_lo, turn_sid = cron_rows[0]
        turn_hi = _one(orch, "SELECT MIN(id) FROM messages WHERE session_id=? AND role='user' AND id>?",
                       (turn_sid, turn_lo), default=BIG) or BIG   # next user inbound bounds this turn

    def _in_turn(rid, sid):
        return turn_sid is not None and sid == turn_sid and turn_lo < rid < turn_hi

    # (a) message_agent: exactly ONE call globally (no untagged extra), it targets worker + carries token A
    #     AND is inside the cron-delivered turn; correlated to exactly one {"status":"sent"} result.
    all_calls = list(_iter_message_agent_calls(orch, base_orch))
    m["sent_nudges"] = len(all_calls)
    m["required_nudges"] = 1
    if len(all_calls) != 1:
        fails.append(f"sent_nudges(total message_agent calls)={len(all_calls)} != 1")
    a_calls = [(rid, cid) for (rid, sid, cid, tgt, msg) in all_calls
               if tok_a in msg and tgt in ("worker", "@worker") and _in_turn(rid, sid)]
    m["message_agent_calls"] = len(a_calls)
    if len(a_calls) != 1:
        fails.append(f"message_agent_calls(cardA->worker, in cron turn)={len(a_calls)} != 1")
    m["card_b_calls"] = sum(1 for (_r, _s, _c, _t, msg) in all_calls if tok_b in msg)
    if m["card_b_calls"] != 0:
        fails.append(f"card_b_calls={m['card_b_calls']} != 0")

    a_ids = tuple(cid for (_r, cid) in a_calls if cid)
    sent = 0
    if orch is not None and a_ids:
        res = _rows(orch, "SELECT content FROM messages WHERE id>? AND role='tool' "
                    "AND tool_name='message_agent' AND tool_call_id IN (%s)"
                    % ",".join("?" * len(a_ids)), (base_orch, *a_ids))
        if len(res) == 1:
            try:
                p = json.loads(res[0][0])
                sent = 1 if isinstance(p, dict) and p.get("status") == "sent" else 0
            except (TypeError, ValueError):
                sent = 0
    m["sent_results"] = sent
    if sent != 1:
        fails.append(f"sent_results(exactly one status:'sent', correlated by tool_call_id)={sent} != 1")

    # (b) card A nudge landed in worker's exact 'Bot Chat' — exactly one, server-stamped envelope.
    m["worker_bot_chat_inbounds"] = _one(worker,
        "SELECT COUNT(*) FROM messages m JOIN sessions s ON s.id=m.session_id "
        "WHERE m.id>? AND m.role='user' AND s.title='Bot Chat' "
        "AND m.content LIKE 'Message from 🤖 orchestrator (@orchestrator): %'||?||'%'", (base_worker, tok_a))
    if m["worker_bot_chat_inbounds"] != 1:
        fails.append(f"worker_bot_chat_inbounds(cardA)={m['worker_bot_chat_inbounds']} != 1")
    m["card_b_inbounds"] = _one(worker,
        "SELECT COUNT(*) FROM messages WHERE id>? AND role='user' AND content LIKE '%'||?||'%'",
        (base_worker, tok_b))
    if m["card_b_inbounds"] != 0:
        fails.append(f"card_b_inbounds={m['card_b_inbounds']} != 0")

    # (c) exactly one human-facing escalation for card B, inside the cron-delivered turn: the assistant
    # message must BEGIN with `F39-ESC:<card-B-token>` (a quoted/negated mention elsewhere does not count).
    # substr(...)=? is a CASE-SENSITIVE (BINARY) prefix match — LIKE is ASCII case-insensitive by default
    # (a lowercase `f39-esc:` would false-pass the exact uppercase marker the SKILL.md mandates).
    esc_prefix = f"F39-ESC:{tok_b}"
    esc = [rid for (rid,) in _rows(orch,
        "SELECT m.id FROM messages m WHERE m.id>? AND m.role='assistant' AND m.session_id=? "
        "AND substr(m.content,1,length(?))=?",
        (base_orch, turn_sid, esc_prefix, esc_prefix))] if turn_sid is not None else []
    esc = [rid for rid in esc if turn_lo < rid < turn_hi]
    m["escalations"] = len(esc)
    if len(esc) != 1:
        fails.append(f"escalations(cardB, in cron turn)={len(esc)} != 1")

    # card A gained exactly one NEW orchestrator-authored on-card nudge comment carrying token A
    # (post-baseline delta — card A must have had zero prior nudges, so a pre-existing comment can't pass).
    if bl.get("card_a_status") != "blocked":
        fails.append(f"baseline card_a_status={bl.get('card_a_status')!r} != 'blocked'")
    if bl.get("card_a_assignee") != "worker":
        fails.append(f"baseline card_a_assignee={bl.get('card_a_assignee')!r} != 'worker'")
    if bl.get("card_a_worker_comments") not in (0,):
        fails.append(f"baseline card_a_worker_comments={bl.get('card_a_worker_comments')!r} != 0")
    if base_a_ids:
        fails.append(f"baseline card_a_nudge_ids={len(base_a_ids)} != 0 (card A must have no prior nudge)")
    # Count ALL new orchestrator nudge comments (any token) → exactly one, and that one carries token A
    # (filtering by token first would let a second, token-less nudge slip through).
    rows_a = {rid: body for (rid, body) in _rows(kanban,
        "SELECT id, body FROM task_comments WHERE task_id=? AND author='orchestrator' "
        "AND body LIKE '[supervise-issues nudge]%'", (card_a,))}
    new_a = [rid for rid in rows_a if rid not in base_a_ids]
    m["card_a_new_comments"] = len(new_a)
    if len(new_a) != 1:
        fails.append(f"card_a_new_comments(new orchestrator nudge comments)={len(new_a)} != 1")
    elif tok_a not in rows_a[new_a[0]]:
        fails.append("card_a new nudge comment does not carry token A")

    # card B: the two prior SUPERVISOR nudges pre-existed (baseline preconditions) and are UNCHANGED post-fire
    # (no third), the card stayed blocked/assigned to worker, and the assignee never responded.
    if base_b_assignee != "worker":
        fails.append(f"baseline card_b_assignee={base_b_assignee!r} != 'worker'")
    if base_b_status != "blocked":
        fails.append(f"baseline card_b_status={base_b_status!r} != 'blocked'")
    if base_b_worker_comments not in (0,):
        fails.append(f"baseline card_b_worker_comments={base_b_worker_comments!r} != 0")
    now_b_ids = {rid for (rid,) in _rows(kanban,
        "SELECT id FROM task_comments WHERE task_id=? AND author='orchestrator' "
        "AND body LIKE '[supervise-issues nudge]%'", (card_b,))}
    m["card_b_nudges"] = len(now_b_ids)
    if len(base_b_ids) != 2:
        fails.append(f"baseline card_b_nudge_ids={len(base_b_ids)} != 2 (pre-fire nudges not seeded/captured)")
    if now_b_ids != base_b_ids:
        fails.append("card_b supervisor-nudge comment set changed since baseline (a third nudge or edit)")
    cur_b = (_rows(kanban, "SELECT status, assignee FROM tasks WHERE id=?", (card_b,)) or [("", "")])[0]
    if cur_b[0] != "blocked":
        fails.append(f"card_b status={cur_b[0]!r} != 'blocked' post-fire")
    if cur_b[1] != "worker":
        fails.append(f"card_b assignee={cur_b[1]!r} != 'worker' post-fire")
    if _one(kanban, "SELECT COUNT(*) FROM task_comments WHERE task_id=? AND author='worker'", (card_b,)) != 0:
        fails.append("card_b has a worker-authored comment (assignee responded)")

    order = ["cron", "cron_inbound", "message_agent_calls", "sent_results", "worker_bot_chat_inbounds",
             "escalations", "card_a_new_comments", "card_b_nudges", "card_b_calls", "card_b_inbounds",
             "required_nudges", "sent_nudges"]
    summary = " ".join(f"{k}={m.get(k)}" for k in order)
    if fails:
        print("FAIL " + summary)
        print("FAIL reasons: " + "; ".join(fails))
        return 1
    print("PASS " + summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
