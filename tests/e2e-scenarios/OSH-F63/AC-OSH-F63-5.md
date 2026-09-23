---
ac: AC-OSH-F63-5
kind: sandbox
model: stub
fixtures:
  - fixtures/openshell/osh-f63-default
  - fixtures/openshell/osh-f63-orchestrator
  - fixtures/openshell/osh-f63-architect
  - fixtures/openshell/osh-f63-builder
  - fixtures/openshell/osh-f63-tester
  - fixtures/openshell/osh-f63-reviewer
timeout_s: 600
---

# AC-OSH-F63-5 — per-profile OpenShell ssh sandbox, one per coworker (sandbox)

On the box, through the operator-provisioned name-scoped OpenShell broker (`osh-f63-*`),
one coworker's sandbox is created from that profile's rendered `openshell policy`; a
`hermes -p builder` terminal call configured with that profile's rendered ssh block
executes inside it (not on the gateway host); an off-policy egress is denied and logged;
the worker-side mounted identity is DENIED sibling access (the hard isolation gate); and
both sandboxes are torn down. `kind: sandbox`, `model: stub` — the LLM is stubbed (the
terminal call is deterministic) and the real dependency is the OpenShell box, not live
inference, so no OneCLI budget is spent (`kind: live` + `model: stub` is rejected by the
harness; this mirrors the merged FLEET-F62 on-box scenarios).

**Concrete names (`<prefix>` = `osh-f63`)** so step 1's sandbox is the exact host step 2's
ssh block targets: the probe is the **builder** profile's own sandbox, named by its
rendered `terminal.ssh_host` = `osh-f63-builder`; the sibling target for the isolation
gate (step 4) is a *different* fleet sandbox, `osh-f63-tester`. Builder is chosen because
its fixture also carries the stdio-mcp scope.

## ⛔ HOLD — do not run on-box until the operator confirms LANE READY (relayed)

The operator is provisioning the broker lane: as of 2026-09-23 it is **built and
unit-tested but NOT yet mounted** into the tester container. Until the operator posts
**LANE READY**, do NOT run this scenario on-box and do NOT spend a verification round on
it — the hermetic floor (AC-OSH-F63-1..4, AC-OSH-F63-6 + suite + doctor on both plugins +
`plugins list`) proceeds independently. The LANE READY confirmation lands on the
operator↔orchestrator edge and is relayed **down** (orchestrator → architect → builder →
tester); the tester never observes it directly and must not self-trigger off a thread
post. An unprovisioned lane is §Gating & carry outcome (c): OSH-F63 stays BLOCKED on that
infra, not a FAIL.

## Common substrate (the OpenShell broker lane, operator-provisioned)

- A name-scoped OpenShell **broker** reachable from the tester container: a unix socket
  `/workspace/extra/osh-broker/osh.sock` fronted by the `openshell` **shim on PATH at
  `/workspace/extra/osh-bin/openshell`**, restricted to `osh-f63-*` sandbox names, with
  `--policy` files under the testbed root `/workspace/extra/hermes-fleet-testbed/` and a
  pinned `--from` image — the scoped way to run `openshell sandbox
  create|exec|delete|ssh-config`, `openshell policy set|prove`, and `openshell logs`.
- The gateway admin credentials are admin over ALL sandboxes and **must never** be
  mounted into the tester container, a worker sandbox, `osh-f63-gw`, or `brev-hermes`.
  Auth rides the mTLS proxy, not a mounted operator credential.
- The pinned `--from` image (operator step 3) **must be Debian trixie / Ubuntu 24.04 based
  (glibc ≥ 2.39)**: OpenShell injects PID 1 `/opt/openshell/bin/openshell-sandbox`, which
  needs glibc ≥ 2.39 — an alpine (musl) or Debian bookworm (glibc 2.36) base restart-loops
  and the sandbox never reaches Ready. A community image on a trixie/24.04 base (route (a):
  nothing extra; route (b): ships `sshd`) serves until OSH-F64 ships the worker Dockerfile.
  The exact `--from` value is recorded in the fixtures' spec (`egress.sandbox_image`) and
  the glibc/OS constraint in `fixtures/openshell/IMAGE-CONSTRAINT.md`; the operator replaces
  the value with the immutable digest at LANE READY, keeping it trixie/24.04.

## Setup (after the fixtures are installed)

- Install the openshell profile fixtures (frontmatter `fixtures:` — the tester installs
  them; this section does not re-install one).
- **Install the managed fragment** so the veto can read its per-profile `expected_ssh_host`
  anchor (without it the fail-closed host-binding predicate refuses every ssh terminal
  call): `export HERMES_MANAGED_DIR="${HERMES_MANAGED_DIR:-$HERMES_HOME/managed}"; mkdir -p
  "$HERMES_MANAGED_DIR"; cp "$SCN/fixtures/openshell/managed/config.yaml"
  "$HERMES_MANAGED_DIR/config.yaml"` (`$SCN` = this scenario dir), then invalidate the
  managed cache. See the [fleet deployment runbook](../../../website/docs/user-guide/fleet-deployment.md) §2.
- The rendered `openshell policy` files ship in each distribution's `distribution_owned`,
  so after install they sit at the profile's installed home. Use these exact paths as the
  `--policy` arguments: builder → `$HERMES_HOME/profiles/builder/policy-builder.yaml`,
  tester → `$HERMES_HOME/profiles/tester/policy-tester.yaml` (the AC-OSH-F63-2 output).
- Confirm the broker lane answers `openshell sandbox list` for `osh-f63-*`.
- Establish, from operator step 2, which `openshell logs --source` (gateway|sandbox)
  records a policy denial, and whether `openshell policy` governs the sandbox's own mTLS
  control connection to `OPENSHELL_ENDPOINT` (this decides route (a) admissibility).

## Steps

1. **Grammar-conformance + policy install/prove, then create the probe AND the sibling**
   (so step 4's outcome is an authorization decision, never a not-found):
   `openshell policy set policy-builder.yaml` then `openshell policy prove` against the
   *rendered* builder policy → expect: the pinned OpenShell CLI **accepts** the emitted
   file and `prove` reports the three allows admitted and `172.17.0.1:10256` + an
   off-policy host denied. Then create both sandboxes:
   `openshell sandbox create --name osh-f63-builder --from <pinned image> --policy
   policy-builder.yaml` — **the probe, whose name IS the builder profile's rendered
   `terminal.ssh_host`** — AND `openshell policy set policy-tester.yaml` +
   `openshell sandbox create --name osh-f63-tester --from <pinned image> --policy
   policy-tester.yaml` — **the sibling** → expect: both reach Ready under their proven
   policies.
2. **Install the RENDERED ssh-config, then reach the probe through it:**
   `openshell sandbox ssh-config osh-f63-builder` → the builder profile's `~/.ssh/config`
   entry for its rendered `terminal.ssh_host` (`osh-f63-builder`), and
   `ssh -G osh-f63-builder` → expect: it resolves `user sandbox` and a `proxycommand`
   invoking the shim `openshell` on PATH (`/workspace/extra/osh-bin/openshell`), and **no
   `identityfile`** (auth rides the mTLS proxy — the rendered `terminal.ssh_key` need only
   EXIST, no key material, and is not referenced by the ssh-config). Then a `hermes -p
   builder` `terminal` call configured with that same rendered ssh block runs
   `hostname; id -u` inside `osh-f63-builder` → expect: both values differ from the
   gateway host's (proves the call executed in the sandbox via the rendered config +
   ProxyCommand, not on the gateway or via ad-hoc settings).
3. From inside `osh-f63-builder`, `curl` a host NOT in the policy → expect: denied
   (connection refused/blocked), and the denial appears in `openshell logs`
   (`--source gateway` or `--source sandbox`, whichever records it per operator step 2).
4. **Worker-side sibling-authz negative control (HARD isolation gate, operator step 2):**
   first, an operator-side `openshell sandbox list` records `osh-f63-tester` **Ready**
   immediately before the worker attempt (so a denial is unambiguously authz, NOT
   not-found). Then from inside `osh-f63-builder`, using ONLY its mounted `OPENSHELL_TLS_*`
   identity (no operator credential, no pre-installed CLI), attempt `sandbox list` and a
   `connect`/`ssh-proxy` to **`osh-f63-tester`** against `OPENSHELL_ENDPOINT` (curl the
   gateway API with the mTLS triple, or an uploaded CLI) → **expect: an AUTHORIZATION
   denial — nonzero exit, no `osh-f63-tester` metadata returned.** **This is a hard gate,
   not a route-selector: if the worker CAN list/connect to the sibling, AC-OSH-F63-5
   FAILS** — profile isolation (topology rule 3, a MUST) is broken and no documentation
   change makes it pass. The row cannot reach PASS until route (b) (`sshd`-in-image + host
   `openshell forward`) or a genuinely `osh-f63-*`-scoped OpenShell identity is
   *implemented* and the WHOLE scenario reruns green with the worker denied.
5. `openshell sandbox delete osh-f63-builder` AND `openshell sandbox delete osh-f63-tester`
   → expect: both teardowns succeed; `sandbox list` shows neither.

## Pass

The criterion holds ONLY when step 1 shows the rendered policy accepted + proven by the
real OpenShell CLI and BOTH `osh-f63-builder` (probe) and `osh-f63-tester` (sibling)
Ready, step 2 shows the rendered ssh-config resolving the probe and in-sandbox execution
through it, step 3 shows the off-policy egress denied and logged, **step 4 shows the
policy-constrained worker in `osh-f63-builder` DENIED access to the Ready sibling
`osh-f63-tester` (nonzero exit, no sibling metadata)**, and step 5 tears both sandboxes
down cleanly. A successful worker-side sibling access in step 4 is an unconditional FAIL.

## Evidence

`scenario-AC-OSH-F63-5/evidence.txt` (the step transcripts, incl. the step-4 sibling-authz
attempt + its allow/deny outcome and the pre-attempt `sandbox list` showing `osh-f63-tester`
Ready) + `scenario-AC-OSH-F63-5/openshell.log` (the egress-denial line, and whichever
`--source` records it). Where a fact is a value rather than a pixel, capture it as command
stdout in `evidence.txt` (the coworker image has no `sqlite3` CLI; this scenario needs none).

## Carry (infra-only)

The ONLY carry condition is the broker lane being **unprovisioned** (operator step 1 not
done) when the hermetic floor (AC-OSH-F63-1..4, AC-OSH-F63-6 + suite + the negative
control) is green — an infrastructure gap, not a test outcome (§Gating & carry outcome
(c)). A sandbox that launches but whose worker reaches a sibling (step 4) is an isolation
FAILURE, never a carry.
