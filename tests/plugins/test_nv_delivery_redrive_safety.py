"""Regression tests for nv-delivery-redrive's CODE_REVIEW findings (FLEET-F62.h): a re-targeted call, a stale tick
snapshot, a late identical sibling and a silent exit 0. Each loads the plugin through the acceptance test's ``fleet``
fixture (real discovery, real executor, real delivery runner, fake target CLI) and asserts one outcome.
"""

from __future__ import annotations

import json
import sys
import textwrap

import pytest

from tests.plugins.test_fleet_f62_h_acceptance import (
    ORCH,
    TARGET,
    _busy,
    _wait_runner_done,
    fleet,
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


@pytest.mark.platforms("linux")
def test_rewritten_target_is_not_tracked(fleet):
    """A pre_tool_call modify that re-targets the call is not ticketed under the target the middleware resolved, so
    a busy refusal from the real target is never re-driven to the original one."""
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
