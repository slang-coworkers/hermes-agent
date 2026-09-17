---
ac: AC-ISO-F14-1
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

# AC-ISO-F14-1 (carried) — proxied egress reaches 200 with the CA trusted (sandbox)

Carried from ISO-F14 under its original id (required PASS at merge-gate P5). From a
Hermes-driven spawn off a rendered profile (through the podman-onecli wrapper), an
outbound HTTPS request through the rendered `https_proxy` returns 200 via OneCLI, TLS
verified against the mounted CA, from a rootless sandbox. Shares AC-FLEET-F62-5/6's
fleet-boot spawn.

## Common substrate

Same wrapper wiring as AC-FLEET-F62-5. The CA is mounted `:ro` at the wrapper's
`PODMAN_ONECLI_EXPECTED_CA` container path.

## Setup (after the fixtures are installed)

- One rendered profile's Hermes-driven podman spawn through the podman-onecli wrapper
  (the common-substrate wiring).
- The profile's OneCLI identity holds the inference secret (granted).

## Steps

1. From the sandbox, `curl` an outbound HTTPS request through the rendered
   `https_proxy` → expect: 200 via OneCLI, TLS verified against the mounted CA;
   `/proc/1/uid_map == "0 1001 1"`.

## Pass

The rendered egress posture routes a real request through the OneCLI proxy to a 200
with the CA trusted, from a rootless sandbox.

## Evidence

`scenario-AC-ISO-F14-1/sandbox.log` + `uid_map.txt`.
