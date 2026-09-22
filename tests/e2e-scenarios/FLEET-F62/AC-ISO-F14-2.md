---
ac: AC-ISO-F14-2
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

# AC-ISO-F14-2 (carried) — direct egress is refused at connect (sandbox)

Carried from ISO-F14 under its original id (required PASS at merge-gate P5). From the
same spawn as AC-ISO-F14-1, a direct (non-proxy) `curl --noproxy` to the API host is
refused at connect (http_code 000, non-zero exit). A 401/403 is a FAIL (it means the
direct route reached the host).

## Common substrate

Same wrapper wiring as AC-FLEET-F62-5; shares the AC-ISO-F14-1 spawn.

## Setup (after the fixtures are installed)

- The operator uid-1001 egress rule is present (proven `direct 000` on the box).

## Steps

1. From the same sandbox, `curl --noproxy '*'` directly to the API host → expect:
   refused at connect (http_code 000, non-zero exit). A 401/403 is a FAIL.

## Pass

The only route out of the sandbox is the OneCLI proxy; a direct connection is refused
at the network layer.

## Evidence

`scenario-AC-ISO-F14-2/sandbox.log` (http_code + exit code).
