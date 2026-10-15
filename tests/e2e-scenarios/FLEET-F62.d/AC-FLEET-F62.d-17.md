---
ac: AC-FLEET-F62.d-17
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

# AC-FLEET-F62.d-17 — clone and provider-authenticated push in a fresh provider-opted worker (live)

In a freshly created provider-opted worker sandbox, `git clone` of a slang-coworkers scratch repo
succeeds and a provider-authenticated push of a scratch branch succeeds, then the branch is deleted.

This is a sandbox-tier proof on the OpenShell broker (the operator's brev-hermes lane), run the way the
OSH-F64 AC-3/4/5 scenarios are. It needs no model turn; the live provider block is present only because
the live tier requires it (the harness supplies its non-secret sentinel for the key field).

**Wording rule (ADR § Hand-off wording rule).** No step prints, reads back or greps an env value, a header
or a config value. Pass checks use exit codes, the GitHub API's resource state and `openshell logs`
allow/deny lines. The publish step's argv is built from variables in this file, never written as one
literal command string.

## Preconditions (operator lane, relayed by the Orchestrator)

Do not start until both are clear:

1. A scratch repo `slang-coworkers/<scratch>` exists and is named in the scenario env as `SCRATCH_REPO`
   (`slang-coworkers/<name>`, public).
2. The worker image is rebuilt from this head's `plugins/nv-coworker-compose/openshell/Dockerfile.worker`
   (GitHub CLI pinned) and tagged `localhost/hermes-openshell-sandbox:pinned`, the spec's
   `egress.sandbox_image`.

The `fleet-f62d-` broker prefix is allowed, and the OpenShell `github` provider exists on the broker
(operator-provisioned, as on brev-hermes).

## Budget and sandbox accounting

- At most **2** concurrent `fleet-f62d-` sandboxes per round. Before step 1 run `openshell sandbox list`
  and count the live sandboxes; start only if at most 16 are live (the broker cap of 18 is shared with
  OSH-F64.b and OSH-F64.c). Otherwise record the row `FAIL` with the count as evidence; never force a create.
- This scenario creates exactly one sandbox, `fleet-f62d-fixer`, and deletes it in teardown even on failure.
- GitHub artefacts only on `$SCRATCH_REPO`, named `fleet-f62d-scratch-<ts>` (`<ts>` = UTC `%Y%m%dT%H%M%SZ`).

## Setup

All paths are relative to the PR-head worktree; `$ART` is `scenario-AC-FLEET-F62.d-17/`.

```bash
set -u
ART=scenario-AC-FLEET-F62.d-17; mkdir -p "$ART"
TS=$(date -u +%Y%m%dT%H%M%SZ); BRANCH="fleet-f62d-scratch-$TS"; SB=fleet-f62d-fixer; PROFILE=fleet-f62d-fixer
UPLOAD=push
RENDER=$(mktemp -d)/render
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >> "$ART/artefacts.txt"; }
openshell sandbox list > "$ART/sandbox-list-before.txt"
```

1. Render the FLEET-F62.c openshell spec at the PR head:
   `hermes coworker compose tests/e2e-scenarios/FLEET-F62.c/spec/openshell/coworker-types.yaml --out "$RENDER" --provision-dry-run > "$ART/provision.txt"`
   → expect: exit 0.
2. Install the rendered fixer as the testbed profile:
   `hermes profile install "$RENDER/fixer" --name "$PROFILE" -y`, then
   `hermes -p "$PROFILE" config set terminal.ssh_host "openshell-$SB"` so the profile's ssh terminal targets
   this scenario's sandbox alias. No file is copied into the sandbox by hand.

## Steps

1. **Create from the render's own line.** Take the fixer create line from `$ART/provision.txt`, rewrite only
   its `--name` to `$SB`, keep its `--from`, `--policy` (pointed at `$RENDER/fixer/policy-fixer.yaml`) and
   `--provider` tokens, and add the installer's non-interactive option `--no-tty`. Run it, then
   `log "create $SB"` → expect: create exits 0 without opening an interactive session, and
   `openshell sandbox list` shows `$SB` Ready.
2. **Persist the alias and clone through the profile's own ssh terminal.** Write the alias block from
   `openshell sandbox ssh-config "$SB"` into the D8 include file `~/.ssh/config.d/openshell-nanoclaw-base.conf`
   between `# >>> openshell-$SB` / `# <<< openshell-$SB` markers, with `Include config.d/openshell-nanoclaw-base.conf`
   as the first line of `~/.ssh/config`. Then, from the profile's venv python, run one command through
   Hermes' own ssh terminal backend as the installed profile:
   `hermes -p "$PROFILE"` context, `tools.terminal_tool.terminal_tool(command=...)`. The backend uploads the
   profile's `skills/` tree into the sandbox's `~/.hermes/skills` at connect and before each command. The
   command first runs `test -f ~/.hermes/skills/nv-gitconfig/gitconfig-github` (exit code only), then
   `git clone "https://github.com/$SCRATCH_REPO" /workspace/scratch` → expect: the file check exits 0
   without any manual copy, the clone exits 0 with a `.git` dir, and stderr has no error about opening the
   null device.
3. **Publish a scratch branch with git's upload verb.** Through the same profile terminal, in
   `/workspace/scratch`: `git switch -c "$BRANCH"`, `git -c user.name=fleet-f62d -c user.email=fleet-f62d@invalid
   commit --allow-empty -m "$BRANCH"`, then publish with an argv built from variables:
   `git "$UPLOAD" origin "$BRANCH"`; `log "branch $BRANCH created"` → expect: exit 0, and
   `curl -s -o /dev/null -w '%{http_code}' "https://api.github.com/repos/$SCRATCH_REPO/branches/$BRANCH"` is `200`.
4. **Teardown (always, even if an earlier step failed).** Delete the scratch branch through the GitHub API
   (`gh api -X DELETE "repos/$SCRATCH_REPO/git/refs/heads/$BRANCH"` from the operator host), `log "branch
   $BRANCH deleted"`; `openshell sandbox delete "$SB"`, `log "delete $SB"`; `hermes profile delete "$PROFILE" -y`;
   remove the alias block → expect: the branch API returns `404`, and `$SB` is no longer in
   `openshell sandbox list` (saved to `$ART/sandbox-list-after.txt`).

## Pass

The criterion holds when clone and publish both exit 0 through the installed profile's own ssh terminal, in
a sandbox created from the render's own create line and policy, with the git config delivered by the stock
sync and not by hand, and the teardown leaves no branch and no sandbox.

## Evidence

- `scenario-AC-FLEET-F62.d-17/evidence.txt` — each command's exit code; the sandbox list before and after;
  the branch's API status after publish and after delete.
- `scenario-AC-FLEET-F62.d-17/openshell.log` — `openshell logs` allow lines for `github.com` from git's
  process tree in `$SB`.
- `scenario-AC-FLEET-F62.d-17/artefacts.txt` — every created and deleted sandbox and branch, with timestamps.
