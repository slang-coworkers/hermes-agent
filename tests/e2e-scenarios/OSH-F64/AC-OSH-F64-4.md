---
ac: AC-OSH-F64-4
kind: live
model: live
base_url: inference-api.nvidia.com
api_mode: anthropic_messages
model_id: aws/anthropic/bedrock-claude-opus-4-8
key_env: ANTHROPIC_API_KEY
fixtures:
  - fixtures/osh-f64-gateway
  - fixtures/osh-f64-worker-orchestrator
  - fixtures/osh-f64-worker-builder
LIVE_MODEL_CALLS_MAX: 40
LIVE_BUDGET_USD: 5
LIVE_ROUND_CALLS_MAX: 120
LIVE_ROUND_BUDGET_USD: 15
timeout_s: 600
---

# AC-OSH-F64-4 — two-bot delivery over Bot Chat in one gateway; fleet-admin veto (live)

On the same throwaway fleet, human → orchestrator Bot Chat (`message_agent` only) runs
change `P7-<nonce>`; orchestrator → builder is refused when unwired and succeeds once
`hermes wire add orchestrator builder`; the builder's delivery turn applies the change
inside ITS worker sandbox and replies `P7-BUILT:<nonce>`; a worker profile's
`cronjob_manage` is denied by the fleet-admin veto (`orchestrator-only`); one gateway, one
port, no a2a; round total under `LIVE_ROUND_CALLS_MAX=120` / `LIVE_ROUND_BUDGET_USD=15`.
The per-scenario cap is `LIVE_MODEL_CALLS_MAX=40` / `LIVE_BUDGET_USD=5`; the round-wide cap
is enforced by the tester across the round's scenarios.

**Process boundary + independence** (as AC-3): Bot Chat is coordination only; every
terminal-bearing action runs in that role's OWN profile-scoped child (FLEET-F62 mechanism,
OSH-F63 §D2). This scenario provisions and tears down its OWN throwaway `osh-f64-gw` fleet
(same shape as AC-3's; it does NOT depend on AC-3 having left a fleet standing).

## Setup
Provision `osh-f64-gw`, start its Hermes gateway and capture its loopback credential URL as
`$GW_URL`, then run `install-into-sandbox.sh <spec> --ref <sha> --gateway-url "$GW_URL"`
(a real run requires `--gateway-url`) so `orchestrator` + `builder` are served with their
worker sandboxes Ready; both have a live OneCLI agent; the fleet-admin veto
(`nv-fleet-gates`, `enforce_sandbox: true`, orchestrator-only admin) is live from the
composed spec. No fixture is re-installed here.

## Steps
1. Human → orchestrator Bot Chat: "run change P7-<nonce>" (`message_agent` in the one
   gateway; NO a2a) → expect: orchestrator accepts (coordination turn; no terminal yet).
2. Orchestrator attempts `message_agent` → builder BEFORE any wire → expect: refused by
   the wiring gate (unwired send blocked).
3. `hermes wire add orchestrator builder`, then orchestrator → builder again → expect:
   delivered; builder receives the task.
4. The builder's delivery turn runs in the BUILDER's own profile-scoped child and issues
   the change as a `terminal` call inside ITS worker sandbox (`osh-f64-builder`), then
   replies `P7-BUILT:<nonce>` visible in the room → expect: the reply carries the exact
   nonce and the change artifact was written in the builder's sandbox (`hostname`/`id -u`
   differ from the gateway).
5. A worker profile (e.g. builder) attempts `cronjob_manage` → expect: denied by the
   fleet-admin veto (`orchestrator-only`).
6. Teardown: delete every `osh-f64-*` sandbox and policy.

## Pass
The two-bot delivery completes over Bot Chat in ONE gateway/one port with no a2a, the
unwired→wired transition gates as specified, the builder's terminal work ran in its own
sandbox (via its profile-scoped child), `cronjob_manage` is orchestrator-only, the scenario
provisioned+tore down its own fleet, and the round stays within the round-wide 120 calls /
$15 cap.

## Evidence
`scenario-AC-OSH-F64-4/step-1..5.png` (room transcript screenshots) +
`scenario-AC-OSH-F64-4/evidence.txt` (the `P7-BUILT:<nonce>` line, the unwired-refusal and
post-wire-success lines, the `cronjob_manage` denial, the one-gateway/one-port check, the
call/$ totals).
