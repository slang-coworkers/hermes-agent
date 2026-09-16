"""Behavior-contract acceptance tests for the nv-coworker-compose egress render (ISO-F14, v2 criteria).

Two pytest: criteria (the v2 set is 5: AC-1/2/5 are sandbox: scenario files, not pytest here):
  - test_ac_iso_f14_3 — render golden: proxy.enabled:false, docker_env proxy+CA vars,
    docker_volumes CA ro-mount, docker_persist_across_processes:false, docker_image,
    container_memory/cpu.
  - test_ac_iso_f14_4 — spawn argv under a fake runtime: run + both probe shapes, and the
    persist-gated reuse-ps contrast (persist:false suppresses it; persist:true emits the
    podman-3.4.4-broken {{.Label "hermes-egress"}} ps).

Discovery harness mirrors tests/hermes_cli/test_plugin_api_compat.py; the argv-capture harness
mirrors tests/tools/test_docker_environment.py. No network; nothing written under ~/.hermes.
"""

import shutil
from pathlib import Path

import pytest
import yaml

from hermes_cli.plugins import PluginManager

PLUGIN_KEY = "nv-coworker-compose"
PLUGIN_SRC = Path(__file__).resolve().parents[2] / "plugins" / PLUGIN_KEY

PROXY_ADDR = "172.17.0.1:10255"
PROXY_URL = "http://172.17.0.1:10255"
NO_PROXY_VALUE = "127.0.0.1,localhost,::1"
CA_CONTAINER_PATH = "/etc/ssl/certs/hermes-egress-ca.crt"
CA_HOST_PATH = "/opt/onecli/ca.crt"
SANDBOX_IMAGE = "hermes-sandbox:iso-f14-test"
CONTAINER_MEMORY = 1024  # MiB
CONTAINER_CPU = 0.5
PROVIDER_ENV = ["ANTHROPIC_API_KEY"]

PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
NO_PROXY_VARS = ("NO_PROXY", "no_proxy")
CA_BUNDLE_VARS = (
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "HERMES_CA_BUNDLE",
    "DENO_CERT",
)

CA_MOUNT = f"{CA_HOST_PATH}:{CA_CONTAINER_PATH}:ro"

DEFAULT_EGRESS = {
    "proxy_addr": PROXY_ADDR,
    "ca_host_path": CA_HOST_PATH,
    "ca_container_path": CA_CONTAINER_PATH,
    "provider_env": list(PROVIDER_ENV),
    "sandbox_image": SANDBOX_IMAGE,
    "container_memory": CONTAINER_MEMORY,
    "container_cpu": CONTAINER_CPU,
}


def _load(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    plugins_dir = home / "plugins"
    plugins_dir.mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, plugins_dir / PLUGIN_KEY)
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": [PLUGIN_KEY]}}), encoding="utf-8"
    )
    empty_bundled = tmp_path / "bundled-plugins"
    empty_bundled.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(empty_bundled))
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled is True
    assert loaded.error is None
    assert loaded.module is not None
    return loaded.module


# ISO-F13 #29 (merged into the base, tip fb11fcab) added _enforce_mount_composition
# (compose.py:657, called at :2396 INSIDE the COWORKER loop only): for coworker roles it OWNS
# terminal.docker_volumes as a closed set and REQUIRES terminal.container_persistent (bool, no
# default — compose.py:684) plus a top-level absolute workspace_root (:706). The DEFAULT path
# (:2414-2428) does NOT go through that gate. _make_spec therefore sets container_persistent on the
# coworker role (via the spine base config) — required — and also on default_config, where it is
# harmless (DEFAULT skips the gate); workspace_root is a required top-level key for the coworker.
WORKSPACE_ROOT = "/data/coworkers"
# ISO-F13's closed set gives a coworker exactly one workspace mount `{ws}:{ws}:rw` where
# `ws = {workspace_root}/{profile_name}/workspace` (merged compose.py:706-708, :790). The coworker
# type in this fixture is "worker" (no shared-learnings clone, install_surfaces=[]), so its full
# docker_volumes after ISO-F14 appends the CA mount must be exactly [WORKER_WORKSPACE_MOUNT, CA_MOUNT].
WORKER_WORKSPACE_MOUNT = f"{WORKSPACE_ROOT}/worker/workspace:{WORKSPACE_ROOT}/worker/workspace:rw"


def _with_persistent(cfg):
    cfg = dict(cfg or {})
    terminal = dict(cfg.get("terminal") or {})
    terminal.setdefault("container_persistent", True)
    cfg["terminal"] = terminal
    return cfg


def _make_spec(tmp_path, *, egress=DEFAULT_EGRESS, type_config=None, spine_config=None,
               default_config=None, extra_volumes=None, name="spec"):
    spec_dir = tmp_path / name
    (spec_dir / "spines").mkdir(parents=True)
    base_config = _with_persistent(spine_config)
    if extra_volumes:
        # Seed spine docker_volumes with a rogue mount to prove ISO-F13 #29's closed-set owner
        # (compose.py:657/:745-750) rejects any spec-declared docker_volumes entry except the
        # shared-learnings clone — so a rogue CA cannot pre-exist at the CA container-path.
        terminal = dict(base_config.get("terminal") or {})
        terminal["docker_volumes"] = list(terminal.get("docker_volumes") or []) + list(extra_volumes)
        base_config["terminal"] = terminal
    (spec_dir / "spines" / "base.yaml").write_text(
        yaml.safe_dump({"identity": "BASE", "config": base_config}), encoding="utf-8"
    )
    spec = {
        "project": "iso-f14",
        "default_profile": "default",
        "orchestrator_profile": "worker",
        "skills_root": "skills",
        "workflows_root": "workflows",
        "overlays_root": "overlays",
        "workspace_root": WORKSPACE_ROOT,   # ISO-F13 #29: required top-level absolute path
        "install_surfaces": [],             # ISO-F13 #29: optional, must be a list if present
        "spines": {"base": {"source": "spines/base.yaml"}},
        "types": {
            "worker": {"extends": ["base"], "identity": "WORKER", "config": type_config or {}},
        },
    }
    if egress is not None:
        spec["egress"] = egress
    # The DEFAULT profile does NOT pass through _enforce_mount_composition (:2414-2428), so it does
    # not require container_persistent; set it on default_config defensively/harmlessly anyway.
    spec["default_config"] = _with_persistent(default_config)
    spec_path = spec_dir / "coworker-types.yaml"
    spec_path.write_text(yaml.safe_dump(spec), encoding="utf-8")
    return spec_path


def _render(module, spec_path, out_dir):
    return module.compose(str(spec_path), str(out_dir))


def _config(rendered_dir):
    return yaml.safe_load((Path(rendered_dir) / "config.yaml").read_text(encoding="utf-8"))


def _dig(mapping, dotted):
    node = mapping
    for key in dotted.split("."):
        if not isinstance(node, dict) or key not in node:
            return (False, None)
        node = node[key]
    return (True, node)


def _looks_like_real_credential(value):
    v = str(value)
    return v.startswith("sk-") or v.startswith("sk_") or len(v) >= 40 and v.isalnum()


def test_ac_iso_f14_3(tmp_path, monkeypatch):
    """Every rendered profile (each coworker type + the DEFAULT multiplexer) carries the OPTION-A render golden: proxy.enabled:false; terminal.docker_env with the four proxy vars = http://<proxy_addr>, NO_PROXY/no_proxy = 127.0.0.1,localhost,::1, the six CA-bundle vars = <ca_container_path>, and each provider name = a non-secret placeholder (no HERMES_PROXY_TOKEN_*); exactly one <ca_host_path>:<ca_container_path>:ro entry in terminal.docker_volumes; terminal.docker_persist_across_processes = false; terminal.docker_image = the fleet sandbox image; terminal.container_memory and terminal.container_cpu pinned; and proxy.enabled:false in the managed fragment out_root/managed/config.yaml."""
    module = _load(tmp_path, monkeypatch)
    rendered = _render(module, _make_spec(tmp_path), tmp_path / "out")
    assert rendered, "compose rendered no profiles"

    for name, rdir in rendered.items():
        cfg = _config(rdir)

        found, enabled = _dig(cfg, "proxy.enabled")
        assert found and enabled is False, f"{name}: proxy.enabled must render False, got {enabled!r}"

        _, env = _dig(cfg, "terminal.docker_env")
        assert isinstance(env, dict), f"{name}: terminal.docker_env missing"
        for var in PROXY_VARS:
            assert env.get(var) == PROXY_URL, f"{name}: {var} == {env.get(var)!r}, want {PROXY_URL!r}"
        for var in NO_PROXY_VARS:
            assert env.get(var) == NO_PROXY_VALUE, f"{name}: {var} == {env.get(var)!r}, want {NO_PROXY_VALUE!r}"
        for var in CA_BUNDLE_VARS:
            assert env.get(var) == CA_CONTAINER_PATH, f"{name}: {var} == {env.get(var)!r}, want the CA path"

        for pname in PROVIDER_ENV:
            assert pname in env, f"{name}: provider placeholder for {pname} missing"
            assert not _looks_like_real_credential(env[pname]), (
                f"{name}: {pname} carries a real-credential-shaped value {env[pname]!r}"
            )
        assert not any(k.startswith("HERMES_PROXY_TOKEN_") for k in env), (
            f"{name}: v2 forbids HERMES_PROXY_TOKEN_* swap tokens, got {sorted(env)!r}"
        )

        _, volumes = _dig(cfg, "terminal.docker_volumes")
        assert isinstance(volumes, list), f"{name}: terminal.docker_volumes missing"
        # Count every mount TARGETING the CA container-path — the canonical one must be the only
        # such mount, so a divergent source at the same path cannot shadow the OneCLI CA.
        ca_target_mounts = [
            str(v) for v in volumes
            if f":{CA_CONTAINER_PATH}:" in str(v) or str(v).endswith(f":{CA_CONTAINER_PATH}")
        ]
        assert ca_target_mounts == [CA_MOUNT], f"{name}: want only the canonical CA :ro mount, got {ca_target_mounts!r}"

        found, persist = _dig(cfg, "terminal.docker_persist_across_processes")
        assert found and persist is False, (
            f"{name}: docker_persist_across_processes must render False, got {persist!r}"
        )

        found, image = _dig(cfg, "terminal.docker_image")
        assert found and image == SANDBOX_IMAGE, f"{name}: docker_image == {image!r}, want {SANDBOX_IMAGE!r}"

        found, mem = _dig(cfg, "terminal.container_memory")
        assert found and mem == CONTAINER_MEMORY, f"{name}: container_memory == {mem!r}, want {CONTAINER_MEMORY!r}"
        found, cpu = _dig(cfg, "terminal.container_cpu")
        assert found and cpu == CONTAINER_CPU, f"{name}: container_cpu == {cpu!r}, want {CONTAINER_CPU!r}"

    # _enforce_egress must APPEND its CA mount to ISO-F13's coworker closed set, NOT replace it:
    # an implementation emitting only [CA_MOUNT] would pass the per-profile CA-target check above yet
    # drop ISO-F13's workspace mount and break cross-row composition. Assert both survive on the
    # coworker profile (the DEFAULT path has no ISO-F13 mount composition, so this is coworker-only).
    worker_dir = next(d for n, d in rendered.items() if n != "default")
    _, worker_vols = _dig(_config(worker_dir), "terminal.docker_volumes")
    assert worker_vols == [WORKER_WORKSPACE_MOUNT, CA_MOUNT], (
        f"coworker docker_volumes must be EXACTLY ISO-F13's workspace mount + ISO-F14's appended CA "
        f"mount — an implementation that replaces the set (drops the workspace mount) or reorders it "
        f"fails here: {worker_vols!r}"
    )

    # ADDENDUM item 2: proxy.enabled:false must ALSO be in the machine-wide managed fragment
    # (out_root/managed/config.yaml), which the managed-scope layer deep-merges managed-wins onto
    # every profile — so the egress topology cannot be overridden per profile.
    managed_cfg = _config(tmp_path / "out" / "managed")
    found, managed_proxy = _dig(managed_cfg, "proxy.enabled")
    assert found and managed_proxy is False, f"managed fragment proxy.enabled must be False, got {managed_proxy!r}"

    # ISO-F14's fail-closed egress belt + CA-mount owner boundary, on EVERY profile. ISO-F13 #29
    # covers COWORKER profiles ONLY: _enforce_mount_composition (compose.py:657, its extra-args
    # allowlist at :704) is called at :2396 INSIDE the coworker loop, while the DEFAULT path
    # (:2414-2428) calls neither it nor _enforce_shared_learnings; and it never touches
    # docker_forward_env / env_passthrough for ANY profile. So ISO-F14 must enforce the egress belt
    # itself for the DEFAULT profile and for those host-re-injection keys, as a PRE-PASS over every
    # profile BEFORE any config.yaml is written (all-or-nothing). Each case → CompositionError AND
    # no partial fleet on disk. (An idempotent "identical CA mount already present → still one" case
    # is intentionally NOT asserted: no spec profile may pre-declare a docker_volumes CA mount — the
    # coworker path is sealed by ISO-F13, the DEFAULT path rejected by ISO-F14's belt.)
    collisions = {
        # rogue CA mount at the OneCLI CA path — must never shadow the OneCLI CA:
        "spine_rogue_ca": dict(extra_volumes=[f"/tmp/untrusted.crt:{CA_CONTAINER_PATH}:ro"]),  # coworker path (ISO-F13 seal)
        "default_rogue_ca": dict(default_config={  # DEFAULT path — ISO-F13 skips it, ISO-F14 must reject
            "terminal": {"docker_volumes": [f"/tmp/untrusted.crt:{CA_CONTAINER_PATH}:ro"]}}),
        # host-value re-injection channels ISO-F13 never touches (ISO-F14's unique belt), both paths:
        "type_forward_env": dict(type_config={"terminal": {"docker_forward_env": ["HTTPS_PROXY"]}}),
        "default_forward_env": dict(default_config={"terminal": {"docker_forward_env": ["HTTPS_PROXY"]}}),
        "default_env_passthrough": dict(default_config={"terminal": {"env_passthrough": ["SSL_CERT_FILE"]}}),
        # docker_env managed-key override — ISO-F13 never touches docker_env for ANY profile;
        # ISO-F14 rejects (a clear fail-closed error) rather than silently force-overwriting:
        "type_docker_env": dict(type_config={"terminal": {"docker_env": {"HTTPS_PROXY": "http://rogue.invalid"}}}),
        "default_docker_env": dict(default_config={"terminal": {"docker_env": {"SSL_CERT_FILE": "/tmp/rogue.pem"}}}),
        # DEFAULT docker_extra_args — ISO-F13's allowlist is coworker-only (its _reject runs inside
        # _enforce_mount_composition at :2396), so ISO-F14 must reject these on the DEFAULT path.
        # --env-file is rejected UNCONDITIONALLY (its contents cannot be validated offline):
        "default_extra_args_e": dict(default_config={"terminal": {"docker_extra_args": ["-e", "HTTPS_PROXY=http://rogue.invalid"]}}),
        "default_extra_args_env_file": dict(default_config={"terminal": {"docker_extra_args": ["--env-file=/tmp/rogue.env"]}}),
        "default_extra_args_v_ca": dict(default_config={"terminal": {"docker_extra_args": ["-v", f"/tmp/untrusted.crt:{CA_CONTAINER_PATH}:ro"]}}),
        "default_extra_args_network": dict(default_config={"terminal": {"docker_extra_args": ["--network=host"]}}),
    }
    for cname, kwargs in collisions.items():
        out_c = tmp_path / f"out-collide-{cname}"
        with pytest.raises(module.CompositionError):
            _render(module, _make_spec(tmp_path, name=f"spec-collide-{cname}", **kwargs), out_c)
        assert not list(out_c.rglob("config.yaml")), (
            f"{cname}: egress collision must be refused before any config.yaml is written (all-or-nothing)"
        )


# ---- AC-4 fake-runtime harness ---------------------------------------------------------------
# Monkeypatch the docker backend's subprocess.run to capture forwarded argv and return canned
# stdout per subcommand, so the real argv-building code runs without a real podman. The verbs the
# backend needs canned output for are: version, info (storage driver), image inspect (entrypoint),
# run (-d spawn + --rm cgroup probe), create (disk-quota probe), ps (reuse probe), inspect.

def _fake_run_factory(calls):
    import subprocess as _sp

    def _fake(cmd, *a, **kw):
        argv = [str(x) for x in cmd]
        calls.append(argv)
        rest = argv[1:]
        joined = " ".join(rest)
        out = ""
        if joined.startswith("version"):
            out = "Docker version 3.4.4"
        elif joined.startswith("info"):
            out = "overlay2"  # make the disk-quota probe fire
        elif joined.startswith("image inspect"):
            out = '["/init"]'
        elif joined.startswith("inspect"):
            out = '[{"HostConfig":{"Memory":1073741824}}]'
        elif joined.startswith("ps"):
            out = ""  # no reusable container
        elif joined.startswith("create"):
            out = "probe-container-id\n"
        elif joined.startswith("run --rm"):  # cgroup-limits probe
            out = ""
        elif joined.startswith("run"):  # main -d spawn
            out = "spawned-container-id-abcdef01\n"
        return _sp.CompletedProcess(argv, 0, stdout=out, stderr="")

    return _fake


def _reset_docker_caches(monkeypatch, docker_mod):
    # Both probe results are cached process-wide (docker.py:747-748); reset them so BOTH probes
    # fire in this test. find_docker is patched so no real binary is required.
    for attr in ("_cgroup_limits_ok", "_storage_opt_ok", "_docker_executable"):
        if hasattr(docker_mod, attr):
            monkeypatch.setattr(docker_mod, attr, None, raising=False)
    monkeypatch.setattr(docker_mod, "find_docker", lambda *a, **k: "/usr/bin/false", raising=False)


def _run_calls(calls):
    return [c for c in calls if len(c) > 1 and c[1] == "run"]


def _has_adjacent(argv, first, second):
    for i in range(len(argv) - 1):
        if argv[i] == first and argv[i + 1] == second:
            return True
    return False


def _token_after(argv, flag):
    for i, tok in enumerate(argv):
        if tok == flag and i + 1 < len(argv):
            return argv[i + 1]
    return None


def _fmt_after(argv):
    return _token_after(argv, "--format")


def test_ac_iso_f14_4(tmp_path, monkeypatch):
    """Driving a _DockerEnvironment spawn from a rendered profile under a fake runtime that captures forwarded argv proves the render's posture reaches podman: the run -d spawn carries --memory <pinned>m, --cpus <pinned>, the image positional, and each egress docker_env key as name-only -e KEY; the cgroup probe (run --rm --cpus 0.5 --memory 64m --pids-limit 32 <image> sleep 0) and the disk-quota probe (create --storage-opt size=1m hello-world) both appear; and with the rendered docker_persist_across_processes:false no reuse ps fires, whereas a persist_across_processes:true control DOES emit the ps bearing the podman-3.4.4-broken {{.Label "hermes-egress"}} format."""
    module = _load(tmp_path, monkeypatch)
    rendered = _render(module, _make_spec(tmp_path), tmp_path / "out4")
    # Use the COWORKER profile: it carries ISO-F13's workspace mount AND ISO-F14's appended CA mount,
    # so the spawn argv must show BOTH as -v (proving the append reached podman, not a replace).
    cfg = _config(next(d for n, d in rendered.items() if n != "default"))
    _, env = _dig(cfg, "terminal.docker_env")
    _, volumes = _dig(cfg, "terminal.docker_volumes")
    _, image = _dig(cfg, "terminal.docker_image")
    _, mem = _dig(cfg, "terminal.container_memory")
    _, cpu = _dig(cfg, "terminal.container_cpu")
    _, persist = _dig(cfg, "terminal.docker_persist_across_processes")
    assert persist is False, "precondition: render must pin persist False"

    egress_keys = list(PROXY_VARS) + list(NO_PROXY_VARS) + list(CA_BUNDLE_VARS) + list(PROVIDER_ENV)

    from tools.environments import docker as docker_mod
    DockerEnvironment = docker_mod.DockerEnvironment

    # --- persist:false spawn (persist:false is the render's pinned egress posture) -------------
    calls = []
    _reset_docker_caches(monkeypatch, docker_mod)
    monkeypatch.setattr(docker_mod.subprocess, "run", _fake_run_factory(calls), raising=False)
    DockerEnvironment(
        image=image,
        cpu=cpu,
        memory=mem,
        disk=100,  # >0 so the disk-quota probe fires; ISO-F14 pins mem/cpu, not disk
        env=dict(env),
        volumes=list(volumes),
        persist_across_processes=False,
        task_id="ac4-persist-false",
    )

    spawns = [c for c in _run_calls(calls) if "--rm" not in c]
    assert spawns, f"no `run -d` spawn captured: {calls!r}"
    spawn = spawns[0]
    assert spawn[1:3] == ["run", "-d"], f"spawn must start `run -d`: {spawn!r}"
    assert spawn[-3:] == [image, "sleep", "infinity"], f"spawn must end `<image> sleep infinity`: {spawn!r}"
    assert _token_after(spawn, "--cpus") == str(cpu), f"spawn --cpus value not adjacent/!= {cpu}: {spawn!r}"
    assert _token_after(spawn, "--memory") == f"{mem}m", f"spawn --memory value not adjacent/!= {mem}m: {spawn!r}"
    assert _has_adjacent(spawn, "-v", CA_MOUNT), f"spawn missing -v {CA_MOUNT}: {spawn!r}"
    assert _has_adjacent(spawn, "-v", WORKER_WORKSPACE_MOUNT), (
        f"spawn missing ISO-F13's workspace -v {WORKER_WORKSPACE_MOUNT} — the append must preserve it: {spawn!r}"
    )
    for key in egress_keys:
        assert _has_adjacent(spawn, "-e", key), f"spawn missing name-only -e {key}: {spawn!r}"
        assert env[key] not in spawn, f"spawn leaked the VALUE of {key} into argv (must be name-only)"

    # The two probes are fixed argvs (docker.py:778-780, :1852) — assert them exactly so an
    # implementation that reorders/omits/pads them cannot pass.
    cgroup_probes = [c for c in _run_calls(calls) if "--rm" in c]
    assert cgroup_probes, f"cgroup-limits probe not fired: {calls!r}"
    assert cgroup_probes[0][1:] == [
        "run", "--rm", "--cpus", "0.5", "--memory", "64m", "--pids-limit", "32", image, "sleep", "0",
    ], f"cgroup probe argv wrong: {cgroup_probes[0]!r}"

    disk_probes = [c for c in calls if len(c) > 1 and c[1] == "create"]
    assert disk_probes, f"disk-quota probe not fired: {calls!r}"
    assert disk_probes[0][1:] == ["create", "--storage-opt", "size=1m", "hello-world"], (
        f"disk-quota probe argv wrong: {disk_probes[0]!r}"
    )

    ps_calls = [c for c in calls if len(c) > 1 and c[1] == "ps"]
    assert not ps_calls, f"persist:false must not fire a reuse ps probe, got {ps_calls!r}"

    # --- persist:true control: proves WHY the render pins persist false -----------------------
    calls2 = []
    _reset_docker_caches(monkeypatch, docker_mod)
    monkeypatch.setattr(docker_mod.subprocess, "run", _fake_run_factory(calls2), raising=False)
    DockerEnvironment(
        image=image,
        cpu=cpu,
        memory=mem,
        disk=100,
        env=dict(env),
        volumes=list(volumes),
        persist_across_processes=True,
        task_id="ac4-persist-true",
    )
    ps_calls2 = [c for c in calls2 if len(c) > 1 and c[1] == "ps"]
    assert ps_calls2, f"persist:true must fire the reuse ps probe, got none: {calls2!r}"
    fmt = _fmt_after(ps_calls2[0]) or ""
    # The egress-off ps uses the label formatter broken on podman 3.4.4's psReporter — the exact
    # gap that makes the reuse probe return None and motivates the render pinning persist false.
    assert '.Label "hermes-egress"' in fmt, f"reuse ps must use the {{.Label \"hermes-egress\"}} formatter: {fmt!r}"
