---
ac: AC-ING-F66-19
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

# AC-ING-F66-19 — a real issue reaches the orchestrator once; a real red check reaches the owner's captured session (live)

This scenario runs on the live OpenShell fleet with ingress enabled and the edge
scope on, against the shared public repo `slang-coworkers/nanoclaw`:

1. A real issue labelled `ing-f66-fleet` reaches the orchestrator's canonical
   Bot Chat exactly once, and a redelivery adds nothing.
2. The fixer opens the round's one scratch PR from its own Bot Chat, which
   claims the PR.
3. That PR's opening CI run fails the scratch job, and the failure is delivered
   into the fixer's CAPTURED session; the orchestrator receives nothing for it.
4. The edge refuses unsigned and badly signed requests.
5. The forwarded port answers on host loopback only.

nanoclaw's unrelated traffic is counted and logged at the edge, never delivered
(ADR D2 Scope). The live model uses the OpenShell-managed `https://inference.local/v1`
provider and its fixed rewrite placeholder, not the OneCLI proxy or `coworkers-live`.

## Gating — do not start before these hold

- The Orchestrator has relayed **LANE READY** for this row: broker prefix
  `ing-f66-` plus lanes L-EDGE, L-FWD, L-HOOK and L-CHK. Each lane's operator
  evidence is copied into `$ART/scenario-AC-ING-F66-19/lanes/`. A lane without
  evidence is `FAIL(env): lane <name> not evidenced`; never assume one.
  - L-HOOK is installed on `slang-coworkers/nanoclaw` only after the scoped edge
    runs, and the public relay points at the edge's `127.0.0.1:8650` listener.
- The tester never writes to GitHub, because it is no-push. Two kinds of scratch
  write happen here:
  - Every nanoclaw write except the PR open in step 3 is one command the
    operator runs through the Orchestrator, reporting the command's UTC
    timestamp: the label preflight and label, the two scratch branches, the
    issue, the redelivery, every job re-run and the teardown.
  - The PR open in step 3 is made by the fixer's own provider-backed `gh`
    inside its worker sandbox.
- **Scratch artefacts on `slang-coworkers/nanoclaw` (§Live-tier sizing).** No
  repo is created or deleted. A run touches only its own artefacts, all named
  `ing-f66-*`, and never another ref, PR, issue or label:
  - **Label preflight.** `ing-f66-fleet` must not exist on nanoclaw; if it
    does, stop with `FAIL(env): scratch label exists`. Otherwise the operator
    creates it, and this run owns it.
  - **Base branch** `ing-f66-scratch-<ts>-base`, cut from `nv-coworkers`, so the
    base-filtered `ci.yml` and `nv-path-guard.yml` do not fire.
  - **Head branch** `ing-f66-scratch-<ts>-fix`, cut from the base, with one
    commit that adds the scratch workflow `.github/workflows/ing-f66-scratch.yml`
    (`on: pull_request` and `workflow_dispatch`) and the flag file `RED`. Its
    one job, `ing-f66-red`, fails while `RED` exists and writes check runs only,
    never a legacy commit status.
  - `pr-guard.yml` and `verify-agent-image.yml` have no branch filter, so they
    also run on the scratch PR. Their events are in scope and are owner turns.
- **Call cap: ≤ 40 model calls** for this scenario, summed over all six profiles.
  Reaching it stops the scenario with `FAIL(env): call cap`, reporting the call
  count and the in-scope delivery count. No event is ever dropped or filtered
  to stay under it.
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
   - mirror the five rendered policies under the host policy root;
   - **rooted-install verification (E9, D15).** First make ONE role's host mirror
     deliberately stale (one appended comment line) and run the install below →
     expect: the install refuses before any sandbox operation (no `ing-f66-<role>`
     exists afterwards; the plan's `policy digest` preflight names that role).
     Record the broker's or installer's refusal line. Then restore that mirror
     byte-identical to the render;
   - run `install-into-sandbox.sh tests/e2e-scenarios/ING-F66/spec/openshell/coworker-types.yaml --ref <head sha> --gateway-url "$GW_URL" --policy-root <lane root>`
     → expect: the five creates each carry `--policy-sha256` and succeed, and five
     `sandbox-policy/<sandbox>.sha256` records exist. A fresh fleet needs only
     `create`; `sandbox replace` is exercised by the release reinstall;
   - start fleet processes from the in-sandbox watchdog, never via
     `sandbox exec`. The gateway start in step 2 is the one exception: it is a
     detached `sandbox exec` background start, the base's documented start path
     (`website/docs/user-guide/fleet-openshell.md:547-553`), and it supersedes
     this line for the gateway start.

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

   Then copy `render/default/scripts/ingress_stage.py` to `$HERMES_HOME/scripts/`.

   **Instrumented gateway start (E6).** This start replaces the plain gateway
   restart after the ingress-key merge, in this order:
   1. Stop the gateway the installer left running (`hermes gateway stop` in the
      DEFAULT home).
   2. Confirm its pid is gone. A start against a running gateway refuses
      (release `hermes_cli/gateway.py:6391`). If the stop fails, Setup is
      FAIL(env) with no retry.
   3. Make the one instrumented start: the `sandbox exec … setsid nohup hermes
      gateway run …` start is one `sh -lc` line. In that same line, after every
      `export` and immediately before the gateway launch, it:
      - runs L-VAL check 1 and check 2 (the ADR's §Operator-lane steps), with
        `$PY` the entrypoint's trusted interpreter, resolved in-sandbox from the
        `_HERMES_PYTHON=` line of `/usr/local/bin/nemoclaw-start`:
        `timeout 15s "$PY" -I /usr/local/lib/nemoclaw/validate-hermes-env-secret-boundary.py env-file /sandbox/.hermes/.env`
        and
        `timeout 15s "$PY" -I /usr/local/lib/nemoclaw/validate-hermes-env-secret-boundary.py runtime-env`;
      - writes the name-only boolean of the private-URL override the URL guard
        reads (`tools/url_safety.py:252`), `set` or `unset`, never a value;
      - writes the exit codes, the guard's stderr lines and that boolean to
        `$HERMES_HOME/.ing-f66/lval.txt`;
      - then launches the gateway in that same environment and appends its pid
        to the same file.

   Expect:
   - `hermes ingress status --json` in the DEFAULT home prints
     `"webhook_guard": {"breach": false, …}`;
   - `plugins.entries.nv-ingress.settings.issue_labels` in that config reads
     `["ing-f66-fleet"]` (`python3 -c`);
   - the in-sandbox `curl -fsS http://127.0.0.1:$WEBHOOK_PORT/health` answers 200,
     where `$WEBHOOK_PORT` is `platforms.webhook.extra.port` read from that
     config with `python3 -c` (18644 for this spec).
3. **Host units and the scoped edge.**
   - On the host, run `hermes ingress render-host <spec> --out <host dir>`. This
     writes `host/` and `hooks/`; they match `fixtures/ing-f66-fleet/host/`, with
     `@LOOPBACK@` standing for the loopback address in the reference copy.
   - Confirm the operator evidence for L-EDGE and L-FWD: the three
     `nv-ingress-*.service` units are `active` with linger.
   - With `python3 -c` over the INSTALLED edge config (`<host_dir>/edge.yaml`),
     read `FWD_PORT` (`gateway.port`), `EDGE` (`listen`) and the scope block.
     Expect `scope.repo` = `slang-coworkers/nanoclaw` and `scope.ref_prefix` =
     `scope.label_prefix` = `ing-f66-`. The edge log is the edge unit's journal
     (`journalctl --user -u nv-ingress-edge.service`).
   - `curl -fsS "http://$EDGE/scope"` → expect HTTP 200 with a `counts` object.
     Save it as `scope-before.json`.
   - Record `CALLS0` = the `session_model_usage` call count summed over all six
     profiles now, before Setup step 4, so the fixer instruction's reply counts
     toward the cap.
4. **Fixer instruction.** Resume the fixer's `title='Bot Chat'` session (see
   **Drive vs observe**) with one message: "For pull requests whose head branch
   starts with `ing-f66-`, answer each ingress delivery with a one-line
   acknowledgement and take no other action." Expect a short reply. This keeps
   the owner from pushing anything that would fire more CI.
5. **Captured ids.** Use `python3 -c` over the in-sandbox state DBs:
   - `ORCH_S`: the id of the `sessions` row with `title='Bot Chat'` in
     `profiles/orchestrator/state.db`;
   - `FIX_S`: the same row in `profiles/fixer/state.db`;
   - `ORCH_M0` and `FIX_M0`: `COALESCE(MAX(id),0)` from `messages` in each of
     those DBs;
   - `FIX_N0`: `count(*)` from `sessions` in the fixer DB.

   Ledger reads are `python3 -c` over the DEFAULT home's
   `plugin-data/nv-ingress/data.db`. Record `T0` (Unix seconds) now.
6. **Budget accounting.** Before every step, and after the last, record the
   current summed call count minus `CALLS0` beside the in-scope delivery count
   (`select count(*) from deliveries where created_at >= T0` in the ledger), and
   stop when the difference reaches the 40-call cap (Gating).

**Drive vs observe.** A drive into a coworker's Bot Chat resumes that stored
`title='Bot Chat'` session over the gateway WS (`$GW_URL`, the FLEET-F62 AC-7
pattern). A dashboard drive selects the profile in the profile combobox first,
expecting the `<profile> »` prompt, because the SPA opens the DEFAULT profile's
chat otherwise. A drive never uses `hermes -z`, `session.create` or the dashboard
new-chat button: each of those mints a fresh session and would break the
"captured session" assertions below.

## Steps

1. **Issue → orchestrator, once.** The operator opens a new issue on
   `slang-coworkers/nanoclaw` carrying the label `ing-f66-fleet`, then records
   `T1` and the issue number `ISSUE`. GitHub sends two deliveries for it,
   `opened` and `labeled`, under distinct delivery ids.
   → expect, within 180 s of `T1`:
   - exactly one new `user` message with `id > ORCH_M0` in session `ORCH_S`. It
     carries the line `ingress-delivery: github/<delivery id>` for one of the two
     ids.
   - In the ledger, `select stage, count(*) from outbox where repo = 'slang-coworkers/nanoclaw' and kind = 'issue' and number = <ISSUE> group by stage`
     gives one `new` row and one `suppressed` row.
   - That `new` row's delivery has `state = 'done'`. Record `ORCH_M1` = the max
     message id after this step.
2. **Redelivery → nothing.** The operator redelivers the issue's `labeled`
   delivery from the hook's delivery log (L-HOOK) and records `T2`.
   → expect, after 60 s:
   - no message with `id > ORCH_M1` in session `ORCH_S`;
   - the edge journal since `T2` shows
     `forward of delivery <that id> acknowledged as duplicate`;
   - the outbox row count for the issue is unchanged.
3. **The fixer opens and claims the round's one scratch PR.** Resume the fixer's
   `FIX_S` session and ask it to open a draft PR on `slang-coworkers/nanoclaw`
   from `ing-f66-scratch-<ts>-fix` into `ing-f66-scratch-<ts>-base`, using its
   provider-backed `gh` in its worker sandbox, with a one-line body that carries
   no v2 template marker (so `label-pr` writes no legacy commit status). Record
   `T3`.
   → expect, for the new PR number `PR`:
   - `select owner_profile, session_id, thread_id from pr_owner where repo = 'slang-coworkers/nanoclaw' and pr = <PR>`
     gives one row (`fixer`, `FIX_S`, the thread id of `FIX_S` from
     `profiles/fixer/state.db`);
   - `hermes ingress status --json` lists no claim refusal for it.

   This PR is reused by AC-ING-F66-20 and AC-ING-F66-24 in this round. Record
   `FIX_M1` and `ORCH_M2` (the max message ids).
4. **The opening CI run's red check → the fixer's captured session (no push).**
   Opening the PR runs the scratch workflow on it, and `ing-f66-red` fails
   because `RED` is on the head. Record `T4` as that check run's
   `completed_at`, read by the operator and relayed, and `D4` as the delivery
   id of its `check_run` `completed` delivery (the edge journal line
   `spooled github check_run delivery <id>` closest after `T4`).
   → expect, within 180 s of `T4`:
   - at least one new `user` message with `id > FIX_M1` in session `FIX_S`, or in
     its compression tip when `FIX_S` was compacted (follow
     `sessions.parent_session_id`). It contains `concluded failure`, the job name
     `ing-f66-red` and the line `ingress-delivery: github/<D4>/<PR>`.
   - no new fixer session: `count(*)` from `sessions` in the fixer DB, excluding
     rows whose `parent_session_id` chain leads to `FIX_S` (compression
     continuations), still equals `FIX_N0`;
   - zero messages with `id > ORCH_M2` in session `ORCH_S` that carry
     `github/<D4>`. A PR-event message that reached the orchestrator before the
     ownership claim landed is recorded as an allowed AC-9 delivery, not a
     failure.
   - every ledger delivery row for `D4` has `target_kind = 'owner'`,
     `target_profile = 'fixer'` and `state = 'done'`.
   - `curl -fsS "http://$EDGE/scope"` → save as `scope-after.json`. The counts
     may have grown with nanoclaw's other traffic. No delivery id that the edge
     journal logged as `out of scope` since `T0` appears in
     `select delivery_id from outbox`.
5. **Unsigned / bad signature die at the edge.** From the host, POST to the edge's
   public URL (the relay that L-HOOK points at), path `/github`:
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
7. **`finally` (always, even when an earlier step failed).** When AC-ING-F66-20
   or AC-ING-F66-24 follows in this round, keep the scratch PR, both branches,
   the issue and the label for it. Otherwise the operator runs the round
   teardown: close the scratch PR and the issue, delete both
   `ing-f66-scratch-<ts>-*` branches, and delete `ing-f66-fleet` only if this
   round created it. Record which, with each teardown command's exit status.

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
  - for step 4, `T4`, `D4` and the `timestamp` of the first delivered fixer
    message, which together bound the delay.
- `scope-before.json` and `scope-after.json`: the edge's `GET /scope` counts.
- `budget.txt`: the summed `session_model_usage` call count beside the
  in-scope delivery count, before and after each step.
- `lanes/` holds the operator evidence for L-HOOK, L-EDGE and L-FWD.
- `teardown.txt`: kept or torn down, with the commands' exit statuses.
- No step's evidence contains a header value, a signature, or any secret
  material (D13).
