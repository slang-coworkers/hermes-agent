---
ac: AC-LOOP-F40-5
kind: live
model: live
base_url: https://inference-api.nvidia.com
model_id: aws/anthropic/bedrock-claude-opus-4-8
fixtures:
  - fixtures/loop-f40-reviewer
timeout_s: 600
---

# AC-LOOP-F40-5 — a human-invited comment makes a real reviewer post a review back on the PR

With a real reviewer model, a human comment that mentions the reviewer bot on a
PR causes the reviewer coworker to read the diff and produce a review, which the
`gh-pr-review-invite` route posts back via `deliver: github_comment` (captured at
a local `gh` stub — no real github.com write). A comment that does **not** mention
the reviewer produces no post-back.

Only the invite route is exercised here; the `gh-pull-request` / `gh-check-suite`
CI-gate routes are covered hermetically by `tests/plugins/test_loop_f40_acceptance.py`
(AC-1..3). Binding the invite route to the one served profile `loop-f40-reviewer`
keeps the URL profile equal to the route `profile`, so the served/allowlist checks
pass (`gateway/platforms/webhook.py:598-611,614-634`).

## Setup

The tester installs the `fixtures:` list before this section runs
(`hermes profile install tests/e2e-scenarios/LOOP-F40/fixtures/loop-f40-reviewer --name loop-f40-reviewer -y`);
do not re-install it here. `$HERMES_HOME` is the shared testbed home and the
**live** env is sourced (`$TB/harness.live.env`; the egress proxy substitutes the
real credential at the `inference-api.nvidia.com` hop). The fixture provider block
is self-contained (literal on-disk `api_key`, remote host) — no `base_url` rewrite
and no `.env` key write are needed.

1. **Stage a DEFAULT (multiplexer) `config.yaml` into `$HERMES_HOME`** (this is
   NOT a `hermes profile install` target — the multiplexer default config is
   deployed by hand). It carries the webhook platform, multiplexing of the one
   served reviewer profile, and the single invite route bound to it:
   ```yaml
   gateway:
     multiplex_profiles: true          # default is false (gateway/config.py:980);
     multiplex_profile_allowlist: [loop-f40-reviewer]   # without it /p/loop-f40-reviewer/... 404s
   platforms:
     webhook:
       enabled: true
       extra:
         host: 127.0.0.1
         port: 8899
         routes:
           gh-pr-review-invite:
             secret: "loop-f40-live-invite-secret"
             profile: loop-f40-reviewer
             events: [issue_comment]
             filters:
               - {field: action, in: [created]}
               - {field: issue.pull_request, exists: true}
               - {field: comment.user.type, equals: "User"}
               - {field: comment.user.login, not_equals: "slang-reviewer[bot]"}
               - {field: comment.body, regex: "(?<![A-Za-z0-9-])@slang-reviewer(?![A-Za-z0-9-])"}
             prompt: "A human invited a review on {repository.full_name} PR #{issue.number}: {comment.body}. Read the change with `gh pr diff {issue.number} --repo {repository.full_name}` and post a concise, actionable review."
             skills: [sdlc-review]
             toolsets: [terminal, hermes-webhook]
             deliver: github_comment
             deliver_extra:
               repo: "{repository.full_name}"
               pr_number: "{issue.number}"
   ```
2. **Provide a `gh` shim on PATH in BOTH the gateway-host env** (where
   `deliver: github_comment` shells `gh`, `webhook.py:1347-1427`) **AND inside the
   reviewer's tool sandbox** (where `gh pr diff` runs). The shim:
   - for `gh pr diff <pr> …` — prints a DETERMINISTIC diff fixture carrying a
     planted sentinel defect (a hard-coded credential literal `AKIA_SENTINEL_LEAK`);
   - for `gh pr comment …` — appends the invocation (argv + `--body`) as one JSON
     line to a capture file `$TB/loop-f40-gh-capture.jsonl` instead of reaching
     github.com (no real GitHub write), then exits 0.
   Use a synthetic PR `slang-coworkers/hermes-agent#4242` at a known head.
3. **Restart the gateway** so the staged DEFAULT config + route load, then wait for
   the listener on `127.0.0.1:8899` to be ready.

## Steps

1. POST a validly-signed `issue_comment` `created` delivery to
   `/p/loop-f40-reviewer/webhooks/gh-pr-review-invite` whose `comment.user.type ==
   "User"`, `comment.user.login == "octocat"`, and `comment.body` mentions
   `@slang-reviewer` (sign the raw body with `loop-f40-live-invite-secret`,
   `X-Hub-Signature-256: sha256=<hmac>`, `X-GitHub-Event: issue_comment`,
   `X-GitHub-Delivery: live-1`; the PR body is `{"action":"created","repository":
   {"full_name":"slang-coworkers/hermes-agent"},"issue":{"number":4242,
   "pull_request":{"url":"https://api.github.com/x"}},"comment":{"body":"@slang-reviewer
   please review","user":{"type":"User","login":"octocat"}}}`) → expect: the route
   dispatches a reviewer run (HTTP 202); the reviewer invokes `gh pr diff 4242`
   (recorded in the sandbox) and the model produces a review naming the sentinel
   defect.
2. Wait (bounded, ≤ `timeout_s`) for the route's `deliver: github_comment` → expect:
   the capture file records exactly one `gh pr comment 4242 --repo
   slang-coworkers/hermes-agent --body <review>` whose body contains the sentinel
   string `AKIA_SENTINEL_LEAK`.
3. POST a second validly-signed `issue_comment` `created` delivery (delivery id
   `live-2`) whose `comment.body` does NOT mention the reviewer (e.g. `"please
   take a look"`) → expect: HTTP 200 `{"status":"ignored"}`, no reviewer run, and
   no new entry in the capture file.

## Pass

The criterion holds when the human-mention comment yields exactly one captured PR
post-back whose body identifies the sentinel defect (proving a real model read the
diff and reviewed it), and the unmentioned comment yields none.

## Evidence

- `step-1.png` / `step-2.png` — the reviewer run and the capture-file contents.
- A `python3 -c` read of the JSON-lines capture asserting the mentioned comment
  produced exactly one `gh pr comment` whose `--body` contains `AKIA_SENTINEL_LEAK`
  and that `gh pr diff` was invoked, and the unmentioned comment produced zero
  (the coworker image has no `sqlite3` CLI; the capture is a plain file):
  ```bash
  python3 -c '
  import json
  rows = [json.loads(l) for l in open("'"$TB"'/loop-f40-gh-capture.jsonl")]
  comments = [r for r in rows if r.get("subcmd") == "pr comment"]
  assert len(comments) == 1, comments
  assert "AKIA_SENTINEL_LEAK" in comments[0]["body"], comments[0]
  assert any(r.get("subcmd") == "pr diff" for r in rows)
  print("PASS: one post-back naming the sentinel; gh pr diff was read")
  '
  ```
