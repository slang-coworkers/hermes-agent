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
executes inside it (not on the gateway host); an in-policy POST is allowed and an
off-policy egress denied + logged; the worker-side mounted identity is DENIED sibling
access (the hard isolation gate); and both sandboxes are torn down. `kind: sandbox`,
`model: stub` — the LLM is stubbed (the terminal call is deterministic) and the real
dependency is the OpenShell box, not live inference, so no OneCLI budget is spent
(`kind: live` + `model: stub` is rejected by the harness; this mirrors the merged
FLEET-F62 on-box scenarios).

**Concrete names (`<prefix>` = `osh-f63`)** so step 1's sandbox is the exact host step 2's
ssh block targets: the probe is the **builder** profile's own sandbox, created with the
bare `--name osh-f63-builder`, which the builder profile's rendered `terminal.ssh_host`
ssh-config alias `openshell-osh-f63-builder` resolves to; the sibling target for the
isolation gate (step 4) is a *different* fleet sandbox, `osh-f63-tester`. Builder is chosen
because its fixture also carries the stdio-mcp scope.

## ⛔ HOLD — do not run on-box until the relayed LANE-READY go

The broker lane is provisioned and host-side-validated (orchestrator msg 52), but AC-5
remains **HELD**. The tester runs it only when ALL of: the fresh hermetic floor
(AC-OSH-F63-1..4, AC-OSH-F63-6 + suite + doctor on both plugins + `plugins list`) is
green at the new head; the `openshell` shim is present on the tester's PATH at
`/workspace/extra/osh-broker-bin/openshell`; an `ssh` client (`openssh-client`) is
present in the tester; and the Orchestrator relays the go. The LANE-READY signal reaches
the tester by relay (operator↔orchestrator → architect → builder → tester), never observed
by the tester directly — do NOT self-trigger off a thread post. An unprovisioned/not-yet-
mounted lane is §Gating & carry outcome (c): OSH-F63 stays BLOCKED on that infra, not a FAIL.

## Common substrate (the OpenShell broker lane, operator-provisioned)

- A name-scoped OpenShell **broker** reachable from the tester container: a unix socket
  `/workspace/extra/osh-broker/osh.sock` (rw) fronted by the `openshell` **shim on PATH at
  `/workspace/extra/osh-broker-bin/openshell` (ro)** — the scoped way to run `openshell
  sandbox create|exec|delete|ssh-config`, `openshell policy set|prove`, and `openshell
  logs`. (An earlier probe of `/workspace/extra/osh-bin/openshell` was the stale path; use
  `/workspace/extra/osh-broker-bin/openshell` only.)
- **Broker scope (enforced by the broker, not just convention):** sandbox names must match
  `osh-f63-*`; `--policy` files must sit under the testbed root
  `/workspace/extra/hermes-fleet-testbed/`; `--from` must be exactly `osh-f63-base:trixie`;
  at most **8** sandboxes; the flags `--gateway` / `--upload` / `--forward` are refused.
- The gateway admin credentials are admin over ALL sandboxes and **must never** be mounted
  into the tester container, a worker sandbox, `osh-f63-gw`, or `brev-hermes`. Auth rides
  the mTLS proxy, not a mounted operator credential.
- The pinned `--from` image is **`osh-f63-base:trixie`** (id `06f0220e7810`, Debian trixie,
  glibc 2.41). OpenShell injects PID 1 `/opt/openshell/bin/openshell-sandbox`, which needs
  glibc ≥ 2.39; an alpine (musl) or Debian bookworm (glibc 2.36) base restart-loops and the
  sandbox never reaches Ready. The broker refuses any other `--from`.

## Setup (after the fixtures are installed)

- Install the openshell profile fixtures (frontmatter `fixtures:` — the tester installs
  them; this section does not re-install one).
- **Install the managed fragment** so the veto can read its per-profile `expected_ssh_host`
  anchor (without it the fail-closed host-binding predicate refuses every ssh terminal
  call): `export HERMES_MANAGED_DIR="${HERMES_MANAGED_DIR:-$HERMES_HOME/managed}"; mkdir -p
  "$HERMES_MANAGED_DIR"; cp "$SCN/fixtures/openshell/managed/config.yaml"
  "$HERMES_MANAGED_DIR/config.yaml"` (`$SCN` = this scenario dir), then invalidate the
  managed cache.
- **Materialize the ssh_key placeholders.** The rendered configs reference
  `terminal.ssh_key` at `/workspace/extra/hermes-fleet-testbed/osh-f63/openshell/keys/<profile>`;
  the render creates them as empty 0600 files on the gateway, but the installed fixtures do
  not carry external key files. Create each referenced key as an empty 0600 placeholder
  after install (the tester container has the testbed mounted rw): for each installed
  profile, `install -D -m 600 /dev/null "$(python3 -c 'import sys,yaml;print(yaml.safe_load(open(sys.argv[1]))["terminal"]["ssh_key"])' "$HERMES_HOME/profiles/<profile>/config.yaml")"`
  (`-D` creates the parent `openshell/keys/` dir, which the installed fixtures do not carry).
  The ssh-config auth rides the mTLS proxy, so the key need only EXIST (no key material).
- The rendered `openshell policy` files ship in each distribution's `distribution_owned`,
  so after install they sit at the profile's installed home. Use these exact paths as the
  `--policy` arguments: builder → `$HERMES_HOME/profiles/builder/policy-builder.yaml`,
  tester → `$HERMES_HOME/profiles/tester/policy-tester.yaml` (the AC-OSH-F63-2 output).
- Confirm the broker lane answers `openshell sandbox list` for `osh-f63-*`.
- Confirm the `openshell` shim is on PATH at `/workspace/extra/osh-broker-bin/openshell` and
  an `ssh` client is present; if either is absent the lane is not yet runnable (outcome c).

## Steps

1. **Grammar-conformance + policy install/prove, then create the probe AND the sibling**
   (so step 4's outcome is an authorization decision, never a not-found):
   `openshell policy set policy-builder.yaml` then `openshell policy prove` against the
   *rendered* builder policy → expect: the pinned OpenShell CLI **accepts** the emitted
   file (proving the real binary parses the render's structure and enforces its
   allows/denies — neither provable by AC-2's hermetic structural check), and `prove`
   reports the **two** allows admitted (OneCLI hop `172.17.0.1:10255` + inference route
   `inference-api.nvidia.com:443`) and `172.17.0.1:10256`, `host.openshell.internal:8080`
   + an off-policy host all denied. Then create BOTH sandboxes:
   `openshell sandbox create --name osh-f63-builder --from osh-f63-base:trixie --policy
   policy-builder.yaml` — **the probe, whose bare `--name` the builder profile's rendered
   `terminal.ssh_host` (`openshell-osh-f63-builder`) resolves to** — AND
   `openshell policy set policy-tester.yaml` + `openshell sandbox create --name
   osh-f63-tester --from osh-f63-base:trixie --policy policy-tester.yaml` — **the sibling**
   → expect: both reach Ready under their proven policies. **First-pull timeout:** the
   first `openshell sandbox create` can exceed the 2-min tool-call timeout while it pulls
   `osh-f63-base:trixie` — run each `create` in the background (or with a ≥5-min bounded
   wait) and poll `openshell sandbox list` for Ready, so a slow first pull is not scored as
   a create failure.
2. **Install the RENDERED ssh-config, then reach the probe through it:**
   `openshell sandbox ssh-config osh-f63-builder` → the builder profile's `~/.ssh/config`
   entry whose `Host` alias is the builder's rendered `terminal.ssh_host`
   (`openshell-osh-f63-builder`), and `ssh -G openshell-osh-f63-builder` → expect: it
   resolves `user sandbox` and a `proxycommand` invoking the shim `openshell` on PATH
   (`/workspace/extra/osh-broker-bin/openshell`). **The "no `IdentityFile`" check is against
   the GENERATED ssh-config stanza, NOT `ssh -G`** — `ssh -G` always prints OpenSSH's
   built-in default `identityfile ~/.ssh/id_*` lines even when the stanza sets none, so it
   would false-fail; assert instead that the `openshell sandbox ssh-config` output contains
   no `IdentityFile` line (auth rides the mTLS proxy — the rendered `terminal.ssh_key` need
   only EXIST, no key material). Then a `hermes -p builder` `terminal` call configured with
   that same rendered ssh block runs `hostname; id -un` inside `osh-f63-builder` → expect:
   the `hostname` differs from the gateway host's AND `id -un` == `sandbox` (proves the call
   executed in the sandbox as the sandbox user via the rendered config + ProxyCommand, not
   on the gateway or via ad-hoc settings). **Numeric UIDs are NOT compared** — they are
   namespace-local and both may legitimately be `1000`; the discriminators are the hostname
   and the `sandbox` username.
3. From inside `osh-f63-builder`, on the record via `openshell logs --source sandbox`:
   **(a) an in-policy POST** to the OneCLI hop `172.17.0.1:10255` (the agent's real traffic
   shape) → expect: allowed, logging `NET:OPEN ALLOWED` + `HTTP:POST ALLOWED`. This is the
   on-box proof that the rendered policy admits the coworker's actual POST traffic, not just
   GET — a GET-only rule would deny this and FAIL the step, which is why AC-2 does not
   hard-code the verb and AC-5 proves it. **(b) a `curl` to a host NOT in the policy** →
   expect: denied, logging an OCSF `NET:OPEN … DENIED … [reason:…]` line. Capturing both
   puts the allow (incl. POST) and the deny on the record.
4. **Worker-side sibling-authz negative control (HARD isolation gate):** first, an
   operator-side `openshell sandbox list` records `osh-f63-tester` **Ready** immediately
   before the worker attempt (so a denial is unambiguously authz, NOT not-found). Then from
   inside `osh-f63-builder`, using ONLY its mounted `OPENSHELL_TLS_*` identity (no operator
   credential, no pre-installed CLI), attempt `sandbox list` and a sibling `connect`/
   `ssh-proxy` to **`osh-f63-tester`** against the gateway control plane
   **`host.openshell.internal:8080`** (curl the gateway API with the mTLS triple, or an
   uploaded CLI) → **expect: denied, nonzero exit, no `osh-f63-tester` metadata returned**
   under the rendered restrictive policy (which never allows `host.openshell.internal:8080`),
   and the denial logged via `openshell logs --source sandbox` as an OCSF `NET:OPEN … DENIED`
   line for `host.openshell.internal:8080`. **This is a hard gate, not a route-selector: if
   the worker CAN list/connect to the sibling, AC-OSH-F63-5 FAILS** — profile isolation
   (topology rule 3, a MUST) is broken and no documentation change makes it pass. The row
   cannot reach PASS until route (b) (`sshd`-in-image + host `openshell forward`) or a
   genuinely `osh-f63-*`-scoped OpenShell identity is *implemented* and the WHOLE scenario
   reruns green with the worker denied.
5. `openshell sandbox delete osh-f63-builder` AND `openshell sandbox delete osh-f63-tester`
   → expect: both teardowns succeed; `sandbox list` shows neither.

## Pass

The criterion holds ONLY when step 1 shows the rendered policy accepted + proven by the
real OpenShell CLI and BOTH `osh-f63-builder` (probe) and `osh-f63-tester` (sibling) Ready,
step 2 shows the rendered ssh-config resolving the probe and in-sandbox execution through
it (`hostname` off-gateway, `id -un` == `sandbox`), step 3 shows the **in-policy POST
allowed** (`HTTP:POST ALLOWED` — the coworker's real traffic admitted) and the off-policy
egress denied and logged, **step 4 shows the policy-constrained worker in `osh-f63-builder`
DENIED access to the Ready sibling `osh-f63-tester` (nonzero exit, no sibling metadata)**,
and step 5 tears BOTH sandboxes down cleanly. A successful worker-side sibling access in
step 4 is an unconditional FAIL.

## Evidence

`scenario-AC-OSH-F63-5/evidence.txt` (the five step transcripts, incl. the step-4
sibling-authz attempt + its allow/deny outcome and the pre-attempt `sandbox list` showing
`osh-f63-tester` Ready) + `scenario-AC-OSH-F63-5/openshell.log` (the OCSF lines from
`openshell logs --source sandbox`: the step-3 off-policy `NET:OPEN … DENIED`, the step-4
`host.openshell.internal:8080` `NET:OPEN … DENIED`, and the step-3 in-policy
`NET:OPEN ALLOWED` + `HTTP:POST ALLOWED`). Where a fact is a value rather than a pixel,
capture it as command stdout in `evidence.txt` (the coworker image has no `sqlite3` CLI;
this scenario needs none).

## Carry (infra-only)

The ONLY carry condition is the broker lane being **unprovisioned or not yet mounted**
(the `openshell` shim / `ssh` client absent tester-side) when the hermetic floor is green —
an infrastructure gap, not a test outcome (§Gating & carry outcome (c)). A sandbox that
launches but whose worker reaches a sibling (step 4), or a policy the CLI rejects/does not
enforce (step 1), is a FAILURE, never a carry.
