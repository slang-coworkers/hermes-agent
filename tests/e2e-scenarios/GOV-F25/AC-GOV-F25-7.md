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

One `hermes gateway run` serves BOTH profiles (multiplexer). `gov-f25-owner`
opens a PR — nv-artifact's `post_tool_call` claims `(repo, pr)`, establishes the
owning card, binds `tasks.session_id`, and registers the ONE durable-retry wake
sub. A LATER signed GitHub delivery for the same PR arrives on the default
profile's `gh-pr` webhook route; because the PR is CLAIMED, the observer durably
enqueues the notification and returns `{"action":"skip"}` (no disposable default
session), and the kanban notifier wakes the OWNER's existing session via the
api_server profile-scoped mirror `POST /p/gov-f25-owner/v1/chat/completions`.
`model: live` — the owner's wake turn is a real model call, so a stub cannot
prove a real peer resumed in the SAME session; the tester pays for it.

**Topology (one gateway, cited to the pinned tree):**
- The api_server is a loopback listener **force-enabled by the presence of
  `API_SERVER_KEY`** (`gateway/run.py:9370`) on `:8642` (`DEFAULT_PORT`,
  `gateway/platforms/api_server.py:266`); under the multiplexer it mirrors
  `/p/<profile>/v1/*` (`api_server.py:36`, `_make_profile_prefix_middleware`
  `:2179-2208`, installed `:7758`) so the wake can reach the secondary owner.
- The `gh-pr` webhook route is served by the **webhook adapter on `:8644`**
  (`DEFAULT_PORT` `gateway/platforms/webhook.py:130`), at `/webhooks/gh-pr` and
  `/p/<profile>/webhooks/gh-pr` (`webhook.py:291,298`) — **not** the api_server
  port. A POST to `:8642/.../webhooks/gh-pr` 404s (the api_server has no webhook
  route); the round-1 defect.
- A secondary multiplex profile **cannot bind its own api_server**
  (`gateway/run.py:9242-9244`), so `gov-f25-owner` deliberately declares no
  `platforms.api_server` and is reached only through the default's mirror.
- `API_SERVER_KEY` is set as a process env var on the gateway, so
  `agent.secret_scope.get_secret("API_SERVER_KEY")` resolves the same key for
  EVERY profile scope — the Bearer that authenticates `/p/gov-f25-owner/...` for
  this scenario is the identical key the notifier's own wake self-POST uses
  (`api_server.py:1902`), which is why the wake is 403-gated on it.

## Setup

The tester installs BOTH `fixtures:` profiles (`gov-f25-default`,
`gov-f25-owner`) before this section; nothing below re-installs one. Everything
here runs against the ONE gateway launched in step 3. This is a `live:`
scenario, so the harness env is `$TB/harness.live.env` (dummy key, real base
URL, credential injected at the OneCLI-proxy hop — never a real key on disk).

1. Put a `gh` stub first on PATH so a create is deterministic and offline (no
   real GitHub write); it echoes the canonical PR URL the observer parses. The
   stub must be on the GATEWAY's PATH (step 3 exports it) so the owner's
   `terminal` tool call resolves it:
   ```bash
   ( source $TB/harness.live.env
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
2. Persist the shared api_server key + ports so every later subshell agrees on
   them (each ```bash block is its own subshell). The key is a test-only literal
   guarding a loopback listener — not a credential to any service:
   ```bash
   ( source $TB/harness.live.env
     cat > $TB/gov-f25.env <<'ENV'
   export API_SERVER_KEY=govf25livesvr2f9c8a1b4d70e6392c5a8f1d0e3b6c9a
   export HERMES_API_PORT=8642
   export WEBHOOK_PORT=8644
   ENV
   )
   ```
3. Launch the ONE gateway on the DEFAULT profile. It serves `gov-f25-owner` too
   (the multiplexer is enabled in `fixtures/gov-f25-default/config.yaml`:
   `gateway.multiplex_profiles: true`, allowlist `[gov-f25-owner]`), exposes the
   `gh-pr` webhook route on `:8644` and the force-enabled api_server on `:8642`,
   and — via `HERMES_BUNDLED_PLUGINS=$WT/plugins` — discovers `nv-artifact` for
   BOTH served profiles (each fixture opts it in through `plugins.enabled`;
   `hermes_cli/plugins.py:90`). `PATH="$TB/bin:$PATH"` puts the `gh` stub on the
   gateway's PATH so the owner's tool call is offline:
   ```bash
   ( source $TB/harness.live.env && source $TB/gov-f25.env && cd $WT
     mkdir -p $ART/scenario-AC-GOV-F25-7
     PATH="$TB/bin:$PATH" \
     HERMES_BUNDLED_PLUGINS="$WT/plugins" \
     API_SERVER_KEY="$API_SERVER_KEY" \
       .venv/bin/hermes -p gov-f25-default gateway run \
         > $ART/scenario-AC-GOV-F25-7/gateway.log 2>&1 &
     echo $! > $TB/gov-f25-gateway.pid )
   # readiness: api_server (:8642) AND webhook adapter (:8644) both listening —
   # both /health are unauthenticated (api_server.py:3162; webhook.py:502).
   ( source $TB/gov-f25.env
     timeout 300 bash -c 'until curl -fsS "http://127.0.0.1:${HERMES_API_PORT}/health" >/dev/null 2>&1; do sleep 2; done'
     timeout 120 bash -c 'until curl -fsS "http://127.0.0.1:${WEBHOOK_PORT}/health" >/dev/null 2>&1; do sleep 2; done' )
   ```
   → expect: both curls return before their timeout, and
   `$ART/scenario-AC-GOV-F25-7/gateway.log` carries `API server listening on
   http://127.0.0.1:8642` (`api_server.py:7876`) and `[webhook] Listening on
   127.0.0.1:8644` (`webhook.py:339`). If either never appears, the gateway
   failed to bring up that adapter — capture the log and FAIL here.
4. Confirm the multiplexer actually serves `gov-f25-owner` (so the wake mirror
   exists): the profile-prefix middleware 404s an unserved profile, so a served
   one returns `200` on the unauthenticated `/p/<profile>/v1/health`
   (`api_server.py:2219`, prefix `:2179-2208`):
   ```bash
   ( source $TB/gov-f25.env
     code=$(curl -sS -o /dev/null -w '%{http_code}' \
       "http://127.0.0.1:${HERMES_API_PORT}/p/gov-f25-owner/v1/health")
     echo "owner-mirror-health=$code"
     [ "$code" = "200" ] || { echo "multiplex NOT serving gov-f25-owner (got $code)"; exit 1; } )
   ```
   → expect: `owner-mirror-health=200`. A `404` means `multiplex_profiles`/the
   allowlist is wrong in the default fixture — a scenario/fixture defect, FAIL
   here.
5. Establish the claim through the OWNER's api_server mirror — a real
   (`model: live`) owner turn that runs the `gh` stub, so `post_tool_call`
   claims `gov-f25/repo#41` first-claim-wins for THIS owner session. The bearer
   is the process-env `API_SERVER_KEY` (same key every profile scope resolves):
   ```bash
   ( source $TB/harness.live.env && source $TB/gov-f25.env
     curl -sS -D $ART/scenario-AC-GOV-F25-7/claim-headers.txt \
       -o $ART/scenario-AC-GOV-F25-7/claim-resp.json \
       -w 'claim-http=%{http_code}\n' \
       -X POST "http://127.0.0.1:${HERMES_API_PORT}/p/gov-f25-owner/v1/chat/completions" \
       -H "Authorization: Bearer $API_SERVER_KEY" \
       -H "Content-Type: application/json" \
       --data '{"model":"aws/anthropic/bedrock-claude-opus-4-8","stream":false,"max_tokens":400,"messages":[{"role":"user","content":"Use the terminal tool to run EXACTLY this command, then reply with the pull-request URL it printed and nothing else: gh pr create --title \"feat: gov-f25 sample\" --body \"scenario claim\""}]}' \
       | tee -a $ART/scenario-AC-GOV-F25-7/claim-code.txt
     # The response header carries the session the api_server ran under.
     grep -i '^X-Hermes-Session-Id:' $ART/scenario-AC-GOV-F25-7/claim-headers.txt | tr -d '\r' )
   ```
   → expect: `claim-http=200` and an `X-Hermes-Session-Id` header (the owning
   session). Because the POST is non-streamed it returns only after the turn
   (and its `post_tool_call`) completes, so the claim is committed by the time
   it returns. If the model does not call the tool (no claim row appears in
   step 6), re-POST once with the same directive — a `live:` turn is
   nondeterministic; the tier budget (`/hermes-ui-driver` §1f) covers the retry.
6. Record the baseline against the EXACT owning session — the card's bound
   `tasks.session_id` (`S0`), its pre-delivery message count `N0`, and the
   default profile's session-count baseline — and PERSIST them to
   `$ART/ac7-baseline.json` so step 2 asserts against the recorded session, not
   "the latest". The `tasks` table lives in the shared **kanban.db**
   (`hermes_cli.kanban_db.connect()`), NOT a profile `state.db`; message counts
   live in each profile's `state.db` (stdlib `sqlite3` — the image has no
   `sqlite3` CLI). A bounded wait tolerates a just-committed claim:
   ```bash
   ( source $TB/harness.live.env && cd $WT
     timeout 120 bash -c '
       until python3 - <<PY
   import os, sqlite3, pathlib, sys
   ledger = pathlib.Path(os.environ["HERMES_HOME"]) / "plugin-data" / "nv-artifact" / "data.db"
   if not ledger.exists(): sys.exit(1)
   row = sqlite3.connect(str(ledger)).execute(
       "SELECT profile FROM ownership WHERE repo=? AND pr=?", ("gov-f25/repo", 41)).fetchone()
   sys.exit(0 if row else 1)
   PY
       do sleep 3; done'
     python3 - <<'PY'
   import os, json, sqlite3, pathlib
   import hermes_cli.kanban_db as kb
   home = pathlib.Path(os.environ["HERMES_HOME"])
   ledger = home / "plugin-data" / "nv-artifact" / "data.db"   # ledger_profile=default → fleet-root home
   row = sqlite3.connect(str(ledger)).execute(
       "SELECT task_id, profile FROM ownership WHERE repo=? AND pr=?", ("gov-f25/repo", 41)).fetchone()
   assert row and row[1] == "gov-f25-owner", ("claim missing/wrong owner", row)
   task_id = row[0]
   with kb.connect() as kc:                       # tasks live in kanban.db, not state.db
       s0 = kc.execute("SELECT session_id FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
   assert s0, "owning card has no bound session_id"
   ow = sqlite3.connect(str(home / "profiles" / "gov-f25-owner" / "state.db"))
   n0 = ow.execute("SELECT COUNT(*) FROM messages WHERE session_id=?", (s0,)).fetchone()[0]
   df = sqlite3.connect(str(home / "profiles" / "gov-f25-default" / "state.db"))
   d0 = df.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
   (pathlib.Path(os.environ["ART"]) / "ac7-baseline.json").write_text(
       json.dumps({"task_id": task_id, "S0": s0, "N0": n0, "default_sessions": d0}))
   print("BASELINE", task_id, s0, n0, d0)
   PY
   )
   ```
   → expect: `BASELINE <task> <S0> <N0> <d0>` printed and `ac7-baseline.json`
   written; the claim owner is `gov-f25-owner`.
7. (Corroborating, best-effort — NOT gating; `## Pass` below is the `state.db`
   assertion, which stands with or without this.) Capture the owner session view
   from the dashboard BEFORE the second delivery. Launch a read-only dashboard
   on the SAME `HERMES_HOME` (shared `state.db`), WITHOUT `API_SERVER_KEY` in its
   env so its backend does not contend for `:8642`, per `/hermes-ui-driver`
   §1a/§1c:
   ```bash
   ( source $TB/harness.live.env && cd $WT
     export HERMES_WEB_DIST=/workspace/agent/build/web_dist-<sha7>   # prebuilt SPA (build is network; runs in an Agent OUTSIDE the harness, /hermes-testbed §4)
     env -u API_SERVER_KEY .venv/bin/hermes dashboard --host 127.0.0.1 --port 9119 --no-open --skip-build \
       > $ART/scenario-AC-GOV-F25-7/dashboard.log 2>&1 &
     echo $! > $TB/gov-f25-dashboard.pid
     timeout 180 bash -c "until grep -qE 'HERMES_DASHBOARD_READY port=|Hermes Web UI' $ART/scenario-AC-GOV-F25-7/dashboard.log; do sleep 2; done" || echo "dashboard not up — screenshots skipped, state.db is authoritative"
     if agent-browser open "http://127.0.0.1:9119/sessions" 2>/dev/null; then
       agent-browser wait --load networkidle
       agent-browser snapshot -i                       # select gov-f25-owner in the profile rail, open session S0
       agent-browser screenshot $ART/scenario-AC-GOV-F25-7/step-1.png --full
     fi )
   ```
   → expect: `step-1.png` of the owner session `S0` before the delivery, if the
   dashboard came up. A dashboard failure here does NOT fail the criterion.

## Steps

1. POST a signed SECOND GitHub delivery (a `pull_request_review` for the SAME
   `gov-f25/repo#41`, a DISTINCT `X-GitHub-Delivery`, a valid
   `X-Hub-Signature-256` over the raw body with the `gh-pr` route secret) to the
   default profile's webhook route on the **webhook adapter port** (`:8644`), the
   ADR outline's `/p/gov-f25-default/webhooks/gh-pr` (`webhook.py:298`):
   ```bash
   ( source $TB/harness.live.env && source $TB/gov-f25.env
     SECRET=gov-f25-gh-pr-test-hmac-secret
     BODY='{"action":"submitted","repository":{"full_name":"gov-f25/repo"},"pull_request":{"number":41,"head":{"sha":"beefcafe"}},"review":{"state":"commented","commit_id":"beefcafe"}}'
     SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | sed 's/^.*= //')"
     curl -sS -o $ART/scenario-AC-GOV-F25-7/webhook-resp.txt \
       -w '%{http_code}' \
       -X POST "http://127.0.0.1:${WEBHOOK_PORT}/p/gov-f25-default/webhooks/gh-pr" \
       -H "Content-Type: application/json" \
       -H "X-GitHub-Event: pull_request_review" \
       -H "X-GitHub-Delivery: gov-f25-delivery-2" \
       -H "X-Hub-Signature-256: $SIG" \
       --data "$BODY" > $ART/scenario-AC-GOV-F25-7/webhook-code.txt )
   ```
   → expect: **HTTP 202** (the webhook adapter is fire-and-forget and returns 202
   before the run starts, `gateway/platforms/webhook.py:969-987`). The PR is
   CLAIMED, so the observer durably enqueues the notification and returns
   `{"action":"skip"}` — the default profile mints NO disposable session for
   this delivery.
2. Wait (bounded by `timeout_s`) for the notifier to wake the owner, polling the
   EXACT recorded `S0` message count from `$ART/ac7-baseline.json` (not "the
   latest session"):
   ```bash
   ( source $TB/harness.live.env
     timeout 600 bash -c '
       until python3 - <<PY
   import os, json, sqlite3, pathlib, sys
   b = json.loads((pathlib.Path(os.environ["ART"]) / "ac7-baseline.json").read_text())
   ow = sqlite3.connect(str(pathlib.Path(os.environ["HERMES_HOME"]) / "profiles" / "gov-f25-owner" / "state.db"))
   n = ow.execute("SELECT COUNT(*) FROM messages WHERE session_id=?", (b["S0"],)).fetchone()[0]
   sys.exit(0 if n > b["N0"] else 1)
   PY
       do sleep 5; done'
     # corroborating (best-effort): capture the owner session after the wake
     if [ -f $TB/gov-f25-dashboard.pid ] && agent-browser open "http://127.0.0.1:9119/sessions" 2>/dev/null; then
       agent-browser wait --load networkidle
       agent-browser snapshot -i
       agent-browser screenshot $ART/scenario-AC-GOV-F25-7/step-2.png --full
     fi )
   ```
   → expect: the owning session `S0` gains a NEW turn (`COUNT > N0`) — the real
   model, woken via the notifier's profile-aware wake to
   `/p/gov-f25-owner/v1/chat/completions`, responded IN THE SAME session.

## Pass

The owning session `S0` (the SAME `session_id` recorded at claim time) produced
a new model turn handling the later delivery — the event reached the owner, not
a fresh orphan — AND the default profile spawned no session for it. The pass is
the `state.db`/kanban.db assertion in `## Evidence`; the dashboard screenshots
are corroborating only.

## Evidence

**Authoritative** (decides `## Pass`; stdlib `sqlite3`, no CLI):
- The **202** in `$ART/scenario-AC-GOV-F25-7/webhook-code.txt`.
- `gateway.log` routing lines showing the claim, the claimed-PR SKIP + durable
  enqueue, and the wake self-POST to `/p/gov-f25-owner/v1/chat/completions`
  (`grep -nE 'gov-f25/repo|action.*skip|/p/gov-f25-owner/v1/chat/completions|wake' $ART/scenario-AC-GOV-F25-7/gateway.log`).
- BOTH halves against the persisted baseline — the EXACT recorded `S0`, not the
  latest session:
  ```bash
  ( source $TB/harness.live.env
    python3 - <<'PY'
  import os, json, sqlite3, pathlib
  home = pathlib.Path(os.environ["HERMES_HOME"])
  b = json.loads((pathlib.Path(os.environ["ART"]) / "ac7-baseline.json").read_text())
  ow = sqlite3.connect(str(home / "profiles" / "gov-f25-owner" / "state.db"))
  n1 = ow.execute("SELECT COUNT(*) FROM messages WHERE session_id=?", (b["S0"],)).fetchone()[0]
  df = sqlite3.connect(str(home / "profiles" / "gov-f25-default" / "state.db"))
  d1 = df.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
  print("owner S0", b["S0"], "N0", b["N0"], "N1", n1, "-> woke:", n1 > b["N0"])
  print("default sessions d0", b["default_sessions"], "d1", d1, "-> no orphan:", d1 == b["default_sessions"])
  # last owner turn on S0 (shows it references the delivered review event)
  for r in ow.execute("SELECT role, substr(content,1,160) FROM messages WHERE session_id=? ORDER BY timestamp DESC LIMIT 2", (b["S0"],)):
      print("S0<-", r)
  assert n1 > b["N0"], "owner session S0 did NOT gain a turn"
  assert d1 == b["default_sessions"], "default profile minted a session (orphan)"
  print("AC-GOV-F25-7 PASS")
  PY
  )
  ```
  → the criterion holds when this prints `AC-GOV-F25-7 PASS` (owner `S0` woke,
  default minted no session).
- Live budget check (`/hermes-ui-driver` §1f) summed over both profiles, to stay
  under `LIVE_MODEL_CALLS_MAX`/`LIVE_BUDGET_USD`:
  ```bash
  ( source $TB/harness.live.env
    for p in gov-f25-owner gov-f25-default; do
      python3 -c "import sqlite3,os; d=sqlite3.connect(os.environ['HERMES_HOME']+'/profiles/$p/state.db'); print('$p', *d.execute('select coalesce(sum(api_call_count),0), round(coalesce(sum(case when actual_cost_usd>0 then actual_cost_usd else estimated_cost_usd end),0),4) from session_model_usage').fetchone())"
    done )
  ```

**Corroborating** (best-effort; a dashboard failure does not fail the criterion):
- `step-1.png` (owner session `S0` before the delivery) / `step-2.png` (`S0`
  after, showing the new turn) from the dashboard session view.

**Teardown:**
```bash
( source $TB/gov-f25.env 2>/dev/null
  [ -f $TB/gov-f25-dashboard.pid ] && kill "$(cat $TB/gov-f25-dashboard.pid)" 2>/dev/null
  [ -f $TB/gov-f25-gateway.pid ] && kill "$(cat $TB/gov-f25-gateway.pid)" 2>/dev/null
  true )
```
