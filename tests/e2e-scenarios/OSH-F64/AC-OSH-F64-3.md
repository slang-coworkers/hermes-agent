---
ac: AC-OSH-F64-3
kind: live
model: live
base_url: inference-api.nvidia.com
api_mode: anthropic_messages
model_id: aws/anthropic/bedrock-claude-opus-4-8
key_env: ANTHROPIC_API_KEY
fixtures:
  - fixtures/osh-f64-gateway
  - fixtures/osh-f64-worker-orchestrator
  - fixtures/osh-f64-worker-architect
  - fixtures/osh-f64-worker-builder
  - fixtures/osh-f64-worker-tester
  - fixtures/osh-f64-worker-reviewer
LIVE_MODEL_CALLS_MAX: 40
LIVE_BUDGET_USD: 5
timeout_s: 600
---

# AC-OSH-F64-3 — throwaway gateway boots the multiplex fleet; per-profile terminal in its own worker sandbox (live)

Through the operator-provisioned, name-prefixed OpenShell access path (`osh-f64-*`), a
throwaway `osh-f64-gw` gateway sandbox boots ONE multiplexed gateway serving `default` +
≥2 coworker profiles; each served coworker's terminal call executes in ITS own worker
sandbox; an off-policy egress is denied and logged; teardown deletes every `osh-f64-*`.
`<prefix>` = `osh-f64`. Route (a) (gateway-brokered ssh via `ProxyCommand`, no sshd, no
key material) is assumed; if OSH-F63's on-box AC-5 recorded route (b), the worker image
and this scenario follow OSH-F63's recorded route.

**Process boundary (composed from FLEET-F62, not re-solved).** Terminal config is
process-global and frozen on first use in one process, so a served (secondary) profile's
terminal call cannot ride the shared gateway process — the composed cross-profile veto
blocks it (OSH-F63 §D2, backstopping FLEET-F62). Per-profile terminal isolation therefore
comes from FLEET-F62's per-profile-scoped subprocess env (`hermes_cli/web_server.py`):
each terminal-bearing turn runs in THAT profile's own bridged process (its profile-scoped
dashboard chat child / `hermes -p <profile> chat`), where `TERMINAL_*` resolves to its own
ssh worker sandbox. This scenario drives terminal turns through that per-profile child,
not a shared-process turn.

## Setup
This scenario provisions and tears down its OWN throwaway fleet (it does not depend on any
other scenario having left one standing). Confirm the broker lane answers `openshell
sandbox list` for `osh-f64-*`; `openshell sandbox create --name osh-f64-gw --from
osh-f64-gateway:pinned --policy /workspace/extra/hermes-fleet-testbed/osh-f64/policy-gateway.yaml`
(the pinned GATEWAY image, §D1 — built from base `nemoclaw-hermes-sandbox:local` + fork core
@95de5f78, its lane APF bound inline; the worker sandboxes below create
`--from localhost/hermes-openshell-sandbox:pinned`); a live OneCLI agent per served COWORKER
profile exists — 5 (orchestrator/architect/builder/tester/reviewer); the gateway/`default`
profile issues no live turn so it needs no dedicated OneCLI agent (operator prerequisite). No
fixture is re-installed here — the tester has already installed the `fixtures:` list.

**Grant preflight (§D7.1):** confirm each served profile's `HERMES_HOME` basename matches a
OneCLI identity GRANTED the inference secret. The openshell render populates the default/gateway
`profile_secret_sets[<role>]` = `["Anthropic-Dev"]` (from `egress.onecli_secret_ids`), which the
deploy's `onecli-onboard` reads to grant each identity. Do NOT run `onecli-onboard` against a
`profile_secret_sets` entry left `[]` — `set_secrets([])` REVOKES that identity's grant → the
served turn 401s. If (re)onboarding is needed, run `hermes -p default onecli-onboard --profile
<role>` from the gateway root (a bare `hermes onecli-onboard --profile <role>` is stripped by
hermes-main → wrong context + missing required flag). With the grant present the live turn
expects a 200; a 401 is a fault to diagnose (grant / identity match / token-bearing route / CA),
not a tolerated state.

Then, BEFORE the installer, in this ORDER — steps 1, 2a, and 3 run INSIDE `osh-f64-gw`; the render +
host-mirror in 2b–2c are HOST-lane commands (`openshell sandbox exec`, host `mkdir`, host redirection)
that reach into the sandbox:
1. Seed the managed dir with the §D6 scan guard: `export HERMES_MANAGED_DIR=/sandbox/.hermes/managed
   && mkdir -p $HERMES_MANAGED_DIR && cp /tmp/osh-lane/managed/config.yaml $HERMES_MANAGED_DIR/` (the
   pre-seeded managed config carries `plugins.scan_on_install: false`, the actual unblock for Phase
   A's first offline install; the installer also writes it by construction before Phase A).
2. Make the per-worker policies HOST-readable for the broker (LANE v4, §D3 `--policy-root`):
   (a) run the staged `installer.py phase-a --spec <spec> --ref <sha>` so `hermes coworker` exists;
   (b) render the policies gateway-internally WITHOUT provisioning —
   `openshell sandbox exec osh-f64-gw -- sh -lc 'hermes coworker compose <spec> --out "$HERMES_HOME/.osh-f64/render"'`;
   (c) mirror each rendered policy to the host testbed root via a QUOTED remote shell (so `$HERMES_HOME`
   expands INSIDE the sandbox, not on the host) — `mkdir -p /workspace/extra/hermes-fleet-testbed/osh-f64/render/<role>`
   then `openshell sandbox exec osh-f64-gw -- sh -lc 'cat "$HERMES_HOME/.osh-f64/render/<role>/policy-<role>.yaml"' > /workspace/extra/hermes-fleet-testbed/osh-f64/render/<role>/policy-<role>.yaml`
   for each of the five roles (orchestrator, architect, builder, tester, reviewer) the installer provisions.
3. Bring the gateway up for Phase B: `export HERMES_DASHBOARD_SESSION_TOKEN=<token>` BEFORE starting
   `hermes serve` (web_server.py:589 — a bare `hermes serve` surfaces no token and the WS `/api/ws`
   upgrade still validates `?token` despite `auth_required:false`), start the sandbox's `hermes serve`
   messaging gateway, wait for readiness, and capture
   `$GW_URL=ws://127.0.0.1:<port>/api/ws?token=$HERMES_DASHBOARD_SESSION_TOKEN`; Phase B probes and
   creates the fleet rooms against it, so the gateway MUST be up before step 1's full install.

## Steps
1. Inside `osh-f64-gw`, first capture the rooted plan for step 2's evidence (a real apply does not
   echo its command vectors — only `--dry-run` prints them; running it in the sandbox's own fresh
   `HERMES_HOME` is what makes `build_plan` emit all five `provision_create` rows — on the host, the
   installed fixtures would suppress them): `install-into-sandbox.sh <spec> --ref <sha> --policy-root
   /workspace/extra/hermes-fleet-testbed/osh-f64 --dry-run > provision-plan.txt` (records the rooted
   `openshell sandbox create --policy <root>/render/<role>/policy-<role>.yaml` line for all five
   roles). Then, in the same sandbox with its `hermes serve` gateway already running (Setup step 3),
   run `install-into-sandbox.sh <spec> --ref <sha> --gateway-url "$GW_URL" --policy-root /workspace/extra/hermes-fleet-testbed/osh-f64`
   (Phase A idempotently re-skips the Setup's staged phase-a → Phase B `hermes coworker
   install-openshell`, which composes the fleet, provisions the five `osh-f64-<profile>` worker
   sandboxes via the OpenShell setup prefix — `sandbox create --policy <root>/render/<profile>/policy-<profile>.yaml`
   (the mirrored HOST path the broker validates) / `ssh-config`, no teardown — creates the fleet rooms
   against the live gateway over `$GW_URL`, and restarts the gateway to pick up the config)
   → expect: ONE externally-listening messaging gateway on ONE
   bound port serving `default` + ≥2 allowlisted coworker profiles (no second gateway, no
   second listening port). Profile-scoped `hermes -p <profile> chat` helper subprocesses
   spawned for terminal-bearing turns are expected and do NOT count against "one
   gateway/one port" (they bind no external port).
2. Verify the worker sandboxes Phase B created in step 1 (no separate `sandbox create` — Phase B
   already created them with `--policy-root`): `openshell sandbox list` shows `osh-f64-<profile>`
   Ready for the ≥2 served coworkers, each bound to its mirrored HOST policy
   `/workspace/extra/hermes-fleet-testbed/osh-f64/render/<profile>/policy-<profile>.yaml` (the
   `--policy` path is the one captured in step 1's `provision-plan.txt`) → expect: each worker sandbox
   is Ready under its rendered policy.
3. Drive a live turn as served coworker A (`architect`) **through architect's own
   profile-scoped chat child** that FIRST issues a `write_file` tool call to
   `.hermes/plans/architect.md` (the fork `nv-fleet-gates` plan-first `pre_tool_call` gate —
   `plugins/nv-fleet-gates/__init__.py:409-416`, `plan_gate` default True — exempts AND
   records `has_plan` ONLY for a `write_file`/`patch` whose path `is_plan_path`; a `terminal`
   `>` to a plan path is NOT exempt and records no plan because `predicates.target_path`
   returns None for `terminal` — `predicates.py:187-192` — so the plan MUST be a `write_file`
   call), THEN a `terminal` call writes a fresh `nonce_architect` to the per-profile marker
   path `/tmp/osh-f64-architect.marker` (for the step-5 independent isolation read) and runs
   `hostname; id -u` (now passes — `has_plan` recorded) → expect: executes inside
   `osh-f64-architect` — architect's profile-child `hostname` EQUALS a broker-side
   `openshell sandbox exec osh-f64-architect -- hostname` probe AND differs from `osh-f64-gw`'s
   and from coworker B's (`osh-f64-builder`) sandbox (routing to the ASSIGNED sandbox — the
   ROUTING half of the GATE); `id -u` captured as a non-gating logged diagnostic, expected
   10001 per the single non-root worker image, §D1/§D2. The plan-file-first ordering PRESERVES
   the file-write scope (it is not weakened); `plan_gate: false` is NOT used (it would diverge
   the rendered `nv-fleet-gates.settings` from FLEET-F62 and FAIL AC-2's equality).
4. Repeat step 3 as served coworker B (`builder`) through B's own profile-scoped child —
   FIRST a `write_file` tool call to `.hermes/plans/builder.md`, THEN a `terminal` call that
   writes a fresh `nonce_builder` to `/tmp/osh-f64-builder.marker` and runs `hostname; id -u`
   — → expect: executes inside `osh-f64-builder` — builder's profile-child `hostname` EQUALS a
   broker-side `openshell sandbox exec osh-f64-builder -- hostname` probe and is distinct from
   coworker A's (`osh-f64-architect`) AND from the `openshell sandbox exec osh-f64-gw -- hostname`
   probe (routing to the ASSIGNED sandbox — the ROUTING half of the GATE); `id -u` logged
   non-gating, expected 10001 as coworker A.
5. **Cross-sandbox ISOLATION (independent broker-side reads — the ISOLATION half of the GATE):**
   read each marker DIRECTLY through the broker, out-of-band of the profile children —
   `openshell sandbox exec osh-f64-architect -- cat /tmp/osh-f64-architect.marker` == `nonce_architect`
   and `openshell sandbox exec osh-f64-builder -- cat /tmp/osh-f64-builder.marker` == `nonce_builder`
   — then require each nonce present ONLY in its provisioned worker:
   `openshell sandbox exec osh-f64-builder -- cat /tmp/osh-f64-architect.marker`,
   `osh-f64-architect -- cat /tmp/osh-f64-builder.marker`, and
   `osh-f64-gw -- cat /tmp/osh-f64-architect.marker` / `osh-f64-gw -- cat /tmp/osh-f64-builder.marker`
   ALL fail with a NONZERO exit status AND ENOENT (`No such file or directory`) — an empty
   EXISTING file (exit 0, no output) OR a permission-denied (EACCES) read FAILS the gate, since
   only true absence proves the marker never landed there (neither the OPPOSITE worker nor the
   gateway sees the other's marker at all) → expect: each `nonce_<profile>` readable ONLY in
   `osh-f64-<profile>` and every cross read nonzero+ENOENT, proving cross-sandbox isolation on
   top of the routing probe.
6. From a worker sandbox, attempt `curl` to a host NOT in its policy → expect: denied, and
   the denial appears in `openshell logs` (the `--source` OSH-F63 recorded).
7. Teardown: delete every `osh-f64-*` sandbox and policy → expect: `sandbox list` /
   `policy list` no longer show any `osh-f64-*`.

## Pass
One gateway/one port serves `default` + ≥2 coworkers, each coworker's terminal call (driven
through its own profile-scoped child) runs in ITS own NAMED worker sandbox — the GATE is BOTH
(routing) each profile-child's `hostname` matches a broker probe of its assigned
`osh-f64-<profile>` and is distinct from the other worker and the broker-probed gateway
hostname, AND (isolation) each profile's `nonce_<profile>` is readable via an INDEPENDENT
broker-side `cat` ONLY in its provisioned worker, absent from the opposite worker and
`osh-f64-gw`; `id -u` is a logged non-gating diagnostic, identical across the single-image
workers by design, §D1/§D2 — an off-policy egress is denied and logged, and teardown is clean.

## Evidence
`scenario-AC-OSH-F64-3/evidence.txt` (per-step transcripts incl. each profile's `nonce_<profile>`
write to `/tmp/osh-f64-<profile>.marker`, the INDEPENDENT broker-side marker reads proving each
nonce present ONLY in its provisioned worker (`osh-f64-architect` == nonce_architect,
`osh-f64-builder` == nonce_builder, and the opposite-worker + `osh-f64-gw` cross reads EACH
recording a NONZERO exit status + ENOENT `No such file or directory` — an empty or
permission-denied read would FAIL — the ISOLATION gate), each profile-child's `hostname`
alongside the matching broker-side `openshell sandbox exec osh-f64-<profile> -- hostname` probe
AND the `openshell sandbox exec osh-f64-gw -- hostname` gateway probe (the ROUTING gate) and the
logged `id -u` per sandbox (non-gating diagnostic, §D1/§D2), the served-profile list, the port
count) + `scenario-AC-OSH-F64-3/openshell.log` (the egress-denial line and its `--source`) +
`scenario-AC-OSH-F64-3/gateway-restart.log` (the install's `hermes gateway restart` exit code and
full traceback, RETAINED — §D8; the run confirms restart exit 0 and a served live 200, or names
the true exit-1 frame for the route-(3) decision).
