"""ING-F66 staging route script for the in-sandbox ingress route (D4).

Rendered into the DEFAULT profile's ``scripts/`` and run by the release webhook
adapter before its 202 (gateway/platforms/webhook.py:788-808 precede :969-981).
The forwarded envelope arrives on stdin. The script stages it durably in the
fleet ledger and prints it back with ``stage`` and ``self_authored``. It never
prints the silence sentinel, so an adapter ``ignored/script`` answer can only
mean a staging failure, which the edge retries.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

PLUGIN_KEY = "nv-ingress"
TRANSPORT_FIELD = "event_type"


def _plugin_dir() -> Path:
    import hermes_constants

    # The plugin discovery roots that can hold nv-ingress: the home's plugins/,
    # then the bundled dir (its override, else the tree beside hermes_constants).
    bundled = os.environ.get("HERMES_BUNDLED_PLUGINS")
    roots = [hermes_constants.get_hermes_home() / "plugins",
             Path(bundled) if bundled else Path(hermes_constants.__file__).resolve().parent / "plugins"]
    for base in roots:
        candidate = base / PLUGIN_KEY
        if (candidate / "ledger.py").is_file():
            return candidate
    raise FileNotFoundError("nv-ingress plugin directory not found")


def _ledger():
    path = _plugin_dir() / "ledger.py"
    spec = importlib.util.spec_from_file_location("_nv_ingress_stage_ledger", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _settings() -> dict:
    from hermes_cli.config import load_config_readonly

    entry = (((load_config_readonly() or {}).get("plugins") or {}).get("entries") or {}).get(PLUGIN_KEY) or {}
    return entry.get("settings") or {}


def main() -> int:
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict) or payload.get("schema") != "ingress.v1":
        print("not an ingress.v1 envelope", file=sys.stderr)
        return 2
    env = {k: v for k, v in payload.items() if k != TRANSPORT_FIELD}
    out = _ledger().stage(env, _settings())
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
