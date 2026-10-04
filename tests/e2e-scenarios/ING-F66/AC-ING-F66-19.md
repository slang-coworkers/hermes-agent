---
ac: AC-ING-F66-19
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

# AC-ING-F66-19 — a real issue reaches the orchestrator once; a real red check reaches the owner's captured session (live)

This scenario runs on the live OpenShell fleet with ingress enabled:

1. A real labelled issue on a public scratch repo reaches the orchestrator's
   canonical Bot Chat exactly once, and a redelivery adds nothing.
2. The fixer opens a draft PR from its own Bot Chat, which claims the PR.
3. A real failing check run on that PR is delivered into the fixer's CAPTURED
   session, and the orchestrator receives nothing for it.
4. The edge refuses unsigned and badly signed requests.
5. The forwarded port answers on host loopback only.

The model is live: a real model answers behind the OneCLI proxy. The provider
block is the FLEET-F62.c spine's `coworkers-live` provider (`base_url` and
`api_mode` above), with the spine's dummy placeholder credential, used unchanged.

## Gating — do not start before these hold

- The Orchestrator has relayed **LANE READY** for this row: broker prefix
  `ing-f66-` plus lanes L-EDGE, L-FWD, L-HOOK and L-CHK. Each lane's operator
  evidence is copied into `$ART/scenario-AC-ING-F66-19/lanes/`. A lane without
  evidence is `FAIL(env): lane <name> not evidenced`; never assume one.
- The tester never writes to GitHub, because it is no-push. Two kinds of scratch
  write happen here:
  - Every scratch-repo write except the PR open in step 3 is one command the
    operator runs through the Orchestrator, reporting the command's UTC
    timestamp: the repo, its workflow, the issue, the failing push, the
    redelivery and the teardown.
  - The PR open in step 3 is made by the fixer's own provider-backed `gh`
    inside its worker sandbox.
- **Scratch repo.** One PUBLIC repo `slang-coworkers/ing-f66-scratch-<ts>`:
  - Its workflow writes check runs only, never a legacy commit status. It has
    one job, `ing-f66-red`, which fails when the file `RED` exists on the head.
  - It has a prepared branch `ing-f66-<ts>-fix` holding one commit.
  - The L-HOOK repo hook points at the edge's public URL and subscribes the
    rendered event list.
- Sandboxes in use: `ing-f66-gw` plus the five `ing-f66-<role>` workers, which
  is 6 of the broker's 18. Never more than 6 at once.

## Setup (after the fixtures are installed)

The `fixtures:` list above is the reference render of the ING-F66 spec copy. The
tester installs it into its local testbed under the fixture names, which gives
the S2/S5 preflight checks something to read. The LIVE fleet runs inside
`ing-f66-gw` under the role names (`default`, `orchestrator`, `fixer`, …), and the
lane installer builds it. No fixture is re-installed here.

1. **Fleet up in `ing-f66-gw`.** Use the OpenShell lane installer pattern from
   `website/docs/user-guide/fleet-openshell.md`:
   - stage the PR head;
   - run `install-into-sandbox.sh tests/e2e-scenarios/ING-F66/spec/openshell/coworker-types.yaml --ref <head sha> --gateway-url "$GW_URL" --policy-root <lane root>`;
   - start fleet processes from the in-sandbox watchdog, never via
     `sandbox exec`.

   Expect: five `ing-f66-<role>` worker sandboxes Ready, and `nv-ingress`
   installed in every profile, because it enters through the spine.
2. **The DEFAULT ingress keys.** The lane installer edits only its own DEFAULT
   keys, so this step adds the ingress keys by hand. Inside `ing-f66-gw`, render
   the spec with `hermes coworker compose <spec> --out "$HERMES_HOME/.ing-f66/render"`, then use one
   `python3 -c` to deep-merge these keys of
   `render/default/config.yaml` into `$HERMES_HOME/config.yaml`:
   - `platforms.webhook`;
   - `plugins.entries.nv-ingress`;
   - `nv-ingress` in `plugins.enabled`.

   Then copy `render/default/scripts/ingress_stage.py` to `$HERMES_HOME/scripts/`
   and restart the gateway.

   Expect:
   - `hermes ingress status --json` in the DEFAULT home prints
     `"webhook_guard": {"breach": false, …}`;
   - the in-sandbox `curl -fsS http://127.0.0.1:$WEBHOOK_PORT/health` answers 200,
     where `$WEBHOOK_PORT` is `platforms.webhook.extra.port` read from that
     config with `python3 -c`.
3. **Host units.**
   - On the host, run `hermes ingress render-host <spec> --out <host dir>`. This
     writes `host/` and `hooks/`; they match `fixtures/ing-f66-fleet/host/`, with
     `@LOOPBACK@` standing for the loopback address in the reference copy.
   - Confirm the operator evidence for L-EDGE and L-FWD: the three
     `nv-ingress-*.service` units are `active` with linger.
   - Read `FWD_PORT` (`gateway.port`) and the edge log path from the rendered
     `host/edge.yaml` with `python3 -c`. The edge log is the edge unit's journal
     (`journalctl --user -u nv-ingress-edge.service`).
4. **Captured ids.** Use `python3 -c` over the in-sandbox state DBs:
   - `ORCH_S`: the id of the `sessions` row with `title='Bot Chat'` in
     `profiles/orchestrator/state.db`;
   - `FIX_S`: the same row in `profiles/fixer/state.db`;
   - `ORCH_M0` and `FIX_M0`: `COALESCE(MAX(id),0)` from `messages` in each of
     those DBs;
   - `FIX_N0`: `count(*)` from `sessions` in the fixer DB.

   Ledger reads are `python3 -c` over the DEFAULT home's
   `plugin-data/nv-ingress/data.db`.
5. **Budget baseline.** Record the `session_model_usage` call count summed over
   all six profiles. Re-check it before every step, and hard-stop at 12 model
   calls for this scenario (the ADR sizing), well under
   `LIVE_MODEL_CALLS_MAX=40`.

**Drive vs observe.** A drive into a coworker's Bot Chat resumes that stored
`title='Bot Chat'` session over the gateway WS (`$GW_URL`, the FLEET-F62 AC-7
pattern). A dashboard drive selects the profile in the profile combobox first,
expecting the `<profile> »` prompt, because the SPA opens the DEFAULT profile's
chat otherwise. A drive never uses `hermes -z`, `session.create` or the dashboard
new-chat button: each of those mints a fresh session and would break the
"captured session" assertions below.

## Steps

1. **Issue → orchestrator, once.** The operator opens a new issue on the scratch
   repo carrying the routing label `fleet`, then records `T1`. GitHub sends two
   deliveries for it, `opened` and `labeled`, under distinct delivery ids.
   → expect, within 180 s of `T1`:
   - exactly one new `user` message with `id > ORCH_M0` in session `ORCH_S`. It
     carries the line `ingress-delivery: github/<delivery id>` for one of the two
     ids, and the issue title.
   - In the ledger, `select stage, count(*) from outbox where repo = '<scratch repo>' and kind = 'issue' group by stage`
     gives one `new` row and one `suppressed` row.
   - That `new` row's delivery has `state = 'done'`. Record `ORCH_M1` = the max
     message id after this step.
2. **Redelivery → nothing.** The operator redelivers the issue's `labeled`
   delivery from the hook's delivery log (L-HOOK) and records `T2`.
   → expect, after 60 s:
   - no message with `id > ORCH_M1` in session `ORCH_S`;
   - the edge journal since `T2` shows
     `forward of delivery <that id> acknowledged as duplicate`;
   - the outbox row count for the repo is unchanged.
3. **The fixer opens and claims the PR.** Resume the fixer's `FIX_S` session and
   ask it to open a draft PR on the scratch repo from the prepared branch
   `ing-f66-<ts>-fix`, using its provider-backed `gh` in its worker sandbox.
   → expect, for the new PR number `PR`:
   - `select owner_profile, session_id, thread_id from pr_owner where repo = '<scratch repo>' and pr = <PR>`
     gives one row (`fixer`, `FIX_S`, the thread id of `FIX_S` from
     `profiles/fixer/state.db`);
   - `hermes ingress status --json` lists no claim refusal for it.

   Record `FIX_M1` and `ORCH_M2` (the max message ids).
4. **Red check → the fixer's captured session.** The operator pushes one commit
   adding `RED` to `ing-f66-<ts>-fix`. The job `ing-f66-red` fails, and
   GitHub sends `check_run` / `check_suite` / `workflow_run` completions. Record
   `T4` as the check run's `completed_at`, read by the operator and relayed.
   → expect, within 180 s of `T4`:
   - at least one new `user` message with `id > FIX_M1` in session `FIX_S`, or in
     its compression tip when `FIX_S` was compacted (follow
     `sessions.parent_session_id`). It contains `concluded failure`, the job name
     `ing-f66-red` and an `ingress-delivery: github/` line ending in `/<PR>`.
   - no new fixer session: `count(*)` from `sessions` in the fixer DB, excluding
     rows whose `parent_session_id` chain leads to `FIX_S` (compression
     continuations), still equals `FIX_N0`;
   - zero messages with `id > ORCH_M2` in session `ORCH_S`;
   - every ledger delivery row for these CI deliveries has
     `target_kind = 'owner'`, `target_profile = 'fixer'` and `state = 'done'`.
5. **Unsigned / bad signature die at the edge.** From the host, POST to the edge's
   public URL path `/github`:
   - (a) a JSON body with the event header (`X-GitHub-Event`, release
     `gateway/platforms/webhook.py:751-757`) and no signature header;
   - (b) the same body with a signature computed at run time from fresh random
     material that is not the host secret. Generate it with `python3 -c`
     (`secrets.token_hex`), never write it to a file, and never print it.

   → expect:
   - both answers are HTTP 401;
   - the edge journal shows two `rejected unsigned or badly signed github request`
     lines;
   - the outbox row count is unchanged and the edge `spool/` gained no file.
6. **The forward is loopback-only.**
   - From the host, probe `FWD_PORT` on the host's first non-loopback interface
     address (`hostname -I | cut -d' ' -f1`) with
     `curl -sS --max-time 5 http://<that addr>:$FWD_PORT/health`
     → expect: connection refused or a timeout.
   - Probe it on loopback with
     `curl -fsS --max-time 5 "http://localhost:$FWD_PORT/health"`
     → expect: HTTP 200.
7. **`finally` (always, even when an earlier step failed).** The operator closes
   the scratch PR, deletes the branch, and deletes the scratch repo. Record each
   teardown command's exit status.

## Pass

The criterion holds when steps 1–6 each meet their expectation against the
session ids captured in Setup (`ORCH_S`, `FIX_S`).

## Evidence

All of it goes under `$ART/scenario-AC-ING-F66-19/`:

- `step-<n>.txt` per step:
  - the edge journal excerpt bounded by the step's timestamps;
  - the outbox and deliveries rows;
  - the `messages` rows (id, role, first 200 chars) from the `python3 -c`
    queries over `profiles/<bot>/state.db` and `plugin-data/nv-ingress/data.db`;
  - for step 4, `T4` and the `timestamp` of the first delivered fixer message,
    which together bound the delay.
- `lanes/` holds the operator evidence for L-HOOK, L-EDGE and L-FWD.
- `budget.txt` holds the summed `session_model_usage` before and after each step.
- No step's evidence contains a header value, a signature, or any secret
  material (D13).
