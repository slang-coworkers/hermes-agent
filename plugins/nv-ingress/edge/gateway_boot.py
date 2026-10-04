"""Host gateway-boot supervisor (D9): start the gateway only when it is truly down.

Health is checked INSIDE the gateway sandbox through the broker, independent of
the forward, so a forward-only outage never triggers a start. Starts are
serialized by a host lock file plus an in-flight window, so one failure causes
at most one start. The start command is the operator-approved argv from the
spec key ``ingress.gateway_start``. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List

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
    return subprocess.run(argv, capture_output=True, text=True, timeout=_EXEC_TIMEOUT_SECONDS, check=False)


def gateway_healthy(gw: Dict[str, Any]) -> bool:
    probe = f"curl -fsS --max-time 5 http://127.0.0.1:{int(gw['port'])}/health"
    try:
        return _run([gw["openshell_bin"], "sandbox", "exec", gw["sandbox"], "--", "sh", "-c", probe]).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


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


def tick(gw: Dict[str, Any], now: float) -> str:
    lock = Path(os.path.expanduser(gw["lock_file"]))
    action = decide(gateway_healthy(gw), start_in_flight(lock, now))
    if action != "start":
        return action
    if not sandbox_listed(gw):
        return "wait"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(now), encoding="utf-8")
    logger.warning("gateway-boot: in-sandbox health check failed; running the approved start")
    try:
        subprocess.Popen(list(gw["start"]), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        logger.error("gateway-boot: the start command could not be launched", exc_info=True)
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
