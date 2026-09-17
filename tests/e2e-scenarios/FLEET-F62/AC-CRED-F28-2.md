---
ac: AC-CRED-F28-2
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

# AC-CRED-F28-2 (carried) — per-profile identity + request-time injection (sandbox)

Carried from CRED-F28 under its original id (required PASS at merge-gate P5). From a
profile SUBPROCESS (a Hermes-driven `_DockerEnvironment` spawn, not a gateway turn),
`curl` through the rendered `https_proxy` returns 401 for profile A before its secrets
are assigned, 200 after `onecli agents set-secrets`, and 401 for a sibling profile B
with no grant — per-profile identity + request-time credential injection, no
credential value ever inside the sandbox. Shares AC-FLEET-F62-5/6's fleet boot;
A = architect, B = builder.

## Common substrate

Same wrapper wiring as AC-FLEET-F62-5. The podman-onecli config points
`gateway_api_base_url` at `http://172.17.0.1:10256` with `api_key_env`→`ONECLI_API_KEY`
LEFT UNSET (the bridge control plane is unauthenticated — see the ADR §Optional-key
reconciliation and the FLEET-F62 exact-origin base-URL edit); the render sets
`insecure_no_auth_origins: ["172.17.0.1:10256"]` so the keyless http bridge is reached.

## Setup (after the fixtures are installed)

- Two coworker profiles (A=architect, B=builder) rendered with distinct OneCLI
  identities.
- `onecli agents` for the five exist selective with only the inference secret.

## Steps

1. Profile A, ungranted: sandboxed `curl` through its `https_proxy` → expect: 401
   (identity exists, no secret).
2. `onecli agents set-secrets` the inference secret to A; A again → expect: 200.
3. Sibling profile B, still ungranted: sandboxed `curl` through B's `https_proxy` →
   expect: 401.

## Pass

Per-profile identity + request-time injection hold — A goes 401→200 on its own grant,
B stays 401 with no grant.

## Evidence

`scenario-AC-CRED-F28-2/sandbox.log` — `podman-calls.log` (forwarded launch argv per
profile), `http-a-pre.txt`(401)/`http-a-post.txt`(200)/`http-b-ungranted.txt`(401),
`uid_map-*.txt`(`0 1001 1`); env-var NAMES only, the run refuses to write any `aoc_`
token.
