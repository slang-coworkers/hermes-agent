"""Acceptance tests for Hermes outbound destination controls and delivery retries.

Adopt-track regression guard; this row adds no plugin.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from hermes_cli.plugins import PluginManager

PLUGIN_KEY = "nv-fleet-gates"
_REPO = Path(__file__).resolve().parents[2]
PLUGIN_SRC = _REPO / "plugins" / PLUGIN_KEY
DOC_PAGE = (
    _REPO
    / "website"
    / "docs"
    / "user-guide"
    / "features"
    / "outbound-destination-acl-and-delivery.md"
)
STAGE_MARKERS = ["[Fix Report]", "[Review Verdict]", "[Resolution]", "[handoff]"]


# --------------------------------------------------------------------------- #
# AC-CH-F51-2 harness — the merged nv-fleet-gates loader/gate/wire helpers.
# --------------------------------------------------------------------------- #
def _base_settings(hermes_home, edges_db, **overrides):
    settings = {
        "role": "worker",
        "profile_roles": {
            "orch": "orchestrator",
            "worker-a": "worker",
            "worker-b": "worker",
            "worker-c": "worker",
        },
        "enforce_sandbox": False,
        "expected_backend": "docker",
        "required_stages": ["OUTPUT_REVIEW"],
        "plan_gate": True,
        "edges_db_path": str(edges_db),
        "stage_markers": STAGE_MARKERS,
        "admin_tools": [
            "onboard_coworker",
            "onboard_project",
            "create_agent",
            "record_decision",
            "record_human_verdict",
        ],
        "skills_root": str(hermes_home / "skills"),
        "shared_learnings_path": str(hermes_home / "shared-learnings"),
    }
    settings.update(overrides)
    return settings


def _prime_registry(monkeypatch, hermes_home, bundled):
    monkeypatch.setenv("HOME", str(hermes_home.parent.parent / "fakehome"))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.delenv("HERMES_ENABLE_PROJECT_PLUGINS", raising=False)
    # importing tools.registry alone does NOT register the built-in tools; the
    # plugin's load-time restricted-set assertion reads the running registry.
    import model_tools

    model_tools.discover_builtin_tools()


def _load(tmp_path, monkeypatch, *, profile="worker-a", edges_db=None):
    hermes_home = tmp_path / "home" / profile
    (hermes_home / "plugins").mkdir(parents=True)
    bundled = tmp_path / "empty-bundled"
    bundled.mkdir(exist_ok=True)
    edges_db = edges_db or (tmp_path / "fleet" / "edges.db")
    edges_db.parent.mkdir(parents=True, exist_ok=True)

    if not PLUGIN_SRC.exists():
        pytest.fail(f"plugin source missing: {PLUGIN_SRC}")
    shutil.copytree(PLUGIN_SRC, hermes_home / "plugins" / PLUGIN_KEY)

    settings = _base_settings(hermes_home, edges_db)
    settings["profile"] = profile
    cfg = {
        "plugins": {
            "enabled": [PLUGIN_KEY],
            "entries": {PLUGIN_KEY: {"settings": settings}},
        }
    }
    (hermes_home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")

    _prime_registry(monkeypatch, hermes_home, bundled)
    manager = PluginManager()
    manager.discover_and_load()
    return manager, hermes_home, edges_db


def _loaded(manager):
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled is True
    assert loaded.error is None
    assert loaded.module is not None
    assert "pre_tool_call" in loaded.hooks_registered
    return loaded


def _gate(manager, tool_name, args, session_id="sess-1", **kwargs):
    results = manager.invoke_hook(
        "pre_tool_call",
        tool_name=tool_name,
        args=args,
        session_id=session_id,
        task_id="task-1",
        tool_call_id="tc-1",
        turn_id="turn-1",
        **kwargs,
    )
    return [r for r in (results or []) if isinstance(r, dict) and r.get("action")]


def _act(directives):
    return directives[0]["action"] if directives else None


def _msg(directives):
    return directives[0].get("message", "") if directives else ""


def _wire(manager, *argv):
    entry = manager._cli_commands["wire"]
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="cmd")
    entry["setup_fn"](sub.add_parser("wire"))
    ns = parser.parse_args(["wire", *argv])
    handler = entry.get("handler_fn") or getattr(ns, "func", None)
    return handler(ns)


def test_ac_ch_f51_2(tmp_path, monkeypatch):
    """The outbound destination ACL enforces wired-only messaging: an unwired
    message_agent/kanban_create destination is refused (action:block, surfaced
    with the wired-teammates list), a wired destination is allowed."""
    manager, _home, _edges = _load(tmp_path, monkeypatch, profile="worker-a")
    _loaded(manager)
    _wire(manager, "add", "worker-a", "worker-b")

    unwired_msg = _gate(manager, "message_agent", {"target": "worker-c"})
    assert _act(unwired_msg) == "block"
    assert "wired-only" in _msg(unwired_msg) and "worker-c" in _msg(unwired_msg)

    unwired_kanban = _gate(manager, "kanban_create", {"assignee": "worker-c"})
    assert _act(unwired_kanban) == "block"
    assert "wired-only" in _msg(unwired_kanban) and "worker-c" in _msg(unwired_kanban)

    assert _act(_gate(manager, "message_agent", {"target": "worker-b"})) != "block"
    assert _act(_gate(manager, "kanban_create", {"assignee": "worker-b"})) != "block"


# --------------------------------------------------------------------------- #
# AC-CH-F51-3 — dead-target short-circuit through the real DeliveryRouter path
# --------------------------------------------------------------------------- #
class _ForbiddenThenOkAdapter:
    """First `fail_times` sends raise a deleted-group Forbidden; then succeed."""

    def __init__(self, fail_times=1):
        self.calls = []
        self._fail_times = fail_times

    async def send(self, chat_id, content, metadata=None):
        self.calls.append(chat_id)
        if len(self.calls) <= self._fail_times:
            raise RuntimeError("Forbidden: the group chat was deleted")
        return {"success": True}


class _TransientFailAdapter:
    def __init__(self):
        self.calls = []

    async def send(self, chat_id, content, metadata=None):
        self.calls.append(chat_id)
        raise RuntimeError("httpx.ReadTimeout: connection timed out")


@pytest.fixture
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr("gateway.dead_targets.get_hermes_home", lambda: tmp_path)
    return tmp_path


@pytest.mark.asyncio
async def test_ac_ch_f51_3(_isolate):
    """A destination that returns a permanent failure is recorded dead and a
    subsequent delivery is short-circuited (skipped==dead_target) without
    re-invoking the adapter, while a transient failure / distinct target stays
    deliverable (not marked dead)."""
    from gateway.config import GatewayConfig, Platform
    from gateway.delivery import DeliveryRouter, DeliveryTarget

    forbidden = _ForbiddenThenOkAdapter(fail_times=99)
    router = DeliveryRouter(GatewayConfig(), adapters={Platform.TELEGRAM: forbidden})
    target = DeliveryTarget.parse("telegram:42")

    res1 = await router.deliver("hi", [target])
    assert res1["telegram:42"]["success"] is False
    assert router.dead_targets.is_dead("telegram", "42") is True
    assert forbidden.calls == ["42"]

    res2 = await router.deliver("hi again", [target])
    assert res2["telegram:42"]["skipped"] == "dead_target"
    assert forbidden.calls == ["42"]

    # A fresh registry over the same on-disk store still reports the target
    # dead: the flag survives a gateway restart.
    from gateway.dead_targets import DeadTargetRegistry

    assert DeadTargetRegistry().is_dead("telegram", "42") is True

    transient = _TransientFailAdapter()
    router2 = DeliveryRouter(
        GatewayConfig(),
        adapters={Platform.TELEGRAM: transient},
        dead_targets=router.dead_targets,
    )
    tgt2 = DeliveryTarget.parse("telegram:77")
    res3 = await router2.deliver("ping", [tgt2])
    assert res3["telegram:77"].get("skipped") != "dead_target"
    assert router2.dead_targets.is_dead("telegram", "77") is False
    assert transient.calls == ["77"]


# --------------------------------------------------------------------------- #
# AC-CH-F51-4 — bounded, honest adapter-reconnect redelivery
# --------------------------------------------------------------------------- #
@pytest.fixture
def _fresh_db(tmp_path, monkeypatch):
    from gateway import delivery_ledger as dl

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(dl, "_db_path", lambda: home / "state.db")
    return home


def _record(
    dl, oid="ob-1", session_key="agent:main:slack:channel:C1", platform="slack"
):
    dl.record_obligation(
        obligation_id=oid,
        session_key=session_key,
        platform=platform,
        chat_id="C1",
        thread_id="171.001",
        content="the final answer",
    )


def _runner(adapter):
    from gateway.config import Platform
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.SLACK: adapter}
    runner._profile_adapters = {}
    runner._active_profile_name = lambda: "default"
    store = MagicMock()
    store.clear_resume_pending = AsyncMock()
    store._store = None
    runner.session_store = None
    runner._async_session_store = store
    return runner


def _ok_adapter():
    adapter = MagicMock()
    adapter.send = AsyncMock(return_value=MagicMock(success=True, error=""))
    return adapter


@pytest.mark.asyncio
async def test_ac_ch_f51_4(_fresh_db):
    """On adapter reconnect a retryable failed obligation is actually re-sent
    with the may-be-duplicate marker and marked delivered; a non-retryable
    failure is not re-sent; an obligation at MAX_ATTEMPTS is abandoned."""
    from gateway import delivery_ledger as dl
    from gateway.config import Platform

    _record(dl, oid="ob-1")
    dl.mark_failed("ob-1", "send_path_degraded")
    adapter = _ok_adapter()
    runner = _runner(adapter)
    n = await runner._redeliver_failed_obligations_for_platform(Platform.SLACK)
    assert n == 1
    assert adapter.send.await_count == 1
    assert adapter.send.call_args.kwargs["content"].startswith(dl.RECONNECTED_MARKER)
    with dl._connect() as conn:
        assert (
            conn.execute(
                "SELECT state FROM delivery_obligations WHERE obligation_id=?",
                ("ob-1",),
            ).fetchone()[0]
            == "delivered"
        )

    _record(dl, oid="ob-2")
    dl.mark_failed("ob-2", "forbidden")
    adapter2 = _ok_adapter()
    runner2 = _runner(adapter2)
    assert await runner2._redeliver_failed_obligations_for_platform(Platform.SLACK) == 0
    assert adapter2.send.await_count == 0
    with dl._connect() as conn:
        assert (
            conn.execute(
                "SELECT state FROM delivery_obligations WHERE obligation_id=?",
                ("ob-2",),
            ).fetchone()[0]
            == "failed"
        )

    _record(dl, oid="ob-3")
    dl.mark_failed("ob-3", "send_path_degraded")
    with dl._connect() as conn:
        conn.execute(
            "UPDATE delivery_obligations SET attempts=? WHERE obligation_id=?",
            (dl.MAX_ATTEMPTS, "ob-3"),
        )
        conn.commit()
    adapter3 = _ok_adapter()
    runner3 = _runner(adapter3)
    await runner3._redeliver_failed_obligations_for_platform(Platform.SLACK)
    assert adapter3.send.await_count == 0
    with dl._connect() as conn:
        assert (
            conn.execute(
                "SELECT state FROM delivery_obligations WHERE obligation_id=?",
                ("ob-3",),
            ).fetchone()[0]
            == "abandoned"
        )


# --------------------------------------------------------------------------- #
# AC-CH-F51-5 — room-membership leg of the destination ACL (native, stock-green)
# --------------------------------------------------------------------------- #
def test_ac_ch_f51_5():
    """The room-membership leg of the destination ACL: a bot may address only
    members in the room roster — an @mention of a roster member resolves to that
    member, an @mention of a non-member resolves to nothing (cannot be routed)."""
    from gateway.hosted_room_discussion import resolve_mentions, validate_roster

    members = validate_roster(
        [
            {"member_id": "worker-a", "profile": "worker-a", "handle": "worker-a"},
            {"member_id": "worker-b", "profile": "worker-b", "handle": "worker-b"},
        ],
        local_profiles={"worker-a", "worker-b", "worker-c"},
    )
    resolved = resolve_mentions(("@worker-b ping",), members, default_all=False)
    assert [m.profile for m in resolved] == ["worker-b"]
    # A non-member (worker-c is a local profile but NOT in this room) is not
    # addressable — resolves to the empty roster slice (differential control).
    assert resolve_mentions(("@worker-c ping",), members, default_all=False) == ()


# --------------------------------------------------------------------------- #
# Documentation-contract control (not an acceptance criterion).
# --------------------------------------------------------------------------- #
def test_ch_f51_doc_contract():
    """The CH-F51 doc page exists (fail-on-base gate), cites the covering Hermes
    surface, and carries the AC-CH-F51 traceability ids."""
    assert DOC_PAGE.is_file()  # new page -> fails on the merge base, passes on head
    text = DOC_PAGE.read_text(encoding="utf-8")
    for marker in ("tag:", "main:", "baseline:"):
        assert marker in text
    for surface in (
        "delivery_ledger",
        "MAX_ATTEMPTS",
        "RECONNECTED_MARKER",
        "DeadTargetRegistry",
        "DeliveryRouter",
        "message_agent",
        "nv-fleet-gates",
        "get_home_channel",
        "validate_roster",
    ):
        assert surface in text
    for ac in ("AC-CH-F51-2", "AC-CH-F51-3", "AC-CH-F51-4", "AC-CH-F51-5"):
        assert ac in text
