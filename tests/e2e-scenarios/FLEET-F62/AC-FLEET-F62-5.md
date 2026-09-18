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
- **UNCOUNTED ENV-READINESS PREFLIGHT — gates the counted round-4 run.** For each of the
  **five coworker profiles (excluding the launch `default`** — a sixth `default` container
  would break the five-container criterion), run one wrapper-mediated sandbox spawn with
  `TERMINAL_CWD=/root` (§ Common substrate — so the auto-cwd bind does not add the tester
  checkout) and inspect the FINAL podman mount list to prove the substrate is SERVER-VISIBLE
  for the ACTUAL rendered mount set: for EVERY bind SOURCE at every bind site (auto-cwd
  `docker.py:1003`, credential, skills, CA, `docker_extra_args`, and the persistent
  `$HERMES_HOME` binds `docker.py:1016-1030`) assert the source RESOLVES server-side (NO
  `statfs`/exit-125 in the podman log) and the spawn is rootless (`cat /proc/1/uid_map` ==
  `0 1001 1`). **This is an ENV-READINESS gate ONLY — it makes NO sandbox-policy judgment.**
  A failure here — a bind source not server-visible (statfs/125), a missing socket/image, a
  permission error — is a `FAIL(env)` surfaced BEFORE the counted rows, never a counted
  defect. The mount-POLICY allowlist (cross-profile exclusion, per-mount rendered mode,
  `--memory`/`--pids-limit`) is asserted SOLELY in the COUNTED step 3, so a real isolation
  regression — e.g. another profile's `$HERMES_HOME` mounted in, which RESOLVES on the shared
  fs and thus PASSES this env preflight — is caught there as a COUNTED product defect, never
  mislabeled `FAIL(env)`.
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
   `docker_volumes` ∪ an EXPLICIT allowlist of Hermes-managed mounts (sandbox home/workspace,
   cache, skills, credential, CA), asserting: (a) every RENDERED `docker_volumes` source
   resolves OUTSIDE every profile home; (b) every Hermes-managed `$HERMES_HOME` source belongs
   to the ACTIVE profile ONLY — a source under ANOTHER profile's home is a FAIL (cross-profile
   exclusion; such a bind RESOLVES on the shared fs and so passes the env-readiness preflight,
   which is exactly why it is caught HERE); (c) NO source or dest outside that allowlist;
   (d) each mount carries its RENDERED mode, read from the `podman inspect` `RW` flag (no mode
   drift, NOT a blanket `ro`) — among the Hermes-managed mounts credential/skills/cache/CA `ro`
   and the dedicated sandbox home/workspace `rw`, and each rendered `docker_volumes` entry at
   its declared mode: the MEM-F43 `/opt/hermes-fleet/shared-learnings` mount `rw` for the
   orchestrator (rendered without a suffix ⇒ `RW:true`) / `ro` for the four workers (§ Design 6),
   the per-profile `/data/hermes-fleet/<p>/workspace` `rw`; (e) `--memory` + `--pids-limit`
   present. **A violation of (a)–(e) is a COUNTED product defect — the mount-policy allowlist
   lives here, never in the env-readiness preflight.** (This replaces the earlier blanket
   "NO source under `$HERMES_HOME`", unsatisfiable against stock Hermes which always emits
   `$HERMES_HOME` sandbox-home/cache binds, with a substantive allowlist: the rendered volumes
   stay outside every home and the Hermes-managed `$HERMES_HOME` binds are confined to the
   active profile, so a cross-profile home leak is still caught — a strengthening, NOT a
   weakening.)

## Pass

Every coworker's sandbox is rootless (`0 1001 1`), uniquely labelled, and its mount set
matches the allowlist — the rendered `docker_volumes` outside every home, each at its
declared mode (the MEM-F43 shared-learnings mount `rw` for the orchestrator / `ro` for the
four workers per § Design 6), plus only the ACTIVE profile's Hermes-managed `$HERMES_HOME`
binds (`ro` except the dedicated sandbox home/workspace) — so no cross-profile home leak
and no mode drift. The `message_agent` delivery runner (host-local by design) is exempt and
named as such.

## Evidence

`scenario-AC-FLEET-F62-5/sandbox.log` + `mounts.json`; plus a `python3 -c` read of
each spawn's `state.db` where a fact is a row not a pixel (no `sqlite3` CLI in the
image).
