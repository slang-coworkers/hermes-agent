"""Unit tests for the OSH-F64 OpenShell-0.0.72 render validation.

Behaviour contract on ``_validate_openshell_egress`` / ``_validate_openshell_read_only``:
the optional ``broker_addr`` is appended to the allow-set and gets the same wildcard /
control-plane (``:10256``) refusal as every entry; the optional ``filesystem_read_only``
fail-closes on a present ``null`` / non-list / empty / control-char / relative / ``..`` /
wildcard entry, and an ABSENT key yields no ``filesystem_policy`` input (OSH-F63 three-slot
back-compat). No network; imports the plugin's compose module directly (its absolute
imports resolve in the venv).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PLUGIN_KEY = "nv-coworker-compose"
BASE_EGRESS = {
    "proxy_addr": "172.17.0.1:18255",
    "inference_route": "inference-api.nvidia.com:443",
    "ssh_control_path": "172.17.0.1:22",
    "sandbox_image": "localhost/hermes-openshell-sandbox:pinned",
}


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "plugins" / PLUGIN_KEY / "plugin.yaml").exists():
            return parent
    pytest.fail("could not locate repo root")


def _compose():
    path = _repo_root() / "plugins" / PLUGIN_KEY / "compose.py"
    spec = importlib.util.spec_from_file_location("osh_f64_compose_unit", path)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: compose.py's frozen @dataclass resolves cls.__module__ via
    # sys.modules during class creation, which is None for an unregistered synthetic module.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_broker_addr_appended_after_the_three_slots():
    """A declared broker_addr is the 4th allow slot, after proxy/inference/ssh, in order."""
    c = _compose()
    params = c._validate_openshell_egress({**BASE_EGRESS, "broker_addr": "172.17.0.1:18777"})
    assert params["allow"] == [
        "172.17.0.1:18255", "inference-api.nvidia.com:443", "172.17.0.1:22", "172.17.0.1:18777",
    ]


def test_absent_broker_and_fs_is_the_three_slot_backcompat():
    """No broker_addr / filesystem_read_only -> OSH-F63's three-slot allow, no fs policy input."""
    c = _compose()
    params = c._validate_openshell_egress(dict(BASE_EGRESS))
    assert params["allow"] == ["172.17.0.1:18255", "inference-api.nvidia.com:443", "172.17.0.1:22"]
    assert params["filesystem_read_only"] is None


def test_broker_addr_control_plane_is_refused():
    """The broker gets the same :10256 control-plane refusal as every allow entry."""
    c = _compose()
    with pytest.raises(c.CompositionError):
        c._validate_openshell_egress({**BASE_EGRESS, "broker_addr": "172.17.0.1:10256"})


def test_broker_addr_wildcard_is_refused():
    """A wildcard broker is refused like any wildcard allow entry."""
    c = _compose()
    with pytest.raises(c.CompositionError):
        c._validate_openshell_egress({**BASE_EGRESS, "broker_addr": "172.17.0.1:*"})


def test_filesystem_read_only_valid_is_returned():
    """A well-formed absolute-path lane is returned for the filesystem_policy render."""
    c = _compose()
    params = c._validate_openshell_egress({**BASE_EGRESS, "filesystem_read_only": ["/opt/osh-lane"]})
    assert params["filesystem_read_only"] == ["/opt/osh-lane"]


@pytest.mark.parametrize("bad", [
    None,                    # present-but-null (distinct from absent)
    [],                      # empty list
    "/opt/osh-lane",         # non-list (a bare string)
    ["/opt/osh-lane\t"],     # tab control char
    ["/opt/osh-lane\x01"],   # C0 control char
    ["/opt/osh-lane\x7f"],   # DEL
    ["opt/osh-lane"],        # relative path
    ["/opt/../etc"],         # .. traversal
    ["/opt/*"],              # wildcard
    [""],                    # empty entry
])
def test_filesystem_read_only_malformed_is_refused(bad):
    """A PRESENT filesystem_read_only fail-closes on any malformed value/entry."""
    c = _compose()
    with pytest.raises(c.CompositionError):
        c._validate_openshell_egress({**BASE_EGRESS, "filesystem_read_only": bad})
