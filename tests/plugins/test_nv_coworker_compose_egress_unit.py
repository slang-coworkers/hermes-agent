"""Non-AC behavior-contract unit tests for the nv-coworker-compose egress writer (ISO-F14).

These complement the two ``pytest:`` acceptance criteria (test_ac_iso_f14_3/_4). They cover
the fail-closed collision belt (ADR design item 3), late-DEFAULT-only all-or-nothing
atomicity (which the acceptance test's spine-seeded CA-conflict case does NOT exercise —
there the first worker raises), the host.docker.internal -> bridge-IP normalization, and the
non-secret placeholder shape. No network; nothing written under ~/.hermes.
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
    assert loaded.enabled is True and loaded.error is None and loaded.module is not None
    return loaded.module


def _make_spec(tmp_path, *, egress=BASE_EGRESS, spine_config=None, default_config=None, name="spec"):
    spec_dir = tmp_path / name
    (spec_dir / "spines").mkdir(parents=True)
    (spec_dir / "spines" / "base.yaml").write_text(
        yaml.safe_dump({"identity": "BASE", "config": dict(spine_config or {})}), encoding="utf-8"
    )
    spec = {
        "project": "iso-f14",
        "default_profile": "default",
        "orchestrator_profile": "worker",
        "spines": {"base": {"source": "spines/base.yaml"}},
        "types": {"worker": {"extends": ["base"], "identity": "WORKER", "config": {}}},
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


@pytest.mark.parametrize("extra", [
    ["-e", "HTTPS_PROXY=http://evil:1"],
    ["--env", "SSL_CERT_FILE=/x"],
    ["--env=CURL_CA_BUNDLE=/x"],
    ["-e", "HERMES_PROXY_TOKEN_A=x"],
    ["--network", "host"],
    ["--net=host"],
    ["--env-file", "/tmp/secrets.env"],
    ["-v", f"/tmp/rogue.crt:{CA_CONTAINER_PATH}:ro"],
    [f"--volume=/tmp/rogue.crt:{CA_CONTAINER_PATH}:ro"],
])
def test_docker_extra_args_collisions_rejected(tmp_path, monkeypatch, extra):
    module = _load(tmp_path, monkeypatch)
    spec = _make_spec(tmp_path, name="extra", spine_config={"terminal": {"docker_extra_args": extra}})
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(tmp_path / "out"))


def test_docker_extra_args_unrelated_allowed(tmp_path, monkeypatch):
    """A non-controlled -e/-v in docker_extra_args is left alone (no over-blocking)."""
    module = _load(tmp_path, monkeypatch)
    spec = _make_spec(tmp_path, name="extra-ok",
                      spine_config={"terminal": {"docker_extra_args": ["-e", "TERM=xterm", "-v", "/data:/data:ro"]}})
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
