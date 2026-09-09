---
ac: AC-LOOP-F35-5
kind: live
model: live
base_url: https://inference-api.nvidia.com
model_id: aws/anthropic/bedrock-claude-opus-4-8
fixtures:
  - fixtures/loop-f35-bot-a
  - fixtures/loop-f35-bot-b
timeout_s: 600
---

# AC-LOOP-F35-5 — two rendered bots exchange a message via message_agent inside one gateway

Two bot-mode-managed profiles exchange a message through `message_agent` inside the
ONE multiplexer gateway (never a2a); the recipient's nonce-bearing reply is visible
in the sender's Bot Chat. `model: live` — a real model answers behind the OneCLI
proxy (dummy `ANTHROPIC_API_KEY`, real base URL). Budget bound per `/hermes-ui-driver`
§1f: `LIVE_MODEL_CALLS_MAX=40`, `LIVE_BUDGET_USD=5`.

## Setup

The tester installs the `fixtures:` profiles (`loop-f35-bot-a`, `loop-f35-bot-b`)
before this section. Both carry the live provider block in their `config.yaml`.
Everything below is post-install:

1. Boot the ONE gateway/dashboard on `127.0.0.1:9119` with, on the DEFAULT profile,
   `gateway.multiplex_profiles: true` and
   `multiplex_profile_allowlist: [loop-f35-bot-a, loop-f35-bot-b]`
   (`/hermes-ui-driver` §1a launch; wait for the `HERMES_DASHBOARD_READY port=9119|Hermes Web UI` line).
2. Write each bot's Bot-Mode identity and materialise its canonical Bot Chat by
   running the plugin's onboard against the shipped spec, over the loopback WS
   (standalone path, so it reaches the running gateway):
   ```bash
   ( source $TB/harness.live.env && cd $WT
     hermes onboard coworker tests/e2e-scenarios/loop-f35/spec/coworker-types.yaml \
       --gateway-url "ws://127.0.0.1:9119/api/ws?token=$GATEWAY_TOKEN" \
       > $ART/scenario-AC-LOOP-F35-5/onboard.log 2>&1 ; echo onboard_exit=$? )
   ```
   This dispatches `profiles.configure` (ui_meta['hermes-bots'] per-key CAS) and the
   `session.list`/`session.create`/`session.title` Bot Chat lifecycle for each bot.
   The force-install rewrites the identical live-provider config, so the model is
   unchanged. `$GATEWAY_TOKEN` is the loopback session token the testbed holds.
3. Start bot-b as the peer endpoint the message is delivered to (ports 9120–9129 are
   the scenario-peer range, `/hermes-ui-driver` §1f):
   ```bash
   ( source $TB/harness.live.env && cd $WT
     hermes -p loop-f35-bot-b serve --host 127.0.0.1 --port 9120 --isolated \
       > $ART/scenario-AC-LOOP-F35-5/peer.log 2>&1 & )
   timeout 300 bash -c "until grep -qE 'HERMES_BACKEND_READY port=9120|Hermes backend listening on 127.0.0.1:9120' $ART/scenario-AC-LOOP-F35-5/peer.log; do sleep 3; done"
   ```
4. Mint a nonce for this run: `NONCE="f35-$(date +%s)"`.

## Steps

1. Open bot-a's Bot Chat in the dashboard and confirm the active profile is bot-a
   (the SPA opens the DEFAULT profile by default — select `loop-f35-bot-a` in the
   profile rail first, `/hermes-ui-driver` §1d, and confirm the composer prompt names
   bot-a):
   ```bash
   agent-browser open "http://127.0.0.1:9119/chat"
   agent-browser wait --load networkidle
   agent-browser snapshot -i                     # select loop-f35-bot-a in the rail by @ref, then its Bot Chat
   agent-browser screenshot $ART/scenario-AC-LOOP-F35-5/step-1.png --full
   ```
   → expect: bot-a's Bot Chat is open and the composer is enabled for `loop-f35-bot-a`.
2. In bot-a's Bot Chat, instruct bot-a to delegate to bot-b via `message_agent`,
   carrying the nonce, then wait for bot-a's turn to complete:
   ```bash
   agent-browser fill @e<composer> "Use message_agent to send loop-f35-bot-b exactly: \"$NONCE please reply\". Then report bot-b's reply back to me."
   agent-browser press Enter
   agent-browser screenshot $ART/scenario-AC-LOOP-F35-5/step-2.png --full
   timeout 600 agent-browser wait --text "$NONCE"   # bot-a's turn shows the delegation/return carrying the nonce
   ```
   → expect: the `message_agent` tool call to `loop-f35-bot-b` returns delivered
   (the gate is satisfied — Bot Chat session + `is_bot_mode_managed`).
3. Read bot-a's Bot Chat transcript and confirm bot-b's nonce-bearing reply landed
   back with the sender:
   ```bash
   agent-browser snapshot -i
   agent-browser screenshot $ART/scenario-AC-LOOP-F35-5/step-3.png --full
   ```
   → expect: bot-b's reply, naming `$NONCE`, is visible in bot-a's Bot Chat.

## Pass

The criterion holds when `$NONCE` crosses `loop-f35-bot-a` → `loop-f35-bot-b` via
`message_agent` inside the one gateway (never a2a) and bot-b's reply naming `$NONCE`
is visible in bot-a's Bot Chat UI.

## Evidence

- `step-1.png`, `step-2.png`, `step-3.png` under `$ART/scenario-AC-LOOP-F35-5/`.
- The nonce inbound row in bot-b's own `state.db` (stdlib sqlite3 — the image has no
  `sqlite3` CLI):
  ```bash
  python3 -c "import sqlite3, os; d = sqlite3.connect(os.environ['HERMES_HOME'] + '/profiles/loop-f35-bot-b/state.db'); [print(r) for r in d.execute(\"select s.id, s.source, m.role, substr(m.content,1,120) from messages m join sessions s on s.id = m.session_id where m.content like '%f35-%' order by m.timestamp desc limit 5\")]"
  ```
- Budget check across both bots (must stay under `LIVE_MODEL_CALLS_MAX=40` / `LIVE_BUDGET_USD=5`):
  ```bash
  python3 -c "import sqlite3, os; d = sqlite3.connect(os.environ['HERMES_HOME'] + '/profiles/loop-f35-bot-b/state.db'); print(*d.execute('select coalesce(sum(api_call_count),0), round(coalesce(sum(case when actual_cost_usd > 0 then actual_cost_usd else estimated_cost_usd end),0),4) from session_model_usage').fetchone())"
  ```
