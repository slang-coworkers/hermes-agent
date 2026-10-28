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
ONE multiplexer gateway (never a2a, no second `serve`); the recipient's reply is
visible in the sender's Bot Chat. `model: live` — a real model answers behind the
OneCLI proxy (dummy `ANTHROPIC_API_KEY`, real base URL). Budget bound per
`/hermes-ui-driver` §1f: `LIVE_MODEL_CALLS_MAX=40`, `LIVE_BUDGET_USD=5`.

The oracle is a **distinct acknowledgement marker**, not the nonce itself: bot-a's
message carries only the nonce `$NONCE`, and bot-b (per its SOUL) replies with
`F35-ACK:<nonce>`. The `F35-ACK:` prefix never appears in the sender's prompt, so
observing `F35-ACK:$NONCE` proves bot-b actually generated the reply — a bare
`$NONCE` match would be satisfied by the sender's own message.

## Setup

The tester installs the `fixtures:` profiles (`loop-f35-bot-a`, `loop-f35-bot-b`)
before this section. Both carry the live provider block in their `config.yaml`.
Everything below is post-install:

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
   `multiplex_profile_allowlist: [loop-f35-bot-a, loop-f35-bot-b]`
   (`/hermes-ui-driver` §1a launch; wait for the `HERMES_DASHBOARD_READY port=9119|Hermes Web UI` line).
   Both bots run as profiles OF THIS gateway via the multiplexer — there is no second
   `serve` process, because `message_agent` delivers inside the one gateway.
3. Write each bot's Bot-Mode identity and materialise its canonical Bot Chat by
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
2. In bot-a's Bot Chat, instruct bot-a to message bot-b via `message_agent`, carrying
   the nonce (NOT the ack marker), then wait for the DISTINCT ack to return:
   ```bash
   agent-browser fill @e<composer> "Use message_agent to send loop-f35-bot-b exactly: \"$NONCE please reply\". Then report bot-b's reply back to me."
   agent-browser press Enter
   agent-browser screenshot $ART/scenario-AC-LOOP-F35-5/step-2.png --full
   timeout 600 agent-browser wait --text "F35-ACK:$NONCE"   # bot-b's generated ack, absent from the sender's prompt
   ```
   → expect: `message_agent` delivers to `loop-f35-bot-b` inside the one gateway, bot-b
   runs a turn and replies `F35-ACK:$NONCE`, and that ack returns into bot-a's Bot Chat.
3. Read bot-a's Bot Chat transcript and confirm bot-b's ack landed back with the sender:
   ```bash
   agent-browser snapshot -i
   agent-browser screenshot $ART/scenario-AC-LOOP-F35-5/step-3.png --full
   ```
   → expect: `F35-ACK:$NONCE` is visible in bot-a's Bot Chat.

## Pass

The criterion holds when `message_agent` carries the message `loop-f35-bot-a` →
`loop-f35-bot-b` inside the one gateway (never a2a, no second `serve`), bot-b generates
`F35-ACK:$NONCE` (the distinct marker, proving bot-b replied), that ack is visible in
bot-a's Bot Chat UI, AND bot-b's own `state.db` holds an `assistant` row containing
`F35-ACK:$NONCE`.

## Evidence

- `step-1.png`, `step-2.png`, `step-3.png` under `$ART/scenario-AC-LOOP-F35-5/`.
- Bot-b actually generated the ack — an `assistant` row in bot-b's own `state.db`
  containing `F35-ACK:$NONCE` (stdlib sqlite3 — the image has no `sqlite3` CLI):
  ```bash
  python3 -c "import sqlite3, os; d = sqlite3.connect(os.environ['HERMES_HOME'] + '/profiles/loop-f35-bot-b/state.db'); rows=[r for r in d.execute(\"select m.role, substr(m.content,1,160) from messages m where m.role='assistant' and m.content like 'F35-ACK:$NONCE%' order by m.timestamp desc limit 5\")]; assert rows, 'no bot-b assistant F35-ACK:$NONCE row'; [print(r) for r in rows]"
  ```
- Budget check across both bots (must stay under `LIVE_MODEL_CALLS_MAX=40` / `LIVE_BUDGET_USD=5`):
  ```bash
  python3 -c "import sqlite3, os; d = sqlite3.connect(os.environ['HERMES_HOME'] + '/profiles/loop-f35-bot-b/state.db'); print(*d.execute('select coalesce(sum(api_call_count),0), round(coalesce(sum(case when actual_cost_usd > 0 then actual_cost_usd else estimated_cost_usd end),0),4) from session_model_usage').fetchone())"
  ```
