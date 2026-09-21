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

# AC-COST-F30-8 — a `/cost` GATEWAY resolution, in TWO required parts (neither substitutes)

This proves the `/cost` GATEWAY resolution path — the surface neither panel AC (AC-6 web,
AC-7 desktop) exercises. Resolution goes through the plugin's REAL `pre_gateway_dispatch`
callback, invoked exactly as `GatewayRunner._handle_message` invokes it (`gateway/run.py:18141`),
with a forged `SessionSource` so `principal_from_event(event) = gateway:<platform.value>:<scope>:<user_id>`.
The container has no real messaging platform, so the transport is stubbed by a minimal gateway
object (`_session_key_for_source` + `_deliver_platform_notice`) driven by the committed harness
`ac8_gateway_drive.py`; `principal_from_event`, the durable CAS, and the effect on the profile's
`plugin_db` all run for real.

**Two parts, both required (see §Pass); neither substitutes for the other:**
- **Part A — a REAL live crossing** proves gateway integration + fail-closed on immeasurable spend.
  Under this container's live model the crossing is `unknown_pricing` (the model prices $0 — see the
  pricing note below), so an authorized Continue clears `blocked` but the session correctly does
  NOT resume (`resumes=false`).
- **Part B — a SEEDED priced `breach`** proves that an authorized Continue makes the session
  RUNNABLE again (`resumes=true`). It is seeded through COST-F29's real accrual path
  (`record_api_request` + `_evaluate_boundary`) with the priced model `claude-opus-4-8` and
  resolved through the SAME real `pre_gateway_dispatch` callback — no live model call.

**Pricing note (F30-R3-2):** Claude Opus 4.8 IS priced in this container (`anthropic`
`usage_pricing.py:211-222`; `bedrock` `:694-703`). The live crossing is nonetheless
`unknown_pricing` because the fleet live provider identity `aws/anthropic/bedrock-claude-opus-4-8`
resolves to `billing_mode='unknown'` (`resolve_billing_route` :1086-1089/:1125) and the provider
schema has no billing-identity override (`hermes_cli/config.py:1611`). So `resumes=true` cannot be
shown by reconfiguring the live provider (unsupported) and is instead proven by SEEDING a priced
`breach` (Part B) via the `--seed-priced-breach` harness — living in `tests/e2e-scenarios/**`, the
allowed surface; NOT a core `agent/usage_pricing.py` edit and NOT a plugin change.

Budget bound per `/hermes-ui-driver`: `LIVE_MODEL_CALLS_MAX=40`, `LIVE_BUDGET_USD=5`. At most ONE
real live crossing (Part A); Part B spends no model calls.

## Setup
- Install the `fixtures:` list (the tester does this before `## Setup`). Do NOT re-install it.
- Write the dummy `ANTHROPIC_API_KEY` into `$HERMES_HOME/profiles/cost-f30-operator/.env`
  post-install (a real key never lands in the repo; the OneCLI proxy injects the real one at the
  `inference-api.nvidia.com` hop).
- **Install `nv-cost-cap` into the profile's OWN plugins dir**:
  `$HERMES_HOME/profiles/cost-f30-operator/plugins/nv-cost-cap/`. `hermes -p cost-f30-operator`
  repoints `HERMES_HOME` to the profile, so a plugin present only in the base home's `plugins/`
  is NOT loaded for the `-p` CLI turn (tester harness-gap, disclosed + fixed round 2). Confirm the
  `-p` turn logs `user: 1 manifest` / `registered hook: pre_gateway_dispatch`.
- Let `PH=$HERMES_HOME/profiles/cost-f30-operator` (its `plugin-data/nv-cost-cap/data.db` is the
  store the harness and evidence read). The authorized operator principal is the fixture's
  `gateway:slack:ws-1:op-1` (so `--platform slack --scope ws-1 --user op-1`); `ws-2` is the
  same operator user in a DIFFERENT workspace scope (unauthorized).

## Steps

### Part A — real live crossing (gateway integration + fail-closed on immeasurable spend)
1. Run one real live turn that crosses the low ceiling:
   `hermes -p cost-f30-operator -z 'Write three short paragraphs about the history of timekeeping.'`
   → expect: exit 0; because the live model prices $0 the turn is `unknown_pricing`, so the plugin
   FAIL-CLOSES — one pending `unknown_pricing` episode exists, `blocked=1` (`ac8_gateway_drive.py`
   auto-discovers it). If one turn's boundary did not fire, send a second `-z` prompt (≤2 turns).
2. Drive an UNAUTHORIZED `/cost continue` (same user, different workspace scope `ws-2`):
   `python3 tests/e2e-scenarios/COST-F30/ac8_gateway_drive.py --home "$PH" --platform slack --scope ws-2 --user op-1 --decision continue`
   → expect: `hook_result.action=="skip"`; `notice` names "not authorized"; `before==after`
   (`applied` 0, `blocked` 1); `resumes==false`.
3. Drive the AUTHORIZED `/cost continue` (`gateway:slack:ws-1:op-1 ∈ operators`):
   `python3 tests/e2e-scenarios/COST-F30/ac8_gateway_drive.py --home "$PH" --platform slack --scope ws-1 --user op-1 --decision continue`
   → expect: `hook_result.action=="skip"`; `notice` names "resumed"; `after.window_start_total ==
   before + 5.0`, `after.blocked==0`, `after.applied==1`; and **`resumes==false`** — the still-unknown
   pricing correctly re-fail-closes the next turn (a session whose spend cannot be measured must not
   be granted budget). Record this JSON's `session` as `$SID_A`.
4. Drive the AUTHORIZED `/cost continue` AGAIN (`--session "$SID_A"`, the now-resolved episode has
   nothing pending to auto-discover):
   `python3 tests/e2e-scenarios/COST-F30/ac8_gateway_drive.py --home "$PH" --session "$SID_A" --platform slack --scope ws-1 --user op-1 --decision continue`
   → expect: `notice` names "already resolved"; `before==after` (still one `applied`, no further advance).

### Part B — seeded priced breach (an authorized Continue makes the session RUNNABLE)
5. Seed a fresh PRICED `breach` through COST-F29's accrual path (no live model call):
   `python3 tests/e2e-scenarios/COST-F30/ac8_gateway_drive.py --home "$PH" --session sess-ac8-partB --platform slack --scope ws-1 --user op-1 --seed-priced-breach`
   → expect: JSON `episode_kinds` contains `"breach"` and NOT `"unknown_pricing"`; `unpriced_signal==0`;
   `priced_total` ≈ 0.225 (> the 0.02 ceiling, < the 5.0 increment); `state.blocked==1`, `crossed==true`.
6. Drive an UNAUTHORIZED `/cost continue` on the seeded session (`ws-2`):
   `python3 tests/e2e-scenarios/COST-F30/ac8_gateway_drive.py --home "$PH" --session sess-ac8-partB --platform slack --scope ws-2 --user op-1 --decision continue`
   → expect: refused; `notice` names "not authorized"; `before==after` (`applied` 0, `blocked` 1);
   `resumes==false`.
7. Drive the AUTHORIZED `/cost continue` on the seeded session (`ws-1`):
   `python3 tests/e2e-scenarios/COST-F30/ac8_gateway_drive.py --home "$PH" --session sess-ac8-partB --platform slack --scope ws-1 --user op-1 --decision continue`
   → expect: `hook_result.action=="skip"`; `notice` names "resumed"; `after.window_start_total ==
   before + 5.0`, `after.blocked==0`, `after.applied==1`; and **`resumes==true`** — the priced breach
   is measurable, one Continue clears the delta, and the `llm_execution` middleware runs `next_call`
   EXACTLY ONCE (the gate a real next turn hits, `agent/conversation_loop.py:3343` via
   `run_llm_execution_middleware`; no second paid model call).
8. Drive the AUTHORIZED `/cost continue` AGAIN on the seeded session (`--session sess-ac8-partB`):
   → expect: `notice` names "already resolved"; `before==after` (still one `applied`, no further advance).

## Pass
BOTH parts. **Part A** — the real live crossing fail-closes (`unknown_pricing`, `blocked=1`); the
unauthorized `/cost` is refused with no mutation; the authorized `/cost continue` applies one
increment + `blocked=0` but `resumes==false` (correct); a repeat is a no-op. **Part B** — the SEEDED
priced `breach`, resolved through the SAME real callback, yields `resumes==true` (`llm_execution`
`next_call` invoked exactly once), one increment, `blocked=0`; a repeat is a no-op. The two are NOT
equivalent and neither substitutes: Part A proves real-gateway integration + fail-closed on
immeasurable spend, Part B proves resume on a measurable (priced) breach.

## Evidence
- **Part A:** the step-1 `hermes -z` stdout/exit (the real live crossing); a `python3 -c` read over
  `$PH/plugin-data/nv-cost-cap/data.db` showing one `unknown_pricing` episode + `blocked=1`; the
  step 2/3/4 harness JSON lines (`drive-A-{unauth,auth,repeat}.json`) with
  `hook_result`/`resumes`/`before`/`after`/`notice`.
- **Part B:** the step-5 seed JSON (`seed-priced.json`: `episode_kinds` = `["breach"]`,
  `unpriced_signal==0`, `priced_total`, `state.blocked==1`); the step 6/7/8 harness JSON lines
  (`drive-B-{unauth,auth,repeat}.json`) with the authorized `resumes==true` in `drive-B-auth.json`.
- A `python3 -c` read over `$PH/plugin-data/nv-cost-cap/data.db` (stdlib `sqlite3`) asserting exactly
  one `applied` `resolutions` row per resolved episode and one `window_start_total` advance each:
  ```
  python3 -c "import sqlite3;c=sqlite3.connect('$PH/plugin-data/nv-cost-cap/data.db');print('applied=',c.execute(\"select count(*) from resolutions where status='applied'\").fetchone()[0]);print('state=',c.execute('select window_start_total,blocked from cap_state').fetchall())"
  ```
- Live budget: bounded per the testbed caps; ~1–2 live turns (Part A only; Part B spends none).
