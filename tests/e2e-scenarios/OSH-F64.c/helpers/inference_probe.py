"""AC-OSH-F64-4 P1/P3: can a process on the fleet start path reach the managed inference route?

Usage: $PY inference_probe.py <profile>

Reads providers.compatible-endpoint from that served profile's config.yaml (`default` is
$HERMES_HOME/config.yaml) and sends ONE `GET <base_url>/models` with the config's own api_key and
extra_headers. It is not a model call. TLS verification is resolved the way Hermes resolves it for a
provider client (agent.ssl_verify.resolve_httpx_verify), and httpx keeps trust_env, so the substrate
proxy env applies. Prints the CA env names with their paths, then "status <code> models <n>" or
"status error <ExceptionClass>". Exits 0 only on HTTP 200. Never prints a header value or a key.
"""
import os
import sys

import httpx
import yaml
from agent.ssl_verify import resolve_httpx_verify

profile = sys.argv[1]
home = os.environ["HERMES_HOME"]
path = os.path.join(home, "config.yaml") if profile == "default" else os.path.join(home, "profiles", profile, "config.yaml")
with open(path, encoding="utf-8") as fh:
    provider = (yaml.safe_load(fh).get("providers") or {}).get("compatible-endpoint") or {}

for name in ("HERMES_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
    print("ca_env", name, os.environ.get(name) or "unset")
print("proxy_env", "HTTPS_PROXY", "set" if os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") else "unset")
print("netns", os.readlink("/proc/self/ns/net"))

headers = dict(provider.get("extra_headers") or {})
headers["Authorization"] = "Bearer " + str(provider.get("api_key") or "")
try:
    resp = httpx.get(
        str(provider.get("base_url") or "").rstrip("/") + "/models",
        headers=headers,
        verify=resolve_httpx_verify(ca_bundle=provider.get("ssl_ca_cert"), ssl_verify=provider.get("ssl_verify")),
        timeout=30,
    )
except httpx.HTTPError as exc:
    # httpx wraps a TLS verify failure and a refused connect alike as ConnectError; the root cause tells them apart.
    root = exc
    while root.__cause__ or root.__context__:
        root = root.__cause__ or root.__context__
    print("status error", type(exc).__name__, type(root).__name__)
    sys.exit(1)
try:
    models = len(resp.json().get("data") or [])
except ValueError:
    models = "unparsed"
print("status", resp.status_code, "models", models)
sys.exit(0 if resp.status_code == 200 else 1)
