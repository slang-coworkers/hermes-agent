"""AC-OSH-F64-4 G3 self-test: one disallowed POST from a guarded process must be rejected before dispatch.

Usage: $PY guard_selftest.py   (started like a fleet process: Setup 5's background start, guard env set)

Sends one placeholder-only POST to https://inference.local/v1/messages, a path the guard never allows.
Prints "armed yes|no" (this PID's <pid>.armed marker) and "netns <ns>", then "selftest rejected" when
the guard raised before dispatch, or "selftest dispatched <status>" / "selftest error <class>" when it
did not. Exits 0 only when armed and rejected. Never prints a header value or a key.
"""
import os
import sys

import httpx
import yaml

guard_dir = os.environ.get("OSH_F64C_ROUTE_GUARD_DIR") or ""
armed = bool(guard_dir) and os.path.exists(os.path.join(guard_dir, "%d.armed" % os.getpid()))
print("armed", "yes" if armed else "no")
print("netns", os.readlink("/proc/self/ns/net"))
with open(os.path.join(os.environ["HERMES_HOME"], "config.yaml"), encoding="utf-8") as fh:
    provider = ((yaml.safe_load(fh) or {}).get("providers") or {}).get("compatible-endpoint") or {}
try:
    resp = httpx.post("https://inference.local/v1/messages", json={},
                      headers={"Authorization": "Bearer " + str(provider.get("api_key") or "")}, timeout=30)
except httpx.TransportError as exc:
    if "route guard rejected" in str(exc):
        print("selftest rejected")
        sys.exit(0 if armed else 1)
    print("selftest error", type(exc).__name__)
    sys.exit(1)
print("selftest dispatched", resp.status_code)
sys.exit(1)
