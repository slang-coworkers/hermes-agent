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
match the active-profile allowlist — the rendered `docker_volumes` (each with its
declared mode) outside every home + only the ACTIVE profile's Hermes-managed
`$HERMES_HOME` binds (credential/skills/cache/CA `:ro`, the persistent sandbox
home/workspace `rw`) — with `--memory`/`--pids-limit` present.

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
- `TERMINAL_CWD=/root` on every profile spawn (preflight + counted), so
  `docker_mount_cwd_to_workspace: true` does NOT auto-mount the tester checkout at
  `/workspace`: `terminal_tool.py:1817` treats `/root` as a non-host cwd, whereas an
  unset cwd under `/home` (`_HOST_CWD_PREFIXES`) WOULD be bound as an unshared,
  non-allowlisted source

Routing through the wrapper — never `$PODMAN` directly — is what makes the
credential-stripping / CA-`:ro` / argument-policing claims real; a raw-podman launch
bypasses the fail-closed allowlist and proves nothing.

## Setup (after the fixtures are installed)

- **Shared-fs `$HERMES_HOME` (§ Deployment item 5, operator DONE):** set
  `TB=/workspace/extra/hermes-fleet-testbed/hermes-FLEET-F62`, `HERMES_HOME=$TB/home`
  (install the six profiles there), and leave `TERMINAL_SANDBOX_DIR` UNSET — so every
  Hermes-emitted bind (`sandboxes/docker/<p>/home`, `workspace`, `skills`, `cache`)
  resolves at the SAME absolute path on the tester and the rootless podman server
  (uid-1000 + uid-1001 rwx). `sudo install` the managed fragment (or export
  `HERMES_MANAGED_DIR`) so nv-fleet-gates reads the fleet role map.
- **Confirm the operator-provisioned SERVER-side bind sources** exist per § Deployment item 1:
  `/data/hermes-fleet/<role>/workspace` (rw, one per coworker) + `/opt/hermes-fleet/shared-learnings`
  on the podman host, uid-1001-accessible (the fixture spec's `workspace_root`/`shared_learnings_root`
  point at them; the shared-learnings mount is `rw` for the orchestrator and `ro` for the other four,
  per the rendered volumes). This testbed's podman is remote/rootless, so `-v host:container` sources
  resolve on the SERVER (`hermes-sandbox`) — do NOT `mkdir` them tester-side; a missing server-side
  dir is an exit-125 statfs = `FAIL(env)`.
- Confirm the five operator-provisioned OneCLI identities have container-configs
  (§ Deployment item 2; secret grants stay scenario-staged, not pre-granted here).
- Confirm the podman socket + the `localhost/hermes-sandbox:pinned` image (§ Deployment
  item 4). `$PODMAN` = the `podman-remote-static` 3.4.4 binary.
- **UNCOUNTED environment PREFLIGHT — gates the counted round-4 run.** For each of the
  **five coworker profiles (excluding the launch `default`** — a sixth `default` container
  would break the five-container criterion), run one wrapper-mediated sandbox spawn with
  `TERMINAL_CWD=/root` (§ Common substrate — so the auto-cwd bind does not add the tester
  checkout) and inspect the FINAL podman mount list against the same allowlist step 3
  asserts. **Classify failures — do NOT blanket-label them `FAIL(env)`:** a missing or
  unreachable socket, a missing image, a missing server-side bind SOURCE, a host-permission
  error, or an exit-125 `statfs` is a `FAIL(env)` (an operator/infra prerequisite) and
  halts before the counted rows; but an UNEXPECTED mount source/destination/mode, a
  cross-profile `$HERMES_HOME` bind, a wrong container label, a missing
  `--memory`/`--pids-limit`, `uid_map != "0 1001 1"`, or any other sandbox-policy mismatch
  is a PRODUCT defect that COUNTS against round 4 — it is exactly the isolation this row
  asserts, and the round-4 bar counts every non-env FAIL. Allowlisting by SOURCE ROOT
  catches EVERY bind site (auto-cwd `docker.py:1003`, credential, skills, CA,
  `docker_extra_args`), not only the persistent `$HERMES_HOME` binds.
- The `message_agent` delivery runner is host-local by design and is EXEMPT from
  this per-profile sandbox proof (named as such).

## Steps

1. For each of the five coworker profiles, issue one sandboxed terminal call as that
   profile's own process (`hermes -p <profile> chat -q "cat /proc/1/uid_map"` or a
   direct `tools.terminal_tool` call under that `HERMES_HOME`; the § Common substrate env
   applies, incl. `TERMINAL_CWD=/root`) → expect: stdout contains `0 1001 1`.
2. `$PODMAN ps -a --filter label=hermes-agent=1` → expect: exactly one container per
   profile, five distinct `hermes-profile=<name>` labels.
3. `$PODMAN inspect <each>` → expect: the resolved mount set == the fleet-RENDERED
   `docker_volumes` ∪ Hermes's managed mounts, asserted as an ALLOWLIST by source root —
   (a) **preserve every rendered `docker_volumes` source, destination, and declared mode**
   (each `/data/hermes-fleet/<p>/workspace` is `rw`, the orchestrator's
   `/opt/hermes-fleet/shared-learnings` is `rw` and the other four coworkers' is `ro`, the
   CA is `ro`), and every rendered source resolves OUTSIDE every profile home; (b) every
   Hermes-managed `$HERMES_HOME` source belongs to the ACTIVE profile ONLY — a source under
   ANOTHER profile's home is a FAIL (cross-profile exclusion); (c) NO source outside the
   allowlist { active profile's `$HERMES_HOME`, `/data/hermes-fleet/<p>/workspace`,
   `/opt/hermes-fleet/shared-learnings`, `/opt/onecli/ca.crt` }; (d) among the ADDITIONAL
   Hermes-managed mounts, credential/skills/cache/CA are `ro` and only the persistent
   sandbox home/workspace are `rw`; (e) `--memory` + `--pids-limit` present. (This replaces
   the earlier blanket "NO source under `$HERMES_HOME`", unsatisfiable against stock Hermes
   which always emits `$HERMES_HOME` sandbox-home/cache binds, with a substantive allowlist:
   the rendered volumes stay outside every home and the Hermes-managed `$HERMES_HOME` binds
   are confined to the active profile, so a cross-profile home leak is still caught — a
   strengthening, NOT a weakening.)

## Pass

Every coworker's sandbox is rootless (`0 1001 1`), uniquely labelled, and its mount set
matches the allowlist — the rendered `docker_volumes` (each with its declared mode)
outside every home, plus only the ACTIVE profile's Hermes-managed `$HERMES_HOME` binds
(credential/skills/cache/CA `ro`, the dedicated sandbox home/workspace `rw`) — so no
cross-profile home leak. The `message_agent` delivery runner (host-local by design) is
exempt and named as such.

## Evidence

`scenario-AC-FLEET-F62-5/sandbox.log` + `mounts.json`; plus a `python3 -c` read of
each spawn's `state.db` where a fact is a row not a pixel (no `sqlite3` CLI in the
image).
