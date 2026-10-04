---
ac: AC-ING-F66-24
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

# AC-ING-F66-24 — after a real host reboot the inbound path comes back on its own and delivers into the owner's captured session (live)

After a REAL reboot of brev-hermes, the three host units (edge, forward,
gateway-boot) come back, and gateway-boot starts the gateway. A red check run
pushed after boot is then delivered into the fixer's captured session. No human
acts between boot and delivery.

The model is live; the provider block is the same as AC-ING-F66-19's.

## Gating — do not start before these hold

- **LANE READY,** as in AC-ING-F66-19.
- **L-RST-HOST: a reboot window.** The tester requests the window through the
  Orchestrator, and the operator schedules it.
  - The reboot stops every live lane on the box, so the tester never performs
    or assumes it.
  - Only a real reboot counts. A user-manager restart or a sandbox restart is not
    a substitute.
  - If no window is granted in this round, the criterion is
    `FAIL(env): host reboot unavailable`, and the tester reports it up as a
    blocker.
- **L-EDGE and L-FWD:** the operator evidence shows the three units
  `enabled` and linger on for the operator user
  (`loginctl show-user <user> -p Linger` → `Linger=yes`).
- **Who writes to GitHub:** scratch writes are operator commands relayed by the
  Orchestrator, as in AC-ING-F66-19.

## Setup (after the fixtures are installed)

No fixture is re-installed here.

1. **The fleet,** as in AC-ING-F66-19 Setup steps 1–3.
2. **A fresh public scratch repo,** set up as in AC-ING-F66-20 Setup step 2: the
   fixer's claimed draft PR `PR` has the job `ing-f66-red`, which fails on
   demand.
3. **Captured before the reboot:**
   - `FIX_S`, `FIX_M0` and `FIX_N0`, as in AC-19;
   - the rendered `FWD_PORT`, from `host/edge.yaml`;
   - `systemctl --user is-enabled` for the three units, each `enabled`.
4. **Budget baseline,** as in AC-19. Hard-stop at 4 model calls for this
   scenario.

## Steps

1. **Real reboot (L-RST-HOST).** Inside the scheduled window, the operator
   reboots brev-hermes and records `T_BOOT`, the kernel boot time
   (`uptime -s`, or `journalctl -b -o short-iso | head -1`).
   → expect: the box is back and `uptime -s` is later than the window start.
2. **Back with no operator command (≤ 300 s of `T_BOOT`).** Wait, issuing no
   start command of any kind, for these signals:
   - `systemctl --user is-active nv-ingress-edge.service nv-ingress-forward.service nv-ingress-gateway-boot.service`
     reports `active` three times;
   - the gateway-boot journal (`journalctl --user -u nv-ingress-gateway-boot.service -b`)
     shows that the in-sandbox health check failed and the approved start ran
     once, for that boot;
   - `curl -fsS --max-time 5 "http://localhost:$FWD_PORT/health"` answers 200.

   → expect: all of them within 300 s of `T_BOOT`.
3. **Red check → captured session.** The operator re-runs the failing job on
   `PR` and records `T3` (its `completed_at`). → expect, within 180 s of `T3`:
   - a new `user` message with `id > FIX_M0` in `FIX_S` (or its compression
     tip), containing `concluded failure`, `ing-f66-red` and an
     `ingress-delivery: github/` line ending in `/<PR>`;
   - no new fixer session, counted as in AC-19 step 4.
4. **`finally` (always).** The operator closes the scratch PR and deletes the
   branch and the scratch repo. Record each exit status.

## Pass

The criterion holds when step 3 delivers into the captured session `FIX_S` after
a real reboot, with no human action between boot and delivery.

## Evidence

All of it goes under `$ART/scenario-AC-ING-F66-24/`:

- `step-<n>.txt` per step:
  - the boot time;
  - unit `is-enabled` before the reboot, and `is-active` after it with
    timestamps;
  - the gateway-boot journal excerpt for this boot;
  - the health probe;
  - the fixer message rows by id.
- `lanes/` holds the operator evidence for L-RST-HOST: the window grant and the
  reboot record.
- `budget.txt` holds the summed `session_model_usage` before and after.
- No evidence file contains a header value, a signature, or secret material (D13).
