---
ac: AC-CH-F51-1
kind: live
model: live
fixtures:
  - fixtures/loop-f37-bot-a
  - fixtures/loop-f37-bot-b
timeout_s: 600
base_url: https://inference-api.nvidia.com
model_id: aws/anthropic/bedrock-claude-opus-4-8
---

# AC-CH-F51-1 — wired-only messaging round-trips a real reply

In a running two-profile fleet, `loop-f37-bot-a`'s `message_agent` to an
**unwired** `loop-f37-bot-b` is refused by the `nv-fleet-gates` WIRING predicate
and the refusal is surfaced to bot A (not merely logged); after
`hermes wire add loop-f37-bot-a loop-f37-bot-b`, the same send reaches bot B and
bot B's reply returns to bot A's session. Both bots are profiles of the one
gateway (Bot-Mode `message_agent` is intra-gateway per the fleet topology), so
this needs no a2a listener — only the shared edges store the gate reads.

## Setup

The tester installs the `fixtures:` list before this section runs
(`hermes profile install tests/e2e-scenarios/LOOP-F37/fixtures/loop-f37-bot-a --name loop-f37-bot-a -y`
and likewise for `loop-f37-bot-b`); do not re-install them here. After install,
with `$HERMES_HOME` = the shared testbed home and the **live** env sourced
(`$TB/harness.live.env`; dummy `ANTHROPIC_API_KEY`, real base URL, credential
injected by the OneCLI proxy):

1. `hermes profile install` does not create a `plugins/` dir, and
   `HERMES_BUNDLED_PLUGINS` is empty, so each profile owns its copy of the
   plugin — copy it in from the worktree for both bots:
   ```bash
   for b in loop-f37-bot-a loop-f37-bot-b; do
     mkdir -p "$HERMES_HOME/profiles/$b/plugins"
     cp -r "$WT/plugins/nv-fleet-gates" "$HERMES_HOME/profiles/$b/plugins/"
   done
   ```
2. Start unwired: `rm -f /tmp/loop-f37-fleet-edges.db` (the shared
   `edges_db_path` both configs name). Confirm no `loop-f37-bot-a -> loop-f37-bot-b`
   edge exists (the store is absent, so there is none).
   No `.env` key write and no `base_url` rewrite are needed: the fixture provider
   block is self-contained (literal on-disk `api_key`, remote host), and
   `$TB/harness.live.env` restores the egress proxy that substitutes the real
   credential at the `inference-api.nvidia.com` hop.
3. Give each profile a Bot-Mode identity so both render as bots of the one
   gateway (`ui_meta['hermes-bots']` is not distribution-owned, so write it
   post-install; JSON is valid YAML, so it merges onto `profile.yaml` cleanly):
   ```bash
   for b in loop-f37-bot-a loop-f37-bot-b; do
     python3 - "$HERMES_HOME/profiles/$b/profile.yaml" "$b" <<'PY'
   import json, sys, os
   p, name = sys.argv[1], sys.argv[2]
   d = {}
   if os.path.exists(p):
       try: d = json.load(open(p, encoding="utf-8"))
       except Exception: d = {}
   ui = dict(d.get("ui_meta") or {})
   ui["hermes-bots"] = {"title": name, "description": "LOOP-F37 live scenario bot", "shape": "square"}
   d["ui_meta"] = ui
   json.dump(d, open(p, "w", encoding="utf-8"), indent=2)
   PY
   done
   ```
4. Launch the one gateway dashboard over the shared home (both profiles are
   discoverable as bots), and wait for the ready line:
   ```bash
   ( source $TB/harness.live.env && cd $WT
     export HERMES_WEB_DIST=<prebuilt SPA dist>
     hermes dashboard --host 127.0.0.1 --port 9119 --no-open --skip-build \
       > $ART/scenario-AC-CH-F51-1/dashboard.log 2>&1 & )
   timeout 300 bash -c "until grep -qE 'HERMES_DASHBOARD_READY port=|Hermes Web UI' \
     $ART/scenario-AC-CH-F51-1/dashboard.log; do sleep 2; done"
   ```

## Steps

The dashboard SPA opens the **default** profile's chat, so select
`loop-f37-bot-a` in the profile combobox before the first message (the option
labelled `this dashboard (loop-f37-bot-a)`), and re-snapshot after the switch.

1. Open `loop-f37-bot-a`'s Bot Chat, select `loop-f37-bot-a` in the profile
   combobox → expect: the composer prompt is prefixed `loop-f37-bot-a `.
   Fill the composer with `Use message_agent to send loop-f37-bot-b the message
   "ping from bot-a" and tell me what happens.` and submit; wait (bounded, up to
   `timeout_s`) for the assistant to settle
   → expect: the turn reports the send was **refused** and surfaces the refusal
   text (naming that `loop-f37-bot-a` is not wired to `loop-f37-bot-b`, listing
   wired teammates) — NOT a silent drop and NOT a delivered reply. `step-1.png`.
2. Wire the edge, then re-prompt:
   ```bash
   ( source $TB/harness.live.env && hermes -p loop-f37-bot-a wire add loop-f37-bot-a loop-f37-bot-b )
   ```
   In `loop-f37-bot-a`'s Bot Chat send the same prompt again; wait (bounded) for
   the reply → expect: the `message_agent` call is delivered and
   `loop-f37-bot-b`'s reply arrives back in `loop-f37-bot-a`'s session as the
   background-completion notification. `step-2.png`.

## Pass

The unwired send is **refused and surfaced** to bot A (step 1), and after
`hermes wire add` the same send **round-trips a real reply** from bot B (step 2).

## Evidence

- `step-1.png` — bot A's transcript showing the refusal text (unwired).
- `step-2.png` — bot A's transcript showing bot B's returned reply (wired).
- The edge appeared only between the steps (stdlib `sqlite3`; the image has no
  `sqlite3` CLI):
  ```bash
  python3 -c "import sqlite3; d=sqlite3.connect('/tmp/loop-f37-fleet-edges.db'); print(d.execute('select from_profile,to_profile,gated from edges order by from_profile,to_profile').fetchall())"
  ```
  expect: `[]`-equivalent before step 2 (or the file absent), and rows including
  `('loop-f37-bot-a','loop-f37-bot-b',0)` and `('loop-f37-bot-b','loop-f37-bot-a',0)` after.
- A reply row for the dispatched message in bot B's `state.db`
  (`$HERMES_HOME/profiles/loop-f37-bot-b/state.db`), per hermes-ui-driver §1f.
- Live budget bound: at most `LIVE_MODEL_CALLS_MAX=40` calls /
  `LIVE_BUDGET_USD=5` per scenario, read from `session_model_usage` summed
  across both bots; over the bound is `FAIL(budget)`.
