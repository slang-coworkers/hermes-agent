"""Behavior-contract acceptance tests for MEM-F42 — bounded always-loaded memory (OKF synthesis).

MEM-F42 is a CONFIGURE row: the nv-coworker-compose renderer must emit a per-profile
`curator` block on the DEFAULT (multiplexer) profile and every coworker profile that
(a) forces the curator SAFETY invariants as fleet invariants regardless of what the
input spec declares —

    curator.enabled: true, curator.backup.enabled: true, curator.backup.keep >= 5

(keep is a floor: a declared value below 5 is raised to 5, a declared value above 5 is
preserved) so every rendered profile is curator-enabled when a correctly profile-scoped
activation path invokes it, and each pass attempts a rollback-able pre-run backup — this
test asserts the render EMITS the keys, not that any process runs the curator — and
(b) preserves the per-role TUNING keys (interval_hours /
min_idle_hours / consolidate / prune_builtins) the extends chain merges from each
profile's own config, so the enforcement touches ONLY the safety leaves and does not
flatten per-role tuning. The enforced safety keys must also survive `hermes config migrate`.

Two input variants under tests/plugins/fixtures/mem-f42/ prove enforcement is
unconditional:
  - coworker-types.yaml (spine base.yaml)          — the safety keys set UNSAFE
    (enabled:false, backup.enabled:false, keep:0), so the render must OVERRIDE; every
    profile authors DIVERGENT per-role tuning that must survive; reviewer declares
    backup.keep:12 (above the floor) that must be PRESERVED.
  - coworker-types-omitted.yaml (base-omitted.yaml) — the whole curator block omitted,
    so the render must INSERT the safety keys.

Assertions read the RAW rendered / raw on-disk config.yaml, never load_config(): the
enforced safety values equal the pin schema defaults (config_defaults.py:2331 enabled,
:2370 backup.enabled, :2371 backup.keep=5), so a deep-merged effective config would
report them present even if the render never wrote them.
"""

from pathlib import Path
import re
import shutil

import yaml

from hermes_cli.plugins import PluginManager
from hermes_cli.config import migrate_config
from hermes_cli.config_migrations import SUPPORT_FLOOR_VERSION
from hermes_cli.config_defaults import DEFAULT_CONFIG

PLUGIN_KEY = "nv-coworker-compose"
REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_SRC = REPO_ROOT / "plugins" / PLUGIN_KEY
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "mem-f42"
SPEC_UNSAFE = FIXTURE_DIR / "coworker-types.yaml"
SPEC_OMITTED = FIXTURE_DIR / "coworker-types-omitted.yaml"
SPEC_VARIANTS = (SPEC_UNSAFE, SPEC_OMITTED)

DOC_PAGE = REPO_ROOT / "website" / "docs" / "user-guide" / "fleet-bounded-memory.md"
DOCS_ROOT = REPO_ROOT / "website" / "docs"

# The two curator safety booleans the render must force True on EVERY profile, and the
# retention floor for backup.keep. These are the requirement's values, not derived from
# the plugin (keeps the test non-circular).
CURATOR_SAFETY_BOOL = {
    "curator.enabled": True,
    "curator.backup.enabled": True,
}
CURATOR_KEEP_FLOOR = 5

# The four per-role tuning keys the render must NOT force (they flow from the spec).
CURATOR_TUNING_KEYS = (
    "curator.interval_hours",
    "curator.min_idle_hours",
    "curator.consolidate",
    "curator.prune_builtins",
)

# Per-role tuning authored per profile in coworker-types.yaml — asserted verbatim on the
# raw config so a flatten-to-one-value or drop-the-key regression is caught. These are
# the fixture's declared values (unsafe variant).
EXPECTED_TUNING = {
    "orchestrator": {"curator.interval_hours": 72, "curator.min_idle_hours": 6,
                     "curator.consolidate": True, "curator.prune_builtins": False},
    "reviewer": {"curator.interval_hours": 336, "curator.min_idle_hours": 1,
                 "curator.consolidate": False, "curator.prune_builtins": True},
    "fixer": {"curator.interval_hours": 240, "curator.min_idle_hours": 4,
              "curator.consolidate": True, "curator.prune_builtins": True},
    "default": {"curator.interval_hours": 500, "curator.min_idle_hours": 5,
                "curator.consolidate": False, "curator.prune_builtins": True},
}
# The three coworker profiles whose tuning must diverge (default is the multiplexer tier).
COWORKER_ROLES = ("orchestrator", "reviewer", "fixer")

# backup.keep the unsafe variant must yield per profile: reviewer declares 12 (preserved
# above the floor); every other profile declares 0 or omits it (raised to the floor).
EXPECTED_KEEP_UNSAFE = {"orchestrator": 5, "reviewer": 12, "fixer": 5, "default": 5}


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
    # Raw rendered file, not the deep-merged effective config: a missing key stays
    # missing so a default-valued invariant cannot pass vacuously.
    return yaml.safe_load((Path(profile_dir) / "config.yaml").read_text(encoding="utf-8"))


def _default_profile_name(rendered):
    for name, profile_dir in rendered.items():
        found, val = _dig(_raw_config(profile_dir), "gateway.multiplex_profiles")
        if found and val is True:
            return name
    raise AssertionError("no rendered profile carries gateway.multiplex_profiles: true")


def _assert_safety(cfg, where):
    for dotted, want in CURATOR_SAFETY_BOOL.items():
        found, got = _dig(cfg, dotted)
        assert found, f"{where}: curator safety key {dotted} absent (render did not enforce it)"
        assert got == want, f"{where}: {dotted} == {got!r}, expected {want!r}"
    found, keep = _dig(cfg, "curator.backup.keep")
    assert found, f"{where}: curator.backup.keep absent (render did not enforce it)"
    assert isinstance(keep, int) and not isinstance(keep, bool), f"{where}: keep not int: {keep!r}"
    assert keep >= CURATOR_KEEP_FLOOR, f"{where}: curator.backup.keep {keep} < floor {CURATOR_KEEP_FLOOR}"


def test_ac_mem_f42_1(tmp_path, monkeypatch):
    """AC-MEM-F42-1: Every rendered COWORKER profile config.yaml carries the curator
    safety invariants — curator.enabled=true, curator.backup.enabled=true, and
    curator.backup.keep>=5 (a declared keep below 5 is raised to 5, a declared keep above
    5 is preserved) — both when the input declares unsafe values (override) and when it
    omits the whole curator block (insert)."""
    _, loaded = _load(tmp_path, monkeypatch)
    for variant_index, spec in enumerate(SPEC_VARIANTS):
        rendered = _render(loaded, spec, tmp_path / f"out-{variant_index}")
        default_name = _default_profile_name(rendered)
        coworkers = [n for n in rendered if n != default_name]
        assert coworkers, f"{spec.name}: fixture must render at least one coworker profile"
        for name in coworkers:
            _assert_safety(_raw_config(rendered[name]), f"{spec.name} coworker {name}")
    # Omitted variant: no keep is declared anywhere, so every profile is INSERTED at
    # exactly the floor 5 — asserted as == 5, not merely >= 5, so an over-eager insert
    # (e.g. 99) is caught, not just a missing key.
    rendered_omitted = _render(loaded, SPEC_OMITTED, tmp_path / "out-omitted")
    for name in rendered_omitted:
        _, keep = _dig(_raw_config(rendered_omitted[name]), "curator.backup.keep")
        assert keep == 5, f"omitted {name}: keep == {keep!r}, expected exactly 5 (insert to floor)"
    # Unsafe variant floor-MAX both ways: reviewer's declared keep:12 preserved, keep:0/absent -> 5.
    rendered = _render(loaded, SPEC_UNSAFE, tmp_path / "out-keep")
    for name, want_keep in EXPECTED_KEEP_UNSAFE.items():
        _, keep = _dig(_raw_config(rendered[name]), "curator.backup.keep")
        assert keep == want_keep, f"{name}: keep == {keep!r}, expected {want_keep!r}"


def test_ac_mem_f42_2(tmp_path, monkeypatch):
    """AC-MEM-F42-2: The rendered DEFAULT (multiplexer) profile config.yaml carries the
    same curator safety invariants with the same values, for both the unsafe-input and
    omitted-input variants, so the safety keys are enforced on the multiplexer profile
    and not only on coworkers."""
    _, loaded = _load(tmp_path, monkeypatch)
    for variant_index, spec in enumerate(SPEC_VARIANTS):
        rendered = _render(loaded, spec, tmp_path / f"out-{variant_index}")
        default_name = _default_profile_name(rendered)
        _assert_safety(_raw_config(rendered[default_name]), f"{spec.name} DEFAULT {default_name}")


def test_ac_mem_f42_3(tmp_path, monkeypatch):
    """AC-MEM-F42-3: Per-role curator tuning is preserved with per-role divergence — every
    rendered profile carries its own declared interval_hours/min_idle_hours/consolidate/
    prune_builtins, the three coworker roles differ (not all equal) on every tuning key,
    and the safety invariants are simultaneously enforced on every profile, proving the
    enforcement forces only the safety keys and does not flatten or clobber per-role
    tuning."""
    _, loaded = _load(tmp_path, monkeypatch)
    rendered = _render(loaded, SPEC_UNSAFE, tmp_path / "out")
    for name, expect in EXPECTED_TUNING.items():
        assert name in rendered, f"fixture must render the {name!r} profile"
        cfg = _raw_config(rendered[name])
        _assert_safety(cfg, f"tuning-profile {name}")
        for dotted, want in expect.items():
            found, got = _dig(cfg, dotted)
            assert found, f"{name}: tuning key {dotted} absent from raw config"
            assert got == want, f"{name}: {dotted} == {got!r}, expected declared {want!r}"
    # Flattening guard: across the three coworker roles each tuning key takes at least two
    # distinct values, so a force-the-whole-block regression collapsing them fails here.
    for dotted in CURATOR_TUNING_KEYS:
        values = {_dig(_raw_config(rendered[r]), dotted)[1] for r in COWORKER_ROLES}
        assert len(values) >= 2, f"{dotted} did not diverge across roles (all {values!r}): tuning flattened"


def test_ac_mem_f42_4(tmp_path, monkeypatch):
    """AC-MEM-F42-4 (derived): For the DEFAULT profile and every coworker, a rendered
    config carrying the enforced curator safety keys survives `hermes config migrate`:
    after the ladder runs from the support floor to the latest version, the safety keys
    are still explicitly present on the raw on-disk config.yaml with their values, and
    the version has advanced to the latest."""
    _, loaded = _load(tmp_path, monkeypatch)
    rendered = _render(loaded, SPEC_UNSAFE, tmp_path / "out")
    default_name = _default_profile_name(rendered)
    assert [n for n in rendered if n != default_name], "fixture must render a coworker profile"

    for profile_name, profile_dir in rendered.items():
        cfg = _raw_config(profile_dir)
        _assert_safety(cfg, f"pre-migrate {profile_name}")

        # Stage at the support floor: >= floor so migrate is not refused, < latest so the
        # ladder actually rewrites the file (a no-op at latest would prove nothing).
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
        _assert_safety(migrated, f"post-migrate {profile_name}")


def _links_to_doc(md_text, md_path):
    for m in re.finditer(r"\]\(([^)]+)\)", md_text):
        target = m.group(1).split("#")[0].split("?")[0].strip()
        if "fleet-bounded-memory" not in target:
            continue
        if target.endswith(".md") and not target.startswith("/"):
            try:
                if (md_path.parent / target).resolve() == DOC_PAGE.resolve():
                    return True
            except (OSError, ValueError):
                pass
        stem = target.rstrip("/").split("/")[-1]
        if stem in (DOC_PAGE.stem, DOC_PAGE.name):
            return True
    return False


def test_ac_mem_f42_5(tmp_path, monkeypatch):
    """AC-MEM-F42-5: A fleet bounded-memory doc page maps OKF synthesis onto the curator
    config, names `hermes curator status` and `hermes curator rollback`, states the
    three-part bounded-memory story (curator + MEM-F41 memory caps + native in-turn
    overflow consolidation), and is reachable by a Markdown link from an existing docs
    page."""
    assert DOC_PAGE.is_file(), f"bounded-memory doc page missing: {DOC_PAGE}"
    text = DOC_PAGE.read_text(encoding="utf-8")
    low = text.lower()

    assert re.search(r"^#{1,6}\s+.*curator", text, re.IGNORECASE | re.MULTILINE), \
        "no Markdown heading mentioning the curator"
    assert "okf" in low, "doc does not mention OKF (the synthesis mapping)"

    assert "hermes curator status" in text, "doc does not name `hermes curator status`"
    assert "hermes curator rollback" in text, "doc does not name `hermes curator rollback`"

    # Story leg 1 (curator), leg 2 (both MEM-F41 memory caps), leg 3 (native in-turn
    # memory-overflow consolidation — proven by proximity, not the ambient word alone,
    # since ordinary curator "consolidation" would satisfy a bare token).
    assert "curator" in low, "doc does not mention the curator (story leg 1)"
    assert "memory.memory_char_limit" in text, "doc does not name memory.memory_char_limit (leg 2)"
    assert "memory.user_char_limit" in text, "doc does not name memory.user_char_limit (leg 2)"
    assert re.search(r"(?:in[- ]turn.{0,160}overflow|overflow.{0,160}in[- ]turn)", low, re.DOTALL), \
        "doc does not explain native in-turn memory-overflow consolidation (leg 3)"

    linked = any(
        _links_to_doc(md.read_text(encoding="utf-8"), md)
        for md in DOCS_ROOT.rglob("*.md")
        if md.resolve() != DOC_PAGE.resolve()
    )
    assert linked, "no existing docs page links to fleet-bounded-memory (Markdown link)"
