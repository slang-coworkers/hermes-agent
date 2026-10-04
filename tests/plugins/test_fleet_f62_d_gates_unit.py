"""Unit behaviour of the FLEET-F62.d plan-gate redirect rule beyond the acceptance test's case list."""

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PREDICATES = REPO_ROOT / "plugins" / "nv-fleet-gates" / "predicates.py"


def _predicates():
    spec = importlib.util.spec_from_file_location("fleet_f62d_gates_predicates", PREDICATES)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("command", [
    "git status 2>&1", "ls -la 2>/dev/null", "pytest -q >/dev/null 2>&1", "make 2>&1 | tail -5",
    "cmd >&2", "cmd &>/dev/null", "cmd 1>&-", "a 2>&1;b", "(a 2>&1)",
])
def test_stream_merge_is_not_a_write(command):
    assert _predicates().is_mutation("terminal", {"command": command}) is False


# bash keeps reading a redirect target up to the next metacharacter, so each of these opens a file
# (`>&2+notes.txt` creates `2+notes.txt`, `>&2"x"` creates `2x`).
@pytest.mark.parametrize("command", [
    "echo x > notes.txt", "echo x >> notes.txt", "git log 2>err.log", "cmd &> out.log",
    "cmd >&out.log", "cmd 2>&1 > out.log",
    "echo x >&2+notes.txt", "echo z >&2,n3", 'echo q >&2"x"', "echo y >/dev/null+n2",
    "cmd >&2x", "cmd >/dev/nullx",
])
def test_file_redirect_is_a_write(command):
    assert _predicates().is_mutation("terminal", {"command": command}) is True
