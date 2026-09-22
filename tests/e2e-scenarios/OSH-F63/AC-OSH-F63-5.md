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

On the box, through the operator-provisioned name-prefixed OpenShell access path, one
coworker's sandbox is created from that profile's rendered `openshell policy`; a
`hermes -p <profile>` terminal call configured with that profile's rendered ssh block
executes inside it (not on the gateway host); an off-policy egress is denied and logged;
the worker-side mounted identity is DENIED sibling access (the hard isolation gate); and
the sandbox is torn down. `kind: sandbox`, `model: stub` — the LLM is stubbed (the
terminal call is deterministic) and the real dependency is the OpenShell box, not live
inference, so no OneCLI budget is spent (`kind: live` + `model: stub` is rejected by the
harness; this mirrors the merged FLEET-F62 on-box scenarios).

**Concrete names used below** (so step 1's sandbox is the exact host step 2's ssh block
targets): the probe is the **builder** profile's own sandbox, named by its rendered
`terminal.ssh_host` = `osh-f63-builder`; the sibling target for the isolation gate
(step 4) is a *different* fleet sandbox, `osh-f63-tester`. Any coworker profile works;
builder is chosen because its fixture also carries the stdio-mcp scope.

## Common substrate (the OpenShell access path, operator-provisioned)

- A name-prefixed OpenShell access path reachable from the tester container — a scoped
  way to run `openshell sandbox create|exec|delete|ssh-config`, `openshell policy
  set|get`, and `openshell logs` against the `nemoclaw` gateway, restricted to
  `osh-f63-*` sandboxes (operator step 1).
- The gateway admin credentials in `/home/ubuntu/.config/openshell/` are admin over ALL
  sandboxes and **must never** be mounted into the tester container, a worker sandbox,
  `osh-f63-gw`, or `brev-hermes`.
- The pinned `--from` image (operator step 3): a community image (route (a): nothing
  extra; route (b): ships `sshd`). The exact `--from` value is recorded in the fixtures'
  spec (`egress.sandbox_image`); the operator replaces it with the immutable digest.

## Setup (after the fixtures are installed)

- Install the openshell profile fixtures (frontmatter `fixtures:` — the tester installs
  them; this section does not re-install one).
- The rendered `openshell policy` files are in each distribution's `distribution_owned`,
  so after install they sit at the profile's installed home. Use these exact paths as the
  `--policy` arguments (the profile NAME, not the fixture dir name): builder →
  `$HERMES_HOME/profiles/builder/policy-builder.yaml`, tester →
  `$HERMES_HOME/profiles/tester/policy-tester.yaml` (the AC-OSH-F63-2 output).
- Confirm the access path answers `openshell sandbox list` for `osh-f63-*`.
- Establish, from operator step 2, which `openshell logs --source` (gateway|sandbox)
  records a policy denial, and whether `openshell policy` governs the sandbox's own mTLS
  control connection to `OPENSHELL_ENDPOINT` (this decides route (a) admissibility).
- Install the builder's `openshell sandbox ssh-config osh-f63-builder` output into the
  gateway user's OpenSSH config so `terminal.ssh_host: osh-f63-builder` resolves through
  the OpenShell `ProxyCommand`, and provision the key at the builder's `terminal.ssh_key`
  path.

## Steps

1. Create BOTH the probe and the sibling target up front, so step 4's outcome is an
   authorization decision and never a not-found: `openshell sandbox create --name
   osh-f63-builder --from <pinned image> --policy
   $HERMES_HOME/profiles/builder/policy-builder.yaml` and `openshell sandbox create
   --name osh-f63-tester --from <pinned image> --policy
   $HERMES_HOME/profiles/tester/policy-tester.yaml` → expect: both sandboxes reach Ready.
2. A `hermes -p builder` `terminal` call (the builder profile's rendered ssh block,
   `ssh_host: osh-f63-builder`) runs `hostname; id -u` inside `osh-f63-builder` →
   expect: both values differ from the gateway host's (proves the call executed in the
   sandbox, not on the gateway).
3. From inside `osh-f63-builder`, `curl` a host NOT in the policy → expect: denied
   (connection refused/blocked), and the denial appears in `openshell logs`
   (`--source gateway` or `--source sandbox`, whichever records it per operator step 2).
4. **Worker-side sibling-authz negative control (HARD isolation gate, operator step 2):**
   from inside `osh-f63-builder`, using ONLY its mounted `OPENSHELL_TLS_*` identity (no
   operator credential, no pre-installed CLI), attempt `sandbox list` and a
   `connect`/`ssh-proxy` against `OPENSHELL_ENDPOINT` targeting the **already-Ready**
   `osh-f63-tester` → **expect: an AUTHORIZATION denial — nonzero exit, no sibling
   metadata returned.** Because `osh-f63-tester` exists and is reachable to the operator,
   a denial here is a policy/authz refusal, NOT a not-found. **This is a hard gate, not a
   route-selector: if the worker CAN list/connect to the sibling, AC-OSH-F63-5 FAILS** —
   profile isolation (topology rule 3, a MUST) is broken and no documentation change makes
   it pass. The row cannot reach PASS until route (b) (`sshd`-in-image + host `openshell
   forward`) or a genuinely `osh-f63-*`-scoped OpenShell identity is *implemented* and the
   WHOLE scenario reruns green with the worker denied.
5. `openshell sandbox delete osh-f63-builder` and `openshell sandbox delete
   osh-f63-tester` → expect: both teardowns succeed; `sandbox list` shows neither.

## Pass

The criterion holds ONLY when step 2 shows in-sandbox execution, step 3 shows the
off-policy egress denied and logged, **step 4 shows a policy-constrained worker DENIED
sibling access (nonzero exit, no sibling metadata)**, and step 5 tears both sandboxes
down cleanly. A successful worker-side sibling access in step 4 is an unconditional FAIL.

## Evidence

`scenario-AC-OSH-F63-5/evidence.txt` (the five step transcripts, incl. the step-4
sibling-authz attempt + its allow/deny outcome) + `scenario-AC-OSH-F63-5/openshell.log`
(the egress-denial line, and whichever `--source` records it). Where a fact is a value
rather than a pixel, capture it as command stdout in `evidence.txt` (the coworker image
has no `sqlite3` CLI; this scenario needs none).

## Carry (infra-only)

The ONLY carry condition is the access path being **unprovisioned** (operator step 1 not
done) when the hermetic floor (AC-OSH-F63-1..4, AC-OSH-F63-6 + suite + the negative
control) is green — an infrastructure gap, not a test outcome. A sandbox that launches
but whose worker reaches a sibling (step 4) is an **isolation FAILURE**, never a carry.
