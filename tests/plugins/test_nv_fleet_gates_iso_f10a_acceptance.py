"""Acceptance tests for ISO-F10.a — the nv-fleet-gates SANDBOX active-profile-vs-launch hard-fail.

One ``test_ac_iso_f10_a_<n>`` per ``pytest:`` acceptance-criterion id
(``AC-ISO-F10.a-<n>``, with ``-`` and ``.`` normalised to ``_``). Loaded from an
isolated ``HERMES_HOME`` through the real ``PluginManager().discover_and_load()``
path; behaviour contract only, no network, nothing under ``~/.hermes``.
"""

from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from hermes_cli.plugins import PluginManager

PLUGIN_KEY = "nv-fleet-gates"
_REPO = Path(__file__).resolve().parents[2]
PLUGIN_SRC = _REPO / "plugins" / PLUGIN_KEY
STAGE_MARKERS = ["[Fix Report]", "[Review Verdict]", "[Resolution]", "[handoff]"]


def _base_settings(hermes_home, edges_db, **overrides):
    settings = {
        "role": "worker",
        "profile_roles": {"orch": "orchestrator", "worker-a": "worker",
                          "worker-b": "worker"},
        "enforce_sandbox": False,
        "expected_backend": "docker",
        "required_stages": ["OUTPUT_REVIEW"],
        "plan_gate": True,
        "edges_db_path": str(edges_db),
        "stage_markers": STAGE_MARKERS,
        "admin_tools": ["onboard_coworker", "create_agent"],
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
    # plugin's load-time restricted-set assertion reads get_all_tool_names().
    import model_tools
    model_tools.discover_builtin_tools()


def _load(tmp_path, monkeypatch, *, profile="orch", **settings_overrides):
    """Load nv-fleet-gates under HERMES_HOME=<tmp>/home/<profile>.

    The launch profile is fixed by the process env HERMES_HOME (read by
    ``get_process_hermes_home()``); a per-turn active profile is simulated in
    the tests with the production ``set_hermes_home_override`` contextvar.
    """
    hermes_home = tmp_path / "home" / profile
    (hermes_home / "plugins").mkdir(parents=True)
    bundled = tmp_path / "empty-bundled"
    bundled.mkdir(exist_ok=True)
    edges_db = tmp_path / "fleet" / "edges.db"
    edges_db.parent.mkdir(parents=True, exist_ok=True)

    if not PLUGIN_SRC.exists():
        pytest.fail(f"plugin source missing: {PLUGIN_SRC}")
    shutil.copytree(PLUGIN_SRC, hermes_home / "plugins" / PLUGIN_KEY)

    settings = _base_settings(hermes_home, edges_db, **settings_overrides)
    settings["profile"] = profile
    cfg = {"plugins": {"enabled": [PLUGIN_KEY],
                       "entries": {PLUGIN_KEY: {"settings": settings}}}}
    (hermes_home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")

    _prime_registry(monkeypatch, hermes_home, bundled)
    manager = PluginManager()
    manager.discover_and_load()
    return manager, hermes_home


def _loaded(manager):
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled is True
    assert loaded.error is None
    assert loaded.module is not None
    assert "pre_tool_call" in loaded.hooks_registered
    return loaded


def _gate(manager, tool_name, args, session_id="sess-1", **kwargs):
    results = manager.invoke_hook(
        "pre_tool_call", tool_name=tool_name, args=args, session_id=session_id,
        task_id="task-1", tool_call_id="tc-1", turn_id="turn-1", **kwargs)
    directives = [r for r in (results or []) if isinstance(r, dict) and r.get("action")]
    return directives[0] if directives else None


def _act(d):
    return d.get("action") if isinstance(d, dict) else None


def _gate_multiplexed(manager, tool_name, args, active_home):
    """Dispatch the gate as a multiplexed gateway turn does.

    Installs the per-turn HERMES_HOME override on this thread — the same
    ``set_hermes_home_override`` call ``gateway/run.py`` ``_profile_runtime_scope``
    makes — then runs the ``pre_tool_call`` hook on a worker thread via
    ``tools.thread_context.propagate_context_to_thread`` (``contextvars.copy_context``
    under the hood), exactly the seam the gateway uses to reach the tool executor.
    ``get_process_hermes_home`` stays the launch env, so the predicate observes
    active-vs-launch across the real thread hop, not a same-thread shortcut.
    """
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from tools.thread_context import propagate_context_to_thread

    token = set_hermes_home_override(str(active_home))
    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(
                propagate_context_to_thread(_gate), manager, tool_name, args
            ).result(timeout=10)
    finally:
        reset_hermes_home_override(token)


def test_ac_iso_f10_a_1(tmp_path, monkeypatch):
    """With enforce_sandbox true, a gateway turn whose ACTIVE profile != the LAUNCH profile is hard-failed by the SANDBOX predicate, naming both profiles.

    The active profile is the worker's home, propagated into the hook's worker
    thread as the multiplexer does; the launch profile stays the process env
    HERMES_HOME. Backend matches so any block is the cross-profile refusal, not a
    mismatch; ``terminal ls`` is non-mutating and matches no critique/wiring/admin
    predicate, so only SANDBOX can fire.
    """
    import tools.terminal_tool as tt
    monkeypatch.setattr(tt, "ensure_task_env", lambda *a, **k: object(), raising=False)

    manager, _launch_home = _load(
        tmp_path, monkeypatch, profile="orch", enforce_sandbox=True, plan_gate=False)
    _loaded(manager)

    monkeypatch.setenv("TERMINAL_ENV", "docker")
    active_home = tmp_path / "home" / "worker-a"
    active_home.mkdir(parents=True, exist_ok=True)
    d = _gate_multiplexed(manager, "terminal", {"command": "ls"}, active_home)
    assert _act(d) == "block"
    msg = d["message"]
    assert "cross-profile" in msg
    assert "worker-a" in msg
    assert "orch" in msg

    # A backend mismatch must not mask the higher-priority cross-profile refusal:
    # the cross-profile branch is placed FIRST, before any TERMINAL_ENV value is
    # trusted (ADR §Design). With a mismatched backend that WOULD otherwise block,
    # the directive must still be the cross-profile refusal, not the mismatch one.
    monkeypatch.setenv("TERMINAL_ENV", "local")
    d = _gate_multiplexed(manager, "terminal", {"command": "ls"}, active_home)
    assert _act(d) == "block"
    assert "cross-profile" in d["message"]
    assert "worker-a" in d["message"] and "orch" in d["message"]


def test_ac_iso_f10_a_2(tmp_path, monkeypatch):
    """With enforce_sandbox true and active profile == launch profile, the cross-profile check does NOT over-block: the launch profile's own gateway turns proceed.

    The multiplexer installs a per-turn override on every turn, including the
    launch profile's own, so this models that turn with an override pointing back
    at the launch home — the anti-regression guard against a naive "block whenever
    an override exists" implementation, which would wrongly refuse the launch
    profile here. ensure_task_env is stubbed ready and the backend matches so a
    benign terminal clears the whole SANDBOX predicate.
    """
    import tools.terminal_tool as tt
    monkeypatch.setattr(tt, "ensure_task_env", lambda *a, **k: object(), raising=False)

    manager, launch_home = _load(
        tmp_path, monkeypatch, profile="orch", enforce_sandbox=True, plan_gate=False)
    _loaded(manager)

    monkeypatch.setenv("TERMINAL_ENV", "docker")
    d = _gate_multiplexed(manager, "terminal", {"command": "ls"}, launch_home)
    assert _act(d) != "block"


def test_ac_iso_f10_a_3(tmp_path, monkeypatch):
    """With enforce_sandbox false (the batch-2 default), the cross-profile check is inert: an active!=launch turn is not blocked by it.

    Preserves the shipped batch-2 posture and AC-ISO-F10-1: the same
    active!=launch condition as _1 is set up, but with enforce_sandbox false the
    SANDBOX predicate returns early and nothing blocks.
    """
    import tools.terminal_tool as tt
    monkeypatch.setattr(tt, "ensure_task_env", lambda *a, **k: object(), raising=False)

    manager, _launch_home = _load(
        tmp_path, monkeypatch, profile="orch", enforce_sandbox=False, plan_gate=False)
    _loaded(manager)

    monkeypatch.setenv("TERMINAL_ENV", "local")
    active_home = tmp_path / "home" / "worker-a"
    active_home.mkdir(parents=True, exist_ok=True)
    d = _gate_multiplexed(manager, "terminal", {"command": "ls"}, active_home)
    assert _act(d) != "block"
