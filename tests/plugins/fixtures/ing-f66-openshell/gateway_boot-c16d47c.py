"""Host gateway-boot supervisor (D9): start the gateway only when it is truly down.

Health is checked INSIDE the gateway sandbox through the broker, independent of
the forward, so a forward-only outage never triggers a start. Starts are
serialized by a host lock file plus an in-flight window, so one failure causes
at most one start. Before a start the sandbox's own processes are inspected: a
live Hermes gateway (healthy or not) or an inspection that cannot be trusted
blocks the start, and a live gateway is logged for the operator, never killed
(E15). The start command is the operator-approved argv from the spec key
``ingress.gateway_start``. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("nv_ingress.gateway_boot")

CHECK_INTERVAL_SECONDS = 15
START_IN_FLIGHT_SECONDS = 120
_EXEC_TIMEOUT_SECONDS = 60


def decide(gateway_healthy: bool, start_in_flight: bool) -> str:
    """``start`` only when the in-sandbox health check failed and no start is in flight."""
    if gateway_healthy:
        return "idle"
    if start_in_flight:
        return "wait"
    return "start"


def _run(argv: List[str]) -> subprocess.CompletedProcess:
    # surrogateescape: a sandbox process's argv may hold non-UTF-8 bytes; they must not crash the
    # supervisor, and the ASCII Hermes tokens still compare exactly.
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                          errors="surrogateescape", timeout=_EXEC_TIMEOUT_SECONDS, check=False)


def _sandbox_sh(gw: Dict[str, Any], script: str) -> subprocess.CompletedProcess:
    # openshell takes the sandbox only as `-n <name>`: a positional name falls back to the
    # last-used sandbox and is run as the command (exit 127).
    return _run([gw["openshell_bin"], "sandbox", "exec", "-n", gw["sandbox"], "--no-tty", "--", "sh", "-c", script])


def gateway_healthy(gw: Dict[str, Any]) -> bool:
    probe = f"curl -fsS --max-time 5 http://127.0.0.1:{int(gw['port'])}/health"
    try:
        return _sandbox_sh(gw, probe).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# One line per process: "<pid>\t<state>\t<name>\t<argv>", the argv NUL-joined as \x1f (with
# newline and tab as \x1e and \x1d), then "#end". The match is done host-side on argv tokens, so
# this script carries no gateway token that could match itself. A pid whose /proc entry vanished
# mid-read exited and is skipped; any other failed read prints ERR and aborts without "#end", so
# an unreadable process is never mistaken for an absent one.
_INSPECT_SCRIPT = r"""
[ -r /proc/self/stat ] || { echo "ERR - proc"; exit 3; }
for d in /proc/[0-9]*; do
  p=${d#/proc/}
  s=$(cat "$d/stat" 2>/dev/null)
  if [ -z "$s" ]; then [ -d "$d" ] || continue; echo "ERR $p stat"; exit 3; fi
  if ! cl=$(tr '\000\n\t' '\037\036\035' < "$d/cmdline" 2>/dev/null); then
    [ -d "$d" ] || continue; echo "ERR $p cmdline"; exit 3
  fi
  n=${s#*\(}; n=${n%\)*}; r=${s##*\) }
  printf '%s\t%s\t%s\t%s\n' "$p" "${r%% *}" "$(printf '%s' "$n" | tr '\t\n' '  ')" "$cl"
done
echo '#end'
"""

_ENTRY_NAMES = ("hermes", "hermes.real")
_PYTHON = re.compile(r"python(3(\.\d+)?)?")


def _command_args(argv: List[str]) -> Optional[List[str]]:
    """The Hermes CLI arguments of ``argv``, or None when it is not a Hermes entry point.

    Entry points: a ``hermes``/``hermes.real`` executable, or a python interpreter running one
    of those scripts, ``hermes_cli/main.py`` or ``-m hermes_cli.main``.
    """
    if not argv:
        return None
    if os.path.basename(argv[0]) in _ENTRY_NAMES:
        return argv[1:]
    if not _PYTHON.fullmatch(os.path.basename(argv[0])):
        return None
    i = 1
    while i < len(argv) and argv[i].startswith("-"):
        opt = argv[i]
        if opt == "-m":
            return argv[i + 2:] if i + 1 < len(argv) and argv[i + 1] == "hermes_cli.main" else None
        if opt in ("-c", "-"):
            return None
        i += 2 if opt in ("-X", "-W") else 1
    if i < len(argv) and (os.path.basename(argv[i]) in _ENTRY_NAMES or argv[i].endswith("hermes_cli/main.py")):
        return argv[i + 1:]
    return None


def is_gateway_argv(argv: List[str]) -> bool:
    """A Hermes ``gateway run|restart`` (or bare ``gateway``) invocation, by the release's token rules."""
    args = _command_args(argv)
    if args is None:
        return False
    rest: List[str] = []
    skip = False
    for tok in args:
        if skip:
            skip = False
        elif tok in ("-p", "--profile"):
            skip = True
        elif not tok.startswith(("-p=", "--profile=")):
            rest.append(tok)
    if "gateway" not in rest:
        return False
    i = rest.index("gateway")
    return i + 1 == len(rest) or rest[i + 1] in ("run", "restart")


def gateway_processes(gw: Dict[str, Any]) -> Tuple[Optional[List[int]], str]:
    """``(pids, reason)``: the live non-zombie Hermes gateway pids in the sandbox.

    ``pids`` is None when the inspection failed or its result is ambiguous; ``reason`` says why.
    """
    try:
        proc = _sandbox_sh(gw, _INSPECT_SCRIPT)
    except subprocess.TimeoutExpired:
        return None, "the process inspection timed out"
    except OSError as exc:
        return None, f"the process inspection could not run ({exc})"
    # split("\n"), not splitlines(): str.splitlines() also breaks on \x1e and \x1d.
    lines = proc.stdout.rstrip("\n").split("\n") if proc.stdout else []
    err = next((ln for ln in lines if ln.startswith("ERR ")), None)
    if err is not None:
        _, pid, what = (err.split(" ", 2) + ["?", "?"])[:3]
        return None, ("/proc is not readable" if pid == "-" else f"the {what} of pid {pid} could not be read")
    if proc.returncode != 0 or not lines or lines[-1] != "#end":
        tail = lines[-1][:120] if lines else (proc.stderr or "").strip()[:120]
        return None, f"the process inspection failed (exit {proc.returncode}, last output {tail!r})"
    pids: List[int] = []
    for line in lines[:-1]:
        parts = line.split("\t", 3)
        if len(parts) != 4 or not parts[0].isdigit() or len(parts[1]) != 1:
            return None, f"the process inspection returned an unparsable line {line[:120]!r}"
        pid, state, name, raw = parts
        if state in ("Z", "X"):
            continue
        argv = [a.replace("\x1e", "\n").replace("\x1d", "\t") for a in raw.split("\x1f")]
        if argv[-1] == "":
            argv.pop()
        if not argv and (name in _ENTRY_NAMES or _PYTHON.fullmatch(name)):
            return None, f"pid {pid} ({name}) has an empty argv"
        if is_gateway_argv(argv):
            pids.append(int(pid))
    return pids, f"inspected {len(lines) - 1} processes"


def sandbox_listed(gw: Dict[str, Any]) -> bool:
    try:
        proc = _run([gw["openshell_bin"], "sandbox", "list"])
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0 and any(gw["sandbox"] in line.split() for line in proc.stdout.splitlines())


def start_in_flight(lock: Path, now: float) -> bool:
    try:
        return now - lock.stat().st_mtime < START_IN_FLIGHT_SECONDS
    except FileNotFoundError:
        return False


_last_block: Optional[tuple] = None


def _warn_once(key: tuple, msg: str, *args: Any) -> None:
    # The check repeats every 15 s while a gateway is held; log each distinct condition once.
    global _last_block
    if key != _last_block:
        logger.warning(msg, *args)
    _last_block = key


def tick(gw: Dict[str, Any], now: float) -> str:
    global _last_block
    lock = Path(os.path.expanduser(gw["lock_file"]))
    if gateway_healthy(gw):
        _last_block = None
        return decide(True, False)
    pids, detail = gateway_processes(gw)
    if pids is None:
        _warn_once(("unknown", detail), "gateway-boot: in-sandbox health check failed and the process inspection "
                   "of %s is inconclusive: %s; not starting — read that sandbox's processes by hand",
                   gw["sandbox"], detail)
        return "unknown"
    if pids:
        _warn_once(("held", tuple(pids)), "gateway-boot: in-sandbox health check failed but a Hermes gateway is "
                   "alive in %s (pids %s); not starting another and not stopping it — inspect or restart it by hand",
                   gw["sandbox"], pids)
        return "held"
    _last_block = None
    action = decide(False, start_in_flight(lock, now))
    if action != "start":
        return action
    if not sandbox_listed(gw):
        return "wait"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(now), encoding="utf-8")
    try:
        subprocess.Popen(list(gw["start"]), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        logger.error("gateway-boot: the start command could not be launched", exc_info=True)
        return "start-failed"
    logger.info("gateway-boot: inspection found zero live gateway PIDs in %s (%s); running the approved start",
                gw["sandbox"], detail)
    return "start"


def main() -> int:
    parser = argparse.ArgumentParser(prog="python3 -m edge.gateway_boot")
    parser.add_argument("--config", required=True, help="the rendered host/edge.yaml")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with open(args.config, encoding="utf-8") as handle:
        gw = json.load(handle)["gateway"]
    while True:
        tick(gw, time.time())
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
