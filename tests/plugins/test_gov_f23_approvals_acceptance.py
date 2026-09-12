"""Acceptance test for GOV-F23 — Approvals primitive rendered by nv-coworker-compose.

One ``test_ac_gov_f23_<n>`` per pytest: acceptance criterion in the ADR
(/workspace/agent/reports/gov-f23.md §Acceptance criteria). The live criterion
AC-GOV-F23-6 has no function here — its artifact is
tests/e2e-scenarios/GOV-F23/AC-GOV-F23-6.md.

The plugin is loaded through the real discovery path from a tmp_path
HERMES_HOME with an EMPTY bundled dir; the render is invoked as a module
function; assertions read the RAW rendered files, never load_config — several
approvals values equal DEFAULT_CONFIG defaults, so a deep-merged read would
report them present even if the render never wrote them.

Fixtures: tests/plugins/fixtures/gov-f23/ (two spec variants sharing one
skills/workflows/overlays tree), per the ADR §Acceptance test.
"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from hermes_cli.config import migrate_config
from hermes_cli.config_defaults import DEFAULT_CONFIG
from hermes_cli.config_migrations import SUPPORT_FLOOR_VERSION
from hermes_cli.plugins import PluginManager

PLUGIN_KEY = "nv-coworker-compose"
PLUGIN_SRC = Path(__file__).resolve().parents[2] / "plugins" / PLUGIN_KEY
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "gov-f23"

# The unsafe variant declares the fleet-uniform approvals keys with the WRONG
# values and a non-empty DEFAULT command_allowlist, so a passthrough / setdefault
# render fails (proves OVERRIDE + STRIP + DEFAULT-empty-force). The omitted variant
# omits the approvals block entirely (proves INSERT).
SPEC_UNSAFE = FIXTURE_DIR / "coworker-types.yaml"
SPEC_OMITTED = FIXTURE_DIR / "coworker-types-omitted.yaml"
SPEC_VARIANTS = (SPEC_UNSAFE, SPEC_OMITTED)

# The requirement's fleet-uniform values, hardcoded here (NOT read from the
# plugin — keeps the test non-circular). These land in the machine-wide managed
# fragment, never in a per-profile config.
APPROVALS_MANAGED = {
    "approvals.mode": "smart",
    "approvals.timeout": 300,
    "approvals.cron_mode": "deny",
    "approvals.single_query_mode": "deny",
    "approvals.unattended_mode": "deny",
    "approvals.denial_breaker_threshold": 3,
}
_RESOLVER_KEYS = ("cron_mode", "single_query_mode", "unattended_mode")

# Per-role values the fixture declares for its two coworker types (exact-equality
# asserted in AC-2). TWO roles with DIFFERENT allowlists prove per-role binding —
# a shared blanket or a permissive ["*"] would fail. Allowlist entries are commands
# the dangerous-command detector flags (so the bypass is meaningful); the deny
# floor is force-push / tag verbs that must never run unattended.
# The deny floor covers force-push, tag AND release verbs (the row's
# "git push --force*, release and tag verbs").
BUILDER_ALLOWLIST = ["rm -rf build", "git reset --hard*"]
BUILDER_DENY = ["git push --force*", "git push -f*", "git tag *", "gh release *"]
REVIEWER_ALLOWLIST = ["git clean -fdx"]
REVIEWER_DENY = ["git push --force*", "git tag *", "gh release *"]
EXPECTED_COWORKERS = {
    "builder": (BUILDER_ALLOWLIST, BUILDER_DENY),
    "reviewer": (REVIEWER_ALLOWLIST, REVIEWER_DENY),
}
DEFAULT_DENY = ["git push --force*", "git push -f*", "git tag *", "gh release *"]  # the floor rendered on DEFAULT

# Behavioural probes for AC-4, run against the `builder` role.
ALLOW_PROBE = "rm -rf build"                  # flagged-dangerous AND in builder's allowlist
NONALLOW_PROBE = "git clean -fdx"             # flagged-dangerous, NOT in builder's allowlist
DENY_PROBE = "git push --force origin main"   # matches builder's deny floor "git push --force*"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _write_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp HERMES_HOME with the plugin copied in and enabled, an EMPTY bundled dir."""
    home = tmp_path / "hermes-home"
    (home / "plugins").mkdir(parents=True)
    import shutil

    shutil.copytree(PLUGIN_SRC, home / "plugins" / PLUGIN_KEY)
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": [PLUGIN_KEY]}}), encoding="utf-8"
    )
    empty_bundled = tmp_path / "bundled-plugins"
    empty_bundled.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(empty_bundled))
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    return home


def _load(home: Path):
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled is True, getattr(loaded, "error", None)
    assert loaded.error is None
    assert loaded.module is not None
    return loaded


def _render(loaded, spec: Path, out_root: Path) -> dict:
    """Invoke the render as a module function; returns {profile_name: dir}."""
    out_root.mkdir(parents=True, exist_ok=True)
    return loaded.module.compose(str(spec), str(out_root))


def _raw_config(profile_dir: str) -> dict:
    return yaml.safe_load((Path(profile_dir) / "config.yaml").read_text(encoding="utf-8")) or {}


def _managed_fragment(out_root: Path) -> dict:
    """Read the machine-wide managed-scope fragment."""
    path = out_root / "managed" / "config.yaml"
    assert path.exists(), f"managed fragment not emitted at {path}"
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _dig(mapping: dict, dotted: str):
    """Walk a dotted path; returns (found, value). Missing key -> (False, None)."""
    node = mapping
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _default_profile_name(rendered: dict) -> str:
    for name, pdir in rendered.items():
        cfg = _raw_config(pdir)
        found, val = _dig(cfg, "gateway.multiplex_profiles")
        if found and val is True:
            return name
    raise AssertionError("no DEFAULT/multiplexer profile (gateway.multiplex_profiles: true) rendered")


def _coworker_names(rendered: dict, default_name: str) -> list[str]:
    return [n for n in rendered if n != default_name]


# ---------------------------------------------------------------------------
# AC-GOV-F23-1 — managed fragment carries the fleet-uniform approvals block
# ---------------------------------------------------------------------------
def test_ac_gov_f23_1(tmp_path, monkeypatch):
    """The render emits one machine-wide managed fragment carrying mode:smart /
    timeout:300 / all three resolvers:deny / denial_breaker_threshold:3, written
    explicitly (override-or-insert)."""
    home = _write_home(tmp_path, monkeypatch)
    loaded = _load(home)
    for i, spec in enumerate(SPEC_VARIANTS):
        out_root = tmp_path / f"out-{i}"
        _render(loaded, spec, out_root)
        frag = _managed_fragment(out_root)
        for dotted, want in APPROVALS_MANAGED.items():
            found, got = _dig(frag, dotted)
            assert found, f"[{spec.name}] managed fragment missing {dotted}"
            assert got == want, f"[{spec.name}] managed {dotted} = {got!r}, want {want!r}"


# ---------------------------------------------------------------------------
# AC-GOV-F23-2 — per-tier command_allowlist + approvals.deny in profile configs
# ---------------------------------------------------------------------------
def test_ac_gov_f23_2(tmp_path, monkeypatch):
    """Each coworker profile carries its EXACT per-role top-level command_allowlist
    and approvals.deny; the DEFAULT/multiplexer profile carries an EXPLICIT EMPTY
    command_allowlist (the in-gateway floor) and the EXACT approvals.deny floor. Two
    roles with different allowlists prove per-role binding (not a shared blanket)."""
    home = _write_home(tmp_path, monkeypatch)
    loaded = _load(home)
    for i, spec in enumerate(SPEC_VARIANTS):
        out_root = tmp_path / f"out-{i}"
        rendered = _render(loaded, spec, out_root)
        default_name = _default_profile_name(rendered)
        coworkers = _coworker_names(rendered, default_name)
        assert set(coworkers) == set(EXPECTED_COWORKERS), (
            f"[{spec.name}] coworkers {sorted(coworkers)} != {sorted(EXPECTED_COWORKERS)}"
        )
        for name in coworkers:
            cfg = _raw_config(rendered[name])
            want_allow, want_deny = EXPECTED_COWORKERS[name]
            found_al, allow = _dig(cfg, "command_allowlist")
            assert found_al and allow == want_allow, (
                f"[{spec.name}] {name}: command_allowlist = {allow!r}, want {want_allow!r}"
            )
            found_deny, deny = _dig(cfg, "approvals.deny")
            assert found_deny and deny == want_deny, (
                f"[{spec.name}] {name}: approvals.deny = {deny!r}, want {want_deny!r}"
            )
            # phantom-key guard: the allowlist must be TOP-LEVEL, not agent.* (no reader there)
            assert not _dig(cfg, "agent.command_allowlist")[0], (
                f"[{spec.name}] {name}: command_allowlist under agent.* (a key with no reader)"
            )

        default_cfg = _raw_config(rendered[default_name])
        found_al, allow = _dig(default_cfg, "command_allowlist")
        assert found_al and allow == [], (
            f"[{spec.name}] DEFAULT command_allowlist = {allow!r}, want [] (in-gateway floor)"
        )
        found_deny, deny = _dig(default_cfg, "approvals.deny")
        assert found_deny and deny == DEFAULT_DENY, (
            f"[{spec.name}] DEFAULT approvals.deny = {deny!r}, want {DEFAULT_DENY!r}"
        )
        assert not _dig(default_cfg, "agent.command_allowlist")[0], (
            f"[{spec.name}] DEFAULT: command_allowlist under agent.* (a key with no reader)"
        )


# ---------------------------------------------------------------------------
# AC-GOV-F23-3 — disjoint immutability split, nothing set to approve
# ---------------------------------------------------------------------------
def test_ac_gov_f23_3(tmp_path, monkeypatch):
    """Fleet-uniform keys live ONLY in the managed fragment; per-role keys live
    ONLY in profile configs; no rendered file sets any resolver/mode to approve."""
    home = _write_home(tmp_path, monkeypatch)
    loaded = _load(home)
    out_root = tmp_path / "out"
    rendered = _render(loaded, SPEC_UNSAFE, out_root)
    frag = _managed_fragment(out_root)

    # (a) fleet-uniform keys absent from every profile config
    for name, pdir in rendered.items():
        cfg = _raw_config(pdir)
        for dotted in APPROVALS_MANAGED:
            assert not _dig(cfg, dotted)[0], f"{name}: fleet-uniform {dotted} leaked into profile config"

    # (b) per-role keys absent from the managed fragment
    assert not _dig(frag, "command_allowlist")[0], "command_allowlist leaked into managed fragment"
    assert not _dig(frag, "approvals.deny")[0], "approvals.deny leaked into managed fragment"

    # (c) nothing rendered as approve, anywhere
    def _assert_no_approve(cfg: dict, where: str):
        found, mode = _dig(cfg, "approvals.mode")
        assert not (found and mode == "approve"), f"{where}: approvals.mode == approve"
        for rk in _RESOLVER_KEYS:
            found, val = _dig(cfg, f"approvals.{rk}")
            assert not (found and val == "approve"), f"{where}: approvals.{rk} == approve"

    _assert_no_approve(frag, "managed fragment")
    for name, pdir in rendered.items():
        _assert_no_approve(_raw_config(pdir), f"profile {name}")


# ---------------------------------------------------------------------------
# AC-GOV-F23-4 — rendered values drive the native guard (non-vacuous + floor)
# ---------------------------------------------------------------------------
def test_ac_gov_f23_4(tmp_path, monkeypatch):
    """(a) A coworker single-query worker with its own rendered config: its
    allowlisted flagged verb short-circuits APPROVED (before the single-query
    deny), a non-allowlisted flagged verb is DENIED (single_query_mode=deny), and
    its deny-floor verb is user-deny BLOCKED. (b) In-gateway fail-safe floor: with
    the process-global allowlist loaded from the DEFAULT profile's EMPTY allowlist,
    a coworker's would-be-allowlisted verb run in an in-gateway cron context is
    DENIED by cron_mode=deny (and the managed cron_mode=deny overrides a
    conflicting profile-local cron_mode=approve, proving the overlay wins)."""
    import tools.approval as ta
    from hermes_cli import managed_scope
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    home = _write_home(tmp_path, monkeypatch)
    loaded = _load(home)
    out_root = tmp_path / "out"
    rendered = _render(loaded, SPEC_UNSAFE, out_root)
    default_name = _default_profile_name(rendered)
    frag = _managed_fragment(out_root)
    assert "builder" in rendered

    def _materialize(profile_dir: str, tag: str):
        """A fresh effective HERMES_HOME (the profile config) + a managed dir (the
        fleet fragment). Returns (home, managed_dir)."""
        eff = tmp_path / f"eff-{tag}"
        eff.mkdir()
        (eff / "config.yaml").write_text(
            (Path(profile_dir) / "config.yaml").read_text(encoding="utf-8"), encoding="utf-8"
        )
        mgd = tmp_path / f"mgd-{tag}"
        mgd.mkdir()
        (mgd / "config.yaml").write_text(yaml.safe_dump(frag), encoding="utf-8")
        return eff, mgd

    def _load_allowlist_for(home_dir: Path, mgd_dir: Path):
        """Model a process loading its permanent allowlist at import for one home."""
        monkeypatch.setenv("HERMES_HOME", str(home_dir))
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(mgd_dir))
        managed_scope.invalidate_managed_cache()
        with ta._lock:
            ta._permanent_approved.clear()
        ta.load_permanent_allowlist()

    builder_home, builder_mgd = _materialize(rendered["builder"], "builder")
    # Inject a conflicting profile-local cron_mode=approve so part (b) proves the
    # managed fragment's cron_mode=deny wins the overlay (not merely reads a default).
    _bcfg = yaml.safe_load((builder_home / "config.yaml").read_text(encoding="utf-8")) or {}
    _bcfg.setdefault("approvals", {})["cron_mode"] = "approve"
    (builder_home / "config.yaml").write_text(yaml.safe_dump(_bcfg), encoding="utf-8")
    default_home, default_mgd = _materialize(rendered[default_name], "default")

    saved = set(ta._permanent_approved)
    override_token = None
    try:
        # (a) the builder as a single-query worker (own process, own allowlist)
        _load_allowlist_for(builder_home, builder_mgd)
        monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "1")
        assert ta._get_single_query_approval_mode() == "deny"
        # allowlisted flagged verb: short-circuits approved BEFORE the single-query deny
        assert ta.detect_dangerous_command(ALLOW_PROBE)[0], "ALLOW_PROBE not flagged — bypass meaningless"
        assert ta._command_matches_permanent_allowlist(ALLOW_PROBE) is True
        assert ta.check_all_command_guards(ALLOW_PROBE, "local")["approved"] is True
        # non-allowlisted flagged verb: single_query_mode=deny denies it instantly
        assert ta.detect_dangerous_command(NONALLOW_PROBE)[0]
        assert ta._command_matches_permanent_allowlist(NONALLOW_PROBE) is False
        assert ta.check_all_command_guards(NONALLOW_PROBE, "local")["approved"] is False
        # deny floor: blocked unconditionally (user_deny), ahead of every bypass
        deny_res = ta.check_all_command_guards(DENY_PROBE, "local")
        assert deny_res["approved"] is False and deny_res.get("user_deny") is True
        monkeypatch.delenv("HERMES_SINGLE_QUERY_SESSION", raising=False)

        # (b) in-gateway fail-safe floor: process-global allowlist = DEFAULT (empty);
        # fresh-config readers scoped to the builder profile via a home override; cron context.
        _load_allowlist_for(default_home, default_mgd)      # _permanent_approved <- DEFAULT (empty)
        override_token = set_hermes_home_override(str(builder_home))
        managed_scope.invalidate_managed_cache()
        monkeypatch.setenv("HERMES_CRON_SESSION", "1")
        # The cron deny branch only runs when the guard sees no interactive/ask
        # context (is_cli/is_ask False, tools/approval.py:4788-4790,4805); scrub both.
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
        # builder WOULD allowlist ALLOW_PROBE, but the process-global set is the empty DEFAULT floor:
        assert ta._command_matches_permanent_allowlist(ALLOW_PROBE) is False
        # managed cron_mode=deny overrides the injected profile-local cron_mode=approve:
        assert ta._get_cron_approval_mode() == "deny"
        assert ta.check_all_command_guards(ALLOW_PROBE, "local")["approved"] is False
    finally:
        if override_token is not None:
            reset_hermes_home_override(override_token)
        monkeypatch.delenv("HERMES_SINGLE_QUERY_SESSION", raising=False)
        monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
        with ta._lock:
            ta._permanent_approved.clear()
            ta._permanent_approved.update(saved)
        managed_scope.invalidate_managed_cache()


# ---------------------------------------------------------------------------
# AC-GOV-F23-5 — per-role keys survive hermes config migrate
# ---------------------------------------------------------------------------
def test_ac_gov_f23_5(tmp_path, monkeypatch):
    """A coworker profile's command_allowlist + approvals.deny survive
    `hermes config migrate` unchanged (staged at the support floor, migrated to
    latest)."""
    home = _write_home(tmp_path, monkeypatch)
    loaded = _load(home)
    out_root = tmp_path / "out"
    rendered = _render(loaded, SPEC_UNSAFE, out_root)
    default_name = _default_profile_name(rendered)
    coworker = _coworker_names(rendered, default_name)[0]
    cfg = _raw_config(rendered[coworker])

    _, allow_before = _dig(cfg, "command_allowlist")
    _, deny_before = _dig(cfg, "approvals.deny")
    assert allow_before and deny_before

    mig_home = tmp_path / "migrate-home"
    mig_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(mig_home))
    staged = copy.deepcopy(cfg)
    staged["_config_version"] = SUPPORT_FLOOR_VERSION  # >= floor (not refused), < latest (ladder rewrites)
    (mig_home / "config.yaml").write_text(yaml.safe_dump(staged), encoding="utf-8")

    result = migrate_config(interactive=False, quiet=True)
    assert isinstance(result, dict)

    migrated = yaml.safe_load((mig_home / "config.yaml").read_text(encoding="utf-8")) or {}
    assert migrated.get("_config_version") == DEFAULT_CONFIG["_config_version"]
    assert migrated.get("_config_version") != SUPPORT_FLOOR_VERSION
    assert _dig(migrated, "command_allowlist")[1] == allow_before
    assert _dig(migrated, "approvals.deny")[1] == deny_before
