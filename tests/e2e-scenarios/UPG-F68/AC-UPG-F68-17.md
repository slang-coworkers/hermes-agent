---
ac: AC-UPG-F68-17
kind: live
model: live
base_url: https://inference-api.nvidia.com
api_mode: anthropic_messages
model_id: aws/anthropic/bedrock-claude-opus-4-8
fixtures:
  - fixtures/upg-f68-bot-a
  - fixtures/upg-f68-bot-b
timeout_s: 600
---

# AC-UPG-F68-17 — bot A `message_agent`s bot B in one gateway; one queued ack, one completion

(ADR `upg-f68.md:151`, outline `upg-f68.md:188-212`, plus erratum E4 §E4.4's one added step, adopted by E6′ §E6.4 /
§E6.11.) In one gateway on the candidate head, bot A `message_agent`s bot B. A's tool result is `queued` with a non-empty
`delivery_id`, and B's reply is visible in A's Bot Chat as exactly one completion carrying the reply text. The added step
holds B's reply until A's sending turn has closed: that is the live proof that the delivery runner outlived A's turn-end
sweep (item 4, `persist_on_release`); `test_ac_upg_f68_19` is its keyword-level regression.

**Gate note (ADR §Design D5 (d), E6′ §E6.5).** This row PASSing does not by itself turn P5 green: the carried
AC-FLEET-F62.h ids must also PASS at the same head.

**Live provider.** The lane's preconfigured live provider at `https://inference-api.nvidia.com`
(`api_mode: anthropic_messages`, model `aws/anthropic/bedrock-claude-opus-4-8`), as the outline names it. Both fixtures
carry it as the named provider `coworkers-live` with an inline placeholder `api_key`: the delivery child that runs B's turn
drops `ANTHROPIC_*` from its env (`tools/environments/local_env_policy.py:127`), and the OneCLI egress proxy replaces the
placeholder with the real credential at the remote host. No real key is in the fixture. Budget: ≤ 6 model calls, inside
the scenario caps `LIVE_MODEL_CALLS_MAX=40` / `LIVE_BUDGET_USD=5`.

## Setup

The tester installs the `fixtures:` list (`upg-f68-bot-a`, `upg-f68-bot-b`, profile dirs with `distribution.yaml` at the
root) before this section runs; nothing below re-installs them. After install, with `$HERMES_HOME` = the shared testbed home
and `$TB/harness.live.env` sourced:

1. **Both bots in the Bot Mode roster the gateway serves** (fleet baseline rule 2: two profiles in ONE gateway). The
   `ui_meta['hermes-bots']` block is not distribution-owned, so it is written post-install:
   ```bash
   for b in upg-f68-bot-a upg-f68-bot-b; do
     python3 - "$HERMES_HOME/profiles/$b/profile.yaml" "$b" <<'PY'
   import json, os, sys
   p, name = sys.argv[1], sys.argv[2]
   d = {}
   if os.path.exists(p):
       try: d = json.load(open(p, encoding="utf-8"))
       except Exception: d = {}
   ui = dict(d.get("ui_meta") or {})
   ui["hermes-bots"] = {"title": name, "description": "UPG-F68 AC-17 live scenario bot", "shape": "square"}
   d["ui_meta"] = ui
   json.dump(d, open(p, "w", encoding="utf-8"), indent=2)
   PY
   done
   ```
2. **The one gateway** runs on the candidate head (`$WT`), multiplexing both profiles, and the desktop app is connected to
   it (the screenshots in §Evidence are taken there). Wait for its ready line before step 1.
3. **Bot A's canonical Bot Chat.** `message_agent` is offered only in a session titled exactly `Bot Chat`
   (`tools/bot_mode_dm.py:130-134`). Open bot A's Bot Chat from the desktop app's Bots list (in the dashboard SPA, select
   `upg-f68-bot-a` in the profile combobox first → expect the `upg-f68-bot-a »` prompt). If bot A has no session titled
   `Bot Chat` yet, submit `/title Bot Chat` in it and wait (bounded) for the acknowledgement. Confirm before step 1:
   ```bash
   python3 -c "import sqlite3, os; d=sqlite3.connect(os.environ['HERMES_HOME'] + '/profiles/upg-f68-bot-a/state.db'); print(d.execute(\"select id, title from sessions where title='Bot Chat'\").fetchall())"
   ```
   → exactly one row.
4. Record the baselines the oracle compares against, and the usage counter, in `$ART/scenario-AC-UPG-F68-17/evidence.txt`:
   ```bash
   for b in upg-f68-bot-a upg-f68-bot-b; do
     python3 -c "import sqlite3, os, sys; b=sys.argv[1]; p=os.environ['HERMES_HOME'] + f'/profiles/{b}/state.db'; d=sqlite3.connect(p) if os.path.exists(p) else None; print(b, 'max_message_id', d.execute('select coalesce(max(id),0) from messages').fetchone()[0] if d else 0)" "$b"
   done
   ```

## Steps

1. In bot A's Bot Chat, send "Ask upg-f68-bot-b for the word 'pong' and tell me what it said" → expect: A calls
   `message_agent` with `to` = bot B, and the tool row's JSON has `status` `queued` and a non-empty `delivery_id`.
   `step-1.png`: A's Bot Chat showing the `queued` tool row.
2. **Hold B's reply until A's sending turn has closed**: the tester's harness keeps bot B from answering (B's profile is
   paused through the gateway, or B's provider stub delays) until A's turn shows its final assistant message, then releases
   B. → expect: A's turn has closed (its final assistant message is shown) while B has not yet answered.
   `step-2.png`: A's Bot Chat with its closed turn, before B is released.
3. Wait (bounded by `timeout_s`) → expect: bot B's state.db holds one `role='user'` row carrying A's attributed message.
   `step-3.png`: B's Bot Chat showing A's inbound.
4. Wait → expect: exactly one completion notification in A's session carrying B's reply text, and A's next assistant
   message names `pong`. `step-4.png`: A's Bot Chat showing the completion with B's reply.

## Pass

The criterion holds when steps 1-4 hold with exactly one completion and no duplicate delivery row in B.

## Evidence

All under `$ART/scenario-AC-UPG-F68-17/`:

- `step-1.png` … `step-4.png`, one per step above, taken in the desktop app connected to the gateway (fleet baseline rule
  2; the standing P5 rule that a UI-observable live row needs step PNGs).
- `evidence.txt`: the Setup baselines, then the stdlib `sqlite3` queries below with their output (the coworker image has
  no `sqlite3` CLI), then the gateway log lines that carry the step-1 `delivery_id`.
  - A's tool row (step 1): the `message_agent` call and its `queued` result with the `delivery_id`:
    ```bash
    python3 -c "import sqlite3, os; d=sqlite3.connect(os.environ['HERMES_HOME'] + '/profiles/upg-f68-bot-a/state.db'); [print(r) for r in d.execute(\"select m.id, m.role, substr(m.tool_calls,1,200), substr(m.content,1,300) from messages m join sessions s on s.id=m.session_id where s.title='Bot Chat' and (instr(coalesce(m.tool_calls,''),'message_agent')>0 or instr(coalesce(m.content,''),'delivery_id')>0) order by m.id\")]"
    ```
  - A's turn closed before B answered (step 2): the timestamp of A's final assistant row for the sending turn, and the
    timestamp of B's first assistant row after its baseline (raw epoch seconds, sub-second kept); the first is earlier:
    ```bash
    python3 -c "import sqlite3, os; h=os.environ['HERMES_HOME']; a=sqlite3.connect(h + '/profiles/upg-f68-bot-a/state.db'); b=sqlite3.connect(h + '/profiles/upg-f68-bot-b/state.db'); print('A', a.execute(\"select m.id, m.timestamp from messages m join sessions s on s.id=m.session_id where s.title='Bot Chat' and m.role='assistant' order by m.id\").fetchall()); print('B', b.execute(\"select m.id, m.timestamp from messages m where m.role='assistant' order by m.id\").fetchall())"
    ```
  - B's inbound (step 3): exactly one `role='user'` row above B's baseline carrying A's attributed message (no duplicate
    delivery row):
    ```bash
    python3 -c "import sqlite3, os; d=sqlite3.connect(os.environ['HERMES_HOME'] + '/profiles/upg-f68-bot-b/state.db'); [print(r) for r in d.execute(\"select s.id, s.title, m.id, substr(m.content,1,200) from messages m join sessions s on s.id=m.session_id where m.role='user' and instr(m.content,'upg-f68-bot-a')>0 order by m.id\")]"
    ```
  - A's completion (step 4): exactly one completion row in A's session carrying B's reply, then A's assistant message
    naming `pong`:
    ```bash
    python3 -c "import sqlite3, os; d=sqlite3.connect(os.environ['HERMES_HOME'] + '/profiles/upg-f68-bot-a/state.db'); [print(r) for r in d.execute(\"select m.id, m.role, substr(m.content,1,300) from messages m join sessions s on s.id=m.session_id where s.title='Bot Chat' and instr(lower(m.content),'pong')>0 order by m.id\")]"
    ```
- The live budget counter, summed across both bots, after the scenario (over the bound is `FAIL(budget)`):
  ```bash
  for b in upg-f68-bot-a upg-f68-bot-b; do
    python3 -c "import sqlite3, os, sys; d=sqlite3.connect(os.environ['HERMES_HOME'] + f'/profiles/{sys.argv[1]}/state.db'); print(sys.argv[1], *d.execute('select coalesce(sum(api_call_count),0), round(coalesce(sum(case when actual_cost_usd > 0 then actual_cost_usd else estimated_cost_usd end),0),4) from session_model_usage').fetchone())" "$b"
  done
  ```
