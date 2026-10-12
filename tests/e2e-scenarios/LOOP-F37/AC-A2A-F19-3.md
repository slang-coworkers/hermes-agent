---
ac: AC-A2A-F19-3
kind: live
model: live
base_url: https://inference-api.nvidia.com
model_id: aws/anthropic/bedrock-claude-opus-4-8
fixtures:
  - fixtures/loop-f37-worker-a
  - fixtures/loop-f37-orch
timeout_s: 600
---

# AC-A2A-F19-3 — a gated-edge message_agent surfaces a human-approval hold, and an operator approve releases it

In a running interactive Bot Chat, `loop-f37-worker-a`'s `message_agent` to
`loop-f37-orch` traverses a **gated** wiring edge, so the `nv-fleet-gates`
predicate returns an `approve` directive and the runtime escalates the call to
the native human-approval gate. The hold surfaces to the operator keyed on the
runtime `pattern_key` `plugin_rule:wire:loop-f37-worker-a:loop-f37-orch`, and an
operator `once`-approve releases exactly that call so it reaches `loop-f37-orch`.

Both bots are profiles of the one gateway (Bot-Mode `message_agent` is
intra-gateway per the fleet topology), so this needs no a2a listener — only the
shared edges store the gate reads and the gateway's approvals surface. Re-scoped
`desktop:` → `live:` per ADR Amendment 1: the desktop stub tier cannot drive
`message_agent` (the shared `apps/desktop/e2e/mock-server.ts` has no per-test
tool-call injection and `message_agent` is injected only in a Bot-Mode session),
so the operator-facing hold+release is proven where a real `message_agent`
actually runs. The gate directive itself (approve + stable `rule_key`) is also
proven with no browser or budget by the hermetic AC-A2A-F19-1 / AC-A2A-F19-2.

## Setup

The tester installs the `fixtures:` list before this section runs
(`hermes profile install tests/e2e-scenarios/LOOP-F37/fixtures/loop-f37-worker-a --name loop-f37-worker-a -y`
and likewise for `loop-f37-orch`); do not re-install them here. After install,
with `$HERMES_HOME` = the shared testbed home and the **live** env sourced
(`$TB/harness.live.env`; the egress proxy that substitutes the real credential at
the `inference-api.nvidia.com` hop). No `base_url` rewrite and no `.env` key write
are needed — the fixture provider block is self-contained (literal on-disk
`api_key`, remote host).

1. `hermes profile install` does not create a `plugins/` dir, and
   `HERMES_BUNDLED_PLUGINS` is empty, so each profile owns its copy of the
   plugin — copy it in from the worktree for both bots:
   ```bash
   for b in loop-f37-worker-a loop-f37-orch; do
     mkdir -p "$HERMES_HOME/profiles/$b/plugins"
     cp -r "$WT/plugins/nv-fleet-gates" "$HERMES_HOME/profiles/$b/plugins/"
   done
   ```
2. Start from a known edges store: `rm -f /tmp/loop-f37-a2a-fleet-edges.db` (the
   shared `edges_db_path` both configs name — distinct from the AC-CH-F51-1 store).
3. Give each profile a Bot-Mode identity so both render as bots of the one
   gateway (`ui_meta['hermes-bots']` is not distribution-owned, so write it
   post-install; JSON is valid YAML, so it merges onto `profile.yaml` cleanly):
   ```bash
   for b in loop-f37-worker-a loop-f37-orch; do
     python3 - "$HERMES_HOME/profiles/$b/profile.yaml" "$b" <<'PY'
   import json, sys, os
   p, name = sys.argv[1], sys.argv[2]
   d = {}
   if os.path.exists(p):
       try: d = json.load(open(p, encoding="utf-8"))
       except Exception: d = {}
   ui = dict(d.get("ui_meta") or {})
   ui["hermes-bots"] = {"title": name, "description": "LOOP-F37 gated-edge live scenario bot", "shape": "square"}
   d["ui_meta"] = ui
   json.dump(d, open(p, "w", encoding="utf-8"), indent=2)
   PY
   done
   ```
4. Seed exactly one **gated** edge for the pair (the `wire add` is bidirectional,
   so it records `worker-a -> orch` and `orch -> worker-a`, both gated; the
   traversal under test is `worker-a -> orch`):
   ```bash
   ( source $TB/harness.live.env && hermes -p loop-f37-worker-a wire add loop-f37-worker-a loop-f37-orch --gated )
   ( source $TB/harness.live.env && hermes -p loop-f37-worker-a wire list )   # shows the gated worker-a<->orch rows
   ```
5. Launch the one gateway dashboard over the shared home as an **interactive**
   session (NOT `single_query`, so the gated-edge approve is HELD for the operator
   rather than auto-denied — contrast AC-A2A-F19-2), and wait for the ready line:
   ```bash
   ( source $TB/harness.live.env && cd $WT
     export HERMES_WEB_DIST=<prebuilt SPA dist>
     hermes dashboard --host 127.0.0.1 --port 9119 --no-open --skip-build \
       > $ART/scenario-AC-A2A-F19-3/dashboard.log 2>&1 & )
   timeout 300 bash -c "until grep -qE 'HERMES_DASHBOARD_READY port=|Hermes Web UI' \
     $ART/scenario-AC-A2A-F19-3/dashboard.log; do sleep 2; done"
   ```

## Steps

The dashboard SPA opens the **default** profile's chat, so select
`loop-f37-worker-a` in the profile combobox before the first message (the option
labelled `this dashboard (loop-f37-worker-a)`), and re-snapshot after the switch.

1. Open `loop-f37-worker-a`'s Bot Chat and select `loop-f37-worker-a` in the
   profile combobox (expect the composer prompt prefixed `loop-f37-worker-a `). In
   the composer submit `/title Bot Chat` and wait (bounded) for the command
   acknowledgement to render. This retitles the current SPA session to the
   canonical `Bot Chat`, which — together with the profile's bot-mode-managed
   `ui_meta['hermes-bots']` — is what makes core inject `message_agent` on the next
   turn: `ensure_message_agent_tool` gates on session title == `Bot Chat` AND
   `is_bot_mode_managed` (bot_mode_dm.py:150-159; a session on any other title
   never sees the tool). `step-1.png`. Confirm before step 2: the sender profile's
   `state.db` has a `sessions` row titled `Bot Chat` (query in Evidence).
2. In the same Bot Chat, fill the composer with `Use message_agent to send
   loop-f37-orch the message "please review" and tell me what happens.` and submit;
   wait (bounded, up to `timeout_s`) for a **pending approval** to surface rather
   than a completed send → expect: the `message_agent` call does NOT complete; the
   gateway emits an `approval.request` carrying `pattern_key:
   plugin_rule:wire:loop-f37-worker-a:loop-f37-orch` (the runtime prefixes the
   plugin's `rule_key` `wire:loop-f37-worker-a:loop-f37-orch` with `plugin_rule:`),
   rendered as a hold on the approvals surface. `step-2.png`.
3. As the operator, answer the hold with choice **`once`** on the dashboard
   approvals surface (agent-browser click the approve-once control, which issues
   the gateway `approval.respond` RPC — the `hermes approvals` CLI is read-only and
   cannot answer a hold) → expect: the hold clears and the held `message_agent` is
   released, delivering `please review` to `loop-f37-orch` (whose own `Bot Chat`
   session is auto-created on delivery — `--create-if-missing`, bot_mode_dm.py:362-374,
   so only the SENDER needs the retitle in step 1). `step-3.png`.

## Pass

The gated-edge `message_agent` surfaces as a **held** approval keyed on
`pattern_key: plugin_rule:wire:loop-f37-worker-a:loop-f37-orch` (step 2), and an
operator **`once`**-approve **releases exactly that call** so it reaches
`loop-f37-orch` (step 3).

## Evidence

- `pending-approval.json` — the captured `approval.request` event payload (or the
  `session.resume` response) for the held call; assert it carries
  `pattern_key: plugin_rule:wire:loop-f37-worker-a:loop-f37-orch`. The pending
  approval lives in the in-memory gateway queue (NOT `state.db`), and the desktop
  approval CARD renders only `description`/`command`/`choices` (not `pattern_key`),
  so this payload — not a screenshot — is what proves the key.
- `step-1.png` — the sender's Bot Chat after `/title Bot Chat` (the canonical
  session ready). Confirm the retitle from the sender `state.db` (stdlib
  `sqlite3`):
  ```bash
  python3 -c "import sqlite3, os; d=sqlite3.connect(os.environ['HERMES_HOME'] + '/profiles/loop-f37-worker-a/state.db'); print(d.execute(\"select id, title from sessions where title='Bot Chat'\").fetchall())"
  ```
  expect: exactly one `Bot Chat` row (so core injects `message_agent` next turn).
- `step-2.png` — the approvals surface showing the hold surfaced to the operator.
- `step-3.png` — the transcript / approvals surface after the `once`-approve,
  showing the hold cleared.
- The released call reaching `loop-f37-orch`: a `python3 -c` read (stdlib
  `sqlite3`; the image has no `sqlite3` CLI) of
  `$HERMES_HOME/profiles/loop-f37-orch/state.db` locating the delivered
  `please review` inbound row in `messages` joined to the `Bot Chat` `sessions`
  row (per hermes-ui-driver §1f):
  ```bash
  python3 -c "import sqlite3, os; d=sqlite3.connect(os.environ['HERMES_HOME'] + '/profiles/loop-f37-orch/state.db'); [print(r) for r in d.execute(\"select s.id, s.source, m.role, substr(m.content,1,120) from messages m join sessions s on s.id = m.session_id order by m.timestamp desc limit 10\")]"
  ```
  expect: a row whose content contains `please review` reaching `loop-f37-orch`
  only AFTER the `once`-approve (the in-memory hold itself is not queryable from
  `state.db`).
- Live budget bound: at most `LIVE_MODEL_CALLS_MAX=40` calls / `LIVE_BUDGET_USD=5`
  per scenario, read from `session_model_usage` summed across both bots; over the
  bound is `FAIL(budget)`. One `message_agent` round is enough.
