"""Unit behaviour of the FLEET-F62.d installer and render edges the acceptance test does not reach."""

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALLER = REPO_ROOT / "plugins" / "nv-coworker-compose" / "openshell" / "installer.py"
SPEC = REPO_ROOT / "tests" / "e2e-scenarios" / "FLEET-F62.c" / "spec" / "openshell" / "coworker-types.yaml"
FLEET_PLUGINS = ("nv-coworker-compose", "nv-fleet-gates")


def _installer():
    spec = importlib.util.spec_from_file_location("fleet_f62d_installer_unit", INSTALLER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.linux_only
def test_ssh_include_is_matched_case_insensitively(tmp_path, monkeypatch):
    """An operator's own `include`/`INCLUDE=` line for the managed file is folded into the single
    first-line Include, so the directive stays exactly once across reruns."""
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "config").write_text(
        "Host op\n    User op\ninclude config.d/openshell-p.conf\nINCLUDE=config.d/openshell-p.conf\n",
        encoding="utf-8")
    inst = _installer()
    block = "Host openshell-p-fixer\n    User sandbox\n"
    inst._persist_ssh_config("p-fixer", "p", block)
    first = (ssh / "config").read_text(encoding="utf-8")
    inst._persist_ssh_config("p-fixer", "p", block)
    assert (ssh / "config").read_text(encoding="utf-8") == first
    lines = first.splitlines()
    assert lines[0] == "Include config.d/openshell-p.conf"
    assert [ln for ln in lines if ln.strip().lower().replace("=", " ").startswith("include ")] == [lines[0]]
    assert "Host op" in lines


def test_snapshot_migration_refuses_an_existing_destination(tmp_path):
    """A legacy snapshot is never moved over an existing out-of-scan snapshot; nothing moves."""
    inst = _installer()
    plugins = tmp_path / "home" / "plugins"
    legacy = plugins / ("probe" + inst._BACKUP_SUFFIX)
    legacy.mkdir(parents=True)
    (legacy / "plugin.yaml").write_text("name: probe\n", encoding="utf-8")
    taken = Path(inst._snapshot_dir(str(plugins))) / "probe"
    taken.mkdir(parents=True)
    with pytest.raises(ValueError):
        inst._migrate_legacy_snapshots([str(legacy)])
    assert legacy.is_dir()


def test_rendered_base_helper_runs_from_the_synced_skill_tree(tmp_path):
    """The rendered fixer's nv-path-guard-base helper answers when invoked from a separate checkout
    directory, the way a worker runs it out of its synced ~/.hermes/skills tree."""
    home = tmp_path / "home" / ".hermes"
    (home / "plugins").mkdir(parents=True)
    for key in FLEET_PLUGINS:
        shutil.copytree(REPO_ROOT / "plugins" / key, home / "plugins" / key)
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": list(FLEET_PLUGINS)}}),
                                      encoding="utf-8")
    (tmp_path / "empty-bundled").mkdir()
    env = dict(os.environ, HOME=str(home.parent), HERMES_HOME=str(home),
               HERMES_BUNDLED_PLUGINS=str(tmp_path / "empty-bundled"), HERMES_ENABLE_PROJECT_PLUGINS="0")
    out = tmp_path / "out"
    proc = subprocess.run([sys.executable, "-m", "hermes_cli.main", "coworker", "compose", str(SPEC), "--out", str(out)],
                          capture_output=True, text=True, env=env, cwd=str(REPO_ROOT))
    assert proc.returncode == 0, proc.stderr[-2000:]
    remote_skills = tmp_path / "remote" / ".hermes" / "skills"
    shutil.copytree(out / "fixer" / "skills", remote_skills)
    checkout = tmp_path / "checkout"
    (checkout / ".github" / "nv-path-guard").mkdir(parents=True)
    (checkout / ".github" / "nv-path-guard" / "nv-main.txt").write_text("src/**\n", encoding="utf-8")
    (checkout / ".github" / "nv-path-guard" / "nv-docs.txt").write_text("docs/**\n", encoding="utf-8")
    script = remote_skills / "nv-path-guard-base" / "scripts" / "pick_base.py"
    got = subprocess.run([sys.executable, str(script), "--repo", ".", "src/a.ts"],
                         capture_output=True, text=True, cwd=str(checkout), timeout=30)
    assert got.returncode == 0 and got.stdout.strip() == "nv-main", (got.returncode, got.stdout, got.stderr)
