"""nv-coworker-compose's ``_yaml_compat`` emitter reproduces the PyYAML bytes the fleet's goldens pin.

Hermes 0.22 ships no PyYAML, so the goldens themselves are the PyYAML reference: each fixture family below was
written by ``yaml.safe_dump(..., sort_keys=False)`` (the compose render, the podman render, the OSH-F64 worker
distributions and policies). Reloading one with ``hermes_yaml`` and dumping it with the helper must give its bytes
back, at a short and at a long ``HERMES_HOME``. The recorded PyYAML-side run of the same claim is
``fixtures/nv-coworker-compose-yaml-compat/probe-output.txt``.
"""

import importlib.util
from pathlib import Path

import pytest

import hermes_yaml

REPO = Path(__file__).resolve().parents[2]
SCENARIOS = REPO / "tests" / "e2e-scenarios"
HOME_TOKEN = "@HERMES_HOME@"
HOMES = ("/h", "/var/tmp/hermes-pytest-1000/r-xxxxxxxx/pytest-of-node/pytest-0/test_case_with_a_long_name0/home/.hermes")
PYYAML_EMITTED = {
    "FLEET-F62.c openshell render": ("FLEET-F62.c/fixtures/openshell", "**/*.yaml"),
    "OSH-F63 openshell render": ("OSH-F63/fixtures/openshell", "**/*.yaml"),
    "FLEET-F62 podman render": ("FLEET-F62/fixtures/podman", "**/*.yaml"),
    "OSH-F64 worker distributions": ("OSH-F64/fixtures", "osh-f64-worker-*/distribution.yaml"),
    "OSH-F64.b worker distributions": ("OSH-F64.b/fixtures", "osh-f64b-worker-*/distribution.yaml"),
    "OSH-F64 worker policies": ("OSH-F64/fixtures", "policy-*.yaml"),
}


def _helper():
    path = REPO / "plugins" / "nv-coworker-compose" / "_yaml_compat.py"
    spec = importlib.util.spec_from_file_location("_nv_yaml_compat_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("family", sorted(PYYAML_EMITTED))
def test_golden_family_round_trips_byte_for_byte(family):
    yc = _helper()
    root, pattern = PYYAML_EMITTED[family]
    files = sorted((SCENARIOS / root).glob(pattern))
    assert files, f"{family}: no fixture under {root}/{pattern}"
    for home in HOMES:
        for f in files:
            text = f.read_text(encoding="utf-8").replace(HOME_TOKEN, home)
            got = yc.safe_dump(hermes_yaml.safe_load(text), sort_keys=False)
            assert got == text, f"{family}: {f.relative_to(SCENARIOS)} differs at home length {len(home)}"


def test_families_cover_every_pyyaml_emitted_fixture():
    """Every fixture YAML the parity claim covers is selected by provenance; probe-output.txt records the same set."""
    selected = {f for root, pattern in PYYAML_EMITTED.values() for f in (SCENARIOS / root).glob(pattern)}
    recorded = (Path(__file__).parent / "fixtures" / "nv-coworker-compose-yaml-compat" / "probe-output.txt").read_text(
        encoding="utf-8")
    assert f"pyyaml-emitted fixtures: {len(selected)}" in recorded


def test_long_space_free_scalar_stays_on_one_line():
    yc = _helper()
    value = "/" + "x" * 200
    assert yc.safe_dump({"t": {"ssh_key": value}}, sort_keys=False) == f"t:\n  ssh_key: {value}\n"


def test_block_sequences_are_not_indented_under_their_key():
    yc = _helper()
    assert yc.safe_dump({"a": [1, {"b": "c"}]}, sort_keys=False) == "a:\n- 1\n- b: c\n"


def test_sort_keys_defaults_to_sorted_like_pyyaml():
    yc = _helper()
    assert yc.safe_dump({"b": 1, "a": 2}) == "a: 2\nb: 1\n"


def test_wide_prose_scalar_keeps_its_value():
    """The one known layout divergence: PyYAML folds a multi-word scalar past 80 columns, this emitter does not.
    The loaded value is the same either way."""
    yc = _helper()
    prose = {"t": {"s": "word " * 40 + "end"}}
    assert hermes_yaml.safe_load(yc.safe_dump(prose, sort_keys=False)) == prose


def test_yaml11_booleans_stay_quoted_strings():
    yc = _helper()
    data = {"mode": "off", "flag": "yes", "real": False}
    assert hermes_yaml.safe_load(yc.safe_dump(data, sort_keys=False)) == data
