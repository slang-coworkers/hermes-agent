"""Acceptance test for OSH-F64 — Fleet under OpenShell (P7 demo).

One test_ac_osh_f64_<n> per pytest: criterion (1, 2, 6, 7, 8, 9, 10, 11); AC-3/4
(live) and AC-5 (ui) are proven by their scenario files. Loads nv-coworker-compose from an
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
INSTALL_SECTION = "install into an existing nemoclaw sandbox"


def _plugin_src(name: str) -> str:
    # Canonical GitHub owner/repo/subdir shorthand (plugins_cmd.py:278-284). LANE v2 resolves it
    # offline via a system git url.insteadOf rewrite to its mirror — a deployment git-config concern,
    # not a lane path baked into the spec (port-fidelity: a real NemoClaw deployment has no mirror).
    return f"slang-coworkers/hermes-agent/plugins/{name}"
# The substrate flip may change only terminal.*, the nv-fleet-gates expected_backend/expected_ssh_host deltas, the container-only proxy belt (dropped on openshell), and the openshell-only plugins.scan_on_install:false scan-guard (§D6).
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
    """Managed fragment with the three documented substrate deltas removed.

    Three managed keys differ by substrate and only by substrate:
    - nv-fleet-gates.expected_ssh_host is ADDED only on openshell (the docker->ssh
      backend host map; compose.py:3311-3314).
    - the TOP-LEVEL proxy belt (proxy.enabled: false) is written ONLY on the container
      path (compose.py:3316,3320, gated on `egress_params is not None`); the openshell
      path forces egress_params=None (compose.py:3182) so it CANNOT emit proxy and
      relocates egress control to the per-profile policy-<role>.yaml allowlist (asserted
      above). Dropping proxy here is symmetric to expected_ssh_host, not a loss of the
      egress guarantee.
    - plugins.scan_on_install: false is emitted ONLY on openshell (§D6): sound only
      under the offline sha-pinned first-party install boundary with GitHub egress
      closed. On the container/plain-host path the scan stays ON (installs run from
      github with egress open), so this key is a genuine openshell-only substrate delta,
      dropped here and asserted separately (test_installer_managed_fragment_… + the
      positive check in test_ac_osh_f64_2).
    """
    out = copy.deepcopy(managed)
    entries = (((out.get("plugins") or {}).get("entries") or {}).get(FLEET_GATES_KEY) or {})
    settings = entries.get("settings")
    if isinstance(settings, dict):
        settings.pop("expected_ssh_host", None)
    plugins = out.get("plugins")
    if isinstance(plugins, dict):
        plugins.pop("scan_on_install", None)  # openshell-only scan-guard delta (§D6)
    out.pop("proxy", None)  # top-level: sibling of approvals/security/plugins
    return out


def _non_substrate(cfg: dict, *, strip_chain_dial: bool = False, strip_secret_sets: bool = False, strip_firecrawl_disabled: bool = False) -> dict:
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
    if strip_chain_dial:
        # §D7 chain-dial fields the openshell render adds per served coworker and the container
        # baseline lacks; secrets.onecli.enabled is rendered on BOTH paths, so it is NOT stripped.
        onecli_sec = (out.get("secrets") or {}).get("onecli")
        if isinstance(onecli_sec, dict):
            onecli_sec.pop("override_existing", None)
        onecli_set = (((out.get("plugins") or {}).get("entries") or {}).get("podman-onecli") or {}).get("settings")
        if isinstance(onecli_set, dict):
            onecli_set.pop("proxy_rewrite", None)
            onecli_set.pop("ca_bundle", None)
    if strip_secret_sets:
        # §D7.1: the openshell render populates profile_secret_sets on the default/gateway config
        # from egress.onecli_secret_ids; the FLEET-F62 baseline keeps []. Strip it from both sides
        # of the default comparison so the rest of the config is still compared for equality.
        onecli_set = (((out.get("plugins") or {}).get("entries") or {}).get("podman-onecli") or {}).get("settings")
        if isinstance(onecli_set, dict):
            onecli_set.pop("profile_secret_sets", None)
    if strip_firecrawl_disabled:
        # §D8: the openshell render disables the bundled firecrawl providers via plugins.disabled
        # (web-firecrawl, browser-firecrawl) on default + every served role; the FLEET-F62 baseline
        # disables neither. Strip those two openshell-only entries from both sides so the rest of the
        # config is still compared for equality (their PRESENCE is asserted positively in test_ac_osh_f64_2).
        plugins = out.get("plugins")
        if isinstance(plugins, dict) and isinstance(plugins.get("disabled"), list):
            plugins["disabled"] = [d for d in plugins["disabled"]
                                   if d not in ("web-firecrawl", "browser-firecrawl")]
            if not plugins["disabled"]:
                plugins.pop("disabled", None)
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


def _hostport(entry: str) -> tuple[str, int]:
    host, _sep, port = str(entry).rpartition(":")
    return (host, int(port))


def _expected_openshell_policy(endpoint_values, binary_paths, read_only, raw_hops=()) -> dict:
    # Full-document equality prevents unreviewed widening of network, executable, or filesystem access:
    # any extra/duplicate endpoint, extra binary, extra writable path, or a control-plane hop that a
    # substring scan would miss (v1 stores host/port as separate fields) fails the render comparison.
    # Endpoint shape by the OpenShell egress classification: a RAW passthrough hop (a host:port the
    # sandbox CONNECT-tunnels through when it dials OneCLI as an HTTPS proxy — the OneCLI 18255 and
    # broker 18777 hops) is {host, port, tls: skip} with NO protocol/enforcement/rules; an HTTP-API
    # endpoint (inference-api:443) keeps protocol:rest + enforce + the POST rule. protocol:rest run
    # over the nested CONNECT is denied by the L7 parser, so proxy hops must be raw tls:skip.
    raw = {_hostport(v) for v in raw_hops}
    endpoints = []
    for host, port in (_hostport(v) for v in endpoint_values):
        if (host, port) in raw:
            endpoints.append({"host": host, "port": port, "tls": "skip"})
        else:
            endpoints.append({"host": host, "port": port, "protocol": "rest", "enforcement": "enforce",
                              "rules": [{"allow": {"method": "POST", "path": "/**"}}]})
    return {
        "version": 1,
        "filesystem_policy": {"include_workdir": True, "read_only": list(read_only), "read_write": ["/tmp"]},
        "landlock": {"compatibility": "best_effort"},
        "process": {"run_as_user": "sandbox", "run_as_group": "sandbox"},
        "network_policies": {"worker-egress": {
            "name": "worker-egress",
            "binaries": [{"path": p} for p in binary_paths],
            "endpoints": endpoints,
        }},
    }


def test_ac_osh_f64_1(tmp_path, monkeypatch):
    """AC-OSH-F64-1: Dockerfile.worker lints clean; install-into-sandbox.sh --dry-run is correct, non-mutating, per-profile, idempotent; never `profile install default`."""
    root = _repo_root()
    # Empty plugins/ home: a passing run proves the staged planner needs no pre-installed coworker.
    home, managed = _setup_home(tmp_path, monkeypatch, with_plugin=False)
    # Pre-seed the managed config so the plan must snapshot the original before the §D6 scan-guard flip.
    (managed / "config.yaml").write_text("plugins:\n  scan_on_install: true\n", encoding="utf-8")
    repo_openshell = _openshell_dir(root)

    lint = _import_file(repo_openshell / "dockerfile_lint.py", "osh_f64_lint")
    result = lint.lint(str(repo_openshell / "Dockerfile.worker"))
    assert result.errors == 0, f"Dockerfile.worker has {result.errors} error-level findings"
    dockerfile_text = (repo_openshell / "Dockerfile.worker").read_text(encoding="utf-8")
    # iproute2 must be INSTALLED (in a RUN apt/apt-get install command), not merely mentioned in a comment.
    logical_dockerfile = re.sub(r"\\\s*\n", " ", dockerfile_text)
    install_commands = [ln for ln in logical_dockerfile.splitlines() if ln.lstrip().startswith("RUN ")]
    assert any(re.search(r"\b(?:apt-get|apt)\s+install\b.*\biproute2\b", ln) for ln in install_commands), \
        "Dockerfile.worker must install iproute2 in a RUN apt/apt-get install command (OpenShell 0.0.72 needs it)"
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
    for name in enabled:  # exact bootstrap vector per enabled plugin — canonical GitHub shorthand
        assert ["hermes", "plugins", "install", _plugin_src(name),
                "--ref", sha, "--enable"] in boot, f"missing exact bootstrap vector for {name}"
    compose_steps = [(i, v) for i, v in enumerate(vecs) if v[1:3] == ["coworker", "compose"]]
    assert len(compose_steps) == 1, "install plan must contain exactly one compose step"
    compose_idx, compose = compose_steps[0]
    out_dir = _opt(compose, "--out")
    assert out_dir and "--provision-dry-run" in compose  # renders --out AND yields the plan on stdout
    assert "--substrate" not in joined
    assert all(vecs.index(v) < compose_idx for v in boot), "bootstrap must precede compose"

    # OpenShell provisioning: the installer executes only the SETUP prefix (one sandbox create,
    # sandbox ssh-config per coworker) during install; the teardown suffix (sandbox delete) is
    # rollback-only (AC-6) and must NOT appear in the install plan — else a plan could create then
    # immediately delete every worker sandbox and still pass. No standalone `openshell policy` verb
    # exists: `sandbox create --policy` binds+enforces the per-worker policy inline.
    provision_steps = [(i, v) for i, v in enumerate(vecs) if v and v[0] == "openshell"]
    assert provision_steps, "install plan provisions no OpenShell worker sandboxes"
    setup_verbs = {("sandbox", "create"), ("sandbox", "ssh-config")}
    teardown_verbs = {("sandbox", "delete")}
    assert len(provision_steps) == 2 * len(COWORKERS), \
        f"install must contain exactly {2 * len(COWORKERS)} OpenShell setup commands"
    assert all(tuple(v[1:3]) in setup_verbs for _, v in provision_steps), \
        "install plan contains an unexpected OpenShell command"
    assert not any(tuple(v[1:3]) in teardown_verbs for _, v in provision_steps), \
        "install plan must reserve OpenShell teardown commands for rollback (AC-6)"
    assert not any(v[1] == "policy" for _, v in provision_steps), \
        "no-policy provision plan carries no standalone `openshell policy` verb (create --policy binds it inline)"
    for verb in setup_verbs:
        got = [v for _, v in provision_steps if tuple(v[1:3]) == verb]
        assert len(got) == len(COWORKERS), f"expected {len(COWORKERS)} OpenShell {' '.join(verb)} steps, got {len(got)}"
    for role in COWORKERS:
        sandbox_name = f"osh-f64-{role}"
        create_hits = [(i, v) for i, v in provision_steps
                       if tuple(v[1:3]) == ("sandbox", "create") and _opt(v, "--name") == sandbox_name]
        ssh_hits = [(i, v) for i, v in provision_steps
                    if tuple(v[1:3]) == ("sandbox", "ssh-config") and sandbox_name in v]
        assert len(create_hits) == len(ssh_hits) == 1, f"{role}: exactly one create + one ssh-config on the bare sandbox name"
        create_idx, create = create_hits[0]
        ssh_idx, _ = ssh_hits[0]
        assert _opt(create, "--from") == spec_data["egress"]["sandbox_image"], \
            f"{role}: create must use the spec's pinned sandbox image"
        assert (_opt(create, "--policy") or "").endswith(f"policy-{role}.yaml"), f"{role}: create must bind --policy policy-{role}.yaml inline"
        assert create_idx < ssh_idx, f"{role}: create must precede ssh-config"
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
                    _plugin_src(name), "--ref", sha, "--enable"] in vecs, \
                f"missing per-profile `-p {role} plugins install {name} --enable`"

    assert any("managed" in ln and str(managed) in ln for ln in lines), "no managed-dir write step"
    # §D6: a DANGEROUS verdict ignores --force, so plugins.scan_on_install: false must reach the
    # managed config before the first scanned `plugins install` (the installs precede the durable
    # step-4 managed write, so the guard is written first); the ORIGINAL managed config is snapshotted
    # before that mutation so rollback (AC-6) can restore it.
    scan_guard_idx = next(
        (i for i, ln in enumerate(lines)
         if str(managed) in ln and "scan_on_install" in ln and "false" in ln.lower()),
        None,
    )
    assert scan_guard_idx is not None, \
        "install plan must write plugins.scan_on_install: false to the managed dir (§D6)"
    managed_backup_idx = next(
        (i for i, ln in enumerate(lines) if "backup" in ln.lower() and str(managed) in ln),
        None,
    )
    assert managed_backup_idx is not None and managed_backup_idx < scan_guard_idx, \
        "the ORIGINAL managed config must be backed up BEFORE the scan-guard write (§D3 step 0, rollback integrity)"
    first_plugins_install = min(
        i for i, v in enumerate(vecs)
        if v[1:3] == ["plugins", "install"] or (v[1] == "-p" and v[3:5] == ["plugins", "install"])
    )
    assert scan_guard_idx < first_plugins_install, \
        "the scan-guard managed write must precede the first `plugins install` (§D6 ordering)"
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
    # §D8 route 1 (gateway propagation): the installer's in-place default edit must ALSO union
    # plugins.disabled for the firecrawl providers, so the multiplex-HOST gateway default (edited
    # in place, NOT `profile install`ed) does not load firecrawl and the boot warm-up never reads
    # its key. A render-only change to the `default` distribution does not reach the installed
    # default (which _desired_default diff-applies), so this proves the installer propagates it.
    inst_disabled = (installed_state.get("plugins") or {}).get("disabled") or []
    assert "web-firecrawl" in inst_disabled and "browser-firecrawl" in inst_disabled, \
        "installer default edit must union plugins.disabled=[web-firecrawl,browser-firecrawl] on the gateway default (§D8 route 1)"
    # A fresh config cannot detect clobbering a pre-existing operator disable.
    existing = {
        "gateway": {"multiplex_profiles": False},
        "plugins": {"disabled": ["operator-block", "web-firecrawl"]},
    }
    _, union_diff, _ = planner.default_config_diff(existing, spec)
    union_state = planner.apply_config_diff(existing, union_diff)
    assert union_state["plugins"]["disabled"] == ["operator-block", "web-firecrawl", "browser-firecrawl"], \
        "plugins.disabled union must preserve operator disables, dedup, append missing, order-stable (§D8 route 1)"
    assert planner.default_config_diff(union_state, spec)[1] == {}, \
        "a re-run over the union'd default must be a no-op (idempotent, §D8 route 1)"
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
    src0 = _plugin_src(name0)

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


def test_installer_managed_fragment_disables_scan_on_install(tmp_path, monkeypatch):
    """§D6 (supports AC-1, NO new AC id): the openshell render emits plugins.scan_on_install:
    false into the managed fragment (ALONGSIDE the nested nv-fleet-gates veto settings, never in
    place of them), and the installer's managed_config_diff(existing, rendered) — a PURE deep-merge,
    rendered-wins — carries it durably over pre-seeded operator managed keys. Disabling the install
    scan is what lets the sha-pinned first-party fleet plugins install offline (a DANGEROUS verdict
    ignores --force, plugin_guard.py:332-335); the scan gate reads the flag from the managed-merged
    load_config() (plugins_cmd.py:94-96, config.py:4049-4063), so the managed key is the in-surface
    fix. FAILS on base: the stock render does not emit scan_on_install."""
    root = _repo_root()
    _setup_home(tmp_path, monkeypatch)
    assert yaml is not None

    # Drive the REAL openshell render and read the ACTUAL rendered managed fragment — the fix lives
    # in the render (_build_managed_fragment), so this asserts the product behaviour, not a stub dict.
    manager = _load_manager()
    out_osh = tmp_path / "out_scan"
    _run_coworker(manager, ["compose", str(_osh_spec(root)), "--out", str(out_osh), "--provision-dry-run"])
    rendered = _load_managed(out_osh)
    assert rendered.get("plugins", {}).get("scan_on_install") is False, \
        "openshell render must emit plugins.scan_on_install: false in the managed fragment (§D6)"
    settings = (((rendered.get("plugins") or {}).get("entries") or {}).get(FLEET_GATES_KEY) or {}).get("settings")
    assert isinstance(settings, dict) and settings.get("profile_roles"), \
        "scan_on_install must ride the SAME fragment as the nested nv-fleet-gates veto settings, not replace them"

    # The flag is an OPENSHELL-ONLY substrate delta (§D6): the plain-host/container render must
    # LEAVE scanning ON, since those installs run from github with egress open. An UNCONDITIONAL
    # emission in _build_managed_fragment would disable scanning outside the offline sha-pinned
    # trust boundary — this negative render is what forbids that.
    plain_out = tmp_path / "out_plain"
    plain_spec = root / "tests" / "e2e-scenarios" / "FLEET-F62" / "spec" / "podman" / "coworker-types.yaml"
    _run_coworker(manager, ["compose", str(plain_spec), "--out", str(plain_out)])
    plain_managed = _load_managed(plain_out)
    assert (plain_managed.get("plugins") or {}).get("scan_on_install", True) is True, \
        "plain-host/container render must keep plugin scanning ENABLED (scan_on_install: false is openshell-only, §D6)"

    # managed_config_diff is the committed (existing, rendered) PURE deep-merge (installer.py:286-295):
    # rendered wins at the leaf so scan_on_install: false overrides a pre-seeded True, while unrelated
    # operator managed keys survive (managed_scope exposes no write helper, so the installer merges).
    planner = _import_file(_openshell_dir(root) / "installer.py", "osh_f64_installer")
    preseed = {
        "telemetry": {"operator_pinned": True},                    # unrelated operator managed policy
        "plugins": {"scan_on_install": True, "keep_me": "yes"},
    }
    merged = planner.managed_config_diff(copy.deepcopy(preseed), rendered)
    assert merged["plugins"]["scan_on_install"] is False, \
        "rendered scan_on_install: false must win over a pre-seeded True (deep-merge, rendered-wins)"
    assert merged["plugins"].get("keep_me") == "yes", \
        "pre-seeded sibling plugins.* keys must survive the deep-merge"
    assert merged.get("telemetry") == {"operator_pinned": True}, \
        "unrelated pre-seeded operator managed keys must survive the deep-merge (merge, not overwrite)"


def test_installer_policy_root_rebases_provision_create_policy(tmp_path, monkeypatch):
    """§D3 --policy-root (supports AC-1, mints no AC id): build_plan rebases each provision-create --policy
    to the host path <root>/render/<role>/policy-<role>.yaml; the default keeps the gateway-internal render
    path."""
    root = _repo_root()
    _setup_home(tmp_path, monkeypatch, with_plugin=False)
    planner = _import_file(_openshell_dir(root) / "installer.py", "osh_f64_installer_policy_root")
    spec = str(_osh_spec(root))
    ref = "a" * 40
    default_render = planner._render_out_dir()   # the exact gateway-internal prefix build_plan uses

    def _creates(steps):
        return [s for s in steps if getattr(s, "tag", None) == "provision_create"]

    def _opt_of(cmd, flag):
        return cmd[cmd.index(flag) + 1]

    def _role(cmd):
        name = _opt_of(cmd, "--name")
        assert name.startswith("osh-f64-"), f"unexpected sandbox name {name!r}"
        return name[len("osh-f64-"):]

    default_creates = _creates(planner.build_plan({}, spec, ref))
    assert default_creates, "build_plan must plan `openshell sandbox create` steps for the fresh fleet"
    for s in default_creates:
        role = _role(s.command)
        assert _opt_of(s.command, "--policy") == f"{default_render}/{role}/policy-{role}.yaml", \
            f"{role}: default --policy must be the gateway-internal render path"

    host_root = "/workspace/extra/hermes-fleet-testbed/osh-f64"
    rooted_creates = _creates(planner.build_plan({}, spec, ref, policy_root=host_root))
    assert len(rooted_creates) == len(default_creates), \
        "--policy-root must not change the number of provision-create steps"
    for s in rooted_creates:
        role = _role(s.command)
        assert _opt_of(s.command, "--policy") == f"{host_root}/render/{role}/policy-{role}.yaml", \
            f"{role}: --policy-root must rebase --policy to <root>/render/<role>/policy-<role>.yaml"


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
    # Bind the spec's egress fields so a proxy/broker swap or a drift FAILS independently of the render.
    assert spec_egress.get("proxy_addr") == "172.17.0.1:18255", "openshell OneCLI hop must be 18255 (0.0.72), not 10255"
    assert spec_egress.get("broker_addr") == "172.17.0.1:18777", "spec must declare the additive broker hop 18777"
    assert spec_egress.get("inference_route") == "inference-api.nvidia.com:443", "spec must retain the pinned inference route"
    assert spec_egress.get("filesystem_read_only") == ["/opt/osh-lane"], "filesystem_read_only must be exactly ['/opt/osh-lane']"
    assert list(spec_egress.get("binaries") or []) == ["/usr/bin/python3*", "/opt/hermes/.venv/bin/python"], \
        "spec must declare the additive interpreter binaries appended to the v1 worker-egress binaries"
    assert spec_egress.get("sandbox_image") == "localhost/hermes-openshell-sandbox:pinned", "worker sandbox_image must be the pinned openshell image"
    # SSH enters through the proxy socket; outbound SSH would broaden the worker policy.
    assert "ssh_control_path" not in spec_egress, "v1 egress must not carry ssh_control_path"

    # Pin the expected endpoints to literals (not derived from the editable spec) so a coordinated
    # spec+render drift to a control-plane hop cannot pass. The two proxy hops (OneCLI 18255 +
    # broker 18777) are raw tls:skip passthroughs; inference-api:443 stays a protocol:rest HTTP API.
    expected_policy = _expected_openshell_policy(
        ["172.17.0.1:18255", "inference-api.nvidia.com:443", "172.17.0.1:18777"],
        ["/usr/bin/curl", "/usr/bin/python3*", "/opt/hermes/.venv/bin/python"],
        ["/usr", "/bin", "/lib", "/etc", "/opt/osh-lane"],
        raw_hops=["172.17.0.1:18255", "172.17.0.1:18777"],
    )
    policy_files = list(out_osh.rglob("policy-*.yaml"))
    assert len(policy_files) == len(COWORKERS), f"expected {len(COWORKERS)} policy files, got {len(policy_files)}"
    policy_paths = {p.name: p for p in policy_files}
    assert set(policy_paths) == {f"policy-{role}.yaml" for role in COWORKERS}
    for name, path in policy_paths.items():
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        assert doc == expected_policy, f"{name}: rendered v1 policy must equal the expected v1 document; got {doc}"

    cmd_lines = [ln.strip() for ln in provision.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert cmd_lines and all(ln.startswith("openshell ") for ln in cmd_lines), "every provision command must be `openshell …`"
    import shlex

    ops = [shlex.split(ln) for ln in cmd_lines]
    assert len(ops) == 3 * len(COWORKERS), \
        f"no-policy provision plan is {3 * len(COWORKERS)} lines (create/ssh-config/delete per coworker), got {len(ops)}"
    assert not any(o[1] == "policy" for o in ops), \
        "no standalone `openshell policy` verb (create --policy binds the per-worker policy inline)"
    verbs = {("sandbox", "create"): [], ("sandbox", "ssh-config"): [], ("sandbox", "delete"): []}
    for o in ops:
        verbs.get(tuple(o[1:3]), []).append(o)
    for verb, got in verbs.items():
        assert len(got) == len(COWORKERS), f"expected {len(COWORKERS)} `{' '.join(verb)}`, got {len(got)}"

    for prof in COWORKERS:
        # The provision plan deliberately splits the two strings: the provision plan's
        # create/ssh-config/delete target is the BARE sandbox name <fleet>-<role>, while the
        # rendered terminal.ssh_host is the ssh-config alias openshell-<bare> that resolves to it.
        # Pin the bare name to the expected fleet literal and assert the render's alias relationship
        # (the managed veto anchor joins them by exactly this rule).
        sandbox_name = f"osh-f64-{prof}"
        ssh_alias = osh_cfgs[prof]["terminal"]["ssh_host"]
        assert ssh_alias == f"openshell-{sandbox_name}", f"{prof}: ssh_host must be the openshell-<bare> alias, got {ssh_alias!r}"
        policy_file = f"policy-{prof}.yaml"
        create = next(o for o in verbs[("sandbox", "create")] if _opt(o, "--name") == sandbox_name)
        # create --policy binds the per-worker policy inline (the no-policy plan has no separate
        # `policy set`), so this join is what keeps "each worker gets its own enforced policy" tested.
        assert _opt(create, "--from") == spec_egress["sandbox_image"] and (_opt(create, "--policy") or "").endswith(policy_file)
        ssh_config = next(o for o in verbs[("sandbox", "ssh-config")] if sandbox_name in o)
        delete = next(o for o in verbs[("sandbox", "delete")] if sandbox_name in o)
        setup = [ops.index(create), ops.index(ssh_config)]
        teardown = [ops.index(delete)]
        assert max(setup) < min(teardown), f"{prof}: all setup must precede all teardown"

    # GLOBAL ordering: every profile's setup completes before ANY profile's teardown begins.
    all_setup = [ops.index(o) for v in (("sandbox", "create"), ("sandbox", "ssh-config")) for o in verbs[v]]
    all_teardown = [ops.index(o) for v in (("sandbox", "delete"),) for o in verbs[v]]
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
        # §D8 route 1: the openshell render disables the bundled firecrawl providers (web-firecrawl,
        # browser-firecrawl) on default + every served role so the multiplex boot warm-up never reads
        # their key; the FLEET-F62 baseline disables neither. Assert PRESENCE positively, then strip
        # these openshell-only entries from both sides of the equality below.
        osh_disabled = ((osh_cfgs[prof].get("plugins") or {}).get("disabled")) or []
        assert "web-firecrawl" in osh_disabled and "browser-firecrawl" in osh_disabled, \
            f"{prof}: openshell render must disable web-firecrawl + browser-firecrawl via plugins.disabled (§D8)"
        baseline_disabled = (baseline[prof].get("plugins") or {}).get("disabled") or []
        assert "web-firecrawl" not in baseline_disabled and "browser-firecrawl" not in baseline_disabled, \
            f"{prof}: FLEET-F62 baseline must NOT disable web-firecrawl or browser-firecrawl (the disable is openshell-only, §D8)"
        assert _non_substrate(osh_cfgs[prof], strip_chain_dial=(prof != "default"), strip_secret_sets=(prof == "default"), strip_firecrawl_disabled=True) == _non_substrate(baseline[prof], strip_secret_sets=(prof == "default"), strip_firecrawl_disabled=True), (
            f"{prof}: non-substrate config drifted from the committed FLEET-F62 render"
        )
    # managed fragment: identical except the three documented substrate deltas. Bound the
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
    # §D6: the openshell substrate carries plugins.scan_on_install: false (the offline sha-pinned
    # install-trust delta), which the plain-host FLEET-F62 baseline does not — a genuine substrate
    # delta, so it is dropped from the equality below and asserted positively here.
    assert osh_managed.get("plugins", {}).get("scan_on_install") is False, \
        "openshell managed must carry plugins.scan_on_install: false (§D6 substrate delta)"
    assert _drop_substrate_managed(osh_managed) == _drop_substrate_managed(baseline_managed), \
        "managed fragment drifted from the committed FLEET-F62 render (beyond the substrate deltas)"


def test_openshell_render_backcompat_without_extension_fields(tmp_path, monkeypatch):
    """A spec without OSH-F64 extension fields preserves the base v1 policy exactly."""
    root = _repo_root()
    _setup_home(tmp_path, monkeypatch)
    manager = _load_manager()
    assert yaml is not None

    spec_data = yaml.safe_load(_osh_spec(root).read_text(encoding="utf-8")) or {}
    egress = spec_data.get("egress") or {}
    egress.pop("broker_addr", None)
    egress.pop("filesystem_read_only", None)
    egress.pop("binaries", None)
    spec_data["egress"] = egress
    stripped = tmp_path / "spec-backcompat" / "coworker-types.yaml"
    # compose reads each spine `source` RELATIVE to the spec file's dir and unconditionally
    # (_safe_join(spec_dir, source) then read_text), so a bare stripped spec with no spines/
    # raises FileNotFoundError before rendering. Materialize the full spec dir (incl. spines/)
    # beside the stripped spec, then overwrite coworker-types.yaml with the stripped egress.
    shutil.copytree(_osh_spec(root).parent, stripped.parent, dirs_exist_ok=True)
    stripped.write_text(yaml.safe_dump(spec_data, sort_keys=False), encoding="utf-8")

    out = tmp_path / "out_backcompat"
    _run_coworker(manager, ["compose", str(stripped), "--out", str(out)])
    policy_files = list(out.rglob("policy-*.yaml"))
    assert len(policy_files) == len(COWORKERS), f"expected {len(COWORKERS)} policy files, got {len(policy_files)}"
    # No-delta spec → OSH-F63 head 4c1ef384's exact shape: BOTH endpoints protocol:rest (no raw
    # hops). OSH-F63's AC-5 dials 18255 as a direct plain-HTTP POST the L7 path serves, so its 18255
    # stays protocol:rest — the tls:skip raw shape is OSH-F64-only (HTTPS-proxy nested-CONNECT use).
    expected_policy = _expected_openshell_policy(
        ["172.17.0.1:18255", "inference-api.nvidia.com:443"],
        ["/usr/bin/curl"],
        ["/usr", "/bin", "/lib", "/etc"],
        raw_hops=(),
    )
    for path in policy_files:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        assert doc == expected_policy, f"{path.name}: back-compat v1 policy must equal the base v1 document; got {doc}"


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
        "post-install onboard command (default context, --gateway-url)": r"hermes\s+-p\s+default\s+onboard\s+coworker[\s\S]{0,80}--gateway-url",
        "onboard creates each bot's canonical Bot Chat": r"canonical\s+bot\s*chat",
        "onboard populates the hermes-bots roster": r"hermes-bots|bots\s+roster",
        "onboard required for message_agent delivery": r"message_agent",
        "in-place backed-up default edit": r"(in[\s-]?place|edit)[\s\S]{0,80}(backup|back up|backed up)",
        "never profile install default": r"(never|do not|not)\s+(run\s+)?`?profile install default`?",
        "rollback restores the default backup": r"(restore|revert)[\s\S]{0,60}(default[\s\S]{0,20})?(config\.yaml|backup)",
        "rollback removes the five coworker profiles": r"(remove|delete)[\s\S]{0,60}(five|5|coworker|profile)[\s\S]{0,20}profile",
        "rollback uninstalls the plugins": r"uninstall[\s\S]{0,40}plugin",
        "rollback restarts the gateway": r"restart[\s\S]{0,40}gateway",
        "rollback deletes sandboxes and policies": r"delete[\s\S]{0,60}sandbox[\s\S]{0,40}(polic|policy)",
        "per-sandbox NemoClaw dashboard/APF observation": r"(dashboard|apf)[\s\S]{0,60}(per[\s-]?sandbox|each\s+sandbox|shows)",
        "scan_on_install disabled": r"scan_on_install[\s\S]{0,20}false",
        "sha-pinned first-party trust rationale": r"(first[\s-]?party[\s\S]{0,120}(sha[\s-]?pinned|40[\s-]?char|pinned)|(sha[\s-]?pinned|40[\s-]?char|pinned)[\s\S]{0,120}first[\s-]?party)",
        "offline fork mirror": r"/opt/hermes/fork",
        "github egress closed": r"(github|egress)[\s\S]{0,40}(closed|disabled|off)",
    }
    for label, pat in obligations.items():
        assert re.search(pat, section), f"install/rollback section missing: {label}"


# --- OneCLI chain-dial (§D7) -------------------------------------------------

ONECLI_KEY = "podman-onecli"
_CA_ENV_NAMES = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "HERMES_CA_BUNDLE")
_PROXY_SPELLINGS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")


def _onecli_settings(cfg: dict) -> dict:
    return (((cfg.get("plugins") or {}).get("entries") or {}).get(ONECLI_KEY) or {}).get("settings") or {}


def _load_onecli(tmp_path, monkeypatch, *, settings: dict | None = None):
    """Load podman-onecli via the real discovery path (test_podman_onecli_secret_source.py shape).

    Returns (OneCLISecretSource class, oneclient module, manager). When `settings` is given it is
    written under plugins.entries.podman-onecli.settings BEFORE discovery, so register(ctx) reads
    it via ctx.get_config and constructs the REGISTERED source from it — the wiring the ambient-
    override test exercises. No secrets: block is written, so discovery does not hydrate anything.
    """
    assert yaml is not None
    root = _repo_root()
    src = root / "plugins" / ONECLI_KEY
    if not (src / "plugin.yaml").exists():
        pytest.fail(f"plugins/{ONECLI_KEY} not present")
    home = tmp_path / "onecli_home"
    (home / "plugins").mkdir(parents=True)
    shutil.copytree(src, home / "plugins" / ONECLI_KEY)
    bundled = tmp_path / "onecli_bundled"
    bundled.mkdir()
    cfg: dict = {"plugins": {"enabled": [ONECLI_KEY]}}
    if settings:
        cfg["plugins"]["entries"] = {ONECLI_KEY: {"settings": settings}}
    (home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    from hermes_cli.plugins import PluginManager

    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[ONECLI_KEY]
    assert loaded.enabled is True and loaded.error is None and loaded.module is not None
    src_mod = loaded.module.secret_source
    return src_mod.OneCLISecretSource, src_mod.oneclient, manager


def test_ac_osh_f64_7(tmp_path, monkeypatch):
    """AC-OSH-F64-7: compose renders the OneCLI chain-dial config into each served coworker's raw config, openshell-only."""
    root = _repo_root()
    _setup_home(tmp_path, monkeypatch)
    manager = _load_manager()
    assert yaml is not None

    # chain_addr is the child's loopback dial, not the worker-policy proxy hop (egress.proxy_addr).
    osh_spec = _osh_spec(root)
    spec_dir = tmp_path / "spec_osh"
    shutil.copytree(osh_spec.parent, spec_dir)
    spec_file = spec_dir / osh_spec.name
    spec_doc = yaml.safe_load(spec_file.read_text(encoding="utf-8")) or {}
    spec_doc.setdefault("egress", {})["chain_addr"] = "127.0.0.1:18256"
    spec_doc["egress"]["ca_bundle"] = "/tmp/osh-f64-ca.pem"
    spec_file.write_text(yaml.safe_dump(spec_doc), encoding="utf-8")

    out_osh = tmp_path / "out_openshell"
    _run_coworker(manager, ["compose", str(spec_file), "--out", str(out_osh)])
    cfgs = _read_rendered_configs(out_osh)
    for prof in COWORKERS:
        onecli = ((cfgs[prof].get("secrets") or {}).get("onecli") or {})
        assert onecli.get("enabled") is True, f"{prof}: secrets.onecli.enabled must be true"
        assert onecli.get("override_existing") is True, f"{prof}: secrets.onecli.override_existing must be true"
        settings = _onecli_settings(cfgs[prof])
        assert settings.get("proxy_rewrite") == "127.0.0.1:18256", f"{prof}: proxy_rewrite must read egress.chain_addr"
        assert settings.get("ca_bundle") == "/tmp/osh-f64-ca.pem", f"{prof}: ca_bundle must read egress.ca_bundle"
    # chain_addr feeds the podman-onecli SETTINGS, not the worker-egress POLICY endpoints (v1 stores
    # host/port as separate fields, so compare pairs — a string scan would be vacuous).
    for policy in out_osh.rglob("policy-*.yaml"):
        doc = yaml.safe_load(policy.read_text(encoding="utf-8")) or {}
        eps = doc["network_policies"]["worker-egress"]["endpoints"]
        pairs = {(str(ep["host"]), int(ep["port"])) for ep in eps}
        assert pairs == {("172.17.0.1", 18255), ("inference-api.nvidia.com", 443), ("172.17.0.1", 18777)}, \
            f"{policy.name}: worker-egress endpoints must stay the three policy hops (got {sorted(pairs)})"
    # Hydration reads the RAW per-profile config, NOT the managed overlay (§D7): the keys must live in raw config.
    managed = _load_managed(out_osh)
    assert "onecli" not in (managed.get("secrets") or {}), "managed fragment must NOT carry secrets.onecli"
    msettings = _onecli_settings(managed)
    assert "proxy_rewrite" not in msettings and "ca_bundle" not in msettings, \
        "managed fragment must NOT carry proxy_rewrite/ca_bundle"
    # The container baseline already enables onecli; only the three chain-dial fields are excluded.
    f62_specs = sorted(p for p in (root / "tests" / "e2e-scenarios" / "FLEET-F62").rglob("coworker-types.yaml"))
    assert f62_specs, "FLEET-F62 spec (container substrate) not found (batch5_merged is the dispatch gate)"
    out_f62 = tmp_path / "out_container"
    _run_coworker(manager, ["compose", str(f62_specs[0]), "--out", str(out_f62)])
    for prof, cfg in _read_rendered_configs(out_f62).items():
        onecli = ((cfg.get("secrets") or {}).get("onecli") or {})
        assert "override_existing" not in onecli, f"{prof}: container render must not carry chain-dial override_existing"
        cset = _onecli_settings(cfg)
        assert "proxy_rewrite" not in cset and "ca_bundle" not in cset, \
            f"{prof}: container render must not carry chain-dial proxy_rewrite/ca_bundle (openshell-only)"


def test_ac_osh_f64_8(tmp_path, monkeypatch):
    """AC-OSH-F64-8: proxy_rewrite rewrites the OneCLI proxy host:port to the chain addr, preserving credential userinfo."""
    OneCLISecretSource, oneclient, _ = _load_onecli(tmp_path, monkeypatch)
    monkeypatch.setattr(
        oneclient, "get_container_config",
        lambda *, agent: {"env": {"HTTPS_PROXY": "http://user:aoc_tok@host.docker.internal:10255"}},
    )
    src = OneCLISecretSource(proxy_rewrite="127.0.0.1:18255")
    result = src.fetch({}, str(tmp_path / "onecli_home"))
    for name in _PROXY_SPELLINGS:
        assert result.secrets.get(name) == "http://user:aoc_tok@127.0.0.1:18255", \
            f"{name}: host:port rewritten to the chain, userinfo (user:aoc_tok) intact"


def test_ac_osh_f64_9(tmp_path, monkeypatch):
    """AC-OSH-F64-9: with proxy_rewrite explicitly unset the source copies the OneCLI proxy verbatim (opt-in optional)."""
    OneCLISecretSource, oneclient, _ = _load_onecli(tmp_path, monkeypatch)
    returned = "http://user:aoc_tok@host.docker.internal:10255"
    monkeypatch.setattr(oneclient, "get_container_config", lambda *, agent: {"env": {"HTTPS_PROXY": returned}})
    src = OneCLISecretSource(proxy_rewrite=None)
    result = src.fetch({}, str(tmp_path / "onecli_home"))
    for name in _PROXY_SPELLINGS:
        assert result.secrets.get(name) == returned, f"{name}: proxy copied verbatim when proxy_rewrite unset"
    assert not any(n in result.secrets for n in _CA_ENV_NAMES), "no CA names forced when ca_bundle unset"


def test_ac_osh_f64_10(tmp_path, monkeypatch):
    """AC-OSH-F64-10: ca_bundle forces the four CA env names; rewrite mode forces only proxy+CA keys, not bootstrap creds."""
    OneCLISecretSource, oneclient, _ = _load_onecli(tmp_path, monkeypatch)
    monkeypatch.setattr(
        oneclient, "get_container_config",
        lambda *, agent: {"env": {
            "HTTPS_PROXY": "http://user:aoc_tok@host.docker.internal:10255",
            "ANTHROPIC_API_KEY": "bootstrap-key", "NO_PROXY": "localhost", "ONECLI_API_KEY": "onecli-secret",
        }},
    )
    src = OneCLISecretSource(proxy_rewrite="127.0.0.1:18255", ca_bundle="/etc/osh-lane/ca-bundle.pem")
    result = src.fetch({}, str(tmp_path / "onecli_home"))
    for name in _CA_ENV_NAMES:
        assert result.secrets.get(name) == "/etc/osh-lane/ca-bundle.pem", f"{name}: forced to the ca_bundle path"
    forced = set(result.secrets)
    assert forced == set(_PROXY_SPELLINGS) | set(_CA_ENV_NAMES), \
        f"rewrite mode forces EXACTLY the proxy + CA keys, got {sorted(forced)}"
    assert "ANTHROPIC_API_KEY" not in forced and "NO_PROXY" not in forced, \
        "bootstrap creds / NO_PROXY are not in the forced (overridable) set — override_existing cannot clobber them"


def test_ac_osh_f64_11(tmp_path, monkeypatch):
    """AC-OSH-F64-11: openshell render populates profile_secret_sets with the inference secret (not []), per served profile."""
    root = _repo_root()
    _setup_home(tmp_path, monkeypatch)
    manager = _load_manager()
    assert yaml is not None

    # 1. The SHIPPED OSH-F64 spec (unmodified) must render each served profile's grant to the inference
    #    secret — catches a spec that ships egress.onecli_secret_ids missing/[] (which would 401 at re-verify).
    osh_spec = _osh_spec(root)
    out_osh = tmp_path / "out_openshell"
    _run_coworker(manager, ["compose", str(osh_spec), "--out", str(out_osh)])
    # The onboard grant map lives on the default/gateway config, where onecli-onboard reads it (§D7.1).
    pss = _onecli_settings(_read_rendered_configs(out_osh)["default"]).get("profile_secret_sets") or {}
    for prof in COWORKERS:
        assert pss.get(prof) == ["Anthropic-Dev"], (
            f"shipped OSH-F64 spec: default profile_secret_sets[{prof}] must grant the inference secret "
            f"['Anthropic-Dev'] under openshell (not []), got {pss.get(prof)!r}"
        )

    # 2. The openshell gate holds: injecting egress.onecli_secret_ids into the non-openshell FLEET-F62 spec
    #    must NOT populate the grant map — catches a renderer that populates regardless of substrate.
    f62_specs = sorted(p for p in (root / "tests" / "e2e-scenarios" / "FLEET-F62").rglob("coworker-types.yaml"))
    assert f62_specs, "FLEET-F62 spec (container substrate) not found"
    f62_dir = tmp_path / "spec_f62"
    shutil.copytree(f62_specs[0].parent, f62_dir)
    f62_file = f62_dir / f62_specs[0].name
    f62_doc = yaml.safe_load(f62_file.read_text(encoding="utf-8")) or {}
    f62_doc.setdefault("egress", {})["onecli_secret_ids"] = ["osh-f64-test-secret"]
    f62_file.write_text(yaml.safe_dump(f62_doc), encoding="utf-8")
    out_f62 = tmp_path / "out_container"
    _run_coworker(manager, ["compose", str(f62_file), "--out", str(out_f62)])
    f62_pss = _onecli_settings(_read_rendered_configs(out_f62)["default"]).get("profile_secret_sets") or {}
    assert {r: f62_pss.get(r) for r in COWORKERS} == {r: [] for r in COWORKERS}, (
        f"container render must IGNORE egress.onecli_secret_ids (openshell gate): every served profile's grant "
        f"must stay [], got {f62_pss!r}"
    )


def test_onecli_registered_source_overrides_ambient(tmp_path, monkeypatch):
    """register(ctx) wires the rendered settings into the REGISTERED source; apply_all with override_existing replaces the inherited ambient proxy, preserving carve-outs."""
    OneCLISecretSource, oneclient, _manager = _load_onecli(
        tmp_path, monkeypatch,
        settings={"proxy_rewrite": "127.0.0.1:18255", "ca_bundle": "/etc/osh-lane/ca-bundle.pem"},
    )
    monkeypatch.setattr(
        oneclient, "get_container_config",
        lambda *, agent: {"env": {
            "HTTPS_PROXY": "http://user:aoc_tok@host.docker.internal:10255",
            "ANTHROPIC_API_KEY": "bootstrap-key", "ONECLI_API_KEY": "onecli-secret",
        }},
    )
    from agent.secret_sources import registry as sr

    ambient = {name: "http://10.200.0.1:3128" for name in _PROXY_SPELLINGS}
    ambient.update({"ANTHROPIC_API_KEY": "bootstrap-key", "NO_PROXY": "localhost", "ONECLI_API_KEY": "bootstrap-onecli"})
    # preserve_existing is a TOP-LEVEL secrets key (registry reads secrets_cfg.get("preserve_existing")).
    sr.apply_all(
        {"preserve_existing": ["ANTHROPIC_API_KEY"], "onecli": {"enabled": True, "override_existing": True}},
        str(tmp_path / "onecli_home"), environ=ambient,
    )
    for name in _PROXY_SPELLINGS:
        assert ambient.get(name) == "http://user:aoc_tok@127.0.0.1:18255", \
            f"{name}: override_existing replaces the inherited denied ambient proxy with the token-bearing chain URL"
    for name in _CA_ENV_NAMES:
        assert ambient.get(name) == "/etc/osh-lane/ca-bundle.pem", f"{name}: forced to the ca_bundle path"
    assert ambient["ANTHROPIC_API_KEY"] == "bootstrap-key", "preserve_existing carve-out honoured"
    assert ambient["NO_PROXY"] == "localhost", "NO_PROXY untouched (source forces only proxy+CA keys)"
    assert ambient["ONECLI_API_KEY"] == "bootstrap-onecli", "protected ONECLI_API_KEY not overwritten by the returned onecli-secret"
