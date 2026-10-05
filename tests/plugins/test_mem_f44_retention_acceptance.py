"""Behavior-contract acceptance tests for MEM-F44 — transcript retention / archiving.

MEM-F44 is a CONFIGURE row: the nv-coworker-compose renderer must enforce four
transcript-retention keys as fleet invariants on the DEFAULT (multiplexer) profile
and every coworker profile, writing the fleet-safe value regardless of what the
input spec declares, and those values must survive `hermes config migrate`.

Two input variants under tests/plugins/fixtures/mem-f44/ prove enforcement is
unconditional for every key:
  - coworker-types.yaml (spine base.yaml)          — all four keys set to UNSAFE
    values, so the render must OVERRIDE each one.
  - coworker-types-omitted.yaml (base-omitted.yaml) — all four keys omitted, so
    the render must INSERT each one.
A passthrough render fails the unsafe variant; a setdefault-style render fails the
unsafe variant; only a render that forces the fleet-safe value passes both.

Assertions read the RAW rendered / raw on-disk config.yaml, never load_config():
sessions.auto_prune=false and compression.in_place=true equal the pin schema
defaults (config_defaults.py:3400, :1010), so a deep-merged effective config would
report them present even if the render never wrote them.

Each key has a real reader at the pin (grep-confirmed):
  sessions.auto_prune   -> sweep maybe_auto_prune_and_vacuum, gate cli.py:2663 /
                           gateway/run.py:7755; deletes {sid}.json/.jsonl and
                           request_dump_{sid}_*.json via _remove_session_files
                           (hermes_state.py:13981) when true.
  sessions.auto_archive -> maybe_auto_archive, gate cli.py:2657 (non-destructive).
  compression.in_place  -> agent/conversation_compression.py:3259.
  checkpoints.auto_prune-> maybe_auto_prune_checkpoints, gate cli.py:2687.
"""

from pathlib import Path
import shutil

import yaml

from hermes_cli.plugins import PluginManager
from hermes_cli.config import migrate_config
from hermes_cli.config_migrations import SUPPORT_FLOOR_VERSION
from hermes_cli.config_defaults import DEFAULT_CONFIG

PLUGIN_KEY = "nv-coworker-compose"
PLUGIN_SRC = Path(__file__).resolve().parents[2] / "plugins" / PLUGIN_KEY
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "mem-f44"
SPEC_UNSAFE = FIXTURE_DIR / "coworker-types.yaml"
SPEC_OMITTED = FIXTURE_DIR / "coworker-types-omitted.yaml"
SPEC_VARIANTS = (SPEC_UNSAFE, SPEC_OMITTED)

# The fleet-safe retention values the render must emit on EVERY profile. These are
# the requirement's values, not derived from the plugin (keeps the test non-circular).
RETENTION_INVARIANTS = {
    "sessions.auto_prune": False,
    "sessions.auto_archive": True,
    "compression.in_place": True,
    "checkpoints.auto_prune": False,
}


def _dig(mapping, dotted):
    node = mapping
    for key in dotted.split("."):
        if not isinstance(node, dict) or key not in node:
            return (False, None)
        node = node[key]
    return (True, node)


def _write_home(tmp_path, monkeypatch):
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
    return manager, loaded


def _render(loaded, spec, out_root):
    return loaded.module.compose(str(spec), str(out_root))


def _raw_config(profile_dir):
    # The raw rendered file, not the deep-merged effective config: a missing key
    # stays missing so a default-valued invariant cannot pass vacuously.
    return yaml.safe_load((Path(profile_dir) / "config.yaml").read_text(encoding="utf-8"))


def _default_profile_name(rendered):
    for name, profile_dir in rendered.items():
        found, val = _dig(_raw_config(profile_dir), "gateway.multiplex_profiles")
        if found and val is True:
            return name
    raise AssertionError("no rendered profile carries gateway.multiplex_profiles: true")


def _assert_retention(cfg, where):
    for dotted, want in RETENTION_INVARIANTS.items():
        found, got = _dig(cfg, dotted)
        assert found, f"{where}: retention key {dotted} absent (render did not enforce it)"
        assert got == want, f"{where}: {dotted} == {got!r}, expected {want!r}"


def test_ac_mem_f44_1(tmp_path, monkeypatch):
    """AC-MEM-F44-1: Every rendered COWORKER profile config.yaml carries all four
    retention keys with the fleet-safe values (sessions.auto_prune=false,
    sessions.auto_archive=true, compression.in_place=true, checkpoints.auto_prune=false),
    both when the input declares unsafe values (override) and when it omits them (insert)."""
    _, loaded = _load(tmp_path, monkeypatch)
    for variant_index, spec in enumerate(SPEC_VARIANTS):
        rendered = _render(loaded, spec, tmp_path / f"out-{variant_index}")
        default_name = _default_profile_name(rendered)
        coworkers = [n for n in rendered if n != default_name]
        assert coworkers, f"{spec.name}: fixture must render at least one coworker profile"
        for name in coworkers:
            _assert_retention(_raw_config(rendered[name]), f"{spec.name} coworker {name}")


def test_ac_mem_f44_2(tmp_path, monkeypatch):
    """AC-MEM-F44-2: The rendered DEFAULT (multiplexer) profile config.yaml carries
    the same four retention keys with the same fleet-safe values, for both the
    unsafe-input and omitted-input variants, so the safety keys are enforced on the
    multiplexer profile and not only on coworkers."""
    _, loaded = _load(tmp_path, monkeypatch)
    for variant_index, spec in enumerate(SPEC_VARIANTS):
        rendered = _render(loaded, spec, tmp_path / f"out-{variant_index}")
        default_name = _default_profile_name(rendered)
        _assert_retention(_raw_config(rendered[default_name]), f"{spec.name} DEFAULT {default_name}")


def test_ac_mem_f44_3(tmp_path, monkeypatch):
    """AC-MEM-F44-3: For the DEFAULT profile and every coworker profile, a rendered
    config carrying the four retention keys survives `hermes config migrate`: after
    the ladder runs from the support floor to the latest version, all four keys are
    still explicitly present on the raw on-disk config.yaml with their fleet values,
    and the version has advanced to the latest."""
    _, loaded = _load(tmp_path, monkeypatch)
    rendered = _render(loaded, SPEC_UNSAFE, tmp_path / "out")
    # Guard against a vacuous pass on an empty render: require the DEFAULT profile
    # and at least one coworker, so the loop below asserts on both tiers.
    default_name = _default_profile_name(rendered)
    assert [n for n in rendered if n != default_name], "fixture must render a coworker profile"

    for profile_name, profile_dir in rendered.items():
        cfg = _raw_config(profile_dir)
        _assert_retention(cfg, f"pre-migrate {profile_name}")

        # Stage the rendered config as a fresh HERMES_HOME/config.yaml at the
        # migration support floor: >= floor so it is not refused, < latest so the
        # ladder actually rewrites the file (a no-op at latest would prove nothing).
        # A plain (non-managed) scope, so save_config writes normally.
        home = tmp_path / "migration-homes" / profile_name
        home.mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(home))
        (home / "config.yaml").write_text(
            yaml.safe_dump({**cfg, "_config_version": SUPPORT_FLOOR_VERSION}),
            encoding="utf-8",
        )

        result = migrate_config(interactive=False, quiet=True)
        assert isinstance(result, dict)

        migrated = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
        assert migrated.get("_config_version") == DEFAULT_CONFIG["_config_version"]
        assert migrated.get("_config_version") != SUPPORT_FLOOR_VERSION
        _assert_retention(migrated, f"post-migrate {profile_name}")
