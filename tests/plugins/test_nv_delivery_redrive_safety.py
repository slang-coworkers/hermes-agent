"""Safety regression tests for nv-delivery-redrive at v0.22.5: a re-targeted call that core admits elsewhere (the E9
guard), a pre-bind runner, a pre-bind publication across retention, and the linger-carried completion. Each loads the
plugin through the shared ``fleet`` fixture (real discovery, real executor, core's delivery runner, the rig's authority
stub) and asserts one outcome.
"""

from __future__ import annotations

import json
import textwrap

import pytest

from tests.plugins.fleet_f62_h_rig import (
    ORCH,
    SENDER_SID,
    TARGET,
    _admissions,
    _append_user_row,
    _completion_output,
    _notices,
    fleet_fixture,
)
from tests.plugins.test_fleet_f62_h_acceptance import _UNVERIFIED, _last_json, _runner_of, _settle_without_row

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
def test_rewritten_admitted_target_is_not_tracked(fleet):
    """A pre_tool_call modify that re-targets the call to another profile, whose authority admits it, is not ticketed
    under the target the middleware resolved: no ticket and no notice, even once the real recipient settles it."""
    _install_rewrite_gate(fleet)
    out = fleet.send("status? REWRITE_S1")
    assert out.get("delivery_id") and out.get("status") == "queued" and "redrive" not in out, out
    assert out.get("to") == f"@{ORCH}", out
    did = out["delivery_id"]
    assert [r["delivery_id"] for r in _admissions(fleet.homes[ORCH], "REWRITE_S1")] == [did]
    assert _admissions(fleet.homes[TARGET], "REWRITE_S1") == []
    fleet.authority.settle(did, reply="REPLY-ORCH-S1", profile=ORCH)
    fleet.tick(4, pause=0.3)
    assert fleet.ticket(did) is None
    for who in ("default", ORCH):
        assert _notices(fleet, who, did) == [], who
    assert fleet.rows(TARGET, "REWRITE_S1") == []


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
def test_observed_prebind_runner_still_fails_closed(fleet, monkeypatch):
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
def test_published_survives_retention_while_its_send_is_open(fleet, monkeypatch):
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


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("row, decision",
                         [("linger_same_pid", "skip"), ("other_pid", "notice"), ("not_settled", "notice")])
def test_linger_carried_completion_counts_only_for_its_own_settled_pid(fleet, row, decision):
    """(E16) A ``-Q`` one-shot's linger carries core's completion into the sender's Bot Chat as a plain user turn
    (hermes_cli/quiet_single_query.py:192-200). For this ticket's pid with a ``settled`` payload, that row means no
    second sender notice; a row naming another pid, or carrying no ``settled``, still gets the notice."""
    drain = fleet.loaded.module.drain
    did, pid = "e16" * 21 + "0", "proc_e16000000001"
    named = "proc_e16000000002" if row == "other_pid" else pid
    status = "queued" if row == "not_settled" else "settled"
    payload = json.dumps({"status": status, "delivery_id": did, "reply": "ACK"})
    _append_user_row(fleet.root, SENDER_SID, f"[IMPORTANT: Background process {named} completed normally "
                                             f"(exit code 0).\nCommand: x\nOutput:\n{payload}\n]")
    ticket = {"delivery_id": did, "state": "settled", "path": "live", "process_id": pid, "published_status": None,
              "settled_by": 1, "sender_home": str(fleet.root), "sender_session_id": SENDER_SID, "reply": "ACK"}
    assert drain._sender_decision(ticket) == decision
