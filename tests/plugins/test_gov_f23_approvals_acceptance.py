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
    # Presentation-path fail-closed transport pins (stock defaults, made immutable).
    # NOT consulted on the unattended deny path (read only at approval.py:4303-4316
    # from :5138/:5631, after the non-interactive block); pinning them stops a flip of
    # transport_fallback to "builtin" on the human-approval/gateway path. AC-7 proves
    # they do NOT close the unattended detector-evading fail-open.
    "security.approval.transport": "builtin",
    "security.approval.transport_fallback": "deny",
}
_RESOLVER_KEYS = ("cron_mode", "single_query_mode", "unattended_mode")

# Per-role ALLOWLISTS the fixture declares for its two coworker types (exact-equality
# asserted in AC-2). TWO roles with DIFFERENT allowlists prove per-role binding —
# a shared blanket or a permissive ["*"] would fail. Allowlist entries are commands
# the dangerous-command detector flags (so the bypass is meaningful).
BUILDER_ALLOWLIST = ["rm -rf build", "git reset --hard*"]
REVIEWER_ALLOWLIST = ["git clean -fdx"]

# The deny floor is a UNIFORM fleet-safety floor — identical on builder, reviewer, and
# DEFAULT — so the RECOGNIZED force-push, tag, and release spellings it enumerates are
# refused unattended on every profile; per-role binding is proven by the allowlists above,
# not by differing floors. It is a best-effort in-surface backstop covering the common,
# combined, and global-option forms, NOT an exhaustive force-push detector, and NOT a
# guarantee that no force/tag/release spelling runs unattended (shell-wrapped detector-
# evading forms remain fail-open, AC-7). Robust refusal is the core dangerous-command
# detector's job — see the ADR §CORE-CHANGE detector ask / the GOV-ENF core row. The globs are fnmatch (fnmatch.fnmatchcase,
# as _match_user_deny_rule uses): `--forc*` matches --force/--force-with-lease in any
# argument order; the -f cluster globs use `[!- ]` (not-dash, not-space) to keep the force
# flag inside a single-dash short cluster so `*` cannot cross a space into a branch name
# (`git *push* -*f*` would over-block a normal `git push -u origin fix`) — one `[!- ]` per
# flag letter before `f`, covering f at cluster positions 1-3 (`-f`, `-uf`, `-uqf`);
# `git push* *+*` / `git -*push* *+*` match an unquoted or shell-quoted +refspec force-push,
# contiguous or behind a global option; the `git -* <verb> *` / `gh -* <verb> *` globs catch
# tag/release behind a global option. Order is significant: AC-2 asserts `rendered == DENY_FLOOR`
# in this exact order, so the RENDER must emit this exact order — the fixtures declare a weak or
# absent deny (never this floor), and the render overwrites it. Residual left to the core detector (defense-in-depth;
# fnmatch cannot bound them): an f at cluster position 4+ (`-uvqf`, unrealistic) and shell
# obfuscation / aliases. Known safe-side over-blocks: a branch literally named with `+`, and
# a commit whose message contains a force-push/tag verb token (both fail closed).
DENY_FLOOR = [
    "git *push* --forc*",
    "git *push* -f",
    "git *push* -f *",
    "git *push* -f[!- ]*",
    "git *push* -[!- ]f*",
    "git *push* -[!- ][!- ]f*",
    "git push* *+*",
    "git -*push* *+*",
    "git tag *",
    "git -* tag *",
    "gh release *",
    "gh -* release *",
]
BUILDER_DENY = DENY_FLOOR
REVIEWER_DENY = DENY_FLOOR
DEFAULT_DENY = DENY_FLOOR
EXPECTED_COWORKERS = {
    "builder": (BUILDER_ALLOWLIST, BUILDER_DENY),
    "reviewer": (REVIEWER_ALLOWLIST, REVIEWER_DENY),
}

# Behavioural probes for AC-4, run against the `builder` role.
ALLOW_PROBE = "rm -rf build"                  # flagged-dangerous AND in builder's allowlist
NONALLOW_PROBE = "git clean -fdx"             # flagged-dangerous, NOT in builder's allowlist
# Every form the deny floor must block unconditionally (user_deny), spanning reordered
# --force, standalone + combined-cluster -f, unquoted + shell-quoted +refspec, and
# tag/release behind a global option. The detector misses several of these, so the floor
# is what blocks them on a rendered worker.
FLOOR_DENY_PROBES = [
    "git push --force origin main",
    "git push origin main --force",              # reordered
    "git push -f origin main",                   # standalone -f
    "git push -uf origin main",                  # f-second cluster
    "git push -fu origin main",                  # f-first cluster
    "git push -fq origin main",
    "git push -qf origin main",
    "git push -uqf origin main",                 # f-third cluster
    "git push origin +main:main",                # +refspec, unquoted
    "git push origin '+main:main'",              # +refspec, shell-quoted
    "git -C /tmp/repo push origin +main:main",   # +refspec behind a global option
    "git -C /tmp/repo tag -f v9.9.9",            # tag behind a global option
    "gh --repo o/r release delete v9.9.9",       # release behind a global option
]
# Negative controls: common commands the floor must NOT over-match (incl. a global-option
# NORMAL push, proving the -* / *push* tolerance does not block a plain push).
FLOOR_ALLOW_PROBES = [
    "git push origin main",
    "git push -u origin fix",
    "git -C /tmp/repo push origin main",
    "git -C /tmp/repo push -u origin fix",
    "git commit -m 'push +1'",
    "git commit -m 'add tag support'",
]
# AC-4 ship-gate (i): a deny-EVADING but DETECTOR-COVERED command. The anchored deny floor
# misses the shell wrapper, but detect_dangerous_command searches (.search) over command
# variants (approval.py:2552-2555), so single_query_mode=deny still REFUSES it before spawn.
# The refusal is the detector's, NOT the floor's — so it is `approved is False` WITHOUT
# `user_deny` — which is exactly the render's real fail-safe mechanism where it holds.
WRAPPED_DETECTED_DENY_PROBE = "cd repo && git push --force"
# AC-7: the deny-EVADING AND detector-EVADING class. Two sub-classes: (a) shell-wrapped/
# spacing tag/release and wrapped +refspec — DANGEROUS_PATTERNS has no tag/release/+refspec
# entry anywhere in the list (approval.py:945-1271); (b) wrapped force-push whose spelling
# the force regexes (approval.py:1227-1228) also miss — a git global option splitting
# `git ... push`, or the force flag inside a combined cluster. In both the anchored floor
# misses the wrapper AND the pattern detector misses the verb, so neither of the two layers
# GOV-F23 configures refuses them; with Tirith allowing/disabled they are PERMITTED PAST the
# native guard (fall through to approval.py:4997 approved:True). This is the UNMET invariant,
# owned by the GOV-ENF core row — recorded honestly, never hidden; GOV-F23 claims NO safety
# credit for it.
FAILOPEN_PROBES = [
    "cd repo && git tag v1",
    "git  tag  v1",
    "cd repo && gh release create v9",
    "cd repo && git push origin +main:main",
    # Wrapped force-push forms the detector ALSO misses: the
    # detector regex needs a CONTIGUOUS `git push` + a standalone `--force`/`-f` token
    # (approval.py:1227-1228), so a git global option splitting the pair, or the force
    # flag inside a combined cluster, evades it — and the anchored floor misses the
    # wrapper. (Unwrapped, both are floor-caught; wrapped, both are fail-open.)
    "cd repo && git -C . push --force",   # global option splits `git ... push`
    "cd repo && git push -uf origin main",  # force flag inside a combined -uf cluster
]


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


def _spec_declared_denies(spec: Path) -> list:
    """Every approvals.deny declared ANYWHERE in the compose INPUT — each type's
    config, default_config, AND each referenced spine's config.approvals.deny
    (resolved relative to the spec file). Absent → nothing appended. Used to assert
    the input is non-compliant (weak/absent) at every source the deny could ride in
    on, so the render's enforcement is genuinely tested and a future compliant edit
    — at the type, DEFAULT, or spine level — fails the precondition loudly."""
    doc = yaml.safe_load(spec.read_text(encoding="utf-8")) or {}
    out = []
    for tdef in (doc.get("types") or {}).values():
        found, val = _dig(tdef or {}, "config.approvals.deny")
        if found:
            out.append(val)
    found, val = _dig(doc.get("default_config") or {}, "approvals.deny")
    if found:
        out.append(val)
    for sdef in (doc.get("spines") or {}).values():
        src = (sdef or {}).get("source")
        if not src:
            continue
        spine_path = spec.parent / src
        if not spine_path.exists():
            continue
        sdoc = yaml.safe_load(spine_path.read_text(encoding="utf-8")) or {}
        found, val = _dig(sdoc, "config.approvals.deny")
        if found:
            out.append(val)
    return out


def _input_declares(spec: Path, dotted: str) -> bool:
    """True if the compose INPUT declares a top-level `dotted` key — in default_config
    or in any referenced spine's config block. Used to prove a strip assertion is
    non-vacuous (the input actually carried the fleet-uniform key the render strips)."""
    doc = yaml.safe_load(spec.read_text(encoding="utf-8")) or {}
    if _dig(doc.get("default_config") or {}, dotted)[0]:
        return True
    for sdef in (doc.get("spines") or {}).values():
        src = (sdef or {}).get("source")
        if not src:
            continue
        spine_path = spec.parent / src
        if spine_path.exists():
            sdoc = yaml.safe_load(spine_path.read_text(encoding="utf-8")) or {}
            if _dig(sdoc.get("config") or {}, dotted)[0]:
                return True
    return False


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
def test_ac_gov_f23_2(tmp_path, monkeypatch):
    """Each coworker profile carries its EXACT per-role top-level command_allowlist
    (a spec passthrough — two roles with different allowlists prove per-role binding,
    not a shared blanket) and the UNIFORM approvals.deny floor, which the render
    ENFORCES (overwrites) rather than passing through: the fixtures declare a
    weak/absent deny, and the assertion `rendered == DENY_FLOOR` proves the render
    replaced it. The DEFAULT/multiplexer profile carries an EXPLICIT EMPTY
    command_allowlist (the in-gateway launch baseline) and the same enforced deny floor."""
    home = _write_home(tmp_path, monkeypatch)
    loaded = _load(home)
    for i, spec in enumerate(SPEC_VARIANTS):
        # False-green guard: the deny floor is a RENDER-ENFORCED uniform safety floor,
        # not a spec passthrough. The spec must declare a deny that DIFFERS from
        # DENY_FLOOR (weak/absent) on every type + default_config, so the postcondition
        # `rendered == DENY_FLOOR` proves the render OVERWROTE it. If a maintainer ever
        # makes a fixture declare DENY_FLOOR, this precondition fails loudly rather than
        # letting a silent passthrough render pass AC-2.
        declared_denies = _spec_declared_denies(spec)
        for declared in declared_denies:
            assert declared != DENY_FLOOR, (
                f"[{spec.name}] a fixture declares approvals.deny == DENY_FLOOR; the input "
                f"must be non-compliant so the render's enforcement is genuinely tested"
            )
        # Shape the adversarial inputs so both enforcement modes are exercised and no future
        # fixture edit can weaken the overwrite proof: the UNSAFE variant must declare at least
        # one (weak) deny (proves OVERWRITE), the OMITTED variant must declare none (proves INSERT).
        if spec is SPEC_UNSAFE:
            assert declared_denies, "UNSAFE variant must declare >=1 weak approvals.deny (proves OVERWRITE)"
        if spec is SPEC_OMITTED:
            assert not declared_denies, "OMITTED variant must declare NO approvals.deny (proves INSERT)"
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
            f"[{spec.name}] DEFAULT command_allowlist = {allow!r}, want [] (in-gateway launch baseline)"
        )
        found_deny, deny = _dig(default_cfg, "approvals.deny")
        assert found_deny and deny == DEFAULT_DENY, (
            f"[{spec.name}] DEFAULT approvals.deny = {deny!r}, want {DEFAULT_DENY!r}"
        )
        assert not _dig(default_cfg, "agent.command_allowlist")[0], (
            f"[{spec.name}] DEFAULT: command_allowlist under agent.* (a key with no reader)"
        )


# ---------------------------------------------------------------------------
def test_ac_gov_f23_3(tmp_path, monkeypatch):
    """Fleet-uniform keys live ONLY in the managed fragment; per-role keys live
    ONLY in profile configs; no rendered file sets any resolver/mode to approve."""
    home = _write_home(tmp_path, monkeypatch)
    loaded = _load(home)
    # Non-vacuous strip guard: the UNSAFE input must actually DECLARE the fleet-uniform
    # keys (spine config + default_config) so the "absent from profile config" assertions
    # below test the render's STRIP, not an input that never carried them. Cover an
    # approvals.* key AND both security.approval.* transport keys — a render that emits the
    # transport pins in managed scope but forgets to strip them from profile configs must
    # not read as green because the fixture happened not to declare them.
    for dotted in ("approvals.cron_mode", "security.approval.transport",
                   "security.approval.transport_fallback"):
        assert _input_declares(SPEC_UNSAFE, dotted), (
            f"UNSAFE fixture must DECLARE {dotted} in its input so the strip assertion is non-vacuous"
        )
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
def test_ac_gov_f23_4(tmp_path, monkeypatch):
    """(a) A coworker single-query worker with its own rendered config: its
    allowlisted flagged verb short-circuits APPROVED (before the single-query
    deny), a non-allowlisted flagged verb is DENIED (single_query_mode=deny), and
    its deny-floor verb is user-deny BLOCKED. (b) In-gateway LAUNCH BASELINE: with
    the process-global allowlist loaded from the DEFAULT profile's EMPTY allowlist,
    a non-allowlisted DETECTED verb run in an in-gateway cron context is DENIED by
    cron_mode=deny (and the managed cron_mode=deny overrides a conflicting
    profile-local cron_mode=approve, proving the overlay wins). This proves the DENY
    MODE, not isolation: the same block then HONESTLY records the cross-profile
    allowlist UNION leak (load_permanent UNIONs into the shared process-global set,
    approval.py:3062-3065; the allowlist short-circuit :4784 precedes the cron deny),
    which the empty DEFAULT does NOT close — a core gap owned by GOV-ENF/P8 with no
    isolation credit, unaffected only for separate-process `chat -q` workers."""
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
        # Deny floor: every ENUMERATED force/tag/release probe (FLOOR_DENY_PROBES) is blocked
        # unconditionally (user_deny), ahead of every bypass and independent of the dangerous-
        # command detector — which misses the reordered / combined-flag / global-option / +refspec
        # forms. Asserting user_deny proves the FLOOR blocks them, not the single-query path.
        for probe in FLOOR_DENY_PROBES:
            res = ta.check_all_command_guards(probe, "local")
            assert res["approved"] is False and res.get("user_deny") is True, probe
        # Negative controls: the floor must NOT over-match these common commands.
        for probe in FLOOR_ALLOW_PROBES:
            assert ta._match_user_deny_rule(probe) is None, probe
        # Ship-gate (i): a deny-EVADING but DETECTOR-COVERED command is still REFUSED before
        # spawn — by the detector via single_query_mode=deny, NOT by the floor. This is the
        # render's real fail-safe mechanism: the floor MISSES the wrapper (no user_deny), yet
        # the command does not execute because detection is position-independent.
        assert ta._match_user_deny_rule(WRAPPED_DETECTED_DENY_PROBE) is None
        assert ta.detect_dangerous_command(WRAPPED_DETECTED_DENY_PROBE)[0] is True
        wd_res = ta.check_all_command_guards(WRAPPED_DETECTED_DENY_PROBE, "local")
        assert wd_res["approved"] is False and not wd_res.get("user_deny")
        monkeypatch.delenv("HERMES_SINGLE_QUERY_SESSION", raising=False)

        # (b) in-gateway LAUNCH BASELINE (NOT steady-state isolation): at gateway launch the
        # process-global allowlist is the DEFAULT profile's EMPTY set, so a non-allowlisted
        # DETECTED command is denied by cron_mode=deny. This proves the DENY MODE works — it
        # does NOT prove the empty DEFAULT provides cross-profile isolation (see the union-leak
        # record below). fresh-config readers scoped to the builder profile via a home override.
        _load_allowlist_for(default_home, default_mgd)      # _permanent_approved <- DEFAULT (empty)
        override_token = set_hermes_home_override(str(builder_home))
        managed_scope.invalidate_managed_cache()
        monkeypatch.setenv("HERMES_CRON_SESSION", "1")
        # The cron deny branch only runs when the guard sees no interactive/ask
        # context (is_cli/is_ask False, tools/approval.py:4788-4790,4805); scrub both.
        monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
        monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
        assert ta._command_matches_permanent_allowlist(ALLOW_PROBE) is False  # empty launch baseline
        # managed cron_mode=deny overrides the injected profile-local cron_mode=approve, and all
        # three unattended resolvers are effective from the managed fragment (not just raw YAML):
        assert ta._get_cron_approval_mode() == "deny"
        assert ta._get_unattended_approval_mode() == "deny"
        assert ta.check_all_command_guards(ALLOW_PROBE, "local")["approved"] is False  # cron_mode=deny denies the DETECTED command

        # HONEST record of the in-gateway cross-profile allowlist UNION leak — a core behavior
        # GOV-F23 config CANNOT close (owned by GOV-ENF/P8, NO isolation credit): load_permanent()
        # UNIONs each profile's allowlist into the ONE shared-gateway process-global set
        # (approval.py:3062-3065), and the allowlist short-circuit (:4784) fires BEFORE the cron
        # deny (:4876). So once a builder in-gateway session init unions builder's allowlist in, a
        # builder-allowlisted command is APPROVED even under cron_mode=deny — the empty DEFAULT
        # baseline does not survive it. (Separate-process kanban `chat -q` workers keep their own
        # _permanent_approved and are unaffected; this leak is specific to in-gateway multiplex.)
        # Prove the CROSS-profile leak, not same-profile allowlisting: after builder's session init
        # unions its allowlist into the shared set, SWITCH the active override to the reviewer
        # profile (whose OWN allowlist does NOT contain ALLOW_PROBE) and show ALLOW_PROBE is still
        # matched + approved — builder's entry leaked across the profile boundary. A future
        # profile-keyed isolation (GOV-ENF/P8) would flip these to False.
        ta.load_permanent_allowlist()  # builder home active -> UNIONs builder's allowlist into the shared set
        reviewer_home, _reviewer_mgd = _materialize(rendered["reviewer"], "reviewer")
        reset_hermes_home_override(override_token)
        override_token = set_hermes_home_override(str(reviewer_home))
        managed_scope.invalidate_managed_cache()
        assert ALLOW_PROBE not in (_raw_config(rendered["reviewer"]).get("command_allowlist") or [])  # not reviewer's own
        assert ta._command_matches_permanent_allowlist(ALLOW_PROBE) is True  # builder's allowlist leaked into the shared set — visible to reviewer
        assert ta.check_all_command_guards(ALLOW_PROBE, "local")["approved"] is True  # short-circuit approves before cron deny — the UNMET cross-profile core gap, GOV-ENF owns it
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


# ---------------------------------------------------------------------------
def test_ac_gov_f23_7(tmp_path, monkeypatch):
    """The deny-EVADING AND detector-EVADING class — (a) shell-wrapped / double-spaced
    tag & release and wrapped +refspec (no DANGEROUS_PATTERNS entry, approval.py:945-1271),
    and (b) wrapped force-push whose spelling the force regexes (approval.py:1227-1228)
    miss: a git global option splitting `git ... push`, or the force flag inside a combined
    cluster — is NEITHER matched by the anchored fnmatch deny floor NOR flagged by
    DANGEROUS_PATTERNS — the two layers GOV-F23 configures. With the
    Tirith content scanner neutralized to `allow` (an independent subsystem this row does
    not configure; absent -> tirith_fail_open default True -> allow), a single-query worker
    PERMITS these PAST the native guard (falls through to approval.py:4997 approved:True).
    This records that UNMET invariant
    HONESTLY (never hidden): GOV-F23 ships the CONFIG with NO safety-completion credit for
    it; robust pre-execution mediation that would refuse this class is owned by the GOV-ENF
    core row, and its irreversible-outbound effects are contained at RUNTIME/DEPLOYMENT by
    the operator's credential ACL before any real-credential unattended run (NOT a merge
    pre-condition; ADR §Ship gate B), with GOV-ENF the lifter. This test asserts only the
    authorization DECISION on a hermetic fixture, never an actual outbound (§Ship gate C).
    When GOV-ENF lands these assertions FLIP (the class becomes refused) — that flip is the
    signal GOV-ENF closed the gap, and this test is updated then. A PASS here means the gap
    is present and documented as expected, NOT that the invariant is met."""
    import tools.approval as ta
    from hermes_cli import managed_scope

    home = _write_home(tmp_path, monkeypatch)
    loaded = _load(home)
    out_root = tmp_path / "out"
    rendered = _render(loaded, SPEC_UNSAFE, out_root)
    assert "builder" in rendered
    frag = _managed_fragment(out_root)

    eff = tmp_path / "eff-failopen"
    eff.mkdir()
    (eff / "config.yaml").write_text(
        (Path(rendered["builder"]) / "config.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    mgd = tmp_path / "mgd-failopen"
    mgd.mkdir()
    (mgd / "config.yaml").write_text(yaml.safe_dump(frag), encoding="utf-8")

    saved = set(ta._permanent_approved)
    try:
        monkeypatch.setenv("HERMES_HOME", str(eff))
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(mgd))
        managed_scope.invalidate_managed_cache()
        with ta._lock:
            ta._permanent_approved.clear()
        ta.load_permanent_allowlist()
        monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "1")
        assert ta._get_single_query_approval_mode() == "deny"
        # Isolate the layer GOV-F23 configures. The unattended-deny branch ALSO consults the
        # Tirith content scanner (approval.py:4828+), an independent subsystem (security.tirith_*)
        # this row neither configures nor relies on. Neutralize it to `allow` so the assertion
        # is about the pattern-detection + approvals.deny layers ONLY: those are what GOV-F23
        # renders, and they do NOT refuse the detector-evading class. (If Tirith is absent the
        # branch honors security.tirith_fail_open, default True -> allow, so the result is the
        # same either way; pinning it makes the test deterministic across Tirith availability.)
        try:
            import tools.tirith_security as _tsec
            monkeypatch.setattr(
                _tsec, "check_command_security",
                lambda *a, **k: {"action": "allow", "findings": [], "summary": ""},
            )
        except ImportError:
            pass  # Tirith not installed; guard honors tirith_fail_open (default True -> allow)
        # Both layers GOV-F23 configures fail to recognize the probe (deny-floor miss AND
        # pattern-detector miss), so with Tirith neutralized the guard approves it — the
        # UNMET invariant GOV-ENF owns, recorded here rather than hidden.
        for probe in FAILOPEN_PROBES:
            assert ta._match_user_deny_rule(probe) is None, probe
            assert ta.detect_dangerous_command(probe)[0] is False, probe
            assert ta.check_all_command_guards(probe, "local")["approved"] is True, probe
    finally:
        monkeypatch.delenv("HERMES_SINGLE_QUERY_SESSION", raising=False)
        with ta._lock:
            ta._permanent_approved.clear()
            ta._permanent_approved.update(saved)
        managed_scope.invalidate_managed_cache()
