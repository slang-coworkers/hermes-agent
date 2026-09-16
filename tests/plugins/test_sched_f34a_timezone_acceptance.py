"""Acceptance tests for SCHED-F34.a — nv-coworker-compose must not emit a `timezone`
on the DEFAULT/multiplexer profile it renders, while coworker profiles keep their own.

Why the DEFAULT profile must carry no timezone (release tree, tag v2026.8.31 / commit
29112be): the gateway promotes any non-empty `timezone` in the top-level/DEFAULT
HERMES_HOME config.yaml to a PROCESS-GLOBAL `HERMES_TIMEZONE` (gateway/run.py:2862-2865,
presence-sensitive per :2654-2656). hermes_time._timezone_cache_identity()
(hermes_time.py:48-51) then pins that single ("environment", tz) identity for EVERY
multiplexed profile, clobbering each coworker's own zone. With the DEFAULT `timezone`
empty/absent, this bridge does not set `HERMES_TIMEZONE` from config; absent an
independent environment override, each coworker profile then resolves under its own
("config", path) identity (hermes_time.py:51, :90-92); no gateway/core change is needed.

Assertions read the RAW rendered config.yaml, never load_config(): a falsy DEFAULT-profile
timezone coincides with the DEFAULT_CONFIG default `timezone: ""` (config_defaults.py:2382),
so only a raw read distinguishes an emitted value from a defaults-merge. The spec is built
inline in tmp_path so the test is self-contained (no external fixture tree).
"""

from pathlib import Path
import shutil

import yaml

from hermes_cli.plugins import PluginManager

PLUGIN_KEY = "nv-coworker-compose"
PLUGIN_SRC = Path(__file__).resolve().parents[2] / "plugins" / PLUGIN_KEY

BASE_TZ = "Asia/Kolkata"
ORCH_TZ = "America/New_York"
REVIEWER_TZ = "Europe/Berlin"


def _write_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with the plugin copied in and enabled, and an EMPTY bundled
    dir — so discovery loads exactly this plugin from a stock tree."""
    home = tmp_path / "hermes-home"
    plugins_dir = home / "plugins"
    plugins_dir.mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, plugins_dir / PLUGIN_KEY)
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": [PLUGIN_KEY]}}),
        encoding="utf-8",
    )
    empty_bundled = tmp_path / "bundled-plugins"
    empty_bundled.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(empty_bundled))
    return home


def _load(tmp_path, monkeypatch):
    _write_home(tmp_path, monkeypatch)
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled is True
    assert loaded.error is None
    assert loaded.module is not None
    return loaded


def _write_spec(root):
    """A minimal coworker-types spec: a base spine carrying a non-empty timezone, plus
    three coworker types — `orchestrator` and `reviewer` each DECLARING a distinct
    config.timezone (divergent per-type overrides), and `fixer` inheriting the spine
    zone. Types declare no skills/workflows, so no SKILL.md source tree is needed."""
    spec_dir = root / "spec"
    spines = spec_dir / "spines"
    spines.mkdir(parents=True)
    (spines / "base.yaml").write_text(yaml.safe_dump({
        "identity": "BASE-IDENTITY",
        "config": {"timezone": BASE_TZ, "sessions": {"auto_prune": False},
                   "terminal": {"container_persistent": True}},
    }), encoding="utf-8")
    spec = spec_dir / "coworker-types.yaml"
    spec.write_text(yaml.safe_dump({
        "workspace_root": "/data/coworkers",
        "default_profile": "default",
        "orchestrator_profile": "orchestrator",
        "spines": {"base": {"source": "spines/base.yaml"}},
        "types": {
            "orchestrator": {"extends": ["base"], "identity": "ORCH",
                             "config": {"timezone": ORCH_TZ}},
            "reviewer": {"extends": ["base"], "identity": "REV",
                         "config": {"timezone": REVIEWER_TZ}},
            "fixer": {"extends": ["base"], "identity": "FIX"},
        },
        "default_config": {"gateway": {
            "multiplex_profiles": True,
            "multiplex_profile_allowlist": ["orchestrator", "reviewer", "fixer"],
        }},
    }), encoding="utf-8")
    return spec


def _raw_config(profile_dir):
    # The RAW rendered file, never load_config(): a missing/empty timezone must stay
    # distinguishable from the DEFAULT_CONFIG default (also ""), so a defaults-merge
    # cannot make an unstripped render pass vacuously.
    return yaml.safe_load((Path(profile_dir) / "config.yaml").read_text(encoding="utf-8"))


def _default_profile_name(rendered):
    """The DEFAULT/multiplexer profile is the one carrying gateway.multiplex_profiles:
    true — in this fixture only the DEFAULT path emits it (no coworker type declares it)."""
    for name, profile_dir in rendered.items():
        cfg = _raw_config(profile_dir)
        gw = cfg.get("gateway") if isinstance(cfg, dict) else None
        if isinstance(gw, dict) and gw.get("multiplex_profiles") is True:
            return name
    raise AssertionError("no rendered profile carries gateway.multiplex_profiles: true")


def _render(loaded, tmp_path):
    spec = _write_spec(tmp_path)
    rendered = loaded.module.compose(str(spec), str(tmp_path / "out"))
    default_name = _default_profile_name(rendered)
    coworkers = {n: d for n, d in rendered.items() if n != default_name}
    assert coworkers, "fixture must render at least one coworker profile"
    return rendered, default_name, coworkers


def test_ac_sched_f34_a_1(tmp_path, monkeypatch):
    """AC-SCHED-F34.a-1: The rendered DEFAULT (multiplexer) profile config.yaml carries
    no effective timezone (absent or empty/falsy) even though the merged spine config
    declares a non-empty one, so the render sets no fleet-global timezone."""
    loaded = _load(tmp_path, monkeypatch)
    rendered, default_name, _ = _render(loaded, tmp_path)
    tz = _raw_config(rendered[default_name]).get("timezone")
    assert not tz, (
        f"DEFAULT profile {default_name!r} carries timezone {tz!r}; the render must strip "
        f"the multiplexer profile timezone (empty/absent) so it is not bridged into "
        f"process-global HERMES_TIMEZONE (gateway/run.py:2862-2865)"
    )


def test_ac_sched_f34_a_2(tmp_path, monkeypatch):
    """AC-SCHED-F34.a-2: Every rendered COWORKER profile config.yaml still carries a
    non-empty timezone — the strip is scoped to the DEFAULT profile only and the
    coworker-profile timezone emission is left intact."""
    loaded = _load(tmp_path, monkeypatch)
    rendered, _, coworkers = _render(loaded, tmp_path)
    for name in coworkers:
        tz = _raw_config(rendered[name]).get("timezone")
        assert tz, (
            f"coworker profile {name!r} lost its timezone (got {tz!r}); the DEFAULT-only "
            f"strip must not touch coworker profiles"
        )


def test_ac_sched_f34_a_3(tmp_path, monkeypatch):
    """AC-SCHED-F34.a-3 (derived): Given coworker types with divergent timezones — two
    declaring a per-type config.timezone override and one inheriting the spine zone —
    each rendered coworker profile carries its own distinct zone while the DEFAULT
    profile carries none, proving per-profile override survives the render."""
    loaded = _load(tmp_path, monkeypatch)
    rendered, default_name, coworkers = _render(loaded, tmp_path)
    zones = {name: _raw_config(rendered[name]).get("timezone") for name in coworkers}
    assert zones.get("orchestrator") == ORCH_TZ, f"orchestrator zone wrong: {zones!r}"
    assert zones.get("reviewer") == REVIEWER_TZ, f"reviewer zone wrong: {zones!r}"
    assert zones.get("fixer") == BASE_TZ, f"fixer (inherited) zone wrong: {zones!r}"
    assert len(set(zones.values())) == len(zones), (
        f"coworker zones are not all distinct — a divergent zone collapsed: {zones!r}"
    )
    assert not _raw_config(rendered[default_name]).get("timezone"), (
        "DEFAULT profile still carries a timezone that would clobber per-profile zones"
    )
