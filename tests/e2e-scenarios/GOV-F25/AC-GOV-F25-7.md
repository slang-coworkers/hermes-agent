---
ac: AC-GOV-F25-7
kind: live
model: live
fixtures:
  - fixtures/gov-f25-default
  - fixtures/gov-f25-owner
timeout_s: 600
base_url: https://inference-api.nvidia.com
model_id: aws/anthropic/bedrock-claude-opus-4-8
---

# AC-GOV-F25-7 — a later webhook delivery for a claimed PR wakes the OWNING secondary-profile session, not a new orphan

One gateway serves two profiles. `gov-f25-owner` opens a PR — nv-artifact's
`post_tool_call` claims `(repo, pr)`, establishes the owning card, binds
`tasks.session_id`, and registers the durable-retry wake sub. A LATER signed
GitHub delivery for the same PR arrives at `gov-f25-default` (which owns the
`gh-pr` webhook route); because the PR is CLAIMED, the observer durably enqueues
the notification and returns `{"action":"skip"}` (no disposable default session),
and the notifier wakes the OWNER's session via the profile-scoped api_server
mirror. `model: live` — the owner's wake turn is a real model call, so a stub
cannot prove a real peer resumed; the tester pays for it.

## Setup

The tester installs BOTH `fixtures:` profiles (`gov-f25-default`,
`gov-f25-owner`) before this section; nothing below re-installs one. The one
gateway must serve both profiles (multiplexer) and expose the `gh-pr` webhook
route and the api_server (with `API_SERVER_KEY` set — the wake self-post is
403-gated on it).

1. Put a `gh` stub first on PATH so a create is deterministic and offline (no
   real GitHub write); it echoes the canonical PR URL the observer parses:
   ```bash
   ( source $TB/harness.env
     mkdir -p $TB/bin
     cat > $TB/bin/gh <<'SH'
   #!/usr/bin/env bash
   # Minimal gh stub: `gh pr create ...` prints the PR URL the observer matches.
   if [ "$1" = "pr" ] && [ "$2" = "create" ]; then
     echo "Creating pull request for feature into main"
     echo "https://github.com/gov-f25/repo/pull/41"
     exit 0
   fi
   exit 0
   SH
     chmod +x $TB/bin/gh )
   ```
2. In the dashboard, SELECT `gov-f25-owner` in the profile combobox → expect the
   `gov-f25-owner »` prompt (the SPA opens the DEFAULT profile's chat regardless
   of `-p`, so this select is mandatory before talking to the owner), per
   `/hermes-ui-driver` §1c.
3. Send the owner a message that makes it run the create (PATH carries the stub
   from step 1): e.g. `Run this exactly and report the URL: gh pr create --title "feat: x" --body "y"`. → expect: the owner runs the `terminal` tool, the
   observer claims `gov-f25/repo#41`, and the ledger has one ownership row.
4. Record the owning session id `S0` and the owner's pre-delivery turn count
   `N0` from the owner's state.db (stdlib `sqlite3`; the coworker image has no
   `sqlite3` CLI), and confirm the claim landed:
   ```bash
   ( source $TB/harness.env
     python3 - <<'PY'
   import os, sqlite3, pathlib
   home = pathlib.Path(os.environ["HERMES_HOME"])
   ledger = home / "plugin-data" / "nv-artifact" / "data.db"   # ledger_profile=default → root home
   lc = sqlite3.connect(str(ledger))
   row = lc.execute("SELECT repo, pr, task_id, profile FROM ownership WHERE repo=? AND pr=?", ("gov-f25/repo", 41)).fetchone()
   assert row and row[3] == "gov-f25-owner", ("claim missing/wrong owner", row)
   task_id = row[2]
   ow = sqlite3.connect(str(home / "profiles" / "gov-f25-owner" / "state.db"))
   s0 = ow.execute("SELECT session_id FROM tasks WHERE id=?", (task_id,)).fetchone()
   # task session_id is stored in the shared kanban.db; resolve the owning session there if needed.
   print("OWNER_TASK", task_id, "OWNER_ROW", row)
   PY
   )
   ```
   (`S0` is the card's `tasks.session_id`; `N0` = `SELECT COUNT(*) FROM messages
   WHERE session_id=S0` over the owner's state.db.)

## Steps

1. POST a signed SECOND GitHub delivery (a `pull_request_review` or
   `issue_comment` for the SAME `gov-f25/repo#41`, a DISTINCT `X-GitHub-Delivery`,
   a valid `X-Hub-Signature-256` over the raw body with the `gh-pr` route secret)
   to the default profile's route:
   ```bash
   ( source $TB/harness.env
     SECRET=gov-f25-gh-pr-test-hmac-secret
     BODY='{"action":"submitted","repository":{"full_name":"gov-f25/repo"},"pull_request":{"number":41,"head":{"sha":"beefcafe"}},"review":{"state":"commented","commit_id":"beefcafe"}}'
     SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | sed 's/^.*= //')"
     curl -sS -o $ART/webhook-resp.txt -w '%{http_code}' \
       -X POST "http://127.0.0.1:${HERMES_API_PORT:-8642}/p/gov-f25-default/webhooks/gh-pr" \
       -H "Content-Type: application/json" \
       -H "X-GitHub-Event: pull_request_review" \
       -H "X-GitHub-Delivery: gov-f25-delivery-2" \
       -H "X-Hub-Signature-256: $SIG" \
       --data "$BODY" > $ART/webhook-code.txt )
   ```
   → expect: **HTTP 202** (the webhook adapter is fire-and-forget and returns 202
   before the run starts, `gateway/platforms/webhook.py:969-987`). The PR is
   CLAIMED, so the observer skips (durable enqueue) and the default profile mints
   NO disposable session for this delivery.
2. Wait (bounded by `timeout_s`) for the notifier to wake the owner, then read
   the owner's turn count:
   ```bash
   ( source $TB/harness.env
     timeout 600 bash -c '
       until python3 -c "
   import os,sqlite3,pathlib,sys
   ow=sqlite3.connect(str(pathlib.Path(os.environ[\"HERMES_HOME\"])/\"profiles\"/\"gov-f25-owner\"/\"state.db\"))
   n=ow.execute(\"SELECT COUNT(*) FROM messages WHERE session_id=(SELECT session_id FROM sessions ORDER BY updated_at DESC LIMIT 1)\").fetchone()[0]
   sys.exit(0 if n>int(os.environ.get(\"N0\",\"0\")) else 1)"; do sleep 5; done'
     agent-browser screenshot $ART/scenario-AC-GOV-F25-7/step-2.png --full )
   ```
   → expect: the owning session `S0` gains a NEW assistant turn whose content
   references the delivered review event (the real model, woken via the
   notifier's profile-aware wake, responded).

## Pass

The owning session `S0` (the SAME `session_id` recorded at claim time) produced a
new model turn handling the later delivery — the event reached the owner, not a
fresh orphan — AND the default profile spawned no session for it.

## Evidence

- `step-1.png` (dashboard owner session before) / `step-2.png` (owner session
  after, showing the new turn) from the dashboard session view.
- The 202 response code in `$ART/webhook-code.txt`.
- stdlib `sqlite3` queries (no CLI) proving both halves:
  ```bash
  ( source $TB/harness.env
    python3 - <<'PY'
  import os, sqlite3, pathlib
  home = pathlib.Path(os.environ["HERMES_HOME"])
  ow = sqlite3.connect(str(home / "profiles" / "gov-f25-owner" / "state.db"))
  s0 = ow.execute("SELECT session_id FROM sessions ORDER BY updated_at DESC LIMIT 1").fetchone()[0]
  n = ow.execute("SELECT COUNT(*) FROM messages WHERE session_id=?", (s0,)).fetchone()[0]
  print("owner S0", s0, "turns", n, "(must exceed N0)")
  df = sqlite3.connect(str(home / "profiles" / "gov-f25-default" / "state.db"))
  # No default-profile session was minted for gov-f25/repo#41 (the observer skipped).
  print("default sessions", df.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
  PY
  )
  ```
  → expect: (a) the owner's `S0` turn count exceeds `N0`; (b) the default
  profile minted no session for this artifact. No plugin-minted session anywhere.
