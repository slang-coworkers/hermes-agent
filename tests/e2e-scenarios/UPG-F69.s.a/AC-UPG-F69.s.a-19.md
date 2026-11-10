---
ac: AC-UPG-F69.s.a-19
kind: live
model: live
base_url: https://inference.local/v1
api_mode: chat_completions
model_id: aws/anthropic/bedrock-claude-opus-5-5
fixtures: []
LIVE_MODEL_CALLS_MAX: 30
LIVE_BUDGET_USD: 10
timeout_s: 600
---

# AC-UPG-F69.s.a-19 — on the scratch fleet, one #2200-style chain posts the triager's comment on the issue (live)

**Criterion.** On the scratch fleet of AC-UPG-F69.s.a-18, one #2200-style chain on a fresh slang-coworkers/nanoclaw
issue posts the triager's comment on the issue. The GitHub API shows it, created after T0 by the fleet's provider
identity, and it is tied to the scratch triager's state.db tool result (the comment URL). No fleet credential posts
any PR review, and the scratch reviewer holds no GitHub credential. (Item 8, live; D-4 option 3: the triage half
only.)

**Model.** The bots run on the box, so their calls go through the inference route of the SHIPPED spec's `egress`,
which SCR_SPEC carries verbatim. The front-matter values above are the lane egress of record
(`tests/e2e-scenarios/ING-F66/spec/openshell/coworker-types.yaml` `egress.inference_provider`). Setup records the
SCR_SPEC values verbatim and checks them against the front-matter; the tester invents no endpoint, and no testbed
live-tier endpoint is used.

- Ceiling: `LIVE_MODEL_CALLS_MAX: 30`, `LIVE_BUDGET_USD: 10` for the triage half (orchestrator and triager), inside
  the tester's $120 share. Hitting it stops the run and is reported, not retried.

**Fixtures.** The AC-UPG-F69.s.a-18 scratch fleet, after its step 7 and before its teardown. Profiles orchestrator,
triager, fixer, reviewer and approver exist in `<SCR_HOME>`, and the scratch gateway is up. Values (`GW`, `SCR_HOME`,
`SENV`, `PFX`, …) are AC-UPG-F69.s.a-18's.

## Standing rules (every hand-off)

- A timing-only failure with a same-node base match in an untouched file is ADVISORY (msg 3088).
- A live miss on model wording, format or timing is a WATCH item, never a counted FAIL (msg 3730).
- Any delete for disk space is asked first. One row in test on the box at a time.
- CI at the head: every lane green, except exact node ids in untouched files that also fail in fork CI at base
  (DN-24 test).
- The gate ends at MERGE-READY; bots never ready or merge.
- **Ids (A6):** the Results row and the reviewer's cross-walk spell `AC-UPG-F69.s.a-19` verbatim, dots included.
- **Item-2 precondition (orch msg 20):** item 2 is DONE. As a precondition, record team-lead's read that the
  127.0.0.1:28645 → v0225:8644 forward is listening in `evidence.txt`. If it is not listening, that is FAIL(env) and
  the run stops.
- **Box steps are test orchestration only:** team-lead runs every step, one at a time, in the AC-UPG-F69.s.a-18 exec
  form; hermes-tester never execs on v0225.

## Who writes to GitHub

- Team-lead (human, own credential) opens the one test issue and later closes it and the PR.
- The bots post only through the triager's `github` provider. Under D-4, SCR_SPEC drops `github` from the
  reviewer's `providers`, so the scratch reviewer holds no GitHub credential. The approver has none already.
- Nobody approves or reviews anything (owner rule, D-4).

## Setup

- **Inference values:** record SCR_SPEC's `egress.inference_provider` `base_url`, `api_mode` and `model_id`
  verbatim in `evidence.txt`. They must equal the front-matter; a difference is FAIL(env) and is reported, never
  fixed in this file.
- **A4 preconditions** (team-lead box facts be6999a6, orch msg 38), recorded in `evidence.txt` before any step:
  1. Installed from the shipped spec shape, the `fleet-f62d-s5a` triager/fixer/reviewer each get the OpenShell
     `github` provider and the shipped inference route. Under D-4, SCR_SPEC removes it from the reviewer. After the
     install, re-check which scratch sandboxes carry the provider, and the inference route, and record both.
     Expected: triager and fixer carry the provider; the reviewer and the approver do not.
  2. All of them share ONE GitHub credential, so one identity. The login is not established beforehand, and there is
     no identity probe. `BOT_LOGIN` is read in step 2 from the author of the comment that the triager's tool result
     names.
  3. The shipped approver holds no GitHub credential.
  - Never an approval attempt as a probe. Not established → FAIL(env) and a DN to team-lead; never a hand fix.
- **D-4:** option 3 (strict), ruled. No fleet credential posts a PR review in this row.
- **W1 pause protocol (A9; D-1 option 2, team-lead msg 100).** Team-lead pauses the watchdog and stops the 28645
  forward before the issue opens, and restores both on every exit path:
  - (a) About 1 h before opening the issue, the tester sends `NEED W1 PAUSE` up to the Orchestrator, directly if it
    has an Orchestrator edge, otherwise up its chain. The Orchestrator posts it to team-lead.
  - (b) The issue is opened only after the Orchestrator confirms team-lead's pause.
  - (c) At the end, on any outcome (pass, fail or abort), the tester sends `W1 DONE` or `W1 ABORT`. Team-lead then
    restores both, records the delivery ids that failed in the window, and redelivers the real ones. The redelivery
    list is posted.
  - (d) S4′ checks that 127.0.0.1:28645 is listening again and targets brev-hermes-v0225:8644.
  - The pause window is a team-lead wait under A1/D26: it is excluded from the tester's time cap and listed in
    `evidence.txt`.
- `T0=$(date -u +%FT%TZ)`, taken after the pause is confirmed.
- Team-lead opens one issue on slang-coworkers/nanoclaw, giving `N`. The ask is a one-line, self-contained doc fix,
  so the chain stays short. The title is prefixed `[UPG-F69.s.a e2e]`.
- **Time (A1, D26):**
  - Every poll has its own wall-clock bound, anchored to its triggering event:
    - step 2: ≤ 600 s from the step-1 kick;
    - step 3: ≤ 120 s from step 2 holding;
    - step 4 is a single read, with no poll.
  - The overall cap counts the tester's active time only. Lane and team-lead waits, the W1 pause window included,
    are excluded and listed in `evidence.txt` with start and end UTC.

## Steps

1. Kick, the #2200 shape through the scratch route. One signed `issues`/`opened` delivery for `N` goes to the
   scratch gateway's `/p/orchestrator/webhooks/github-orch`:
   - payload = `{"action":"opened","issue":<GET issue N>,"repository":<GET repo>,"sender":<issue author>}`;
   - headers `X-GitHub-Event: issues`, a fresh `X-GitHub-Delivery`, and `X-Hub-Signature-256` from the route's
     secret, computed in GW and never printed.
   → expect: HTTP 2xx and not `"status": "ignored"`; a new session in `<SCR_HOME>/profiles/orchestrator/state.db`
   whose first user turn names `N` (a `python3 -c` sqlite3 query, since the image has no sqlite3 CLI).
2. Triage comment (poll ≤ 600 s from the kick), read anonymously with
   `curl -q --fail --silent --show-error -H 'Accept: application/vnd.github+json' 'https://api.github.com/repos/slang-coworkers/nanoclaw/issues/<N>/comments?per_page=100'`.
   Every GitHub read in this scenario (steps 1, 2 and 4) uses this anonymous form, never `gh`, so no fleet
   credential is exercised.
   → expect:
   - `<SCR_HOME>/profiles/triager/state.db` holds the `terminal` tool call that posted the comment (the issue-comment
     command for `N` with `--repo slang-coworkers/nanoclaw` and a body file), exit 0. Its output carries the comment
     URL `…/issues/N#issuecomment-<id>`;
   - the comment with that `<id>` exists on issue `N` with `created_at ≥ T0`, read through the PUBLIC GitHub API
     without any credential (team-lead msg 112: no credential is exercised for the login). Its `user.login` is
     recorded as `BOT_LOGIN`, and its author is not team-lead's account.
3. Stop the chain, right after step 2 holds. Stop the scratch gateway and serve: the recorded pids, plus any process
   whose environ has `HERMES_HOME=<SCR_HOME>`, as in AC-UPG-F69.s.a-18 step 8; never the shipped gateway. This bounds
   the spend, and nothing past the triage half runs.
   → expect: no scratch process is left.
4. Owner rule and D-4.
   → expect:
   - for every PR on slang-coworkers/nanoclaw created after `T0`, no review by `BOT_LOGIN` (any state) and no
     APPROVED review by any bot;
   - the verdict carries both lines verbatim:
     - `approver approval not posted: no credential (owner rule: approvals are human-only)`;
     - `review half blocked: approvals human-only; GitHub has no comment-only review permission; awaiting egress enforcement`.

## Pass

Steps 1–4 hold. Comment wording, length or format is a WATCH item (msg 3730), never a counted FAIL. The counted
oracles are GitHub API state plus the state.db tool-call rows.

## Evidence

`artifacts/scenario-AC-UPG-F69.s.a-19/`, holding:

- `step-<n>.json` for each API read, and the state.db query outputs;
- `BOT_LOGIN`, `N`, `T0`;
- the inference values recorded at Setup;
- `evidence.txt` (`## <step> at=<UTC>`, command, `rc=`, key lines, tokens and secrets redacted), the W1 pause window
  and every excluded wait with start and end UTC.

## After

The W1 restore (c) first. Then S4′ (AC-UPG-F69.s.a-18 step 7, repeated, with W1 (d)) proves the shipped fleet
unchanged across this criterion.

## Teardown

AC-UPG-F69.s.a-18 step 8. The issue and the PR stay open until team-lead closes them (a human GitHub write).
