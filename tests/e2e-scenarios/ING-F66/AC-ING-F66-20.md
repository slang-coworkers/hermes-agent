---
ac: AC-ING-F66-20
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
  - the reviewer's worker sandbox reads `slang-coworkers/nanoclaw`'s commit
    check-runs endpoint through its attached github provider;
  - the approver's worker sandbox reads the same endpoint with no credential,
    through `/usr/bin/curl` and its rendered GET-only API-host rule.

  Missing either → `FAIL(env): lane L-CHK not evidenced`.
- **Who writes to GitHub:** nanoclaw writes (the job re-run, the teardown) are
  operator commands relayed by the Orchestrator, as in AC-ING-F66-19; the tester
  never writes. This scenario opens no PR and pushes nothing: it reuses the
  round's one scratch PR from AC-ING-F66-19 step 3.
- **Call cap: ≤ 40 model calls** for this scenario, summed over all six
  profiles. Reaching it stops the scenario with `FAIL(env): call cap`, reporting
  the call count and the in-scope delivery count; no event is dropped or
  filtered to stay under it.
- **Time cap:** the whole scenario, Setup and `finally` included, fits the
  tier's 10-minute cap as active time, per the E17 time rule (## Time, below):

  | part | cap |
  |---|---|
  | id captures, reusing AC-19's running fleet | ≤ 30 s |
  | step 1 | ≤ 60 s |
  | step 2, one shared deadline | ≤ 180 s |
  | restart | ≤ 120 s |
  | post-restart delivery | ≤ 180 s |
  | teardown | ≤ 30 s |

  E11 adds the two concurrent transport preflights to Setup, now under one
  shared 180 s deadline (E17, D26). They count inside the same 600 s
  active-time cap (D18). The ceilings now sum to 780 s, so the 600 s cap binds
  before every per-step ceiling can be reached; no other step's own deadline
  changes.

## Setup (after the fixtures are installed)

No fixture is re-installed here.

1. **The fleet from AC-ING-F66-19 Setup steps 1–3 is still up** in `ing-f66-gw`
   (when run alone, perform those steps first).
2. **The round's one scratch PR** on `slang-coworkers/nanoclaw` (AC-ING-F66-19
   step 3, `ing-f66-scratch-<ts>-fix` into `ing-f66-scratch-<ts>-base`). When
   this scenario runs alone, perform AC-19's Gating scratch setup and its
   Setup steps 4–5, its Setup step 7 (the fixer transport preflight, E11) and
   step 3 first.
   - Its head carries `RED`, so the scratch check run `ing-f66-red` concludes
     `failure`. It has NO legacy commit status: the scratch workflow writes check
     runs only, and the PR body carries no v2 template marker, so `label-pr`
     writes none. The operator confirms the PR head's combined commit status is
     empty.
   - `select owner_profile, session_id from pr_owner where repo = 'slang-coworkers/nanoclaw' and pr = <PR>`
     gives (`fixer`, `FIX_S` or a verified compression continuation of
     `FIX_S` with the same thread id, as AC-ING-F66-19 step 3 allows).
   - Record `HEAD` = the PR head SHA.
3. **Captured ids,** with `python3 -c` over the in-sandbox state DBs:
   - `REV_S` and `APR_S`: the `title='Bot Chat'` session ids in
     `profiles/reviewer/state.db` and `profiles/approver/state.db`;
   - `REV_M0`, `APR_M0` and `FIX_M0`: the max `messages.id` in each DB;
   - `FIX_S` and `FIX_N0`, as in AC-19.
4. **Budget baseline,** as in AC-19: record `T0` and `CALLS0` (the summed call
   count now; when this scenario runs alone, take it before AC-19's Setup step 4
   instead), then the count minus `CALLS0` beside the in-scope delivery count
   before every step and after the last, and stop when it reaches the 40-call
   cap (Gating).
5. **Transport preflight (E11), before step 2.** One CLI drive each on `REV_S`
   and `APR_S` (AC-ING-F66-19's **Tool-bearing drives** rules), run
   concurrently, each under its own profile's turn lock. Each asks for exactly
   one harmless terminal call, `echo ing-f66-preflight-<profile>`, with no
   GitHub call and no write. The expectations, per profile:
   - the echo appears in a `tool` row of that turn;
   - the turn has no `sandbox:` refusal;
   - the CLI exits 0.

   Any `sandbox:` refusal stops the scenario as
   `FAIL(env): transport preflight refused (<reason>)` before any further
   scratch write. Then `REV_M0` and `APR_M0` are re-recorded. The session ids,
   `T0` and `CALLS0` are unchanged, and the preflight calls count toward the
   40-call cap. **The two preflights share one 180 s deadline from their
   launch (E17, D26)**, replacing ≤ 60 s: the same shape as step 2. Their pass
   conditions are unchanged. Their elapsed time counts inside the scenario's
   600 s active-time cap.

Drives resume the stored `Bot Chat` session over the gateway WS, as in AC-19;
a dashboard drive selects the profile in the profile combobox first. A drive
that needs a tool is a CLI drive under AC-ING-F66-19's **Tool-bearing drives**
rules (E11).

## Steps

1. **Validator, no secret, no override (≤ 60 s).**
   - L-VAL in `ing-f66-gw` (E6). The lane runs it: the operator through the box
     read verb, or the tester in-sandbox through the broker where the lane
     allows it; the report records which. → expect: pass. The evidence is
     `$HERMES_HOME/.ing-f66/lval.txt` from the instrumented gateway start
     (AC-ING-F66-19 Setup step 2):
     - check 1 and check 2 both exit 0;
     - the recorded pid equals the RUNNING gateway's pid. That pid is the `pid`
       field of the DEFAULT home's `gateway.pid` (release
       `gateway/status.py:217-220`, written at start by `write_pid_file`,
       `gateway/status.py:1144-1160`). It is read raw from the file together
       with that pid's live `/proc/<pid>/cmdline`, which is a `gateway run`
       command. It is never read through `get_running_pid()`,
       `hermes gateway status` or `hermes gateway stop` (E10): those reject the
       wrapper's `hermes.real` cmdline (`gateway/status.py:505-510`) and remove
       the file. If `gateway.pid` is absent or unreadable, step 1 is FAIL(env),
       not a PASS. AC-19 Setup's process and lock records corroborate the raw
       pid-file comparison and never replace it.

     If the pids differ (the gateway was restarted in between, for example by
     the gateway-boot unit), the tester does NOT start a second gateway on top,
     because a start against a running gateway refuses (release
     `hermes_cli/gateway.py:6391`). It stops the running gateway with AC-19
     Setup's stop step (E10): `hermes gateway stop` in the DEFAULT home, then
     the recorded-pid SIGTERM path if that stop finds no gateway. It then
     repeats its one instrumented start, makes the post-start records, and
     records both pids. If the stop step fails, step 1 is FAIL(env), with no
     retry. A non-zero exit, a missing file or a pid mismatch is not a PASS.
   - The operator also runs, through the box read verb, a host-side boolean
     substring scan of the gateway sandbox's `$HERMES_HOME` files for the bytes
     of the host-held platform secret files that `host/edge.yaml` names →
     expect: `no match`. The scan prints only the boolean, never the material or
     a digest of it.
   - Last, `lval.txt` gives the private-URL env override the URL guard reads
     (release `tools/url_safety.py:252`) by name, `set` or `unset` only, never a
     value, for that same recorded gateway process.

   → expect `unset`. `set` fails this step and the scenario (fail closed).
2. **Both readers refuse (one shared 180 s deadline).** Send both requests
   together, each as a CLI drive (E11, AC-ING-F66-19's **Tool-bearing drives**
   rules), so each worker runs its own `checks_gate.py` through `terminal` in
   its own profile process. The rows below are read on each session's
   compression lineage (AC-ING-F66-19's after-turn id check):
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
   - Restart the gateway sandbox (L-RST-SB, E6): `openshell sandbox restart
     ing-f66-gw` through the broker, or the box read verb's H11. The lane runs
     it: the operator through the box read verb, or the tester through the
     broker where the lane allows it; the report records which. Record `T3`.
     Nobody runs any other command.
   - Wait for three signals:
     - the gateway ready marker in the in-sandbox gateway log;
     - `systemctl --user is-active nv-ingress-forward.service` reporting `active`
       on the host;
     - `curl -fsS --max-time 5 "http://localhost:$FWD_PORT/health"` answering 200.

     → expect: all three within 120 s of `T3`.
   - If the first post-restart inspection finds zero live gateway PIDs, expect
     exactly one approved start launch (one `running the approved start` line in
     the gateway-boot journal, H5; the same-tick start log is the
     empty-inspection evidence) and, once ready, exactly one live gateway pid in
     the sandbox by the §D9 Alive-guard match, from a read-only pid/argv scan.
     If a gateway is already live, the healthy probe skips the inspection, so
     expect zero start lines and one live gateway pid from that same scan.
     Record the journal lines and the scan; any other count FAILs the step
     (E15).
   - The operator re-runs only the scratch workflow's failed job on the PR
     (no push) and records `T3b` (its `completed_at`). → expect, within 180 s
     of `T3b`:
     - a new `user` message with `id > FIX_M0` in `FIX_S` (or its compression
       tip), containing `concluded failure`, `ing-f66-red` and an
       `ingress-delivery: github/` line ending in `/<PR>`;
     - no new fixer session, counted as in AC-19 step 4.
4. **`finally` (always).** When AC-ING-F66-24 follows in this round, keep the
   scratch PR, both branches, the issue and the label for it. Otherwise the
   operator runs the round teardown (AC-ING-F66-19 step 7). Record which, with
   each teardown command's exit status.

## Time

- **Time (E17, ruling D26).** The 600 s cap (`timeout_s`) measures the tester's active time from `T0` to the end of `finally`.
  - **Lane waits are excluded.** A lane wait runs from the moment a lane read or lane step is filed, or an operator ask leaves the tester through the chain, until its result is in the tester's hands. During one, the tester runs nothing that changes the fleet or GitHub; read-only reads are allowed and count as active time.
  - **Event deadlines stay on the wall clock**, and a lane wait never pauses them: the preflights' shared 180 s and step 2's shared 180 s, both from their launch; the restart's 120 s from `T3`; and delivery's 180 s from the re-run's completion. The 40-call cap and `LIVE_BUDGET_USD` are unchanged.
  - **Evidence.** `budget.txt` (or a `time.txt` beside it) lists each lane wait as `<what> filed <ISO> → result <ISO>`, plus the active total. An active total over 600 s is `FAIL(env): time cap`, uncounted.
  - **A retest at a new head (D26).**
    - Step 1 re-runs in full after the new instrumented start: both L-VAL exits, raw `gateway.pid` against the live `/proc` pid, the override unset, and one H9.
    - Step 2 carries under D1 only if all of these are recorded:
      - the reviewer and approver workers were not recreated, and their policy records are unchanged;
      - the delta touches neither `checks_gate.py` nor the ci-gate skill;
      - the round's scratch PR head is unchanged, `ing-f66-red` still concludes failure, and its combined status is still empty, by H18:<PR>. For the E16 retest that is #1926 at `f0da7c62`, read by H18:1926.
      - Otherwise step 2 re-runs.
    - Steps 3 and 4 run in full; then AC-24 runs in full.

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
- `lanes/` holds the lane evidence for L-VAL (`lval.txt`), L-RST-SB and L-CHK,
  each with who ran it (E6).
- `budget.txt` holds the summed `session_model_usage` call count beside the
  in-scope delivery count, before and after each step.
- `teardown.txt`: kept or torn down, with the commands' exit statuses.
- No evidence file contains a header value, a signature, or secret material (D13).
