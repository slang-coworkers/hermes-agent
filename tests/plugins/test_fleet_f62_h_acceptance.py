"""Acceptance test for FLEET-F62.h at v0.22.5: a tracked local delivery gets one truthful final outcome.

Ships as ``tests/plugins/test_fleet_f62_h_acceptance.py``, written against Hermes ``v0.22.5`` = ``bf0203a7``: a local
``message_agent`` DM is admitted by the target profile's session authority (a durable FIFO), never refused busy. The
plugin follows the receipt and verifies the row (D5a); it never re-sends, re-admits, spawns, or cancels an admitted
receipt (UPG-F69 erratum E4′). One ``test_ac_fleet_f62_h_<n>`` per active ``pytest:`` id at T: 4, 5, 7, 8, 9, 12, 13,
14 and 17, plus ``test_ac_upg_f69_7`` (DN-E4-12, E4″). Ids 1, 2, 3, 6 and 11 are WITHDRAWN (premise lost at v0.22.5,
D-6 msg 888 + DN-E4-n msg 1162), id 10 is premise-lost and id 16 stale (DN-E4-6), id 15 is WITHDRAWN (ruling A1): no
test def, ids kept. Id 3's three probe shapes live in id 17.

Loads ``plugins/nv-delivery-redrive`` through the real ``PluginManager().discover_and_load()`` from an isolated
``HERMES_HOME`` with an EMPTY bundled-plugin dir (shape of ``tests/hermes_cli/test_plugin_api_compat.py``). Every send
is a real ``message_agent`` tool call run by a real ``AIAgent``'s sequential executor, so core's whole authorization
(scope, ``pre_tool_call`` + ``modify``, pruned-args, guardrails), the plugin middleware and ``post_tool_call`` all
run. ``message_agent`` itself is core's: ``_start_delivery`` -> ``_admit_live_dm`` -> the authority stub (the rig) ->
real ``terminal_tool(background=True)`` -> core's own ``_wait_live_dm`` runner. The stub writes the target's row only
when a case's owner does. Drainer ticks run through the real ``on_kanban_dispatch_tick`` hook. A gateway restart is a
fresh process registry recovered from the checkpoint plus a freshly loaded plugin manager.

Sender = the default profile (handle ``hermes``, home = the root), target = ``researcher``, orchestrator =
``orchestrator``. Behaviour contract only: no source reading, no network, nothing under ~/.hermes. POSIX only (the
plugin's notice pass serializes on ``fcntl.flock``).
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

from tests.plugins.fleet_f62_h_rig import (  # fleet_fixture registers the fleet fixture
    _admissions,
    _append_user_row,
    _assistant_turn,
    _bot_chat,
    _busy,
    _completion_output,
    fleet_fixture,
    install_runner_shim,
    _make_sender_agent,
    _notices,
    ORCH,
    _queued_completion,
    _REPO,
    _restart_gateway,
    _runner_sessions,
    SENDER_SID,
    TARGET,
    _wait_completion,
    _wait_runner_done,
)

_UNVERIFIED = ("Could not verify your message in the target's Bot Chat; delivery outcome is unknown. Do NOT resend. "
               "Check the target before considering another send.")
_OWNER_SETTLES = "A later outcome will be reported if the owner settles."
_VERIFIABLE = "A later outcome will be reported if the message becomes verifiable."


def _last_json(text):
    try:
        return json.loads((text or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {}


def _append_target_row(fleet, content, *, message_uid=None):
    from hermes_state import SessionDB

    sid, _msgs = _bot_chat(fleet.homes[TARGET])
    db = SessionDB(db_path=fleet.homes[TARGET] / "state.db")
    try:
        return db.append_message(sid, role="user", content=content, message_uid=message_uid)
    finally:
        db.close()


def _settle_without_row(fleet, did, reply="UNPERSISTED-H17", *, unreadable=False):
    """The owner's turn ends `settled` but its user row never landed (tui_gateway/prompt_turn.py:996-1002).
    ``unreadable``: the target transcript is corrupted before the settle, so the plugin's probe is `unknown`."""
    fleet.authority.claim(did)
    if unreadable:
        (fleet.homes[TARGET] / "state.db").write_bytes(b"not a sqlite database" * 64)
    fleet.authority.settle(did, reply=reply, row_first=False)


def _assert_untouched(fleet, did, needle, *, rows):
    """The plugin made no admission, no target row and no spawn: one tool admission for ``did``, one receipt for
    ``needle``, ``rows`` owner rows, and no more core runners than this process's admissions (one each at most)."""
    assert len(fleet.authority.tool_admissions(did)) == 1, fleet.authority.calls(did=did)
    assert [r["delivery_id"] for r in _admissions(fleet.homes[TARGET], needle)] == [did]
    assert len(fleet.rows(TARGET, needle)) == rows, fleet.rows(TARGET, needle)
    admitted = [c for c in fleet.authority.calls() if c["pid"] == os.getpid()]
    assert len(_runner_sessions(fleet)) <= len(admitted), (_runner_sessions(fleet), admitted)


def _finals(fleet, did):
    return [n for n in fleet.loaded.module.store.notices(did) if n["notice_kind"] == "final"]


def _lates(fleet, did):
    return [n for n in fleet.loaded.module.store.notices(did) if n["notice_kind"] in ("late", "verified")]


def _assert_one_each(fleet, did, *, late=None):
    """One sender and one orchestrator FINAL in the transcripts; ``late`` = the number of late notices each (≤ 1)."""
    assert fleet.until(lambda: len(_notices(fleet, "default", did)) >= 1 and len(_notices(fleet, ORCH, did)) >= 1,
                       timeout=30.0)
    fleet.tick(3, pause=0.3)
    expect = 1 + (late or 0)
    assert len(_notices(fleet, "default", did)) == expect and len(_notices(fleet, ORCH, did)) == expect, (
        _notices(fleet, "default", did), _notices(fleet, ORCH, did))


# --------------------------------------------------------------------------- #
# Followed receipts: an unreadable transcript, a one-shot sender, core's authorization
# --------------------------------------------------------------------------- #
@pytest.mark.platforms("linux")
@pytest.mark.parametrize("case", ["corrupt_db", "vanished_db"])
def test_ac_fleet_f62_h_4(fleet, case):
    """A tracked delivery whose target transcript cannot be read when its receipt settles is never reported `settled`: it ends `ambiguous`/`unknown`, unverified, with the D-3 FINAL text. This covers a corrupt db, and a db that existed at dispatch and then disappeared."""
    ack = fleet.send("status? PAYLOAD_H4")
    did = ack["delivery_id"]
    assert ack.get("redrive", {}).get("tracked") is True, ack
    fleet.authority.claim(did)
    db = fleet.homes[TARGET] / "state.db"
    if case == "corrupt_db":
        db.write_bytes(b"not a sqlite database" * 64)
    else:
        for leftover in fleet.homes[TARGET].glob("state.db*"):
            leftover.unlink()                              # an observed db that disappeared is unknown, not absent
    fleet.authority.settle(did, reply="REPLY-H4", row_first=False)
    assert fleet.until(lambda: fleet.ticket(did)["state"] != "pending", timeout=60.0), fleet.ticket(did)
    t = fleet.ticket(did)
    assert (t["state"], t["reason"], t["unverified"], t["matched_row_id"]) == ("ambiguous", "unknown", 1, None), t
    _assert_one_each(fleet, did, late=0)
    for who in ("default", ORCH):
        body = str(_notices(fleet, who, did)[0]["content"])
        assert body == f"Delivery {did} to @{TARGET}: {_UNVERIFIED} {_VERIFIABLE}", (who, body)
    assert _lates(fleet, did) == []
    assert len(fleet.authority.tool_admissions(did)) == 1


_ONESHOT_SENDER = textwrap.dedent('''\
    import json, sys
    sys.path.insert(0, {repo!r})
    sys.path.insert(0, {testdir!r})
    from pathlib import Path
    from hermes_cli import plugins as plugins_mod
    from tools import bot_mode_dm
    from tests.plugins.fleet_f62_h_rig import install_authority
    install_authority({state_dir!r})
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
        state_dir=str(fleet.state_dir), dm_dir=str(fleet.dm_dir), shim=str(fleet.shim), body=body,
        task=fleet.task_id), encoding="utf-8")
    return subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", env={**os.environ, "HERMES_HOME": str(fleet.root)})


def _ack_of(proc):
    out, err = proc.communicate(timeout=180)
    assert proc.returncode == 0, err[-2000:]
    return json.loads(out.strip().splitlines()[-1])


@pytest.mark.platforms("linux")
def test_ac_fleet_f62_h_5(fleet, tmp_path):
    """A one-shot sender that exits while its delivery is still queued behind a busy target's turn leaves the delivery followed by the gateway drainer. When the owner settles it with this ticket's claimed row, the target's reply reaches the sender's Bot Chat as one delivery notice naming the `delivery_id`."""
    ack = _ack_of(_oneshot_send(fleet, tmp_path, "status? PAYLOAD_H5"))
    did = ack["delivery_id"]
    assert ack.get("status") == "queued" and ack.get("redrive", {}).get("tracked") is True, ack
    fleet.tick(3, pause=0.5)                              # the sender process is gone; the target's turn still runs
    assert fleet.rows(TARGET, "PAYLOAD_H5") == [] and fleet.ticket(did)["state"] == "pending"
    fleet.authority.settle(did, reply="REPLY-H5")
    assert fleet.until(lambda: fleet.ticket(did)["state"] == "settled", timeout=60.0), fleet.ticket(did)
    assert fleet.until(lambda: len(_notices(fleet, "default", did)) == 1, timeout=30.0)
    fleet.tick(3, pause=0.3)
    notes = _notices(fleet, "default", did)
    assert len(notes) == 1 and did in str(notes[0]["content"]) and "REPLY-H5" in str(notes[0]["content"]), notes
    assert [r["delivery_id"] for r in _admissions(fleet.homes[TARGET], "PAYLOAD_H5")] == [did]
    assert len(fleet.rows(TARGET, "PAYLOAD_H5")) == 1


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


def _enable_gate(fleet, name="f62h-test-gate", yaml_text=_GATE_YAML, init=_GATE_INIT):
    gate = fleet.root / "plugins" / name
    gate.mkdir(parents=True)
    (gate / "plugin.yaml").write_text(yaml_text, encoding="utf-8")
    (gate / "__init__.py").write_text(init, encoding="utf-8")
    cfg = yaml.safe_load((fleet.root / "config.yaml").read_text(encoding="utf-8-sig"))
    cfg["plugins"]["enabled"].append(name)
    (fleet.root / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    fleet.load()
    fleet.tick()


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("case", ["hook_block", "guardrail_block", "hook_modify"])
def test_ac_fleet_f62_h_7(fleet, case, monkeypatch):
    """Every message_agent call, including one sent while an earlier ticket for the pair is still open, goes through core's complete authorization on the real executor. A pre_tool_call block and a guardrail block each yield no ticket and no row. A pre_tool_call modify of the message is what gets ticketed and delivered."""
    from agent.tool_guardrails import ToolGuardrailDecision

    _enable_gate(fleet)
    first = fleet.send("status? PAYLOAD_H7_FIRST")       # an open ticket for the pair: an admitted, queued receipt
    assert fleet.ticket(first["delivery_id"])["state"] == "pending"
    assert fleet.authority.receipt(first["delivery_id"])["status"] == "queued"
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
        assert "MODIFIED_H7" in fleet.ticket(out["delivery_id"])["content"]
        fleet.authority.settle(out["delivery_id"], reply="REPLY-H7-MOD")
    fleet.authority.settle(first["delivery_id"], reply="REPLY-H7")
    assert fleet.until(lambda: fleet.ticket(first["delivery_id"])["state"] == "settled", timeout=60.0)
    if case == "hook_modify":
        assert fleet.until(lambda: fleet.ticket(out["delivery_id"])["state"] == "settled", timeout=60.0)
        assert len(fleet.rows(TARGET, "MODIFIED_H7")) == 1
        assert fleet.rows(TARGET, "MODIFY_ME_H7") == []
    else:
        fleet.tick(3, pause=0.3)
        assert fleet.rows(TARGET, "FORBIDDEN_H7") == [] and fleet.rows(TARGET, "GUARDED_H7") == []
        assert _admissions(fleet.homes[TARGET], "FORBIDDEN_H7") == _admissions(fleet.homes[TARGET], "GUARDED_H7") == []
        assert len(fleet.loaded.module.store.tickets_for_pair(str(fleet.root), str(fleet.homes[TARGET]))) == 1
    assert len(fleet.rows(TARGET, "PAYLOAD_H7_FIRST")) == 1


# --------------------------------------------------------------------------- #
# Live-owner path (R4 condition 3, ruling B1)
# --------------------------------------------------------------------------- #
_H8_WINDOWS = {"claimed_owner_gone": 8, "admitted_unknown": 8, "legacy_expired": 8}
_H8_LANDS = ("settled_later", "no_runner", "pending_restart", "runner_killed_after_admission")
_H8_LEGACY_TICKET = ("v0222_cli_ticket", "legacy_cli_bounded_row_watch", "legacy_cli_open_attempt_identical_later",
                     "legacy_cli_settled_undecided_notice")
_H8_FINAL_UNKNOWN = ("Delivery {did} to @" + TARGET + " outcome unknown [reason: unknown]: the live owner has not reported "
                     "this delivery's outcome. Do NOT resend; check their Bot Chat before resending. " + _OWNER_SETTLES)


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("case", ["settled_later", "failed_later", "claimed_owner_gone", "no_runner", "legacy_cancelled",
                                  "admitted_unknown", "legacy_expired", "pending_restart",
                                  "runner_killed_after_admission", *_H8_LEGACY_TICKET])
def test_ac_fleet_f62_h_8(fleet, case, monkeypatch):
    """A live-owner delivery whose core runner ended, never started, or was killed while its receipt was still `queued` or `claimed` is followed by the plugin past the runner. Its final outcome is reported to the sender and the orchestrator. The plugin NEVER re-sends, re-admits, or cancels an admitted receipt: no target turn, no new admission, no target row from the plugin. Cases: (a) The owner settles later: `settled` only once the target's Bot Chat holds the full `role='user'` row newer than the ticket's baseline and claimed by this ticket (D5a); otherwise `ambiguous`/`unknown`. The reply reaches the sender. (b) It fails later: `failed` with the receipt's typed reason. (c) It is claimed and the owner goes away: never re-sent, `ambiguous`/`unknown` at the bound. (d) An ack with `notification_error` and no runner is tracked from its receipt. (e) Core's runner cancels an unadmitted legacy record whose owner is provably gone (`runtime_offline`): reported `cancelled` with that reason, never re-sent. (f) An admitted receipt still `queued` at the bound is not cancelled. It ends `unknown` (`ambiguous`/`unknown`) with the D-3 FINAL text, and at most one late notice follows. An unadmitted legacy record still `queued` at the bound is cancelled by the plugin and reported `failed`/`queued_expired`, with no row. (g) Core's pending completion ("Delivery remains pending …") reached the sender, the gateway restarts, then the receipt settles: exactly one plugin sender notice with the reply. (h) The runner is killed after core's admission and before it reports an outcome: the drainer follows the receipt and never re-admits it. (i) A pending `path='cli'` ticket persisted by the v0.22.2 plugin is followed from its receipt, or ends `ambiguous`/`unknown` when it has none. A closed one with `late_open=1` follows DN-E4-11: it gets one `LATE` `settled` only for a row in a bounded stored window that it alone claims; otherwise its FINAL stands. A closed, `settled` one whose notice was undecided at the upgrade gets exactly one sender decision, as at H. Nothing is spawned."""
    from tools.bot_failure_reasons import RUNTIME_OFFLINE
    from tools.process_registry_notifications import format_process_notification
    import tools.terminal_tool as terminal_module

    if case in _H8_LEGACY_TICKET:
        _h8_legacy(fleet, case)
        return
    home = fleet.homes[TARGET]
    if case in _H8_WINDOWS:
        fleet.settings(redrive_window_seconds=_H8_WINDOWS[case])
    legacy = case in ("legacy_cancelled", "legacy_expired")
    if legacy:
        fleet.authority.mode("legacy")                   # a queued record with no admission_id
    if case == "no_runner":
        monkeypatch.setattr(terminal_module, "terminal_tool",
                            lambda *a, **k: json.dumps({"error": "spawn refused (test)"}))
    elif case in ("pending_restart", "legacy_cancelled"):
        # Core's runner gives up only after 30 min, re-checking its owner once per 300 s window; both shortened inside
        # the runner process (read by the shim) so core's own outcome arrives within the test.
        bounds = {"_LIVE_WAIT_SECONDS": 0.2, "_LIVE_WAIT_MAX_SECONDS": 0.5 if case == "pending_restart" else 20.0}
        (fleet.state_dir / "runner.json").write_text(json.dumps(bounds), encoding="utf-8")
    elif case == "runner_killed_after_admission":
        (fleet.state_dir / "runner.gate").write_text("", encoding="utf-8")   # the runner never reaches core's code
    ack = fleet.send("status? PAYLOAD_H8")
    did = ack["delivery_id"]
    assert ack.get("redrive", {}).get("tracked") is True and fleet.ticket(did)["path"] == "live", ack
    receipt = fleet.authority.receipt(did)
    assert receipt["status"] == "queued" and bool(receipt.get("admission_id")) is not legacy, receipt
    pending = None
    if case == "no_runner":
        assert "process_id" not in ack and ack.get("notification_error"), ack
        assert _runner_sessions(fleet) == []
    elif case == "pending_restart":
        _wait_runner_done(fleet, ack["process_id"])
        _wait_completion(fleet, ack["process_id"])
        assert "Delivery remains pending" in _completion_output(fleet, ack["process_id"])
        pending = _queued_completion(fleet, ack["process_id"])
    elif case == "legacy_cancelled":
        for leftover in home.glob("state.db*"):
            leftover.unlink()                            # the owner is provably gone: no store to own the Bot Chat
        _wait_runner_done(fleet, ack["process_id"], timeout=30.0)
        assert fleet.until(fleet.registry.get(ack["process_id"])._completion_event.is_set, ticks=False, timeout=30.0)
        out = json.loads(_completion_output(fleet, ack["process_id"]))
        assert (out.get("status"), out.get("reason")) == ("cancelled", RUNTIME_OFFLINE), out
    else:
        if case == "claimed_owner_gone":
            fleet.authority.claim(did)
        # The runner ends without printing an outcome: a stand-in for its 30-min bound, or for a crash.
        fleet.registry.kill_process(ack["process_id"], consume_output=True)
        _wait_runner_done(fleet, ack["process_id"])
        if case == "runner_killed_after_admission":
            (fleet.state_dir / "runner.gate").unlink()
            assert [c["pid"] for c in fleet.authority.calls(did=did)] == [os.getpid()]   # the runner never ran
    if case != "legacy_cancelled":
        fleet.tick(2, pause=0.3)
        assert fleet.ticket(did)["state"] == "pending"
    _h8_finish(fleet, case, did, ack, pending, monkeypatch, format_process_notification)
    late = 1 if case == "admitted_unknown" else 0
    _assert_one_each(fleet, did, late=late)
    assert len(_lates(fleet, did)) == 2 * late
    landed = case in _H8_LANDS or case == "admitted_unknown"
    _assert_untouched(fleet, did, "PAYLOAD_H8", rows=1 if landed else 0)
    if landed:
        assert fleet.ticket(did)["matched_row_id"] is not None, fleet.ticket(did)


def _h8_settled(fleet, case, did, ack, pending, monkeypatch, fmt):
    fleet.authority.settle(did, reply="LIVE-REPLY-H8")
    assert fleet.until(lambda: fleet.ticket(did)["state"] == "settled"), fleet.ticket(did)
    assert fleet.until(lambda: len(_notices(fleet, "default", did)) == 1, timeout=30.0)
    assert "LIVE-REPLY-H8" in str(_notices(fleet, "default", did)[0]["content"])


def _h8_failed(fleet, case, did, ack, pending, monkeypatch, fmt):
    fleet.authority.fail(did, "provider_rate_limit", "429")
    assert fleet.until(lambda: fleet.ticket(did)["state"] == "failed"), fleet.ticket(did)
    assert fleet.ticket(did)["reason"] == "provider_rate_limit", fleet.ticket(did)


def _h8_legacy_cancelled(fleet, case, did, ack, pending, monkeypatch, fmt):
    from tools.bot_failure_reasons import RUNTIME_OFFLINE

    assert fleet.until(lambda: fleet.ticket(did)["state"] == "cancelled"), fleet.ticket(did)
    assert fleet.ticket(did)["reason"] == RUNTIME_OFFLINE, fleet.ticket(did)


def _h8_pending_restart(fleet, case, did, ack, pending, monkeypatch, fmt):
    # Before it went down, the gateway injected core's pending completion into the sender's Bot Chat.
    _append_user_row(fleet.root, SENDER_SID,
                     fmt({"type": "completion", "session_id": ack["process_id"], **pending}),
                     display_kind="process_complete", display_metadata={"display_text": "x"})
    _restart_gateway(fleet, monkeypatch)
    fleet.authority.settle(did, reply="LATE-REPLY-H8")
    assert fleet.until(lambda: fleet.ticket(did)["state"] == "settled"), fleet.ticket(did)
    assert fleet.until(lambda: len(_notices(fleet, "default", did)) == 1)
    assert "LATE-REPLY-H8" in str(_notices(fleet, "default", did)[0]["content"])


def _h8_legacy_expired(fleet, case, did, ack, pending, monkeypatch, fmt):
    assert fleet.until(lambda: fleet.ticket(did)["state"] != "pending", timeout=60.0), fleet.ticket(did)
    assert (fleet.ticket(did)["state"], fleet.ticket(did)["reason"]) == ("failed", "queued_expired")
    receipt = fleet.authority.receipt(did)
    assert (receipt["status"], receipt["reason"]) == ("cancelled", "queued_expired"), receipt


def _h8_unknown_at_bound(fleet, case, did, ack, pending, monkeypatch, fmt):
    """claimed_owner_gone, admitted_unknown: an admitted receipt is never cancelled."""
    assert fleet.until(lambda: fleet.ticket(did)["state"] != "pending", timeout=60.0), fleet.ticket(did)
    t = fleet.ticket(did)
    assert (t["state"], t["reason"], t["late_open"]) == ("ambiguous", "unknown", 1), t
    receipt = fleet.authority.receipt(did)
    assert receipt["status"] == ("claimed" if case == "claimed_owner_gone" else "queued"), receipt
    assert fleet.until(lambda: len(_notices(fleet, "default", did)) == 1 and len(_notices(fleet, ORCH, did)) == 1)
    for who in ("default", ORCH):
        assert str(_notices(fleet, who, did)[0]["content"]) == _H8_FINAL_UNKNOWN.format(did=did)
    if case == "admitted_unknown":
        fleet.authority.settle(did, reply="LATE-H8")          # the owner settles after the bound: one LATE
        assert fleet.until(lambda: fleet.ticket(did)["late_open"] == 0, timeout=30.0), fleet.ticket(did)
        assert fleet.until(lambda: len(_notices(fleet, "default", did)) == 2, timeout=30.0)
        assert "Late outcome: delivery" in str(_notices(fleet, "default", did)[-1]["content"])


_H8_FINISH = {"settled_later": _h8_settled, "no_runner": _h8_settled, "runner_killed_after_admission": _h8_settled,
              "failed_later": _h8_failed, "legacy_cancelled": _h8_legacy_cancelled,
              "pending_restart": _h8_pending_restart, "legacy_expired": _h8_legacy_expired,
              "claimed_owner_gone": _h8_unknown_at_bound, "admitted_unknown": _h8_unknown_at_bound}


def _h8_finish(fleet, case, did, ack, pending, monkeypatch, format_process_notification):
    _H8_FINISH[case](fleet, case, did, ack, pending, monkeypatch, format_process_notification)


def _legacy_ticket(fleet, did, body, *, state, attempt, **fields):
    """A ticket as the v0.22.2 plugin persisted it (``path='cli'``), in the plugin's own db."""
    store, send = fleet.loaded.module.store, fleet.loaded.module.send
    now = time.time()
    row = {"delivery_id": did, "kind": "message", "sender": "default", "target": TARGET,
           "target_home": str(fleet.homes[TARGET]), "sender_home": str(fleet.root), "sender_session_id": SENDER_SID,
           "author_json": json.dumps({"id": "bot:default", "name": "hermes", "is_bot": True}),
           "content": send.build_content("default", fleet.root, body), "content_verified": 1, "path": "cli",
           "state": state, "created_at": now - 60, "deadline": now + 3600, "db_seen": 1,
           "resolved_at": None if state == "pending" else now, **fields}
    store.commit_ticket(row, {"dm_path": None, "spawned_at": now - 60, **attempt})
    return row["content"]


def _target_max_row(fleet):
    return fleet.loaded.module.probe.max_row(fleet.homes[TARGET], db_seen=True)[1]


def _append_target_reply(fleet, text):
    from hermes_state import SessionDB

    sid, _msgs = _bot_chat(fleet.homes[TARGET])
    db = SessionDB(db_path=fleet.homes[TARGET] / "state.db")
    try:
        return db.append_message(sid, role="assistant", content=text)
    finally:
        db.close()


def _h8_legacy(fleet, case):
    """(i): tickets the v0.22.2 plugin persisted, met by the first ticks at v0.22.5."""
    store = fleet.loaded.module.store
    if case == "legacy_cli_settled_undecided_notice":
        for sub, output in (("refused", '{"reason": "target_busy", "error": "busy"}'), ("carried", "REPLY-H8-CARRIED")):
            session = fleet.registry.spawn_local(f"printf '%s\\n' {shlex.quote(output)}", task_id=fleet.task_id,
                                                 owner_task_id=fleet.task_id, notify_on_complete=True)
            _wait_runner_done(fleet, session.id)
            _wait_completion(fleet, session.id)
            did = (sub[0] * 64)[:64]
            _legacy_ticket(fleet, did, f"status? PAYLOAD_H8_{sub.upper()}", state="settled", process_id=session.id,
                           settled_by=1, reply="REPLY-H8-CARRIED", attempt={"n": 1, "state": "over"})
            fleet.tick(3, pause=0.3)
            sender, orch = _notices(fleet, "default", did), _notices(fleet, ORCH, did)
            # H's decision: a clean refusal was not the outcome, so one sender FINAL; settled never reaches the orch.
            assert (len(sender), len(orch)) == ((1, 0) if sub == "refused" else (0, 0)), (sub, sender, orch)
            assert len(_finals(fleet, did)) == (1 if sub == "refused" else 0)
        return
    if case == "v0222_cli_ticket":
        ack = fleet.send("status? PAYLOAD_H8")             # a v0.22.2 ticket whose delivery has a receipt
        did = ack["delivery_id"]
        store.update_ticket(did, path="cli")
        fleet.registry.kill_process(ack["process_id"], consume_output=True)   # its runner did not survive the upgrade
        _wait_runner_done(fleet, ack["process_id"])
        orphan = "e" * 64                                  # and one with none
        _legacy_ticket(fleet, orphan, "status? PAYLOAD_H8_NORECEIPT", state="pending",
                       attempt={"n": 1, "state": "over", "baseline": _target_max_row(fleet), "over_row": None})
        fleet.tick(2, pause=0.3)
        assert fleet.ticket(did)["path"] == "live" and fleet.ticket(did)["state"] == "pending", fleet.ticket(did)
        t = fleet.ticket(orphan)
        assert (t["state"], t["reason"], t["late_open"]) == ("ambiguous", "unknown", 0), t
        assert t["error"] == "pending across the v0.22.5 upgrade; this version does not re-drive"
        fleet.authority.settle(did, reply="LIVE-REPLY-H8")
        assert fleet.until(lambda: fleet.ticket(did)["state"] == "settled"), fleet.ticket(did)
        _assert_one_each(fleet, did, late=0)
        _assert_one_each(fleet, orphan, late=0)
        assert _OWNER_SETTLES not in str(_notices(fleet, "default", orphan)[0]["content"])
        assert fleet.authority.calls(did=orphan) == []
        _assert_untouched(fleet, did, "status? PAYLOAD_H8", rows=1)
        return
    body = "status? PAYLOAD_H8_WATCH" if case == "legacy_cli_bounded_row_watch" else "status? PAYLOAD_H8_IDENT"
    did = "d" * 64
    base = _target_max_row(fleet)
    if case == "legacy_cli_bounded_row_watch":
        # The row landed inside the v0.22.2 attempt's bounded window, unreadable when that plugin last looked.
        content = fleet.loaded.module.send.build_content("default", fleet.root, body)
        row = _append_target_row(fleet, content)
        _append_target_reply(fleet, "ACK-H8-WATCH")
        attempt = {"n": 1, "state": "over", "baseline": base, "over_row": _target_max_row(fleet) - 1}
    else:
        attempt = {"n": 1, "state": "running", "baseline": base}   # still open at the upgrade: hi = INF
    _legacy_ticket(fleet, did, body, state="ambiguous", reason="unknown", late_open=1,
                   error="an attempt may still land or its row is unproven", attempt=attempt)
    fleet.tick(3, pause=0.3)
    t = fleet.ticket(did)
    assert t["late_open"] == 0, t
    if case == "legacy_cli_bounded_row_watch":
        assert t["matched_row_id"] == row, t
        _assert_one_each(fleet, did, late=1)
        late = str(_notices(fleet, "default", did)[-1]["content"])
        assert late.startswith(f"Late outcome: delivery {did} to @{TARGET} settled") and "ACK-H8-WATCH" in late, late
        return
    _assert_one_each(fleet, did, late=0)
    fleet.settings(redrive_window_seconds=8)
    ack = fleet.send(body)                                 # an identical later message lands
    fleet.authority.settle(ack["delivery_id"], reply="REPLY-H8-IDENT")
    # The legacy attempt may still have written that row, so neither ticket can claim it: the later one ends unknown.
    assert fleet.until(lambda: fleet.ticket(ack["delivery_id"])["state"] != "pending", timeout=60.0)
    later = fleet.ticket(ack["delivery_id"])
    assert (later["state"], later["reason"], later["matched_row_id"]) == ("ambiguous", "unknown", None), later
    assert len(fleet.rows(TARGET, "PAYLOAD_H8_IDENT")) == 1
    fleet.tick(3, pause=0.3)
    assert fleet.ticket(did)["matched_row_id"] is None and _lates(fleet, did) == [], fleet.ticket(did)
    assert len(_notices(fleet, "default", did)) == 1 and len(_notices(fleet, ORCH, did)) == 1


# --------------------------------------------------------------------------- #
# Notices, restart, the idle regression
# --------------------------------------------------------------------------- #
@pytest.mark.platforms("linux")
@pytest.mark.parametrize("case", ["normal", "busy_orchestrator", "orch_no_bot_chat", "restart_after_append"])
def test_ac_fleet_f62_h_9(fleet, case, monkeypatch):
    """A typed final failure reaches the sender's originating session (its Bot Chat tip) and the orchestrator profile's Bot Chat as exactly one process_complete notice row each. Each row names the delivery_id, the target and the closed-vocabulary reason. This holds when the orchestrator is busy, when it has no Bot Chat yet, and when the drainer restarts between the append and its bookkeeping."""
    if case == "orch_no_bot_chat":
        for leftover in fleet.homes[ORCH].glob("state.db*"):
            leftover.unlink()
    orch_lock = _busy(fleet, ORCH) if case == "busy_orchestrator" else None
    if orch_lock is not None:
        orch_lock.__enter__()
    try:
        ack = fleet.send("status? PAYLOAD_H9")
        did = ack["delivery_id"]
        if case == "restart_after_append":
            store = fleet.loaded.module.store
            real_mark = store.mark_notice_appended
            monkeypatch.setattr(store, "mark_notice_appended",
                                lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash before mark")))
        fleet.authority.fail(did, "provider_rate_limit", "429 from the provider")
        assert fleet.until(lambda: fleet.ticket(did)["state"] == "failed", timeout=60.0), fleet.ticket(did)
        fleet.tick(2, pause=0.3)
        if case == "restart_after_append":
            monkeypatch.setattr(store, "mark_notice_appended", real_mark)
            fleet.load()
            fleet.tick(3, pause=0.3)
    finally:
        if orch_lock is not None:
            orch_lock.__exit__(None, None, None)
    assert fleet.until(lambda: len(_notices(fleet, "default", did)) >= 1 and len(_notices(fleet, ORCH, did)) >= 1,
                       timeout=30.0)
    fleet.tick(3, pause=0.3)
    for who in ("default", ORCH):
        notes = _notices(fleet, who, did)
        assert len(notes) == 1, (who, notes)
        text = str(notes[0]["content"])
        assert did in text and TARGET in text and "provider_rate_limit" in text, text


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("case", ["idle", "settle_during_runner", "transform_missed", "publish_lost_after_persist",
                                  "detached_pruned"])
def test_ac_fleet_f62_h_12(fleet, case, monkeypatch):
    """(regression for idle, settle_during_runner and transform_missed; publish_lost_after_persist and detached_pruned need the plugin's (b′) notice) A delivery to an idle target behaves as on 0.22: the ack keeps core's status, delivery_id, process_id and reply_delivery values, the reply completion text is unchanged, the message lands once, and no notice is written. One sender notice, never two (E1 F5): no plugin notice when a live receipt settles while core's runner is completing and a drainer tick lands between core's exited mark and its output transform, or when the plugin's transform records nothing. When core's publication fails after its result write, no plugin notice is written in-process (the documented residual), and exactly one after a gateway restart. When a crash leaves the attempt-1 runner to be recovered as a detached session that core later prunes (_completion_event set, no completion queued), exactly one plugin sender notice is written on that restarted gateway."""
    import re
    import threading

    from tools import process_registry as pr_mod

    real_redact = pr_mod._redact_process_result
    if case == "transform_missed":
        # The plugin's transform records nothing for this runner (timed out / skipped); core still publishes the
        # completion, and the one-notice rule keys on _completion_event, not on the transform.
        fleet.settings(heartbeat_stale_seconds=2)
        monkeypatch.setattr(fleet.loaded.module.send, "reported_success", lambda *a, **k: False)
    elif case == "publish_lost_after_persist":
        # Core persists the runner's result, then its publication raises before completion_queue.put.
        def lost(result):
            if (isinstance(result, dict) and result.get("type") == "completion"
                    and "--run-delivery" in str(result.get("command") or "")):
                raise RuntimeError("gateway died before publishing")
            return real_redact(result)
        monkeypatch.setattr(pr_mod, "_redact_process_result", lost)
    ack = fleet.send("status? PAYLOAD_H12")
    did, pid = ack["delivery_id"], ack["process_id"]
    assert ack.get("status") == "queued" and ack.get("reply_delivery") == "notification", ack
    assert re.fullmatch(r"[0-9a-f]{64}", did or ""), ack
    session = fleet.registry.get(pid)
    assert session is not None and "--run-delivery" in session.command
    if case == "settle_during_runner":
        # Core marks the session exited before it publishes the completion and runs the output transform
        # (tools/process_registry.py). Pause the completion inside that window and tick there.
        in_window, release = threading.Event(), threading.Event()

        def paused(result):
            if isinstance(result, dict) and result.get("session_id") == pid:
                in_window.set()
                release.wait(timeout=30)
            return real_redact(result)
        monkeypatch.setattr(pr_mod, "_redact_process_result", paused)
        try:
            fleet.authority.settle(did, reply="REPLY-FROM-researcher")
            assert in_window.wait(timeout=40), "the runner's completion never reached the transform"
            assert session.exited
            fleet.tick(3, pause=0.3)                     # exited + settled receipt, transform not yet run
            assert _notices(fleet, "default", did) == [], "exited alone must not authorize a sender notice"
        finally:
            release.set()
        assert fleet.until(lambda: session._completion_event.is_set(), ticks=False, timeout=30.0)
    elif case == "detached_pruned":
        # The next gateway process recovers the live runner from the checkpoint as a detached session with no pipe.
        # Once its PID is gone core prunes it, sets _completion_event and queues no completion: only the plugin can
        # tell the sender.
        assert _restart_gateway(fleet, monkeypatch, crash=True) == 1
        recovered = fleet.registry._running.get(pid)
        assert recovered is not None and recovered.detached and not recovered._completion_event.is_set()
        fleet.authority.settle(did, reply="REPLY-FROM-researcher")
        assert fleet.until(lambda: fleet.registry.get(pid) is None, ticks=False, timeout=40.0)
        assert recovered._completion_event.is_set() and fleet.registry.completion_queue.empty()
        assert fleet.until(lambda: len(_notices(fleet, "default", did)) == 1, timeout=30.0)
        fleet.tick(3, pause=0.3)
        notes = _notices(fleet, "default", did)
        assert len(notes) == 1 and "REPLY-FROM-researcher" in str(notes[0]["content"]), notes
        assert len(fleet.rows(TARGET, "PAYLOAD_H12")) == 1
        return
    else:
        fleet.authority.settle(did, reply="REPLY-FROM-researcher")
        _wait_runner_done(fleet, pid, timeout=40.0)
        assert fleet.until(session._completion_event.is_set, ticks=False, timeout=30.0)
    if case == "publish_lost_after_persist":
        assert (fleet.root / "logs" / "process-results" / f"{pid}.json").exists()
        fleet.tick(3, pause=0.3)
        assert _notices(fleet, "default", did) == []     # the documented in-process residual
        while not fleet.registry.completion_queue.empty():
            fleet.registry.completion_queue.get_nowait()
        _restart_gateway(fleet, monkeypatch)
        assert fleet.until(lambda: len(_notices(fleet, "default", did)) == 1, timeout=30.0)
        fleet.tick(3, pause=0.3)
        notes = _notices(fleet, "default", did)
        assert len(notes) == 1 and "REPLY-FROM-researcher" in str(notes[0]["content"]), notes
        assert len(fleet.rows(TARGET, "PAYLOAD_H12")) == 1
        return
    if case == "transform_missed":
        fleet.tick(4, pause=1.0)                         # well past heartbeat_stale_seconds
    assert fleet.until(lambda: fleet.ticket(did)["state"] == "settled", timeout=30.0), fleet.ticket(did)
    assert len(fleet.rows(TARGET, "PAYLOAD_H12")) == 1
    assert "REPLY-FROM-researcher" in _completion_output(fleet, pid)
    fleet.tick(3, pause=0.3)
    assert _notices(fleet, "default", did) == []
    assert _notices(fleet, ORCH, did) == []


def _to_unknown_at_bound(fleet, body, *, claim):
    """A tracked delivery whose receipt is still open (claimed or admitted-queued) when its bound passes."""
    ack = fleet.send(body)
    did = ack["delivery_id"]
    if claim:
        fleet.authority.claim(did)
    fleet.registry.kill_process(ack["process_id"], consume_output=True)   # core's runner is past its own bound
    _wait_runner_done(fleet, ack["process_id"])
    assert fleet.until(lambda: fleet.ticket(did)["state"] != "pending", timeout=40.0), fleet.ticket(did)
    t = fleet.ticket(did)
    assert (t["state"], t["reason"], t["late_open"], t["final_suffix"]) == ("ambiguous", "unknown", 1,
                                                                         "owner_settles"), t
    return did


def _kinds_in_order(fleet, who, did):
    return [(n.get("display_metadata") or {})["nv_delivery_redrive"]["notice_kind"] for n in _notices(fleet, who, did)]


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("case", ["live_claimed", "admitted_queued_at_bound", "late_settled_no_row",
                                  "settle_before_first_notice_pass", "failed_notice_pass", "overlapping_ticks"])
def test_ac_fleet_f62_h_13(fleet, case, monkeypatch):
    """(derived) At the bound, a tracked delivery that may still land is never reported as a failure: a live receipt still `claimed`, or admitted and still `queued`, ends `ambiguous`/`unknown` with its notices and the D-3 FINAL text. The ticket survives a retention interval while that receipt is open, and when it resolves, at most one late-outcome notice follows (§E4.2 item 6)."""
    fleet.settings(redrive_window_seconds=8, late_verify_seconds=6)
    drain, notify = fleet.loaded.module.drain, fleet.loaded.module.notify
    if case in ("live_claimed", "admitted_queued_at_bound"):
        fleet.settings(retention_seconds=1)
        did = _to_unknown_at_bound(fleet, "status? PAYLOAD_H13", claim=case == "live_claimed")
        assert fleet.authority.receipt(did)["status"] == ("claimed" if case == "live_claimed" else "queued")
        _assert_one_each(fleet, did, late=0)
        for who in ("default", ORCH):
            assert str(_notices(fleet, who, did)[0]["content"]) == _H8_FINAL_UNKNOWN.format(did=did)
        time.sleep(2.0)  # health: allow HX013 -- AC-13 waits out retention_seconds=1 before the GC tick that must keep the ticket
        fleet.tick(2, pause=0.3)
        assert fleet.ticket(did) is not None and fleet.ticket(did)["late_open"] == 1
        fleet.settings(retention_seconds=3600)
        fleet.authority.settle(did, reply="LATE-H13")
        assert fleet.until(lambda: fleet.ticket(did)["late_open"] == 0, timeout=30.0), fleet.ticket(did)
        _assert_one_each(fleet, did, late=1)
        assert "LATE-H13" in str(_notices(fleet, "default", did)[-1]["content"])
        return
    if case == "late_settled_no_row":
        # DN-E4-9 (1): settled with no row → a 6 s verify window. A row inside it → one VERIFIED; none → one LATE.
        verified = _to_unknown_at_bound(fleet, "status? PAYLOAD_H13_ROW", claim=True)
        never = _to_unknown_at_bound(fleet, "status? PAYLOAD_H13_NOROW", claim=True)
        before_window = dict(fleet.ticket(verified))                           # a tick's snapshot from before it
        for did in (verified, never):
            fleet.authority.settle(did, reply="REPLY-H13", row_first=False)
        assert fleet.until(lambda: all(fleet.ticket(d)["late_verify_until"] for d in (verified, never)), timeout=30.0)
        until = fleet.ticket(verified)["late_verify_until"]
        fleet.tick(2, pause=0.3)
        assert fleet.ticket(verified)["late_verify_until"] == until            # set once
        assert _lates(fleet, verified) == [] and _lates(fleet, never) == []
        in_window = dict(fleet.ticket(verified))
        fleet.authority.row(verified)                                          # the owner's row lands late
        drain._late_check(before_window, time.time())                          # two overlapping late deciders
        drain._late_verify(in_window, time.time())
        assert fleet.until(lambda: fleet.ticket(verified)["late_open"] == 0, timeout=30.0), fleet.ticket(verified)
        assert fleet.ticket(verified)["matched_row_id"] is not None, fleet.ticket(verified)
        assert fleet.until(lambda: fleet.ticket(never)["late_open"] == 0, timeout=30.0), fleet.ticket(never)
        _assert_one_each(fleet, verified, late=1)
        _assert_one_each(fleet, never, late=1)
        assert len({n["notice_kind"] for n in _lates(fleet, verified)}) == 1, _lates(fleet, verified)
        assert str(_notices(fleet, "default", never)[-1]["content"]) == (
            f"Late outcome: delivery {never} to @{TARGET}: {_UNVERIFIED}")
        return
    if case == "settle_before_first_notice_pass":
        real = drain._decide_notices
        monkeypatch.setattr(drain, "_decide_notices", lambda *a, **k: None)   # the bound's notice pass has not run
        did = _to_unknown_at_bound(fleet, "status? PAYLOAD_H13", claim=True)
        fleet.authority.settle(did, reply="LATE-H13")
        fleet.tick(3, pause=0.3)
        assert fleet.ticket(did)["late_open"] == 1 and _lates(fleet, did) == [], fleet.ticket(did)   # gated (6a)
        monkeypatch.setattr(drain, "_decide_notices", real)
    elif case == "failed_notice_pass":
        real_append = notify.append

        def flaky(notice, *a, **k):
            if notice["recipient"] == notify.SENDER and notice["notice_kind"] == notify.FINAL:
                raise RuntimeError("sender transcript locked")
            return real_append(notice, *a, **k)
        monkeypatch.setattr(notify, "append", flaky)
        did = _to_unknown_at_bound(fleet, "status? PAYLOAD_H13", claim=True)
        fleet.authority.settle(did, reply="LATE-H13")
        assert fleet.until(lambda: len(_notices(fleet, ORCH, did)) == 2, timeout=30.0)
        assert _notices(fleet, "default", did) == []                           # its LATE waits behind its FINAL
        monkeypatch.setattr(notify, "append", real_append)
    else:                                                                      # overlapping_ticks
        import threading

        ack = fleet.send("status? PAYLOAD_H13")
        did = ack["delivery_id"]
        first, second = dict(fleet.ticket(did)), dict(fleet.ticket(did))
        assert drain._finish(first, "ambiguous", "unknown", error="first decider", late_open=1) is True
        assert drain._finish(second, "failed", "unknown", error="second decider") is False
        assert second["state"] == "ambiguous" and fleet.ticket(did)["error"] == "first decider"
        drain._decide_notices(True)
        passes = [threading.Thread(target=notify.deliver_pending, args=(fleet.loaded.module.settings.orchestrator(
            fleet.root),)) for _ in range(2)]
        for t in passes:
            t.start()
        for t in passes:
            t.join(timeout=30)
        fleet.authority.settle(did, reply="LATE-H13")
    assert fleet.until(lambda: fleet.ticket(did)["late_open"] == 0, timeout=30.0), fleet.ticket(did)
    _assert_one_each(fleet, did, late=1)
    for who in ("default", ORCH):
        assert _kinds_in_order(fleet, who, did) == ["final", "late"], (who, _notices(fleet, who, did))
        assert str(_notices(fleet, who, did)[0]["content"]).endswith(_OWNER_SETTLES)


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("case", ["unset", "unknown_profile"])
def test_ac_fleet_f62_h_14(fleet, case, monkeypatch):
    """(derived) Without a valid `orchestrator_profile` the plugin tracks nothing: the `message_agent` tool result is byte-for-byte core's (no `redrive` key), the runner's completion is core's own text, and the plugin writes no ticket and no notice."""
    raw = []
    real_start = fleet.bot_mode_dm._start_delivery
    monkeypatch.setattr(fleet.bot_mode_dm, "_start_delivery",
                        lambda *a, **k: raw.append(real_start(*a, **k)) or raw[-1])
    fleet.settings(orchestrator_profile=None if case == "unset" else "no-such-profile")
    raw_out = fleet.send("status? PAYLOAD_H14", raw=True)
    assert raw and raw_out == raw[-1], (raw_out, raw)
    ack = json.loads(raw_out)
    assert ack.get("status") == "queued" and "redrive" not in ack, ack
    fleet.authority.settle(ack["delivery_id"], reply="REPLY-H14", row_first=False)   # an unverifiable success, even
    _wait_runner_done(fleet, ack["process_id"])
    _wait_completion(fleet, ack["process_id"])
    out = _completion_output(fleet, ack["process_id"])
    assert _last_json(out) == {"reply": "REPLY-H14", "status": "settled", "delivery_id": ack["delivery_id"]}, out
    fleet.tick(4, pause=0.3)
    assert fleet.ticket(ack["delivery_id"]) is None
    assert _notices(fleet, "default", ack["delivery_id"]) == [] and _notices(fleet, ORCH, ack["delivery_id"]) == []


# --------------------------------------------------------------------------- #
# D5a: a settled receipt is success only with its claimed row (E5)
# --------------------------------------------------------------------------- #
def _assert_unverified_end(fleet, did, body_needle, *, target_readable=True):
    assert fleet.until(lambda: fleet.ticket(did)["state"] == "ambiguous", timeout=60.0), fleet.ticket(did)
    assert fleet.ticket(did)["reason"] == "unknown", fleet.ticket(did)
    assert fleet.until(lambda: len(_notices(fleet, "default", did)) == 1 and len(_notices(fleet, ORCH, did)) == 1,
                       timeout=30.0)
    fleet.tick(3, pause=0.5)
    assert len(_notices(fleet, "default", did)) == 1 and len(_notices(fleet, ORCH, did)) == 1
    for who in ("default", ORCH):
        body = str(_notices(fleet, who, did)[0]["content"])
        assert body == f"Delivery {did} to @{TARGET}: {_UNVERIFIED} {_VERIFIABLE}", (who, body)
    assert fleet.ticket(did)["matched_row_id"] is None
    if target_readable:                                  # never assert absence of a row that cannot be read
        assert fleet.rows(TARGET, body_needle) == []


_H17_PROBE_SHAPES = ("compaction_clone_after_baseline", "prefix_only", "continuation_tip")


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("case", ["push", "push_unknown", "non_push", "before_commit", "identical_live",
                                  "commit_late_then_row", "hook_timeout_external", "stall_before_bind",
                                  "stale_prebind", "slow_spawn", "overlap_ordinary", "overlap_retarget",
                                  "overlap_retarget_real", "overlap_retarget_real_bind_during_poll",
                                  "clock_rollback", "inflight_insert_fail", "inflight_insert_fail_after_return",
                                  "concurrent_transforms", "live_same_runner_collision", "live_prune_during_publish",
                                  "residual_deferred_watcher",
                                  "residual_busy_tail", "residual_idless_kill", "residual_post_timeout",
                                  "residual_worker_cap", "residual_worker_start", *_H17_PROBE_SHAPES])
def test_ac_fleet_f62_h_17(fleet, case, request, monkeypatch):
    """(E5, user ruling msg 120) A tracked delivery's settled outcome is never reported as success unless the target's Bot Chat holds the full role='user' row newer than the baseline and claimed by this ticket (D5a). This covers a live receipt settled by an owner whose turn never persisted the row. For a push sender, core's queued completion is rewritten before publication to status: ambiguous with exactly the text "Could not verify your message in the target's Bot Chat; delivery outcome is unknown. Do NOT resend. Check the target before considering another send." That holds also when the runner beats the ticket commit, and when the commit lands after the plugin's 10 s wait (then a verified late row gives one final settled sender notice). For a non-push sender, core's raw row comes first (waived), then one correction notice. An identical-content sibling's row never vouches. A compaction clone of an older identical message (fresh id, same `message_uid`) and a prefix-only row never vouch, and a row that lands in a compression continuation tip does. The ticket ends ambiguous/unknown with one sender and one orchestrator notice."""
    if case in _H17_PROBE_SHAPES:
        _probe_shape(fleet, case)
        return
    if case.startswith("overlap_retarget"):   # the hook's real recipient is the orchestrator's stubbed authority
        _overlap(fleet, monkeypatch, retarget=True,
                 sibling_target="sibling-target" if case == "overlap_retarget" else ORCH,
                 bind_during_poll=case == "overlap_retarget_real_bind_during_poll")
        return
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

    fleet.settings(redrive_window_seconds=12)
    first, second = fleet.send("status? PAYLOAD_H17_SAME"), fleet.send("status? PAYLOAD_H17_SAME")
    ids = (first["delivery_id"], second["delivery_id"])
    for n, did in enumerate(ids):                      # only the first owner turn persisted its row
        fleet.authority.settle(did, reply=f"SAME-{n}", row_first=n == 0)
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

    for home in fleet.homes.values():
        data = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8-sig"))
        data["plugins"]["hook_callback_timeout"] = 120
        (home / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    fleet.load()
    fleet.tick()
    send = fleet.loaded.module.send
    real_reported = send.reported_success
    lock, entered = threading.Lock(), []
    armed, second_in, release = threading.Event(), threading.Event(), threading.Event()

    def held(output, *a, **k):
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
        return real_reported(output, *a, **k)
    monkeypatch.setattr(send, "reported_success", held)
    first, second = fleet.send("status? PAYLOAD_H17_CONC_A"), fleet.send("status? PAYLOAD_H17_CONC_B")
    armed.set()
    try:
        for n, ack in enumerate((first, second)):
            fleet.authority.settle(ack["delivery_id"], reply=f"CONC-{n}", row_first=False)
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
    cmd = fleet.bot_mode_dm._delivery_command(["hermes", "-p", TARGET], dm, stdin_file=False)
    send = fleet.loaded.module.send
    real, entered = send.reported_success, threading.Event()

    def held(output, *a, **k):
        if f'"delivery_id": "{name}"' in str(output):
            entered.set()
            assert release.wait(timeout=240), "holder never released"
        return real(output, *a, **k)
    monkeypatch.setattr(send, "reported_success", held)

    def run():
        if tool_call_id:
            set_current_observability_context(tool_call_id=tool_call_id)
        transform_process_output(json.dumps({"status": "settled", "delivery_id": name}), command=cmd, returncode=0)
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
            fleet.authority.row(did)                   # the owner's late flush lands the row before the commit
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


def _install_retarget_gate(fleet):
    """Load ``_RETARGET_INIT`` as a real plugin: core runs its modify inside the executor, after the plugin's
    middleware has read the pre-modify target."""
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
        _install_retarget_gate(fleet)
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
        else:                                             # the hook's real recipient settles it, row unproven
            real_did = _admissions(fleet.homes[ORCH], "RETARGET_ME_H17")[0]["delivery_id"]
            fleet.authority.settle(real_did, reply="REPLY-FROM-orchestrator", row_first=False, profile=ORCH)
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



def _probe_shape(fleet, shape):
    """The three probe shapes ruled into this id (msg 1162): a post-baseline compaction clone of an older identical
    message and a prefix-only row never vouch for a settled receipt; a row in a compression continuation tip does."""
    from hermes_state import SessionDB

    home = fleet.homes[TARGET]
    if shape == "continuation_tip":
        db = SessionDB(db_path=home / "state.db")
        try:
            db.end_session("bc-live", "compression")
            db.create_session("bc-live-tip", source="cli", parent_session_id="bc-live")
        finally:
            db.close()
        ack = fleet.send("status? PAYLOAD_H17_TIP")
        fleet.authority.settle(ack["delivery_id"], reply="REPLY-H17-TIP")      # the owner writes into the tip
        assert fleet.until(lambda: fleet.ticket(ack["delivery_id"])["state"] == "settled", timeout=60.0)
        rows = fleet.rows(TARGET, "PAYLOAD_H17_TIP")
        assert len(rows) == 1 and rows[0]["session_id"] == "bc-live-tip", rows
        assert fleet.ticket(ack["delivery_id"])["matched_row_id"] == rows[0]["id"]
        return
    needle = "PAYLOAD_H17_CLONE" if shape == "compaction_clone_after_baseline" else "PAYLOAD_H17_PREFIX"
    older = None
    if shape == "compaction_clone_after_baseline":
        older = fleet.send(f"status? {needle}")          # an earlier identical message lands and settles
        fleet.authority.settle(older["delivery_id"], reply="REPLY-H17-OLDER")
        assert fleet.until(lambda: fleet.ticket(older["delivery_id"])["state"] == "settled", timeout=60.0)
    ack = fleet.send(f"status? {needle}")
    did = ack["delivery_id"]
    fleet.authority.claim(did)
    if older is not None:
        original = fleet.rows(TARGET, needle)[0]
        _append_target_row(fleet, original["content"], message_uid=original["message_uid"])   # fresh id, same uid
    else:
        whole = fleet.ticket(did)["content"]
        _append_target_row(fleet, whole[: len(whole) // 2])                                  # prefix only
    fleet.authority.settle(did, reply="REPLY-H17-SHAPE", row_first=False)
    _wait_runner_done(fleet, ack["process_id"], timeout=40.0)
    assert fleet.until(lambda: fleet.registry.get(ack["process_id"])._completion_event.is_set(), ticks=False,
                       timeout=30.0)
    out = _completion_output(fleet, ack["process_id"])
    assert _last_json(out).get("status") == "ambiguous" and _UNVERIFIED in out, out
    assert fleet.until(lambda: fleet.ticket(did)["state"] == "ambiguous", timeout=60.0), fleet.ticket(did)
    t = fleet.ticket(did)
    assert (t["reason"], t["unverified"], t["matched_row_id"]) == ("unknown", 1, None), t
    _assert_one_each(fleet, did, late=0)
    for who in ("default", ORCH):
        assert str(_notices(fleet, who, did)[0]["content"]) == f"Delivery {did} to @{TARGET}: {_UNVERIFIED} {_VERIFIABLE}"
    if older is not None:
        assert fleet.ticket(older["delivery_id"])["matched_row_id"] == fleet.rows(TARGET, needle)[0]["id"]


# --------------------------------------------------------------------------- #
# DMs core answers without tracking (DN-E4-12, E4″.1)
# --------------------------------------------------------------------------- #
_F69_7_MODES = {"held_not_sent": "held", "already_failed": "terminal:failed:provider_rate_limit",
                "already_cancelled": "terminal:cancelled:runtime_offline", "queue_full": "refuse:queue_full",
                "draining": "refuse:runtime_draining", "same_id_twice": "terminal:failed:provider_rate_limit"}
_F69_7_NEGATIVES = ("modified_target", "unconfirmed_recipient")


def _f69_7_no_notice(fleet, case, raw):
    """E4″.2's negatives: core answered, but nothing confirms the recipient was the target's own profile, so the
    plugin records no ticket and writes no notice."""
    from tools.bot_failure_reasons import HELD_NOT_SENT

    if case == "modified_target":                        # the modify sends it to the orchestrator, which refuses
        _install_retarget_gate(fleet)
        fleet.authority.mode("held", profile=ORCH)
        body, reason = "status? RETARGET_ME_H17 PAYLOAD_F69_7", HELD_NOT_SENT
    else:                                                # no ready authority: core's own runtime_unavailable
        fleet.authority.configure(ready=[ORCH])
        body, reason = "status? PAYLOAD_F69_7", "runtime_unavailable"
    out = fleet.send(body, raw=True)
    assert [out] == raw, (out, raw)                      # byte-for-byte core's tool result
    ack = json.loads(out)
    did = ack["delivery_id"]
    assert ack["reason"] == reason and not {"status", "to", "redrive"} & set(ack), ack
    assert fleet.authority.receipt(did) is None          # nothing under the target's home confirms it
    if case == "modified_target":
        assert fleet.authority.receipt(did, profile=ORCH) is not None
    else:
        assert not [p for p in fleet.dm_dir.iterdir()    # _settled_no_send took the payload and the intent
                    if p.name.endswith(".live.json") or fleet.bot_mode_dm._dm_delivery_id(p) == did]
    fleet.tick(3, pause=0.3)
    assert fleet.ticket(did) is None
    assert fleet.loaded.module.store.notices(did) == []
    assert all(_notices(fleet, name, did) == [] for name in fleet.homes), {n: _notices(fleet, n, did)
                                                                            for n in fleet.homes}


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("case", [*_F69_7_MODES, *_F69_7_NEGATIVES])
def test_ac_upg_f69_7(fleet, case, monkeypatch):
    """(DN-E4-12, msg 1288) While tracking is live, a local `message_agent` DM that core answers without the plugin tracking it gives the orchestrator profile exactly one notice per `delivery_id`: a typed FINAL for a recipient-confirmed held no-send or a receipt already `failed`/`cancelled`, and an `unknown` notice for a recipient-confirmed uncertain admission refusal. A `runtime_unavailable` no-send, or any answer whose recipient cannot be confirmed, gets no notice. The sender's tool result is byte-for-byte core's, and the plugin writes no sender notice."""
    from tools.bot_failure_reasons import HELD_NOT_SENT

    raw = []
    real_start = fleet.bot_mode_dm._start_delivery
    monkeypatch.setattr(fleet.bot_mode_dm, "_start_delivery",
                        lambda *a, **k: raw.append(real_start(*a, **k)) or raw[-1])
    if case in _F69_7_NEGATIVES:
        _f69_7_no_notice(fleet, case, raw)
        return
    if case == "same_id_twice":                          # a same-id resend of a delivery core already finished
        real_write, fixed = fleet.bot_mode_dm._write_dm_file, []

        def same_file(content):
            if not fixed:
                fixed.append(real_write(content))
            else:
                Path(fixed[0]).write_text(content, encoding="utf-8")
            return fixed[0]
        monkeypatch.setattr(fleet.bot_mode_dm, "_write_dm_file", same_file)
    fleet.authority.mode(_F69_7_MODES[case])
    # Distinct arguments per send: core's executor appends a loop warning to an identical repeated call. The pinned
    # intent beside the reused DM file keeps the second send on the first one's delivery_id.
    outs = [fleet.send(f"status? PAYLOAD_F69_7 #{n}", raw=True) for n in range(2 if case == "same_id_twice" else 1)]
    assert outs == raw[-len(outs):], (outs, raw)        # byte-for-byte core's tool result
    acks = [json.loads(o) for o in outs]
    did = acks[0]["delivery_id"]
    assert all(a["delivery_id"] == did and "redrive" not in a for a in acks), acks
    t = fleet.ticket(did)
    assert t is not None and t["path"] == "core", t
    expected = {"held_not_sent": ("failed", HELD_NOT_SENT), "already_failed": ("failed", "provider_rate_limit"),
                "already_cancelled": ("cancelled", "runtime_offline"), "queue_full": ("ambiguous", "unknown"),
                "draining": ("ambiguous", "unknown"), "same_id_twice": ("failed", "provider_rate_limit")}[case]
    assert (t["state"], t["reason"]) == expected, t
    assert len([r for r in fleet.loaded.module.store.tickets_for_pair(str(fleet.root), str(fleet.homes[TARGET]))
                if r["delivery_id"] == did]) == 1
    error = acks[0].get("error") or fleet.authority.receipt(did)["error"]
    detail = error.rstrip().removesuffix(".")
    if t["state"] == "ambiguous":
        text = (f"Delivery {did} to @{TARGET} outcome unknown [reason: unknown]: {detail}. The sender was told not to "
                "resend; the plugin does not track this delivery.")
    else:
        text = (f"Delivery {did} to @{TARGET} {t['state']} [reason: {t['reason']}]: {detail}. The sender has core's "
                "own answer; the plugin does not re-send it.")
    assert fleet.until(lambda: len(_notices(fleet, ORCH, did)) == 1, timeout=30.0)
    fleet.tick(3, pause=0.3)
    notes = _notices(fleet, ORCH, did)
    assert len(notes) == 1 and str(notes[0]["content"]) == text, (notes, text)
    assert _notices(fleet, "default", did) == []
    assert [n["recipient"] for n in fleet.loaded.module.store.notices(did)] == ["orchestrator"]
