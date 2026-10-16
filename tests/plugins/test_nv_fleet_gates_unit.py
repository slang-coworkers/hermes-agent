"""Host-writer containment for nv-fleet-gates (LOOP-F37-7's ``predicates.within``) on symlinks.

On 3.14 ``Path.resolve()`` returns a symlink loop unresolved instead of raising (3.11/3.12), so a
loop target would read as in-tree. These pin that a skill_manage target which cannot be shown
in-tree (a loop, a dangling link) is blocked for a worker, while an in-tree target, existing or
not yet created, is allowed. Behaviour contract only: no source reads, no network, nothing
under ~/.hermes.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import hermes_yaml as yaml

from hermes_cli.plugins import PluginManager

PLUGIN_KEY = "nv-fleet-gates"
PLUGIN_SRC = Path(__file__).resolve().parents[2] / "plugins" / PLUGIN_KEY


@pytest.fixture
def worker(tmp_path, monkeypatch):
    home = tmp_path / "home" / "worker-a"
    (home / "plugins").mkdir(parents=True)
    bundled = tmp_path / "empty-bundled"
    bundled.mkdir()
    shutil.copytree(PLUGIN_SRC, home / "plugins" / PLUGIN_KEY)
    skills = home / "skills"
    skills.mkdir()
    cfg = {"plugins": {"enabled": [PLUGIN_KEY], "entries": {PLUGIN_KEY: {"settings": {
        "profile_roles": {"worker-a": "worker", "orch": "orchestrator"},
        "edges_db_path": str(tmp_path / "fleet-edges.db"),
        "plan_gate": False,
        "enforce_sandbox": False,
        "skills_root": str(skills),
        "shared_learnings_path": str(tmp_path / "shared-learnings"),
        "stage_markers": ["[Fix Report]"],
        "required_stages": ["OUTPUT_REVIEW"],
    }}}}}
    (home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.delenv("HERMES_ENABLE_PROJECT_PLUGINS", raising=False)
    # The gate-time backstop blocks every call while restricted core is unregistered, which
    # would make the block assertions below pass for the wrong reason.
    import model_tools
    model_tools.discover_builtin_tools()
    mgr = PluginManager()
    mgr.discover_and_load()
    loaded = mgr._plugins[PLUGIN_KEY]
    assert loaded.enabled is True
    assert loaded.error is None
    return mgr, skills


def _skill_write(mgr, file_path):
    results = mgr.invoke_hook(
        "pre_tool_call", tool_name="skill_manage",
        args={"operations": [{"name": "x", "action": "write_file",
                              "file_path": file_path, "file_content": "y"}]},
        session_id="s1", task_id="t", tool_call_id="tc", turn_id="tn")
    directives = [r for r in (results or []) if isinstance(r, dict) and r.get("action")]
    return directives[0] if directives else None


def _is_host_writer_block(d):
    return isinstance(d, dict) and d.get("action") == "block" and "host-writer" in d.get("message", "")


@pytest.mark.platforms("posix")
def test_symlink_loop_target_blocked_for_worker(worker):
    """A skill_manage target under a symlink loop inside the skills tree is blocked."""
    mgr, skills = worker
    (skills / "loop1").symlink_to(skills / "loop2")
    (skills / "loop2").symlink_to(skills / "loop1")
    assert _is_host_writer_block(_skill_write(mgr, "loop1/SKILL.md"))


@pytest.mark.platforms("posix")
def test_dangling_link_target_blocked_for_worker(worker):
    """A skill_manage target through a dangling in-tree link cannot be shown in-tree and is blocked."""
    mgr, skills = worker
    (skills / "dangling").symlink_to(skills / "not-there")
    assert _is_host_writer_block(_skill_write(mgr, "dangling/SKILL.md"))


def test_in_tree_real_path_allowed(worker):
    """A skill_manage target that exists inside the skills tree is allowed."""
    mgr, skills = worker
    (skills / "x").mkdir()
    (skills / "x" / "SKILL.md").write_text("y", encoding="utf-8")
    d = _skill_write(mgr, "x/SKILL.md")
    assert not (isinstance(d, dict) and d.get("action") == "block")


def test_in_tree_path_not_yet_created_allowed(worker):
    """A skill_manage target inside the skills tree that does not exist yet is allowed."""
    mgr, skills = worker
    assert not (skills / "new").exists()
    d = _skill_write(mgr, "new/SKILL.md")
    assert not (isinstance(d, dict) and d.get("action") == "block")
