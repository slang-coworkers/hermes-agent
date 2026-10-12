"""Non-AC behavior-contract unit tests for the nv-coworker-compose egress writer (ISO-F14).

These complement the two ``pytest:`` acceptance criteria (test_ac_iso_f14_3/_4): the fail-closed
collision belt, all-or-nothing atomicity across coworker and DEFAULT profiles, the
host.docker.internal -> bridge-IP normalization, and the non-secret placeholder shape. No network;
nothing written under ~/.hermes.
"""

import shutil
from pathlib import Path

import pytest
import yaml

from hermes_cli.plugins import PluginManager

PLUGIN_KEY = "nv-coworker-compose"
PLUGIN_SRC = Path(__file__).resolve().parents[2] / "plugins" / PLUGIN_KEY

CA_CONTAINER_PATH = "/etc/ssl/certs/hermes-egress-ca.crt"
CA_HOST_PATH = "/opt/onecli/ca.crt"

BASE_EGRESS = {
    "proxy_addr": "172.17.0.1:10255",
    "ca_host_path": CA_HOST_PATH,
    "ca_container_path": CA_CONTAINER_PATH,
    "provider_env": ["ANTHROPIC_API_KEY"],
    "sandbox_image": "hermes-sandbox:iso-f14-test",
    "container_memory": 1024,
    "container_cpu": 0.5,
}


def _load(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    plugins_dir = home / "plugins"
    plugins_dir.mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, plugins_dir / PLUGIN_KEY,
                    ignore=shutil.ignore_patterns("__pycache__"))
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
    assert loaded.enabled is True and loaded.error is None and loaded.module is not None
    return loaded.module


def _make_spec(tmp_path, *, egress=BASE_EGRESS, spine_config=None, default_config=None,
               types=None, install_surfaces=None, orchestrator_profile="worker", name="spec"):
    spec_dir = tmp_path / name
    (spec_dir / "spines").mkdir(parents=True)
    base_config = dict(spine_config or {})
    # ISO-F13 requires container_persistent set explicitly to a bool per role; the spine base carries
    # it so every type that extends "base" inherits it.
    terminal = dict(base_config.get("terminal") or {})
    terminal.setdefault("container_persistent", True)
    base_config["terminal"] = terminal
    (spec_dir / "spines" / "base.yaml").write_text(
        yaml.safe_dump({"identity": "BASE", "config": base_config}), encoding="utf-8"
    )
    spec = {
        "project": "iso-f14",
        "default_profile": "default",
        "orchestrator_profile": orchestrator_profile,
        # ISO-F13 mount composition requires an explicit workspace_root (abs, outside the fleet
        # Hermes root); install_surfaces defaults to none.
        "workspace_root": "/data/coworkers",
        "install_surfaces": [] if install_surfaces is None else install_surfaces,
        "spines": {"base": {"source": "spines/base.yaml"}},
        "types": types or {"worker": {"extends": ["base"], "identity": "WORKER", "config": {}}},
    }
    if egress is not None:
        spec["egress"] = egress
    if default_config is not None:
        spec["default_config"] = default_config
    spec_path = spec_dir / "coworker-types.yaml"
    spec_path.write_text(yaml.safe_dump(spec), encoding="utf-8")
    return spec_path


def _config(rendered_dir):
    return yaml.safe_load((Path(rendered_dir) / "config.yaml").read_text(encoding="utf-8"))


def test_late_default_collision_is_atomic(tmp_path, monkeypatch):
    """A divergent CA mount present ONLY on the DEFAULT profile still fails the whole render
    with no partial fleet on disk — the assertion pre-pass runs before any profile is written."""
    module = _load(tmp_path, monkeypatch)
    out = tmp_path / "out"
    spec = _make_spec(
        tmp_path, name="late-default",
        default_config={"terminal": {"docker_volumes": [f"/tmp/rogue.crt:{CA_CONTAINER_PATH}:ro"]}},
    )
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(out))
    assert not list(out.rglob("config.yaml")), "no partial fleet may be written on a late-profile collision"


def test_canonical_ca_mount_on_second_coworker_is_atomic(tmp_path, monkeypatch):
    """A CA mount pre-declared on a SECOND coworker fails the whole render with no partial fleet.
    ISO-F13 owns each coworker's docker_volumes as a closed set (the render appends the CA mount
    itself), so the egress pre-pass projects that composition onto a COPY of every coworker and
    rejects a spec-declared CA mount there — before the first coworker is written, so neither lands."""
    module = _load(tmp_path, monkeypatch)
    out = tmp_path / "out"
    ca_mount = f"{CA_HOST_PATH}:{CA_CONTAINER_PATH}:ro"
    types = {
        "worker-a": {"extends": ["base"], "identity": "A", "config": {}},
        "worker-b": {"extends": ["base"], "identity": "B",
                     "config": {"terminal": {"docker_volumes": [ca_mount]}}},
    }
    spec = _make_spec(tmp_path, name="two-coworkers", types=types, orchestrator_profile="worker-a")
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(out))
    assert not list(out.rglob("config.yaml")), "no coworker may be written when a later coworker declares a CA mount"


def test_generated_install_surface_ca_collision_is_atomic(tmp_path, monkeypatch):
    """An install_surface equal to the CA container path makes ISO-F13 compose a same-path mount at
    that path — a CA-target mount the render did not author. The egress pre-pass projects mount
    composition, so the belt catches that generated mount before any profile is written."""
    module = _load(tmp_path, monkeypatch)
    out = tmp_path / "out"
    spec = _make_spec(tmp_path, name="surface-ca", install_surfaces=[CA_CONTAINER_PATH])
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(out))
    assert not list(out.rglob("config.yaml")), "a composition-generated CA-target mount must fail before any write"


def test_docker_env_controlled_key_rejected(tmp_path, monkeypatch):
    module = _load(tmp_path, monkeypatch)
    spec = _make_spec(tmp_path, name="env-collide",
                      spine_config={"terminal": {"docker_env": {"HTTPS_PROXY": "http://evil:1"}}})
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(tmp_path / "out"))


def test_docker_env_proxy_token_rejected(tmp_path, monkeypatch):
    module = _load(tmp_path, monkeypatch)
    spec = _make_spec(tmp_path, name="tok-collide",
                      spine_config={"terminal": {"docker_env": {"HERMES_PROXY_TOKEN_A": "x"}}})
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(tmp_path / "out"))


def test_docker_env_non_mapping_rejected(tmp_path, monkeypatch):
    module = _load(tmp_path, monkeypatch)
    spec = _make_spec(tmp_path, name="env-nonmap",
                      spine_config={"terminal": {"docker_env": ["not", "a", "map"]}})
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(tmp_path / "out"))


def test_docker_forward_env_controlled_rejected(tmp_path, monkeypatch):
    module = _load(tmp_path, monkeypatch)
    spec = _make_spec(tmp_path, name="fwd-collide",
                      spine_config={"terminal": {"docker_forward_env": ["ANTHROPIC_API_KEY"]}})
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(tmp_path / "out"))


def test_docker_forward_env_proxy_alias_allowed_and_rendered_as_four(tmp_path, monkeypatch):
    """FLEET-F62 C1: an EXACT proxy spelling in docker_forward_env is permitted (the render
    forwards the four proxy NAMES so the live token-bearing value wins over the docker_env
    placeholder at exec), and the rendered forward_env is EXACTLY the four canonical spellings —
    the spec's single entry is canonicalized, not duplicated."""
    module = _load(tmp_path, monkeypatch)
    spec = _make_spec(tmp_path, name="fwd-proxy-ok",
                      spine_config={"terminal": {"docker_forward_env": ["HTTPS_PROXY"]}})
    rendered = module.compose(str(spec), str(tmp_path / "out"))
    for rdir in rendered.values():
        fwd = _config(rdir)["terminal"]["docker_forward_env"]
        assert set(fwd) == {"HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"}, fwd


def test_docker_forward_env_discards_unrelated_names(tmp_path, monkeypatch):
    """The render OWNS docker_forward_env: a spec-declared non-controlled forward name
    (allowed past the guard) is overwritten, so the rendered forward_env is EXACTLY the
    four proxy spellings — never a fifth entry that could ride an unexpected name into
    the sandbox."""
    module = _load(tmp_path, monkeypatch)
    spec = _make_spec(tmp_path, name="fwd-extra",
                      spine_config={"terminal": {"docker_forward_env": ["MY_APP_FLAG"]}})
    rendered = module.compose(str(spec), str(tmp_path / "out"))
    for rdir in rendered.values():
        assert _config(rdir)["terminal"]["docker_forward_env"] == [
            "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"
        ]


def test_docker_forward_env_control_plane_key_rejected(tmp_path, monkeypatch):
    """The OneCLI control-plane key is never forwardable — it authenticates the render to
    OneCLI and must stay host-only (§ Security condition 3). It is a controlled egress name,
    so the four-proxy relax never applies to it and the controlled-name check refuses it."""
    module = _load(tmp_path, monkeypatch)
    spec = _make_spec(tmp_path, name="fwd-onecli",
                      spine_config={"terminal": {"docker_forward_env": ["ONECLI_API_KEY"]}})
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(tmp_path / "out"))


def test_env_passthrough_control_plane_key_rejected(tmp_path, monkeypatch):
    """env_passthrough forwards a NAME (the host value rides into the sandbox at exec), so
    ONECLI_API_KEY there would leak the control-plane credential — the belt must refuse it
    on this path too, not only on docker_forward_env (FLEET-F62 C1 'never forwarded')."""
    module = _load(tmp_path, monkeypatch)
    spec = _make_spec(tmp_path, name="passthrough-onecli",
                      spine_config={"terminal": {"env_passthrough": ["ONECLI_API_KEY"]}})
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(tmp_path / "out"))


def test_docker_env_control_plane_key_rejected(tmp_path, monkeypatch):
    """A spec-set docker_env entry for the control-plane key is refused: the render owns the
    egress env and the control-plane key must never appear in a sandbox's environment."""
    module = _load(tmp_path, monkeypatch)
    spec = _make_spec(tmp_path, name="env-onecli",
                      spine_config={"terminal": {"docker_env": {"ONECLI_API_KEY": "x"}}})
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(tmp_path / "out"))


def test_docker_extra_args_control_plane_key_rejected(tmp_path, monkeypatch):
    """A name-only `-e ONECLI_API_KEY` in docker_extra_args forwards the host value — the
    same leak vector as env_passthrough — so the extra-args belt refuses the control-plane
    key as a controlled env name."""
    module = _load(tmp_path, monkeypatch)
    spec = _make_spec(tmp_path, name="extra-onecli",
                      spine_config={"terminal": {"docker_extra_args": ["-e", "ONECLI_API_KEY"]}})
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(tmp_path / "out"))


def test_docker_forward_env_ca_var_still_rejected(tmp_path, monkeypatch):
    """The relax is EXACTLY the four proxy spellings — a controlled NON-proxy name (a CA-trust
    var) in docker_forward_env still collides with the render-owned egress posture."""
    module = _load(tmp_path, monkeypatch)
    spec = _make_spec(tmp_path, name="fwd-ca",
                      spine_config={"terminal": {"docker_forward_env": ["SSL_CERT_FILE"]}})
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(tmp_path / "out"))


@pytest.mark.parametrize("extra", [
    ["-e", "HTTPS_PROXY=http://evil:1"],
    ["--env", "SSL_CERT_FILE=/x"],
    ["--env=CURL_CA_BUNDLE=/x"],
    ["-e", "HERMES_PROXY_TOKEN_A=x"],
    ["-eHTTPS_PROXY=http://evil:1"],
    ["-e", 7, "HTTPS_PROXY=http://evil:1"],                # non-string entry the backend would discard
    ["--network", "host"],
    ["--net=host"],
    ["--memory", "0"],
    ["--cpus", "4"],
    ["-m=0"],
    ["-m512m"],
    ["--env-file", "/tmp/secrets.env"],
    ["-v", f"/tmp/rogue.crt:{CA_CONTAINER_PATH}:ro"],
    [f"--volume=/tmp/rogue.crt:{CA_CONTAINER_PATH}:ro"],
    [f"-v/tmp/rogue.crt:{CA_CONTAINER_PATH}:ro"],
    ["-v", f"{CA_HOST_PATH}:{CA_CONTAINER_PATH}:ro"],      # the render owns the CA mount, so even a canonical one is refused
    ["--mount", f"type=bind,source=/tmp/x,destination={CA_CONTAINER_PATH}"],
    [f"--mount=type=bind,src=/tmp/x,target={CA_CONTAINER_PATH}"],
])
def test_docker_extra_args_collisions_rejected(tmp_path, monkeypatch, extra):
    module = _load(tmp_path, monkeypatch)
    spec = _make_spec(tmp_path, name="extra", spine_config={"terminal": {"docker_extra_args": extra}})
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(tmp_path / "out"))


@pytest.mark.parametrize("extra", [
    ["-v", CA_CONTAINER_PATH],            # separate, target-only
    [f"--volume={CA_CONTAINER_PATH}"],    # joined, target-only
    [f"-v{CA_CONTAINER_PATH}"],           # attached, target-only
])
def test_default_target_only_ca_volume_rejected(tmp_path, monkeypatch, extra):
    """A colon-less `-v <ca_path>` mounts an anonymous volume AT the CA path, shadowing the OneCLI CA.
    The DEFAULT profile skips ISO-F13's docker_extra_args allowlist, so ISO-F14's belt must catch every
    target-only spelling on that profile, before any config.yaml is written."""
    module = _load(tmp_path, monkeypatch)
    out = tmp_path / "out"
    spec = _make_spec(tmp_path, name="default-targetonly",
                      default_config={"terminal": {"docker_extra_args": extra}})
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(out))
    assert not list(out.rglob("config.yaml")), "a target-only CA volume on DEFAULT must fail before any write"


@pytest.mark.parametrize("extra", [
    ["--privileged"],                                        # isolation escape
    ["--rootfs", "/some/dir"],                               # image-pin escape (bypasses docker_image)
    ["--use-api-socket"],                                    # engine API socket exposure
    ["--tmpfs", CA_CONTAINER_PATH],                          # short-form tmpfs shadowing the CA path
    ["-v", "/run/podman/podman.sock:/var/run/docker.sock"],  # engine-socket mount = second egress route
    ["--device", "/dev/kmsg"],                               # host device passthrough
    ["--cap-add", "SYS_ADMIN"],                              # capability escalation (--cap-drop is allowlisted; add is not)
    ["evil-image:latest"],                                   # positional image override (defeats the pin)
])
def test_default_extra_args_non_allowlisted_rejected(tmp_path, monkeypatch, extra):
    """Under OPTION A the DEFAULT-profile docker_extra_args belt is the sole gate (runtime egress guards
    inert; ISO-F13's allowlist runs only in the coworker loop), so it must default-deny every non-mount-safe
    flag before any config.yaml is written — else a second egress route (engine socket), an isolation escape
    (--privileged/--use-api-socket/--device/--cap-add), or an image-pin escape (--rootfs/positional image)
    slips through the DEFAULT profile."""
    module = _load(tmp_path, monkeypatch)
    out = tmp_path / "out"
    spec = _make_spec(tmp_path, name="default-escape",
                      default_config={"terminal": {"docker_extra_args": extra}})
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(out))
    assert not list(out.rglob("config.yaml")), "a non-allowlisted DEFAULT docker_extra_args flag must fail before any write"


def test_default_allowlisted_extra_arg_allowed(tmp_path, monkeypatch):
    """The default-deny allowlist still passes a vetted non-mount flag on the DEFAULT profile
    (no over-blocking): --read-only is on ISO-F13's allowlist, so the render succeeds."""
    module = _load(tmp_path, monkeypatch)
    spec = _make_spec(tmp_path, name="default-allow",
                      default_config={"terminal": {"docker_extra_args": ["--read-only"]}})
    assert module.compose(str(spec), str(tmp_path / "out"))


def test_default_colon_less_ca_docker_volume_rejected(tmp_path, monkeypatch):
    """A colon-less docker_volumes entry equal to the CA container path is a target-only spec AT the
    CA path (its single field is the container dest); the belt rejects it on the DEFAULT profile."""
    module = _load(tmp_path, monkeypatch)
    out = tmp_path / "out"
    spec = _make_spec(tmp_path, name="default-colonless-ca",
                      default_config={"terminal": {"docker_volumes": [CA_CONTAINER_PATH]}})
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(out))
    assert not list(out.rglob("config.yaml")), "a colon-less CA-path docker_volumes entry must fail before any write"


@pytest.mark.parametrize("name", ["HTTPS_PROXY", " HTTPS_PROXY ", "NODE_EXTRA_CA_CERTS",
                                  "HERMES_PROXY_TOKEN_X", "ONECLI_API_KEY", "not a name", ""])
def test_provider_env_reserved_or_malformed_rejected(tmp_path, monkeypatch, name):
    """A provider name that is a reserved control (or malformed) is refused at validation —
    else the provider loop would clobber the proxy/CA value or emit a forbidden token. The
    OneCLI control-plane key is reserved too, so it can never enter via provider_env."""
    module = _load(tmp_path, monkeypatch)
    egress = dict(BASE_EGRESS, provider_env=[name])
    with pytest.raises(module.CompositionError):
        module.compose(str(_make_spec(tmp_path, name="prov", egress=egress)), str(tmp_path / "out"))


@pytest.mark.parametrize("terminal", [
    {"docker_env": {" HTTPS_PROXY ": "http://evil:1"}},
    {"docker_env": {" HERMES_PROXY_TOKEN_X ": "x"}},
    {"docker_forward_env": [" HTTPS_PROXY "]},
    {"env_passthrough": ["HTTPS_PROXY"]},
    {"env_passthrough": [" ANTHROPIC_API_KEY "]},
])
def test_whitespace_and_passthrough_collisions_rejected(tmp_path, monkeypatch, terminal):
    """Controlled names survive a runtime whitespace-strip, and env_passthrough is another
    forwarding path — both must be caught after strip, not by raw-name match."""
    module = _load(tmp_path, monkeypatch)
    spec = _make_spec(tmp_path, name="ws", spine_config={"terminal": terminal})
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(tmp_path / "out"))


def test_docker_extra_args_unrelated_allowed(tmp_path, monkeypatch):
    """A non-mount, non-egress extra_arg the egress belt does not control passes it (no
    over-blocking). It must also be on ISO-F13's coworker allowlist, since that gate runs on
    the same profile: --shm-size is a vetted non-mount resource flag both permit."""
    module = _load(tmp_path, monkeypatch)
    spec = _make_spec(tmp_path, name="extra-ok",
                      spine_config={"terminal": {"docker_extra_args": ["--shm-size", "64m"]}})
    assert module.compose(str(spec), str(tmp_path / "out"))


def test_host_docker_internal_normalized(tmp_path, monkeypatch):
    """A proxy_addr carrying the docker-desktop alias renders as the literal bridge IP."""
    module = _load(tmp_path, monkeypatch)
    egress = dict(BASE_EGRESS, proxy_addr="host.docker.internal:10255")
    rendered = module.compose(str(_make_spec(tmp_path, name="norm", egress=egress)), str(tmp_path / "out"))
    for rdir in rendered.values():
        env = _config(rdir)["terminal"]["docker_env"]
        assert env["HTTPS_PROXY"] == "http://172.17.0.1:10255"
        assert "host.docker.internal" not in env["HTTPS_PROXY"]


def test_provider_placeholder_is_not_a_credential(tmp_path, monkeypatch):
    module = _load(tmp_path, monkeypatch)
    rendered = module.compose(str(_make_spec(tmp_path, name="ph")), str(tmp_path / "out"))
    for rdir in rendered.values():
        val = _config(rdir)["terminal"]["docker_env"]["ANTHROPIC_API_KEY"]
        assert not val.startswith(("sk-", "sk_"))
        assert not (len(val) >= 40 and val.isalnum())


def test_unrelated_docker_env_key_preserved(tmp_path, monkeypatch):
    """_enforce_egress merges controlled keys into the existing docker_env, leaving an
    unrelated entry (a legitimate LANG) intact."""
    module = _load(tmp_path, monkeypatch)
    spec = _make_spec(tmp_path, name="preserve",
                      spine_config={"terminal": {"docker_env": {"LANG": "C.UTF-8"}}})
    rendered = module.compose(str(spec), str(tmp_path / "out"))
    for rdir in rendered.values():
        env = _config(rdir)["terminal"]["docker_env"]
        assert env["LANG"] == "C.UTF-8"
        assert env["HTTPS_PROXY"] == "http://172.17.0.1:10255"


def test_absent_egress_block_no_enforcement(tmp_path, monkeypatch):
    """A spec without an egress block renders unchanged (backward compatible): no
    proxy.enabled, no egress docker_env, and no proxy.enabled in the managed fragment."""
    module = _load(tmp_path, monkeypatch)
    out = tmp_path / "out"
    rendered = module.compose(str(_make_spec(tmp_path, name="noegress", egress=None)), str(out))
    for rdir in rendered.values():
        cfg = _config(rdir)
        assert "enabled" not in (cfg.get("proxy") or {})
        assert "HTTPS_PROXY" not in ((cfg.get("terminal") or {}).get("docker_env") or {})
    assert "proxy" not in _config(out / "managed")
