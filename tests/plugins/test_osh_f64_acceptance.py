"""Acceptance test for OSH-F64 — Fleet under OpenShell (P7 demo).

One test_ac_osh_f64_<n> per pytest: criterion (1, 2, 6); AC-3/4 (live) and AC-5
(ui) are proven by their scenario files. Loads nv-coworker-compose from an
isolated HERMES_HOME via the real discovery path, asserts registration, then
drives the behaviour. Behaviour contract, not a byte snapshot
(AGENTS.md:122-125,804-823). No network (every hermes invocation is
--dry-run / pure planning). Must FAIL on the stock v2026.8.31 tree.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import os
import re
import shutil
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

import pytest

try:
    import yaml
except Exception:  # pragma: no cover - yaml is a Hermes runtime dependency
    yaml = None

pytestmark = pytest.mark.linux_only  # subprocess bash + s6 supervisor detection are POSIX-only

PLUGIN_KEY = "nv-coworker-compose"
SHA40 = re.compile(r"^[0-9a-fA-F]{40}$")
COWORKERS = ("orchestrator", "architect", "builder", "tester", "reviewer")
FORBIDDEN_EGRESS = ("172.17.0.1:10256", "0.0.0.0/0", "::/0")
INSTALL_SECTION = "install into an existing nemoclaw sandbox"
# The substrate flip is allowed to change terminal.* and the veto's backend delta only.
FLEET_GATES_KEY = "nv-fleet-gates"


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "plugins" / PLUGIN_KEY / "plugin.yaml").exists():
            return parent
    pytest.fail(f"could not locate repo root with plugins/{PLUGIN_KEY} from {here}")


def _osh_spec(root: Path) -> Path:
    return root / "tests" / "e2e-scenarios" / "OSH-F64" / "spec" / "coworker-types.yaml"


def _committed_fleet_f62_render(root: Path) -> dict[str, dict]:
    """Parsed COMMITTED FLEET-F62 fixture render, keyed by profile (default + five roles)."""
    assert yaml is not None
    hits = sorted((root / "tests" / "e2e-scenarios" / "FLEET-F62" / "fixtures").rglob("config.yaml"))
    if not hits:
        pytest.fail("committed FLEET-F62 fixture render not found (batch5_merged is the dispatch gate)")
    out: dict[str, dict] = {}
    for cfg in hits:
        name = re.sub(r"^fleet-f62-", "", cfg.parent.name)
        out[name] = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    return out


def _openshell_dir(base: Path) -> Path:
    return base / "plugins" / PLUGIN_KEY / "openshell"


def _setup_home(tmp_path: Path, monkeypatch, *, with_plugin: bool = True) -> tuple[Path, Path]:
    """Isolated HERMES_HOME + a managed dir.

    HERMES_HOME itself IS the default profile (get_profile_dir("default") ->
    _get_default_hermes_home(), profiles.py:385-390), so the existing default
    config the installer backs up and edits is HERMES_HOME/config.yaml; the
    managed fragment is a SEPARATE file at HERMES_MANAGED_DIR/config.yaml
    (managed_scope.py:52-121). with_plugin=False leaves plugins/ empty so the
    staged-planner bootstrap can be exercised without a pre-installed coworker.
    """
    root = _repo_root()
    home = tmp_path / "hermes_home"
    (home / "plugins").mkdir(parents=True)
    if with_plugin:
        shutil.copytree(root / "plugins" / PLUGIN_KEY, home / "plugins" / PLUGIN_KEY)
    bundled = tmp_path / "empty_bundled"
    bundled.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    (home / "config.yaml").write_text(
        "plugins:\n  enabled: [nv-coworker-compose]\ngateway:\n  multiplex_profiles: false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    return home, managed


def _load_manager():
    from hermes_cli.plugins import PluginManager

    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_KEY]  # absent unless the plugin loaded (stock-tree runs fail earlier, at _repo_root)
    assert loaded.enabled is True
    assert loaded.error is None
    assert loaded.module is not None
    return manager


def _coworker_command(manager) -> dict:
    # CLI commands live in manager._cli_commands (Dict[str, dict]; plugins.py:3753); each
    # entry is a dict with setup_fn/handler_fn keys (plugins.py:2153-2162). There is no
    # get_cli_commands() accessor.
    entry = manager._cli_commands.get("coworker")
    if entry is None:
        pytest.fail("nv-coworker-compose did not register the `coworker` CLI command")
    return entry


def _run_coworker(manager, argv: list[str]) -> str:
    import argparse

    entry = _coworker_command(manager)
    parser = argparse.ArgumentParser(prog="coworker")
    entry["setup_fn"](parser)
    ns = parser.parse_args(argv)
    handler = getattr(ns, "func", None) or entry["handler_fn"]
    buf = io.StringIO()
    with redirect_stdout(buf):
        handler(ns)
    return buf.getvalue()


def _run_script_dry_run(script: Path, spec: Path, ref: str, env: dict) -> str:
    proc = subprocess.run(
        ["bash", str(script), str(spec), "--ref", ref, "--dry-run"],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert proc.returncode == 0, f"install-into-sandbox.sh --dry-run failed: {proc.stderr}"
    return proc.stdout


def _import_file(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        pytest.fail(f"cannot import module at {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _tree_fingerprint(root: Path) -> dict[str, str]:
    fp = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            fp[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return fp


def _read_rendered_configs(out_dir: Path) -> dict[str, dict]:
    """Profile config.yaml, keyed by profile — ONLY dirs that hold a distribution.yaml.

    Excludes out/managed/config.yaml, which is the managed fragment, not a
    distribution — so the six-distribution count is not inflated.
    """
    assert yaml is not None
    configs: dict[str, dict] = {}
    for cfgfile in sorted(out_dir.rglob("config.yaml")):
        if not (cfgfile.parent / "distribution.yaml").exists():
            continue
        configs[cfgfile.parent.name] = yaml.safe_load(cfgfile.read_text(encoding="utf-8")) or {}
    assert configs, f"no rendered distribution config.yaml under {out_dir}"
    return configs


def _load_managed(out_dir: Path) -> dict:
    assert yaml is not None
    hits = sorted(out_dir.rglob("managed/config.yaml"))
    assert hits, f"no managed/config.yaml rendered under {out_dir}"
    return yaml.safe_load(hits[0].read_text(encoding="utf-8")) or {}


def _committed_fleet_f62_managed(root: Path) -> dict:
    assert yaml is not None
    hits = sorted((root / "tests" / "e2e-scenarios" / "FLEET-F62" / "fixtures").rglob("managed/config.yaml"))
    if not hits:
        pytest.fail("committed FLEET-F62 managed fixture not found")
    return yaml.safe_load(hits[0].read_text(encoding="utf-8")) or {}


def _drop_substrate_managed(managed: dict) -> dict:
    """Managed fragment with the two documented substrate deltas removed.

    Two managed keys differ by substrate and only by substrate:
    - nv-fleet-gates.expected_ssh_host is ADDED only on openshell (the docker->ssh
      backend host map; compose.py:3311-3314).
    - the TOP-LEVEL proxy belt (proxy.enabled: false) is written ONLY on the container
      path (compose.py:3316,3320, gated on `egress_params is not None`); the openshell
      path forces egress_params=None (compose.py:3182) so it CANNOT emit proxy and
      relocates egress control to the per-profile policy-<role>.yaml allowlist (asserted
      above). Dropping proxy here is symmetric to expected_ssh_host, not a loss of the
      egress guarantee.
    """
    out = copy.deepcopy(managed)
    entries = (((out.get("plugins") or {}).get("entries") or {}).get(FLEET_GATES_KEY) or {})
    settings = entries.get("settings")
    if isinstance(settings, dict):
        settings.pop("expected_ssh_host", None)
    out.pop("proxy", None)  # top-level: sibling of approvals/security/plugins
    return out


def _non_substrate(cfg: dict) -> dict:
    """Config with the substrate surface removed: terminal.*, the veto's expected_backend
    delta, and the container-only top-level proxy belt the openshell substrate skips.

    On the container path _enforce_egress writes top-level proxy.enabled:false
    (compose.py:956), gated on the container path and SKIPPED on openshell (compose.py:3263-3264;
    egress_params=None at :3182) where egress is the per-profile openshell policy asserted
    separately — so proxy is a genuine substrate delta and is dropped. `skills.external_dirs`
    is NOT dropped: it is a host-side discovery path that must survive the substrate flip, and
    it survives on openshell when declared under each coworker type's `config.skills.external_dirs`
    (a substrate-independent deep-merge, compose.py:2335/2342) — NOT in a spine `config:` or
    `default_config`, which would leak it into the skills-free `default` profile
    (compose.py:3151/2789) and break the default comparison — so ordinary equality checks it and a
    fleet that lost it on openshell FAILS.
    """
    out = {k: v for k, v in copy.deepcopy(cfg).items() if k != "terminal"}
    entries = (((out.get("plugins") or {}).get("entries") or {}).get(FLEET_GATES_KEY) or {})
    settings = entries.get("settings")
    if isinstance(settings, dict):
        settings.pop("expected_backend", None)
    out.pop("proxy", None)  # container-only egress belt (compose.py:956); skipped on openshell
    return out


def _endpoint_values(egress: dict) -> set[str]:
    """The network endpoints an egress section admits — host:port / host / URL, not CA paths or image names."""
    vals: set[str] = set()

    def looks_like_endpoint(s: str) -> bool:
        return bool(re.search(r":\d{2,5}$", s) or re.match(r"^[a-z][a-z0-9+.-]*://", s)
                    or re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", s))

    def walk(v):
        if isinstance(v, str):
            if looks_like_endpoint(v):
                vals.add(v)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(egress)
    return vals


def _opt(tokens: list[str], flag: str) -> str | None:
    return tokens[tokens.index(flag) + 1] if flag in tokens and tokens.index(flag) + 1 < len(tokens) else None


def test_ac_osh_f64_1(tmp_path, monkeypatch):
    """AC-OSH-F64-1: Dockerfile.worker lints clean; install-into-sandbox.sh --dry-run is correct, non-mutating, per-profile, idempotent; never `profile install default`."""
    root = _repo_root()
    # Empty plugins/ home: a passing run proves the staged planner needs no pre-installed coworker.
    home, managed = _setup_home(tmp_path, monkeypatch, with_plugin=False)
    repo_openshell = _openshell_dir(root)

    lint = _import_file(repo_openshell / "dockerfile_lint.py", "osh_f64_lint")
    result = lint.lint(str(repo_openshell / "Dockerfile.worker"))
    assert result.errors == 0, f"Dockerfile.worker has {result.errors} error-level findings"
    ref_report = root / "tests" / "e2e-scenarios" / "OSH-F64" / "fixtures" / "dockerfile.worker.lint.txt"
    assert result.report == ref_report.read_text(encoding="utf-8")

    spec = _osh_spec(root)
    # The canonical enabled set is the spine's config.plugins.enabled — the set compose renders
    # from (deep-merges each spine's `config`, compose.py:2335; reads plugins.enabled off the
    # merged config, :610). The FLEET-F62/OSH-F63 pattern declares it in the spine, not at the
    # top level. Deriving the installer's EXPECTED bootstrap set from the spine (not a top-level
    # mirror) makes AC-1 fail if the installer bootstraps a set that has drifted from the render.
    spine = yaml.safe_load((spec.parent / "spines" / "base.yaml").read_text(encoding="utf-8")) or {}
    enabled = list(((spine.get("config") or {}).get("plugins") or {}).get("enabled", []))
    assert enabled, "spine config.plugins.enabled is empty"

    spec_data = yaml.safe_load(spec.read_text(encoding="utf-8")) or {}
    # No top-level plugins.enabled mirror — the canonical source is the spine (read above); a
    # redundant top-level mirror would drift from what compose renders.
    assert "enabled" not in (spec_data.get("plugins") or {}), \
        "OSH-F64 spec must NOT carry a top-level plugins.enabled mirror (canonical source is spine config.plugins.enabled)"

    script = repo_openshell / "install-into-sandbox.sh"
    sha = "a" * 40
    before_home, before_managed = _tree_fingerprint(home), _tree_fingerprint(managed)
    t1 = _run_script_dry_run(script, spec, sha, os.environ.copy())
    t2 = _run_script_dry_run(script, spec, sha, os.environ.copy())
    assert t1 == t2, "dry-run must be byte-identical across runs (determinism)"
    assert _tree_fingerprint(home) == before_home and _tree_fingerprint(managed) == before_managed, \
        "dry-run must not mutate home or managed dir"

    import shlex

    lines = [ln.strip() for ln in t1.splitlines() if ln.strip()]
    joined = "\n".join(lines)
    idx = {ln: i for i, ln in enumerate(lines)}

    vecs = [shlex.split(ln) for ln in lines]
    boot = [v for v in vecs if v[1:3] == ["plugins", "install"] and "-p" not in v]
    assert len(boot) == len(enabled), f"expected {len(enabled)} bootstrap installs, got {len(boot)}"
    for name in enabled:  # exact bootstrap vector per enabled plugin
        assert ["hermes", "plugins", "install", f"slang-coworkers/hermes-agent/plugins/{name}",
                "--ref", sha, "--enable"] in boot, f"missing exact bootstrap vector for {name}"
    compose_steps = [(i, v) for i, v in enumerate(vecs) if v[1:3] == ["coworker", "compose"]]
    assert len(compose_steps) == 1, "install plan must contain exactly one compose step"
    compose_idx, compose = compose_steps[0]
    out_dir = _opt(compose, "--out")
    assert out_dir and "--provision-dry-run" in compose  # renders --out AND yields the plan on stdout
    assert "--substrate" not in joined
    assert all(vecs.index(v) < compose_idx for v in boot), "bootstrap must precede compose"

    # OpenShell provisioning: the installer executes only the SETUP prefix (one sandbox create,
    # policy set, sandbox ssh-config per coworker) during install; the teardown suffix (sandbox
    # delete, policy delete) is rollback-only (AC-6) and must NOT appear in the install plan — else
    # a plan could create then immediately delete every worker sandbox and still pass.
    provision_steps = [(i, v) for i, v in enumerate(vecs) if v and v[0] == "openshell"]
    assert provision_steps, "install plan provisions no OpenShell worker sandboxes"
    setup_verbs = {("sandbox", "create"), ("policy", "set"), ("sandbox", "ssh-config")}
    teardown_verbs = {("sandbox", "delete"), ("policy", "delete")}
    assert not any(tuple(v[1:3]) in teardown_verbs for _, v in provision_steps), \
        "install plan must reserve OpenShell teardown commands for rollback (AC-6)"
    for verb in setup_verbs:
        got = [v for _, v in provision_steps if tuple(v[1:3]) == verb]
        assert len(got) == len(COWORKERS), f"expected {len(COWORKERS)} OpenShell {' '.join(verb)} steps, got {len(got)}"
    assert compose_idx < min(i for i, _ in provision_steps), "provisioning must follow compose"

    prof_installs = [v for v in vecs if v[1:3] == ["profile", "install"]]
    assert len(prof_installs) == len(COWORKERS)
    profile_install_indexes = [i for i, v in enumerate(vecs) if v[1:3] == ["profile", "install"]]
    assert max(i for i, _ in provision_steps) < min(profile_install_indexes), \
        "worker provisioning must complete before coworker profiles are installed"
    assert all(v[3:] != ["default"] and "default" not in v[3:4] for v in prof_installs)
    for role in COWORKERS:  # exact profile-install vector from the rendered <out>/<role> source
        assert ["hermes", "profile", "install", f"{out_dir}/{role}", "--name", role, "-y"] in prof_installs, \
            f"missing exact `profile install {out_dir}/{role} --name {role} -y`"
        for name in enabled:  # every enabled plugin installed UNDER this served profile (distribution excludes plugins)
            assert ["hermes", "-p", role, "plugins", "install",
                    f"slang-coworkers/hermes-agent/plugins/{name}", "--ref", sha, "--enable"] in vecs, \
                f"missing per-profile `-p {role} plugins install {name} --enable`"

    assert any("managed" in ln and str(managed) in ln for ln in lines), "no managed-dir write step"
    # DIAG-1: the ORIGINAL default config must be backed up BEFORE any plugins install/enable rewrites it.
    backup_idx = next(i for i, ln in enumerate(lines) if "backup" in ln.lower() and str(home / "config.yaml") in ln)
    first_mutation = min(vecs.index(v) for v in vecs
                         if v[1:3] in (["plugins", "install"], ["plugins", "enable"])
                         or (v[1] == "-p" and v[3:5] in (["plugins", "install"], ["plugins", "enable"])))
    assert backup_idx < first_mutation, "default-config backup must precede any plugins install/enable (DIAG-1)"
    assert "gateway.multiplex_profiles" in joined and "multiplex_profile_allowlist" in joined
    assert any("gateway restart" in ln for ln in lines), "no `hermes gateway restart` step"
    restart_idx = next(i for i, ln in enumerate(lines) if "gateway restart" in ln)

    # Rooms: the spec `rooms:` mapping id->{name, members} is created via the onboarding
    # groups.create path (nv-coworker-compose/__init__.py:758-774), NOT by compose. Assert each
    # declared room is planned EXACTLY once (naming its members), after compose and before restart.
    spec_rooms = spec_data.get("rooms") or {}
    assert spec_rooms, "spec declares no rooms"
    # OSH-F64 must PRESERVE the complete FLEET-F62 rooms mapping unchanged (composes FLEET-F62
    # unchanged) — a shrunk rooms set would silently under-provision the fleet.
    fleet_spec = yaml.safe_load(
        (root / "tests" / "e2e-scenarios" / "FLEET-F62" / "spec" / "podman" / "coworker-types.yaml")
        .read_text(encoding="utf-8")
    ) or {}
    assert spec_rooms == (fleet_spec.get("rooms") or {}), \
        "OSH-F64 must preserve the complete FLEET-F62 rooms mapping unchanged"
    # groups.create ONLY (a generic 'room' line would let echo/no-op steps pass); exactly one per room.
    room_steps = [(i, ln) for i, ln in enumerate(lines) if "groups.create" in ln]
    assert len(room_steps) == len(spec_rooms), \
        "plan must contain exactly one groups.create step per declared room"
    for rid, rmeta in spec_rooms.items():
        hits = [(i, ln) for i, ln in room_steps if rid in ln]
        assert len(hits) == 1, f"room {rid} must be planned exactly once, got {len(hits)}"
        r_idx, r_ln = hits[0]
        assert vecs.index(compose) < r_idx < restart_idx, f"room {rid} must be planned after compose, before restart"
        assert rmeta.get("name", rid) in r_ln, f"room {rid} step must carry its configured name"
        for m in (rmeta.get("members") or []):
            assert m in r_ln, f"room {rid} step must name member {m}"

    # Wires: the OSH-F64 top-level `wires:` installer input (list of [from, to] pairs) →
    # bidirectional `hermes wire add <from> <to>` (nv-fleet-gates cli.py:12-28). The spec must
    # declare the COMPLETE five-edge ring, orchestrator↔builder ABSENT (AC-4's unwired negative
    # control), and the plan must emit exactly one wire-add per edge, after compose, before restart.
    spec_wires = spec_data.get("wires") or []
    assert spec_wires, "spec declares no `wires` ring (OSH-F64 installer input)"
    assert all(isinstance(pair, list) and len(pair) == 2 for pair in spec_wires), \
        "every wire must contain exactly two endpoints"
    spec_edges = {frozenset(pair) for pair in spec_wires}
    required_edges = {
        frozenset(("orchestrator", "architect")),
        frozenset(("architect", "builder")),
        frozenset(("builder", "tester")),
        frozenset(("tester", "reviewer")),
        frozenset(("reviewer", "orchestrator")),
    }
    assert len(spec_wires) == len(required_edges) and spec_edges == required_edges, \
        "OSH-F64 spec must declare the complete five-edge ring (orch↔arch↔builder↔tester↔reviewer↔orch)"
    assert frozenset(["orchestrator", "builder"]) not in spec_edges, \
        "orchestrator↔builder must NOT be in the wire ring (it is AC-4's unwired negative control)"
    wires = [v for v in vecs if v[1:3] == ["wire", "add"]]
    assert len(wires) == len(required_edges), \
        "plan must contain exactly one wire-add command per ring edge"
    plan_edges = {frozenset(w[3:5]) for w in wires}
    assert plan_edges == spec_edges, (
        f"wire plan {sorted(sorted(e) for e in plan_edges)} != spec ring {sorted(sorted(e) for e in spec_edges)}"
    )
    assert vecs.index(compose) < min(vecs.index(w) for w in wires) and \
        max(vecs.index(w) for w in wires) < restart_idx, "wires must come after compose, before restart"

    planner = _import_file(repo_openshell / "installer.py", "osh_f64_installer")
    fresh = {"gateway": {"multiplex_profiles": False}}
    _, fresh_diff, fresh_backup = planner.default_config_diff(fresh, spec)
    assert fresh_diff and fresh_backup is True
    installed_state = planner.apply_config_diff(fresh, fresh_diff)
    _, inst_diff, inst_backup = planner.default_config_diff(installed_state, spec)
    assert not inst_diff and inst_backup is False
    full_state = {
        "default_config": installed_state, "installed_ref": sha,
        "plugins": {n: {"ref": sha, "enabled": True} for n in enabled},
        "profile_plugins": {role: {n: {"ref": sha, "enabled": True} for n in enabled} for role in COWORKERS},
        "profiles": list(COWORKERS), "managed_installed": True, "rooms": "all", "wires": "all"}

    def _plan_vecs(state) -> list[list[str]]:
        out = []
        for step in planner.build_plan(state, spec, sha):
            c = getattr(step, "command", step)
            out.append(list(c) if isinstance(c, (list, tuple)) else shlex.split(str(c)))
        return out

    name0, role0 = enabled[0], COWORKERS[0]
    src0 = f"slang-coworkers/hermes-agent/plugins/{name0}"

    # fully-installed home: NO mutating step at all (state-aware idempotence).
    mutating = re.compile(r"(plugins install|plugins enable|plugins disable|plugins uninstall|"
                          r"profile install|--force|gateway restart|\bbackup\b|wire add|room add|room create|\b(write|update|edit)\b)")
    assert not [" ".join(v) for v in _plan_vecs(full_state) if mutating.search(" ".join(v))], \
        "fully-installed home must plan no mutating step"

    # same-ref DISABLED (default AND a profile) → exactly `plugins enable <name>`, NO reinstall of that plugin.
    disabled = copy.deepcopy(full_state)
    disabled["plugins"][name0]["enabled"] = False
    disabled["profile_plugins"][role0][name0]["enabled"] = False
    dvecs = _plan_vecs(disabled)
    assert ["hermes", "plugins", "enable", name0] in dvecs, "disabled default plugin must plan `hermes plugins enable <name>`"
    assert ["hermes", "-p", role0, "plugins", "enable", name0] in dvecs, "disabled profile plugin must plan `-p <role> plugins enable <name>`"

    def _is_install_of(v: list[str], name: str) -> bool:
        is_install = v[1:3] == ["plugins", "install"] or (v[1] == "-p" and v[3:5] == ["plugins", "install"])
        return is_install and any(name in tok for tok in v)

    assert not any(_is_install_of(v, name0) for v in dvecs), "a disabled plugin must be enabled, not reinstalled"

    # different-ref (default AND a profile) → a force reinstall at the pinned ref for that plugin.
    stale = copy.deepcopy(full_state)
    stale["plugins"][name0]["ref"] = "b" * 40
    stale["profile_plugins"][role0][name0]["ref"] = "b" * 40
    svecs = _plan_vecs(stale)
    assert any(v[1:3] == ["plugins", "install"] and src0 in v and "--force" in v and "--enable" in v and sha in v
               for v in svecs), "different-ref default plugin must plan `plugins install … --ref <sha> --force --enable`"
    assert any(v[1:3] == ["-p", role0] and "install" in v and src0 in v and "--force" in v and "--enable" in v and sha in v
               for v in svecs), "different-ref profile plugin must plan `-p <role> plugins install … --force --enable`"


def test_ac_osh_f64_2(tmp_path, monkeypatch):
    """AC-OSH-F64-2: the full FLEET-F62 spec composes under substrate: openshell; the flip touches only the substrate surface."""
    root = _repo_root()
    _setup_home(tmp_path, monkeypatch)
    manager = _load_manager()
    assert yaml is not None

    osh_spec = _osh_spec(root)
    out_osh = tmp_path / "out_openshell"
    # OSH-F63's interface: `--provision-dry-run` renders to --out AND prints the
    # deterministic provision plan to stdout (nv-coworker-compose/__init__.py:901-907;
    # test_osh_f63_acceptance.py:546). No provision*.txt is ever written under --out, so
    # the plan is captured here from stdout, not from a file.
    provision = _run_coworker(manager, ["compose", str(osh_spec), "--out", str(out_osh), "--provision-dry-run"])
    osh_cfgs = _read_rendered_configs(out_osh)
    assert set(osh_cfgs) == ({"default"} | set(COWORKERS)), "expected exactly six distributions"

    hosts, keys = set(), set()
    for prof in COWORKERS:
        term = osh_cfgs[prof].get("terminal", {})
        assert term.get("backend") == "ssh"
        assert term.get("ssh_host") and prof in str(term["ssh_host"])
        assert term.get("ssh_user") and term.get("ssh_port")
        assert term.get("ssh_key") and not str(term["ssh_key"]).strip().startswith("---")
        assert not any(k.startswith("docker_") for k in term)
        hosts.add(term["ssh_host"])
        keys.add(term["ssh_key"])
    assert len(hosts) == len(COWORKERS) and len(keys) == len(COWORKERS)

    spec_egress = (yaml.safe_load(osh_spec.read_text(encoding="utf-8")) or {}).get("egress", {})
    endpoints = _endpoint_values(spec_egress)
    assert len(endpoints) == 3, f"egress must declare exactly three endpoints, got {endpoints}"
    policy_files = list(out_osh.rglob("policy-*.yaml"))
    assert len(policy_files) == len(COWORKERS), f"expected {len(COWORKERS)} policy files, got {len(policy_files)}"
    policy_paths = {p.name: p for p in policy_files}
    assert set(policy_paths) == {f"policy-{role}.yaml" for role in COWORKERS}
    for name, path in policy_paths.items():
        text = path.read_text(encoding="utf-8")
        doc = yaml.safe_load(text) or {}
        # EXACT set — a policy admitting the three plus a fourth (e.g. evil.example.com) must FAIL
        assert _endpoint_values(doc) == endpoints, f"{name}: policy endpoint set drift {_endpoint_values(doc)}"
        for bad in FORBIDDEN_EGRESS:
            assert bad not in text

    cmd_lines = [ln.strip() for ln in provision.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert cmd_lines and all(ln.startswith("openshell ") for ln in cmd_lines), "every provision command must be `openshell …`"
    import shlex

    ops = [shlex.split(ln) for ln in cmd_lines]
    verbs = {("sandbox", "create"): [], ("policy", "set"): [], ("sandbox", "ssh-config"): [],
             ("sandbox", "delete"): [], ("policy", "delete"): []}
    for o in ops:
        verbs.get(tuple(o[1:3]), []).append(o)
    for verb, got in verbs.items():
        assert len(got) == len(COWORKERS), f"expected {len(COWORKERS)} `{' '.join(verb)}`, got {len(got)}"

    for prof in COWORKERS:
        sandbox_name = osh_cfgs[prof]["terminal"]["ssh_host"]  # create --name MUST equal the rendered ssh_host
        policy_file = f"policy-{prof}.yaml"
        create = next(o for o in verbs[("sandbox", "create")] if _opt(o, "--name") == sandbox_name)
        assert _opt(create, "--from") and (_opt(create, "--policy") or "").endswith(policy_file)
        policy_set = next(o for o in verbs[("policy", "set")] if any(o_t.endswith(policy_file) for o_t in o))
        ssh_config = next(o for o in verbs[("sandbox", "ssh-config")] if sandbox_name in o)
        delete = next(o for o in verbs[("sandbox", "delete")] if sandbox_name in o)
        policy_delete = next(o for o in verbs[("policy", "delete")] if any(o_t.endswith(policy_file) for o_t in o))
        setup = [ops.index(create), ops.index(policy_set), ops.index(ssh_config)]
        teardown = [ops.index(delete), ops.index(policy_delete)]
        assert max(setup) < min(teardown), f"{prof}: all setup must precede all teardown"

    # GLOBAL ordering: every profile's setup completes before ANY profile's teardown begins.
    all_setup = [ops.index(o) for v in (("sandbox", "create"), ("policy", "set"), ("sandbox", "ssh-config")) for o in verbs[v]]
    all_teardown = [ops.index(o) for v in (("sandbox", "delete"), ("policy", "delete")) for o in verbs[v]]
    assert max(all_setup) < min(all_teardown), "all fleet setup must finish before any teardown"

    baseline = _committed_fleet_f62_render(root)
    for prof in ({"default"} | set(COWORKERS)):
        assert prof in baseline, f"FLEET-F62 baseline missing {prof}"
        # The openshell render must NOT carry the container proxy belt (container-only, skipped on
        # openshell — compose.py:3263-3264 — where egress is the openshell policy). Bound the
        # dropped delta to the exact baseline belt and assert its absence on openshell, so the
        # equality below cannot be satisfied by injecting proxy back in.
        assert baseline[prof].get("proxy") == {"enabled": False}, \
            f"{prof}: FLEET-F62 baseline must carry the container proxy belt proxy:{{enabled: false}}"
        assert "proxy" not in osh_cfgs[prof], \
            f"{prof}: openshell config must NOT carry the container proxy belt (egress is the openshell policy)"
        # skills.external_dirs is NOT dropped: it must survive the substrate flip (declared under
        # each coworker type's config.skills.external_dirs, not the spine/default), so the equality
        # below checks it on both sides — the coworkers carry it, the default carries none.
        assert _non_substrate(osh_cfgs[prof]) == _non_substrate(baseline[prof]), (
            f"{prof}: non-substrate config drifted from the committed FLEET-F62 render"
        )
    # managed fragment: identical except the two documented substrate deltas. Bound the
    # accepted proxy delta — the baseline container belt is exactly {enabled: false}, and
    # the openshell managed must carry NO proxy key (egress_params is None on openshell,
    # compose.py:3182, so proxy.enabled is never written; egress is the per-profile policy
    # asserted above). This keeps the drop from masking a lost egress guarantee.
    baseline_managed = _committed_fleet_f62_managed(root)
    assert baseline_managed.get("proxy") == {"enabled": False}, \
        "FLEET-F62 baseline managed must carry the container proxy belt proxy:{enabled:false}"
    osh_managed = _load_managed(out_osh)
    assert "proxy" not in osh_managed, \
        "openshell managed must NOT carry the container proxy belt — egress is the per-profile openshell policy"
    assert _drop_substrate_managed(osh_managed) == _drop_substrate_managed(baseline_managed), \
        "managed fragment drifted from the committed FLEET-F62 render (beyond the substrate deltas)"


def test_ac_osh_f64_6(tmp_path, monkeypatch):
    """AC-OSH-F64-6: fleet-openshell.md gains the "install into an existing NemoClaw sandbox" section."""
    root = _repo_root()
    doc = root / "website" / "docs" / "user-guide" / "fleet-openshell.md"
    assert doc.exists(), "website/docs/user-guide/fleet-openshell.md must exist"
    lines = doc.read_text(encoding="utf-8").splitlines()

    start = next((i for i, ln in enumerate(lines) if ln.lstrip("#").strip().lower() == INSTALL_SECTION), None)
    assert start is not None, f'runbook missing the "{INSTALL_SECTION}" heading'
    level = len(lines[start]) - len(lines[start].lstrip("#"))
    end = len(lines)
    for j in range(start + 1, len(lines)):
        stripped = lines[j].lstrip("#")
        if lines[j].startswith("#") and (len(lines[j]) - len(stripped)) <= level:
            end = j
            break
    section = "\n".join(lines[start:end]).lower()

    # Each obligation tied to its object, not a bare keyword.
    obligations = {
        "five worker sandboxes": r"(five|5)[\s\S]{0,40}worker\s+sandbox",
        "plugin installation instruction": r"(?:\binstall(?:ed|ing|s)?\b[\s\S]{0,60}\bplugins?\b|\bplugins?\b[\s\S]{0,60}\binstall(?:ed|ing|s)?\b)",
        "per-bot policy/APF": r"(per[\s-]?bot|per[\s-]?profile|each)[\s\S]{0,40}(polic|apf)",
        "desktop forward": r"(desktop|dashboard)[\s\S]{0,60}forward",
        "in-place backed-up default edit": r"(in[\s-]?place|edit)[\s\S]{0,80}(backup|back up|backed up)",
        "never profile install default": r"(never|do not|not)\s+(run\s+)?`?profile install default`?",
        "rollback restores the default backup": r"(restore|revert)[\s\S]{0,60}(default[\s\S]{0,20})?(config\.yaml|backup)",
        "rollback removes the five coworker profiles": r"(remove|delete)[\s\S]{0,60}(five|5|coworker|profile)[\s\S]{0,20}profile",
        "rollback uninstalls the plugins": r"uninstall[\s\S]{0,40}plugin",
        "rollback restarts the gateway": r"restart[\s\S]{0,40}gateway",
        "rollback deletes sandboxes and policies": r"delete[\s\S]{0,60}sandbox[\s\S]{0,40}(polic|policy)",
        "per-sandbox NemoClaw dashboard/APF observation": r"(dashboard|apf)[\s\S]{0,60}(per[\s-]?sandbox|each\s+sandbox|shows)",
    }
    for label, pat in obligations.items():
        assert re.search(pat, section), f"install/rollback section missing: {label}"
