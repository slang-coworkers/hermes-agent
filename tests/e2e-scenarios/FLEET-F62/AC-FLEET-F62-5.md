---
ac: AC-FLEET-F62-5
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

# AC-FLEET-F62-5 — per-profile rootless podman sandbox, one per coworker (sandbox)

With the six profiles installed and the podman-onecli fail-closed wrapper set as
`HERMES_DOCKER_BINARY`, one sandboxed terminal call per coworker profile — issued
**as that profile's OWN process** (a `hermes -p <profile> chat -q` child / kanban
worker / direct `tools.terminal_tool` call under that `HERMES_HOME`, never a
gateway-served Bot Chat turn) — prints `/proc/1/uid_map == "0 1001 1"`; each
profile owns exactly one distinctly-labelled container; and each container's Mounts
are the rendered `docker_volumes` + Hermes's `:ro` CA/skills/credential mounts, none
under `$HERMES_HOME`, with `--memory`/`--pids-limit` present.

## Common substrate (the CRED-F28/ISO-F14 driver wiring, verbatim — NOT raw podman)

- `HERMES_DOCKER_BINARY=$WT/plugins/podman-onecli/bin/podman-onecli-wrap`
- `PODMAN_ONECLI_PODMAN=$PODMAN` (the `podman-remote-static` binary)
- `PODMAN_ONECLI_EXPECTED_PROXY=172.17.0.1:10255`
- `PODMAN_ONECLI_EXPECTED_CA=/etc/ssl/certs/hermes-egress-ca.crt`
- `PODMAN_ONECLI_LOG=$TB/podman-calls.log`
- `PODMAN_ONECLI_ALLOWED_ENV=ANTHROPIC_API_KEY` (every rendered coworker forwards
  `ANTHROPIC_API_KEY` as its provider placeholder, so the wrapper must allowlist it or
  it rejects the launch as a non-allowlisted env var; `PODMAN_ONECLI_FORBIDDEN_ENV`
  keeps `ONECLI_API_KEY` host-only by default)
- `CONTAINER_HOST=<the hermes-sandbox podman socket>`

Routing through the wrapper — never `$PODMAN` directly — is what makes the
credential-stripping / CA-`:ro` / argument-policing claims real; a raw-podman launch
bypasses the fail-closed allowlist and proves nothing.

## Setup (after the fixtures are installed)

- Install the six into a temp `HERMES_HOME`; `sudo install` the managed fragment (or
  export `HERMES_MANAGED_DIR`) so nv-fleet-gates reads the fleet role map.
- **Confirm the operator-provisioned SERVER-side bind sources** exist per § Deployment item 1:
  `/data/hermes-fleet/<role>/workspace` (rw, one per coworker) + `/opt/hermes-fleet/shared-learnings`
  (ro) on the podman host, uid-1001-accessible (the fixture spec's `workspace_root`/`shared_learnings_root`
  point at them). This testbed's podman is remote/rootless, so `-v host:container` sources resolve on the
  SERVER (`hermes-sandbox`) — do NOT `mkdir` them tester-side; a missing server-side dir is an exit-125
  statfs = `FAIL(env)`.
- Confirm the five operator-provisioned OneCLI identities have container-configs
  (§ Deployment item 2; secret grants stay scenario-staged, not pre-granted here).
- Confirm the podman socket + the `localhost/hermes-sandbox:pinned` image (§ Deployment
  item 4). `$PODMAN` = the `podman-remote-static` 3.4.4 binary.
- The `message_agent` delivery runner is host-local by design and is EXEMPT from
  this per-profile sandbox proof (named as such).

## Steps

1. For each of the five coworker profiles, issue one sandboxed terminal call as that
   profile's own process (`hermes -p <profile> chat -q "cat /proc/1/uid_map"` or a
   direct `tools.terminal_tool` call under that `HERMES_HOME`) → expect: stdout
   contains `0 1001 1`.
2. `$PODMAN ps -a --filter label=hermes-agent=1` → expect: exactly one container per
   profile, five distinct `hermes-profile=<name>` labels.
3. `$PODMAN inspect <each>` → expect: Mounts == the rendered `docker_volumes` +
   Hermes CA/skills/credential `:ro` mounts, NO source path under `$HERMES_HOME`;
   `--memory`/`--pids-limit` present.

## Pass

Every coworker's sandbox is rootless (`0 1001 1`), uniquely labelled, and its mounts
are the rendered set with no home leakage.

## Evidence

`scenario-AC-FLEET-F62-5/sandbox.log` + `mounts.json`; plus a `python3 -c` read of
each spawn's `state.db` where a fact is a row not a pixel (no `sqlite3` CLI in the
image).
