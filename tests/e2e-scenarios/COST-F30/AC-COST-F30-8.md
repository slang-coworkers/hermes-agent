---
ac: AC-COST-F30-8
kind: live
model: live
base_url: https://inference-api.nvidia.com
model_id: aws/anthropic/bedrock-claude-opus-4-8
fixtures:
  - fixtures/cost-f30-operator
timeout_s: 600
---

# AC-COST-F30-8 — a real crossing raises the decision and a `/cost` gateway command resolves it once

This proves the `/cost` GATEWAY resolution path — the surface neither panel AC exercises.
A real model turn crosses the very low Tier-2 ceiling and the session is paused with the
enriched `/cost` notice; resolution goes through a real `pre_gateway_dispatch` `/cost` event
whose principal is `principal_from_event(event) = gateway:<platform.value>:<scope>:<user_id>`.
Only `continue` is spent live (the ceiling/Stop branches are proven hermetically by AC-3/AC-4).

Budget bound per `/hermes-ui-driver`: `LIVE_MODEL_CALLS_MAX=40`, `LIVE_BUDGET_USD=5`.

## Setup
- Install the `fixtures:` list (the tester does this before `## Setup`). Do NOT re-install it.
- Write the dummy `ANTHROPIC_API_KEY` into `$HERMES_HOME/profiles/cost-f30-operator/.env`
  post-install (a real key never lands in the repo; the OneCLI proxy injects the real one at
  the `inference-api.nvidia.com` hop).
- Start `cost-f30-operator` on a messaging gateway against the live provider. Single profile —
  one bot session the operator resolves via `/cost`; not agent-to-agent.
- **Align the operator principal:** set the fixture's `operators` to the operator account's
  ACTUAL `gateway:<platform.value>:<scope>:<user_id>` (the default `gateway:slack:ws-1:op-1`
  is a placeholder). `principal_from_event` builds `platform` from the adapter's `Platform.value`
  and `scope` from `scope_id or guild_id or "-"`, so it must match the sender exactly.

## Steps
1. Send a real prompt that crosses the low ceiling in ≤2 turns → expect: the session is
   paused/blocked and the enriched `/cost` notice is delivered on the gateway (the synthetic
   pause naming `/cost continue|stop|ceiling <usd>`).
2. Send `/cost continue` from an UNAUTHORIZED sender (a different user, or the same user in a
   different workspace scope — whose `principal_from_event` id is NOT in `operators`) → expect:
   no increment, the session still paused (its next turn is still the paused notice).
3. Send `/cost continue` from the AUTHORIZED operator (`principal_from_event(event) ∈ operators`)
   → expect: exactly one increment; the session resumes on its next turn (a normal prompt is
   answered, not the paused notice).
4. Send `/cost continue` again for the same episode → expect: a reported no-op
   ("already-resolved"), no second increment.

## Pass
A real crossing raises the `/cost` decision (1); an unauthorized `/cost` is refused with no
effect (2); one authorized `/cost continue` applies exactly one increment and resumes (3); a
repeat is a no-op (4).

## Evidence
- The gateway transcript for steps 1–4 (paused notice → refused → resumed → no-op).
- A `python3 -c` read over `$HERMES_HOME/profiles/cost-f30-operator/plugin-data/nv-cost-cap/data.db`
  (stdlib `sqlite3`) asserting exactly one `applied` `resolutions` row for the episode and one
  `window_start_total` advance; live tier bounded per the testbed caps.
