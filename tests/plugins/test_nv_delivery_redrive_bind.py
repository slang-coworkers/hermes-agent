"""nv-delivery-redrive's per-call hook identity (ADR D5a.5): the ``tool_execution`` middleware binds a ``tool_call_id``
around ``next_call`` so each delivery runner's completion transform gets its own hook gate key, and the drainer's own
re-drives never enter core's registry. Each test loads the plugin through the acceptance test's ``fleet`` fixture
(real discovery, real middleware chain).
"""

from __future__ import annotations

import contextvars
import re

import pytest

from tests.plugins.test_fleet_f62_h_acceptance import TARGET, _busy, _wait_runner_done, fleet

_MINTED = re.compile(r"nv-redrive:[0-9a-f]{32}")


def _seen_ids(fleet, monkeypatch, *calls, raise_in_next=False):
    """Run each ``(tool_name, tool_call_id)`` through the plugin's middleware with a recording ``next_call``.
    Returns, per call, the observability ids bound while ``next_call`` ran and the ids bound after it returned."""
    from tools import approval_context as ctx

    middleware = fleet.loaded.module.send.middleware
    seen = []

    def record(_payload):
        seen.append((ctx._approval_tool_call_id.get(), ctx._approval_turn_id.get(), ctx._approval_session_id.get()))
        if raise_in_next:
            raise RuntimeError("downstream failed (test)")
        return "{}"

    def run():
        ctx.set_current_observability_context(turn_id="turn-outer", tool_call_id="call-outer",
                                              session_id="sess-outer")
        after = []
        for tool_name, call_id in calls:
            try:
                middleware(tool_name=tool_name, args={"target": "nobody", "message": "x"}, next_call=record,
                           task_id="t", session_id="sess-kw", tool_call_id=call_id)
            except RuntimeError:
                assert raise_in_next
            after.append((ctx._approval_tool_call_id.get(), ctx._approval_turn_id.get(),
                          ctx._approval_session_id.get()))
        return after
    return seen, contextvars.Context().run(run)


@pytest.mark.platforms("linux")
def test_each_message_agent_call_binds_its_own_tool_call_id(fleet, monkeypatch):
    """Two message_agent calls see their own tool_call_id inside next_call; turn_id and session_id pass unchanged."""
    seen, _after = _seen_ids(fleet, monkeypatch, ("message_agent", "call-1"), ("message_agent", "call-2"))
    assert seen == [("call-1", "turn-outer", "sess-outer"), ("call-2", "turn-outer", "sess-outer")], seen


@pytest.mark.platforms("linux")
def test_empty_tool_call_id_gets_a_distinct_minted_id(fleet, monkeypatch):
    """A call with no tool_call_id is bound to a fresh nv-redrive:<uuid4 hex> id, never "" and never reused."""
    seen, _after = _seen_ids(fleet, monkeypatch, ("message_agent", ""), ("message_agent", None))
    ids = [s[0] for s in seen]
    assert all(_MINTED.fullmatch(i) for i in ids), ids
    assert ids[0] != ids[1], ids


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("raise_in_next", [False, True])
def test_binding_is_restored_after_next_call(fleet, monkeypatch, raise_in_next):
    """The caller's ids are back in place once next_call returns or raises."""
    seen, after = _seen_ids(fleet, monkeypatch, ("message_agent", "call-1"), raise_in_next=raise_in_next)
    assert seen == [("call-1", "turn-outer", "sess-outer")], seen
    assert after == [("call-outer", "turn-outer", "sess-outer")], after


@pytest.mark.platforms("linux")
def test_other_tools_are_not_bound(fleet, monkeypatch):
    """A call of any other tool passes through with the caller's ids untouched."""
    seen, _after = _seen_ids(fleet, monkeypatch, ("terminal", "call-9"))
    assert seen == [("call-outer", "turn-outer", "sess-outer")], seen


@pytest.mark.platforms("linux")
def test_drainer_redrive_adds_no_registry_session(fleet):
    """A drainer re-drive (spawn_attempt) runs outside core's process registry, so it never reaches the transform."""
    with _busy(fleet):
        ack = fleet.send("status? PAYLOAD_BIND_DRAIN")
        _wait_runner_done(fleet, ack["process_id"])
    before = set(fleet.registry._running) | set(fleet.registry._finished)
    assert fleet.until(lambda: len(fleet.attempts(ack["delivery_id"])) >= 2, timeout=30.0)
    assert set(fleet.registry._running) | set(fleet.registry._finished) == before
    assert fleet.until(lambda: fleet.ticket(ack["delivery_id"])["state"] == "settled", timeout=60.0)
    assert len(fleet.rows(TARGET, "PAYLOAD_BIND_DRAIN")) == 1
