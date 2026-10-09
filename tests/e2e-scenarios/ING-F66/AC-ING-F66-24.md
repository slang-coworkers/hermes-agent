---
ac: AC-ING-F66-24
kind: live
model: live
base_url: https://inference.local/v1
api_mode: chat_completions
model_id: aws/anthropic/bedrock-claude-opus-5-5
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

# AC-ING-F66-24 — after a service-level restart nothing is lost: an event held in the edge spool lands once in the owner's captured session (live)

This is a SERVICE-level restart drill (operator ruling, msg 356), not a machine
reboot. With the edge up and the forward stopped, a signed check failure for the
round's scratch PR is answered 202 and held in the edge spool. Then the OpenShell
gateway, the three nv-ingress user units and the forward are restarted. That
same delivery must land in the fixer's captured session exactly once, with no
manual delivery action, and an event sent after the restart must land too.

The model is live; the provider block is the same as AC-ING-F66-19's.

## Gating — do not start before these hold

- **LANE READY,** as in AC-ING-F66-19.
- **L-RST-SVC.** The operator performs the service restart in step 2, or
  authorizes the tester to issue the exact restart commands. It is never
  assumed: without that, the criterion is `FAIL(env): service restart not
  authorized`, reported up as a blocker. Stopping the forward unit in step 1
  is part of the same authorization.
  - A request that reaches the edge while the edge itself is down is not
    accepted. The operator redelivers any such delivery from the hook's delivery
    log once the edge is back, and records it.
- **L-EDGE and L-FWD:** the operator evidence shows the three units
  `enabled` and linger on for the operator user
  (`loginctl show-user <user> -p Linger` → `Linger=yes`).
- **Who writes to GitHub:** nanoclaw writes (the job re-runs, the teardown) are
  operator commands relayed by the Orchestrator, as in AC-ING-F66-19. This
  scenario opens no PR and pushes nothing.
- **Call cap: ≤ 24 model calls** for this scenario, summed over all six
  profiles. Reaching it stops the scenario with `FAIL(env): call cap`, reporting
  the call count and the in-scope delivery count; no event is dropped or
  filtered to stay under it.

## Setup (after the fixtures are installed)

No fixture is re-installed here.

1. **The fleet,** as in AC-ING-F66-19 Setup steps 1–3, with the edge's `scope`
   block active.
2. **The round's one scratch PR** `PR` on `slang-coworkers/nanoclaw` (AC-19
   step 3), claimed by the fixer, whose scratch job `ing-f66-red` fails on
   demand. When this scenario runs alone, perform AC-19's Gating scratch setup,
   its Setup steps 4–5 and its step 3 first.
3. **Re-captured now:**
   - `FIX_S`, `FIX_M0` and `FIX_N0`, as in AC-19;
   - `FWD_PORT` (18644 for this spec) and `SPOOL` = `<edge_state_dir>/spool`,
     from the installed `host/edge.yaml` with `python3 -c`;
   - `systemctl --user is-enabled` for the three units, each `enabled`.
4. **Budget baseline,** as in AC-19: record `T0` and `CALLS0` (the summed call
   count now; when this scenario runs alone, take it before AC-19's Setup step 4
   instead), then the count minus `CALLS0` beside the in-scope delivery count
   before every step and after the last, and stop when it reaches the 24-call
   cap (Gating).

## Steps

1. **Edge up, forward down: the event is held.** Stop ONLY the forward unit
   (`systemctl --user stop nv-ingress-forward.service`); the edge stays up.
   Confirm `systemctl --user is-active nv-ingress-edge.service` is `active` and
   the forward is `inactive`. The operator re-runs only the scratch workflow's
   failed job on `PR` (no push) and records the check run's `completed_at`.
   → expect:
   - the hook's `check_run` `completed` delivery for that run is answered 202 by
     the edge (the operator's read of the hook's delivery response, L-HOOK);
   - the edge journal has the line `spooled github check_run delivery <id>` for
     it. Record that id as `D1`;
   - `$SPOOL/github-D1.json` exists while the forward is down;
   - no message carrying `github/D1` in `FIX_S` yet.

   Record `D1`, the delivery response and the spool listing.
2. **Restart the services, not the machine (L-RST-SVC).** The operator (or the
   tester, under the authorization) restarts the OpenShell gateway, then the
   edge, forward and gateway-boot units, and records each command's timestamp.
   Nobody runs a delivery command of any kind.
   → expect: `systemctl --user is-active nv-ingress-forward.service` reports
   `active`, and `curl -fsS --max-time 5 "http://localhost:$FWD_PORT/health"`
   answers 200, within 120 s of the last restart command. Record `T_fwd`, the
   time the forward returned (the first passing health probe).
   If the first post-restart inspection finds zero live gateway PIDs, expect
   exactly one approved start launch (one `running the approved start` line in
   the gateway-boot journal, H5; the same-tick start log is the empty-inspection
   evidence) and, once ready, exactly one live gateway pid in the sandbox by the
   §D9 Alive-guard match, from a read-only pid/argv scan. If a gateway is
   already live, the healthy probe skips the inspection, so expect zero start
   lines and one live gateway pid from that same scan. Record the journal lines
   and the scan; any other count FAILs the step (E15).
3. **`D1` lands exactly once, with no manual delivery action.**
   → expect, within 180 s of `T_fwd`:
   - exactly one message in `FIX_S` (or its compression tip) carrying
     `ingress-delivery: github/D1/<PR>`: `select count(*) from messages where session_id = <FIX_S or its tip> and content like '%ingress-delivery: github/D1/<PR>%'`
     returns `1`. It contains `concluded failure` and `ing-f66-red`.
   - `$SPOOL/github-D1.json` is gone and the edge journal shows
     `forward of delivery D1 acknowledged as accepted`;
   - the ledger's delivery row for `D1` has `target_kind = 'owner'`,
     `target_profile = 'fixer'` and `state = 'done'`;
   - no new fixer session, counted as in AC-19 step 4.
4. **An event after the restart lands too.** The operator re-runs the failed
   job once more and records its `completed_at`; take `D2` from the edge journal
   as in step 1. → expect, within 180 s: exactly one message carrying
   `ingress-delivery: github/D2/<PR>` in the same captured session, and the
   fixer's session count still unchanged.
5. **`finally` (always).** AC-24 is the round's last live scenario, so the
   operator runs the round teardown: close the scratch PR and the issue, delete
   both `ing-f66-scratch-<ts>-*` branches, and delete `ing-f66-fleet` only if
   this round created it. If a step failed before the forward came back, start
   the forward unit first. Record each command's exit status.

## Time

- **Time (E17, ruling D26).** The 600 s cap (`timeout_s`) measures the tester's active time from `T0` to the end of `finally`.
  - **Lane waits are excluded.** A lane wait runs from the moment a lane read or lane step is filed, or an operator ask leaves the tester through the chain, until its result is in the tester's hands. During one, the tester runs nothing that changes the fleet or GitHub; read-only reads are allowed and count as active time.
  - **Event deadlines stay on the wall clock**, and a lane wait never pauses them: the 120 s from the service restart, within which the forward unit is `active` and a loopback health probe on 18644 answers, with `T_fwd` recorded when the forward returns; and the 180 s windows for `D1` from `T_fwd` and for `D2` from its re-run. The 24-call cap and `LIVE_BUDGET_USD` are unchanged.
  - **Evidence.** `budget.txt` (or a `time.txt` beside it) lists each lane wait as `<what> filed <ISO> → result <ISO>`, plus the active total. An active total over 600 s is `FAIL(env): time cap`, uncounted.

## Pass

The criterion holds when `D1`, acknowledged by the edge while the forward was
down, is delivered exactly once into the captured session `FIX_S` after the
service restart with no manual delivery action, and `D2` is delivered after the
restart: nothing is lost across the restart.

## Evidence

All of it goes under `$ART/scenario-AC-ING-F66-24/`:

- `step-<n>.txt` per step:
  - the edge journal lines for `D1` and `D2`;
  - the spool listing before and after the restart;
  - unit `is-enabled`, and `is-active` with timestamps around the restart;
  - `T_fwd` and the health probe;
  - the fixer message rows by id, with the marker counts for `D1` and `D2`.
- `budget.txt`: the summed `session_model_usage` call count beside the
  in-scope delivery count, before and after each step.
- `lanes/` holds the operator evidence for L-RST-SVC: the authorization and the
  restart commands' timestamps.
- `teardown.txt`: the teardown commands' exit statuses.
- No evidence file contains a header value, a signature, or secret material (D13).
