"""ISO-F10 (ADOPT-verify) — stock Hermes provides one container per session.

Behaviour contracts proving the release tree at v2026.8.31 already delivers the
per-session-container half of NanoClaw's isolation model. These tests PASS on
the stock tree — that IS the adopt claim. The fail-on-base / pass-on-head
merge-gate asymmetry is carried by the separate doc-contract test
(tests/hermes_cli/test_iso_f10_doc_contract.py), not by this file.

Mirrors the sanctioned hermetic idioms of
tests/tools/test_docker_session_isolation.py, tests/tools/test_ensure_task_env.py
and tests/tools/test_docker_find.py: env-driven resolvers with the config->env
bridge marked done, patched _create_environment, no docker daemon, no network,
nothing written under ~/.hermes.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools import terminal_tool
from tools.environments import docker as docker_mod


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Snapshot and clear every module-global these resolvers touch so tests
    control the env and no state leaks into later tests."""
    before_overrides = dict(terminal_tool._task_env_overrides)
    terminal_tool._task_env_overrides.clear()
    with terminal_tool._container_alias_lock:
        before_aliases = dict(terminal_tool._container_aliases)
        terminal_tool._container_aliases.clear()
    with terminal_tool._session_cwd_lock:
        before_cwd = dict(terminal_tool._session_cwd)
        terminal_tool._session_cwd.clear()
    with terminal_tool._env_lock:
        before_envs = dict(terminal_tool._active_environments)
        before_activity = dict(terminal_tool._last_activity)
        terminal_tool._active_environments.clear()
        terminal_tool._last_activity.clear()
    with terminal_tool._creation_locks_lock:
        before_locks = dict(terminal_tool._creation_locks)
        terminal_tool._creation_locks.clear()
    # The config->env bridge is one-shot; mark it done so explicit env wins.
    monkeypatch.setattr(terminal_tool, "_terminal_config_bridge_attempted", True)
    docker_mod._docker_executable = None
    yield
    docker_mod._docker_executable = None
    terminal_tool._task_env_overrides.clear()
    terminal_tool._task_env_overrides.update(before_overrides)
    with terminal_tool._container_alias_lock:
        terminal_tool._container_aliases.clear()
        terminal_tool._container_aliases.update(before_aliases)
    with terminal_tool._session_cwd_lock:
        terminal_tool._session_cwd.clear()
        terminal_tool._session_cwd.update(before_cwd)
    with terminal_tool._env_lock:
        terminal_tool._active_environments.clear()
        terminal_tool._active_environments.update(before_envs)
        terminal_tool._last_activity.clear()
        terminal_tool._last_activity.update(before_activity)
    with terminal_tool._creation_locks_lock:
        terminal_tool._creation_locks.clear()
        terminal_tool._creation_locks.update(before_locks)


def _enable_isolation(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_CONTAINER_PERSISTENT", "false")


def _disable_isolation(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("TERMINAL_CONTAINER_PERSISTENT", "true")


def _fake_env():
    return SimpleNamespace(
        cleanup=lambda *a, **k: None,
        execute=lambda *a, **k: {"returncode": 0, "output": ""},
    )


def test_ac_iso_f10_3(monkeypatch):
    """On the docker backend, terminal.container_persistent: false selects one-container-per-session isolation, and the default (true/unset) selects the one-shared-container-per-profile mode."""
    _enable_isolation(monkeypatch)
    assert terminal_tool._session_isolation_enabled() is True
    assert terminal_tool._docker_persistent_profile_scoped() is False
    _disable_isolation(monkeypatch)
    assert terminal_tool._session_isolation_enabled() is False
    assert terminal_tool._docker_persistent_profile_scoped() is True
    # The unset default is the persistent (profile-shared) mode too.
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.delenv("TERMINAL_CONTAINER_PERSISTENT", raising=False)
    assert terminal_tool._session_isolation_enabled() is False
    assert terminal_tool._docker_persistent_profile_scoped() is True


def test_ac_iso_f10_4(monkeypatch):
    """Under per-session isolation two distinct sessions resolve to distinct container identities; under the profile-shared default both resolve to one shared identity."""
    _enable_isolation(monkeypatch)
    a = terminal_tool._resolve_container_task_id("iso-f10-sess-a")
    b = terminal_tool._resolve_container_task_id("iso-f10-sess-b")
    assert a == "iso-f10-sess-a"
    assert b == "iso-f10-sess-b"
    assert a != b
    _disable_isolation(monkeypatch)
    assert (
        terminal_tool._resolve_container_task_id("iso-f10-sess-a")
        == terminal_tool._resolve_container_task_id("iso-f10-sess-b")
        == "default"
    )


def test_ac_iso_f10_5(monkeypatch):
    """Under per-session isolation each session gets and REUSES its own container (one per session, not per turn) and tearing one down leaves another's intact; under the default two sessions share one container."""
    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", lambda: None)

    _enable_isolation(monkeypatch)
    fake_a, fake_b = _fake_env(), _fake_env()
    with patch.object(terminal_tool, "_create_environment",
                      side_effect=[fake_a, fake_b]) as create:
        assert terminal_tool.ensure_task_env("iso-f10-sess-a") is fake_a
        assert terminal_tool.ensure_task_env("iso-f10-sess-a") is fake_a
        assert create.call_count == 1
        assert terminal_tool.ensure_task_env("iso-f10-sess-b") is fake_b
        assert create.call_count == 2
        assert [c.kwargs["task_id"] for c in create.call_args_list] == [
            "iso-f10-sess-a", "iso-f10-sess-b"]
        terminal_tool.cleanup_vm("iso-f10-sess-a")
        assert terminal_tool.get_active_env("iso-f10-sess-a") is None
        assert terminal_tool.get_active_env("iso-f10-sess-b") is fake_b

    with terminal_tool._env_lock:
        terminal_tool._active_environments.clear()
        terminal_tool._last_activity.clear()
    with terminal_tool._creation_locks_lock:
        terminal_tool._creation_locks.clear()

    _disable_isolation(monkeypatch)
    fake_shared = _fake_env()
    with patch.object(terminal_tool, "_create_environment",
                      side_effect=[fake_shared]) as create:
        env_a = terminal_tool.ensure_task_env("iso-f10-sess-a")
        env_b = terminal_tool.ensure_task_env("iso-f10-sess-b")
        assert env_a is env_b is fake_shared
        assert create.call_count == 1
        assert create.call_args_list[0].kwargs["task_id"] == "default"


def test_ac_iso_f10_6(monkeypatch, tmp_path):
    """Podman is honored as a drop-in container runtime for the per-session sandbox: an explicit HERMES_DOCKER_BINARY override is used, and podman on PATH is accepted when docker is absent."""
    podman = tmp_path / "podman"
    podman.write_text("#!/bin/sh\n")
    podman.chmod(0o755)
    monkeypatch.setenv("HERMES_DOCKER_BINARY", str(podman))
    assert docker_mod.find_docker() == str(podman)

    docker_mod._docker_executable = None
    monkeypatch.delenv("HERMES_DOCKER_BINARY", raising=False)

    def _which(name):
        return "/usr/bin/podman" if name == "podman" else None

    with patch("tools.environments.docker.shutil.which", side_effect=_which):
        assert docker_mod.find_docker() == "/usr/bin/podman"
