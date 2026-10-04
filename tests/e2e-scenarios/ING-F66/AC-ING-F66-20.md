---
ac: AC-ING-F66-20
kind: live
model: live
base_url: https://inference-api.nvidia.com
api_mode: anthropic_messages
model_id: aws/anthropic/bedrock-claude-opus-4-8
fixtures:
  - fixtures/ing-f66-fleet/ing-f66-default
  - fixtures/ing-f66-fleet/ing-f66-orchestrator
  - fixtures/ing-f66-fleet/ing-f66-triager
  - fixtures/ing-f66-fleet/ing-f66-fixer
  - fixtures/ing-f66-fleet/ing-f66-reviewer
  - fixtures/ing-f66-fleet/ing-f66-approver
LIVE_MODEL_CALLS_MAX: 40
LIVE_BUDGET_USD: 5
timeout_s: 600
---

# AC-ING-F66-20 — no secret in the gateway sandbox; reviewer and approver refuse on a red check run; delivery survives a sandbox restart (live)

On the same live fleet as AC-ING-F66-19:

1. The NemoClaw environment validator passes in the gateway sandbox with ingress
   enabled, no host-held platform secret is anywhere in it, and the running
   gateway does not carry the private-URL override.
2. The reviewer (provider read) and the approver (credential-free read) both
   refuse a PR whose head carries a failing check run and no legacy commit status,
   and each names the failing check.
3. After a gateway-sandbox restart, with no other operator command, a new red
   check is delivered into the fixer's captured session.

The model is live; the provider block is the same as AC-ING-F66-19's.

## Gating — do not start before these hold

- **AC-ING-F66-19's gating holds:** LANE READY relayed, and the lane evidence
  for L-EDGE, L-FWD and L-HOOK is copied.
- **L-CHK is evidenced for both readers:**
  - the reviewer's worker sandbox reads the scratch repo's commit check-runs
    endpoint through its attached github provider;
  - the approver's worker sandbox reads the same endpoint with no credential,
    through `/usr/bin/curl` and its rendered GET-only API-host rule.

  Missing either → `FAIL(env): lane L-CHK not evidenced`.
- **Who writes to GitHub:** scratch writes are operator commands relayed by the
  Orchestrator, as in AC-ING-F66-19; the tester never writes.
- **Time cap:** the whole scenario, Setup and `finally` included, fits the
  tier's 10-minute cap:

  | part | cap |
  |---|---|
  | id captures, reusing AC-19's running fleet | ≤ 30 s |
  | step 1 | ≤ 60 s |
  | step 2, one shared deadline | ≤ 180 s |
  | restart | ≤ 120 s |
  | post-restart delivery | ≤ 180 s |
  | teardown | ≤ 30 s |

## Setup (after the fixtures are installed)

No fixture is re-installed here.

1. **The fleet from AC-ING-F66-19 Setup steps 1–3 is still up** in `ing-f66-gw`
   (when run alone, perform those steps first).
2. **A fresh public scratch repo** `slang-coworkers/ing-f66-scratch-<ts>`, with
   the same workflow as AC-19.
   - The fixer opens a draft PR `PR` on it from a branch whose head contains
     `RED`, so its single check run `ing-f66-red` concludes `failure`. The repo
     writes no legacy commit status.
   - `select owner_profile, session_id from pr_owner where pr = <PR>` gives
     (`fixer`, `FIX_S`).
   - Record `HEAD` = the PR head SHA.
3. **Captured ids,** with `python3 -c` over the in-sandbox state DBs:
   - `REV_S` and `APR_S`: the `title='Bot Chat'` session ids in
     `profiles/reviewer/state.db` and `profiles/approver/state.db`;
   - `REV_M0`, `APR_M0` and `FIX_M0`: the max `messages.id` in each DB;
   - `FIX_S` and `FIX_N0`, as in AC-19.
4. **Budget baseline,** as in AC-19. Hard-stop at 16 model calls for this
   scenario.

Drives resume the stored `Bot Chat` session over the gateway WS, as in AC-19;
a dashboard drive selects the profile in the profile combobox first.

## Steps

1. **Validator, no secret, no override (≤ 60 s).**
   - The operator runs the NemoClaw env validator in `ing-f66-gw` with ingress
     enabled (lane L-VAL) → expect: pass.
   - The operator runs a host-side boolean substring scan of the gateway
     sandbox's `$HERMES_HOME` files for the bytes of the host-held platform
     secret files that `host/edge.yaml` names → expect: `no match`. The scan
     prints only the boolean: no material, no digest, no length.
   - The operator reads the environment of the RUNNING gateway process. Its pid
     is in the DEFAULT home's `gateway.pid` (release `gateway/status.py:217-220`).
     The check is whether that environment carries the private-URL override the
     URL guard reads (release `tools/url_safety.py:252`). The report gives the
     variable's name and `set` or `unset` only, never a value.

   → expect `unset`. `set` fails this step and the scenario (fail closed).
2. **Both readers refuse (one shared 180 s deadline).** Send both requests
   together:
   - resume `REV_S` and ask the reviewer to review `PR` at head `HEAD`;
   - resume `APR_S` and ask the approver for the final approval of `PR` at the
     same head.

   → expect, both within the shared deadline, captured separately per bot:
   - the reviewer's reply (a `messages` row in `REV_S` with `id > REV_M0` and
     role `assistant`) refuses to approve and names the failing check run
     `ing-f66-red`;
   - the approver's reply (in `APR_S`, `id > APR_M0`) refuses and names
     `ing-f66-red` too;
   - the reviewer's worker terminal log (`tool` rows in `REV_S`) shows its
     `checks_gate.py <repo> <PR> <HEAD> --read provider` run, exit 3, with the
     check named under `refused`;
   - the approver's log (in `APR_S`) shows `checks_gate.py <repo> <PR> <HEAD> --read anonymous`,
     exit 3, with the check named;
   - the operator lists the scratch PR's reviews and confirms there is no
     `APPROVED` review and no approval on it.
3. **Restart, then deliver (restart ≤ 120 s, delivery ≤ 180 s).**
   - The operator restarts the gateway sandbox `ing-f66-gw` through the broker
     (lane L-RST-SB) and records `T3`. Nobody runs any other command.
   - Wait for three signals:
     - the gateway ready marker in the in-sandbox gateway log;
     - `systemctl --user is-active nv-ingress-forward.service` reporting `active`
       on the host;
     - `curl -fsS --max-time 5 "http://localhost:$FWD_PORT/health"` answering 200.

     → expect: all three within 120 s of `T3`.
   - The operator re-runs the failing job and records `T3b` (its
     `completed_at`). → expect, within 180 s of `T3b`:
     - a new `user` message with `id > FIX_M0` in `FIX_S` (or its compression
       tip), containing `concluded failure`, `ing-f66-red` and an
       `ingress-delivery: github/` line ending in `/<PR>`;
     - no new fixer session, counted as in AC-19 step 4.
4. **`finally` (always).** The operator closes the scratch PR and deletes the
   branch and the scratch repo. Record each exit status.

## Pass

The criterion holds when all three steps meet their expectations:

- step 1: the validator passes, the scan reports `no match`, and the override is
  `unset`;
- step 2: both refusals name the check;
- step 3: a post-restart event is delivered into the captured fixer session with
  no human action beyond the restart.

## Evidence

All of it goes under `$ART/scenario-AC-ING-F66-20/`:

- `step-<n>.txt` per step:
  - the validator output;
  - the boolean scan result;
  - the override name with set/unset;
  - the reviewer and approver transcript rows (id, role, first 300 chars);
  - both `checks_gate.py` argv and JSON lines, quoted from the tool rows;
  - the unit status with timestamps, and the health probe;
  - the fixer message rows by id.
- `lanes/` holds the operator evidence for L-VAL, L-RST-SB and L-CHK.
- `budget.txt` holds the summed `session_model_usage` before and after each step.
- No evidence file contains a header value, a signature, or secret material (D13).
