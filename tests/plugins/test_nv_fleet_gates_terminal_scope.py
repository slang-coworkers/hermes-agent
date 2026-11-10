"""nv-fleet-gates' sandbox predicate resolves TERMINAL_* the way the terminal tool does.

A Hermes 0.22.5 kanban/managed worker for a non-launch profile binds that profile's terminal.* in a per-profile
terminal scope (tools/terminal_scope.py) and its os.environ carries no TERMINAL_*. The gate must read the scope first,
then the process env, with the tool's defaults, and keep failing closed: an ssh backend whose host is this profile's
managed expected_ssh_host is allowed; a local backend, another host, no host, a refusal scope, or nothing at all is
refused. Behaviour contract only: no source reads, no network, nothing under ~/.hermes.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import hermes_yaml as yaml
import pytest

from hermes_cli.plugins import PluginManager

PLUGIN_KEY = "nv-fleet-gates"
PLUGIN_SRC = Path(__file__).resolve().parents[2] / "plugins" / PLUGIN_KEY
HOST = "openshell-scw-worker-a.default"


@pytest.fixture
def gate(tmp_path, monkeypatch):
    home = tmp_path / "home" / "worker-a"
    (home / "plugins").mkdir(parents=True)
    bundled = tmp_path / "empty-bundled"
    bundled.mkdir()
    shutil.copytree(PLUGIN_SRC, home / "plugins" / PLUGIN_KEY)
    roles = {"worker-a": "worker", "orch": "orchestrator"}
    cfg = {"plugins": {"enabled": [PLUGIN_KEY], "entries": {PLUGIN_KEY: {"settings": {
        "profile_roles": roles,
        "edges_db_path": str(tmp_path / "fleet-edges.db"),
        "plan_gate": False,
        "enforce_sandbox": True,
        "expected_backend": "ssh",
        "skills_root": str(home / "skills"),
        "shared_learnings_path": str(tmp_path / "shared-learnings"),
    }}}}}
    (home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text(yaml.safe_dump({"plugins": {"entries": {PLUGIN_KEY: {"settings": {
        "profile_roles": roles, "expected_ssh_host": {"worker-a": HOST}}}}}}), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    monkeypatch.delenv("HERMES_ENABLE_PROJECT_PLUGINS", raising=False)
    for k in ("TERMINAL_ENV", "TERMINAL_SSH_HOST"):
        monkeypatch.delenv(k, raising=False)
    from hermes_cli import managed_scope

    managed_scope.invalidate_managed_cache()
    # The gate-time backstop blocks every call while restricted core is unregistered.
    import model_tools

    model_tools.discover_builtin_tools()
    import tools.terminal_tool as tt

    ready = {"n": 0}

    def _ready(*a, **k):
        ready["n"] += 1
        return object()

    monkeypatch.setattr(tt, "ensure_task_env", _ready)
    mgr = PluginManager()
    mgr.discover_and_load()
    loaded = mgr._plugins[PLUGIN_KEY]
    assert loaded.enabled is True and loaded.error is None
    yield mgr, ready
    managed_scope.invalidate_managed_cache()


def _block(mgr, scope=None):
    """The block message for a terminal call, under a bound terminal scope when *scope* is given."""
    from tools.terminal_scope import reset_terminal_scope, set_terminal_scope

    token = set_terminal_scope(scope) if scope is not None else None
    try:
        results = mgr.invoke_hook("pre_tool_call", tool_name="terminal", args={"command": "echo hi"},
                                  session_id="s1", task_id="t", tool_call_id="tc", turn_id="tn")
    finally:
        if token is not None:
            reset_terminal_scope(token)
    for r in results or []:
        if isinstance(r, dict) and r.get("action") == "block":
            return r.get("message")
    return None


def test_a_bound_scope_with_the_profiles_ssh_host_is_allowed_without_any_terminal_env(gate):
    mgr, ready = gate
    assert _block(mgr, {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": HOST}) is None
    assert ready["n"] == 1


@pytest.mark.parametrize("scope,needle", [
    ({"TERMINAL_ENV": "local", "TERMINAL_SSH_HOST": HOST}, "resolved backend 'local' != expected 'ssh'"),
    ({}, "resolved backend 'local' != expected 'ssh'"),  # a key the scope omits takes the tool's default
    ({"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": "openshell-scw-sibling.default"}, "is not this profile's sandbox"),
    ({"TERMINAL_ENV": "ssh"}, "is not this profile's sandbox"),
])
def test_a_bound_scope_with_a_local_backend_or_another_host_is_refused(gate, scope, needle):
    mgr, ready = gate
    msg = _block(mgr, scope)
    assert msg and needle in msg, msg
    assert ready["n"] == 0  # refused before the task env is touched


def test_the_scope_wins_over_the_process_env(gate, monkeypatch):
    mgr, _ = gate
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_SSH_HOST", HOST)
    msg = _block(mgr, {"TERMINAL_ENV": "local"})
    assert msg and "resolved backend 'local'" in msg
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.delenv("TERMINAL_SSH_HOST")
    assert _block(mgr, {"TERMINAL_ENV": "ssh", "TERMINAL_SSH_HOST": HOST}) is None


def test_a_refusal_scope_is_refused(gate):
    from tools.terminal_scope import TerminalPolicyRefusal

    mgr, ready = gate
    msg = _block(mgr, TerminalPolicyRefusal("config.yaml unparseable"))
    assert msg and "terminal policy is unavailable" in msg and ready["n"] == 0


def test_without_a_scope_the_process_env_decides_as_before(gate, monkeypatch):
    mgr, _ = gate
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("TERMINAL_SSH_HOST", HOST)
    assert _block(mgr) is None
    monkeypatch.setenv("TERMINAL_SSH_HOST", "openshell-scw-sibling.default")
    assert "is not this profile's sandbox" in (_block(mgr) or "")
    monkeypatch.setenv("TERMINAL_ENV", "local")
    assert "resolved backend 'local'" in (_block(mgr) or "")


def test_neither_a_scope_nor_a_terminal_env_is_refused(gate):
    mgr, ready = gate
    msg = _block(mgr)
    assert msg and "resolved backend 'local' != expected 'ssh'" in msg and ready["n"] == 0
