"""Safety regression tests for nv-delivery-redrive: re-targeted calls (the E9 guard), a stale tick
snapshot, a late identical sibling and a silent exit 0. Each loads the plugin through the acceptance test's ``fleet``
fixture (real discovery, real executor, real delivery runner, fake target CLI) and asserts one outcome.
"""

from __future__ import annotations

import json
import sys
import textwrap

import pytest

from tests.plugins.test_fleet_f62_h_acceptance import (
    _UNVERIFIED,
    ORCH,
    TARGET,
    _admissions,
    _busy,
    _completion_output,
    _last_json,
    _notices,
    _runner_of,
    _settle_without_row,
    _wait_runner_done,
    fleet,
    live_owner,
)

_SILENT_TARGET = textwrap.dedent('''\
    #!{python}
    import json, os
    with open({pidlog!r}, "a", encoding="utf-8") as f:
        f.write(json.dumps({{"pid": os.getpid()}}) + "\\n")
''')

_REWRITE_YAML = ("name: f62h-rewrite-gate\nversion: 0.0.1\ndescription: test gate\nmanifest_version: 1\n"
                 "provides_hooks: [pre_tool_call]\nprovides_tools: []\n")
_REWRITE_INIT = textwrap.dedent(f'''\
    def register(ctx):
        def _gate(tool_name=None, args=None, **kwargs):
            if tool_name == "message_agent" and "REWRITE_S1" in str((args or {{}}).get("message")):
                return {{"action": "modify", "args": {{"target": "{ORCH}"}}}}
            return None
        ctx.register_hook("pre_tool_call", _gate)
''')


def _install_rewrite_gate(fleet):
    """A third-party pre_tool_call plugin whose modify re-targets any message_agent call carrying REWRITE_S1 to ORCH."""
    import hermes_yaml as yaml

    gate = fleet.root / "plugins" / "f62h-rewrite-gate"
    gate.mkdir(parents=True)
    (gate / "plugin.yaml").write_text(_REWRITE_YAML, encoding="utf-8")
    (gate / "__init__.py").write_text(_REWRITE_INIT, encoding="utf-8")
    cfg = yaml.safe_load((fleet.root / "config.yaml").read_text(encoding="utf-8-sig"))
    cfg["plugins"]["enabled"].append("f62h-rewrite-gate")
    (fleet.root / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    fleet.load()
    fleet.tick()


@pytest.mark.platforms("linux")
def test_rewritten_target_is_not_tracked(fleet):
    """A pre_tool_call modify that re-targets the call is not ticketed under the target the middleware resolved, so
    a busy refusal from the real target is never re-driven to the original one."""
    _install_rewrite_gate(fleet)
    with _busy(fleet, ORCH):                             # the real target refuses busy
        out = fleet.send("status? REWRITE_S1")
        assert out.get("delivery_id") and out.get("process_id") and "redrive" not in out, out
        _wait_runner_done(fleet, out["process_id"])
    fleet.tick(6, pause=1.0)
    assert fleet.ticket(out["delivery_id"]) is None
    assert fleet.rows(TARGET, "REWRITE_S1") == []


@pytest.mark.platforms("linux")
def test_stale_snapshot_keeps_live_writer_running(fleet, monkeypatch):
    """A tick whose process snapshot predates a live attempt's writer leaves that attempt running, and the message
    still lands exactly once."""
    probe = fleet.loaded.module.probe
    real = probe.scan_processes
    stale = {"armed": False}

    def scan():
        if stale["armed"]:
            stale["armed"] = False
            return []
        return real()

    monkeypatch.setattr(probe, "scan_processes", scan)
    fleet.configure(TARGET, pre=5.0)
    ack = fleet.send("status? PAYLOAD_S2")
    did = ack["delivery_id"]
    assert fleet.until(lambda: fleet.pidlog.exists(), ticks=False, timeout=10.0)
    fleet.configure(TARGET, pre=0.1)
    stale["armed"] = True
    fleet.tick()
    assert not stale["armed"]
    assert fleet.attempts(did)[0]["state"] == "running", fleet.attempts(did)
    assert fleet.until(lambda: fleet.ticket(did)["state"] == "settled", timeout=60.0)
    assert len(fleet.rows(TARGET, "PAYLOAD_S2")) == 1
    assert len(fleet.attempts(did)) == 1


@pytest.mark.platforms("linux")
def test_late_identical_sibling_row_is_not_claimed(fleet):
    """A ticket whose attempt was still open at its bound does not claim, once that attempt ends row-less, the sole
    row an overlapping identical sibling may have written: neither ticket settles on it and nothing is re-sent."""
    import psutil

    fleet.settings(redrive_window_seconds=10)
    body = "status? PAYLOAD_S3"
    fleet.configure(TARGET, pre=20.0, mode="not_owned")  # attempt 1's turn outlives the bound, then ends row-less
    first = fleet.send(body)
    runner = psutil.Process(fleet.registry.get(first["process_id"]).pid)
    # The fake logs its pid only after reading its spec: from then on attempt 1 is a not-owned turn, whatever follows.
    assert fleet.until(lambda: fleet.pidlog.exists(), ticks=False, timeout=10.0)
    fleet.configure(TARGET, pre=0.1)
    runner.kill()                                        # releases the runner-held flock; the target CLI lives on
    _wait_runner_done(fleet, first["process_id"])
    second = fleet.send(body)
    _wait_runner_done(fleet, second["process_id"])
    assert len(fleet.rows(TARGET, "PAYLOAD_S3")) == 1
    a, b = first["delivery_id"], second["delivery_id"]
    assert fleet.until(lambda: fleet.ticket(a)["late_open"] == 1, timeout=30.0)
    assert fleet.until(lambda: fleet.ticket(a)["late_open"] == 0, timeout=40.0)
    assert fleet.until(lambda: fleet.ticket(b)["state"] != "pending", timeout=30.0)
    fleet.tick(3, pause=0.5)
    ta, tb = fleet.ticket(a), fleet.ticket(b)
    assert ta["matched_row_id"] is None and tb["matched_row_id"] is None, (ta, tb)
    assert json.loads(ta["late_outcome"])["state"] == "ambiguous", ta
    assert tb["state"] == "ambiguous", tb
    assert len(fleet.rows(TARGET, "PAYLOAD_S3")) == 1
    assert len(fleet.attempts(b)) == 1


@pytest.mark.platforms("linux")
def test_silent_exit0_is_never_resent(fleet):
    """A target turn that exits 0 with no output and no row is never re-sent: the ticket ends ambiguous/unknown."""
    fleet.fake.write_text(_SILENT_TARGET.format(python=sys.executable, pidlog=str(fleet.pidlog)), encoding="utf-8")
    ack = fleet.send("status? PAYLOAD_S4")
    did = ack["delivery_id"]
    _wait_runner_done(fleet, ack["process_id"])
    assert fleet.until(lambda: fleet.ticket(did)["state"] != "pending", timeout=40.0)
    fleet.tick(4, pause=1.0)
    t = fleet.ticket(did)
    assert (t["state"], t["reason"]) == ("ambiguous", "unknown"), t
    assert len(fleet.attempts(did)) == 1
    assert len(fleet.pidlog.read_text(encoding="utf-8-sig").splitlines()) == 1
    assert fleet.rows(TARGET, "PAYLOAD_S4") == []


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("sibling", ["other_recipient", "real_recipient"])
def test_retarget_with_overlapping_same_task_row(fleet, monkeypatch, sibling):
    """A re-targeted call overlapping an open same-task row opened inside its spawn window, its bind held past its
    completion: no ticket, core's ack byte-for-byte, no plugin attempt to either profile and no notice. Its completion
    is core's byte-for-byte when the row names another recipient, and exactly the unverified rewrite when it names the
    call's real recipient."""
    import threading

    from agent.redact import redact_terminal_output
    from tools.process_registry import _completion_output as core_tail

    _install_rewrite_gate(fleet)
    store = fleet.loaded.module.store
    gate, acks, core_acks, rows = threading.Event(), [], [], []
    real_start = fleet.bot_mode_dm._start_delivery
    monkeypatch.setattr(fleet.bot_mode_dm, "_start_delivery",
                        lambda *a, **k: core_acks.append(real_start(*a, **k)) or core_acks[-1])
    real_spawn = fleet.bot_mode_dm._spawn_delivery

    def stalled(*a, **k):
        rows.append(store.open_inflight(sender_session_id="bc-sibling", task_id=fleet.task_id,
                                        target="sibling-target" if sibling == "other_recipient" else ORCH, baseline=0))
        out = real_spawn(*a, **k)
        assert gate.wait(timeout=120), "spawn gate never released"
        return out

    monkeypatch.setattr(fleet.bot_mode_dm, "_spawn_delivery", stalled)
    sender = threading.Thread(target=lambda: acks.append(fleet.send("status? REWRITE_S1", raw=True)), daemon=True)
    sender.start()
    try:
        assert fleet.until(lambda: _runner_of(fleet) is not None, ticks=False, timeout=30.0)
        session = _runner_of(fleet)
        snapshot = dict(store.inflight(rows[0]))
        assert fleet.until(session._completion_event.is_set, ticks=False, timeout=40.0)
        out = _completion_output(fleet, session.id)
    finally:
        gate.set()
        sender.join(timeout=120)
    ack = json.loads(acks[0])
    assert core_acks and acks[0] == core_acks[-1], (acks, core_acks)
    if sibling == "other_recipient":
        assert out == redact_terminal_output(core_tail(session)["output"], session.command), out
    else:
        expected = json.dumps({"status": "ambiguous", "reason": "unknown", "delivery_id": ack["delivery_id"],
                               "to": f"@{ORCH}", "detail": _UNVERIFIED})
        assert out == redact_terminal_output(expected, session.command), (out, expected)
    assert dict(store.inflight(rows[0])) == snapshot
    fleet.tick(3, pause=0.5)
    assert "redrive" not in ack and fleet.ticket(ack["delivery_id"]) is None, ack
    assert fleet.attempts(ack["delivery_id"]) == []
    assert len(fleet.pidlog.read_text(encoding="utf-8-sig").splitlines()) == 1
    for who in ("default", ORCH):
        assert _notices(fleet, who, ack["delivery_id"]) == [], who


def _prebind_settle(fleet, monkeypatch, body):
    """Stall the sender inside next_call after core spawned the runner; returns (gate, acks, sender, session, did)."""
    import threading

    gate, acks = threading.Event(), []
    real_spawn = fleet.bot_mode_dm._spawn_delivery

    def stalled(*a, **k):
        out = real_spawn(*a, **k)
        assert gate.wait(timeout=120), "spawn gate never released"
        return out

    monkeypatch.setattr(fleet.bot_mode_dm, "_spawn_delivery", stalled)
    sender = threading.Thread(target=lambda: acks.append(fleet.send(body)), daemon=True)
    sender.start()
    assert fleet.until(lambda: _runner_of(fleet) is not None and _admissions(fleet.homes[TARGET], body),
                       ticks=False, timeout=30.0)
    return gate, acks, sender, _runner_of(fleet), _admissions(fleet.homes[TARGET], body)[0]["delivery_id"]


@pytest.mark.platforms("linux")
def test_observed_prebind_runner_still_fails_closed(fleet, live_owner, monkeypatch):
    """A pre-bind runner whose exit a poll observed before its completion reached the transform is still found, so
    its unverified settled fails closed instead of passing raw."""
    import tools.process_registry as registry_mod

    real_transform = registry_mod.transform_process_output

    def observed_first(output, **k):
        if "--run-delivery" in str(k.get("command") or "") and k.get("returncode") is not None:
            for session in list(fleet.registry._finished.values()):
                if session.command == k.get("command"):
                    fleet.registry._poll_observed.add(session.id)
        return real_transform(output, **k)

    monkeypatch.setattr(registry_mod, "transform_process_output", observed_first)
    gate, _acks, sender, session, did = _prebind_settle(fleet, monkeypatch, "status? PAYLOAD_S5_OBSERVED")
    try:
        _settle_without_row(fleet, did)
        assert fleet.until(session._completion_event.is_set, ticks=False, timeout=40.0)
        assert session.id in fleet.registry._poll_observed
        out = _completion_output(fleet, session.id)
        assert _last_json(out).get("status") == "ambiguous" and _UNVERIFIED in out, out
        assert '"status": "settled"' not in out, out
    finally:
        gate.set()
        sender.join(timeout=120)


@pytest.mark.platforms("linux")
def test_published_survives_retention_while_its_send_is_open(fleet, live_owner, monkeypatch):
    """A pre-bind fail-closed publication outlives the retention sweep while its send is still open, so the ticket
    committed after the bind still records that the sender was told the delivery is unverified."""
    drain = fleet.loaded.module.drain
    monkeypatch.setattr(drain, "INFLIGHT_KEEP_SECONDS", 0.0)
    gate, acks, sender, session, did = _prebind_settle(fleet, monkeypatch, "status? PAYLOAD_S6_RETAIN")
    try:
        _settle_without_row(fleet, did)
        assert fleet.until(session._completion_event.is_set, ticks=False, timeout=40.0)
        assert _last_json(_completion_output(fleet, session.id)).get("status") == "ambiguous"
        fleet.tick(3, pause=0.5)                         # every sweep past retention while the send is open
    finally:
        gate.set()
        sender.join(timeout=120)
    assert acks and acks[0]["delivery_id"] == did, acks
    assert fleet.until(lambda: fleet.ticket(did) is not None, ticks=False, timeout=10.0)
    assert fleet.ticket(did)["published_status"] == "ambiguous", fleet.ticket(did)
