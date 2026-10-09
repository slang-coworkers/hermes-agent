"""A fake ``openshell`` 0.0.72 CLI for plugin tests that shell out to openshell (ING-F66 E16, ADR (A)).

Mocked-subprocess tests cannot validate the host CLI's argv parser. ``install()`` writes this fake as an executable into ``tmp_path``; a test passes its path as
``gw["openshell_bin"]`` and leaves ``subprocess`` alone. The fake enforces the 0.0.72 contract of
the two calls gateway-boot makes, ``sandbox exec`` and ``sandbox list``:

- the word after ``sandbox exec`` is ``-n <name>`` or ``--name <name>``. A positional name fails as
  on 0.0.72, which falls back to the last-used sandbox and runs the name as the command;
- ``sandbox exec`` takes only ``-n/--name``, ``--no-tty`` and ``--``; ``sandbox list`` takes nothing;
  any other subcommand or flag fails with ``unknown``;
- no argv element may contain a newline or carriage return.

Every call is recorded in ``calls.jsonl``; a refused call also goes to ``refusals.jsonl`` with the
reason. A valid call answers from the queue of its kind: ``probe`` (an exec whose command names
``/health``), ``inspect`` (any other exec) or ``list``. An answer is ``{"rc", "stdout", "stderr"}``,
``{"sleep": <s>}`` or ``{"run": true}`` (run the command after ``--`` on the host). The last answer
of a queue repeats; the cursor persists across calls in the fake's directory.
"""
from __future__ import annotations

import json
import os
import sys
import time

# The CLI half runs once per openshell call, so it imports only what it uses; the test-side helpers
# below import the rest when a test calls them.

_DIR_ENV = "NV_FAKE_OPENSHELL_DIR"
_LIST_TABLE = "NAME PHASE\ning-f66-gw Ready\n"


def refusal(argv: "list[str]") -> "tuple[int, str] | None":
    """``(exit code, reason)`` when 0.0.72 would refuse ``argv`` (the program name excluded), else None."""
    command_at = argv.index("--") + 1 if argv[:2] == ["sandbox", "exec"] and "--" in argv else len(argv)
    for i, arg in enumerate(argv):
        if "\n" in arg or "\r" in arg:
            # numbered as the broker numbers it: from 0 within the command after `--`
            return 1, f"command argument {i - command_at if i >= command_at else i} contains newline or carriage return"
    if argv[:1] != ["sandbox"] or argv[1:2] not in (["exec"], ["list"]):
        return 2, f"unknown subcommand: {' '.join(argv[:2])!r}"
    if argv[1] == "list":
        return (2, f"unknown flag for sandbox list: {argv[2]!r}") if len(argv) > 2 else None
    rest = argv[2:]
    if not rest or rest[0] not in ("-n", "--name"):
        if rest and not rest[0].startswith("-"):
            return 127, (f"positional sandbox name {rest[0]!r}: 0.0.72 falls back to the last-used sandbox "
                         "and runs it as the command")
        return 2, "sandbox exec needs -n/--name before --"
    if len(rest) < 2 or rest[1].startswith("-"):
        return 2, f"a value is required for {rest[0]!r}"
    i = 2
    while i < len(rest) and rest[i] != "--":
        if rest[i] != "--no-tty":
            return 2, f"unknown flag for sandbox exec: {rest[i]!r}"
        i += 1
    if i >= len(rest) - 1:
        return 2, "sandbox exec needs a command after --"
    return None


def _kind(argv: "list[str]") -> str:
    if argv[1] == "list":
        return "list"
    command = argv[argv.index("--") + 1:]
    return "probe" if any("/health" in a for a in command) else "inspect"


def _next_answer(d: str, kind: str) -> dict:
    with open(os.path.join(d, "answers.json"), encoding="utf-8") as f:
        queue = json.load(f).get(kind)
    if not queue:
        queue = [{"rc": 0, "stdout": _LIST_TABLE}] if kind == "list" else [{"rc": 0}]
    state_file = os.path.join(d, "state.json")
    state = {}
    if os.path.exists(state_file):
        with open(state_file, encoding="utf-8") as f:
            state = json.load(f)
    at = state.get(kind, 0)
    state[kind] = at + 1
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f)
    return queue[min(at, len(queue) - 1)]


def _append(path: str, record: dict) -> None:
    with open(path, "a", encoding="utf-8") as log:
        log.write(json.dumps(record) + "\n")


def main(argv: "list[str]") -> int:
    d = os.environ[_DIR_ENV]
    refused = refusal(argv)
    _append(os.path.join(d, "calls.jsonl"), {"argv": argv, "kind": None if refused else _kind(argv)})
    if refused:
        _append(os.path.join(d, "refusals.jsonl"), {"argv": argv, "reason": refused[1]})
        sys.stderr.write(f"Error: {refused[1]}\n")
        return refused[0]
    answer = _next_answer(d, _kind(argv))
    if answer.get("run"):
        import subprocess

        proc = subprocess.run(argv[argv.index("--") + 1:], stdin=subprocess.DEVNULL, capture_output=True, check=False)
        sys.stdout.buffer.write(proc.stdout)
        sys.stderr.buffer.write(proc.stderr)
        return proc.returncode
    if answer.get("sleep"):
        time.sleep(float(answer["sleep"]))
    sys.stdout.buffer.write(answer.get("stdout", "").encode("utf-8", "surrogateescape"))
    sys.stderr.buffer.write(answer.get("stderr", "").encode("utf-8", "surrogateescape"))
    return int(answer.get("rc", 0))


class FakeOpenshell:
    """Handle on one installed fake: its path, its answers and what it recorded."""

    def __init__(self, d):
        self.dir = d
        self.bin = str(d / "openshell")
        self.launch_log = d / "launches.log"
        self.start = [str(d / "start-recorder")]

    def answer(self, **queues: list) -> None:
        (self.dir / "answers.json").write_text(json.dumps(queues), encoding="utf-8")
        (self.dir / "state.json").unlink(missing_ok=True)

    def _read(self, name: str) -> "list[dict]":
        path = self.dir / name
        return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def calls(self, kind: "str | None" = None) -> "list[list[str]]":
        """Every recorded argv, or only the accepted calls of ``kind`` (``probe``, ``inspect``, ``list``)."""
        return [c["argv"] for c in self._read("calls.jsonl") if kind is None or c["kind"] == kind]

    def refusals(self) -> "list[dict]":
        return self._read("refusals.jsonl")

    def launches(self, expect: int = 0, timeout: float = 10.0) -> int:
        """How many times the start recorder ran, waiting up to ``timeout`` for ``expect`` of them.

        The supervisor starts it detached, so its record lands shortly after ``tick()`` returns.
        """
        deadline = time.monotonic() + (timeout if expect else 0.5)
        while True:
            n = len(self.launch_log.read_text(encoding="utf-8").splitlines()) if self.launch_log.exists() else 0
            if (expect and n >= expect) or time.monotonic() >= deadline:
                return n
            time.sleep(0.05)


def install(tmp_path, **queues: list) -> FakeOpenshell:
    """Write the fake CLI and a start recorder under ``tmp_path`` and load its answer queues."""
    import shlex
    from pathlib import Path

    d = Path(tmp_path) / "fake-openshell"
    d.mkdir(parents=True, exist_ok=True)
    for name, body in (
        # -S: the fake needs only the stdlib, and skipping site halves its start-up on every call
        ("openshell", f"{_DIR_ENV}={shlex.quote(str(d))} exec {shlex.quote(sys.executable)} -S "
                      f"{shlex.quote(str(Path(__file__).resolve()))} \"$@\"\n"),
        ("start-recorder", f"echo launched >> {shlex.quote(str(d / 'launches.log'))}\n"),
    ):
        script = d / name
        script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        script.chmod(0o755)
    fake = FakeOpenshell(d)
    fake.answer(**queues)
    return fake


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
