"""Hermetic acceptance tests for FLEET-F62.d — e2e readiness for the base-NanoClaw fleet on OpenShell
(ships to ``tests/plugins/test_fleet_f62_d_acceptance.py``).

One ``test_ac_fleet_f62_d_<n>`` per ``pytest:`` row of the ADR's ``## Acceptance criteria`` (AC-1..AC-16, in
order); the docstring's first line is the criterion text. AC-17 and AC-18 are ``live:`` scenario files, not here.

Shape: the FLEET-F62.c acceptance test's isolated ``HERMES_HOME`` (the two fleet plugins copied in, an EMPTY
bundled-plugin dir) driving the real ``hermes coworker compose`` subprocess; the installer is imported by file
path and driven through ``build_plan`` / ``collect_home_state`` / ``_execute_step`` with ``subprocess`` captured
(the OSH-F64 shape); nv-fleet-gates is loaded through ``PluginManager().discover_and_load()`` and driven by
``invoke_hook("pre_tool_call", ...)`` (the nv-fleet-gates shape).

Behaviour contract, not a snapshot: no network, nothing under ``~/.hermes``, no plugin source is read. The
inputs read as files (the worker Dockerfile, the rendered distribution, the spec skill text, the runbook page)
are the shipped artefacts whose behaviour each criterion names. Wording rule (ADR § Hand-off wording rule): no
credential value, header, variable name or recipe appears here — structure only.

Fails on ``d0402936`` (each test's docstring tail names its base-red arm) and passes once the ADR's D1-D13 land.
"""

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

HOME_TOKEN = "@HERMES_HOME@"

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGINS_SRC = REPO_ROOT / "plugins"
COMPOSE_KEY = "nv-coworker-compose"
GATES_KEY = "nv-fleet-gates"
FLEET_PLUGINS = (COMPOSE_KEY, GATES_KEY)
OPENSHELL_DIR = PLUGINS_SRC / COMPOSE_KEY / "openshell"

F62C = REPO_ROOT / "tests" / "e2e-scenarios" / "FLEET-F62.c"
SPEC = F62C / "spec" / "openshell" / "coworker-types.yaml"
OSH_F63 = REPO_ROOT / "tests" / "e2e-scenarios" / "OSH-F63"
OSH_F63_SPEC = OSH_F63 / "spec" / "openshell" / "coworker-types.yaml"
OSH_F63_GOLDEN = OSH_F63 / "fixtures" / "openshell"
LINT_GOLDEN = REPO_ROOT / "tests" / "e2e-scenarios" / "OSH-F64" / "fixtures" / "dockerfile.worker.lint.txt"
RUNBOOK = REPO_ROOT / "website" / "docs" / "user-guide" / "fleet-openshell.md"

PROJECT = "nanoclaw-base"
COWORKERS = ("orchestrator", "triager", "fixer", "reviewer", "approver")
GITHUB_WORKERS = ("triager", "fixer", "reviewer")
NON_PROVIDER_WORKERS = ("orchestrator", "approver")
GITHUB_HOSTS = {"api.github.com", "github.com"}
# E2E-9 scope: the shared `anonymous_reads` renderer input (ING-F66 D11) — GET-only on the API host, base curl only.
APPROVER_GITHUB = [("api.github.com", 443)]
ANON_READ_RULES = [{"allow": {"method": "GET", "path": "/**"}}]
# The release always-ask protected instruction basenames (release tools/file_tools.py:736-738).
PROTECTED_BASENAMES = ("AGENTS.md", "CLAUDE.md", "SOUL.md", ".cursorrules")
# Literal-credential shapes (structure only — never a real value): GitHub token prefixes, plus any
# "<scheme> <long opaque blob>" auth-value shape.
_LITERAL_TOKEN_RE = re.compile(r"gh[opsur]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}")
_AUTH_VALUE_RE = re.compile(r"(?i)\b(?:basic|bearer|token)\s+[A-Za-z0-9+/=_\-]{24,}")
SHA = "a" * 40


# --------------------------------------------------------------------------- render harness
def _isolated_home(tmp_path):
    """HERMES_HOME with the fleet plugins copied in and an empty bundled dir. On a tree without the plugins
    nothing is copied and the compose subprocess fails (the stock-tree red arm)."""
    home = tmp_path / "home" / ".hermes"
    (home / "plugins").mkdir(parents=True)
    for key in FLEET_PLUGINS:
        src = PLUGINS_SRC / key
        if src.is_dir():
            shutil.copytree(src, home / "plugins" / key)
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": list(FLEET_PLUGINS)}}), encoding="utf-8")
    (tmp_path / "empty-bundled").mkdir(exist_ok=True)
    return home


def _compose(spec, out, home, *extra):
    env = dict(os.environ)
    env.update(
        HOME=str(home.parent),
        HERMES_HOME=str(home),
        HERMES_BUNDLED_PLUGINS=str(home.parent.parent / "empty-bundled"),
        HERMES_ENABLE_PROJECT_PLUGINS="0",
    )
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "coworker", "compose", str(spec), "--out", str(out), *extra],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
    )


@pytest.fixture
def rendered(tmp_path):
    """(out_dir, provision_plan_lines) for the F62.c openshell spec at this head."""
    home = _isolated_home(tmp_path)
    out = tmp_path / "out"
    proc = _compose(SPEC, out, home, "--provision-dry-run")
    assert proc.returncode == 0, f"compose failed: {proc.stderr[-2000:]}"
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip().startswith("openshell ")]
    assert lines, "compose --provision-dry-run printed no openshell plan"
    return out, lines


def _config(out, role):
    return yaml.safe_load((out / role / "config.yaml").read_text(encoding="utf-8")) or {}


def _policy(out, role):
    path = out / role / f"policy-{role}.yaml"
    assert path.is_file(), f"no rendered policy for {role}: {path}"
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _fs(policy):
    return policy.get("filesystem_policy") or {}


def _endpoints(policy):
    out = []
    for np in (policy.get("network_policies") or {}).values():
        for ep in (np or {}).get("endpoints") or []:
            out.append(ep)
    return out


def _create_line(lines, role):
    hits = [ln for ln in lines if ln.startswith("openshell sandbox create ") and f"--name {PROJECT}-{role} " in ln + " "]
    assert len(hits) == 1, f"{role}: expected exactly one create line, got {hits}"
    return hits[0].split()


def _opt_values(argv, flag):
    return [argv[i + 1] for i, tok in enumerate(argv[:-1]) if tok == flag]


def _covered(path, entries):
    """True when ``path`` equals an entry or sits under it (directory prefix on a component boundary)."""
    p = path.rstrip("/")
    return any(p == e.rstrip("/") or p.startswith(e.rstrip("/") + "/") for e in entries)


# --------------------------------------------------------------------------- installer harness
def _installer(tag="fleet_f62d_installer"):
    path = OPENSHELL_DIR / "installer.py"
    if not path.is_file():
        pytest.fail(f"installer missing at {path}")
    spec = importlib.util.spec_from_file_location(tag, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def inst_env(tmp_path, monkeypatch):
    """Isolated HERMES_HOME / HERMES_MANAGED_DIR / HOME for the installer (never the real home)."""
    home = tmp_path / "ihome"
    (home / "plugins").mkdir(parents=True)
    managed = tmp_path / "managed"
    managed.mkdir()
    user_home = tmp_path / "user"
    user_home.mkdir()
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    return home, managed, user_home


def _spec_data():
    return yaml.safe_load(SPEC.read_text(encoding="utf-8")) or {}


# ============================================================================ AC-1 .. AC-5 (fixes 2-5)
def test_ac_fleet_f62_d_1(tmp_path, rendered):
    """(fix 2, E2E-8) The compose provisioning plan binds each provider-opted worker's attached providers on its create line, one create-time provider option per provider, and contains no post-create attach line; a providers-free fleet's plan and policies stay byte-identical to the committed OSH-F63 goldens (FLEET-F62.c AC-10 unchanged).

    Base red (d0402936): the F62.c plan has three `sandbox provider attach` lines and no create-line provider."""
    _out, lines = rendered
    assert not [ln for ln in lines if "provider attach" in ln], "no post-create provider attach line may remain"
    for role in GITHUB_WORKERS:
        assert _opt_values(_create_line(lines, role), "--provider") == ["github"], \
            f"{role}: create line must bind exactly the github provider at create"
    for role in NON_PROVIDER_WORKERS:
        assert _opt_values(_create_line(lines, role), "--provider") == [], f"{role}: create line must bind no provider"
    # Default-off: the providers-free OSH-F63 fleet's plan and policies stay byte-identical to its goldens.
    home = _isolated_home(tmp_path / "f63")
    out63 = tmp_path / "f63-out"
    prov = _compose(OSH_F63_SPEC, out63, home, "--provision-dry-run")
    assert prov.returncode == 0, prov.stderr[-2000:]
    assert prov.stdout.encode("utf-8") == (OSH_F63_GOLDEN / "provision.dry-run.txt").read_bytes(), \
        "OSH-F63 provision plan changed (default-off regression)"
    policies = sorted(out63.glob("*/policy-*.yaml"))
    assert policies, "OSH-F63 render produced no policy files"
    for pol in policies:
        golden = OSH_F63_GOLDEN / f"osh-f63-{pol.parent.name}" / pol.name
        assert pol.read_bytes() == golden.read_bytes(), f"OSH-F63 {pol.name} changed (default-off regression)"


def test_ac_fleet_f62_d_2(inst_env):
    """(fix 2, E2E-6) The installer's executed sandbox-create step is non-interactive and binds the same create-time providers as the plan: every create argv carries the no-tty option, provider-opted workers carry one provider option per attached provider, and no installer step attaches a provider after create.

    Base red: the installer create argv (installer.py:534) carries neither --no-tty nor --provider."""
    inst = _installer("fleet_f62d_inst_ac2")
    steps = inst.build_plan({}, str(SPEC), SHA)
    creates = {}
    for step in steps:
        if getattr(step, "tag", None) == "provision_create":
            argv = list(step.command)
            creates[argv[argv.index("--name") + 1]] = argv
    assert set(creates) == {f"{PROJECT}-{r}" for r in COWORKERS}, f"create set {sorted(creates)}"
    for name, argv in creates.items():
        assert "--no-tty" in argv, f"{name}: the executed create must be non-interactive"
        role = name[len(PROJECT) + 1:]
        want = ["github"] if role in GITHUB_WORKERS else []
        assert _opt_values(argv, "--provider") == want, f"{name}: create-time providers {_opt_values(argv, '--provider')}"
    for step in steps:
        cmd = step.command if isinstance(step.command, (list, tuple)) else str(step.command).split()
        assert not ("provider" in cmd and "attach" in cmd), f"post-create attach step found: {cmd}"


def test_ac_fleet_f62_d_3(tmp_path, rendered):
    """(fix 3) Every F62.c worker policy grants read-write to the null device through the spec's new opt-in `egress.filesystem_read_write` key, and a spec that does not declare that key renders the unchanged `read_write` floor (OSH-F63 policies byte-identical).

    Base red: there is no egress.filesystem_read_write and every worker read_write is ["/tmp"]."""
    out, _lines = rendered
    rw_spec = (_spec_data().get("egress") or {}).get("filesystem_read_write")
    assert isinstance(rw_spec, list) and "/dev/null" in rw_spec, "spec must declare egress.filesystem_read_write"
    for role in COWORKERS:
        rw = _fs(_policy(out, role)).get("read_write") or []
        assert "/dev/null" in rw, f"{role}: read_write {rw} must grant the null device"
    # Absent key → the unchanged floor. The spec tree is copied so its relative spine/skill roots resolve.
    spec_copy = tmp_path / "spec-copy"
    shutil.copytree(SPEC.parent, spec_copy)
    data = _spec_data()
    data["egress"].pop("filesystem_read_write")
    variant = spec_copy / SPEC.name
    variant.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    proc = _compose(variant, tmp_path / "noKey-out", _isolated_home(tmp_path / "noKey"))
    assert proc.returncode == 0, proc.stderr[-2000:]
    for role in COWORKERS:
        assert _fs(_policy(tmp_path / "noKey-out", role)).get("read_write") == ["/tmp"], \
            f"{role}: a spec without the key must keep the read_write floor"
    for i, bad in enumerate((["dev/null"], ["/dev/*"], ["/tmp/../etc"], ["/"], [])):
        data["egress"]["filesystem_read_write"] = bad
        variant.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        bad_proc = _compose(variant, tmp_path / f"bad-out-{i}", _isolated_home(tmp_path / f"bad-{i}"))
        assert bad_proc.returncode != 0, f"malformed filesystem_read_write {bad!r} must fail the render"


def test_ac_fleet_f62_d_4():
    """(fix 4, E2E-8) The pinned worker image installs the GitHub CLI from a version-pinned package at the exact path every provider-opted policy names, and still lints clean.

    Base red: Dockerfile.worker installs no gh package."""
    spec = importlib.util.spec_from_file_location("fleet_f62d_lint", OPENSHELL_DIR / "dockerfile_lint.py")
    lint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lint)
    dockerfile = OPENSHELL_DIR / "Dockerfile.worker"
    result = lint.lint(str(dockerfile))
    assert result.errors == 0, f"Dockerfile.worker lint errors: {result.findings}"
    assert result.report == LINT_GOLDEN.read_text(encoding="utf-8"), "lint report must stay the committed zero-finding golden"
    logical = re.sub(r"\\\s*\n", " ", dockerfile.read_text(encoding="utf-8"))
    installs = [ln for ln in logical.splitlines() if ln.lstrip().startswith("RUN ") and re.search(r"\bapt(?:-get)?\s+install\b", ln)]
    assert any(re.search(r"(?<![\w-])gh=[0-9][^\s]*", ln) for ln in installs), \
        "the GitHub CLI must be installed from a version-pinned apt package (gh=<version>)"
    gh_bins = [b for b in ((_spec_data().get("providers") or {}).get("github") or {}).get("binaries") or []
               if Path(b).name == "gh"]
    assert gh_bins == ["/usr/bin/gh"], f"the github provider must name the Debian package's install path, got {gh_bins}"


def _gitconfig_sections(text):
    """[(section_header, {key: value})] parsed line-wise (git config grammar subset; no configparser)."""
    sections, cur = [], None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            cur = (line[1:-1].strip(), [])
            sections.append(cur)
            continue
        assert cur is not None, f"gitconfig key outside a section: {raw!r}"
        key, _, value = line.partition("=")
        cur[1].append((key.strip().lower(), value.strip()))
    return sections


_SECRET_KEY_RE = re.compile(r"(?i)(api[_-]?key|token|secret|passw|credential|auth)")
_ENV_REF_RE = re.compile(r"^\$\{env:[A-Z][A-Z0-9_]*\}$")


_SENTINEL_SHAPE = re.compile(r"dummy-[a-z0-9-]+-not-a-real-key")


def _spine_sentinels(spine_path=None):
    """The spec's declared provider key sentinels (spine `providers.<p>.api_key`). Origin is not trust: each must
    have the self-declaring non-secret shape, or the spine itself carries an inline value."""
    spine = yaml.safe_load(Path(spine_path or SPEC.parent / "spines" / "base.yaml").read_text(encoding="utf-8")) or {}
    provs = ((spine.get("config") or {}).get("providers") or {})
    sentinels = {str(v.get("api_key")) for v in provs.values() if isinstance(v, dict) and v.get("api_key")}
    assert all(_SENTINEL_SHAPE.fullmatch(value) for value in sentinels), "spine carries an inline provider key value"
    return sentinels


def _inline_secret_leaves(cfg, sentinels, path=()):
    """Every secret-named scalar leaf whose value is neither an env reference nor a declared sentinel."""
    bad = []
    if isinstance(cfg, dict):
        for k, v in cfg.items():
            bad += _inline_secret_leaves(v, sentinels, path + (str(k),))
    elif isinstance(cfg, list):
        for i, v in enumerate(cfg):
            bad += _inline_secret_leaves(v, sentinels, path + (str(i),))
    elif isinstance(cfg, str) and path and _SECRET_KEY_RE.search(path[-1]):
        if not (_ENV_REF_RE.match(cfg) or cfg in sentinels or cfg == ""):
            bad.append(".".join(path))
    return bad


GITCONFIG_REL = "skills/nv-gitconfig/gitconfig-github"
REMOTE_HERMES = "/home/sandbox/.hermes"


def _assert_stock_delivery(out):
    """The rendered gitconfig reaches each worker through the stock, profile-scoped ssh skills sync, at exactly the
    absolute path the image's system git include names. The profiles are switched in ONE process the way the
    multiplexed gateway does (home override, no cache reset), so a process-wide list cannot leak or drop the file."""
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from tools.environments.file_sync import iter_sync_files
    target = f"{REMOTE_HERMES}/{GITCONFIG_REL}"
    for role in ("fixer", "approver", "triager"):
        profile_home = out.parent / f"installed-{role}"
        if not profile_home.exists():
            shutil.copytree(out / role, profile_home)
        token = set_hermes_home_override(str(profile_home))
        try:
            synced = {remote for _host, remote in iter_sync_files(REMOTE_HERMES)}
        finally:
            reset_hermes_home_override(token)
        if role in GITHUB_WORKERS:
            assert target in synced, f"{role}: the stock ssh sync must upload the gitconfig to {target}"
        else:
            assert target not in synced, f"{role}: another profile's gitconfig leaked into this profile's sync"
    logical = re.sub(r"\\\s*\n", " ", (OPENSHELL_DIR / "Dockerfile.worker").read_text(encoding="utf-8"))
    includes = re.findall(r"git\s+config\s+--system\s+(?:--add\s+)?include\.path\s+['\"]?([^\s'\"&]+)", logical)
    assert includes == [target], f"the image's system git include must name exactly {target}, got {includes}"


def test_ac_fleet_f62_d_5(rendered):
    """(fix 5) Each provider-opted worker's render carries a git configuration that selects the credential source the provider rewrites for the GitHub host, delivered into the worker by a stock Hermes channel; no rendered file holds a literal secret; non-provider profiles carry none of it.

    Base red: no worker carries a rendered gitconfig under skills/nv-gitconfig/."""
    out, _lines = rendered
    for role in GITHUB_WORKERS:
        cfg = _config(out, role)
        assert not ((cfg.get("terminal") or {}).get("credential_files")), \
            f"{role}: delivery must not use the process-wide terminal.credential_files list"
        gitcfg = out / role / GITCONFIG_REL
        assert gitcfg.is_file(), f"{role}: rendered gitconfig {GITCONFIG_REL} missing"
        assert not (gitcfg.parent / "SKILL.md").exists(), f"{role}: the gitconfig dir must not be a discoverable skill"
        owned = (yaml.safe_load((out / role / "distribution.yaml").read_text(encoding="utf-8")) or {}).get("distribution_owned") or []
        assert "skills" in owned, f"{role}: skills must be distribution-owned so install carries the gitconfig"
        sections = _gitconfig_sections(gitcfg.read_text(encoding="utf-8"))
        host_creds = [kv for hdr, kv in sections
                      if re.fullmatch(r'credential\s+"https://github\.com/?"', hdr)]
        assert len(host_creds) == 1, f"{role}: exactly one host-scoped credential section for https://github.com"
        helpers = [v for k, v in host_creds[0] if k == "helper"]
        assert len(helpers) >= 2 and helpers[0] in ("", '""'), \
            f"{role}: the helper list must reset inherited helpers before selecting the provider-backed one"
        assert helpers[1:] in (["!gh auth git-credential"], ["!/usr/bin/gh auth git-credential"]), \
            f"{role}: after the reset, the only helper must be the image GitHub CLI's git-credential mode, got {helpers[1:]}"
        for _hdr, kv in sections:
            for key, value in kv:
                assert key not in ("username", "password", "extraheader"), \
                    f"{role}: the gitconfig must carry no credential value (found key {key!r})"
    _assert_stock_delivery(out)
    for role in NON_PROVIDER_WORKERS + ("default",):
        cfg = _config(out, role)
        assert not ((cfg.get("terminal") or {}).get("credential_files")), f"{role}: no credential_files expected"
        assert not (out / role / GITCONFIG_REL).parent.exists(), f"{role}: no rendered gitconfig expected"
    # No literal secret anywhere in the render: every secret-named config leaf is an env reference or the spec's
    # declared sentinel (negative control first, so a vacuous checker cannot pass), and no file carries a
    # credential shape (the redactor must be a fixed point).
    sentinels = _spine_sentinels()
    assert sentinels, "the spec must declare its non-secret provider key sentinel"
    bad_spine = out.parent / "bad-spine.yaml"
    bad_spine.write_text(yaml.safe_dump({"config": {"providers": {"p": {"api_" + "key": "Q" * 32}}}}), encoding="utf-8")
    with pytest.raises(AssertionError):
        _spine_sentinels(bad_spine)   # an arbitrary value declared in the spine is not a sentinel
    synthetic = {"providers": {"p": {"api_" + "key": "Z" * 32}}}
    assert _inline_secret_leaves(synthetic, sentinels) == ["providers.p.api_key"], "secret-leaf checker is vacuous"
    for role in COWORKERS + ("default",):
        assert not _inline_secret_leaves(_config(out, role), sentinels), \
            f"{role}: inline secret value(s) at {_inline_secret_leaves(_config(out, role), sentinels)}"
    from agent.redact import redact_sensitive_text
    for path in sorted(p for p in out.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        text = path.read_text(encoding="utf-8", errors="replace")
        assert not _LITERAL_TOKEN_RE.search(text), f"{path.relative_to(out)}: literal token shape"
        assert not _AUTH_VALUE_RE.search(text), f"{path.relative_to(out)}: literal auth-value shape"
        if path.suffix != ".py":
            assert redact_sensitive_text(text, force=True) == text, f"{path.relative_to(out)}: redactor would mask a value"


# ============================================================================ AC-6 .. AC-10 (installer, E2E-1..5)
def test_ac_fleet_f62_d_6(inst_env, monkeypatch):
    """(E2E-1) The installer's pre-reinstall plugin snapshot lands outside every scanned plugins dir, so discovery after a reinstall loads exactly one plugin per key and it is the fresh one.

    Base red: the snapshot is copied to <plugins>/<name>.osh-f64.bak, which the scanner loads under the same key."""
    home, _managed, _user = inst_env
    inst = _installer("fleet_f62d_inst_ac6")
    plugins_dir = home / "plugins"
    fixture = plugins_dir / "f62d-probe"
    fixture.mkdir()
    (fixture / "plugin.yaml").write_text(
        "manifest_version: 1\nname: f62d-probe\nversion: 1.0.0\ndescription: probe\nprovides_tools: []\nprovides_hooks: []\n",
        encoding="utf-8")
    (fixture / "__init__.py").write_text("MARK = 'old'\n\ndef register(ctx):\n    pass\n", encoding="utf-8")
    # A legacy snapshot left by the base installer (same manifest name, inside the scanned dir) must be migrated
    # out — preserved, not deleted — on a plan that schedules no --force at all.
    legacy = plugins_dir / "f62d-legacy.osh-f64.bak"
    shutil.copytree(fixture, legacy)
    (legacy / "plugin.yaml").write_text((fixture / "plugin.yaml").read_text(encoding="utf-8").replace("f62d-probe", "f62d-legacy"),
                                        encoding="utf-8")
    (plugins_dir / "f62d-legacy").mkdir()
    (plugins_dir / "f62d-legacy" / "plugin.yaml").write_text((legacy / "plugin.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    (plugins_dir / "f62d-legacy" / "__init__.py").write_text("MARK = 'fresh'\n\ndef register(ctx):\n    pass\n", encoding="utf-8")
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    state = inst.collect_home_state(spec, str(SPEC))
    migrate = [s for s in inst.build_plan(state, str(SPEC), SHA) if getattr(s, "tag", None) == "legacy_snapshot_migrate"]
    assert len(migrate) == 1, "a home carrying a legacy in-scan snapshot must plan exactly one migration step"
    ctx = inst._apply_ctx(spec, str(SPEC))
    inst._execute_step(migrate[0], ctx)
    assert not legacy.exists(), "the legacy snapshot must leave the scanned plugins dir"
    moved = [p for p in home.parent.rglob("plugin.yaml") if "f62d-legacy" in p.read_text(encoding="utf-8")
             and plugins_dir not in p.parents]
    assert len(moved) == 1, f"the legacy snapshot must be preserved outside the scan root, got {moved}"
    # The Phase-A entry point (which filters executable tags) must also run the migration before any bootstrap install.
    shutil.copytree(moved[0].parent, legacy)
    # phase_a refuses a spec without the pinned offline lane; use a lane-enabled copy, never edit the F62.c spec.
    phase_spec_dir = home.parent / "phase-a-spec"
    shutil.copytree(SPEC.parent, phase_spec_dir)
    phase_spec = phase_spec_dir / SPEC.name
    phase_data = yaml.safe_load(phase_spec.read_text(encoding="utf-8"))
    phase_data.setdefault("egress", {})["pinned_offline_lane"] = True
    phase_spec.write_text(yaml.safe_dump(phase_data), encoding="utf-8")
    ran, real_execute = [], inst._execute_step
    inst._execute_step = lambda step, c: ran.append(getattr(step, "tag", None))
    try:
        inst.phase_a(str(phase_spec), SHA)
    finally:
        inst._execute_step = real_execute
    assert "legacy_snapshot_migrate" in ran, f"phase_a must run the legacy migration, ran {ran}"
    installs = [i for i, tag in enumerate(ran) if tag in ("plugin_install", "plugin_force", "plugin_enable")]
    assert not installs or ran.index("legacy_snapshot_migrate") < min(installs), "migration must precede bootstrap installs"
    shutil.rmtree(legacy)
    snap = inst._snapshot_step(str(plugins_dir), "f62d-probe")
    assert getattr(snap, "tag", None) == "plugin_snapshot"
    inst._execute_step(snap, {"home": str(home), "managed_dir": "", "out": str(home / ".osh-f64" / "render")})
    # Simulate the --force reinstall writing the fresh copy in place.
    (fixture / "__init__.py").write_text("MARK = 'fresh'\n\ndef register(ctx):\n    pass\n", encoding="utf-8")
    manifests = [p for p in plugins_dir.rglob("plugin.yaml") if "f62d-probe" in p.read_text(encoding="utf-8")]
    assert manifests == [fixture / "plugin.yaml"], f"a snapshot copy sits inside the scanned plugins dir: {manifests}"
    bundled = home.parent / "empty-bundled"
    bundled.mkdir(exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": ["f62d-probe", "f62d-legacy"]}}), encoding="utf-8")
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    from hermes_cli.plugins import PluginManager
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins["f62d-probe"]
    assert loaded.enabled is True and loaded.error is None and loaded.module is not None
    assert getattr(loaded.module, "MARK", None) == "fresh", "discovery loaded the snapshot instead of the fresh install"
    legacy_loaded = manager._plugins["f62d-legacy"]
    assert getattr(legacy_loaded.module, "MARK", None) == "fresh", "a migrated legacy snapshot must no longer shadow the install"


def _managed_render_maps(spec):
    roles = [t for t in (spec.get("types") or {}) if t != spec.get("default_profile", "default")]
    orch = spec.get("orchestrator_profile")
    return ({r: ("orchestrator" if r == orch else "worker") for r in roles},
            {r: f"openshell-{spec['project']}-{r}" for r in roles})


def _managed_fragment(roles, hosts):
    return {
        "approvals": {"mode": "smart"},
        "security": {"approval": {"transport": "builtin"}},
        "logging": {"level": "debug"},
        "plugins": {"scan_on_install": False,
                    "entries": {GATES_KEY: {"settings": {"profile_roles": roles, "expected_ssh_host": hosts}}}},
    }


def test_ac_fleet_f62_d_7(inst_env):
    """(E2E-2) A managed fragment whose fleet keys differ from the render (stale roles or stale expected ssh host) is planned for rewrite, and after apply it carries the rendered values while unrelated keys survive.

    Base red: collect_home_state reports a complete-but-stale fragment as installed (keys merely present)."""
    _home, managed, _user = inst_env
    inst = _installer("fleet_f62d_inst_ac7")
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    roles, hosts = _managed_render_maps(spec)
    stale_cases = {
        "stale-roles": _managed_fragment({"builder": "worker"}, hosts),
        "stale-host": _managed_fragment(roles, {**hosts, "fixer": "openshell-old-fixer"}),
    }
    for label, frag in stale_cases.items():
        (managed / "config.yaml").write_text(yaml.safe_dump(frag), encoding="utf-8")
        state = inst.collect_home_state(spec, str(SPEC))
        assert state["managed_installed"] is False, f"{label}: a stale fragment must not read as installed"
        assert any(getattr(s, "tag", None) == "managed_write" for s in inst.build_plan(state, str(SPEC), SHA)), \
            f"{label}: build_plan must schedule the managed rewrite"
        ctx = inst._apply_ctx(spec, str(SPEC))
        render_managed = Path(ctx["out"]) / "managed"
        render_managed.mkdir(parents=True, exist_ok=True)
        (render_managed / "config.yaml").write_text(yaml.safe_dump(
            {"plugins": {"entries": {GATES_KEY: {"settings": {"profile_roles": roles, "expected_ssh_host": hosts}}}}}),
            encoding="utf-8")
        write = next(s for s in inst.build_plan(state, str(SPEC), SHA) if getattr(s, "tag", None) == "managed_write")
        inst._execute_step(write, ctx)
        merged = yaml.safe_load((managed / "config.yaml").read_text(encoding="utf-8")) or {}
        settings = merged["plugins"]["entries"][GATES_KEY]["settings"]
        assert settings["profile_roles"] == roles and settings["expected_ssh_host"] == hosts, f"{label}: rewrite must carry the render"
        assert merged["logging"] == {"level": "debug"}, f"{label}: an unrelated managed key must survive"
    (managed / "config.yaml").write_text(yaml.safe_dump(_managed_fragment(roles, hosts)), encoding="utf-8")
    state = inst.collect_home_state(spec, str(SPEC))
    assert state["managed_installed"] is True, "a fragment equal to the render is installed"
    assert not any(getattr(s, "tag", None) == "managed_write" for s in inst.build_plan(state, str(SPEC), SHA))


class _CP:
    def __init__(self, stdout="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, "", returncode


def _capture_subprocess(monkeypatch, stdout_for=None):
    """Capture every subprocess.run / Popen the installer issues (never executes anything)."""
    calls = []

    def _run(cmd, *args, **kwargs):
        calls.append(("run", list(cmd), kwargs))
        return _CP(stdout=(stdout_for(list(cmd)) if stdout_for else "") or "")

    class _Popen:
        def __init__(self, cmd, *args, **kwargs):
            calls.append(("popen", list(cmd), kwargs))
            self.pid, self.returncode = 4242, None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(subprocess, "run", _run)
    monkeypatch.setattr(subprocess, "Popen", _Popen)
    return calls


def _ssh_block(alias, name):
    return f"Host {alias}\n    User sandbox\n    ProxyCommand openshell ssh-proxy --name {name}\n"


def test_ac_fleet_f62_d_8(inst_env, monkeypatch):
    """(E2E-3) Each worker's ssh-config output is persisted into a managed include file that the gateway user's OpenSSH config includes, one host block per worker alias, idempotently across reruns.

    Base red: provision_sshconfig only prints the openshell output (installer.py:845); nothing is persisted."""
    _home, _managed, user_home = inst_env
    inst = _installer("fleet_f62d_inst_ac8")
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    ssh_dir = user_home / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "config").write_text("Host operator-box\n    User op\n", encoding="utf-8")

    def _stdout(cmd):
        if cmd[:3] == ["openshell", "sandbox", "ssh-config"]:
            return _ssh_block(f"openshell-{cmd[3]}", cmd[3])
        return ""

    _capture_subprocess(monkeypatch, _stdout)
    steps = [s for s in inst.build_plan({}, str(SPEC), SHA) if getattr(s, "tag", None) == "provision_sshconfig"]
    assert len(steps) == len(COWORKERS)
    ctx = inst._apply_ctx(spec, str(SPEC))

    def _apply_all():
        for step in steps:
            inst._execute_step(step, ctx)
        return {p.relative_to(ssh_dir): p.read_bytes() for p in sorted(ssh_dir.rglob("*")) if p.is_file()}

    first = _apply_all()
    main = (ssh_dir / "config").read_text(encoding="utf-8")
    includes = [ln for ln in main.splitlines() if ln.strip().lower().startswith("include ")]
    assert len(includes) == 1, f"~/.ssh/config must carry exactly one Include, got {includes}"
    assert "Host operator-box" in main, "the operator's own ssh config must survive"
    target = includes[0].split(None, 1)[1].strip()
    inc = (ssh_dir / target) if not os.path.isabs(target) else Path(target)
    assert inc.is_file(), f"included file {inc} not written"
    hosts = re.findall(r"(?m)^Host\s+(\S+)", inc.read_text(encoding="utf-8"))
    assert sorted(hosts) == sorted(f"openshell-{PROJECT}-{r}" for r in COWORKERS), f"one block per alias, got {hosts}"
    assert _apply_all() == first, "a rerun must be byte-identical (idempotent)"


def test_ac_fleet_f62_d_9(inst_env, monkeypatch):
    """(E2E-4) An existing worker sandbox whose rendered policy differs from the policy it was created with is deleted and re-created; one whose policy matches is left alone.

    Base red: provision_create returns early whenever a sandbox of that name exists (installer.py:836-841)."""
    _home, _managed, _user = inst_env
    inst = _installer("fleet_f62d_inst_ac9")
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    ctx = inst._apply_ctx(spec, str(SPEC))
    out = Path(ctx["out"])
    creates = [s for s in inst.build_plan({}, str(SPEC), SHA) if getattr(s, "tag", None) == "provision_create"]
    for role in COWORKERS:
        (out / role).mkdir(parents=True, exist_ok=True)
        (out / role / f"policy-{role}.yaml").write_text(f"version: 1\n# {role} v1\n", encoding="utf-8")
    monkeypatch.setattr(inst, "_sandbox_exists", lambda name: False)
    calls = _capture_subprocess(monkeypatch)
    for step in creates:
        inst._execute_step(step, ctx)   # first provisioning records each sandbox's policy digest
    calls.clear()
    monkeypatch.setattr(inst, "_sandbox_exists", lambda name: True)
    changed = "fixer"
    (out / changed / f"policy-{changed}.yaml").write_text("version: 1\n# fixer v2\n", encoding="utf-8")
    for step in creates:
        inst._execute_step(step, ctx)
    ops = [(c[1][2], c[1][c[1].index("--name") + 1] if "--name" in c[1] else c[1][-1])
           for c in calls if c[1][:2] == ["openshell", "sandbox"] and c[1][2] in ("create", "delete")]
    assert ops == [("delete", f"{PROJECT}-{changed}"), ("create", f"{PROJECT}-{changed}")], \
        f"only the drifted sandbox is deleted then re-created, got {ops}"
    # A --policy-root host mirror that differs from the rendered policy must halt before any create/delete.
    mirror = Path(ctx["home"]).parent / "mirror"
    rooted = [s for s in inst.build_plan({}, str(SPEC), SHA, policy_root=str(mirror))
              if getattr(s, "tag", None) == "provision_create" and f"{PROJECT}-{changed}" in s.command]
    assert len(rooted) == 1
    (mirror / "render" / changed).mkdir(parents=True)
    (mirror / "render" / changed / f"policy-{changed}.yaml").write_text("version: 1\n# stale mirror\n", encoding="utf-8")
    (out / changed / f"policy-{changed}.yaml").write_text("version: 1\n# fixer v3\n", encoding="utf-8")
    digests = {p: p.read_bytes() for p in sorted(Path(ctx["home"]).rglob("*.sha256"))}
    assert digests, "first provisioning must have recorded each sandbox's policy digest"
    calls.clear()
    with pytest.raises(Exception):
        inst._execute_step(rooted[0], ctx)
    assert {p: p.read_bytes() for p in sorted(Path(ctx["home"]).rglob("*.sha256"))} == digests, \
        "a refused mirror must leave every recorded policy digest unchanged"
    assert not [c for c in calls if c[1][:2] == ["openshell", "sandbox"] and c[1][2] in ("create", "delete")], \
        "a stale --policy-root mirror must be refused before any sandbox is deleted or created"


# The installer's refused env names, assembled from segments so no credential-shaped literal appears in this file.
_REFUSED = {"_".join(parts): "x" for parts in (("HERMES", "DASHBOARD", "SESSION", "TOK" + "EN"),
                                                ("API", "SERVER", "K" + "EY"),
                                                ("OSH", "BROKER", "TOK" + "EN", "FILE"))}


@pytest.mark.parametrize("supervised", [False, True])
def test_ac_fleet_f62_d_10(inst_env, monkeypatch, supervised):
    """(E2E-5) The installer's final gateway restart returns: the restart child is launched detached in its own session, with stdin closed, output to a log file and a bounded wait, whether or not a service manager supervises the gateway; the refused secret-shaped names stay out of its env.

    Base red: the restart step runs `hermes gateway restart` in the foreground via _run (installer.py:842-844)."""
    _home, _managed, _user = inst_env
    inst = _installer("fleet_f62d_inst_ac10")
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    for name, value in _REFUSED.items():
        monkeypatch.setenv(name, value)
    if not supervised:
        monkeypatch.setenv("PATH", str(_user))   # no systemctl / launchctl reachable
    restart = next(s for s in inst.build_plan({}, str(SPEC), SHA) if getattr(s, "tag", None) == "restart")
    calls = _capture_subprocess(monkeypatch)
    inst._execute_step(restart, inst._apply_ctx(spec, str(SPEC)))
    launches = [c for c in calls if c[1][-2:] == ["gateway", "restart"] or c[1][-3:] == ["gateway", "run", "--replace"]]
    assert len(launches) == 1, f"exactly one gateway restart launch, got {[c[1] for c in launches]}"
    kind, argv, kw = launches[0]
    detached = (kind == "popen" and kw.get("start_new_session") is True) or (kind == "run" and argv[:2] == ["setsid", "-f"])
    assert detached, f"the restart child must run in its own session, got {kind} {argv}"
    assert kw.get("stdin") is subprocess.DEVNULL, "the restart child's stdin must be closed"
    assert kw.get("stdout") not in (None, subprocess.PIPE), "restart output must go to a log file, not the installer's console"
    leaked = sorted(n for n in _REFUSED if n in (kw.get("env") or {}))
    assert kw.get("env") is not None and not leaked, f"refused names leaked into the restart env: {leaked}"
    for c in calls:
        if c[0] == "run":
            assert c[2].get("timeout") is not None or c[1][:2] == ["setsid", "-f"], f"unbounded run: {c[1]}"


# ============================================================================ AC-11 .. AC-16 (worker runtime, gates, flow, docs)
def test_ac_fleet_f62_d_11(rendered):
    """(E2E-7) The worker image user's home dir is inside every rendered worker policy's readable set, so the login shell's startup files are readable under Landlock.

    Base red: every worker policy's read_only is /usr /bin /lib /etc and read_write is /tmp — the home is in neither."""
    out, _lines = rendered
    logical = re.sub(r"\\\s*\n", " ", (OPENSHELL_DIR / "Dockerfile.worker").read_text(encoding="utf-8"))
    m = re.search(r"\buseradd\b[^\n]*?--create-home[^\n]*?\s([a-z_][a-z0-9_-]*)\s*(?:&&|$)", logical, re.M)
    assert m, "Dockerfile.worker must create the sandbox user with a home dir"
    user_home = f"/home/{m.group(1)}"
    for role in COWORKERS:
        rw = _fs(_policy(out, role)).get("read_write") or []
        assert _covered(user_home, rw), f"{role}: {user_home} must be inside read_write {rw}"


def _binaries(policy):
    return {str((b or {}).get("path", "")) for np in (policy.get("network_policies") or {}).values()
            for b in (np or {}).get("binaries") or []}


def test_ac_fleet_f62_d_12(tmp_path, rendered):
    """(E2E-9) The approver gets read-only, credential-free GitHub access: its policy allows only GET to the GitHub API host (no web host), through the base curl binary with no GitHub CLI or git binary, it binds no provider at create, and its config carries no credential reference.

    Base red: the approver policy has no GitHub endpoint."""
    out, lines = rendered
    approver = _policy(out, "approver")
    gh = [ep for ep in _endpoints(approver) if ep.get("host") in GITHUB_HOSTS]
    assert [(ep.get("host"), int(ep.get("port"))) for ep in gh] == APPROVER_GITHUB, \
        f"the approver's only GitHub endpoint must be the API host, got {gh}"
    assert gh[0].get("protocol") == "rest" and gh[0].get("enforcement") == "enforce", f"API endpoint not enforced: {gh[0]}"
    assert gh[0].get("rules") == ANON_READ_RULES, f"approver API rules {gh[0].get('rules')} must be one GET /** rule"
    bins = _binaries(approver)
    assert "/usr/bin/curl" in bins, f"the base curl binary must reach the API host, got {sorted(bins)}"
    assert not any(b.rsplit("/", 1)[-1] in ("gh", "git") for b in bins), f"approver carries no gh/git binary: {sorted(bins)}"
    # Additive input: a spec without `egress.anonymous_reads` renders the approver policy minus that endpoint,
    # with every binary, rule and filesystem lane unchanged.
    spec_copy = tmp_path / "spec-copy"
    shutil.copytree(SPEC.parent, spec_copy)
    data = _spec_data()
    assert "anonymous_reads" in (data.get("egress") or {}), "the approver read must come from egress.anonymous_reads"
    data["egress"].pop("anonymous_reads")
    (spec_copy / SPEC.name).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    proc = _compose(spec_copy / SPEC.name, tmp_path / "noAnon-out", _isolated_home(tmp_path / "noAnon"))
    assert proc.returncode == 0, proc.stderr[-2000:]
    stripped = yaml.safe_load(yaml.safe_dump(approver))
    for np in (stripped.get("network_policies") or {}).values():
        np["endpoints"] = [ep for ep in np.get("endpoints") or [] if ep.get("host") not in GITHUB_HOSTS]
    assert _policy(tmp_path / "noAnon-out", "approver") == stripped, \
        "the anonymous read must add exactly one endpoint and nothing else"
    assert _opt_values(_create_line(lines, "approver"), "--provider") == [], "approver must bind no provider at create"
    cfg = _config(out, "approver")
    assert not ((cfg.get("terminal") or {}).get("credential_files")), "approver carries no credential file"
    dumped = yaml.safe_dump(cfg)
    assert "${env:" not in dumped, "approver config must carry no credential reference"


def _load_gates(tmp_path, monkeypatch):
    home = tmp_path / "ghome" / "worker-a"
    (home / "plugins").mkdir(parents=True)
    src = PLUGINS_SRC / GATES_KEY
    if not src.is_dir():
        pytest.fail(f"plugin source missing: {src}")
    shutil.copytree(src, home / "plugins" / GATES_KEY)
    edges = tmp_path / "fleet" / "edges.db"
    edges.parent.mkdir(parents=True, exist_ok=True)
    settings = {"role": "worker", "profile": "worker-a", "profile_roles": {"worker-a": "worker"},
                "plan_gate": True, "required_stages": ["OUTPUT_REVIEW"], "edges_db_path": str(edges),
                "skills_root": str(home / "skills"), "shared_learnings_path": str(home / "shared-learnings")}
    (home / "config.yaml").write_text(yaml.safe_dump(
        {"plugins": {"enabled": [GATES_KEY], "entries": {GATES_KEY: {"settings": settings}}}}), encoding="utf-8")
    bundled = tmp_path / "empty-bundled"
    bundled.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.delenv("HERMES_ENABLE_PROJECT_PLUGINS", raising=False)
    import model_tools
    model_tools.discover_builtin_tools()
    from hermes_cli.plugins import PluginManager
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[GATES_KEY]
    assert loaded.enabled is True and loaded.error is None and "pre_tool_call" in loaded.hooks_registered
    return manager


def _gate_action(manager, command):
    results = manager.invoke_hook("pre_tool_call", tool_name="terminal", args={"command": command},
                                  session_id="sess-noplan", task_id="t", tool_call_id="tc", turn_id="turn")
    acts = [r.get("action") for r in (results or []) if isinstance(r, dict) and r.get("action")]
    return acts[0] if acts else None


def test_ac_fleet_f62_d_13(tmp_path, monkeypatch):
    """(E2E-10) The plan gate does not count a pure stream merge (stderr into stdout, a redirect to the null device) as a file write, while every real file redirect still counts.

    Base red: predicates._segment_mutates treats any '>' as a write, so `git status 2>&1` is blocked without a plan."""
    manager = _load_gates(tmp_path, monkeypatch)
    for cmd in ("git status 2>&1", "ls -la 2>/dev/null", "pytest -q >/dev/null 2>&1", "make 2>&1 | tail -5",
                "cmd >&2", "cmd &>/dev/null"):
        assert _gate_action(manager, cmd) != "block", f"stream merge counted as a write: {cmd!r}"
    for cmd in ("echo x > notes.txt", "echo x >> notes.txt", "git log 2>err.log", "cmd &> out.log",
                "cmd >&out.log", "cmd 2>&1 > out.log"):
        assert _gate_action(manager, cmd) == "block", f"real file redirect not counted as a write: {cmd!r}"


def test_ac_fleet_f62_d_14(rendered):
    """(E2E-12) Every rendered coworker's instructions forbid targeting protected agent-instruction files in an unattended flow, naming the basenames the release always-ask gate protects.

    Base red: no rendered SOUL.md names the protected basenames."""
    out, _lines = rendered
    for role in COWORKERS:
        soul = (out / role / "SOUL.md").read_text(encoding="utf-8")
        lines = [ln for ln in soul.splitlines() if all(b in ln for b in PROTECTED_BASENAMES)]
        assert len(lines) == 1, f"{role}: SOUL.md must carry exactly one invariant naming all protected basenames"
        assert re.search(r"(?i)unattended", lines[0]), f"{role}: the invariant must scope the ban to unattended flows"


def test_ac_fleet_f62_d_15():
    """(E2E-15) The OpenShell runbook no longer names an in-sandbox watchdog as the start path and documents the OSH-F64.c D1 start path (sandbox exec plus a background process, behind fail-closed route gates).

    Base red: fleet-openshell.md § Live-lane preconditions item 4 says 'Start from the in-sandbox watchdog'."""
    text = RUNBOOK.read_text(encoding="utf-8")
    m = re.search(r"(?ms)^### Live-lane preconditions for a served turn\s*$(.*?)(?=^#{2,3} |\Z)", text)
    assert m, "runbook must keep its live-lane preconditions section"
    section = m.group(1).lower()
    assert "watchdog" not in section, "the live-lane start path must not name an in-sandbox watchdog"
    for term in ("openshell sandbox exec", "background", "fail-closed", "route gate"):
        assert term in section, f"the D1 start path must mention {term!r}"


def _pick_base(script, repo, paths):
    return subprocess.run([sys.executable, str(script), "--repo", str(repo), *paths],
                          capture_output=True, text=True, timeout=30)


def test_ac_fleet_f62_d_16(tmp_path, rendered):
    """(E2E-18) The triager and fixer pick the PR base branch from the repo's path-guard allowlists: a bundled helper returns the one branch whose allowlist owns every changed path, and refuses (no base) when none or several do; the two workflows invoke it and never take the base from the default branch or the issue text.

    Base red: no nv-path-guard-base skill is rendered and neither workflow names a base-selection step."""
    out, _lines = rendered
    for role in ("triager", "fixer"):
        assert (out / role / "skills" / "nv-path-guard-base" / "SKILL.md").is_file(), f"{role}: helper skill not rendered"
    script = out / "fixer" / "skills" / "nv-path-guard-base" / "scripts" / "pick_base.py"
    assert script.is_file(), "the helper script must ship inside the skill"
    repo = tmp_path / "repo"
    guard = repo / ".github" / "nv-path-guard"
    guard.mkdir(parents=True)
    (guard / "nv-main.txt").write_text("# core\nsrc/**\ndocs/**\n", encoding="utf-8")
    (guard / "nv-coworkers.txt").write_text("knowledge_base/**\nCHANGELOG-NV.md\n", encoding="utf-8")
    (guard / "nv-dashboard.txt").write_text("dashboard/**\ndocs/dashboard.md\n", encoding="utf-8")
    # The issue text would name nv-coworkers; the paths belong to nv-main.
    got = _pick_base(script, repo, ["docs/README.md", "src/index.ts"])
    assert got.returncode == 0 and got.stdout.strip() == "nv-main", f"owning branch expected, got {got.stdout!r} rc={got.returncode}"
    none = _pick_base(script, repo, ["docs/README.md", "knowledge_base/x.md"])
    assert none.returncode != 0 and not none.stdout.strip(), "a path set owned by no single branch must refuse"
    many = _pick_base(script, repo, ["docs/dashboard.md"])
    assert many.returncode != 0 and not many.stdout.strip(), "a path set owned by several branches must refuse"
    # Adversarial ownership (the guard's gitwildmatch semantics): negation, `**`, anchoring, directory-only.
    adv = tmp_path / "adv"
    aguard = adv / ".github" / "nv-path-guard"
    aguard.mkdir(parents=True)
    # git's parent rule: `src/**` + `!src/overlay/**` does NOT release src/overlay/x.ts (its parent dir stays
    # matched), so nv-legacy and nv-hermes would both own it; re-including the parent dir first does release it.
    (aguard / "nv-main.txt").write_text("src/*\n!src/overlay/\nsrc/overlay/*\n!src/overlay/**\n/setup/\n",
                                        encoding="utf-8")
    (aguard / "nv-legacy.txt").write_text("lib/**\n!lib/keep/**\n", encoding="utf-8")
    (aguard / "nv-hermes.txt").write_text("src/overlay/**\n**/hermes-*.md\n", encoding="utf-8")
    cases = {
        ("src/core.ts",): "nv-main",
        ("src/overlay/x.ts",): "nv-hermes",           # parent re-included, then descendants negated out of nv-main
        ("lib/keep/a.c",): "nv-legacy",               # parent rule: `!lib/keep/**` cannot release it from `lib/**`
        ("docs/deep/hermes-notes.md",): "nv-hermes",  # leading `**/` matches at any depth
        ("setup/install.sh",): "nv-main",             # anchored directory pattern
        ("nested/setup/install.sh",): None,           # the anchor must not match deeper
    }
    for paths, want in cases.items():
        res = _pick_base(script, adv, list(paths))
        if want is None:
            assert res.returncode != 0 and not res.stdout.strip(), f"{paths}: no owner, must refuse"
        else:
            assert res.returncode == 0 and res.stdout.strip() == want, f"{paths}: expected {want}, got {res.stdout!r}"
    for role, wf in (("triager", "triage-issue"), ("fixer", "implement")):
        body = (out / role / "skills" / wf / "SKILL.md").read_text(encoding="utf-8")
        assert "nv-path-guard-base" in body, f"{role}: the {wf} workflow must invoke the base helper"
        assert re.search(r"(?i)never[^.\n]*(default branch|issue text)", body), \
            f"{role}: the {wf} workflow must forbid taking the base from the default branch or the issue text"
