"""AC-ING-F66-3 on v0.22.5: the staging route script's settings snapshot (UPG-F69.c erratum E3, D14 + D14.b).

The in-gateway plugin writes the loader's view of the stage settings at ``register()``; the route
script, run per delivery in a fresh interpreter the way the release webhook adapter runs it
(``gateway/platforms/webhook_filters.py`` ``run_route_script``), stages from that snapshot while
every config file it was computed from is unchanged, and from the real loader otherwise.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_yaml as yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_KEY = "nv-ingress"
PLUGIN_SRC = REPO_ROOT / "plugins" / PLUGIN_KEY
SETTINGS = {"route": "ingress", "orchestrator_profile": "orchestrator", "self_logins": ["fleet-bot"],
            "issue_labels": ["fleet"], "drain_interval_seconds": 3600}


def _write_config(root: Path, settings: dict) -> None:
    (root / "config.yaml").write_text(yaml.safe_dump({
        "plugins": {"enabled": [PLUGIN_KEY], "entries": {PLUGIN_KEY: {"settings": settings}}}}), encoding="utf-8")


@pytest.fixture
def stage_home(tmp_path, monkeypatch):
    """An isolated DEFAULT home with nv-ingress loaded, its route script rendered, and the ledger
    created by the gateway-side open."""
    from hermes_cli.plugins import PluginManager

    root = tmp_path / "root"
    (root / "plugins").mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, root / "plugins" / PLUGIN_KEY, ignore=shutil.ignore_patterns("__pycache__"))
    (root / "scripts").mkdir()
    shutil.copy(PLUGIN_SRC / "route_scripts" / "ingress_stage.py", root / "scripts" / "ingress_stage.py")
    _write_config(root, SETTINGS)
    bundled = tmp_path / "empty-bundled"
    bundled.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled is True and loaded.error is None, loaded.error
    ledger = sys.modules[f"{loaded.module.__name__}.ledger"]
    ledger.connect().close()
    snapshot = root / "plugin-data" / PLUGIN_KEY / "stage-settings.json"
    try:
        yield SimpleNamespace(root=root, ledger=ledger, snapshot=snapshot, db=ledger._db_path(),
                              envelope=sys.modules[f"{loaded.module.__name__}.envelope"])
    finally:
        manager.unload()


def _pr_event(home, delivery: str, sender: str) -> dict:
    payload = {"action": "opened", "repository": {"full_name": "o/ing-f66-snap"},
               "sender": {"login": sender, "type": "User"},
               "pull_request": {"number": 7, "head": {"sha": "a" * 40, "ref": "feat"}, "merged": False}}
    env = home.envelope.normalize("github", "pull_request", {"X-GitHub-Event": "pull_request",
                                                             "X-GitHub-Delivery": delivery}, payload)
    env["event_type"] = home.envelope.TRANSPORT_EVENT
    return env


def _run_stage(home, payload: dict) -> tuple[dict, set[str]]:
    """Run the rendered script as the adapter does (same interpreter, stdin, cwd and env factory),
    with ``-X importtime`` so the child's own imports are observable."""
    from tools.environments.local import build_subprocess_env

    script = home.root / "scripts" / "ingress_stage.py"
    proc = subprocess.run([sys.executable, "-X", "importtime", str(script)], input=json.dumps(payload),
                          capture_output=True, text=True, encoding="utf-8", timeout=120, cwd=str(script.parent),
                          env=build_subprocess_env(), check=False)
    assert proc.returncode == 0, proc.stderr[-2000:]
    imported = {line.rsplit("|", 1)[-1].strip() for line in proc.stderr.splitlines() if line.startswith("import time:")}
    return json.loads(proc.stdout), imported


def _outbox(home, delivery: str):
    with contextlib.closing(sqlite3.connect(home.db)) as conn:
        return conn.execute("SELECT kind, self_authored FROM outbox WHERE delivery_id = ?", (delivery,)).fetchall()


def _journal_mode(home) -> str:
    with contextlib.closing(sqlite3.connect(home.db)) as conn:
        return conn.execute("PRAGMA journal_mode").fetchone()[0]


def test_ac_ing_f66_3_fresh_snapshot(stage_home):
    """With a current snapshot the script stages from it: the outbox row is right, the child never
    imports the core config loader, and the ledger's journal mode is unchanged by the stage."""
    assert stage_home.snapshot.is_file(), "register() wrote no stage settings snapshot"
    mode = _journal_mode(stage_home)
    out, imported = _run_stage(stage_home, _pr_event(stage_home, "d-snap-fresh", "fleet-bot"))
    assert (out["stage"], out["self_authored"]) == ("new", True)
    assert _outbox(stage_home, "d-snap-fresh") == [("pr", 1)]
    assert "hermes_cli.config" not in imported
    assert _journal_mode(stage_home) == mode


def test_ac_ing_f66_3_no_snapshot(stage_home):
    """Without a snapshot the script stages through the real loader."""
    stage_home.snapshot.unlink()
    out, imported = _run_stage(stage_home, _pr_event(stage_home, "d-snap-none", "fleet-bot"))
    assert (out["stage"], out["self_authored"]) == ("new", True)
    assert _outbox(stage_home, "d-snap-none") == [("pr", 1)]
    assert "hermes_cli.config" in imported


def test_ac_ing_f66_3_stale_snapshot(stage_home):
    """A `self_logins` edit made after the snapshot is read through the loader, so the event is
    marked self-authored by the edited value."""
    assert stage_home.snapshot.is_file()
    written = stage_home.snapshot.stat().st_mtime_ns
    _write_config(stage_home.root, {**SETTINGS, "self_logins": ["fleet-bot", "octo-new"]})
    cfg = stage_home.root / "config.yaml"
    later = max(cfg.stat().st_mtime_ns, written) + 1_000_000_000
    os.utime(cfg, ns=(later, later))
    out, imported = _run_stage(stage_home, _pr_event(stage_home, "d-snap-stale", "octo-new"))
    assert (out["stage"], out["self_authored"]) == ("new", True)
    assert _outbox(stage_home, "d-snap-stale") == [("pr", 1)]
    assert "hermes_cli.config" in imported
