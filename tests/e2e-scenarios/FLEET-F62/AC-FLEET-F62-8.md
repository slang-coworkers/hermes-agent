---
ac: AC-FLEET-F62-8
kind: live
model: live
base_url: https://inference-api.nvidia.com
model_id: aws/anthropic/bedrock-claude-opus-4-8
fixtures:
  - fixtures/podman/fleet-f62-default
  - fixtures/podman/fleet-f62-orchestrator
  - fixtures/podman/fleet-f62-architect
  - fixtures/podman/fleet-f62-builder
  - fixtures/podman/fleet-f62-tester
  - fixtures/podman/fleet-f62-reviewer
timeout_s: 600
---

# AC-FLEET-F62-8 — Half B completes the pipeline; unwired sends refused (live)

Continues AC-FLEET-F62-7's boot. The forward chain completes: tester runs the toy
test in ITS sandbox and replies `P6-TESTED:<nonce>:PASS` to reviewer over the T↔R
edge; reviewer reads the diff in ITS sandbox and replies `P6-REVIEW:<nonce>:APPROVE`
to the orchestrator over the reviewer↔orchestrator return edge — so the
orchestrator→architect→builder→tester→reviewer pipeline completes on ONE gateway.
Two negative controls prove the wired-only veto: an unwired `message_agent` is refused
with the exact message and leaves no `state.db` row.

Bounded `LIVE_MODEL_CALLS_MAX=40` / `LIVE_BUDGET_USD=5` (per-scenario); round-total
ceiling across 7+8+retries `LIVE_ROUND_CALLS_MAX=120` / `LIVE_ROUND_BUDGET_USD=15`.

## Setup (after the fixtures are installed)

- Half A (AC-FLEET-F62-7) complete: the one gateway is serving the five, the five
  edges are wired, and builder has emitted `P6-BUILT:<nonce>` to tester over B↔T.
- Distinct-ack-marker oracle: each ack prefix (`P6-TESTED:`, `P6-REVIEW:`) never
  appears in the sender's own prompt.

## Steps

1. tester (having received `P6-BUILT` from builder over B↔T) runs the toy test in ITS
   sandbox → replies `P6-TESTED:<nonce>:PASS` forward to reviewer over T↔R.
2. reviewer reads the diff in ITS sandbox → replies `P6-REVIEW:<nonce>:APPROVE` to the
   orchestrator on the reviewer↔orchestrator edge, closing the loop.
3. Negative control (unwired): builder `message_agent(target="orchestrator")` →
   expect refusal `wired-only: builder is not wired to orchestrator; wired teammates:
   ['architect', 'tester']` (assert prefix + membership, sorted Python list repr) and
   NO new row in orchestrator `state.db`.
4. Negative control (unwired): reviewer `message_agent(target="builder")` → refused
   likewise (`wired-only: reviewer is not wired to builder; wired teammates:
   ['orchestrator', 'tester']`).

## Pass

The pipeline completes end-to-end — `P6-TESTED:<nonce>:PASS` reaches reviewer and
`P6-REVIEW:<nonce>:APPROVE` reaches the orchestrator's Bot Chat on the return edge —
AND both unwired sends are refused with the exact message; each ack prefix never
appears in the sender's own prompt.

## Evidence

`scenario-AC-FLEET-F62-8/step-1..n.png` + `evidence.txt` + the negative-control
`state.db` no-row queries.
