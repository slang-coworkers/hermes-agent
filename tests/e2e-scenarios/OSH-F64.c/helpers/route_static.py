"""AC-OSH-F64-4 G1: the static route check, before any model call.

Usage:
  python route_static.py policy <policy-gateway.yaml> <spec coworker-types.yaml>
  $PY route_static.py configs <spec coworker-types.yaml>

`policy` checks the gateway policy the sandbox was created with: its only REST endpoint is
inference.local:443 with exactly the four method+path rules, and its only other endpoint is the
spec's broker hop. `configs` checks every installed config under $HERMES_HOME (the default store and
each served role the spec declares) against the spec's egress.inference_provider: provider, base_url,
api_mode, the placeholder (compared by equality, never printed), the X-Hermes-Profile header, and the
absence of every second route or credential the gated render scrubs.

Prints one "g1 <store> <check> PASS|FAIL <detail>" line per check (key names and booleans
only), then "route_static_ok yes|no". Exits 0 only when no check FAILs.
"""
import os
import sys

import yaml

RULES = {("POST", "/v1/chat/completions"), ("POST", "/v1/messages"), ("POST", "/v1/responses"), ("GET", "/v1/models")}
HEADER = "X-Hermes-Profile"
failed = []


def check(store, name, ok, detail=""):
    print("g1", store, name, "PASS" if ok else "FAIL", detail)
    if not ok:
        failed.append((store, name))


def load(path):
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def policy(policy_path, spec):
    endpoints = [e for p in (load(policy_path).get("network_policies") or {}).values() for e in (p.get("endpoints") or [])]
    rest = [e for e in endpoints if e.get("protocol") == "rest"]
    raw = [e for e in endpoints if e.get("protocol") != "rest"]
    check("policy", "rest_endpoint", [(e.get("host"), e.get("port")) for e in rest] == [("inference.local", 443)],
          "rest_endpoints %d" % len(rest))
    rules = [((r.get("allow") or {}).get("method"), (r.get("allow") or {}).get("path")) for e in rest for r in (e.get("rules") or [])]
    check("policy", "rules", len(rules) == len(RULES) and set(rules) == RULES, "rules %d" % len(rules))
    host, _, port = str((spec.get("egress") or {}).get("broker_addr") or "").rpartition(":")
    check("policy", "raw_hop", [(e.get("host"), str(e.get("port"))) for e in raw] == [(host, port)], "raw_endpoints %d" % len(raw))


def dotted(cfg, path):
    for part in path.split("."):
        if not isinstance(cfg, dict) or part not in cfg:
            return False
        cfg = cfg[part]
    return True


def configs(spec):
    want = (spec.get("egress") or {}).get("inference_provider") or {}
    home = os.environ["HERMES_HOME"]
    default = spec.get("default_profile", "default")
    stores = {"default": os.path.join(home, "config.yaml")}
    for role in spec.get("types") or {}:
        if role != default:
            stores[role] = os.path.join(home, "profiles", role, "config.yaml")
    for store, path in stores.items():
        if not os.path.exists(path):
            check(store, "config_present", False)
            continue
        cfg = load(path)
        prov = (cfg.get("providers") or {}).get(want.get("provider")) or {}
        check(store, "provider", (cfg.get("model") or {}).get("provider") == want.get("provider"))
        check(store, "base_url", prov.get("base_url") == want.get("base_url"))
        check(store, "api_mode", prov.get("api_mode") == want.get("api_mode"))
        check(store, "placeholder", prov.get("api_key") == want.get("rewrite_placeholder"))
        check(store, "header", (prov.get("extra_headers") or {}).get(HEADER) == "%s-%s" % (want.get("attribution_tag_prefix"), store))
        scrub = ["custom_providers", "fallback_model", "fallback_providers", "secrets.onecli",
                 "plugins.entries.podman-onecli"] + ["delegation." + k for k in ("provider", "base_url", "api_key")]
        scrub += ["auxiliary.%s.%s" % (task, k) for task in (cfg.get("auxiliary") or {}) for k in ("provider", "base_url", "api_key")]
        for key in scrub:
            check(store, "absent:" + key, not dotted(cfg, key))
        check(store, "absent:plugins.enabled.podman-onecli",
              "podman-onecli" not in ((cfg.get("plugins") or {}).get("enabled") or []))


def main():
    mode = sys.argv[1]
    if mode == "policy":
        policy(sys.argv[2], load(sys.argv[3]))
    elif mode == "configs":
        configs(load(sys.argv[2]))
    else:
        print("unknown mode", mode, file=sys.stderr)
        return 2
    print("route_static_ok", "no" if failed else "yes")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
