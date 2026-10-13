---
ac: AC-FLEET-F62.f-9
kind: live
model: live
base_url: https://inference-api.nvidia.com
api_mode: anthropic_messages
model_id: aws/anthropic/bedrock-claude-opus-4-8
key_env: ANTHROPIC_API_KEY
fixtures: []
LIVE_MODEL_CALLS_MAX: 0
timeout_s: 600
---

# AC-FLEET-F62.f-9: a rooted brev-hermes install succeeds on the broker's digest and refuses a stale mirror (live)

**Criterion.** On brev-hermes, a rooted install with no host mount and no symlink workaround succeeds against the
broker's digest, and a deliberately stale host mirror for one role is refused before any sandbox operation, naming
that role.

**Kind note.** This is a `live:` lane criterion, but **no model call is made**: it proves an install, not an agent
turn. The provider block above is there for completeness and is not used: `key_env` holds a dummy value, and OneCLI
injects the real key. `LIVE_MODEL_CALLS_MAX` is not consumed. The outcome is shell-observable, not UI-observable.
Per the operator's standing rule of 2026-10-06, the evidence is `evidence.txt` plus logs, and no screenshots are
required.

**Runs on.** The operator lane, on brev-hermes (the operator's own NemoClaw Hermes sandbox), at the PR head under
test, only once the Orchestrator releases brev-hermes for it: no e2e run owns brev-hermes, and the operator has
done L1 on its broker (at Orchestrator msg 36 that means e2e run 3 on v2026.8.31-e2e.2 has reached its verdict).
It takes no ING-F66 lane slot, no tester time during an e2e run, and no box load from either.

**Fixtures.** None from the fork. The spec is the operator's brev-hermes install spec (the one the e2e reinstall
uses, i.e. the `nanoclaw-base` fleet), staged with `install-into-sandbox.sh` at the PR head.

## Setup

Every step below is an **operator-lane step**, run by the operator or by an operator-approved line in
`reports/lane-request.txt`. Never run any of it by hand inside brev-hermes.

- **L0:** the brev-hermes container has NO symlink, bind or copy of the host policy root at the `--policy-root`
  path. Record `ls -d <policy-root>` from inside brev-hermes (expect exit 2).
- **L1 (the lane prerequisite; until it holds, the criterion cannot pass):** the brev-hermes broker serves
  `openshell policy digest --policy F` and `openshell sandbox create … --policy-sha256 <hex>` for the sandbox prefix
  this install uses. If any worker sandbox already exists, `sandbox replace … --policy-sha256` is required too.
  These are the ING-F66 §D15 verbs. Today they are LANE READY only on the ING-F66 lane, scoped to `ing-f66-*` and
  `nanoclaw-base-*`. Whether the brev-hermes broker carries them is **unverified**. Record, before step 1:
  - the broker's verbatim answer to `openshell policy digest --policy <a host file under the root>` (expect one
    `sha256sum` line);
  - its verbatim answer for a prefix it does not allow. This confirms the absent-verb wording
    `flag not allowed: policy digest` (exit 2); if the wording differs, report the difference as a spec erratum.
- **L2:** the operator renders and mirrors the five per-worker policies under the host `--policy-root`, as the
  runbook says, and records the `sha256sum` of each host file next to the sha256 of its in-sandbox render.
- **L3:** no `<project>-<role>` worker sandbox from an earlier run exists. If some do, the operator records which
  ones (they will be replaced).

## Steps

1. Append one extra comment line to ONE role's host mirror (say `fixer`) and run
   `install-into-sandbox.sh <spec> --ref <head-sha> --gateway-url <ws-url> --policy-root <host dir>` inside
   brev-hermes → expect: a non-zero exit; the installer's refusal names `<project>-fixer` and gives the broker
   sha256 vs the render's; `openshell sandbox list` shows no new or replaced worker sandbox; the installer's
   `sandbox-policy/*.sha256` records are unchanged.
2. Restore that host mirror so it is byte-identical to the render (re-check `sha256sum`) and run the same install
   → expect: exit 0. Every worker `sandbox create` (or `replace`) line in the broker audit carries
   `--policy-sha256`, and five worker sandboxes are listed Ready. The five `sandbox-policy/<sandbox>.sha256`
   records equal the render digests.
3. Teardown: none required (this is the production reinstall). If the operator runs it on a scratch prefix
   instead, delete those sandboxes → expect: none listed.

## Pass

The criterion holds when step 1 refuses with zero sandbox operations, naming the stale role, and step 2 completes
with every bind carrying the render's sha256, with L0 recorded (no host mount, no symlink).

## Evidence

- `scenario-AC-FLEET-F62.f-9/evidence.txt`: the L0-L3 records, each step's exit code and refusal line,
  `openshell sandbox list` before and after each step, and the records' digests.
- `scenario-AC-FLEET-F62.f-9/broker-audit.log`: the broker's lines for the digest calls and for the creates or
  replaces.
- `scenario-AC-FLEET-F62.f-9/install.log`: both installer runs.

**Budget.** No model spend. Operator time is about 30 min (two installs plus the L1 records).
