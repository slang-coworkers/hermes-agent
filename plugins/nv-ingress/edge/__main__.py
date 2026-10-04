"""``python3 -m edge --config <edge.yaml>``: run the host edge receiver until stopped."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import threading

from . import make_server


def main() -> int:
    parser = argparse.ArgumentParser(prog="python3 -m edge")
    parser.add_argument("--config", required=True, help="the rendered host/edge.yaml")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with open(args.config, encoding="utf-8") as handle:
        cfg = json.load(handle)
    server, thread = make_server(cfg)
    stop = threading.Event()
    sighup = getattr(signal, "SIGHUP", None)
    if sighup is not None:
        signal.signal(sighup, lambda *_a: server.edge.reload())
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_a: stop.set())
    stop.wait()
    server.shutdown()
    server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
