---
ac: AC-ISO-F14-5
kind: sandbox
model: n/a
fixtures:
  - fixtures/iso-f14-worker
timeout_s: 300
---

# AC-ISO-F14-5 — the render's cgroup memory limit reaches the kernel (HostConfig.Memory != 0)

The cgroup limits the render pins survive the wrapper's fail-closed allowlist and reach
the kernel: on the same Hermes-spawned sandbox as AC-ISO-F14-1,
`podman inspect --format '{{.HostConfig.Memory}}'` returns a non-zero value equal to the
pinned `container_memory` — proving the render's limit was not dropped by a fail-closed
`_cgroup_limits_ok=False`.

## Setup
- The AC-ISO-F14-1 container is up — reuse it. Capture its container id/name from
  `scenario-AC-ISO-F14-1/podman-calls.log`.

## Steps
1. `$PODMAN inspect --format '{{.HostConfig.Memory}}' <container>`.
   → expect: a non-zero integer equal to the pinned `container_memory`
   (1024 MiB = 1073741824 bytes).

## Pass
`HostConfig.Memory != 0` and equals the pinned limit (1073741824) — proving the render's
`container_memory` reached the kernel through the wrapper's cgroup-probe allowlist, not
dropped by a fail-closed `_cgroup_limits_ok=False`.

## Evidence
- `scenario-AC-ISO-F14-5/inspect-memory.log` — the `inspect` output.
