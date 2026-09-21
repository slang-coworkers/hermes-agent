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

This proves the `/cost` GATEWAY resolution path — the surface neither panel AC exercises. A
real model turn crosses the very low Tier-2 ceiling and the session is blocked; resolution then
goes through the plugin's REAL `pre_gateway_dispatch` callback, invoked exactly as
`GatewayRunner._handle_message` invokes it (`gateway/run.py:18141`), with a forged
`SessionSource` so `principal_from_event(event) = gateway:<platform.value>:<scope>:<user_id>`.
The container has no real messaging platform, so the transport is stubbed by a minimal gateway
object (only `_session_key_for_source` + `_deliver_platform_notice`) driven by the committed
harness `ac8_gateway_drive.py`; `principal_from_event`, the durable CAS, and the effect on the
profile's `plugin_db` all run for real. Only `continue` is spent live (the ceiling/Stop branches
are proven hermetically by AC-3/AC-4).

Budget bound per `/hermes-ui-driver`: `LIVE_MODEL_CALLS_MAX=40`, `LIVE_BUDGET_USD=5`.

## Setup
- Install the `fixtures:` list (the tester does this before `## Setup`). Do NOT re-install it.
- Write the dummy `ANTHROPIC_API_KEY` into `$HERMES_HOME/profiles/cost-f30-operator/.env`
  post-install (a real key never lands in the repo; the OneCLI proxy injects the real one at
  the `inference-api.nvidia.com` hop). No messaging gateway is started — the harness drives the
  real `pre_gateway_dispatch` path in-process.
- Let `PH=$HERMES_HOME/profiles/cost-f30-operator` (the profile home; its `plugin-data/nv-cost-cap/data.db`
  is the store the harness and the evidence read). The authorized operator principal is the
  fixture's `gateway:slack:ws-1:op-1` (so `--platform slack --scope ws-1 --user op-1`).

## Steps
1. Run one real live turn that crosses the low ceiling:
   `hermes -p cost-f30-operator -z 'Write three short paragraphs about the history of timekeeping.'`
   → expect: exit 0; the turn's fire boundary records a breach and blocks the session — one
   pending episode exists (`ac8_gateway_drive.py` auto-discovers it). If one turn's spend is under
   the 0.02 ceiling, send a second `-z` prompt (≤2 turns, still bounded).
2. Drive an UNAUTHORIZED `/cost continue` (a sender NOT in `operators`):
   `python3 tests/e2e-scenarios/COST-F30/ac8_gateway_drive.py --home "$PH" --platform slack --scope ws-2 --user op-1 --decision continue`
   → expect: JSON `notice` names "not authorized"; `before == after` (no increment, `applied` 0,
   `blocked` 1) — the same-user-different-workspace principal `gateway:slack:ws-2:op-1` is refused.
3. Drive the AUTHORIZED `/cost continue` (`gateway:slack:ws-1:op-1 ∈ operators`):
   `python3 tests/e2e-scenarios/COST-F30/ac8_gateway_drive.py --home "$PH" --platform slack --scope ws-1 --user op-1 --decision continue`
   → expect: JSON `notice` names "resumed"; `after.window_start_total == before.window_start_total + 0.05`
   (one `escalation_increment_usd`), `after.blocked == 0`, `after.applied == 1`. Record this JSON's
   `session` value as `$SID` for step 5.
4. Prove the resume with a second real live turn:
   `hermes -p cost-f30-operator -z 'Reply with the single word ACKNOWLEDGED.'`
   → expect: exit 0 and a real model answer (the session is no longer blocked — its turn runs
   instead of being skipped), corroborating `after.blocked == 0` from step 3.
5. Drive the AUTHORIZED `/cost continue` AGAIN for the same (now-resolved) episode — pass
   `--session "$SID"` from step 3, because a resolved episode has nothing pending to auto-discover:
   `python3 tests/e2e-scenarios/COST-F30/ac8_gateway_drive.py --home "$PH" --session "$SID" --platform slack --scope ws-1 --user op-1 --decision continue`
   → expect: JSON `notice` names "already resolved"; `before == after` — still exactly one `applied`
   row and no further `window_start_total` advance.

## Pass
A real crossing raises the decision (1); an unauthorized `/cost` is refused with no effect (2);
one authorized `/cost continue` applies exactly one increment and unblocks (3); a second live
turn runs, proving resume (4); a repeat `/cost` is a reported no-op (5).

## Evidence
- The harness JSON line per step 2/3/5 (`before`/`after`/`notice`), saved to
  `scenario-AC-COST-F30-8/drive-{unauth,auth,repeat}.json`.
- The step-1 and step-4 `hermes -z` stdout/exit (crossing, then resumed answer).
- A `python3 -c` read over `$PH/plugin-data/nv-cost-cap/data.db` (stdlib `sqlite3`) asserting
  exactly one `applied` `resolutions` row for the episode and one `window_start_total` advance:
  ```
  python3 -c "import sqlite3;c=sqlite3.connect('$PH/plugin-data/nv-cost-cap/data.db');print('applied=',c.execute(\"select count(*) from resolutions where status='applied'\").fetchone()[0]);print('state=',c.execute('select window_start_total,blocked from cap_state').fetchall())"
  ```
- Live budget: bounded per the testbed caps; ~2 live turns.
