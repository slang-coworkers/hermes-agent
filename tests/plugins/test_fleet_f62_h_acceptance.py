"""Acceptance test for FLEET-F62.h: a busy target gets its message re-driven automatically (variant U).

Ships as ``tests/plugins/test_fleet_f62_h_acceptance.py``, written against Hermes ``v0.22.2`` = ``d923a5eb`` (the
0.22.0 delivery line: the per-profile turn flock, the ``target_busy`` refusal, the live-owner mailbox). There is one
``test_ac_fleet_f62_h_<n>`` per active ``pytest:`` row of the ADR ``reports/fleet-f62.h.md`` §Acceptance criteria
(ids 1-14 and 17, same order). Id 15 is WITHDRAWN by user ruling A1 (no test def, id kept). Id 16 is a live
scenario. Id 17 is erratum E5 (user ruling, orch msg 120).

Loads ``plugins/nv-delivery-redrive`` through the real ``PluginManager().discover_and_load()`` from an isolated
``HERMES_HOME`` with an EMPTY bundled-plugin dir (shape of ``tests/hermes_cli/test_plugin_api_compat.py``). Every send
is a real ``message_agent`` tool call run by a real ``AIAgent``'s sequential executor, so core's whole authorization
(scope, ``pre_tool_call`` + ``modify``, pruned-args, guardrails), the plugin middleware and ``post_tool_call`` all
run. ``message_agent`` itself is core's: real ``terminal_tool(background=True, _host_local=True)`` -> real delivery
runner -> real per-profile flock -> a FAKE target ``hermes`` script that writes its inbound row with the real
``SessionDB`` (turn-start persistence) and replies on stdout. The runner process starts through a shim that runs
core's own ``_delivery_main`` and only shortens its live-wait bounds when a test asks for it. Drainer ticks run
through the real ``on_kanban_dispatch_tick`` hook. A gateway restart is a fresh process registry recovered from the
checkpoint plus a freshly loaded plugin manager.

Sender = the default profile (handle ``hermes``, home = the root), target = ``researcher``, orchestrator =
``orchestrator``. Behaviour contract only: no source reading, no network, nothing under ~/.hermes. POSIX only (core's
turn lock is fcntl-based).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from unittest.mock import patch

import pytest
import hermes_yaml as yaml

from tests.plugins.fleet_f62_h_rig import (  # fleet_fixture / live_owner_fixture register the fleet and live_owner fixtures
    _admissions,
    _alive,
    _append_user_row,
    _assistant_turn,
    _attempt_ids,
    _bot_chat,
    _busy,
    _completion_output,
    fleet_fixture,
    _has_query_child,
    install_runner_shim,
    _kill,
    live_owner_fixture,
    _make_sender_agent,
    _notices,
    ORCH,
    _pidlog_profiles,
    _queued_completion,
    _REPO,
    _restart_gateway,
    _seed_bot_chat,
    SENDER_SID,
    _spec_read,
    TARGET,
    _target_pids,
    _wait_completion,
    _wait_runner_done,
)


# --------------------------------------------------------------------------- #
# CLI path: re-drive, bound, late acceptance, unreadable transcript
# --------------------------------------------------------------------------- #
@pytest.mark.platforms("linux")
@pytest.mark.parametrize("wait", ["wait1", "wait0"])
def test_ac_fleet_f62_h_1(fleet, wait):
    """A local delivery whose target's turn lock is held past bot_mode.turn_wait_seconds and then released lands exactly once in the target's Bot Chat with no human action. Every attempt runs on the attempt-1 DM path, so _dm_delivery_id of each attempt equals the ack's delivery_id, and the ticket ends settled. Holds with a 1 s and a 0 s turn wait."""
    if wait == "wait0":
        fleet.settings(turn_wait_seconds=0)
    with _busy(fleet):
        ack = fleet.send("status? PAYLOAD_H1")
        did = ack.get("delivery_id")
        assert ack.get("status") == "queued" and did, ack
        assert ack.get("redrive", {}).get("tracked") is True, ack
        _wait_runner_done(fleet, ack["process_id"])
        fleet.tick(3, pause=0.5)
        assert fleet.rows(TARGET, "PAYLOAD_H1") == []
    assert fleet.until(lambda: fleet.ticket(did)["state"] == "settled", timeout=60.0)
    assert len(fleet.rows(TARGET, "PAYLOAD_H1")) == 1
    assert len(fleet.attempts(did)) >= 2
    assert _attempt_ids(fleet, did) == {did}
    # The busy completion only said "queued": the settled outcome arrives as exactly one final sender notice.
    assert fleet.until(lambda: len(_notices(fleet, "default", did)) == 1, timeout=15.0)
    assert "REPLY-FROM-researcher" in str(_notices(fleet, "default", did)[0]["content"])


@pytest.mark.platforms("linux")
def test_ac_fleet_f62_h_2(fleet):
    """A target that stays busy past redrive_window_seconds ends the ticket failed with reason queued_expired after at most max_attempts attempts. No row for that message lands afterwards, even once the target is free."""
    fleet.settings(redrive_window_seconds=12)
    with _busy(fleet):
        ack = fleet.send("status? PAYLOAD_H2")
        did = ack["delivery_id"]
        assert fleet.until(lambda: fleet.ticket(did)["state"] in ("failed", "ambiguous"), timeout=90.0)
        t = fleet.ticket(did)
        assert (t["state"], t["reason"]) == ("failed", "queued_expired"), t
        assert 1 <= len(fleet.attempts(did)) <= 6
    fleet.tick(4, pause=1.0)
    time.sleep(2.0)  # health: allow HX013 -- AC-2's negative check: the window in which no late row may land
    assert fleet.rows(TARGET, "PAYLOAD_H2") == []
    assert fleet.ticket(did)["state"] == "failed"
    assert fleet.until(lambda: len(_notices(fleet, "default", did)) == 1, timeout=15.0)
    assert "queued_expired" in str(_notices(fleet, "default", did)[0]["content"])


def _kill_before_row(fleet, body):
    """Send ``body`` and kill its runner and target CLI in the target's pre-row sleep: no row, no outcome."""
    import psutil

    # The re-drive that follows can still be running 40 s after this send under load; the bound must outlast it.
    fleet.settings(redrive_window_seconds=300)
    fleet.configure(TARGET, pre=5.0)
    started = _pidlog_profiles(fleet).count(TARGET)
    ack = fleet.send(body)
    runner = psutil.Process(fleet.registry.get(ack["process_id"]).pid)
    assert fleet.until(lambda: _spec_read(fleet, started), ticks=False, timeout=30.0)
    fleet.configure(TARGET, pre=0.1)
    _kill(runner)
    _wait_runner_done(fleet, ack["process_id"])
    return ack


def _append_target_row(fleet, content, *, message_uid=None):
    from hermes_state import SessionDB

    sid, _msgs = _bot_chat(fleet.homes[TARGET])
    db = SessionDB(db_path=fleet.homes[TARGET] / "state.db")
    try:
        return db.append_message(sid, role="user", content=content, message_uid=message_uid)
    finally:
        db.close()


def _land_live_row(fleet, delivery_id):
    """The live owner's turn persisted its user row, as a persisting owner does before it settles (E5, D5a)."""
    from tools import bot_live_delivery as mailbox

    record = mailbox.read_delivery_result(fleet.homes[TARGET], delivery_id)
    assert record is not None and record.get("message"), record
    return _append_target_row(fleet, record["message"])


def _shape_not_a_new_acceptance(fleet, shape):
    """A post-baseline compaction clone of an older identical message, or a prefix-only row, never settles an
    attempt; a continuation tip is followed."""
    from hermes_state import SessionDB

    if shape == "continuation_tip":
        home = fleet.homes[TARGET]
        _seed_bot_chat(home, "bc-r-root")
        db = SessionDB(db_path=home / "state.db")
        try:
            db.end_session("bc-r-root", "compression")
            db.create_session("bc-r-tip", source="cli", parent_session_id="bc-r-root")
        finally:
            db.close()
        ack = fleet.send("status? PAYLOAD_H3_TIP")
        assert fleet.until(lambda: fleet.ticket(ack["delivery_id"])["state"] == "settled", timeout=30.0)
        rows = fleet.rows(TARGET, "PAYLOAD_H3_TIP")
        assert len(rows) == 1 and rows[0]["session_id"] == "bc-r-tip", rows
        assert len(fleet.attempts(ack["delivery_id"])) == 1
        return
    body = "status? PAYLOAD_H3_CLONE" if shape == "compaction_clone_after_baseline" else "status? PAYLOAD_H3_PREFIX"
    older = None
    if shape == "compaction_clone_after_baseline":
        older = fleet.send(body)                          # an earlier identical message lands and settles
        assert fleet.until(lambda: fleet.ticket(older["delivery_id"])["state"] == "settled", timeout=30.0)
    else:
        _seed_bot_chat(fleet.homes[TARGET], "bc-r-prefix")  # the prefix row needs a Bot Chat to land in
    ack = _kill_before_row(fleet, body)
    did = ack["delivery_id"]
    if older is not None:
        original = fleet.rows(TARGET, "PAYLOAD_H3_CLONE")[0]
        _append_target_row(fleet, original["content"], message_uid=original["message_uid"])   # fresh id, same uid
    else:
        whole = fleet.ticket(did)["content"]
        _append_target_row(fleet, whole[: len(whole) // 2])                                  # prefix only
    assert fleet.until(lambda: fleet.ticket(did)["state"] == "settled", timeout=60.0)
    assert len(fleet.attempts(did)) == 2, fleet.attempts(did)                                # it was re-driven
    if older is not None:
        rows = fleet.rows(TARGET, "PAYLOAD_H3_CLONE")
        assert len({r["message_uid"] for r in rows}) == 2, rows
        assert fleet.ticket(did)["matched_row_id"] != fleet.ticket(older["delivery_id"])["matched_row_id"]
    else:
        full = [r for r in fleet.rows(TARGET, "PAYLOAD_H3_PREFIX") if fleet.ticket(did)["content"] in r["content"]]
        assert len(full) == 1, full


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("shape", ["alive_runner", "row_then_killed", "orphan_target",
                                   "killed_before_row_then_identical", "late_row_identical_exit0_unpersisted",
                                   "compaction_clone_after_baseline", "continuation_tip", "prefix_only"])
def test_ac_fleet_f62_h_3(fleet, shape):
    """A first attempt that is accepted late is never re-sent, and the message lands exactly once with the ticket settled. The drainer ticks while that attempt's writer is still alive and before its row exists, so the late acceptance and the re-drive decision really overlap (E1 F3). Three cases: the runner is alive at a tick; the row landed and the runner and target were killed; the runner was SIGKILLed while an orphaned target CLI persisted its row later. Two identical-content cases: (d) an attempt killed before its row, observed over before an identical later message lands, is re-driven once, and both tickets settle with two rows; (e) an attempt that persisted its row but lost its outcome, overlapping an identical later attempt that exited 0 without a row, re-sends neither: one row, and both tickets end ambiguous/unknown. Three probe-shape cases: a post-baseline compaction clone of an older identical message (fresh id, same message_uid) and a prefix-only row never count as the killed attempt's row, so it is re-driven once; and a row that lands in a compression continuation tip settles the ticket."""
    import psutil

    if shape in ("compaction_clone_after_baseline", "continuation_tip", "prefix_only"):
        _shape_not_a_new_acceptance(fleet, shape)
        return

    if shape == "killed_before_row_then_identical":
        # Under load the re-drive can still be running 40 s after the first send; the ticket's bound must outlast it.
        fleet.settings(redrive_window_seconds=300)
        fleet.configure(TARGET, pre=5.0)
        started = _pidlog_profiles(fleet).count(TARGET)
        first = fleet.send("status? PAYLOAD_H3")
        runner = psutil.Process(fleet.registry.get(first["process_id"]).pid)
        assert fleet.until(lambda: _spec_read(fleet, started), ticks=False, timeout=30.0)
        fleet.configure(TARGET, pre=0.1)
        _kill(runner)                                    # dies in the pre-row sleep: no row, no recorded outcome
        _wait_runner_done(fleet, first["process_id"])
        fleet.tick()                                     # observed over (its window closes) before the second send
        second = fleet.send("status? PAYLOAD_H3")       # identical content, own window
        _wait_runner_done(fleet, second["process_id"])
        assert fleet.until(lambda: all(fleet.ticket(a["delivery_id"])["state"] == "settled" for a in (first, second)),
                           timeout=60.0)
        t1, t2 = fleet.ticket(first["delivery_id"]), fleet.ticket(second["delivery_id"])
        assert len(fleet.rows(TARGET, "PAYLOAD_H3")) == 2 and t1["matched_row_id"] != t2["matched_row_id"], (t1, t2)
        assert len(fleet.attempts(first["delivery_id"])) == 2 and len(fleet.attempts(second["delivery_id"])) == 1
        return
    if shape == "late_row_identical_exit0_unpersisted":
        fleet.settings(redrive_window_seconds=12)
        fleet.configure(TARGET, pre=3.0)
        started = _pidlog_profiles(fleet).count(TARGET)
        first = fleet.send("status? PAYLOAD_H3")
        runner = psutil.Process(fleet.registry.get(first["process_id"]).pid)
        assert fleet.until(lambda: _spec_read(fleet, started), ticks=False, timeout=30.0)
        fleet.configure(TARGET, mode="exit0_unpersisted", pre=0.1)
        _kill(runner, tree=False)                        # orphaned target writes its row later; its outcome is lost
        _wait_runner_done(fleet, first["process_id"])
        second = fleet.send("status? PAYLOAD_H3")       # overlapping window; exits 0 without persisting
        _wait_runner_done(fleet, second["process_id"])
        assert fleet.until(lambda: len(fleet.rows(TARGET, "PAYLOAD_H3")) == 1, ticks=False, timeout=15.0)
        ids = (first["delivery_id"], second["delivery_id"])
        assert fleet.until(lambda: all(fleet.ticket(d)["state"] != "pending" for d in ids), timeout=60.0)
        for d in ids:
            assert (fleet.ticket(d)["state"], fleet.ticket(d)["reason"]) == ("ambiguous", "unknown"), fleet.ticket(d)
            assert len(fleet.attempts(d)) == 1
        fleet.tick(3, pause=0.5)
        assert len(fleet.rows(TARGET, "PAYLOAD_H3")) == 1
        return
    if shape == "alive_runner":
        fleet.configure(TARGET, pre=6.0)
    elif shape == "row_then_killed":
        fleet.configure(TARGET, pre=0.2, mode="hang_after_row")
    else:
        fleet.configure(TARGET, pre=5.0)
    ack = fleet.send("status? PAYLOAD_H3")
    did = ack["delivery_id"]
    runner = psutil.Process(fleet.registry.get(ack["process_id"]).pid)
    if shape == "alive_runner":
        fleet.tick(4, pause=0.5)                        # still in the target's pre-row sleep: alive, not yet seen
        assert _alive(runner.pid) and fleet.rows(TARGET, "PAYLOAD_H3") == []
        assert len(fleet.attempts(did)) == 1, fleet.attempts(did)
    elif shape == "row_then_killed":
        assert fleet.until(lambda: len(fleet.rows(TARGET, "PAYLOAD_H3")) == 1, ticks=False, timeout=20.0)
        _kill(runner)
        fleet.tick(2, pause=0.5)
    else:
        assert fleet.until(lambda: _has_query_child(runner),
                           ticks=False, timeout=10.0)
        _kill(runner, tree=False)                        # SIGKILL the runner only: the target CLI is orphaned
        fleet.tick(3, pause=0.5)                         # orphan still before its row: no re-drive may start
        assert any(_alive(p) for p in _target_pids(fleet)) and fleet.rows(TARGET, "PAYLOAD_H3") == []
        assert len(fleet.attempts(did)) == 1, fleet.attempts(did)
    assert fleet.until(lambda: fleet.ticket(did)["state"] == "settled", timeout=30.0)
    assert len(fleet.rows(TARGET, "PAYLOAD_H3")) == 1
    assert len(fleet.attempts(did)) == 1


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("case", ["unreadable", "lost_existing_db"])
def test_ac_fleet_f62_h_4(fleet, case):
    """A delivery whose target transcript cannot be read is never re-attempted, and at the bound it ends ambiguous with reason unknown. This covers a corrupt db, and a db that existed at dispatch and then disappeared."""
    fleet.settings(redrive_window_seconds=12)
    _seed_bot_chat(fleet.homes[TARGET], "bc-existing")    # the target db exists before dispatch
    with _busy(fleet):
        ack = fleet.send("status? PAYLOAD_H4")
        did = ack["delivery_id"]
        _wait_runner_done(fleet, ack["process_id"])
        db = fleet.homes[TARGET] / "state.db"
        if case == "unreadable":
            db.write_bytes(b"not a sqlite database" * 64)
        else:
            for leftover in fleet.homes[TARGET].glob("state.db*"):
                leftover.unlink()                          # an observed db that disappeared is unknown, not absent
        assert fleet.until(lambda: fleet.ticket(did)["state"] in ("failed", "ambiguous", "settled"), timeout=90.0)
    t = fleet.ticket(did)
    assert (t["state"], t["reason"]) == ("ambiguous", "unknown"), t
    assert len(fleet.attempts(did)) == 1


_ONESHOT_SENDER = textwrap.dedent('''\
    import json, sys
    sys.path.insert(0, {repo!r})
    sys.path.insert(0, {testdir!r})
    from pathlib import Path
    from hermes_cli import plugins as plugins_mod
    from tools import bot_mode_dm, bot_relay
    bot_relay._hermes_cli = lambda: {fake!r}
    bot_mode_dm._dm_dir = lambda: Path({dm_dir!r})
    import {module} as t
    t.install_runner_shim({shim!r})
    manager = plugins_mod.PluginManager(); manager.discover_and_load(); plugins_mod._plugin_manager = manager
    agent = t._make_sender_agent()
    messages = []
    agent._execute_tool_calls_sequential(t._assistant_turn("researcher", {body!r}, "call-oneshot"), messages, {task!r})
    print([m for m in messages if m.get("role") == "tool"][-1]["content"])
''')


def _oneshot_send(fleet, tmp_path, body, name="oneshot_sender.py"):
    """Run one message_agent send in a separate sender process that exits right after the ack."""
    script = tmp_path / name
    script.write_text(_ONESHOT_SENDER.format(
        repo=str(_REPO), testdir=str(Path(__file__).resolve().parent), module=Path(__file__).stem,
        fake=str(fleet.fake), dm_dir=str(fleet.dm_dir), shim=str(fleet.shim), body=body, task=fleet.task_id),
        encoding="utf-8")
    return subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", env={**os.environ, "HERMES_HOME": str(fleet.root)})


def _ack_of(proc):
    out, err = proc.communicate(timeout=180)
    assert proc.returncode == 0, err[-2000:]
    return json.loads(out.strip().splitlines()[-1])


@pytest.mark.platforms("linux")
def test_ac_fleet_f62_h_5(fleet, tmp_path):
    """A one-shot sender that exits while its delivery is pending on a busy target leaves the delivery owned by the gateway drainer. The drainer delivers it once the target is free, and the target's reply reaches the sender's Bot Chat as one delivery notice naming the delivery_id."""
    with _busy(fleet):
        ack = _ack_of(_oneshot_send(fleet, tmp_path, "status? PAYLOAD_H5"))
        did = ack["delivery_id"]
        assert ack.get("redrive", {}).get("tracked") is True, ack
        fleet.tick(3, pause=1.0)                          # the sender process is gone; the target is still busy
        assert fleet.rows(TARGET, "PAYLOAD_H5") == []
        assert fleet.ticket(did)["state"] == "pending"
    assert fleet.until(lambda: fleet.ticket(did)["state"] == "settled", timeout=60.0)
    assert len(fleet.rows(TARGET, "PAYLOAD_H5")) == 1
    assert fleet.until(lambda: len(_notices(fleet, "default", did)) == 1, timeout=15.0)
    note = str(_notices(fleet, "default", did)[0]["content"])
    assert did in note and ("REPLY-FROM-researcher" in note or "ACK researcher" in note), note


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("case", ["both_refused", "identical_overtake", "orphan_overtake"])
def test_ac_fleet_f62_h_6(fleet, case):
    """Two messages from one sender to one target land once each under their own delivery_ids. When both first attempts were refused busy, they land in send order. When the second, identical message overtakes a busy-refused first one, both still land (two rows, two settled tickets). When the first attempt's runner dies while its target CLI is orphaned and a second message lands before the orphan's row, each message still lands exactly once. Under Option Z the later row comes first in the last two cases: the D4 (i) and (iii) exceptions (user question (a) for (iii))."""
    if case == "orphan_overtake":
        import psutil
        from hermes_cli import middleware

        strict = "tool_dispatch" in middleware.VALID_MIDDLEWARE          # Option S holds the second send
        # The orphan is held for as long as the second message takes under load; its ticket's bound must outlast that.
        fleet.settings(redrive_window_seconds=600)
        hold = fleet.state_dir / "orphan.hold"
        hold.write_text("", encoding="utf-8")
        fleet.configure(TARGET, pre=0.1, hold=str(hold))
        try:
            first = fleet.send("status? PAYLOAD_H6_FIRST")
            runner = psutil.Process(fleet.registry.get(first["process_id"]).pid)
            assert fleet.until(lambda: TARGET in _pidlog_profiles(fleet) and Path(f"{hold}.in").exists(),
                               ticks=False, timeout=60.0)
            fleet.configure(TARGET, pre=0.1)             # the orphan read its spec with the hold; later CLIs never wait
            _kill(runner, tree=False)                    # releases the runner-held flock; the target CLI lives on
            _wait_runner_done(fleet, first["process_id"])
            if strict:
                hold.unlink()                            # Option S holds the second send until the first resolves
            second = fleet.send("status? PAYLOAD_H6_SECOND")
            _wait_runner_done(fleet, second["process_id"])
            if not strict:                               # the second row lands while the orphan is still held
                assert fleet.until(lambda: len(fleet.rows(TARGET, "PAYLOAD_H6_SECOND")) == 1, ticks=False,
                                   timeout=60.0)
        finally:
            hold.unlink(missing_ok=True)
        ids = (first["delivery_id"], second["delivery_id"])
        assert fleet.until(lambda: all(fleet.ticket(d)["state"] == "settled" for d in ids), timeout=60.0)
        one, two = fleet.rows(TARGET, "PAYLOAD_H6_FIRST"), fleet.rows(TARGET, "PAYLOAD_H6_SECOND")
        assert len(one) == 1 and len(two) == 1, (one, two)
        assert all(len(fleet.attempts(d)) == 1 for d in ids)
        assert (one[0]["id"] < two[0]["id"]) is strict, (one, two)
        return
    body_one = "status? PAYLOAD_H6_FIRST"
    body_two = body_one if case == "identical_overtake" else "status? PAYLOAD_H6_SECOND"
    with _busy(fleet):
        first = fleet.send(body_one)
        _wait_runner_done(fleet, first["process_id"])
        if case == "both_refused":
            second = fleet.send(body_two)
            _wait_runner_done(fleet, second["process_id"])
            fleet.tick(2, pause=0.5)
    if case == "identical_overtake":
        second = fleet.send(body_two)                    # target free, drainer not ticked yet: attempt 1 lands now
        _wait_runner_done(fleet, second["process_id"])
        assert len(fleet.rows(TARGET, "PAYLOAD_H6_FIRST")) == 1
    assert first["delivery_id"] != second["delivery_id"], (first, second)
    assert fleet.until(lambda: all(fleet.ticket(a["delivery_id"])["state"] == "settled" for a in (first, second)),
                       timeout=60.0)
    if case == "identical_overtake":
        assert len(fleet.rows(TARGET, "PAYLOAD_H6_FIRST")) == 2
        return
    one, two = fleet.rows(TARGET, "PAYLOAD_H6_FIRST"), fleet.rows(TARGET, "PAYLOAD_H6_SECOND")
    assert len(one) == 1 and len(two) == 1, (one, two)
    assert one[0]["id"] < two[0]["id"]


_GATE_YAML = ("name: f62h-test-gate\nversion: 0.0.1\ndescription: test gate\nmanifest_version: 1\n"
              "provides_hooks: [pre_tool_call]\nprovides_tools: []\n")
_GATE_INIT = textwrap.dedent('''\
    def register(ctx):
        def _gate(tool_name=None, args=None, **kwargs):
            msg = str((args or {}).get("message"))
            if tool_name == "message_agent" and "FORBIDDEN_H7" in msg:
                return {"action": "block", "message": "f62h-test-gate: blocked"}
            if tool_name == "message_agent" and "MODIFY_ME_H7" in msg:
                return {"action": "modify", "args": {"message": "status? MODIFIED_H7"}}
            return None
        ctx.register_hook("pre_tool_call", _gate)
''')


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("case", ["hook_block", "guardrail_block", "hook_modify"])
def test_ac_fleet_f62_h_7(fleet, case, monkeypatch):
    """Every message_agent call, including one sent while an earlier ticket for the pair is still open, goes through core's complete authorization on the real executor. A pre_tool_call block and a guardrail block each yield no ticket and no row. A pre_tool_call modify of the message is what gets ticketed and delivered."""
    from agent.tool_guardrails import ToolGuardrailDecision

    gate = fleet.root / "plugins" / "f62h-test-gate"
    gate.mkdir(parents=True)
    (gate / "plugin.yaml").write_text(_GATE_YAML, encoding="utf-8")
    (gate / "__init__.py").write_text(_GATE_INIT, encoding="utf-8")
    cfg = yaml.safe_load((fleet.root / "config.yaml").read_text(encoding="utf-8-sig"))
    cfg["plugins"]["enabled"].append("f62h-test-gate")
    (fleet.root / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    fleet.load()
    fleet.tick()
    with _busy(fleet):
        first = fleet.send("status? PAYLOAD_H7_FIRST")   # an open ticket for the pair
        assert fleet.ticket(first["delivery_id"])["state"] == "pending"
        if case == "guardrail_block":
            real = fleet.agent._tool_guardrails.before_call
            monkeypatch.setattr(fleet.agent._tool_guardrails, "before_call", lambda name, args: (
                ToolGuardrailDecision(action="block", code="f62h_test", message="f62h guardrail: blocked",
                                      tool_name=name) if "GUARDED_H7" in str((args or {}).get("message"))
                else real(name, args)))
        body = {"hook_block": "status? FORBIDDEN_H7", "guardrail_block": "status? GUARDED_H7",
                "hook_modify": "status? MODIFY_ME_H7"}[case]
        out = fleet.send(body)
        if case != "hook_modify":
            assert "delivery_id" not in out and "blocked" in json.dumps(out), out
        else:
            assert out.get("delivery_id") and out.get("redrive", {}).get("tracked") is True, out
            _wait_runner_done(fleet, out["process_id"])
        _wait_runner_done(fleet, first["process_id"])
        fleet.tick(2, pause=0.5)
    assert fleet.until(lambda: fleet.ticket(first["delivery_id"])["state"] == "settled", timeout=60.0)
    if case == "hook_modify":
        assert fleet.until(lambda: fleet.ticket(out["delivery_id"])["state"] == "settled", timeout=60.0)
        assert len(fleet.rows(TARGET, "MODIFIED_H7")) == 1
        assert fleet.rows(TARGET, "MODIFY_ME_H7") == []
    else:
        fleet.tick(3, pause=0.5)
        assert fleet.rows(TARGET, "FORBIDDEN_H7") == [] and fleet.rows(TARGET, "GUARDED_H7") == []
    assert len(fleet.rows(TARGET, "PAYLOAD_H7_FIRST")) == 1


# --------------------------------------------------------------------------- #
# Live-owner path
# --------------------------------------------------------------------------- #


_H8_WINDOWS = {"claimed_owner_gone": 12, "owner_gone_expired": 8}


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("case", ["settled_later", "failed_later", "claimed_owner_gone", "no_runner",
                                  "core_cancelled", "owner_gone_expired", "pending_restart",
                                  "runner_died_after_admission"])
def test_ac_fleet_f62_h_8(fleet, live_owner, case, monkeypatch):
    """(R4 condition 3, ruling B1) A live-owner delivery whose core runner ended (or never started) with the receipt still queued or claimed is followed by the plugin past the runner, and its final outcome is reported to the sender and the orchestrator. The plugin NEVER re-sends or re-admits it: no CLI re-drive, no new mailbox admission, no target row from the plugin. Eight cases: (a) the owner settles later: settled only once the target's Bot Chat holds the full role='user' row newer than the ticket's baseline and claimed by this ticket (D5a, E5); otherwise ambiguous/unknown. The reply reaches the sender; (b) it fails later: failed with the receipt's typed reason; (c) it is claimed and the owner goes away: never re-sent, ambiguous/unknown at the bound; (d) an ack with notification_error and no runner is tracked from its receipt; (e) core's own runner cancels the unclaimed receipt (runtime_offline): reported cancelled with that reason, never re-sent; (f) still queued with the owner gone at the bound: the plugin cancels it under the mailbox lock and reports failed/queued_expired, with no row; (g) core's pending completion ("Delivery remains pending …") reached the sender, the gateway restarts, then the receipt settles: exactly one plugin sender notice with the reply (E2 X2); (h) the runner admitted the message and died before printing its outcome: the drainer finds the receipt first and only follows it."""
    from tools import bot_live_delivery as mailbox
    from tools.bot_failure_reasons import RUNTIME_OFFLINE
    from tools.process_registry_notifications import format_process_notification
    import tools.terminal_tool as terminal_module

    home = fleet.homes[TARGET]
    if case in _H8_WINDOWS:
        fleet.settings(redrive_window_seconds=_H8_WINDOWS[case])
    if case == "no_runner":
        monkeypatch.setattr(terminal_module, "terminal_tool",
                            lambda *a, **k: json.dumps({"error": "spawn refused (test)"}))
    elif case in ("pending_restart", "core_cancelled"):
        # Core's runner gives up only after 30 min, and re-checks the owner once per 300 s window; both shortened
        # inside the runner process (read by the shim) so core's own outcome arrives within the test.
        bounds = {"_LIVE_WAIT_SECONDS": 0.2, "_LIVE_WAIT_MAX_SECONDS": 0.5 if case == "pending_restart" else 20.0}
        (fleet.state_dir / "runner.json").write_text(json.dumps(bounds), encoding="utf-8")
    elif case == "runner_died_after_admission":
        # Send with no live owner (so _start_delivery takes the CLI path) and the runner held before core runs; the
        # owner appears afterwards, so only the runner itself can admit the message (tools/bot_mode_dm.py:617-627).
        live_owner.release()
        (fleet.state_dir / "runner.gate").write_text("", encoding="utf-8")
    ack = fleet.send("status? PAYLOAD_H8")
    did = ack["delivery_id"]
    assert ack.get("redrive", {}).get("tracked") is True, ack
    if case == "runner_died_after_admission":
        from hermes_cli.active_sessions import try_acquire_active_session

        assert mailbox.read_delivery_result(home, did) is None and fleet.ticket(did)["path"] == "cli", ack
        back, refusal = try_acquire_active_session(
            session_id="bc-live", surface="desktop", config={}, registry_home=home,
            metadata={"live_session_id": "live-h8-back", "bot_live_delivery_consumer": True})
        assert refusal is None and back is not None
        monkeypatch.setattr(live_owner, "release", back.release)
        (fleet.state_dir / "runner.gate").unlink()
        assert fleet.until(lambda: mailbox.read_delivery_result(home, did) is not None, ticks=False, timeout=30.0)
    assert mailbox.read_delivery_result(home, did)["status"] == "queued", ack

    def owner_claims():
        claim = mailbox.claim_pending_delivery(home, mailbox.find_canonical_live_owner(home))
        assert claim is not None and claim["delivery_id"] == did, claim

    if case == "no_runner":
        assert "process_id" not in ack and ack.get("notification_error"), ack
    elif case == "pending_restart":
        _wait_runner_done(fleet, ack["process_id"])
        _wait_completion(fleet, ack["process_id"])
        assert "Delivery remains pending" in _completion_output(fleet, ack["process_id"])
        pending = _queued_completion(fleet, ack["process_id"])
    elif case == "core_cancelled":
        live_owner.release()                             # the owner vanishes while core's runner still waits
        _wait_runner_done(fleet, ack["process_id"], timeout=30.0)
        # exited is set before the completion is queued (tools/process_registry.py:641, :1610-1616)
        assert fleet.until(fleet.registry.get(ack["process_id"])._completion_event.is_set, ticks=False, timeout=30.0)
        out = json.loads(_completion_output(fleet, ack["process_id"]))
        assert (out.get("status"), out.get("reason")) == ("cancelled", RUNTIME_OFFLINE), out
        receipt = mailbox.read_delivery_result(home, did)
        assert (receipt["status"], receipt["reason"]) == ("cancelled", RUNTIME_OFFLINE), receipt
    else:
        if case == "claimed_owner_gone":
            owner_claims()
        # The runner ends without printing an outcome: a stand-in for its 30-min bound, or for a crash right after
        # it admitted the message (case h), which no completion text can tell the drainer about.
        fleet.registry.kill_process(ack["process_id"], consume_output=True)
        _wait_runner_done(fleet, ack["process_id"])
    if case != "core_cancelled":
        fleet.tick(2, pause=0.5)
        assert fleet.ticket(did)["state"] == "pending"

    def finish_owner_result():
        owner_claims()
        if case == "failed_later":
            mailbox.complete_delivery(home, did, status="failed", reason="provider_rate_limit", error="429")
        else:
            _land_live_row(fleet, did)
            mailbox.complete_delivery(home, did, status="settled", reply="LIVE-REPLY-H8")
        final = "failed" if case == "failed_later" else "settled"
        assert fleet.until(lambda: fleet.ticket(did)["state"] == final), fleet.ticket(did)
        if final == "failed":
            assert fleet.ticket(did)["reason"] == "provider_rate_limit", fleet.ticket(did)
        assert fleet.until(lambda: len(_notices(fleet, "default", did)) == 1 and len(_notices(fleet, ORCH, did)) == 1)
        if final == "settled":
            assert "LIVE-REPLY-H8" in str(_notices(fleet, "default", did)[0]["content"])
    def finish_core_cancelled():
        assert fleet.until(lambda: fleet.ticket(did)["state"] == "cancelled"), fleet.ticket(did)
        assert fleet.ticket(did)["reason"] == RUNTIME_OFFLINE, fleet.ticket(did)
        assert fleet.until(lambda: len(_notices(fleet, "default", did)) == 1 and len(_notices(fleet, ORCH, did)) == 1)
    def finish_pending_restart():
        # Before it went down, the gateway injected core's pending completion into the sender's Bot Chat.
        _append_user_row(fleet.root, SENDER_SID,
                         format_process_notification({"type": "completion", "session_id": ack["process_id"],
                                                      **pending}),
                         display_kind="process_complete", display_metadata={"display_text": "x"})
        _restart_gateway(fleet, monkeypatch)
        owner_claims()
        _land_live_row(fleet, did)
        mailbox.complete_delivery(home, did, status="settled", reply="LATE-REPLY-H8")
        assert fleet.until(lambda: fleet.ticket(did)["state"] == "settled"), fleet.ticket(did)
        assert fleet.until(lambda: len(_notices(fleet, "default", did)) == 1)
        assert "LATE-REPLY-H8" in str(_notices(fleet, "default", did)[0]["content"])
    def finish_runner_died():
        fleet.tick(4, pause=0.5)
        assert fleet.ticket(did)["state"] == "pending" and len(fleet.attempts(did)) == 1, fleet.attempts(did)
        owner_claims()
        _land_live_row(fleet, did)
        mailbox.complete_delivery(home, did, status="settled", reply="LIVE-REPLY-H8")
        assert fleet.until(lambda: fleet.ticket(did)["state"] == "settled"), fleet.ticket(did)
    def finish_owner_gone():
        live_owner.release()
        assert fleet.until(lambda: fleet.ticket(did)["state"] in ("ambiguous", "failed", "settled", "cancelled"),
                           timeout=60.0)
        receipt = mailbox.read_delivery_result(home, did)
        if case == "claimed_owner_gone":
            assert (fleet.ticket(did)["state"], fleet.ticket(did)["reason"]) == ("ambiguous", "unknown")
            assert receipt["status"] == "claimed", receipt
        else:
            assert (fleet.ticket(did)["state"], fleet.ticket(did)["reason"]) == ("failed", "queued_expired")
            assert (receipt["status"], receipt["reason"]) == ("cancelled", "queued_expired"), receipt
    finish = {"settled_later": finish_owner_result, "no_runner": finish_owner_result,
              "failed_later": finish_owner_result, "core_cancelled": finish_core_cancelled,
              "pending_restart": finish_pending_restart, "runner_died_after_admission": finish_runner_died}
    finish.get(case, finish_owner_gone)()
    assert fleet.until(lambda: len(_notices(fleet, "default", did)) == 1 and len(_notices(fleet, ORCH, did)) == 1,
                       timeout=30.0)
    fleet.tick(3, pause=0.5)
    assert len(_notices(fleet, "default", did)) == 1 and len(_notices(fleet, ORCH, did)) == 1
    landed = case in ("settled_later", "no_runner", "pending_restart", "runner_died_after_admission")
    rows = fleet.rows(TARGET, "PAYLOAD_H8")
    assert len(rows) == (1 if landed else 0), rows      # the owner's own row (E5); never one from the plugin
    if landed:
        assert fleet.ticket(did)["matched_row_id"] is not None, fleet.ticket(did)
    assert [r["delivery_id"] for r in _admissions(home, "PAYLOAD_H8")] == [did]
    assert len(fleet.attempts(did)) == 1, fleet.attempts(did)


# --------------------------------------------------------------------------- #
# Notices, the busy completion, restart, the idle regression
# --------------------------------------------------------------------------- #
@pytest.mark.platforms("linux")
@pytest.mark.parametrize("case", ["normal", "busy_orchestrator", "orch_no_bot_chat", "restart_after_append"])
def test_ac_fleet_f62_h_9(fleet, case, monkeypatch):
    """A typed final failure reaches the sender's originating session (its Bot Chat tip) and the orchestrator profile's Bot Chat as exactly one process_complete notice row each. Each row names the delivery_id, the target and the closed-vocabulary reason. This holds when the orchestrator is busy, when it has no Bot Chat yet, and when the drainer restarts between the append and its bookkeeping."""
    fleet.settings(redrive_window_seconds=12)
    if case == "orch_no_bot_chat":
        for leftover in fleet.homes[ORCH].glob("state.db*"):
            leftover.unlink()
    orch_lock = _busy(fleet, ORCH) if case == "busy_orchestrator" else None
    if orch_lock is not None:
        orch_lock.__enter__()
    try:
        with _busy(fleet):
            ack = fleet.send("status? PAYLOAD_H9")
            did = ack["delivery_id"]
            if case == "restart_after_append":
                store = fleet.loaded.module.store
                real_mark = store.mark_notice_appended
                monkeypatch.setattr(store, "mark_notice_appended",
                                    lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash before mark")))
            assert fleet.until(lambda: fleet.ticket(did)["state"] == "failed", timeout=90.0)
            fleet.tick(2, pause=0.5)
            if case == "restart_after_append":
                monkeypatch.setattr(store, "mark_notice_appended", real_mark)
                fleet.load()
                fleet.tick(3, pause=0.5)
    finally:
        if orch_lock is not None:
            orch_lock.__exit__(None, None, None)
    assert fleet.until(lambda: len(_notices(fleet, "default", did)) >= 1 and len(_notices(fleet, ORCH, did)) >= 1,
                       timeout=30.0)
    fleet.tick(3, pause=0.5)
    for who in ("default", ORCH):
        notes = _notices(fleet, who, did)
        assert len(notes) == 1, (who, notes)
        text = str(notes[0]["content"])
        assert did in text and TARGET in text and "queued_expired" in text, text


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("drainer", ["live", "stale", "untracked"])
@pytest.mark.parametrize("kind", ["flock_busy", "not_owned"])
def test_ac_fleet_f62_h_10(fleet, kind, drainer):
    """(derived) While tracking is live, the sender's target_busy runner completion, from a held turn lock (TurnBusyError: "retry shortly") or from a Bot Chat open on another surface (SESSION_NOT_OWNED: "Try again later"), is rewritten to status: queued under the same delivery_id. It keeps reason: target_busy, says the message is queued for automatic re-delivery with "do NOT resend", and contains none of "retry shortly", "Try again later" or "safe to resend" (E1 F4). With a stale drainer heartbeat, or with tracking off (no valid orchestrator_profile), the completion output is byte-for-byte the runner's own output (E2 M2)."""
    from contextlib import nullcontext

    from agent.redact import redact_terminal_output
    from tools.process_registry import _completion_output as core_tail

    if drainer == "stale":
        fleet.loaded.module.store.set_heartbeat(time.time() - 3600)
    elif drainer == "untracked":
        fleet.settings(orchestrator_profile=None)
    if kind == "not_owned":
        fleet.configure(TARGET, mode="not_owned")
    with _busy(fleet) if kind == "flock_busy" else nullcontext():
        ack = fleet.send("status? PAYLOAD_H10")
        _wait_runner_done(fleet, ack["process_id"])
        _wait_completion(fleet, ack["process_id"])
        out = _completion_output(fleet, ack["process_id"])
    if drainer == "live":
        assert ack["delivery_id"] in out and "automatic re-delivery" in out and "do NOT resend" in out, out
        payload = json.loads(out)
        assert (payload.get("status"), payload.get("reason"), payload.get("delivery_id")) == (
            "queued", "target_busy", ack["delivery_id"]), payload
        for stale_advice in ("retry shortly", "Try again later", "safe to resend"):
            assert stale_advice not in out, (stale_advice, out)
        return
    session = fleet.registry.get(ack["process_id"])
    assert out == redact_terminal_output(core_tail(session)["output"], session.command), out
    assert "target_busy" in out and "redrive" not in ack, (out, ack)
    assert ("retry shortly" if kind == "flock_busy" else "Try again later") in out, out


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("case", ["restart", "crash_after_launching", "crash_after_spawn"])
def test_ac_fleet_f62_h_11(fleet, case, monkeypatch):
    """A delivery that is mid re-drive survives a gateway restart (registry kill_all() plus a freshly loaded plugin manager) and then lands exactly once and ends settled. This also holds when the drainer crashed after committing an attempt launching, both before the spawn and after the spawn but before the running commit."""
    with _busy(fleet):
        ack = fleet.send("status? PAYLOAD_H11")
        did = ack["delivery_id"]
        _wait_runner_done(fleet, ack["process_id"])
        if case == "restart":
            fleet.tick(2, pause=1.5)
        else:
            drain, store = fleet.loaded.module.drain, fleet.loaded.module.store
            with monkeypatch.context() as crash:
                if case == "crash_after_launching":
                    crash.setattr(drain, "spawn_attempt",
                                  lambda *a, **k: (_ for _ in ()).throw(SystemExit("crash before spawn")))
                else:
                    crash.setattr(store, "commit_running",
                                  lambda *a, **k: (_ for _ in ()).throw(SystemExit("crash after spawn")))
                # invoke_hook swallows the callback's SystemExit (hermes_cli/plugins_dispatch.py:241): the crash
                # leaves the committed attempt behind exactly as a dead drainer would.
                assert fleet.until(lambda: bool(fleet.attempts(did)) and len(fleet.attempts(did)) >= 2
                                   and fleet.attempts(did)[-1]["state"] == "launching", timeout=8.0)
        fleet.registry.kill_all(fleet.task_id, source="gateway_shutdown")   # every registry spawn here is the test's
        fleet.load()
        assert fleet.ticket(did)["state"] == "pending"
    assert fleet.until(lambda: fleet.ticket(did)["state"] == "settled", timeout=60.0)
    assert len(fleet.rows(TARGET, "PAYLOAD_H11")) == 1
    assert _attempt_ids(fleet, did) == {did}


_H12_TARGET_SPECS = {"detached_pruned": {"pre": 4.0}}     # the runner must still be alive when the gateway crashes


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("case", ["idle", "settle_during_runner", "transform_missed", "publish_lost_after_persist",
                                  "detached_pruned"])
def test_ac_fleet_f62_h_12(fleet, case, monkeypatch, request):
    """(regression for idle, settle_during_runner and transform_missed; publish_lost_after_persist and detached_pruned need the plugin's (b′) notice) A delivery to an idle target behaves as on 0.22: the ack keeps core's status, delivery_id, process_id and reply_delivery values, the reply completion text is unchanged, the message lands once, and no notice is written. One sender notice, never two (E1 F5): no plugin notice when a live receipt settles while core's runner is completing and a drainer tick lands between core's exited mark and its output transform, or when the plugin's transform records nothing. When core's publication fails after its result write, no plugin notice is written in-process (the documented residual), and exactly one after a gateway restart. When a crash leaves the attempt-1 runner to be recovered as a detached session that core later prunes (_completion_event set, no completion queued), exactly one plugin sender notice is written on that restarted gateway."""
    import re
    import threading

    from tools import bot_live_delivery as mailbox
    from tools import process_registry as pr_mod

    home = fleet.homes[TARGET]
    real_redact = pr_mod._redact_process_result
    if case == "settle_during_runner":
        request.getfixturevalue("live_owner")
    elif case == "transform_missed":
        # The plugin's transform records nothing for this runner (timed out / skipped); core still publishes the
        # completion, and the one-notice rule keys on _completion_event, not on the transform.
        fleet.settings(heartbeat_stale_seconds=2)
        monkeypatch.setattr(fleet.loaded.module.store, "record_attempt_outcome", lambda *a, **k: None)
    elif case == "publish_lost_after_persist":
        # Core persists the runner's result, then its publication raises before completion_queue.put. Patched before
        # the send because an idle target's runner can finish before the send returns.
        def lost(result):
            if (isinstance(result, dict) and result.get("type") == "completion"
                    and "--run-delivery" in str(result.get("command") or "")):
                raise RuntimeError("gateway died before publishing")
            return real_redact(result)
        monkeypatch.setattr(pr_mod, "_redact_process_result", lost)
    if case in _H12_TARGET_SPECS:
        fleet.configure(TARGET, **_H12_TARGET_SPECS[case])
    ack = fleet.send("status? PAYLOAD_H12")
    did, pid = ack["delivery_id"], ack["process_id"]
    assert ack.get("status") == "queued" and ack.get("reply_delivery") == "notification", ack
    assert re.fullmatch(r"[0-9a-f]{64}", did or ""), ack
    session = fleet.registry.get(pid)
    assert session is not None and "--run-delivery" in session.command
    if case == "settle_during_runner":
        # Core marks the session exited before it publishes the completion and runs the output transform
        # (tools/process_registry.py:1586-1639). Pause the completion inside that window and tick there.
        in_window, release = threading.Event(), threading.Event()

        def paused(result):
            if isinstance(result, dict) and result.get("session_id") == pid:
                in_window.set()
                release.wait(timeout=30)
            return real_redact(result)
        monkeypatch.setattr(pr_mod, "_redact_process_result", paused)
        try:
            claim = mailbox.claim_pending_delivery(home, mailbox.find_canonical_live_owner(home))
            assert claim is not None and claim["delivery_id"] == did, claim
            _land_live_row(fleet, did)
            mailbox.complete_delivery(home, did, status="settled", reply="REPLY-FROM-researcher")
            assert in_window.wait(timeout=40), "the runner's completion never reached the transform"
            assert session.exited
            fleet.tick(3, pause=0.3)                     # exited + settled receipt, transform not yet run
            assert _notices(fleet, "default", did) == [], "exited alone must not authorize a sender notice"
        finally:
            release.set()
        assert fleet.until(lambda: session._completion_event.is_set(), ticks=False, timeout=30.0)
        assert _notices(fleet, "default", did) == []
        assert len(_admissions(home, "PAYLOAD_H12")) == 1
    elif case == "publish_lost_after_persist":
        _wait_runner_done(fleet, pid, timeout=40.0)
        assert fleet.until(lambda: len(fleet.rows(TARGET, "PAYLOAD_H12")) == 1, ticks=False, timeout=10.0)
        assert fleet.until(session._completion_event.is_set, ticks=False, timeout=10.0)   # set even when publish raised
        assert (fleet.root / "logs" / "process-results" / f"{pid}.json").exists()
        fleet.tick(3, pause=0.5)
        assert _notices(fleet, "default", did) == []     # the documented in-process residual
        while not fleet.registry.completion_queue.empty():
            fleet.registry.completion_queue.get_nowait()
        _restart_gateway(fleet, monkeypatch)
        assert fleet.until(lambda: len(_notices(fleet, "default", did)) == 1, timeout=30.0)
        fleet.tick(3, pause=0.5)
        notes = _notices(fleet, "default", did)
        assert len(notes) == 1 and "REPLY-FROM-researcher" in str(notes[0]["content"]), notes
        assert len(fleet.rows(TARGET, "PAYLOAD_H12")) == 1
        return
    elif case == "detached_pruned":
        # The next gateway process recovers the live runner from the checkpoint as a detached session with no pipe.
        # Once its PID is gone core prunes it, sets _completion_event and queues no completion
        # (tools/process_registry.py:1057-1072): only the plugin can tell the sender.
        assert _restart_gateway(fleet, monkeypatch, crash=True) == 1
        recovered = fleet.registry._running.get(pid)
        assert recovered is not None and recovered.detached and not recovered._completion_event.is_set()
        assert fleet.until(lambda: fleet.registry.get(pid) is None, ticks=False, timeout=40.0)
        assert recovered._completion_event.is_set()
        assert fleet.registry.completion_queue.empty()
        assert fleet.until(lambda: len(_notices(fleet, "default", did)) == 1, timeout=30.0)
        fleet.tick(3, pause=0.5)
        notes = _notices(fleet, "default", did)
        assert len(notes) == 1 and did in str(notes[0]["content"]), notes
        assert len(fleet.rows(TARGET, "PAYLOAD_H12")) == 1
        return
    else:
        _wait_runner_done(fleet, pid, timeout=40.0)
        _wait_completion(fleet, pid)
        if case == "transform_missed":
            assert (fleet.root / "logs" / "process-results" / f"{pid}.json").exists()
            fleet.tick(4, pause=1.0)                     # well past heartbeat_stale_seconds
        assert len(fleet.rows(TARGET, "PAYLOAD_H12")) == 1
    assert "REPLY-FROM-researcher" in _completion_output(fleet, pid)
    fleet.tick(3, pause=0.5)
    assert _notices(fleet, "default", did) == []
    assert _notices(fleet, ORCH, did) == []


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("case", ["cli_hung_writer", "live_claimed"])
def test_ac_fleet_f62_h_13(fleet, case, request):
    """(derived) At the bound, a ticket whose attempt may still land is never reported queued_expired. A still-running CLI writer, or a live receipt still claimed, ends ambiguous/unknown with its notices, and the drainer starts no re-drive for a later ticket of the pair until that attempt resolves. The ticket survives a retention interval while that attempt is open, and when it resolves, one late-outcome notice follows."""
    from tools import bot_live_delivery as mailbox

    fleet.settings(redrive_window_seconds=10, retention_seconds=1)
    home = fleet.homes[TARGET]
    if case == "cli_hung_writer":
        fleet.configure(TARGET, pre=15.0)               # attempt 1 is a live writer across the whole bound
        first = fleet.send("status? PAYLOAD_H13_FIRST")
        assert fleet.until(lambda: fleet.pidlog.exists(), ticks=False, timeout=10.0)
        fleet.configure(TARGET, pre=0.1)                # the fake reads its spec at start: later turns are quick
    else:
        lease = request.getfixturevalue("live_owner")
        first = fleet.send("status? PAYLOAD_H13_FIRST")
        assert mailbox.claim_pending_delivery(home, mailbox.find_canonical_live_owner(home)) is not None
    did = first["delivery_id"]
    assert fleet.until(lambda: fleet.ticket(did)["state"] != "pending", timeout=40.0)
    t = fleet.ticket(did)
    assert (t["state"], t["reason"]) == ("ambiguous", "unknown"), t
    assert fleet.until(lambda: len(_notices(fleet, "default", did)) == 1 and len(_notices(fleet, ORCH, did)) == 1)
    time.sleep(2.0)  # health: allow HX013 -- AC-13 waits out retention_seconds=1 before the GC tick that must keep the ticket
    fleet.tick(2, pause=0.5)                             # past retention: an open late attempt keeps the ticket
    assert fleet.ticket(did) is not None and fleet.ticket(did)["late_open"] == 1
    # The rest asserts post-resolution rows, not GC; the second ticket's own window outlasts the first's late writer.
    fleet.settings(retention_seconds=3600, redrive_window_seconds=90)
    if case == "live_claimed":
        lease.release()
    second = fleet.send("status? PAYLOAD_H13_SECOND")
    fleet.tick(3, pause=0.5)
    if case == "cli_hung_writer":
        assert len(fleet.attempts(second["delivery_id"])) == 1   # core's own first attempt only: no re-drive yet
        assert fleet.until(lambda: len(fleet.rows(TARGET, "PAYLOAD_H13_FIRST")) == 1, timeout=30.0)
    else:
        _land_live_row(fleet, did)
        mailbox.complete_delivery(home, did, status="settled", reply="LATE-H13")
    assert fleet.until(lambda: len(_notices(fleet, "default", did)) == 2, timeout=30.0)   # one late-outcome notice
    assert fleet.ticket(did)["late_open"] == 0
    assert fleet.until(lambda: fleet.ticket(second["delivery_id"])["state"] == "settled", timeout=60.0)
    if case == "cli_hung_writer":
        one, two = fleet.rows(TARGET, "PAYLOAD_H13_FIRST"), fleet.rows(TARGET, "PAYLOAD_H13_SECOND")
        assert len(one) == 1 and len(two) == 1 and one[0]["id"] < two[0]["id"], (one, two)


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("case", ["unset", "unknown_profile"])
def test_ac_fleet_f62_h_14(fleet, case, monkeypatch):
    """(derived) Without a valid orchestrator_profile the plugin tracks nothing: the message_agent tool result is byte-for-byte core's (no redrive key), the runner's target_busy completion is core's own text, and nothing is re-driven."""
    raw = []
    real_start = fleet.bot_mode_dm._start_delivery
    monkeypatch.setattr(fleet.bot_mode_dm, "_start_delivery",
                        lambda *a, **k: raw.append(real_start(*a, **k)) or raw[-1])
    fleet.settings(orchestrator_profile=None if case == "unset" else "no-such-profile")
    with _busy(fleet):
        raw_out = fleet.send("status? PAYLOAD_H14", raw=True)
        assert raw and raw_out == raw[-1], (raw_out, raw)
        ack = json.loads(raw_out)
        assert ack.get("status") == "queued" and "redrive" not in ack, ack
        _wait_runner_done(fleet, ack["process_id"])
        _wait_completion(fleet, ack["process_id"])
        assert "retry shortly" in _completion_output(fleet, ack["process_id"])
    fleet.tick(4, pause=0.5)
    assert fleet.ticket(ack["delivery_id"]) is None
    assert fleet.rows(TARGET, "PAYLOAD_H14") == []


# AC-FLEET-F62.h-15 is WITHDRAWN by user ruling A1 (Option Z; Option S rejected): no test def, id kept.


_UNVERIFIED = ("Could not verify your message in the target's Bot Chat; delivery outcome is unknown. Do NOT resend. "
               "Check the target before considering another send.")


def _last_json(text):
    try:
        return json.loads((text or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {}


def _settle_without_row(fleet, did, reply="UNPERSISTED-H17", *, unreadable=False):
    """The live owner's turn ends `settled` but its user row never landed (tui_gateway/prompt_turn.py:996-1002).
    ``unreadable``: the target transcript is corrupted after the claim (which reads it) and before the settle, so the
    plugin's probe is `unknown`."""
    from tools import bot_live_delivery as mailbox

    home = fleet.homes[TARGET]
    claim = mailbox.claim_pending_delivery(home, mailbox.find_canonical_live_owner(home))
    assert claim is not None and claim["delivery_id"] == did, claim
    if unreadable:
        (home / "state.db").write_bytes(b"not a sqlite database" * 64)
    mailbox.complete_delivery(home, did, status="settled", reply=reply)


def _assert_unverified_end(fleet, did, body_needle, *, target_readable=True):
    assert fleet.until(lambda: fleet.ticket(did)["state"] == "ambiguous", timeout=60.0), fleet.ticket(did)
    assert fleet.ticket(did)["reason"] == "unknown", fleet.ticket(did)
    assert fleet.until(lambda: len(_notices(fleet, "default", did)) == 1 and len(_notices(fleet, ORCH, did)) == 1,
                       timeout=30.0)
    fleet.tick(3, pause=0.5)
    assert len(_notices(fleet, "default", did)) == 1 and len(_notices(fleet, ORCH, did)) == 1
    for who in ("default", ORCH):
        body = str(_notices(fleet, who, did)[0]["content"])
        assert body == f"Delivery {did} to @{TARGET}: {_UNVERIFIED}", (who, body)   # exact, no generic suffix
    assert fleet.ticket(did)["matched_row_id"] is None
    if target_readable:                                  # never assert absence of a row that cannot be read
        assert fleet.rows(TARGET, body_needle) == []


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("case", ["push", "push_unknown", "non_push", "before_commit", "identical_live",
                                  "commit_late_then_row", "cli", "hook_timeout_external", "stall_before_bind",
                                  "stale_prebind", "slow_spawn", "overlap_ordinary", "overlap_retarget",
                                  "overlap_retarget_real", "overlap_retarget_real_bind_during_poll",
                                  "clock_rollback", "inflight_insert_fail", "inflight_insert_fail_after_return",
                                  "concurrent_transforms", "live_same_runner_collision", "live_prune_during_publish",
                                  "residual_deferred_watcher",
                                  "residual_busy_tail", "residual_idless_kill", "residual_post_timeout",
                                  "residual_worker_cap", "residual_worker_start"])
def test_ac_fleet_f62_h_17(fleet, case, request, monkeypatch):
    """(E5, user ruling msg 120) A tracked delivery's settled outcome is never reported as success unless the target's Bot Chat holds the full role='user' row newer than the baseline and claimed by this ticket (D5a). This covers a live receipt settled by an owner whose turn never persisted the row, and a CLI turn that exited 0 with its reply and no row. For a push sender, core's queued completion is rewritten before publication to status: ambiguous with exactly the text "Could not verify your message in the target's Bot Chat; delivery outcome is unknown. Do NOT resend. Check the target before considering another send." That holds also when the runner beats the ticket commit, and when the commit lands after the plugin's 10 s wait (then a verified late row gives one final settled sender notice). For a non-push sender, core's raw row comes first (waived), then one correction notice. An identical-content sibling's row never vouches. The ticket ends ambiguous/unknown with one sender and one orchestrator notice."""
    if case == "cli":
        _cli_exit0_no_row(fleet)
        return
    if case.startswith("overlap_retarget"):   # CLI path to the hook's real recipient
        _overlap(fleet, monkeypatch, retarget=True,
                 sibling_target="sibling-target" if case == "overlap_retarget" else ORCH,
                 bind_during_poll=case == "overlap_retarget_real_bind_during_poll")
        return
    request.getfixturevalue("live_owner")
    if case == "overlap_ordinary":
        _overlap(fleet, monkeypatch, retarget=False)
        return
    if case in ("clock_rollback", "inflight_insert_fail", "inflight_insert_fail_after_return"):
        _ordinary_unsafe_bookkeeping(fleet, monkeypatch, kind=case)
        return
    if case == "non_push":
        _non_push_unverified(fleet)
        return
    if case == "identical_live":
        _identical_live(fleet)
        return
    if case == "concurrent_transforms":
        _concurrent_transforms(fleet, monkeypatch)
        return
    if case in ("live_same_runner_collision", "live_prune_during_publish"):
        _live_same_runner_collision(fleet, monkeypatch, prune=case == "live_prune_during_publish")
        return
    if case.startswith("residual_"):
        _residual(fleet, monkeypatch, request, case.removeprefix("residual_"))
        return
    if case in ("stall_before_bind", "stale_prebind", "slow_spawn"):
        _stall_before_bind(fleet, monkeypatch, stale=case == "stale_prebind", slow_spawn=case == "slow_spawn")
        return
    if case in ("before_commit", "commit_late_then_row", "hook_timeout_external"):
        _commit_race(fleet, case, monkeypatch)
        return
    ack = fleet.send("status? PAYLOAD_H17_PUSH")
    did, pid = ack["delivery_id"], ack["process_id"]
    assert ack.get("redrive", {}).get("tracked") is True, ack
    # push_unknown: the probe cannot read the target transcript; unknown gets the same text, never asserting absence.
    _settle_without_row(fleet, did, unreadable=case == "push_unknown")
    _wait_runner_done(fleet, pid, timeout=40.0)
    assert fleet.until(lambda: fleet.registry.get(pid)._completion_event.is_set(), ticks=False, timeout=30.0)
    out = _completion_output(fleet, pid)
    assert _last_json(out).get("status") == "ambiguous" and _UNVERIFIED in out, out
    assert '"status": "settled"' not in out and "UNPERSISTED-H17" not in out, out
    _assert_unverified_end(fleet, did, "PAYLOAD_H17_PUSH", target_readable=case != "push_unknown")


def _non_push_unverified(fleet):
    """A sender that cannot receive completions: core's raw `settled` row is not rewritten (waived), and the plugin
    writes exactly one correction notice."""
    import contextvars

    from gateway.session_context import declare_stateless_channel
    from hermes_state import SessionDB

    fleet.agent = _make_sender_agent()
    fleet.agent._session_db = SessionDB(db_path=fleet.root / "state.db")   # arms core's durable row (bot_mode_dm.py:739)

    def stateless_send():
        declare_stateless_channel()
        return fleet.send("status? PAYLOAD_H17_NONPUSH")

    ack = contextvars.copy_context().run(stateless_send)
    did, pid = ack["delivery_id"], ack["process_id"]
    assert ack.get("redrive", {}).get("tracked") is True, ack
    assert "cannot receive completion notifications" in str(ack.get("detail") or ""), ack
    _settle_without_row(fleet, did)
    _wait_runner_done(fleet, pid, timeout=40.0)

    def core_raw_rows():
        _sid, msgs = _bot_chat(fleet.root)
        return [m for m in msgs if m.get("display_kind") == "process_complete"
                and not (m.get("display_metadata") or {}).get("nv_delivery_redrive")
                and pid in str(m.get("content") or "")]
    assert fleet.until(lambda: len(core_raw_rows()) == 1, ticks=False, timeout=30.0)
    assert '"status": "settled"' in str(core_raw_rows()[0]["content"])        # the waived residual, not rewritten
    _assert_unverified_end(fleet, did, "PAYLOAD_H17_NONPUSH")
    assert _UNVERIFIED in str(_notices(fleet, "default", did)[0]["content"])


def _identical_live(fleet):
    """Two identical live messages, of which only one persists: the sibling's row never vouches for the other."""
    from tools import bot_live_delivery as mailbox

    fleet.settings(redrive_window_seconds=12)
    home = fleet.homes[TARGET]
    first, second = fleet.send("status? PAYLOAD_H17_SAME"), fleet.send("status? PAYLOAD_H17_SAME")
    ids = (first["delivery_id"], second["delivery_id"])
    owner = mailbox.find_canonical_live_owner(home)
    for n, did in enumerate(ids):
        claim = mailbox.claim_pending_delivery(home, owner)
        assert claim is not None and claim["delivery_id"] == did, (claim, ids)
        if n == 0:
            _land_live_row(fleet, did)                   # only the first owner turn persisted its row
        mailbox.complete_delivery(home, did, status="settled", reply=f"SAME-{n}")
    for ack in (first, second):
        _wait_runner_done(fleet, ack["process_id"], timeout=40.0)
    assert fleet.until(lambda: all(fleet.ticket(d)["state"] != "pending" for d in ids), timeout=60.0)
    for ack in (first, second):
        out = _completion_output(fleet, ack["process_id"])
        assert _last_json(out).get("status") != "settled", out
    for did in ids:
        assert (fleet.ticket(did)["state"], fleet.ticket(did)["reason"]) == ("ambiguous", "unknown"), fleet.ticket(did)
        assert fleet.until(lambda: len(_notices(fleet, "default", did)) == 1 and len(_notices(fleet, ORCH, did)) == 1)
    assert len(fleet.rows(TARGET, "PAYLOAD_H17_SAME")) == 1


def _concurrent_transforms(fleet, monkeypatch):
    """E11: two tracked live runners whose completion transforms overlap: the first is held inside the plugin until the
    second has entered. Core keys concurrent calls of one hook callback by the call's id, so each completion must carry
    its own id; otherwise the second is skipped and core's raw settled is published. Both must be rewritten exactly."""
    import threading

    from agent.redact import redact_terminal_output
    from tools import bot_live_delivery as mailbox

    for home in fleet.homes.values():
        data = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8-sig"))
        data["plugins"]["hook_callback_timeout"] = 120
        (home / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    fleet.load()
    fleet.tick()
    store = fleet.loaded.module.store
    real_lookup = store.attempt_by_dm_path
    lock, entered = threading.Lock(), []
    armed, second_in, release = threading.Event(), threading.Event(), threading.Event()

    def held(dm_path, *a, **k):
        # Only the two runners' completion transforms: post_tool_call also runs on a hook worker, during the sends.
        if armed.is_set() and threading.current_thread().name.startswith("hermes-hook-"):
            with lock:
                first = not entered
                if threading.current_thread().ident not in entered:
                    entered.append(threading.current_thread().ident)
                if len(entered) == 2:
                    second_in.set()
            if first:
                assert second_in.wait(timeout=60.0), "the second completion's transform never entered"
                release.set()
        return real_lookup(dm_path, *a, **k)
    monkeypatch.setattr(store, "attempt_by_dm_path", held)
    home = fleet.homes[TARGET]
    first, second = fleet.send("status? PAYLOAD_H17_CONC_A"), fleet.send("status? PAYLOAD_H17_CONC_B")
    owner = mailbox.find_canonical_live_owner(home)
    armed.set()
    try:
        for n, ack in enumerate((first, second)):
            claim = mailbox.claim_pending_delivery(home, owner)
            assert claim is not None and claim["delivery_id"] == ack["delivery_id"], (claim, ack)
            mailbox.complete_delivery(home, ack["delivery_id"], status="settled", reply=f"CONC-{n}")
        for ack in (first, second):
            _wait_runner_done(fleet, ack["process_id"], timeout=40.0)
        for ack in (first, second):
            assert fleet.until(lambda: fleet.registry.get(ack["process_id"])._completion_event.is_set(), ticks=False,
                               timeout=90.0)
    finally:
        second_in.set()
    assert release.is_set() and len(entered) == 2, entered
    for ack in (first, second):
        session = fleet.registry.get(ack["process_id"])
        expected = json.dumps({"status": "ambiguous", "reason": "unknown", "delivery_id": ack["delivery_id"],
                               "to": f"@{TARGET}", "detail": _UNVERIFIED})
        out = _completion_output(fleet, ack["process_id"])
        assert out == redact_terminal_output(expected, session.command), (out, expected)
    for ack, needle in ((first, "PAYLOAD_H17_CONC_A"), (second, "PAYLOAD_H17_CONC_B")):
        _assert_unverified_end(fleet, ack["delivery_id"], needle)


class _GatewayWatcherRig:
    """A real GatewayRunner whose event loop runs on a thread, with a bound messaging session. A runner spawned inside a
    send then gets core's live watcher (tools/terminal_tool_background.py:197-202); every completion the watcher would
    hand the sender agent is captured."""

    def __init__(self, fleet, monkeypatch):
        import asyncio
        import threading
        from types import SimpleNamespace as NS
        from unittest.mock import AsyncMock

        import gateway.run as gateway_run
        from gateway.config import GatewayConfig, Platform
        from gateway.session_context import set_session_vars

        monkeypatch.setattr(gateway_run, "_hermes_home", fleet.root)
        self.loop = asyncio.new_event_loop()
        self.injected, self.sent = [], []
        self.runner = gateway_run.GatewayRunner(GatewayConfig())
        self.session_key = "agent:main:telegram:dm:123"
        self.adapter = NS(send=AsyncMock(side_effect=lambda *a, **k: self.sent.append(a[1])),
                          handle_message=AsyncMock(), _active_sessions={})
        self.runner.adapters[Platform.TELEGRAM] = self.adapter

        async def _capture(synth_text, evt):
            self.injected.append((synth_text, evt))
            return True
        self.runner._enqueue_process_completion_notification = _capture
        self.runner._running, self.runner._gateway_loop = True, self.loop
        monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: self.runner)
        self.thread = threading.Thread(target=self.loop.run_forever, name="f62h-gateway-loop", daemon=True)
        self.thread.start()
        self.tokens = set_session_vars(platform="telegram", chat_id="123", session_key=self.session_key)

    def close(self):
        from gateway.session_context import clear_session_vars

        clear_session_vars(self.tokens)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=10)

    def injected_for(self, process_id):
        return [text for text, evt in self.injected if evt.get("session_id") == process_id]

    def start_sender(self, fleet, body, acks):
        """The send runs on its own thread inside this rig's bound session context (threads do not inherit it)."""
        import contextvars
        import threading

        thread = threading.Thread(target=contextvars.copy_context().run,
                                  args=(lambda: acks.append(fleet.send(body)),), daemon=True)
        thread.start()
        return thread

    def run_deferred(self, process_id):
        """Start the one watcher core parked in ``pending_watchers`` the way its post-turn drain does: on the loop, from
        a context with no bound call id (gateway/run_turn.py:1666-1671)."""
        import asyncio

        from tools.process_registry import process_registry as registry

        mine = [w for w in registry.pending_watchers if w.get("session_id") == process_id]
        assert len(mine) == 1, registry.pending_watchers
        registry.pending_watchers = [w for w in registry.pending_watchers if w.get("session_id") != process_id]
        self.runner._running = True
        asyncio.run_coroutine_threadsafe(self.runner._run_process_watcher(mine[0]), self.loop)


def _live_same_runner_collision(fleet, monkeypatch, *, prune=False):
    """E11 (vi): in a gateway, core's live watcher hands the sender agent its own transform of the runner's output, under
    the same bound id as the reader's transform. With the reader's transform held in the plugin's 10 s wait (the
    fail-closed case), the watcher must wait for the reader's publish, so the plugin's rewrite reaches the sender.
    ``prune`` (E11.1): while that publish is held, another spawn prunes the registry with the runner older than the
    finished-session TTL; the unpublished session must stay registered rather than be dropped or reloaded."""
    import sys
    import threading

    from tools import process_registry as pr_mod

    store = fleet.loaded.module.store
    real_commit, gate = store.commit_ticket, threading.Event()
    real_publish, published = fleet.registry._publish_finished, threading.Event()

    def held_commit(*a, **k):
        assert gate.wait(timeout=120), "commit gate never released"
        return real_commit(*a, **k)

    def held_publish(session, *a, **k):
        if "--run-delivery" in session.command:
            assert published.wait(timeout=120), "publish gate never released"
        return real_publish(session, *a, **k)
    monkeypatch.setattr(store, "commit_ticket", held_commit)
    if prune:
        monkeypatch.setattr(fleet.registry, "_publish_finished", held_publish)
    else:
        published.set()
    rig = _GatewayWatcherRig(fleet, monkeypatch)
    acks, sender = [], None
    try:
        sender = rig.start_sender(fleet, "status? PAYLOAD_H17_GWLIVE", acks)
        assert fleet.until(lambda: _runner_of(fleet) is not None
                           and _admissions(fleet.homes[TARGET], "PAYLOAD_H17_GWLIVE"), ticks=False, timeout=30.0)
        session = _runner_of(fleet)
        fleet.until(lambda: session.watcher_platform, ticks=False, timeout=10.0)  # core stamps it after registering
        assert session.watcher_platform == "telegram", "no live gateway watcher was armed"
        did = _admissions(fleet.homes[TARGET], "PAYLOAD_H17_GWLIVE")[0]["delivery_id"]
        _settle_without_row(fleet, did)
        assert fleet.until(lambda: session.exited, ticks=False, timeout=40.0)
        if prune:
            assert fleet.until(lambda: session.id in fleet.registry._finished, ticks=False, timeout=40.0)
            assert not session._completion_event.is_set(), "the publish was not held"
            # A TTL of 0 ages the runner without touching started_at, which its completion event carries.
            with patch.object(pr_mod, "FINISHED_TTL_SECONDS", 0):
                other = fleet.registry.spawn_local(f"{shlex.quote(sys.executable)} -c pass", task_id="f62h-prune")
            assert fleet.registry._finished.get(session.id) is session, "an unpublished session was pruned"
            assert fleet.registry.get(session.id) is session, "the watcher would read a reloaded copy"
            assert not session._completion_event.is_set() and not rig.injected_for(session.id), "prune missed the hold"
            assert fleet.until(lambda: other.exited, ticks=False, timeout=30.0)
            published.set()
        assert fleet.until(lambda: rig.injected_for(session.id), ticks=False, timeout=60.0), "watcher never injected"
    finally:
        gate.set()
        published.set()
        if sender is not None:
            sender.join(timeout=120)
        rig.close()
    texts = rig.injected_for(session.id)
    assert len(texts) == 1, texts
    assert _UNVERIFIED in texts[0] and '"status": "ambiguous"' in texts[0], texts[0]
    assert '"status": "settled"' not in texts[0] and "UNPERSISTED-H17" not in texts[0], texts[0]
    assert session._completion_event.is_set()
    assert not any("outcome is unknown" in str(m) for m in rig.sent), rig.sent
    _assert_unverified_end(fleet, did, "PAYLOAD_H17_GWLIVE")


def _hold_idless_transform(fleet, monkeypatch, release, *, name="holder", tool_call_id=""):
    """Run one call of the plugin's transform through core on a thread with NO bound call id (or ``tool_call_id``), and
    hold it inside the plugin until ``release`` is set: the competing call that makes core skip a later one."""
    import contextvars
    import threading

    from tools.approval_context import set_current_observability_context
    from tools.process_registry import transform_process_output

    dm = str(fleet.dm_dir / f"{name}.dm")
    cmd = fleet.bot_mode_dm._delivery_command([str(fleet.fake), "-p", TARGET, *fleet.bot_relay.BOT_CHAT_TURN_ARGS], dm,
                                              stdin_file=False)
    store = fleet.loaded.module.store
    real, entered = store.attempt_by_dm_path, threading.Event()

    def held(dm_path, *a, **k):
        if str(dm_path).endswith(f"{name}.dm"):
            entered.set()
            assert release.wait(timeout=240), "holder never released"
        return real(dm_path, *a, **k)
    monkeypatch.setattr(store, "attempt_by_dm_path", held)

    def run():
        if tool_call_id:
            set_current_observability_context(tool_call_id=tool_call_id)
        transform_process_output('{"status": "settled", "delivery_id": "holder"}', command=cmd, returncode=0)
    thread = threading.Thread(target=contextvars.Context().run, args=(run,), daemon=True)
    thread.start()
    assert entered.wait(timeout=30), "the holder never entered the plugin"
    return thread


def _hold_reader_finish(fleet, monkeypatch, gate):
    """Keep the runner's reader from finishing (it has read the whole output, `settled` line included) until ``gate``."""
    real = fleet.registry._finish_exited

    def held(session, *a, **k):
        if "--run-delivery" in session.command:
            assert gate.wait(timeout=240), "reader gate never released"
        return real(session, *a, **k)
    monkeypatch.setattr(fleet.registry, "_finish_exited", held)


def _assert_backstop(fleet, ack, needle):
    """The named-residual backstop for a tracked ticket still unverified at a drainer tick: ambiguous/unknown, exactly one
    correction to the sender and one to the orchestrator, and no resend."""
    _assert_unverified_end(fleet, ack["delivery_id"], needle)
    assert len(fleet.attempts(ack["delivery_id"])) == 1, fleet.attempts(ack["delivery_id"])


def _residual(fleet, monkeypatch, request, path):
    """Assert each named residual exposes raw settled on its actual surface, then reaches the conditional backstop."""
    import threading

    release, reader_gate = threading.Event(), threading.Event()
    needle = f"PAYLOAD_H17_RES_{path.upper()}"
    rig = None
    try:
        if path in ("post_timeout", "worker_cap"):
            for home in fleet.homes.values():
                data = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8-sig"))
                data["plugins"]["hook_callback_timeout"] = 2
                (home / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
            fleet.load()
            fleet.tick()
        if path == "post_timeout":
            _hold_idless_transform(fleet, monkeypatch, release)
            # health: allow HX013 -- the elapsed 2 s hook-callback bound is the post_timeout residual under test
            time.sleep(3.0)                               # the holder outlived the 2 s bound: callback-wide suppression
        elif path == "worker_cap":
            fleet.manager._hook_timeout_suppression_seconds = 0.5
            for n in range(3):
                _hold_idless_transform(fleet, monkeypatch, release, name=f"holder{n}", tool_call_id=f"cap-{n}")
                time.sleep(3.0)  # health: allow HX013 -- each holder outlives the 2 s bound: the worker cap fills over that time
        if path in ("deferred_watcher", "busy_tail"):
            rig = _GatewayWatcherRig(fleet, monkeypatch)
            rig.runner._running = False                   # not serving at spawn: core parks the watcher
        if path in ("idless_kill", "busy_tail"):
            _hold_reader_finish(fleet, monkeypatch, reader_gate)
        acks = []
        if rig is not None:
            rig.start_sender(fleet, f"status? {needle}", acks).join(timeout=120)
        else:
            acks.append(fleet.send(f"status? {needle}"))
        ack = acks[0]
        assert ack.get("redrive", {}).get("tracked") is True, ack
        if path in ("deferred_watcher", "idless_kill", "busy_tail"):
            _hold_idless_transform(fleet, monkeypatch, release)
        refused = []
        if path == "worker_start":
            import hermes_cli.plugins_dispatch as dispatch

            real_thread = dispatch.threading.Thread

            class _NoStart(real_thread):
                def start(self):
                    if self.name.startswith("hermes-hook-"):
                        refused.append(self.name)
                        raise RuntimeError("can't start new thread (test)")
                    return super().start()
            monkeypatch.setattr(dispatch.threading, "Thread", _NoStart)
        _settle_without_row(fleet, ack["delivery_id"])
        session = fleet.registry.get(ack["process_id"])
        if path in ("idless_kill", "busy_tail"):
            assert fleet.until(lambda: '"status": "settled"' in session.output_buffer, ticks=False, timeout=40.0)
            fleet.registry.kill_all(fleet.task_id, source="f62h-residual", consume_output=False)
        assert fleet.until(session._completion_event.is_set, ticks=False, timeout=60.0)
        if path in ("deferred_watcher", "busy_tail"):
            if path == "busy_tail":                       # a still-running launching turn makes core send the receipt
                rig.adapter._active_sessions = {rig.session_key: object()}
            rig.run_deferred(session.id)
            assert fleet.until(lambda: rig.injected_for(session.id), ticks=False, timeout=60.0)
            exposed = rig.injected_for(session.id)[0]
            if path == "busy_tail":
                assert fleet.until(lambda: rig.sent, ticks=False, timeout=30.0)
                exposed = rig.sent[-1]                    # the chat receipt itself carries the tail on a nonzero exit
        else:
            exposed = _completion_output(fleet, ack["process_id"])
        if path == "worker_start":
            monkeypatch.setattr(dispatch.threading, "Thread", real_thread)
            assert refused, "no hook worker start was refused"
        assert '"status": "settled"' in exposed, exposed
    finally:
        release.set()
        reader_gate.set()
        if rig is not None:
            rig.close()
    _assert_backstop(fleet, ack, needle)


def _runner_of(fleet):
    """The one live delivery runner this test spawned (core's registry, owner_task_id = the test's task id)."""
    found = [s for s in list(fleet.registry._running.values())
             if "--run-delivery" in s.command and getattr(s, "owner_task_id", "") == fleet.task_id]
    return found[0] if len(found) == 1 else None


def _commit_race(fleet, case, monkeypatch):
    """The runner publishes before the ticket commit. The plugin's own 10 s wait fails closed (before_commit), a row
    landing after that rewrite still settles with one final sender notice (commit_late_then_row), and a hook timeout
    below 10 s is core's fail-open, reported under the waiver (hook_timeout_external)."""
    import threading

    from tools import bot_live_delivery as mailbox

    if case == "hook_timeout_external":
        for home in fleet.homes.values():
            data = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8-sig"))
            data["plugins"]["hook_callback_timeout"] = 3
            (home / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
        fleet.load()
        fleet.tick()
    store = fleet.loaded.module.store
    real_commit, gate = store.commit_ticket, threading.Event()

    def held(*a, **k):
        assert gate.wait(timeout=120), "commit gate never released"
        return real_commit(*a, **k)
    monkeypatch.setattr(store, "commit_ticket", held)
    acks = []
    sender = threading.Thread(target=lambda: acks.append(fleet.send("status? PAYLOAD_H17_RACE")), daemon=True)
    sender.start()
    try:
        assert fleet.until(lambda: _runner_of(fleet) is not None and _admissions(fleet.homes[TARGET], "PAYLOAD_H17_RACE"),
                           ticks=False, timeout=30.0)
        session = _runner_of(fleet)
        did = _admissions(fleet.homes[TARGET], "PAYLOAD_H17_RACE")[0]["delivery_id"]
        _settle_without_row(fleet, did)
        assert fleet.until(session._completion_event.is_set, ticks=False, timeout=40.0)   # published, ticket uncommitted
        assert store.ticket(did) is None
        out = _completion_output(fleet, session.id)
        if case == "hook_timeout_external":
            assert _last_json(out).get("status") == "settled", out   # core's fail-open (waived), not a fail-closed pass
        else:
            assert _last_json(out).get("status") == "ambiguous" and _UNVERIFIED in out, out
        if case == "commit_late_then_row":
            _land_live_row(fleet, did)                   # the owner's late flush lands the row before the commit
    finally:
        gate.set()
        sender.join(timeout=120)
    assert acks and acks[0]["delivery_id"] == did, acks
    if case == "commit_late_then_row":
        assert fleet.until(lambda: fleet.ticket(did)["state"] == "settled", timeout=60.0), fleet.ticket(did)
        assert fleet.until(lambda: len(_notices(fleet, "default", did)) == 1, timeout=30.0)
        fleet.tick(3, pause=0.5)
        notes = _notices(fleet, "default", did)
        assert len(notes) == 1 and "UNPERSISTED-H17" in str(notes[0]["content"]), notes
        assert len(fleet.rows(TARGET, "PAYLOAD_H17_RACE")) == 1
        return
    _assert_unverified_end(fleet, did, "PAYLOAD_H17_RACE")


def _stall_before_bind(fleet, monkeypatch, *, stale=False, slow_spawn=False):
    """E9.1: the sender stalls inside next_call after core spawned the runner, so the runner reaches the transform
    before its call's in-flight row is bound. With no persisted target row it still fails closed to ambiguous.
    ``stale``: the stall outlasts 60 s after the spawn while the drainer ticks; ``slow_spawn``: 60 s pass, ticking,
    before core spawns at all. Neither ages the open row out."""
    import threading

    gate, acks = threading.Event(), []
    real_spawn = fleet.bot_mode_dm._spawn_delivery

    def stalled(*a, **k):
        if slow_spawn:                                    # work before the spawn has no core bound
            pre_end = time.monotonic() + 65.0
            while time.monotonic() < pre_end:
                fleet.tick()
                time.sleep(2.0)  # health: allow HX013 -- 65 s of pre-spawn work under live ticks IS the slow-spawn case
        out = real_spawn(*a, **k)                         # the runner is spawned; next_call has not returned
        assert gate.wait(timeout=120), "spawn gate never released"
        return out
    monkeypatch.setattr(fleet.bot_mode_dm, "_spawn_delivery", stalled)
    sender = threading.Thread(target=lambda: acks.append(fleet.send("status? PAYLOAD_H17_PREBIND")), daemon=True)
    sender.start()
    try:
        assert fleet.until(lambda: _runner_of(fleet) is not None
                           and _admissions(fleet.homes[TARGET], "PAYLOAD_H17_PREBIND"), ticks=False,
                           timeout=100.0 if slow_spawn else 30.0)
        session = _runner_of(fleet)
        did = _admissions(fleet.homes[TARGET], "PAYLOAD_H17_PREBIND")[0]["delivery_id"]
        if stale:
            stall_end = time.monotonic() + 65.0           # past the 60 s in-flight age, with live drainer ticks
            while time.monotonic() < stall_end:
                fleet.tick()
                time.sleep(2.0)  # health: allow HX013 -- 65 s past the 60 s in-flight age under live ticks IS the stale case
        settled_at = time.monotonic()
        _settle_without_row(fleet, did)
        assert fleet.until(session._completion_event.is_set, ticks=False, timeout=40.0)
        assert time.monotonic() - settled_at > 10.0      # the plugin's whole 10 s wait ran inside the stall
        assert fleet.loaded.module.store.ticket(did) is None and not acks                # still before the bind
        out = _completion_output(fleet, session.id)
        assert _last_json(out).get("status") == "ambiguous" and _UNVERIFIED in out, out
        assert '"status": "settled"' not in out and "UNPERSISTED-H17" not in out, out
    finally:
        gate.set()
        sender.join(timeout=120)
    assert acks and acks[0]["delivery_id"] == did, acks
    _assert_unverified_end(fleet, did, "PAYLOAD_H17_PREBIND")


_RETARGET_INIT = textwrap.dedent('''\
    def register(ctx):
        def _gate(tool_name=None, args=None, **kwargs):
            if tool_name == "message_agent" and "RETARGET_ME_H17" in str((args or {}).get("message")):
                return {"action": "modify", "args": {"target": "orchestrator"}}
            return None
        ctx.register_hook("pre_tool_call", _gate)
''')


def _overlap(fleet, monkeypatch, *, retarget, sibling_target="sibling-target", bind_during_poll=False):
    """E9.2-E9.4: a same-task in-flight row is opened after this call's D3 row and before its spawn, and the call
    stalls before its bind. Ordinary call: fails closed, sibling row never bound or marked. Re-target whose real
    recipient no candidate names: core's completion byte-for-byte. Re-target whose real recipient a candidate names
    (``sibling_target == ORCH``): exactly the unverified rewrite, still with no ticket, retry, resend or notice, also
    when its own bind is released inside the gate's 10 s poll (``bind_during_poll``, the D5a.2 latch)."""
    import threading

    from agent.redact import redact_terminal_output
    from tools.process_registry import _completion_output as core_tail

    if retarget:
        gate_dir = fleet.root / "plugins" / "f62h-retarget-gate"
        gate_dir.mkdir(parents=True)
        (gate_dir / "plugin.yaml").write_text(_GATE_YAML.replace("f62h-test-gate", "f62h-retarget-gate"),
                                              encoding="utf-8")
        (gate_dir / "__init__.py").write_text(_RETARGET_INIT, encoding="utf-8")
        cfg = yaml.safe_load((fleet.root / "config.yaml").read_text(encoding="utf-8-sig"))
        cfg["plugins"]["enabled"].append("f62h-retarget-gate")
        (fleet.root / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
        fleet.load()
        fleet.tick()
        fleet.configure(ORCH, pre=0.1, mode="exit0_unpersisted")   # the real recipient replies, row unproven
    store = fleet.loaded.module.store
    gate, acks, sibling, sibling_snap, own, core_acks = threading.Event(), [], [], [], [], []
    real_open = store.open_inflight
    monkeypatch.setattr(store, "open_inflight", lambda *a, **k: own.append(real_open(*a, **k)) or own[-1])
    real_spawn = fleet.bot_mode_dm._spawn_delivery
    real_start = fleet.bot_mode_dm._start_delivery
    monkeypatch.setattr(fleet.bot_mode_dm, "_start_delivery",
                        lambda *a, **k: core_acks.append(real_start(*a, **k)) or core_acks[-1])
    in_transform = threading.Event()
    if bind_during_poll:
        import tools.process_registry as registry_mod

        real_transform = registry_mod.transform_process_output

        def entered(output, **k):
            if "--run-delivery" in str(k.get("command") or "") and k.get("returncode") is not None:
                in_transform.set()
            return real_transform(output, **k)
        monkeypatch.setattr(registry_mod, "transform_process_output", entered)

    def stalled(*a, **k):
        # Opened AFTER this call's D3 row and BEFORE the spawn: a "latest open row of the task" match would take it.
        sibling.append(real_open(sender_session_id="bc-sibling", task_id=fleet.task_id, target=sibling_target,
                                 baseline=0))
        sibling_snap.append(dict(store.inflight(sibling[0])))
        out = real_spawn(*a, **k)
        assert gate.wait(timeout=120), "spawn gate never released"
        return out
    monkeypatch.setattr(fleet.bot_mode_dm, "_spawn_delivery", stalled)
    body = "status? RETARGET_ME_H17" if retarget else "status? PAYLOAD_H17_OVERLAP"
    sender = threading.Thread(target=lambda: acks.append(fleet.send(body, raw=True)), daemon=True)
    sender.start()
    try:
        assert fleet.until(lambda: _runner_of(fleet) is not None, ticks=False, timeout=30.0)
        session = _runner_of(fleet)
        assert len(own) == 1 and sibling_snap and sibling_snap[0]["state"] == "open", (own, sibling_snap)
        assert not sibling_snap[0].get("process_id"), sibling_snap
        if not retarget:
            assert fleet.until(lambda: _admissions(fleet.homes[TARGET], "PAYLOAD_H17_OVERLAP"), ticks=False,
                               timeout=30.0)
            did = _admissions(fleet.homes[TARGET], "PAYLOAD_H17_OVERLAP")[0]["delivery_id"]
            _settle_without_row(fleet, did)
        if bind_during_poll:
            assert in_transform.wait(timeout=30.0), "the runner's completion never reached the transform"
            entered_at = time.monotonic()
            time.sleep(1.0)  # health: allow HX013 -- a negative check: 1 s into its window the gate must still be polling
            assert not session._completion_event.is_set(), "the gate did not poll"
            assert not store.inflight(own[0]).get("process_id"), store.inflight(own[0])
            assert dict(store.inflight(sibling[0])) == sibling_snap[0], (store.inflight(sibling[0]), sibling_snap[0])
            gate.set()
            assert fleet.until(lambda: store.inflight(own[0]).get("process_id") == session.id, ticks=False,
                               timeout=5.0, pause=0.05), store.inflight(own[0])
            assert time.monotonic() - entered_at < 7.5, "the own bind landed too late in the poll"
            assert store.inflight(own[0])["state"] == "untracked", store.inflight(own[0])
        assert fleet.until(session._completion_event.is_set, ticks=False, timeout=40.0)
        if bind_during_poll:
            assert time.monotonic() - entered_at < 9.5, "the 10 s expiry, not the own bind, ended the poll"
        out = _completion_output(fleet, session.id)
        if retarget:
            argv = shlex.split(session.command)
            assert argv[argv.index("-p") + 1] == ORCH, session.command   # the hook's real recipient
            if sibling_target != ORCH:
                assert out == redact_terminal_output(core_tail(session)["output"], session.command), out
        else:
            assert _last_json(out).get("status") == "ambiguous" and _UNVERIFIED in out, out
            assert '"status": "settled"' not in out, out
        assert dict(store.inflight(sibling[0])) == sibling_snap[0], (store.inflight(sibling[0]), sibling_snap[0])
    finally:
        gate.set()
        sender.join(timeout=120)
    ack = json.loads(acks[0])
    assert dict(store.inflight(sibling[0])) == sibling_snap[0], (store.inflight(sibling[0]), sibling_snap[0])
    if retarget:
        if sibling_target == ORCH:
            expected = json.dumps({"status": "ambiguous", "reason": "unknown", "delivery_id": ack["delivery_id"],
                                   "to": f"@{ORCH}", "detail": _UNVERIFIED})
            assert out == redact_terminal_output(expected, session.command), (out, expected)
        assert core_acks and acks[0] == core_acks[-1], (acks, core_acks)
        assert "redrive" not in ack and fleet.ticket(ack["delivery_id"]) is None, ack
        fleet.tick(3, pause=0.5)
        for who in ("default", ORCH):
            assert _notices(fleet, who, ack["delivery_id"]) == [], who
        assert fleet.attempts(ack["delivery_id"]) == []
        return
    _assert_unverified_end(fleet, did, "PAYLOAD_H17_OVERLAP")


def _ordinary_unsafe_bookkeeping(fleet, monkeypatch, *, kind):
    """E9.3: a backward clock step across the spawn (``clock_rollback``), or a failed in-flight insert settled before
    (``inflight_insert_fail``) or after (``inflight_insert_fail_after_return``) next_call returns: an ordinary call
    still publishes no raw settled."""
    import threading

    store = fleet.loaded.module.store
    rows, insert_calls = [], []
    real_open = store.open_inflight
    if kind.startswith("inflight_insert_fail"):
        def failing(*a, **k):
            insert_calls.append(k)
            raise RuntimeError("in-flight insert failed (test)")
        monkeypatch.setattr(store, "open_inflight", failing)
    else:
        monkeypatch.setattr(store, "open_inflight", lambda *a, **k: rows.append(real_open(*a, **k)) or rows[-1])
    gate, acks = threading.Event(), []
    real_spawn = fleet.bot_mode_dm._spawn_delivery

    def stalled(*a, **k):
        real_time = time.time
        if kind == "clock_rollback":                      # the wall clock steps back after D3 opened the row
            monkeypatch.setattr(time, "time", lambda: real_time() - 3600.0)
        try:
            out = real_spawn(*a, **k)
        finally:
            monkeypatch.setattr(time, "time", real_time)
        assert gate.wait(timeout=120), "spawn gate never released"
        return out
    monkeypatch.setattr(fleet.bot_mode_dm, "_spawn_delivery", stalled)
    sender = threading.Thread(target=lambda: acks.append(fleet.send("status? PAYLOAD_H17_BOOK", raw=True)), daemon=True)
    sender.start()
    try:
        assert fleet.until(lambda: _runner_of(fleet) is not None
                           and _admissions(fleet.homes[TARGET], "PAYLOAD_H17_BOOK"), ticks=False, timeout=30.0)
        session = _runner_of(fleet)
        if kind == "clock_rollback":
            assert rows and session.started_at < store.inflight(rows[0])["created_at"], (session.started_at, rows)
        else:
            assert len(insert_calls) == 1, insert_calls
        did = _admissions(fleet.homes[TARGET], "PAYLOAD_H17_BOOK")[0]["delivery_id"]
        if kind == "inflight_insert_fail_after_return":
            gate.set()
            sender.join(timeout=120)                      # next_call has returned before the settle
            assert acks
        _settle_without_row(fleet, did)
        assert fleet.until(session._completion_event.is_set, ticks=False, timeout=40.0)
        out = _completion_output(fleet, session.id)
        assert _last_json(out).get("status") == "ambiguous" and _last_json(out).get("detail") == _UNVERIFIED, out
        assert '"status": "settled"' not in out and "UNPERSISTED-H17" not in out, out
    finally:
        gate.set()
        sender.join(timeout=120)
    ack = json.loads(acks[0])
    if kind.startswith("inflight_insert_fail"):
        assert "redrive" not in ack and fleet.ticket(did) is None, ack   # untracked (AC-14), yet never a raw settled
        return
    _assert_unverified_end(fleet, did, "PAYLOAD_H17_BOOK")


def _cli_exit0_no_row(fleet):
    """A text-only CLI turn whose persists failed exits 0 with its reply and no row (D5a): rewritten, never settled."""
    fleet.settings(redrive_window_seconds=12)
    fleet.configure(TARGET, mode="exit0_unpersisted", pre=0.1)
    ack = fleet.send("status? PAYLOAD_H17_CLI")
    did, pid = ack["delivery_id"], ack["process_id"]
    assert ack.get("redrive", {}).get("tracked") is True, ack
    _wait_runner_done(fleet, pid, timeout=40.0)
    assert fleet.until(lambda: fleet.registry.get(pid)._completion_event.is_set(), ticks=False, timeout=30.0)
    out = _completion_output(fleet, pid)
    assert _last_json(out).get("status") == "ambiguous" and _UNVERIFIED in out, out
    assert "REPLY-FROM-researcher" not in out, out
    _assert_unverified_end(fleet, did, "PAYLOAD_H17_CLI")
    assert len(fleet.attempts(did)) == 1, fleet.attempts(did)    # never re-sent (B1 / D5 "exit 0, absent")
