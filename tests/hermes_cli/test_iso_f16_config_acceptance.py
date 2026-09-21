"""Acceptance test for requirement ISO-F16 — Per-group container config and multi-provider runner.

CONFIGURE / adopt-native row: no plugin is loaded. These tests prove that Hermes's
native per-profile ``config.yaml`` + profile distributions + provider-neutral session
storage + kanban per-card overrides satisfy the requirement.

conftest sandboxes HERMES_HOME, sets HERMES_DISABLE_LAZY_INSTALLS=1 and TZ=UTC, and resets
the plugin singleton (tests/conftest.py:494,520,545). load_config()'s cache is keyed by
config path (config.py:294), so repointing HERMES_HOME per profile resolves each cleanly.
"""

import json
import shutil
from pathlib import Path

import pytest
import yaml

from hermes_cli.config import load_config
from hermes_cli.profile_distribution import install_distribution, update_distribution
from hermes_cli.profiles import get_profile_dir
from hermes_cli import kanban_db
from hermes_state import SessionDB

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "iso_f16"


def _staged(tmp_path: Path, role: str, tag: str = "a") -> Path:
    dest = tmp_path / f"src-{role}-{tag}"
    shutil.copytree(FIXTURES / role, dest)
    return dest


def _install(tmp_path: Path, role: str, name: str, tag: str = "i") -> Path:
    install_distribution(str(_staged(tmp_path, role, tag)), name=name)
    return get_profile_dir(name)


def _installed_raw(profile_dir: Path) -> dict:
    return yaml.safe_load((profile_dir / "config.yaml").read_text(encoding="utf-8"))


def _resolved(monkeypatch, profile_dir: Path) -> dict:
    monkeypatch.setenv("HERMES_HOME", str(profile_dir))
    return load_config()


def test_ac_iso_f16_1(tmp_path, monkeypatch):
    """A coworker's model, provider, effort, fast-mode and fallback chain resolve from that
    profile's own config.yaml, and two profiles resolve independent values (no shared runner)."""
    orch = _install(tmp_path, "orchestrator", "iso-f16-orch", tag="1o")
    work = _install(tmp_path, "worker", "iso-f16-work", tag="1w")
    raw_orch, raw_work = _installed_raw(orch), _installed_raw(work)

    a = _resolved(monkeypatch, orch)
    b = _resolved(monkeypatch, work)

    for cfg, raw in ((a, raw_orch), (b, raw_work)):
        assert cfg["model"]["default"] == raw["model"]["default"]
        assert cfg["model"]["provider"] == raw["model"]["provider"]
        assert cfg["fallback_providers"] == raw["fallback_providers"]
        assert cfg["providers"] == raw["providers"]
        assert cfg["agent"]["reasoning_effort"] == raw["agent"]["reasoning_effort"]
        assert cfg["agent"]["service_tier"] == raw["agent"]["service_tier"]

    assert a["model"]["default"] != b["model"]["default"]
    assert a["model"]["provider"] != b["model"]["provider"]
    assert a["providers"] != b["providers"]
    assert a["fallback_providers"] != b["fallback_providers"]
    assert a["agent"]["reasoning_effort"] != b["agent"]["reasoning_effort"]
    assert a["agent"]["service_tier"] != b["agent"]["service_tier"]


def test_ac_iso_f16_2(tmp_path, monkeypatch):
    """A coworker's container config (terminal.*, mcp_servers, timezone, toolsets) resolves
    per-profile from its own config.yaml, with two profiles disjoint (config-level only —
    runtime terminal/MCP isolation is process-global, owned by ISO-F15/GOV-F27/CRED-F28)."""
    orch = _install(tmp_path, "orchestrator", "iso-f16-orch", tag="2o")
    work = _install(tmp_path, "worker", "iso-f16-work", tag="2w")
    raw_orch, raw_work = _installed_raw(orch), _installed_raw(work)

    a = _resolved(monkeypatch, orch)
    b = _resolved(monkeypatch, work)

    for cfg, raw in ((a, raw_orch), (b, raw_work)):
        assert cfg["terminal"]["backend"] == raw["terminal"]["backend"]
        assert cfg["terminal"]["docker_image"] == raw["terminal"]["docker_image"]
        assert cfg["terminal"]["docker_volumes"] == raw["terminal"]["docker_volumes"]
        assert cfg["terminal"]["docker_mount_cwd_to_workspace"] == raw["terminal"]["docker_mount_cwd_to_workspace"]
        assert cfg["mcp_servers"] == raw["mcp_servers"]
        assert cfg["timezone"] == raw["timezone"]
        assert cfg["toolsets"] == raw["toolsets"]

    assert a["terminal"]["docker_image"] != b["terminal"]["docker_image"]
    assert set(a["terminal"]["docker_volumes"]).isdisjoint(b["terminal"]["docker_volumes"])
    assert set(a["mcp_servers"]).isdisjoint(b["mcp_servers"])
    assert a["timezone"] != b["timezone"]
    assert set(a["toolsets"]).isdisjoint(b["toolsets"])


def test_ac_iso_f16_3(tmp_path):
    """A profile distribution ships and reverts config: install copies config.yaml; plain
    update preserves a local edit; --force-config reverts it; a second fresh install of the
    same source yields a byte-identical config.yaml (distribution.yaml provenance excluded)."""
    src = _staged(tmp_path, "orchestrator", tag="3a")
    dist_bytes = (src / "config.yaml").read_bytes()
    install_distribution(str(src), name="iso-f16-a")
    cfg = get_profile_dir("iso-f16-a") / "config.yaml"
    assert cfg.read_bytes() == dist_bytes

    cfg.write_text(cfg.read_text(encoding="utf-8") + "\n# local edit\n", encoding="utf-8")
    edited = cfg.read_bytes()
    update_distribution("iso-f16-a", force_config=False)
    assert cfg.read_bytes() == edited
    update_distribution("iso-f16-a", force_config=True)
    assert cfg.read_bytes() == dist_bytes

    install_distribution(str(_staged(tmp_path, "orchestrator", tag="3b")), name="iso-f16-b")
    assert (get_profile_dir("iso-f16-b") / "config.yaml").read_bytes() == dist_bytes


def test_ac_iso_f16_4(tmp_path):
    """Session storage is provider-neutral across a mid-session provider switch: a session
    created under provider A keeps its id after update_session_model and resolves provider B."""
    with SessionDB(db_path=tmp_path / "state.db") as db:
        # model_config is a dict (serialized internally); billing_provider is not a kwarg.
        db.create_session(
            session_id="s1",
            source="cli",
            model="model-a",
            model_config={"model": "model-a", "provider": "prov-a"},
        )
        before = db.get_session("s1")
        assert before is not None
        assert json.loads(before["model_config"])["provider"] == "prov-a"

        db.update_session_model("s1", "model-b", provider="prov-b")
        row = db.get_session("s1")
        assert row is not None
        assert row["id"] == "s1"
        assert row["model"] == "model-b"
        mc = json.loads(row["model_config"])
        assert mc["model"] == "model-b"
        assert mc["provider"] == "prov-b"


def test_ac_iso_f16_5(tmp_path):
    """Kanban overrides model, provider and reasoning effort per card; a bare provider is
    rejected; clearing model+effort resets the card to the profile default."""
    conn = kanban_db.connect(db_path=tmp_path / "kanban.db")
    try:
        tid = kanban_db.create_task(conn, title="t", assignee="worker")

        assert kanban_db.set_model_override(conn, tid, "model-x", provider="prov-y")
        task = kanban_db.get_task(conn, tid)
        assert task.model_override == "model-x"
        assert task.provider_override == "prov-y"

        assert kanban_db.set_reasoning_effort(conn, tid, "high")
        assert kanban_db.get_task(conn, tid).reasoning_effort == "high"

        with pytest.raises(ValueError):
            kanban_db.set_model_override(conn, tid, None, provider="prov-z")

        kanban_db.set_model_override(conn, tid, "", provider=None)
        kanban_db.set_reasoning_effort(conn, tid, None)
        cleared = kanban_db.get_task(conn, tid)
        assert cleared.model_override is None
        assert cleared.provider_override is None
        assert cleared.reasoning_effort is None
    finally:
        conn.close()


def test_iso_f16_model_normalization_canonicalizes_raw_form(tmp_path, monkeypatch):
    """A raw-form model:{name, provider} canonicalizes to model.default/provider (the
    normalization clause of AC-ISO-F16-1)."""
    home = tmp_path / "iso-f16-rawform"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n  name: raw-model-id\n  provider: raw-prov\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    cfg = load_config()
    assert cfg["model"]["default"] == "raw-model-id"
    assert cfg["model"]["provider"] == "raw-prov"
    assert "name" not in cfg["model"]


def test_iso_f16_per_profile_resolution_is_path_keyed_not_size_keyed(tmp_path, monkeypatch):
    """Two profiles whose config.yaml files share an identical (mtime_ns, size) signature
    but differ in content each resolve their own model — the loader keys resolution by full
    config path, so dropping the path from the cache key would be caught here.
    """
    a = tmp_path / "iso-f16-cache-a"
    b = tmp_path / "iso-f16-cache-b"
    a.mkdir()
    b.mkdir()
    (a / "config.yaml").write_text(
        "model:\n  default: aaaaaaaa\n  provider: pa\n", encoding="utf-8"
    )
    (b / "config.yaml").write_text(
        "model:\n  default: bbbbbbbb\n  provider: pb\n", encoding="utf-8"
    )
    shutil.copystat(a / "config.yaml", b / "config.yaml")
    sa, sb = (a / "config.yaml").stat(), (b / "config.yaml").stat()
    assert (sa.st_mtime_ns, sa.st_size) == (sb.st_mtime_ns, sb.st_size)

    monkeypatch.setenv("HERMES_HOME", str(a))
    cfg_a = load_config()
    monkeypatch.setenv("HERMES_HOME", str(b))
    cfg_b = load_config()

    assert cfg_a["model"]["default"] == "aaaaaaaa"
    assert cfg_b["model"]["default"] == "bbbbbbbb"
    assert cfg_a["model"]["default"] != cfg_b["model"]["default"]
