"""Unit tests for the RT-F01 render enforcement in nv-coworker-compose.

These complement (do not overlap) the acceptance test: they pin behaviours the
per-criterion drives only exercise for the ``webhook`` platform, so a future
refactor that special-cased ``webhook`` could not silently pass. The plugin is
loaded from an isolated HERMES_HOME with an empty bundled dir through the real
``PluginManager().discover_and_load()`` path, matching the acceptance shape.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_KEY = "nv-coworker-compose"
PLUGIN_SRC = REPO_ROOT / "plugins" / PLUGIN_KEY


@pytest.fixture()
def module(tmp_path, monkeypatch):
    from hermes_cli.plugins import PluginManager

    hermes_home = tmp_path / "hermes_home"
    (hermes_home / "plugins").mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, hermes_home / "plugins" / PLUGIN_KEY)
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": [PLUGIN_KEY]}}), encoding="utf-8"
    )
    bundled = tmp_path / "empty_bundled"
    bundled.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")

    manager = PluginManager()
    manager.discover_and_load()
    entry = manager._plugins[PLUGIN_KEY]
    assert entry.enabled is True and entry.error is None and entry.module is not None
    return entry.module


def _write_spec(spec_dir: Path, spec: dict) -> Path:
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spines").mkdir(exist_ok=True)
    (spec_dir / "spines" / "base.yaml").write_text(
        yaml.safe_dump({"identity": "Base.", "invariants": ["Help."],
                        "config": {"terminal": {"container_persistent": True}}}, sort_keys=False),
        encoding="utf-8",
    )
    wf = spec_dir / "workflows" / "wf"
    wf.mkdir(parents=True, exist_ok=True)
    (wf / "SKILL.md").write_text("# wf\n\nWork.\n", encoding="utf-8")
    path = spec_dir / "coworker-types.yaml"
    path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    return path


def _spec() -> dict:
    return {
        "workspace_root": "/data/coworkers",
        "orchestrator_profile": "orchestrator",
        "default_profile": "default",
        "spines": {"base": {"source": "spines/base.yaml"}},
        "default_config": {},
        "types": {
            "orchestrator": {"extends": ["base"], "identity": "Orch.", "workflows": ["wf"], "config": {}},
            "worker": {"extends": ["base"], "identity": "Work.", "workflows": ["wf"], "config": {}},
        },
    }


def _render(module, tmp_path, spec, tag):
    module.compose(str(_write_spec(tmp_path / f"spec_{tag}", spec)), str(tmp_path / f"out_{tag}"))


def test_layout_refuses_noncanonical_non_webhook_platform(module, tmp_path):
    """The canonical-layout check is platform-generic, not webhook-specific: a
    coworker spelling a NON-port-binding, non-webhook platform (discord) under a
    gateway.<platform> alias is refused just as a webhook alias is."""
    spec = _spec()
    spec["types"]["worker"]["config"] = {"gateway": {"discord": {"enabled": True}}}
    with pytest.raises(module.CompositionError):
        _render(module, tmp_path, spec, "discord_alias")


def test_layout_accepts_canonical_non_port_binding_platform(module, tmp_path):
    """A coworker enabling a non-port-binding platform under the canonical
    platforms.<x> map renders — the layout/port-binding checks refuse only
    aliases and port binders, not every platform."""
    spec = _spec()
    spec["types"]["worker"]["config"] = {"platforms": {"discord": {"enabled": True}}}
    _render(module, tmp_path, spec, "discord_canonical")


@pytest.mark.parametrize("badkey", ["Webhook", " webhook "])
def test_layout_refuses_noncanonical_platform_key_spelling(module, tmp_path, badkey):
    """A platform key spelled non-canonically under 'platforms' (mixed case or
    surrounding whitespace) is refused. Core normalizes it to the real platform
    and would enable the listener, so the canonical-name gate must catch it before
    the raw-key port-binding check (which matches only the canonical value)."""
    spec = _spec()
    spec["types"]["worker"]["config"] = {"platforms": {badkey: {"enabled": True}}}
    with pytest.raises(module.CompositionError):
        _render(module, tmp_path, spec, f"badkey_{badkey.strip()}")


@pytest.mark.parametrize("bad_extra", ["bad", None])
def test_layout_refuses_non_mapping_extra_on_coworker(module, tmp_path, bad_extra):
    """A platform block whose present 'extra' is not a mapping — a scalar or an
    explicit null — is refused at render time. Left in place it would be written
    to config.yaml, and the runtime loader cannot merge that shape: it drops the
    platform config, silently discarding the enforced allowlist."""
    spec = _spec()
    spec["types"]["worker"]["config"] = {"platforms": {"telegram": {"enabled": True, "extra": bad_extra}}}
    with pytest.raises(module.CompositionError):
        _render(module, tmp_path, spec, f"cw_bad_extra_{bad_extra}")


@pytest.mark.parametrize("bad_extra", ["bad", None])
def test_layout_refuses_non_mapping_extra_on_default(module, tmp_path, bad_extra):
    """Same rejection on the DEFAULT profile's platform block."""
    spec = _spec()
    spec["default_config"] = {"platforms": {"telegram": {"enabled": True, "extra": bad_extra}}}
    with pytest.raises(module.CompositionError):
        _render(module, tmp_path, spec, f"def_bad_extra_{bad_extra}")


def test_enforce_multiplex_sets_roster_when_spec_is_silent(module, tmp_path):
    """A spec that never mentions multiplexing still renders a DEFAULT config
    whose gateway.multiplex_profiles is True, carries no retired allowlist, and,
    once the roster is installed and the rest parked, the multiplexer serves
    exactly default + the declaration-order roster — enforcement is explicit,
    never by assumption."""
    import os
    from hermes_cli.profiles import profiles_to_serve

    out = tmp_path / "out_silent"
    module.compose(str(_write_spec(tmp_path / "spec_silent", _spec())), str(out))
    default_cfg = yaml.safe_load((out / "default" / "config.yaml").read_text(encoding="utf-8"))
    gateway = default_cfg["gateway"]
    assert gateway["multiplex_profiles"] is True
    assert "multiplex_profile_allowlist" not in gateway and "multiplex_profile_allowlist" not in default_cfg
    profiles_root = Path(os.environ["HERMES_HOME"]) / "profiles"
    for name in ("orchestrator", "worker"):
        shutil.copytree(out / name, profiles_root / name)
    (profiles_root / "stray").mkdir()
    (profiles_root / "stray" / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    module.park_non_fleet_profiles(["orchestrator", "worker"])
    assert [name for name, _ in profiles_to_serve(True)] == ["default", "orchestrator", "worker"]


def test_onboard_parks_non_fleet_profiles(module, tmp_path, monkeypatch):
    """The onboard path scopes the multiplexer to the fleet: after `_run_onboard`, a live
    non-fleet profile is parked and the served set is exactly default + the roster."""
    import os
    from hermes_cli.profiles import profile_is_parked, profiles_to_serve

    profiles_root = Path(os.environ["HERMES_HOME"]) / "profiles"
    (profiles_root / "stray").mkdir(parents=True)
    (profiles_root / "stray" / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    assert "stray" in {name for name, _ in profiles_to_serve(True)}

    monkeypatch.setattr(module, "_install", lambda src, name: shutil.copytree(src, profiles_root / name))
    monkeypatch.setattr(module, "_profile_revisions", lambda *a, **k: {})
    monkeypatch.setattr(module, "_configure_bot_meta", lambda *a, **k: True)
    monkeypatch.setattr(module, "_ensure_canonical_bot_chat", lambda *a, **k: True)
    result = module._run_onboard(str(_write_spec(tmp_path / "spec_onboard", _spec())))

    assert result == {"ok": True, "resumed_cron_jobs": [], "kept_paused_cron_jobs": []}, result
    assert [name for name, _ in profiles_to_serve(True)] == ["default", "orchestrator", "worker"]
    assert profile_is_parked(profiles_root / "stray")
    assert not profile_is_parked(profiles_root / "orchestrator") and not profile_is_parked(profiles_root / "worker")


def test_onboard_failed_install_parks_nothing(module, tmp_path, monkeypatch):
    """A failed coworker install fails the onboard and leaves every non-fleet profile served:
    an incomplete fleet must not take working profiles offline."""
    import os
    from hermes_cli.profiles import profile_is_parked, profiles_to_serve

    profiles_root = Path(os.environ["HERMES_HOME"]) / "profiles"
    (profiles_root / "stray").mkdir(parents=True)
    (profiles_root / "stray" / "config.yaml").write_text("model: {}\n", encoding="utf-8")

    def _install(src, name):
        if name == "worker":
            raise RuntimeError("install refused")
        shutil.copytree(src, profiles_root / name)

    monkeypatch.setattr(module, "_install", _install)
    monkeypatch.setattr(module, "_profile_revisions", lambda *a, **k: {})
    monkeypatch.setattr(module, "_configure_bot_meta", lambda *a, **k: True)
    monkeypatch.setattr(module, "_ensure_canonical_bot_chat", lambda *a, **k: True)
    result = module._run_onboard(str(_write_spec(tmp_path / "spec_onboard_fail", _spec())))

    assert result["ok"] is False and "install worker" in result["error"]
    assert not profile_is_parked(profiles_root / "stray")
    assert "stray" in {name for name, _ in profiles_to_serve(True)}


def test_onboard_later_failure_parks_nothing(module, tmp_path, monkeypatch):
    """Installs succeed but a later onboard step fails: the onboard fails and the non-fleet
    profile stays served."""
    import os
    from hermes_cli.profiles import profile_is_parked, profiles_to_serve

    profiles_root = Path(os.environ["HERMES_HOME"]) / "profiles"
    (profiles_root / "stray").mkdir(parents=True)
    (profiles_root / "stray" / "config.yaml").write_text("model: {}\n", encoding="utf-8")

    monkeypatch.setattr(module, "_install", lambda src, name: shutil.copytree(src, profiles_root / name))
    monkeypatch.setattr(module, "_profile_revisions", lambda *a, **k: {})
    monkeypatch.setattr(module, "_configure_bot_meta", lambda *a, **k: False)
    monkeypatch.setattr(module, "_ensure_canonical_bot_chat", lambda *a, **k: True)
    result = module._run_onboard(str(_write_spec(tmp_path / "spec_onboard_meta", _spec())))

    assert result["ok"] is False
    assert not profile_is_parked(profiles_root / "stray")
    assert "stray" in {name for name, _ in profiles_to_serve(True)}


def _cron_spec() -> dict:
    spec = _spec()
    spec["types"]["orchestrator"]["cron_jobs"] = [
        {"name": "supervise-issues", "schedule": "every 720m", "prompt": "Supervise the board."}]
    spec["types"]["worker"]["cron_jobs"] = [
        {"name": "extra", "schedule": "every 60m", "prompt": "An unapproved job."}]
    return spec


def _set_resume_allowlist(value):
    import os
    cfg_path = Path(os.environ["HERMES_HOME"]) / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    cfg.setdefault("plugins", {}).setdefault("entries", {}).setdefault(PLUGIN_KEY, {}) \
        .setdefault("settings", {})["onboard_resume_cron_jobs"] = value
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")


def _job(profile, name):
    import os
    from cron.jobs import list_jobs, use_cron_store

    with use_cron_store(Path(os.environ["HERMES_HOME"]) / "profiles" / profile):
        return next(j for j in list_jobs(include_disabled=True) if j["name"] == name)


def _all_kept():
    return [{"profile": p, "name": n, "id": _job(p, n)["id"]}
            for p, n in (("orchestrator", "supervise-issues"), ("worker", "extra"))]


def _real_onboard(module, tmp_path, monkeypatch, tag, *, bot_chat_ok=True):
    """Drive _run_onboard through the real install_distribution; only the gateway RPC
    helpers are stubbed."""
    monkeypatch.setattr(module, "_profile_revisions", lambda *a, **k: {})
    monkeypatch.setattr(module, "_configure_bot_meta", lambda *a, **k: True)
    monkeypatch.setattr(module, "_ensure_canonical_bot_chat", lambda *a, **k: bot_chat_ok)
    return module._run_onboard(str(_write_spec(tmp_path / f"spec_{tag}", _cron_spec())))


def test_onboard_resumes_only_allowlisted_new_cron_jobs(module, tmp_path, monkeypatch):
    """A fresh onboard resumes only the default-allowlisted orchestrator/supervise-issues; a
    second rendered job outside the allowlist stays paused and is reported as kept paused."""
    result = _real_onboard(module, tmp_path, monkeypatch, "fresh")
    sup, extra = _job("orchestrator", "supervise-issues"), _job("worker", "extra")
    assert result == {
        "ok": True,
        "resumed_cron_jobs": [{"profile": "orchestrator", "name": "supervise-issues", "id": sup["id"]}],
        "kept_paused_cron_jobs": [{"profile": "worker", "name": "extra", "id": extra["id"]}],
    }, result
    assert sup["enabled"] is True and sup["state"] == "scheduled", sup
    assert extra["enabled"] is False and extra["state"] == "paused", extra


def test_reonboard_keeps_an_operator_pause(module, tmp_path, monkeypatch):
    """A job that existed before the onboard keeps its state: an operator's pause survives a
    re-onboard, and nothing is resumed."""
    from cron.jobs import pause_job, use_cron_store

    _real_onboard(module, tmp_path, monkeypatch, "first")
    sup = _job("orchestrator", "supervise-issues")
    import os
    with use_cron_store(Path(os.environ["HERMES_HOME"]) / "profiles" / "orchestrator"):
        pause_job(sup["id"], reason="operator hold")
    result = _real_onboard(module, tmp_path, monkeypatch, "second")
    sup = _job("orchestrator", "supervise-issues")
    assert result == {"ok": True, "resumed_cron_jobs": [], "kept_paused_cron_jobs": []}, result
    assert sup["enabled"] is False and sup["state"] == "paused", sup


def test_onboard_does_not_resume_a_job_another_writer_added(module, tmp_path, monkeypatch):
    """A job another writer adds to the store during the onboard is new and allowlisted by
    name, but this render did not ship it, so it stays paused and only the rendered job
    resumes."""
    import os
    from cron.jobs import create_job, list_jobs, use_cron_store

    real_install = module._install
    orch_home = Path(os.environ["HERMES_HOME"]) / "profiles" / "orchestrator"
    intruder = {}

    def _install(src, name):
        out = real_install(src, name)
        if name == "orchestrator":
            with use_cron_store(orch_home):
                intruder.update(create_job("Not from the render.", "every 30m",
                                           name="supervise-issues", paused=True))
        return out

    monkeypatch.setattr(module, "_install", _install)
    result = _real_onboard(module, tmp_path, monkeypatch, "intruder")
    with use_cron_store(orch_home):
        jobs = {j["id"]: j for j in list_jobs(include_disabled=True)}
    sup = next(j for j in jobs.values() if j["name"] == "supervise-issues" and j["id"] != intruder["id"])
    assert result == {
        "ok": True,
        "resumed_cron_jobs": [{"profile": "orchestrator", "name": "supervise-issues", "id": sup["id"]}],
        "kept_paused_cron_jobs": [{"profile": "worker", "name": "extra", "id": _job("worker", "extra")["id"]}],
    }, result
    assert jobs[intruder["id"]]["state"] == "paused", jobs[intruder["id"]]
    assert [j["id"] for j in jobs.values() if j["state"] == "scheduled"] == [sup["id"]]


def test_failed_onboard_resumes_no_cron_job(module, tmp_path, monkeypatch):
    """An onboard that fails resumes nothing: the rendered jobs stay paused for review."""
    result = _real_onboard(module, tmp_path, monkeypatch, "fail", bot_chat_ok=False)
    sup = _job("orchestrator", "supervise-issues")
    assert result["ok"] is False
    assert sup["enabled"] is False and sup["state"] == "paused", sup
    assert result["resumed_cron_jobs"] == [] and result["kept_paused_cron_jobs"] == []


def test_empty_resume_allowlist_resumes_nothing(module, tmp_path, monkeypatch):
    """`onboard_resume_cron_jobs: []` keeps the upstream posture: every new job stays paused."""
    _set_resume_allowlist([])
    result = _real_onboard(module, tmp_path, monkeypatch, "empty")
    assert result == {"ok": True, "resumed_cron_jobs": [], "kept_paused_cron_jobs": _all_kept()}, result
    assert _job("orchestrator", "supervise-issues")["state"] == "paused"


def test_failed_resume_repauses_jobs_already_resumed(module, tmp_path, monkeypatch):
    """Resume is all-or-nothing: when a later allowlisted resume fails, an earlier one is paused
    again and the onboard fails."""
    import cron.jobs as cj

    _set_resume_allowlist(["orchestrator/supervise-issues", "worker/extra"])
    real_resume = cj.resume_job

    def _resume(job_id):
        if _job("worker", "extra")["id"] == job_id:
            raise RuntimeError("resume refused")
        return real_resume(job_id)

    monkeypatch.setattr(cj, "resume_job", _resume)
    result = _real_onboard(module, tmp_path, monkeypatch, "rollback")
    assert result["ok"] is False and "resume onboarded cron jobs" in result["error"]
    assert result["resumed_cron_jobs"] == []
    assert _job("orchestrator", "supervise-issues")["state"] == "paused"
    assert _job("worker", "extra")["state"] == "paused"


def test_malformed_resume_allowlist_resumes_nothing(module, tmp_path, monkeypatch, caplog):
    """One malformed entry invalidates the whole setting: it is treated as [] with a warning,
    so even the valid orchestrator/supervise-issues entry is not honoured."""
    import logging

    _set_resume_allowlist(["orchestrator/supervise-issues", "/"])
    with caplog.at_level(logging.WARNING):
        result = _real_onboard(module, tmp_path, monkeypatch, "malformed")
    assert result == {"ok": True, "resumed_cron_jobs": [], "kept_paused_cron_jobs": _all_kept()}, result
    assert _job("orchestrator", "supervise-issues")["state"] == "paused"
    assert any("onboard_resume_cron_jobs" in r.getMessage() for r in caplog.records)
