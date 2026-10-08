"""Surface-outcome contracts for the nv-cost-cap `/cost` resolution notices.

A granted set-ceiling AT/BELOW current spend is still applied (the ceiling IS written) but LEAVES the
per-session money block set, so `_finish` reports `money_block_cleared=False` and the operator notice
must NOT claim the block cleared; a ceiling ABOVE spend clears it; and a Stop is never reported as a
Continue. Loaded through the REAL discovery path from an isolated HERMES_HOME with an empty bundled dir.
Behaviour contract (substring assertions on the honest message, not a snapshot of the whole string); no
network, nothing under ~/.hermes. Fails on the stock tree (the F30 symbols do not exist).
"""

import shutil
from pathlib import Path

import pytest
import yaml

from hermes_cli.plugins import PluginManager

PLUGIN_KEY = "nv-cost-cap"
REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_SRC = REPO_ROOT / "plugins" / PLUGIN_KEY

OPERATOR = "dashboard:oauth:org-1:op-1"
TODAY = "2026-09-19"


@pytest.fixture
def mod(tmp_path, monkeypatch):
    # Function-scoped + injected monkeypatch so it runs AFTER conftest's autouse HERMES_HOME sandbox.
    if not PLUGIN_SRC.is_dir():
        pytest.fail(f"plugin source not found at {PLUGIN_SRC}")
    hermes_home = tmp_path / "home"
    (hermes_home / "plugins").mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, hermes_home / "plugins" / PLUGIN_KEY)
    cfg = {
        "plugins": {
            "enabled": [PLUGIN_KEY],
            "entries": {PLUGIN_KEY: {"settings": {
                "operators": [OPERATOR],
                "escalation_increment_usd": 5.0,
                "escalation_reconcile_seconds": 3600,
            }}},
        }
    }
    (hermes_home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    bundled = tmp_path / "empty_bundled"
    bundled.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled is True, getattr(loaded, "error", None)
    assert loaded.error is None and loaded.module is not None
    return loaded.module


def _seed_blocked_breach(module, session_id):
    """A Tier-2 mortal breach at spend 10.0, blocked, no ESTOP belt engaged (estop_on_breach unset)."""
    module.store.set_state(
        session_id, effective_usd=10.0, window_start_total=0.0, day_start_total=0.0,
        budget_gen=1, blocked=1, immortal=0,
    )
    ep = module.store.record_episode(
        session_id, "breach", 1, TODAY, dedup_key=f"{session_id}:breach:1",
    )
    assert ep
    return ep


def test_below_spend_ceiling_reports_block_still_active(mod):
    """A granted set-ceiling at/below current spend leaves blocked=1; the notice must not claim the
    per-session cost block cleared."""
    ep = _seed_blocked_breach(mod, "sess-below")
    res = mod.set_episode_ceiling("default", "sess-below", ep, 1, "5.00", OPERATOR)  # 5.00 < spend 10.0
    assert res.get("granted") is True and res.get("decision") == "ceiling", res
    assert res.get("money_block_cleared") is False, res
    assert dict(mod.store.get_state("sess-below"))["blocked"] == 1, "a ceiling below spend leaves the block"
    text = mod._cost_outcome_text(res).lower()
    assert "remains active" in text, text
    assert "cost block is cleared" not in text, text


def test_above_spend_ceiling_clears_block(mod):
    """A granted set-ceiling above current spend clears the money block, and the notice says so."""
    ep = _seed_blocked_breach(mod, "sess-above")
    res = mod.set_episode_ceiling("default", "sess-above", ep, 1, "12.50", OPERATOR)  # 12.50 > spend 10.0
    assert res.get("granted") is True and res.get("money_block_cleared") is True, res
    assert dict(mod.store.get_state("sess-above"))["blocked"] == 0, "a ceiling above spend clears the block"
    text = mod._cost_outcome_text(res).lower()
    assert "ceiling set" in text
    assert "remains active" not in text, text


def test_stop_outcome_text_is_never_continue(mod):
    """A granted Stop is reported as stopped and never borrows the Continue notice."""
    ep = _seed_blocked_breach(mod, "sess-stop")
    res = mod.resolve_escalation("sess-stop", ep, 1, "stop", OPERATOR)
    assert res.get("granted") is True and res.get("decision") == "stop", res
    assert dict(mod.store.get_state("sess-stop"))["blocked"] == 1, "Stop keeps the session blocked"
    text = mod._cost_outcome_text(res)
    assert "stopped" in text.lower()
    assert "Continue applied" not in text, text
