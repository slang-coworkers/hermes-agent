---
ac: AC-LOOP-F35-5
kind: live
model: live
base_url: https://inference-api.nvidia.com
model_id: aws/anthropic/bedrock-claude-opus-4-8
fixtures:
  - fixtures/a2a-f21-loopf35-a
  - fixtures/a2a-f21-loopf35-b
timeout_s: 600
---

# AC-LOOP-F35-5 — two rendered bots exchange a message via message_agent inside one gateway

Two Bot-Mode-managed profiles exchange a message through `message_agent` inside the
ONE multiplexer gateway (never a2a, no second `serve`); the recipient's reply is
visible in the sender's Bot Chat. `model: live` — a real model answers behind the
OneCLI proxy (dummy `api_key`, real base URL). This criterion is carried from
LOOP-F35 (operator ruling) under its original id; it ships here as a self-contained
copy under `tests/e2e-scenarios/A2A-F21/`, NOT an edit of the merged `loop-f35`
files. Budget bound per `/hermes-ui-driver` §1f: `LIVE_MODEL_CALLS_MAX=40`,
`LIVE_BUDGET_USD=5`.

The oracle is a **distinct acknowledgement marker**, not the nonce itself: bot-a's
message carries only the nonce `$NONCE`, and bot-b (per its identity) replies with
`F35-ACK:<nonce>`. The `F35-ACK:` prefix never appears in the sender's prompt, so
observing `F35-ACK:$NONCE` proves bot-b actually generated the reply — a bare
`$NONCE` match would be satisfied by the sender's own message.

**What this scenario proves beyond delivery: the provider config survives `hermes
onboard`.** The earlier LOOP-F35 live FAIL was `hermes onboard` force-rendering the
recipient from a spine that carried only the bare `api:`/`key_env:` shape, clobbering
the fixtures' working `providers.coworkers-live` block into that bare shape → the
recipient's `message_agent` delivery subprocess (whose env is stripped of
`ANTHROPIC_*`) then resolved no provider and 404'd. The fix is entirely under
`tests/**`: this scenario's own spine (`spec/spines/live-base.yaml`) carries the FULL
`providers.coworkers-live` block with an inline dummy `api_key` and the literal remote
`base_url`, so the deep-merged render leaves a working block on disk after onboard.
The §Setup regression oracle reads the POST-onboard config to prove it survived.

## Setup

The tester installs the `fixtures:` profiles (`a2a-f21-loopf35-a`,
`a2a-f21-loopf35-b`) before this section; do not re-install them here. Both carry
the full live provider block in their `config.yaml`. `$SCN=$WT/tests/e2e-scenarios/A2A-F21`,
`AC=AC-LOOP-F35-5`, `mkdir -p $ART/scenario-$AC`. Everything below is post-install,
with the **live** env sourced (`$TB/harness.live.env`; the OneCLI egress proxy
substitutes the real credential at the `inference-api.nvidia.com` hop):

1. Enable the opt-in plugin in the testbed home so its `onboard coworker` CLI verb
   exists (the plugin is not bundled; the `fixtures:` install does not enable it):
   ```bash
   ( source $TB/harness.live.env
     mkdir -p "$HERMES_HOME/plugins"
     cp -R "$WT/plugins/nv-coworker-compose" "$HERMES_HOME/plugins/"
     hermes plugins enable nv-coworker-compose
     hermes onboard --help | grep -F coworker )   # must print the `coworker` verb, exit 0
   ```
2. Boot the ONE gateway/dashboard on `127.0.0.1:9119` with, on the DEFAULT profile,
   `gateway.multiplex_profiles: true` and
   `multiplex_profile_allowlist: [a2a-f21-loopf35-a, a2a-f21-loopf35-b]`
   (`/hermes-ui-driver` §1a launch; wait for the
   `HERMES_DASHBOARD_READY port=9119|Hermes Web UI` line). Both bots run as profiles
   OF THIS gateway via the multiplexer — there is no second `serve` process, because
   `message_agent` delivers inside the one gateway. The sender turn is driven via the
   CLI canonical `Bot Chat` path (Step 1), NOT the web composer (which opens a fresh
   non-`Bot Chat` session where `message_agent` is absent — `tools/bot_mode_dm.py:152`,
   the round-1 defect); the dashboard is used only to OBSERVE the ack by opening the
   sender's canonical `Bot Chat` via exact-resume (Step 2).
3. Write each bot's Bot-Mode identity and materialise its canonical Bot Chat by
   running the plugin's onboard against THIS scenario's spec, over the loopback WS
   (standalone path, so it reaches the running gateway):
   ```bash
   ( source $TB/harness.live.env && cd $WT
     hermes onboard coworker tests/e2e-scenarios/A2A-F21/spec/coworker-types.yaml \
       --gateway-url "ws://127.0.0.1:9119/api/ws?token=$GATEWAY_TOKEN" \
       > $ART/scenario-AC-LOOP-F35-5/onboard.log 2>&1 ; echo onboard_exit=$? )
   ```
   This dispatches `profiles.configure` (ui_meta['hermes-bots'] per-key CAS) and the
   `session.list`/`session.create`/`session.title` Bot Chat lifecycle for each bot,
   and force-replaces each `SOUL.md` from the spec `identity:` (which is why bot-b's
   ack protocol lives in its spec identity, not only in its fixture SOUL.md).
   `$GATEWAY_TOKEN` is the loopback session token the testbed holds.
4. Mint a nonce for this run: `export NONCE="f35-$(date +%s)"` (exported so the subshell
   `python3 -c` reads it via `os.environ`).
5. **Regression oracle for the onboard-clobber fix.** Confirm each profile's
   POST-onboard `config.yaml` STILL carries `providers.coworkers-live` with the
   literal remote `base_url` — exactly what the clobber destroyed before the spine
   fix (PyYAML ships with Hermes; the image has no yaml CLI):
   ```bash
   ( source $TB/harness.live.env && python3 -c "
   import yaml, os
   home = os.environ['HERMES_HOME']
   for bot in ('a2a-f21-loopf35-a', 'a2a-f21-loopf35-b'):
       c = yaml.safe_load(open(home + '/profiles/' + bot + '/config.yaml'))
       prov = (c.get('providers') or {}).get('coworkers-live') or {}
       expected = {'base_url': 'https://inference-api.nvidia.com', 'api_mode': 'anthropic_messages', 'api_key': 'dummy-a2a-f21-not-a-real-key', 'default_model': 'aws/anthropic/bedrock-claude-opus-4-8'}
       assert all(prov.get(k) == v for k, v in expected.items()), (bot, 'providers.coworkers-live did NOT survive onboard: ' + repr(prov))
       assert 'key_env' not in prov, (bot, 'provider regressed to key_env: ' + repr(prov))
       model = c.get('model') or {}
       assert model.get('provider') == 'coworkers-live', (bot, 'model.provider lost: ' + repr(model))
       assert model.get('default') == 'aws/anthropic/bedrock-claude-opus-4-8', (bot, 'model.default lost: ' + repr(model))
       print(bot, 'post-onboard full providers.coworkers-live survived:', repr(prov))" )
   ```
6. **PREFLIGHT (no model call, before Step 1 — orchestrator P3 requirement): prove the
   exact-resume surface actually renders.** Read `a2a-f21-loopf35-a`'s canonical `Bot Chat`
   session id (`title='Bot Chat'`, `hidden=1`), seed a sentinel `assistant` row into it via
   stdlib `sqlite3` (NO model call), open it by exact-resume, and confirm the sentinel
   RENDERS. Then delete the sentinel and drop the PTY attach token so Step 2 spawns a fresh
   resume rather than reattaching this preflight session.
   ```bash
   SID=$( source $TB/harness.live.env && python3 -c "import sqlite3, os; d=sqlite3.connect(os.environ['HERMES_HOME']+'/profiles/a2a-f21-loopf35-a/state.db'); r=d.execute(\"select id from sessions where title='Bot Chat' and hidden=1 order by started_at desc limit 1\").fetchone(); assert r, 'no hidden Bot Chat session for a2a-f21-loopf35-a'; print(r[0])" ) || { echo "PREFLIGHT FAILED — no hidden Bot Chat session for a2a-f21-loopf35-a; STOP" >&2; exit 1; }
   export SID
   SENTINEL_ROWID=$( source $TB/harness.live.env && python3 -c "import sqlite3, os, time; d=sqlite3.connect(os.environ['HERMES_HOME']+'/profiles/a2a-f21-loopf35-a/state.db'); cur=d.execute(\"insert into messages (session_id, role, content, timestamp, active) values (?, 'assistant', ?, ?, 1)\", (os.environ['SID'], 'PREFLIGHT-'+os.environ['NONCE'], time.time())); d.commit(); print(cur.lastrowid)" ) || { echo "PREFLIGHT FAILED — could not seed sentinel row; STOP" >&2; exit 1; }
   export SENTINEL_ROWID
   agent-browser open "http://127.0.0.1:9119/chat?profile=a2a-f21-loopf35-a&resume=$SID"
   agent-browser wait --load networkidle
   if ! timeout 120 agent-browser wait --text "PREFLIGHT-$NONCE"; then
     agent-browser screenshot "$ART/scenario-AC-LOOP-F35-5/preflight-failed.png" --full || true
     echo "PREFLIGHT FAILED — exact-resume did not render the seeded sentinel; STOP, do not run Step 1, bounce to hermes-architect" >&2
     exit 1
   fi
   agent-browser screenshot $ART/scenario-AC-LOOP-F35-5/preflight.png --full
   ( source $TB/harness.live.env && python3 -c "import sqlite3, os; d=sqlite3.connect(os.environ['HERMES_HOME']+'/profiles/a2a-f21-loopf35-a/state.db'); cur=d.execute('delete from messages where id=?', (int(os.environ['SENTINEL_ROWID']),)); d.commit(); assert cur.rowcount == 1, ('sentinel delete removed %d rows' % cur.rowcount); print('deleted sentinel', os.environ['SENTINEL_ROWID'])" ) || { echo "PREFLIGHT FAILED — sentinel cleanup did not remove exactly one row; STOP, do not run Step 1" >&2; exit 1; }
   agent-browser eval "localStorage.removeItem('hermes.pty.token.chat')"
   agent-browser open about:blank
   ```
   Sentinel renders → exact-resume is proven; delete done, proceed to Step 1. Sentinel does
   NOT render (`No sessions yet`/404) → STOP: do not run Step 1, do not spend the authorized
   round; bounce to hermes-architect with `preflight-failed.png`, and the architect escalates
   to the orchestrator for a dashboard CORE-CHANGE or an operator ADR bar-change. `state.db`/CLI
   never substitute for this render proof.

## Steps

1. **Drive `a2a-f21-loopf35-a`'s sender turn in its canonical `Bot Chat` via the CLI** —
   the session titled exactly `Bot Chat` is the only one where `message_agent` is injected
   (`tools/bot_mode_dm.py:152` gates the tool on that title). The web-dashboard composer
   opens a NEW auto-titled session where `message_agent` is absent (returns
   `Tool 'message_agent' does not exist`) — that was the round-1 defect; do NOT use it.
   ```bash
   ( source $TB/harness.live.env
     hermes -p a2a-f21-loopf35-a chat -c "Bot Chat" --create-if-missing -Q \
       -q "Use message_agent to send a2a-f21-loopf35-b exactly: \"$NONCE please reply\". Then report bot-b's reply back to me." \
       > $ART/scenario-AC-LOOP-F35-5/sender-cli.log 2>&1 ; echo sender_exit=$? )
   ```
   (`-q`/`--query` is the prompt; `-Q` is the boolean quiet flag; `-c "Bot Chat"
   --create-if-missing` resumes the onboard-created canonical Bot Chat, creating one only
   if absent.) → expect: `message_agent` delivers to `a2a-f21-loopf35-b` inside the one
   gateway (never a2a, no second `serve`); bot-b runs a turn and replies `F35-ACK:$NONCE`;
   that ack round-trips back into `a2a-f21-loopf35-a`'s `Bot Chat` `state.db` as the
   background-completion notification.
2. **Observe the ack in the web UI by exact-resume** (this replaces the round-1 composer
   path, which was the defect). Read `a2a-f21-loopf35-a`'s canonical `Bot Chat` session id
   from its `state.db` (the session titled exactly `Bot Chat`), then open it BY ID in the
   dashboard — the by-id resume route renders the hidden canonical session even though the
   session *list* excludes it (`web_routers/sessions.py:596-616,642-693` apply no hidden
   filter; `ChatPage.tsx:350-426,1164-1186` → `/api/pty`):
   ```bash
   SID=$( source $TB/harness.live.env && python3 -c "import sqlite3, os; d=sqlite3.connect(os.environ['HERMES_HOME']+'/profiles/a2a-f21-loopf35-a/state.db'); r=d.execute(\"select id from sessions where title='Bot Chat' and hidden=1 order by started_at desc limit 1\").fetchone(); assert r, 'no hidden Bot Chat session for a2a-f21-loopf35-a'; print(r[0])" ) || exit 1
   export SID   # the exact hidden Bot Chat id the screenshot renders and the Evidence asserts
   agent-browser open "http://127.0.0.1:9119/chat?profile=a2a-f21-loopf35-a&resume=$SID"
   agent-browser wait --load networkidle
   timeout 600 agent-browser wait --text "F35-ACK:$NONCE"   # bot-b's generated ack, absent from the sender's prompt
   agent-browser screenshot $ART/scenario-AC-LOOP-F35-5/step-2.png --full
   ```
   → expect: `a2a-f21-loopf35-a`'s canonical `Bot Chat` transcript renders at
   `/chat?profile=a2a-f21-loopf35-a&resume=$SID` with `F35-ACK:$NONCE` visible.

## Pass

The criterion holds (BOTH halves, per `/hermes-ui-driver` §1f) when `message_agent`
carries `a2a-f21-loopf35-a` → `a2a-f21-loopf35-b` inside the one gateway (never a2a, no
second `serve`), `a2a-f21-loopf35-b` generates `F35-ACK:$NONCE` (the distinct marker
proving it replied), AND both halves hold:

- **(UI half)** `F35-ACK:$NONCE` renders in `a2a-f21-loopf35-a`'s canonical `Bot Chat` in
  the web dashboard, opened by the exact-resume route
  `/chat?profile=a2a-f21-loopf35-a&resume=<its Bot Chat session id>` (Step 2) — a captured
  `step-2.png` screenshot.
- **(state half)** `a2a-f21-loopf35-b`'s own `state.db` holds an `assistant` row containing
  `F35-ACK:$NONCE` AND the ack round-tripped into `a2a-f21-loopf35-a`'s `Bot Chat`
  `state.db`; and the §Setup regression oracle showed `providers.coworkers-live` survived
  `hermes onboard` for both bots (proving delivery worked because the provider block was
  not clobbered).

The sender turn is driven via CLI (Step 1, where `message_agent` injects) and the ack is
OBSERVED via web exact-resume (Step 2) — never the web composer (the round-1 defect: it
opens a non-`Bot Chat` session). Neither half may be dropped nor a different surface
substituted.

**Hard-evidence bar (orchestrator P3).** The screenshot must show `F35-ACK:$NONCE` from the
exact sender session selected by `title='Bot Chat' AND hidden=1` (the same id used in the
`resume=<id>` URL); that same `F35-ACK:$NONCE` must occur in BOTH profiles' `state.db` rows;
the complete post-onboard `providers.coworkers-live` oracle (§Setup step 5) is mandatory;
and `state.db`/CLI/REST output may corroborate but never replace the screenshot.

## Evidence

- **(UI half — the bar; not substitutable)** `step-2.png` under `$ART/scenario-AC-LOOP-F35-5/`
  — an agent-browser screenshot of `a2a-f21-loopf35-a`'s canonical `Bot Chat` (the session with
  `title='Bot Chat'`, `hidden=1`, the same id used in the `resume=<id>` URL) showing the exact
  `F35-ACK:$NONCE`. `state.db`/CLI/REST corroborate only — they never substitute for this screenshot.
- **(state half)** the post-onboard config-survival read from §Setup step 5 (the regression
  oracle) — its stdout must show both bots' full `providers.coworkers-live` survived.
- **(state half)** a `python3 -c` read (stdlib sqlite3 — the image has no `sqlite3` CLI) of
  BOTH profiles' `state.db`: (a) `a2a-f21-loopf35-b` generated the ack, and (b) the ack
  round-tripped into `a2a-f21-loopf35-a`'s `Bot Chat` session:
  ```bash
  python3 -c "
  import sqlite3, os
  home = os.environ['HERMES_HOME']
  db_b = sqlite3.connect(home + '/profiles/a2a-f21-loopf35-b/state.db')
  rows_b = [r for r in db_b.execute(\"select role, substr(content,1,160) from messages where role='assistant' and instr(content, 'F35-ACK:$NONCE') > 0 order by timestamp desc limit 5\")]
  assert rows_b, 'no bot-b assistant F35-ACK:$NONCE row'
  sid = os.environ['SID']
  db_a = sqlite3.connect(home + '/profiles/a2a-f21-loopf35-a/state.db')
  meta = db_a.execute(\"select id, title, hidden from sessions where id=? and title='Bot Chat' and hidden=1\", (sid,)).fetchone()
  assert meta, 'exported SID is not the hidden Bot Chat session: ' + sid
  rows_a = [r for r in db_a.execute(\"select role, substr(content,1,160) from messages where session_id=? and instr(content, 'F35-ACK:$NONCE') > 0 order by timestamp desc limit 5\", (sid,))]
  assert rows_a, 'ack did not round-trip into bot-a hidden Bot Chat session ' + sid
  print('bot-a session meta (same id as the resume URL):', meta)
  print('bot-b generated:', rows_b)
  print('bot-a round-trip:', rows_a)"
  ```
- **Correlation (the P3 bar):** the SAME `$NONCE` value appears in the `step-2.png` screenshot
  AND in both profiles' `state.db` rows above AND the complete `providers.coworkers-live` block
  survived onboard (§Setup step 5) — one nonce tying the UI screenshot, both `state.db` states,
  and the surviving provider config together.
- The round did not run away — summed across BOTH bots' sessions (sender and recipient
  each make model calls), the model-call count is non-zero and both it and cost stay
  under the live caps (`LIVE_MODEL_CALLS_MAX=40` / `LIVE_BUDGET_USD=5`):
  ```bash
  python3 -c "
  import sqlite3, os
  home = os.environ['HERMES_HOME']
  calls = 0; cost = 0.0
  for bot in ('a2a-f21-loopf35-a', 'a2a-f21-loopf35-b'):
      db = home + '/profiles/' + bot + '/state.db'
      if not os.path.exists(db):
          continue
      c, u = sqlite3.connect(db).execute('select coalesce(sum(api_call_count),0), coalesce(sum(case when actual_cost_usd > 0 then actual_cost_usd else estimated_cost_usd end),0) from session_model_usage').fetchone()
      calls += c or 0; cost += u or 0
  cost = round(cost, 4)
  print('both bots: calls=', calls, 'cost=', cost)
  assert 0 < calls < 40 and cost < 5, (calls, cost)"
  ```

## Env fallback (does not count against the round caps, per "FAIL (env) and ESCALATE never count")

If `message_agent` delivery still fails for an environmental reason distinct from the
onboard clobber this fix addresses (for example the bare-`hermes`-PATH delivery issue
of `tools/bot_mode_dm.py:364` recurs), the tester records `FAIL(env)`/`ESCALATE` with
the §Setup step 5 post-onboard config read attached as evidence of whether the
provider block survived — separating the fix under test (the block survives onboard)
from any residual harness gap. The tester must not rewrite the spec or fixtures to
force a pass.
