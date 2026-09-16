"""Acceptance test for ISO-F15 — Session driver seam.

Contract: the nv-coworker-compose render (compose.py) emits, for EVERY rendered
profile (each coworker type AND the DEFAULT multiplexer), terminal.backend:
docker and terminal.docker_shared_container_key: "", and refuses — fail-closed,
all-or-nothing — any spec that sets terminal.backend to a value other than
"docker" or terminal.docker_shared_container_key to anything other than an
omitted key / the exact string "". `local` is banned fleet-wide; the podman
wrapper (HERMES_DOCKER_BINARY) is fleet-global deployment config this render
does NOT write.

Loader shape mirrors tests/hermes_cli/test_plugin_api_compat.py and the fork's
tests/plugins/test_nv_coworker_compose_acceptance.py: the plugin is loaded
through real discovery from an isolated HERMES_HOME with an empty
HERMES_BUNDLED_PLUGINS, then compose() is driven on inline specs written under
tmp_path and each rendered config.yaml is read back off disk.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from hermes_cli.plugins import PluginManager

PLUGIN_KEY = "nv-coworker-compose"
PLUGIN_SRC = Path(__file__).resolve().parents[2] / "plugins" / PLUGIN_KEY

# The fleet standardises on docker+podman, so every OTHER backend is refused —
# not only `local` but the other built-in container backends too, plus `podman`
# (the plausible operator mistake: podman is selected via HERMES_DOCKER_BINARY,
# the Hermes backend stays exactly "docker"), an arbitrary string, and a
# non-string (which must raise CompositionError, never a raw TypeError).
BANNED_BACKENDS = [
    "local",
    "ssh",
    "modal",
    "singularity",
    "daytona",
    "vercel_sandbox",
    "podman",
    "not-a-backend",
    ["docker"],
]

# Only an omitted key or the exact string "" is accepted. Falsey non-strings are
# refused because the config->env bridge serialises them to a NON-empty env
# string (False -> "False"), which would activate a shared container identity.
BANNED_SHARED_KEYS = [False, 0, [], {}, None, "team-shared"]


# --------------------------------------------------------------------------- #
# Isolated-HERMES_HOME load helpers (test_plugin_api_compat.py shape)
# --------------------------------------------------------------------------- #
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
    """Load nv-coworker-compose through PluginManager; yield (manager, module, home)."""
    home = _write_home(tmp_path, monkeypatch)
    manager = PluginManager()
    manager.discover_and_load()
    entry = manager._plugins[PLUGIN_KEY]
    assert entry.enabled is True
    assert entry.error is None
    assert entry.module is not None
    return manager, entry.module, home


# --------------------------------------------------------------------------- #
# Inline-spec + render helpers
# --------------------------------------------------------------------------- #
_OMIT = object()  # sentinel: leave the key out of the rendered spec entirely


def _terminal_block(backend, shared_key):
    terminal = {"container_persistent": True}  # ISO-F13: required per-role key
    if backend is not _OMIT:
        terminal["backend"] = backend
    if shared_key is not _OMIT:
        terminal["docker_shared_container_key"] = shared_key
    return terminal


def _type(identity="ROLE", *, backend=_OMIT, shared_key=_OMIT, extends=None):
    type_spec = {"identity": identity, "config": {"terminal": _terminal_block(backend, shared_key)}}
    if extends is not None:
        type_spec["extends"] = extends
    return type_spec


def _write_spec(
    spec_dir,
    *,
    types,
    default_config=None,
    spine_config=None,
    default_profile="default",
    orchestrator_profile=None,
):
    """Write coworker-types.yaml (+ an optional spine) and return its path.

    The orchestrator profile must be one of the declared coworker types
    (_require_canonical_profile_names), so it defaults to the first type. A
    spine's `config` merges into the DEFAULT config unconditionally and into a
    coworker type only when that type names the spine in `extends` (_resolve_type).
    """
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec = {
        "workspace_root": "/data/coworkers",  # ISO-F13: required fleet key
        "orchestrator_profile": orchestrator_profile or next(iter(types)),
        "default_profile": default_profile,
        "types": types,
    }
    if default_config is not None:
        spec["default_config"] = default_config
    if spine_config is not None:
        spines_dir = spec_dir / "spines"
        spines_dir.mkdir(parents=True, exist_ok=True)
        (spines_dir / "base.yaml").write_text(
            yaml.safe_dump({"config": spine_config}), encoding="utf-8"
        )
        spec["spines"] = {"base": {"source": "spines/base.yaml"}}
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
        assert isinstance(node, dict), f"{dotted}: {key} not a mapping (got {node!r})"
        assert key in node, f"{dotted}: {key} absent"
        node = node[key]
    return node


def _no_config_written(out_root):
    return not list(Path(out_root).rglob("config.yaml"))


def _err_names(exc, *, profile, key, value):
    """The CompositionError message names the profile, the key and the value."""
    msg = str(exc.value)
    return profile in msg and key in msg and (str(value) in msg or repr(value) in msg)


# --------------------------------------------------------------------------- #
# Load proof (registration; not an acceptance criterion)
# --------------------------------------------------------------------------- #
def test_plugin_loads_and_registers(loaded):
    manager, _module, _home = loaded
    assert "coworker" in manager._cli_commands


# --------------------------------------------------------------------------- #
# Acceptance criteria — one test per AC-ISO-F15-<n>, same order as the ADR
# --------------------------------------------------------------------------- #
def test_ac_iso_f15_1(loaded, tmp_path):
    """Every rendered profile — each coworker type AND the DEFAULT multiplexer — carries terminal.backend: docker, force-written."""
    module = loaded[1]
    spec = _write_spec(
        tmp_path / "spec",
        types={
            "fixer": _type("FIX"),
            "reviewer": _type("REV", backend="docker"),
        },
    )
    rendered = _render(module, spec, tmp_path / "out")
    for name in ("fixer", "reviewer", "default"):
        assert name in rendered, f"{name} not rendered: {sorted(rendered)}"
        assert _dig(_config(rendered[name]), "terminal.backend") == "docker", name


def test_ac_iso_f15_2(loaded, tmp_path):
    """Any terminal.backend other than docker on any profile is banned (CompositionError, all-or-nothing); local banned fleet-wide."""
    module = loaded[1]
    CompositionError = module.CompositionError

    for case_index, value in enumerate(BANNED_BACKENDS):
        spec = _write_spec(tmp_path / f"spec-cw-{case_index}", types={"fixer": _type("FIX", backend=value)})
        with pytest.raises(CompositionError) as exc:
            _render(module, spec, tmp_path / f"out-cw-{case_index}")
        assert _err_names(exc, profile="fixer", key="terminal.backend", value=value), str(exc.value)

        spec = _write_spec(
            tmp_path / f"spec-def-{case_index}",
            types={"fixer": _type("FIX", backend="docker")},
            default_config={"terminal": {"backend": value}},
        )
        with pytest.raises(CompositionError) as exc:
            _render(module, spec, tmp_path / f"out-def-{case_index}")
        assert _err_names(exc, profile="default", key="terminal.backend", value=value), str(exc.value)

        # DEFAULT pinned docker so the rejection is attributed to the coworker, not DEFAULT.
        spec = _write_spec(
            tmp_path / f"spec-sp-{case_index}",
            types={"fixer": _type("FIX", extends=["base"])},
            default_config={"terminal": {"backend": "docker"}},
            spine_config={"terminal": {"backend": value}},
        )
        with pytest.raises(CompositionError) as exc:
            _render(module, spec, tmp_path / f"out-sp-{case_index}")
        assert _err_names(exc, profile="fixer", key="terminal.backend", value=value), str(exc.value)

    # All-or-nothing: the invalid profile is DEFAULT (resolved/written last), yet a
    # fail-closed pre-pass must leave ZERO config.yaml on disk.
    late = _write_spec(
        tmp_path / "spec-late",
        types={"fixer": _type("FIX", backend="docker"), "reviewer": _type("REV", backend="docker")},
        default_config={"terminal": {"backend": "local"}},
    )
    out_late = tmp_path / "out-late"
    with pytest.raises(CompositionError):
        _render(module, late, out_late)
    assert _no_config_written(out_late), "late-invalid render left a partial fleet on disk"

    clean = _write_spec(tmp_path / "spec-clean", types={"fixer": _type("FIX", backend="docker")})
    rendered = _render(module, clean, tmp_path / "out-clean")
    assert _dig(_config(rendered["fixer"]), "terminal.backend") == "docker"


def test_ac_iso_f15_3(loaded, tmp_path):
    """terminal.docker_shared_container_key is force-written empty on every profile; every other value (incl falsey non-strings) is refused."""
    module = loaded[1]
    CompositionError = module.CompositionError

    spec = _write_spec(
        tmp_path / "spec-ok",
        types={"fixer": _type("FIX", backend="docker"), "reviewer": _type("REV", backend="docker", shared_key="")},
    )
    rendered = _render(module, spec, tmp_path / "out-ok")
    for name in ("fixer", "reviewer", "default"):
        assert _dig(_config(rendered[name]), "terminal.docker_shared_container_key") == "", name

    for case_index, value in enumerate(BANNED_SHARED_KEYS):
        spec = _write_spec(
            tmp_path / f"spec-sk-{case_index}",
            types={"fixer": _type("FIX", backend="docker", shared_key=value)},
        )
        with pytest.raises(CompositionError) as exc:
            _render(module, spec, tmp_path / f"out-sk-{case_index}")
        assert _err_names(exc, profile="fixer", key="terminal.docker_shared_container_key", value=value), str(exc.value)

    # All-or-nothing: an invalid shared key on DEFAULT (last) leaves no config.yaml.
    late = _write_spec(
        tmp_path / "spec-sk-late",
        types={"fixer": _type("FIX", backend="docker")},
        default_config={"terminal": {"docker_shared_container_key": "team-shared"}},
    )
    out_late = tmp_path / "out-sk-late"
    with pytest.raises(CompositionError):
        _render(module, late, out_late)
    assert _no_config_written(out_late), "late-invalid shared-key render left a partial fleet on disk"
