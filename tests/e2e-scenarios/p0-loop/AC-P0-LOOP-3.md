---
ac: AC-P0-LOOP-3
kind: live
model: live
base_url: "${ANTHROPIC_BASE_URL}"
model_id: "aws/anthropic/bedrock-claude-opus-4-8[1m]"
fixtures:
  - fixtures/p0-loop-bot-a
  - fixtures/p0-loop-bot-b
timeout_s: 600
---

# AC-P0-LOOP-3 — two profiles greet each other over a2a

`p0-loop-bot-a`, driven through the live tier, uses `a2a_call` to ask
`p0-loop-bot-b` to reply with a short hello carrying a nonce marker; bot-b's
reply is persisted in its own `state.db` under a `source='a2a'` session and is
rendered back in bot-a's dashboard. This proves the a2a **round trip +
visibility** (profile-naming itself is proved by AC-1 and AC-2).

**Live tier / provider (round-2 amendment).** `kind: live` + `model: live`
select the OneCLI live tier: the tester sources `$TB/harness.live.env` (not
`harness.env`), which drops the proxy pin back to the container's OneCLI proxy
and allow-lists the resolved inference host. Both fixtures configure a custom
provider `coworkers-live` pointing at the coworkers' OWN gateway through that
proxy — `api: "${ANTHROPIC_BASE_URL}"`, `api_mode: anthropic_messages`,
`key_env: ANTHROPIC_API_KEY`, model `aws/anthropic/bedrock-claude-opus-4-8[1m]`
(also `model.default`, so the config is authoritative for the run). `api` is an
env reference expanded on every `load_config()`
(`hermes_cli/config.py:2931-2945`, applied `:4042-4043`); the value is
**container-specific** (a coworkers'-gateway host in one container, a localhost
proxy port in another), so the fixture commits the literal `${ANTHROPIC_BASE_URL}`
and the tester's live env supplies the real value at runtime.
`anthropic_messages` is a valid api_mode (`hermes_cli/providers.py:38,470`). The
local OneCLI proxy substitutes the outbound `x-api-key` for that host, so
`ANTHROPIC_API_KEY` stays a dummy and no real key ever touches
`$TB`/`$WT`/`$ART`. Budget bound: `LIVE_MODEL_CALLS_MAX=40` /
`LIVE_BUDGET_USD=5` for this scenario — past either, stop and record
`FAIL(budget)`.

The a2a transport, round trip, and `state.db` recording are independently proven
**hermetically** by the T-suite (T5: bot-b `state.db` gains a `source='a2a'`
session with the inbound nonce and the assistant reply, no live model), so this
criterion adds only the **live-model** leg (bot-a autonomously issuing
`a2a_call`, and bot-b generating its reply from a real model).

## Setup

The tester installs the `fixtures:` list itself
(`hermes profile install tests/e2e-scenarios/p0-loop/fixtures/p0-loop-bot-a --name p0-loop-bot-a -y`
and the same for `p0-loop-bot-b`) before this section runs; do not re-install
them here. After install, with `$HERMES_HOME` = the shared testbed home:

1. The empty `HERMES_BUNDLED_PLUGINS` dir means each profile owns its plugins,
   so copy both plugins from the worktree into **each** profile (a2a copied
   flat as `a2a`, so its discovery key is the manifest name `a2a-platform`), and
   write the dummy key post-install (`.env` is user-owned and stripped on
   install — the OneCLI proxy substitutes the real credential, so a dummy is all
   Hermes ever holds):
   ```bash
   for b in p0-loop-bot-a p0-loop-bot-b; do
     mkdir -p "$HERMES_HOME/profiles/$b/plugins"
     cp -r "$WT/plugins/hello"          "$HERMES_HOME/profiles/$b/plugins/"
     cp -r "$WT/plugins/platforms/a2a"  "$HERMES_HOME/profiles/$b/plugins/a2a"
     printf 'ANTHROPIC_API_KEY=dummy\n' >> "$HERMES_HOME/profiles/$b/.env"
   done
   ```
   Each installed `config.yaml` already sets `plugins.enabled: [hello, a2a-platform]`.
   `${ANTHROPIC_BASE_URL}` is supplied by `$TB/harness.live.env`.
2. **bot-b (inbound peer).** Its `config.yaml` already sets
   `platforms.a2a.enabled: true` and `platforms.a2a.extra.port: 9120` (loopback;
   no `extra.agents`, so the default agent answers the root url). Start bot-b's
   gateway inside the harness (port 9120 is in the scenario-peer range
   9120–9129; the dashboard keeps 9119):
   ```bash
   ( source $TB/harness.live.env && cd $WT
     hermes -p p0-loop-bot-b gateway run -v --force --no-supervise \
       > $ART/scenario-AC-P0-LOOP-3/peer.log 2>&1 & )
   timeout 300 bash -c "until grep -qF 'A2A: serving Agent Card + JSON-RPC on http://127.0.0.1:9120' \
     $ART/scenario-AC-P0-LOOP-3/peer.log; do sleep 3; done"
   ```
   (`-v` raises stderr to INFO — `hermes_cli/subcommands/gateway.py:51-55`; the
   readiness line is logged at INFO — `plugins/platforms/a2a/adapter.py:444-446`;
   a2a listener binds `http://127.0.0.1:9120` — `adapter.py:346`.)
3. **bot-a (driver).** Its `config.yaml` already names the peer
   (`a2a_agents.bot-b.url: http://127.0.0.1:9120`). Launch bot-a's dashboard
   with the `a2a` toolset pinned so `a2a_call` reaches bot-a's model (the pin is
   validated after `discover_plugins()` and bypasses the coding-posture
   collapse — `tui_gateway/server.py:6012-6076`; the no-pin fallback reads
   `platform_toolsets.cli`, not `.a2a`, `:6132-6155`):
   ```bash
   ( source $TB/harness.live.env && cd $WT
     export HERMES_WEB_DIST=<prebuilt SPA dist>
     export HERMES_TUI_TOOLSETS=hermes-cli,a2a
     hermes -p p0-loop-bot-a dashboard --host 127.0.0.1 --port 9119 \
       --no-open --isolated --skip-build > $ART/scenario-AC-P0-LOOP-3/dashboard.log 2>&1 & )
   timeout 300 bash -c "until grep -qE 'HERMES_DASHBOARD_READY port=|Hermes Web UI' \
     $ART/scenario-AC-P0-LOOP-3/dashboard.log; do sleep 2; done"
   ```
4. Mint the nonce for this run: `NONCE=$(python3 -c 'import secrets; print(secrets.token_hex(4))')`
   — the marker is `P0LOOP-$NONCE`.

## Steps

The dashboard SPA opens the **default** profile's chat regardless of
`-p p0-loop-bot-a … --isolated` (the same round-1 fact AC-2 established), so
bot-a's profile must be selected in the UI before the ask — otherwise the
composer talks to `default`, which has no `a2a_agents.bot-b` peer and no
`coworkers-live` model in scope.

1. Open bot-a's dashboard chat (`agent-browser open "http://127.0.0.1:9119/chat"`,
   `wait --load networkidle`, `snapshot -i`), open the profile combobox and
   select the launched profile `p0-loop-bot-a` (the option labelled
   `this dashboard (p0-loop-bot-a)`, by `@ref`), then re-snapshot (refs are dead
   after the rerender)
   → expect: the composer prompt is **prefixed with the profile name**
   `p0-loop-bot-a ` (a named profile renders `<profile> <glyph>` —
   `ui-tui/src/lib/prompt.ts:31-33`; the glyph is skin-configurable, so assert
   the `p0-loop-bot-a ` **prefix**, not a specific glyph); screenshot `step-1.png`.
2. Fill the composer with a natural-language ask and submit — **never** send
   `/hello` over a2a: a2a frames every inbound message including `/`-text, so a
   peer can never reach the gateway's slash commands
   (`plugins/platforms/a2a/security.py:214-222`; `adapter.py:734,766-780`).
   Prompt:
   `Use a2a to contact bot-b and ask it to reply with a short hello that contains the marker P0LOOP-<nonce>; then tell me bot-b's exact reply.`
   (substitute the real `$NONCE`) → expect: bot-a's model issues
   `a2a_call(agent="bot-b", message="… P0LOOP-<nonce> …")`
   (`plugins/platforms/a2a/tools.py:259-302`); screenshot `step-2.png`.
3. Wait (≤ `timeout_s`) for bot-a's chat to render bot-b's returned greeting
   containing `P0LOOP-<nonce>`
   (`timeout 600 agent-browser wait --text "P0LOOP-<nonce>"`) → expect: the
   marker-bearing greeting is visible in bot-a's dashboard; screenshot
   `step-3.png`.

## Pass

bot-b's `state.db` has a `source='a2a'` session whose inbound message **and**
assistant reply both contain `P0LOOP-<nonce>`, **and** bot-a's dashboard visibly
renders the returned greeting/marker.

## Evidence

- `step-1.png` — `p0-loop-bot-a` selected in the combobox; composer prompt prefixed `p0-loop-bot-a `.
- `step-2.png` — bot-a issues the a2a delegation.
- `step-3.png` — bot-a's chat shows the returned greeting containing `P0LOOP-<nonce>`.
- State half — bot-b's own per-profile `state.db` (the image has no `sqlite3`
  CLI, so the stdlib form; schema `hermes_state_common.py:395-460`, per-profile
  db `hermes_cli/profiles.py:385-390`):
  ```bash
  python3 -c "import sqlite3, os; d = sqlite3.connect(os.environ['HERMES_HOME'] + '/profiles/p0-loop-bot-b/state.db'); rows=[r for r in d.execute(\"select s.source, m.role, substr(m.content,1,160) from messages m join sessions s on s.id = m.session_id where s.source='a2a' and m.content like '%P0LOOP-$NONCE%' order by m.timestamp\")]; print(len(rows), 'matching rows'); [print(r) for r in rows]"
  ```
  Assert at least the inbound row and bot-b's assistant reply both match the
  nonce.
- Budget — before finishing, read `session_model_usage` summed over both bots
  (per `/hermes-ui-driver` §1f) against `LIVE_MODEL_CALLS_MAX=40` /
  `LIVE_BUDGET_USD=5`; over either bound is `FAIL(budget)`.
