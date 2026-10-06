"""The F62.c worker policies let the sandbox user write where the image says it works."""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
FLEET_PLUGINS = ("nv-coworker-compose", "nv-fleet-gates")
DOCKERFILE = REPO_ROOT / "plugins" / "nv-coworker-compose" / "openshell" / "Dockerfile.worker"
SPEC = REPO_ROOT / "tests" / "e2e-scenarios" / "FLEET-F62.c" / "spec" / "openshell" / "coworker-types.yaml"
COWORKERS = ("orchestrator", "triager", "fixer", "reviewer", "approver")


def _image_workdir():
    logical = re.sub(r"\\\s*\n", " ", DOCKERFILE.read_text(encoding="utf-8"))
    dirs = re.findall(r"(?mi)^\s*WORKDIR\s+(\S+)\s*$", logical)
    assert dirs, "Dockerfile.worker must set a WORKDIR"
    return dirs[-1]


def _render(tmp_path):
    home = tmp_path / "home" / ".hermes"
    (home / "plugins").mkdir(parents=True)
    for key in FLEET_PLUGINS:
        shutil.copytree(REPO_ROOT / "plugins" / key, home / "plugins" / key)
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": list(FLEET_PLUGINS)}}), encoding="utf-8")
    (tmp_path / "empty-bundled").mkdir()
    env = dict(os.environ, HOME=str(home.parent), HERMES_HOME=str(home),
               HERMES_BUNDLED_PLUGINS=str(tmp_path / "empty-bundled"), HERMES_ENABLE_PROJECT_PLUGINS="0")
    out = tmp_path / "out"
    proc = subprocess.run([sys.executable, "-m", "hermes_cli.main", "coworker", "compose", str(SPEC), "--out", str(out)],
                          capture_output=True, text=True, env=env, cwd=str(REPO_ROOT), stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, proc.stderr[-2000:]
    return out


def _covered(path, entries):
    p = path.rstrip("/")
    return any(p == e.rstrip("/") or p.startswith(e.rstrip("/") + "/") for e in entries)


def test_every_worker_policy_can_write_the_image_workdir(tmp_path):
    """OpenShell's include_workdir does not grant write on the image's WORKDIR, so a clone there
    (`git clone … /workspace/scratch`) fails with EACCES unless read_write covers it."""
    workdir = _image_workdir()
    out = _render(tmp_path)
    for role in COWORKERS:
        policy = yaml.safe_load((out / role / f"policy-{role}.yaml").read_text(encoding="utf-8")) or {}
        rw = (policy.get("filesystem_policy") or {}).get("read_write") or []
        assert _covered(workdir, rw), f"{role}: image WORKDIR {workdir} must be inside read_write {rw}"
