---
ac: AC-FLEET-F62-6
kind: sandbox
model: stub
fixtures:
  - fixtures/podman/fleet-f62-default
  - fixtures/podman/fleet-f62-orchestrator
  - fixtures/podman/fleet-f62-architect
  - fixtures/podman/fleet-f62-builder
  - fixtures/podman/fleet-f62-tester
  - fixtures/podman/fleet-f62-reviewer
timeout_s: 300
---

# AC-FLEET-F62-6 — sandbox egress is OneCLI-proxy-only, credential-free (sandbox)

Inside each of the five sandboxes: the proxy env points at the OneCLI egress hop
(172.17.0.1:10255), the CA is readable and mounted `:ro`, and NO real provider
credential is present (only `HERMES_PROXY_TOKEN_*` swap tokens / placeholders); a
`curl` through the proxy reaches the model API (200/401), a `--noproxy` direct call
is refused (000), the control plane :10256 and tenant API :10254 are unreachable, and
the five profiles carry five DISTINCT proxy identities.

## Common substrate

Same wrapper wiring as AC-FLEET-F62-5 (`HERMES_DOCKER_BINARY` = the podman-onecli
wrapper, `PODMAN_ONECLI_*` coordinates). Shares AC-FLEET-F62-5's fleet-boot spawns.

## Setup (after the fixtures are installed)

- Same fleet-boot spawns as AC-FLEET-F62-5.
- The operator uid-1001 egress rule is present for the `--noproxy` clause (proven
  `direct 000` on the box).
- Per § Deployment item 2's grant ordering, AC-CRED-F28-2 has ALREADY run its
  ungranted checks FIRST, so ALL FIVE identities are now granted the inference secret:
  each identity has a container-config (probe `GET /v1/container-config?agent=<id>`; a
  "not configured" is a `FAIL(env)`) and the grant (`onecli agents set-secrets`) has been
  applied to all five after AC-CRED-F28-2 passed. Step 2 below is therefore a
  deterministic `200`, not a masked `401`.

## Steps

1. Inside each sandbox `env` → expect: `HTTPS_PROXY`/`https_proxy` authority ==
   172.17.0.1:10255, CA-trust vars set, CA file readable and mounted `:ro`; assert NO
   key in `plugin_strip_env_keys() ∪ _ALWAYS_STRIP_KEYS` carries a real value (only
   `HERMES_PROXY_TOKEN_*` swap tokens / placeholders).
2. `curl` via the proxy to `https://inference-api.nvidia.com/v1/models` → expect: 200
   (all five granted per § Deployment item 2 ordering); never a 401 (a 401 = failed grant
   setup) and never a TLS error.
3. `curl --noproxy '*' https://inference-api.nvidia.com/v1/models` → expect:
   http_code 000 / refused (a 401/403 here is a FAIL).
4. `curl` the control plane 172.17.0.1:10256 AND the tenant API 172.17.0.1:10254 from
   the sandbox → expect: both unreachable (000); only the proxy :10255 is reachable.
5. sha256 the injected proxy token per profile → expect: five DISTINCT identities
   (never log the value).

## Pass

Each sandbox reaches the outside world only via its own OneCLI proxy identity on
:10255, holds no real credential, and cannot reach the control plane :10256 or tenant
API :10254 directly.

## Evidence

`scenario-AC-FLEET-F62-6/sandbox.log` + `evidence.txt` (env-var NAMES and http codes
only, never token values).
