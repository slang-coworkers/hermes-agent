---
ac: AC-ISO-F14-5
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

# AC-ISO-F14-5 (carried) — the cgroup memory limit reaches the kernel (sandbox)

Carried from ISO-F14 under its original id (required PASS at merge-gate P5). On the
Hermes-spawned sandbox, `podman inspect` of `HostConfig.Memory` returns non-zero equal
to the pinned `container_memory` — the render's cgroup limits survive the wrapper's
fail-closed allowlist and reach the kernel. Shares AC-FLEET-F62-5/6's fleet-boot
spawn.

## Common substrate

Same wrapper wiring as AC-FLEET-F62-5; shares the AC-ISO-F14-1 spawn. The render pins
`terminal.container_memory` per profile (1024 MiB in the podman fixture).

## Setup (after the fixtures are installed)

- GAP1 cpu-cgroup delegation done on the box (`Delegate=cpu cpuset io memory pids`).

## Steps

1. `$PODMAN inspect --format '{{.HostConfig.Memory}}'` the Hermes-spawned sandbox →
   expect: non-zero, equal to the pinned `container_memory` bytes (1024 MiB =
   1073741824).

## Pass

The render's cgroup memory limit survives the wrapper's fail-closed allowlist and
reaches the kernel.

## Evidence

`scenario-AC-ISO-F14-5/inspect.json` (`HostConfig.Memory`).
