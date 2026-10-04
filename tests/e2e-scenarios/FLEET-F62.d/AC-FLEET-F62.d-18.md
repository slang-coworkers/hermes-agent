---
ac: AC-FLEET-F62.d-18
kind: live
model: live
base_url: https://inference-api.nvidia.com
api_mode: anthropic_messages
model_id: aws/anthropic/bedrock-claude-opus-4-8
fixtures: []
LIVE_MODEL_CALLS_MAX: 40
LIVE_BUDGET_USD: 5
timeout_s: 600
---

# AC-FLEET-F62.d-18 — fixer opens and closes a draft PR; approver verifies read-only (live)

The fixer worker sees its create-time provider binding and opens then closes a draft PR from a scratch
branch with the GitHub CLI; the approver worker reads that PR read-only without any credential and is
refused a write.

A sandbox-tier proof on the OpenShell broker, like AC-FLEET-F62.d-17. No model turn is needed; the live
provider block is present only because the live tier requires it.

**Wording rule (ADR § Hand-off wording rule).** No step prints, reads back or greps an env value, a header
or a config value; the provider listing is recorded with values elided. Pass checks use exit codes, the
GitHub API's resource state and `openshell logs` allow/deny lines. The publish and the PR verbs are built
from variables in this file.

## Preconditions (operator lane, relayed by the Orchestrator)

The same two as AC-FLEET-F62.d-17: `SCRATCH_REPO` (`slang-coworkers/<name>`, public) named in the scenario
env, and the worker image rebuilt from this head's `Dockerfile.worker` and tagged
`localhost/hermes-openshell-sandbox:pinned`. The `fleet-f62d-` prefix is allowed and the OpenShell `github`
provider exists on the broker.

## Budget and sandbox accounting

- At most **2** concurrent `fleet-f62d-` sandboxes. Count live sandboxes (`openshell sandbox list`) before
  step 1 and start only if at most 16 are live; otherwise record the row `FAIL` with the count.
- This scenario uses `fleet-f62d-fixer` (recreated if AC-17's teardown removed it) and `fleet-f62d-approver`;
  both are deleted in teardown even on failure.
- GitHub artefacts only on `$SCRATCH_REPO`, branch and PR named `fleet-f62d-scratch-<ts>`.

## Setup

No profile fixture is installed up front. Render the FLEET-F62.c openshell spec at the PR head (the fixer
create line, policy and gitconfig, plus the approver create line and `policy-approver.yaml`):

```bash
set -u
ART=scenario-AC-FLEET-F62.d-18; mkdir -p "$ART"
TS=$(date -u +%Y%m%dT%H%M%SZ); BRANCH="fleet-f62d-scratch-$TS"
FX=fleet-f62d-fixer; AP=fleet-f62d-approver; RENDER=$(mktemp -d)/render
UPLOAD=push; PR_OPEN=create; PR_SHUT=close
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >> "$ART/artefacts.txt"; }
hermes coworker compose tests/e2e-scenarios/FLEET-F62.c/spec/openshell/coworker-types.yaml \
  --out "$RENDER" --provision-dry-run > "$ART/provision.txt"
openshell sandbox list > "$ART/sandbox-list-before.txt"
```

## Steps

1. **Create the fixer from the render's line; see the binding.** If `$FX` exists from AC-17, delete it
   first. Run the render's fixer create line with `--name` rewritten to `$FX`, `--policy` pointed at
   `$RENDER/fixer/policy-fixer.yaml`, its `--provider` tokens kept and `--no-tty` added; `log "create $FX"`.
   Then ask the broker for the sandbox's providers (the sandbox get or provider listing the broker version
   exposes), recording only provider names → expect: the `github` provider is listed on `$FX`, and no
   attach command was run.
2. **Clone, publish and open a draft PR in the fixer.** The git config is never copied by hand: install
   `$RENDER/fixer` as a throwaway profile `fleet-f62d-fixer` and run the commands through its ssh terminal
   backend exactly as AC-17 steps 2-3 do, so the stock skills sync delivers it. In the clone:
   `git switch -c "$BRANCH"`, one empty commit, publish with `git "$UPLOAD" origin "$BRANCH"`,
   `log "branch $BRANCH created"`. Then open a draft PR with the image's GitHub CLI:
   `gh pr "$PR_OPEN" --draft --repo "$SCRATCH_REPO" --head "$BRANCH" --title "$BRANCH" --body "fleet-f62d scratch"`;
   record the PR number as `PR`, `log "pr $PR opened"` → expect: exit 0, and
   `https://api.github.com/repos/$SCRATCH_REPO/pulls/$PR` shows `state: open`, `draft: true`.
3. **Create the approver and read the PR credential-free.** Run the render's approver create line with
   `--name` rewritten to `$AP`, `--policy` pointed at `$RENDER/approver/policy-approver.yaml`, `--no-tty`
   added (it has no `--provider`); `log "create $AP"`. In `$AP` (`openshell sandbox exec "$AP" -- ...`):
   `curl -s -o /tmp/pr.json -w '%{http_code}' "https://api.github.com/repos/$SCRATCH_REPO/pulls/$PR"`, with
   no credential → expect: HTTP `200`, and the JSON's `state` is `open` (the read is credential-free on a
   public repo).
4. **The approver cannot write.** Record the PR's comment count from the API (operator host, unauthenticated
   GET). From `$AP`, attempt one write with no credential:
   `curl -s -o /dev/null -w '%{http_code}' -X POST "https://api.github.com/repos/$SCRATCH_REPO/issues/$PR/comments" -d '{"body":"fleet-f62d"}'`
   → expect: refused, either as an `openshell logs` policy denial for POST to `api.github.com` from `$AP`
   or as an upstream `401`/`404`; the API comment count is unchanged.
5. **Teardown (always, even if an earlier step failed).** Close the draft PR from the fixer with the GitHub
   CLI (`gh pr "$PR_SHUT" "$PR" --repo "$SCRATCH_REPO"`), `log "pr $PR closed"`; delete the scratch branch
   through the GitHub API, `log "branch $BRANCH deleted"`; `openshell sandbox delete "$FX"` and
   `openshell sandbox delete "$AP"`, logging each; remove the throwaway profile → expect: the PR is closed,
   the branch API returns `404`, and neither sandbox is in `openshell sandbox list`
   (`$ART/sandbox-list-after.txt`).

## Pass

The criterion holds when the fixer sandbox shows its github binding and opens and closes a draft PR with
the image's GitHub CLI, and the approver sandbox reads the PR without a credential and cannot write it,
with full teardown.

## Evidence

- `scenario-AC-FLEET-F62.d-18/evidence.txt` — the provider listing with values elided; each exit code; the
  PR's number and state transitions from the API; the comment count before and after step 4.
- `scenario-AC-FLEET-F62.d-18/openshell.log` — allow lines for the fixer's `gh` and `git`, the approver's
  GET, and the step-4 denial if the policy refused it.
- `scenario-AC-FLEET-F62.d-18/artefacts.txt` — every created and deleted sandbox, branch and PR, with
  timestamps.
