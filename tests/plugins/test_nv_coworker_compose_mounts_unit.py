"""Unit tests for the ISO-F13 mount-composition robustness guards.

These cover fail-closed edges that sit alongside the AC-ISO-F13-* acceptance
criteria (which live in test_nv_coworker_compose_mounts_acceptance.py): a mount
host path that smuggles a ':' (and thus extra host:container:mode fields); a
malformed non-list terminal.docker_volumes that an earlier writer would otherwise
normalize away before the mount-composition check can reject it; a non-canonical
'//'-prefixed path that evades the string '/workspace' guards; and the second mount
channel terminal.docker_extra_args, which is appended verbatim to `docker run` and is
allowlisted to vetted non-mount flags (every mount/host-exposure flag — including
podman's --rootfs, --tmpfs, --use-api-socket — and every short flag is refused). Same loader shape as the acceptance test: the
plugin is loaded through the real discovery path from an isolated HERMES_HOME with
an empty HERMES_BUNDLED_PLUGINS, then driven through module.compose() on inline
specs written under tmp_path.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from hermes_cli.plugins import PluginManager

pytestmark = pytest.mark.linux_only

PLUGIN_KEY = "nv-coworker-compose"
PLUGIN_SRC = Path(__file__).resolve().parents[2] / "plugins" / PLUGIN_KEY


def _write_home(tmp_path, monkeypatch):
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
    return home


@pytest.fixture
def module(tmp_path, monkeypatch):
    _write_home(tmp_path, monkeypatch)
    manager = PluginManager()
    manager.discover_and_load()
    entry = manager._plugins[PLUGIN_KEY]
    assert entry.enabled is True and entry.error is None and entry.module is not None
    return entry.module


def _type(*, container_persistent=False, docker_volumes=None, docker_extra_args=None):
    terminal = {"backend": "docker", "docker_image": "hermes-sandbox:pinned",
                "container_persistent": container_persistent}
    if docker_volumes is not None:
        terminal["docker_volumes"] = docker_volumes
    if docker_extra_args is not None:
        terminal["docker_extra_args"] = docker_extra_args
    return {"identity": "FIX", "config": {"terminal": terminal}}


def _write(spec_dir, spec):
    spec_dir.mkdir(parents=True, exist_ok=True)
    path = spec_dir / "coworker-types.yaml"
    path.write_text(yaml.safe_dump(spec), encoding="utf-8")
    return path


def _base(**over):
    spec = {
        "workspace_root": "/data/coworkers",
        "install_surfaces": [],
        "orchestrator_profile": "fixer",
        "default_profile": "default",
        "types": {"fixer": _type()},
    }
    spec.update(over)
    return spec


def test_install_surface_with_colon_refused(module, tmp_path):
    """A ':' in an install surface would smuggle a :/workspace destination + extra
    mount fields — it must be refused, not string-formatted into a volume spec."""
    spec = _write(tmp_path / "s", _base(install_surfaces=["/data/tools:/workspace"]))
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(tmp_path / "out"))


def test_workspace_root_with_colon_refused(module, tmp_path):
    """A ':' in workspace_root is refused for the same reason."""
    spec = _write(tmp_path / "s", _base(workspace_root="/data/x:/y"))
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(tmp_path / "out"))


def test_scalar_docker_volumes_with_clone_refused(module, tmp_path):
    """A non-list terminal.docker_volumes must be rejected BEFORE _enforce_shared_learnings
    normalizes it to a list — otherwise a malformed/injected scalar is silently dropped
    when a shared-learnings clone is configured."""
    spec = _write(
        tmp_path / "s",
        _base(
            shared_learnings_root="/data/learnings-clone",
            types={"fixer": _type(docker_volumes="/etc:/etc:ro")},
        ),
    )
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(tmp_path / "out"))


def test_scalar_docker_volumes_without_clone_refused(module, tmp_path):
    """The same non-list guard fires with no clone configured."""
    spec = _write(
        tmp_path / "s",
        _base(types={"fixer": _type(docker_volumes="/etc:/etc:ro")}),
    )
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(tmp_path / "out"))


def test_nonlist_install_surfaces_refused(module, tmp_path):
    """A non-list install_surfaces (e.g. a bare string) is refused, not treated as
    an iterable of surface declarations."""
    spec = _write(tmp_path / "s", _base(install_surfaces="/data/tools"))
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(tmp_path / "out"))


def test_install_surface_at_clone_destination_refused(module, tmp_path):
    """An install surface equal to the shared-learnings mount point collides with the
    clone's container destination (both would emit -v ...:/mnt/shared-learnings) and
    must be refused so the closed set never carries a shadowing mount."""
    spec = _write(tmp_path / "s", _base(
        shared_learnings_root="/data/learnings-clone",
        install_surfaces=[module.WIKI_MOUNT],
    ))
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(tmp_path / "out"))


def test_symlink_loop_workspace_root_refused(module, tmp_path):
    """A workspace_root that is a symlink loop fails closed: Path.resolve() raises
    RuntimeError on 3.11/3.12 and the containment check must convert that to a
    CompositionError, not let it escape raw."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.symlink_to(b)
    b.symlink_to(a)
    spec = _write(tmp_path / "s", _base(workspace_root=str(a)))
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(tmp_path / "out"))


def test_double_slash_workspace_root_refused(module, tmp_path):
    """A "//workspace-fleet" workspace_root slips past both the `.startswith("/workspace")`
    and `:/workspace` guards, so _require_abs_mount_path must reject it as non-canonical."""
    spec = _write(tmp_path / "s", _base(workspace_root="//workspace-fleet"))
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(tmp_path / "out"))


def test_double_slash_install_surface_refused(module, tmp_path):
    """A "//workspace" install surface bypasses the same string guards — refuse it up front."""
    spec = _write(tmp_path / "s", _base(install_surfaces=["//workspace"]))
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(tmp_path / "out"))


def test_double_slash_clone_destination_collision_refused(module, tmp_path):
    """A "//mnt/shared-learnings" install surface would evade the clone-destination
    collision check (its string != WIKI_MOUNT) yet resolve onto WIKI_MOUNT; the
    non-canonical guard rejects it before it can shadow the clone."""
    spec = _write(tmp_path / "s", _base(
        shared_learnings_root="/data/learnings-clone",
        install_surfaces=[f"/{module.WIKI_MOUNT}"],
    ))
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(tmp_path / "out"))


@pytest.mark.parametrize("extra", [
    pytest.param(["-v", "/data/coworkers:/stolen:ro"], id="short-v-space"),
    pytest.param(["-v=/data/coworkers:/stolen"], id="short-v-equals"),
    pytest.param(["-v/data/coworkers:/stolen"], id="short-v-glued"),
    pytest.param(["-itv/data/coworkers:/stolen"], id="short-v-bundled-glued"),
    pytest.param(["-iv", "/data/coworkers:/stolen"], id="short-v-bundled-space"),
    pytest.param(["--volume", "/data/coworkers:/stolen"], id="volume-space"),
    pytest.param(["--volume=/data/coworkers:/stolen"], id="volume-equals"),
    pytest.param(["--mount", "type=bind,source=/data/coworkers,target=/stolen"], id="mount-space"),
    pytest.param(["--mount=type=bind,source=/data/coworkers,target=/stolen"], id="mount-equals"),
    pytest.param(["--volumes-from", "orchestrator-container"], id="volumes-from"),
    pytest.param(["--rootfs", "/data/coworkers"], id="rootfs-space"),
    pytest.param(["--rootfs=/data/coworkers"], id="rootfs-equals"),
    pytest.param(["--tmpfs", "/data/coworkers/fixer/workspace"], id="tmpfs-space"),
    pytest.param(["--tmpfs=/scratch"], id="tmpfs-equals"),
    pytest.param(["--use-api-socket"], id="use-api-socket"),
    pytest.param(["--device", "/dev/sda:/dev/sda"], id="device"),
    pytest.param(["--privileged"], id="privileged"),
    pytest.param(["--frobnicate=1"], id="unknown-long-flag"),
    pytest.param(["-e", "FOO=bar"], id="short-env"),
    pytest.param(["nginx"], id="bare-positional"),
    pytest.param(["--network=ns:/proc/1/ns/net"], id="network-ns-hostpath"),
    pytest.param(["--volume-driver=local"], id="volume-driver"),
    pytest.param(["--shm-size=1g", "-v", "/data/coworkers:/stolen"], id="mount-after-allowed"),
])
def test_docker_extra_args_disallowed_flag_refused(module, tmp_path, extra):
    """Every docker_extra_args flag not on the mount-safe allowlist is refused — mount
    flags plus podman's --rootfs (an arbitrary host dir as the container root), --tmpfs
    (an out-of-set mount), --use-api-socket (the host daemon socket), --network=ns:<path>
    (a host-namespace join), --volume-driver, --device, --privileged, unknown flags, bare
    positionals, and short flags — all of which reach `docker run` verbatim
    (docker.py:1368-1401). A mount flag in a later flag position is still caught."""
    spec = _write(tmp_path / "s", _base(types={"fixer": _type(docker_extra_args=extra)}))
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(tmp_path / "out"))


def test_docker_extra_args_non_list_refused(module, tmp_path):
    """A scalar docker_extra_args (not a list) is refused, never split into flags."""
    spec = _write(tmp_path / "s", _base(types={"fixer": _type(docker_extra_args="-v /data:/x")}))
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(tmp_path / "out"))


def test_docker_extra_args_non_string_entry_refused(module, tmp_path):
    """A non-string docker_extra_args entry fails closed rather than being skipped the way
    docker.py:1371 would silently drop it."""
    spec = _write(tmp_path / "s", _base(
        types={"fixer": _type(docker_extra_args=["--shm-size=1g", 5])},
    ))
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(tmp_path / "out"))


@pytest.mark.parametrize("extra", [
    pytest.param(["--memory"], id="value-flag-missing-value"),
    pytest.param(["--memory="], id="value-flag-empty-inline-value"),
    pytest.param(["--memory", "-v/data/coworkers:/stolen"], id="value-flag-flaglike-value"),
])
def test_docker_extra_args_value_flag_needs_concrete_value(module, tmp_path, extra):
    """A value-taking allowlisted flag must be followed by a concrete, non-flag-like value
    (inline or space-separated, never empty). The flag-like-value case is why the walker
    cannot be tricked into swallowing a mount flag ("-v...") as an allowed flag's value
    instead of rejecting it."""
    spec = _write(tmp_path / "s", _base(types={"fixer": _type(docker_extra_args=extra)}))
    with pytest.raises(module.CompositionError):
        module.compose(str(spec), str(tmp_path / "out"))


def test_docker_extra_args_allowlisted_flags_pass_through(module, tmp_path):
    """Allowlisted non-mount flags render unchanged — inline =value, space-separated value,
    and boolean forms alike — and the closed docker_volumes set is unaffected."""
    extra = [
        "--shm-size=1g", "--cap-drop", "ALL", "--pids-limit", "512",
        "--read-only", "--memory=512m", "--ulimit", "nofile=1024:2048",
    ]
    spec = _write(tmp_path / "s", _base(types={"fixer": _type(docker_extra_args=extra)}))
    rendered = module.compose(str(spec), str(tmp_path / "out"))
    cfg = yaml.safe_load((Path(rendered["fixer"]) / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["terminal"]["docker_extra_args"] == extra
    assert cfg["terminal"]["docker_volumes"] == [
        "/data/coworkers/fixer/workspace:/data/coworkers/fixer/workspace:rw"
    ]
