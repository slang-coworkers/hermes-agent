"""Acceptance test for ISO-F13 — Mount composition / allowlist.

Ships to tests/plugins/test_nv_coworker_compose_mounts_acceptance.py. Drives the
nv-coworker-compose render (compose.py) on inline specs written under tmp_path
and reads each rendered profile's config.yaml back off disk, mirroring
tests/hermes_cli/test_plugin_api_compat.py and the fork's
tests/plugins/test_nv_coworker_compose_acceptance.py: the plugin is loaded
through the real discovery path from an isolated HERMES_HOME with an empty
HERMES_BUNDLED_PLUGINS.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from hermes_cli.plugins import PluginManager
from hermes_constants import get_default_hermes_root

pytestmark = pytest.mark.linux_only

PLUGIN_KEY = "nv-coworker-compose"
PLUGIN_SRC = Path(__file__).resolve().parents[2] / "plugins" / PLUGIN_KEY
WIKI_MOUNT = "/mnt/shared-learnings"


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
def loaded(tmp_path, monkeypatch):
    home = _write_home(tmp_path, monkeypatch)
    manager = PluginManager()
    manager.discover_and_load()
    entry = manager._plugins[PLUGIN_KEY]
    assert entry.enabled is True
    assert entry.error is None
    assert entry.module is not None
    return manager, entry.module, home


def _type(identity, *, container_persistent=..., mount_cwd=None, docker_volumes=None):
    terminal = {"backend": "docker", "docker_image": "hermes-sandbox:pinned"}
    if container_persistent is not ...:
        terminal["container_persistent"] = container_persistent
    if mount_cwd is not None:
        terminal["docker_mount_cwd_to_workspace"] = mount_cwd
    if docker_volumes is not None:
        terminal["docker_volumes"] = docker_volumes
    return {"identity": identity, "config": {"terminal": terminal}}


def _write_spec(spec_dir, *, workspace_root, install_surfaces, types, shared_learnings_root=None):
    spec_dir.mkdir(parents=True, exist_ok=True)
    # _require_canonical_profile_names demands orchestrator_profile be in the
    # roster, so name whichever type is present (or the first) as orchestrator.
    orchestrator = "orchestrator" if "orchestrator" in types else next(iter(types))
    spec = {
        "workspace_root": str(workspace_root),
        "install_surfaces": [str(s) for s in install_surfaces],
        "orchestrator_profile": orchestrator,
        "default_profile": "default",
        "types": types,
    }
    if shared_learnings_root is not None:
        spec["shared_learnings_root"] = str(shared_learnings_root)
    path = spec_dir / "coworker-types.yaml"
    path.write_text(yaml.safe_dump(spec), encoding="utf-8")
    return path


def _write_raw_spec(spec_dir, spec):
    """Write an arbitrary spec dict verbatim (for edge cases _write_spec cannot
    express, e.g. an omitted / null / blank workspace_root)."""
    spec_dir.mkdir(parents=True, exist_ok=True)
    path = spec_dir / "coworker-types.yaml"
    path.write_text(yaml.safe_dump(spec), encoding="utf-8")
    return path


def _render(module, spec_path, out_root):
    return module.compose(str(spec_path), str(out_root))


def _config(rendered_dir):
    return yaml.safe_load((Path(rendered_dir) / "config.yaml").read_text(encoding="utf-8"))


def _dig(mapping, dotted):
    node = mapping
    for key in dotted.split("."):
        assert isinstance(node, dict), f"{dotted}: {key} not a mapping"
        node = node[key]
    return node


def _volumes(cfg):
    return [str(v) for v in _dig(cfg, "terminal.docker_volumes")]


def _parse_vol(entry):
    parts = entry.split(":")
    assert len(parts) in (2, 3), f"malformed volume spec: {entry!r}"
    host, container = parts[0], parts[1]
    mode = parts[2] if len(parts) == 3 else "rw"
    return host, container, mode


def _assert_outside(host_path, root):
    resolved = Path(host_path).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        return
    pytest.fail(f"{host_path} resolves inside the fleet root {root}")


def test_plugin_loads_and_registers(loaded):
    manager, _module, _home = loaded
    assert "coworker" in manager._cli_commands


def test_ac_iso_f13_1(loaded, tmp_path):
    """Workspace mounted rw at a path whose volume string omits :/workspace, host outside the fleet
    root; a :/workspace* destination and an absent/null/blank workspace_root are refused."""
    module = loaded[1]
    fleet_root = get_default_hermes_root()
    spec = _write_spec(
        tmp_path / "spec",
        workspace_root="/data/coworkers",
        install_surfaces=["/data/shared-skills"],
        types={
            "orchestrator": _type("ORCH", container_persistent=True),
            "fixer": _type("FIX", container_persistent=False),
        },
    )
    rendered = _render(module, spec, tmp_path / "out")
    for name in ("orchestrator", "fixer"):
        volumes = _volumes(_config(rendered[name]))
        rw = [v for v in volumes if _parse_vol(v)[2] == "rw"]
        assert len(rw) == 1, f"{name}: expected one rw workspace mount, got {rw}"
        host, container, _mode = _parse_vol(rw[0])
        assert host == f"/data/coworkers/{name}/workspace"
        assert container == host
        # docker.py:998-999 suppresses the auto cwd->/workspace bind on any
        # volume string containing ":/workspace" as a substring.
        assert ":/workspace" not in rw[0]
        _assert_outside(host, fleet_root)

    for spec_dir, ws_root, surfaces in (
        ("spec-ws", "/workspace-fleet", []),
        ("spec-surf", "/data/coworkers", ["/workspace-tools"]),
    ):
        bad = _write_spec(
            tmp_path / spec_dir, workspace_root=ws_root, install_surfaces=surfaces,
            types={"fixer": _type("FIX", container_persistent=False)},
        )
        with pytest.raises(module.CompositionError):
            _render(module, bad, tmp_path / f"out-{spec_dir}")

    # workspace_root is a mandatory fleet key: omitted, null, and blank each
    # fail closed (this is what separates the chosen design from a fail-open
    # gate-on-presence form, so it must be pinned).
    base = {
        "install_surfaces": [], "orchestrator_profile": "fixer",
        "default_profile": "default",
        "types": {"fixer": _type("FIX", container_persistent=False)},
    }
    for spec_dir, override in (
        ("spec-ws-missing", {}),
        ("spec-ws-null", {"workspace_root": None}),
        ("spec-ws-blank", {"workspace_root": ""}),
    ):
        bad = _write_raw_spec(tmp_path / spec_dir, {**base, **override})
        with pytest.raises(module.CompositionError):
            _render(module, bad, tmp_path / f"out-{spec_dir}")


def test_ac_iso_f13_2(loaded, tmp_path):
    """docker_volumes is the closed set: workspace + shared-learnings clone + install surfaces."""
    module = loaded[1]
    surfaces = ["/data/shared-skills", "/data/shared-tools"]

    spec = _write_spec(
        tmp_path / "spec-noclone",
        workspace_root="/data/coworkers",
        install_surfaces=surfaces,
        types={"fixer": _type("FIX", container_persistent=False)},
    )
    volumes = _volumes(_config(_render(module, spec, tmp_path / "out-noclone")["fixer"]))
    assert "/data/coworkers/fixer/workspace:/data/coworkers/fixer/workspace:rw" in volumes
    for s in surfaces:
        assert f"{s}:{s}:ro" in volumes
    assert len(volumes) == 1 + len(surfaces)

    clone = "/data/learnings-clone"
    spec2 = _write_spec(
        tmp_path / "spec-clone",
        workspace_root="/data/coworkers",
        install_surfaces=surfaces,
        shared_learnings_root=clone,
        types={
            "orchestrator": _type("ORCH", container_persistent=True),
            "fixer": _type("FIX", container_persistent=False),
        },
    )
    rendered = _render(module, spec2, tmp_path / "out-clone")

    orch = _volumes(_config(rendered["orchestrator"]))
    assert "/data/coworkers/orchestrator/workspace:/data/coworkers/orchestrator/workspace:rw" in orch
    assert f"{clone}:{WIKI_MOUNT}" in orch
    for s in surfaces:
        assert f"{s}:{s}:ro" in orch
    assert len(orch) == 2 + len(surfaces)

    fixer = _volumes(_config(rendered["fixer"]))
    assert "/data/coworkers/fixer/workspace:/data/coworkers/fixer/workspace:rw" in fixer
    assert f"{clone}:{WIKI_MOUNT}:ro" in fixer
    for s in surfaces:
        assert f"{s}:{s}:ro" in fixer
    assert len(fixer) == 2 + len(surfaces)


def test_ac_iso_f13_3(loaded, tmp_path, monkeypatch):
    """Mounts resolving into/over the fleet root (incl. sibling profile dirs and
    via symlink) and spec-supplied docker_volumes are refused with CompositionError."""
    module = loaded[1]
    err = module.CompositionError

    # A <root>/profiles/<name> layout forces get_default_hermes_root() up to
    # <root>, making a sibling profile dir a descendant of the protected root.
    root = tmp_path / "fleet"
    (root / "profiles" / "orchestrator").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "orchestrator"))
    assert get_default_hermes_root().resolve() == root.resolve()

    def _fixer():
        return {"fixer": _type("FIX", container_persistent=False)}

    spec_a = _write_spec(tmp_path / "spec-a", workspace_root=root / "coworkers",
                         install_surfaces=[], types=_fixer())
    with pytest.raises(err):
        _render(module, spec_a, tmp_path / "out-a")

    target = root / "secret"
    target.mkdir()
    link_root = tmp_path / "sneaky"
    link_root.symlink_to(target)
    spec_b = _write_spec(tmp_path / "spec-b", workspace_root=link_root,
                         install_surfaces=[], types=_fixer())
    with pytest.raises(err):
        _render(module, spec_b, tmp_path / "out-b")

    spec_c = _write_spec(tmp_path / "spec-c", workspace_root="/data/coworkers",
                         install_surfaces=[root / "profiles" / "reviewer"], types=_fixer())
    with pytest.raises(err):
        _render(module, spec_c, tmp_path / "out-c")

    spec_d = _write_spec(tmp_path / "spec-d", workspace_root="/data/coworkers",
                         install_surfaces=[root.parent], types=_fixer())
    with pytest.raises(err):
        _render(module, spec_d, tmp_path / "out-d")

    spec_e = _write_spec(
        tmp_path / "spec-e", workspace_root="/data/coworkers", install_surfaces=[],
        types={"fixer": _type("FIX", container_persistent=False,
                              docker_volumes=["/etc:/etc:ro"])},
    )
    with pytest.raises(err):
        _render(module, spec_e, tmp_path / "out-e")

    spec_f = _write_spec(tmp_path / "spec-f", workspace_root="/data/coworkers",
                         install_surfaces=[], shared_learnings_root=root / "shared",
                         types=_fixer())
    with pytest.raises(err):
        _render(module, spec_f, tmp_path / "out-f")


def test_ac_iso_f13_4(loaded, tmp_path):
    """docker_mount_cwd_to_workspace forced true even when the spec sets false."""
    module = loaded[1]
    spec = _write_spec(
        tmp_path / "spec",
        workspace_root="/data/coworkers",
        install_surfaces=[],
        types={"fixer": _type("FIX", container_persistent=False, mount_cwd=False)},
    )
    cfg = _config(_render(module, spec, tmp_path / "out")["fixer"])
    assert _dig(cfg, "terminal.docker_mount_cwd_to_workspace") is True


def test_ac_iso_f13_5(loaded, tmp_path):
    """container_persistent rendered explicitly per role; omission / non-bool refused."""
    module = loaded[1]

    spec = _write_spec(
        tmp_path / "spec",
        workspace_root="/data/coworkers",
        install_surfaces=[],
        types={
            "orchestrator": _type("ORCH", container_persistent=True),
            "fixer": _type("FIX", container_persistent=False),
        },
    )
    rendered = _render(module, spec, tmp_path / "out")
    assert _dig(_config(rendered["orchestrator"]), "terminal.container_persistent") is True
    assert _dig(_config(rendered["fixer"]), "terminal.container_persistent") is False

    spec_missing = _write_spec(
        tmp_path / "spec-missing", workspace_root="/data/coworkers",
        install_surfaces=[], types={"fixer": _type("FIX")},
    )
    with pytest.raises(module.CompositionError):
        _render(module, spec_missing, tmp_path / "out-missing")

    spec_bad = _write_spec(
        tmp_path / "spec-bad", workspace_root="/data/coworkers", install_surfaces=[],
        types={"fixer": _type("FIX", container_persistent="false")},
    )
    with pytest.raises(module.CompositionError):
        _render(module, spec_bad, tmp_path / "out-bad")
