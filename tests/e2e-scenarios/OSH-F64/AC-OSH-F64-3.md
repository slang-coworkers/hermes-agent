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

Then, inside `osh-f64-gw` and BEFORE the installer, in this ORDER:
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
   for each served coworker role.
3. Bring the gateway up for Phase B: `export HERMES_DASHBOARD_SESSION_TOKEN=<token>` BEFORE starting
   `hermes serve` (web_server.py:589 — a bare `hermes serve` surfaces no token and the WS `/api/ws`
   upgrade still validates `?token` despite `auth_required:false`), start the sandbox's `hermes serve`
   messaging gateway, wait for readiness, and capture
   `$GW_URL=ws://127.0.0.1:<port>/api/ws?token=$HERMES_DASHBOARD_SESSION_TOKEN`; Phase B probes and
   creates the fleet rooms against it, so the gateway MUST be up before step 1's full install.

## Steps
1. Inside `osh-f64-gw`, with its `hermes serve` gateway already running (Setup step 3), run
   `install-into-sandbox.sh <spec> --ref <sha> --gateway-url "$GW_URL" --policy-root /workspace/extra/hermes-fleet-testbed/osh-f64`
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
   `/workspace/extra/hermes-fleet-testbed/osh-f64/render/<profile>/policy-<profile>.yaml` (record the
   `--policy` path from the step-1 provision transcript) → expect: each worker sandbox is Ready under
   its rendered policy.
3. Drive a live turn as served coworker A (`architect`) **through architect's own
   profile-scoped chat child** that issues a `terminal` call writing+reading a nonce and
   running `hostname; id -u` → expect: executes inside `osh-f64-architect` (nonce
   round-trips; `hostname`/`id -u` differ from `osh-f64-gw`'s and from coworker B's sandbox).
4. Repeat step 3 as served coworker B (`builder`) through B's own profile-scoped child →
   expect: executes inside `osh-f64-builder` (distinct `hostname`/`id -u`), proving
   per-profile sandbox routing in the one multiplexed gateway.
5. From a worker sandbox, attempt `curl` to a host NOT in its policy → expect: denied, and
   the denial appears in `openshell logs` (the `--source` OSH-F63 recorded).
6. Teardown: delete every `osh-f64-*` sandbox and policy → expect: `sandbox list` /
   `policy list` no longer show any `osh-f64-*`.

## Pass
One gateway/one port serves `default` + ≥2 coworkers, each coworker's terminal call (driven
through its own profile-scoped child) runs in ITS own worker sandbox (distinct host/uid,
nonce round-trip), an off-policy egress is denied and logged, and teardown is clean.

## Evidence
`scenario-AC-OSH-F64-3/evidence.txt` (per-step transcripts incl. the two nonce round-trips
with their differing `hostname`/`id -u`, the served-profile list, the port count) +
`scenario-AC-OSH-F64-3/openshell.log` (the egress-denial line and its `--source`).
