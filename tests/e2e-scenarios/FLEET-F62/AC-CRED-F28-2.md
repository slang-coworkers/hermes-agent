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

Carried from CRED-F28 under its original id (required PASS at merge-gate P5;
strengthened in place per the operator's round-6 directive — the added revoke→401
step, id + `sandbox:` kind unchanged). From a profile SUBPROCESS (a Hermes-driven
`_DockerEnvironment` spawn, not a gateway turn), `curl` through the rendered
`https_proxy` returns 401 for profile A before its secret is granted, 200 after the
grant, 401 again after the grant is revoked, and 401 for a sibling profile B with no
grant — per-profile identity + request-time credential injection, no credential value
ever inside the sandbox. Shares AC-FLEET-F62-5/6's fleet boot; A = architect,
B = builder.

## Common substrate

Same wrapper wiring as AC-FLEET-F62-5. The podman-onecli config points
`gateway_api_base_url` at `http://172.17.0.1:10256` with `api_key_env`→`ONECLI_API_KEY`
LEFT UNSET (the bridge control plane is unauthenticated — see the ADR §Optional-key
reconciliation and the FLEET-F62 exact-origin base-URL edit); the render sets
`insecure_no_auth_origins: ["172.17.0.1:10256"]` so the keyless http bridge is reached.

## Setup (after the fixtures are installed)

- Two coworker profiles (A=architect, B=builder) rendered with distinct OneCLI
  identities.
- **This scenario runs FIRST, before any fleet-wide grant** (§ Deployment item 2
  ordering). The five OneCLI identities EXIST with a container-config — the operator
  onboarded them (probe `GET /v1/container-config?agent=<id>`; a "not configured" here is
  a `FAIL(env)`) — but A and B start UNGRANTED (no inference secret). This scenario stages
  A's grant itself in step 2; the other four are granted only AFTER it passes, before
  AC-FLEET-F62-6 and the AC-7/8 live pipeline. Do NOT pre-grant any identity here — the
  401-before-grant states below require A and B ungranted at entry.

## Steps

1. Profile A, ungranted: sandboxed `curl` through its `https_proxy` → expect: 401
   (identity exists, no secret).
2. Grant A the inference secret via the C1b-fixed `set_secrets` — resolve
   `identifier`→`uuid` (`GET /api/agents`), then
   `PUT /api/agents/<uuid>/secrets {"secretIds":["4d1da6ff-4af8-44f3-8831-07b11bfd004f"]}`
   → 200 (NOT the old `POST /api/agents/<identifier>/secrets`, which 404s); A again
   through its `https_proxy` → expect: 200 (§ Round-6 amendment C1b).
3. Revoke A: the same C1b `set_secrets` with `{"secretIds":[]}` (empty list) → A again
   through its `https_proxy` → expect: 401 (grant removed).
4. Sibling profile B, still ungranted: sandboxed `curl` through B's `https_proxy` →
   expect: 401.

## Round-6 (C1, exec-mediation)

Every profile's proxy `curl` in steps 1–4 is a HERMES-issued `docker exec` (not a raw
`podman exec`); the forwarded `docker_forward_env` proxy URL (all four spellings, incl.
the `aoc_` userinfo) wins over the `docker_env` placeholder at exec, so A's granted
request is 200 and its ungranted/revoked and B's are 401 — each carrying only its OWN
profile's token (no cross-profile bleed). See § Round-6 amendment C1.

## Pass

Per-profile identity + request-time injection hold — A goes 401→grant→200→revoke→401
on its own grant then revoke, B stays 401 with no grant.

## Evidence

`scenario-AC-CRED-F28-2/sandbox.log` — `podman-calls.log` (forwarded launch argv per
profile, token redacted to sha256), `http-a-pre.txt`(401)/`http-a-post.txt`(200)/`http-a-revoked.txt`(401)/`http-b-ungranted.txt`(401),
`uid_map-*.txt`(`0 1001 1`), `exec-via-hermes=PASS` (each `curl`'s exec in the wrapper
audit line); env-var NAMES only, the run refuses to write any `aoc_` token (token-absent
grep of `config.yaml`/argv/`podman-calls.log`).
