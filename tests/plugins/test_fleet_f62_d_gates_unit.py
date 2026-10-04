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


# Python's `\d`/`\s` are Unicode-aware, bash's fd digits and blanks are not: each of these creates
# a file (a CRLF script line `echo a >&2` creates `2\r`).
@pytest.mark.parametrize("command", [
    "printf x >&٢", "printf x >&2\rprobe", "printf x >&2\vo2", "printf x >&2\fo3",
    "printf x >&2 o5", "printf x >&2\xa0o6", "printf x >&2\x85o7", "printf x >&2\x1co8",
    "echo a >&2\r", "echo a >\r/dev/null",
    "echo a >&2\r\n", "echo a >&2\r\nls", "echo a >&2\r;ls", "echo a >&2\r && ls",
])
def test_unicode_or_control_redirect_target_is_a_write(command):
    assert _predicates().is_mutation("terminal", {"command": command}) is True


@pytest.mark.parametrize("command", [
    "echo a >&2\n", "echo a 2>&1\tls", "echo a >\t/dev/null",
])
def test_ascii_blank_still_ends_a_stream_merge(command):
    assert _predicates().is_mutation("terminal", {"command": command}) is False
