---
ac: AC-LOOP-F39-6
kind: live
model: live
base_url: https://inference-api.nvidia.com
model_id: aws/anthropic/bedrock-claude-opus-4-8
key_env: ANTHROPIC_API_KEY
fixtures:
  - fixtures/orchestrator
  - fixtures/worker
timeout_s: 600
---

# AC-LOOP-F39-6 — the rendered supervise-issues 12h cron fires, nudges an assignee, escalates on repeat

The rendered supervise-issues 12h `--deliver bot-chat` cron, made due and fired **through the gateway
scheduler**, runs one sweep that escalates a card already nudged twice with no assignee response to the human
as one owner-input `kanban_block(kind="needs_input")` on the board instead of a third nudge, and wakes the
orchestrator's OWN canonical Bot Chat turn, which `message_agent`s the assignee of a silent card (the nudge
lands in the assignee's Bot Chat). `model:
live` — a real model answers behind the OneCLI proxy. The on-disk provider is the named-provider
`api_key`-literal form (`providers.coworkers-live` with an inline dummy `api_key` the egress proxy replaces
at the wire), NOT a bare `key_env: ANTHROPIC_API_KEY` block: that shape 404/401s behind the proxy and does
not resolve across the profile/child execution boundaries the fire crosses (a secondary profile's secret
scope does not read process env — `agent/secret_scope.py:176` — and the cron delivery / `message_agent`
hops do not carry `ANTHROPIC_*` through), so the inline-key form is required (matches the FLEET-F62
precedent fixtures; the frontmatter `key_env` line is descriptive metadata only — see §Setup 4). Budget: `LIVE_MODEL_CALLS_MAX=40`, `LIVE_BUDGET_USD=5`, **ONE run, no re-drive**.

The oracle is strict and exactly-one, mirroring AC-FLEET-F62-7. The cron-delivered envelope
(`[Cronjob "supervise-issues" output …`) and the `message_agent` envelope
(`Message from 🤖 orchestrator (@orchestrator): …`) are BOTH server-stamped (not spoofable by the model),
and each nudge carries this run's seeded card token (`F39A-$NONCE`). The escalation is a **recorded act**,
not text: the orchestrator's `kanban_block(task_id=f39b-$NONCE, kind="needs_input")` with its `ok` result, in
the cron session the scheduler principal admitted for this execution (`session_admissions.request_id =
'cron:$JOB_ID:<execution id>'`, `principal_id = 'cron-owner'`), plus exactly one needs_input `blocked` event
on card B inside that execution's `[claimed_at, finished_at]`. No reply text is matched, so the wording and
where `F39-ESC:` sits never decide the verdict. A single force-due fire drives BOTH behaviours: card A (no
prior nudges) → exactly one nudge; card B (two seeded unanswered supervisor nudges) → exactly one owner-input
escalation, no third nudge. The oracle is the executable `oracle.py` shipped alongside this file (it parses
`tool_calls` arrays and correlates each result by `tool_call_id` — a bare row `COUNT(*)` is not sufficient).

## Setup

The tester installs the `fixtures:` profile dirs (`orchestrator`, `worker`) before this section — the
`default` profile is NOT a fixture (Hermes rejects installing a distribution named `default`,
`hermes_cli/profile_distribution.py:530`); its config ships as `fixtures/default.config.yaml` and is merged
into the root `$HERMES_HOME/config.yaml` in step 1. Everything below is post-install. The canonical
`tests/hermes_cli/fixtures/loop-f39/` (its updated `skills/supervise-issues/SKILL.md` + the `orchestrator`
type's `config: {toolsets: [hermes-cli, kanban]}`) is the onboard-render source in step 3.

Persist run vars in one sourced file (referenced throughout). Source the testbed env FIRST so
`HERMES_KANBAN_HOME` is resolved — the testbed exports `HERMES_KANBAN_HOME=$TB/kanban`, so the board is NOT
at `$HERMES_HOME/kanban.db`; `KANBAN_DB` below resolves it the way `kanban_db.py` does:
```bash
source "$TB/harness.live.env"          # testbed env: HERMES_HOME, HERMES_KANBAN_HOME, GATEWAY_TOKEN, WT, ART …
cat > "$TB/fleet-f62a.env" <<EOF
PORT=9119                                  # the gateway api_server (fire route + scheduler)
DASH_PORT=9120                             # the separate web-UI dashboard for the required post-sweep screenshots (dashboard defaults to 9119 too — must differ from PORT)
NONCE="f39-$(date +%s)"
API_SERVER_KEY="$(python3 -c 'import secrets;print(secrets.token_hex(24))')"   # 48 hex chars, ≥16
KANBAN_DB="${HERMES_KANBAN_DB:-${HERMES_KANBAN_HOME:-$HERMES_HOME}/kanban.db}"
SCN="$WT/tests/e2e-scenarios/FLEET-F62.a"
EOF
source "$TB/fleet-f62a.env"
```

0. **Stage + enable `nv-coworker-compose`** (the testbed points `HERMES_BUNDLED_PLUGINS` at an EMPTY dir, so
   the `onboard coworker` verb is absent until the plugin is installed — mirror AC-LOOP-F35-5 §Setup 1):
   ```bash
   mkdir -p "$HERMES_HOME/plugins"
   cp -R "$WT/plugins/nv-coworker-compose" "$HERMES_HOME/plugins/"
   hermes plugins enable nv-coworker-compose
   hermes plugins list | grep -F nv-coworker-compose      # must show it enabled
   hermes onboard --help | grep -F coworker               # the `coworker` verb now exists, exit 0
   ```
1. **Default config + secret scope.** Merge `fixtures/default.config.yaml` (the multiplex block below, plus
   `plugins.enabled: [nv-coworker-compose]`) into
   the root `$HERMES_HOME/config.yaml`, and write the SAME `API_SERVER_KEY` into BOTH `$HERMES_HOME/.env`
   AND `$HERMES_HOME/profiles/orchestrator/.env` — the orchestrator's own secret scope, because a
   `/p/orchestrator/` request does NOT fall through to process env (`agent/secret_scope.py:176`, tested by
   `tests/gateway/test_api_server_multiplex_secret_scope.py`). The `orchestrator` fixture's config pins
   `platforms: {api_server: {enabled: false}}` so the env key does not bind a second listener
   (`gateway/config.py:2298-2323`, `gateway/run.py:16667`).
   ```yaml
   # fixtures/default.config.yaml — merged into $HERMES_HOME/config.yaml
   gateway:
     multiplex_profiles: true
     multiplex_profile_allowlist: [orchestrator, worker]   # orchestrator MUST be here so its cron ticks
   plugins:
     enabled: [nv-coworker-compose]
   ```
2. **Boot the FULL messaging gateway** on the DEFAULT profile — `hermes gateway run --no-supervise`
   (`hermes_cli/subcommands/gateway.py:47`), with `API_SERVER_KEY` + `API_SERVER_HOST=127.0.0.1` +
   `API_SERVER_PORT=$PORT` exported in the process env. This is the process that loads the aiohttp
   **api_server platform** — the one that serves `POST /p/<profile>/api/jobs/{id}/run`
   (`gateway/platforms/api_server.py:6803` `_handle_run_job`) and runs the per-profile scheduler tick.
   Do NOT boot `hermes dashboard`/`hermes serve` for the fire: that is the uvicorn **web UI** server
   (`hermes_cli/main.py:11995` `cmd_dashboard`), a SEPARATE process that does not load gateway platforms and
   returns HTTP 405 (`allow: GET`) on the `/p/.../api/jobs/{id}/run` route. Wait for `GET
   http://127.0.0.1:$PORT/health` → 200 (the api_server listener is up; the api_server registers `/health`,
   `/health/detailed`, `/v1/health` — `gateway/platforms/api_server.py:2217-2219` — not `/api/health`). A dashboard SPA is launched
   separately (on `$DASH_PORT`, against the same `$HERMES_HOME`) only AFTER a successful sweep, for the
   required post-sweep UI screenshots in §Evidence.
3. **Onboard-render the canonical spec** over the loopback WS (renders the cron + skill + Bot Chats):
   ```bash
   ( source "$TB/fleet-f62a.env" && source "$TB/harness.live.env" && cd "$WT"
     hermes onboard coworker tests/hermes_cli/fixtures/loop-f39/coworker-types.yaml \
       --gateway-url "ws://127.0.0.1:$PORT/api/ws?token=$GATEWAY_TOKEN" \
       > "$ART/scenario-AC-LOOP-F39-6/onboard.log" 2>&1 ; echo onboard_exit=$? )
   ```
   → expect `orchestrator/skills/supervise-issues/SKILL.md` present and `orchestrator/cron/jobs.json` with
   one job `name=supervise-issues`, `skills=["supervise-issues"]`, `deliver="bot-chat"`,
   `schedule={"kind":"interval","minutes":720}`, `enabled` truthy. Select the job by NAME (not `jobs[0]`)
   and assert its fields before firing; append `JOB_ID` to `$TB/fleet-f62a.env`:
   ```bash
   JOB_ID=$(python3 - "$HERMES_HOME/profiles/orchestrator/cron/jobs.json" <<'PY'
   import json, sys
   jobs = json.load(open(sys.argv[1]))["jobs"]
   j = next(x for x in jobs if x.get("name") == "supervise-issues")
   assert j.get("deliver") == "bot-chat", j.get("deliver")
   assert j.get("skills") == ["supervise-issues"], j.get("skills")
   assert j.get("schedule", {}).get("kind") == "interval" and j["schedule"].get("minutes") == 720
   assert j.get("enabled", True) and not j.get("no_agent")
   print(j["id"])
   PY
   ) ; echo "JOB_ID=$JOB_ID" >> "$TB/fleet-f62a.env" ; source "$TB/fleet-f62a.env"
   ```
4. **Post-onboard merge — deterministic DEEP-MERGE (never a `cp`/file-replace), so the render's `toolsets`
   survive.** `hermes onboard` renders each profile's `config.yaml` from the coworker-types.yaml type
   `config` — so `orchestrator/config.yaml` gains `toolsets: [hermes-cli, kanban]` (`compose.py:2107`
   deep-merges the type `config`, `:2482` writes it) but NOT the live `coworkers-live` provider, which is
   fixture-only. Restore the provider by DEEP-MERGING the installed fixture config ONTO the rendered config
   (fixture wins on the provider / `model` / `api_server` keys; the rendered `toolsets` and identity are
   preserved — a naive `cp` of the fixture config, which has no `toolsets`, would silently strip
   `kanban_list`/`kanban_comment` and the sweep would no-op). Also re-write the `worker` silent SOUL (onboard
   force-installs the spine SOUL). Do NOT touch the rendered `skills/supervise-issues/SKILL.md` or
   `cron/jobs.json`. The cron delivery spawns `hermes chat` (no `-p`; its `HERMES_HOME` is the orchestrator profile) as a fresh subprocess that reads
   the merged `orchestrator/config.yaml`, so the live provider takes effect for the fire.
   ```bash
   for p in orchestrator worker; do
     python3 - "$SCN/fixtures/$p/config.yaml" "$HERMES_HOME/profiles/$p/config.yaml" <<'PY'
   import sys, yaml
   src, dst = sys.argv[1], sys.argv[2]
   fix = yaml.safe_load(open(src, encoding="utf-8")) or {}
   cur = yaml.safe_load(open(dst, encoding="utf-8")) or {}
   def dm(a, b):                 # deep-merge b (fixture) onto a (rendered); dict merges key-wise, leaf wins
       for k, v in b.items():
           a[k] = dm(a[k], v) if isinstance(v, dict) and isinstance(a.get(k), dict) else v
       return a
   yaml.safe_dump(dm(cur, fix), open(dst, "w", encoding="utf-8"), sort_keys=False)
   PY
   done
   cp "$SCN/fixtures/worker/SOUL.md" "$HERMES_HOME/profiles/worker/SOUL.md"
   ```
   Then assert the merge landed AND the render survived — fail the scenario here rather than waste the fire:
   ```bash
   python3 - <<'PY'
   import os, json, yaml
   H = os.environ["HERMES_HOME"]
   o = yaml.safe_load(open(f"{H}/profiles/orchestrator/config.yaml", encoding="utf-8"))
   assert o["model"]["provider"] == "coworkers-live", o.get("model")
   assert o["providers"]["coworkers-live"]["base_url"] == "https://inference-api.nvidia.com"
   assert o["providers"]["coworkers-live"].get("api_key"), "orchestrator api_key empty"
   assert o["providers"]["coworkers-live"]["api_mode"] == "anthropic_messages"
   assert o["platforms"]["api_server"]["enabled"] is False
   assert o.get("toolsets") == ["hermes-cli", "kanban"], f"toolsets lost by the merge: {o.get('toolsets')}"
   w = yaml.safe_load(open(f"{H}/profiles/worker/config.yaml", encoding="utf-8"))
   assert w["providers"]["coworkers-live"].get("api_key"), "worker api_key empty"
   assert os.path.isfile(f"{H}/profiles/orchestrator/skills/supervise-issues/SKILL.md"), "SKILL.md clobbered"
   jobs = json.load(open(f"{H}/profiles/orchestrator/cron/jobs.json", encoding="utf-8"))["jobs"]
   assert any(j.get("name") == "supervise-issues" and j.get("deliver") == "bot-chat" for j in jobs), "cron clobbered"
   soul = open(f"{H}/profiles/worker/SOUL.md", encoding="utf-8").read().lower()
   assert "stay silent" in soul or "never reply" in soul, "worker SOUL is not the silent one"
   print("post-merge OK: provider+toolsets+api_server restored; skill/cron/silent-SOUL intact")
   PY
   ```
   The `toolsets` assertion is load-bearing: the render supplies them from the orchestrator type
   `config.toolsets` and the deep-merge preserves them, but any file-replace would drop them; without
   `kanban` the orchestrator cannot `kanban_list`/`kanban_comment` and the sweep silently no-ops.
5. **Seed the shared board** `$KANBAN_DB` (= the testbed's `$HERMES_KANBAN_HOME/kanban.db`, NOT
   `$HERMES_HOME/kanban.db`) with raw stdlib sqlite (backdate directly —
   `_append_event` hardcodes `now`; and `kanban_show` reads comments from `task_comments`, not
   `task_events` — `tools/kanban_tools.py:532`, `hermes_cli/kanban_db.py:4008-4017` — so each seeded nudge
   writes BOTH a `task_comments` row and a matching `commented` event):
   ```bash
   python3 - "$KANBAN_DB" "$NONCE" <<'PY'
   import sqlite3, sys, time, json
   db, nonce = sys.argv[1], sys.argv[2]                     # $KANBAN_DB = the testbed's resolved board
   now = int(time.time()); blocked_at = now - 25*3600       # > 24h stale
   c = sqlite3.connect(db)
   def card(cid, token):
       c.execute("INSERT INTO tasks (id,title,status,assignee,created_by,created_at) VALUES (?,?,?,?,?,?)",
                 (cid, f"Silent chain {token}", "blocked", "worker", "test", blocked_at))
       c.execute("INSERT INTO task_events (task_id,kind,payload,created_at) VALUES (?,?,?,?)",
                 (cid, "blocked", json.dumps({"reason": "awaiting worker input"}), blocked_at))
   card(f"f39a-{nonce}", f"F39A-{nonce}")                   # card A: 0 prior nudges → one nudge
   card(f"f39b-{nonce}", f"F39B-{nonce}")                   # card B: 2 prior unanswered nudges → escalate
   for k in (1, 2):                                          # card B: the two prior supervisor nudges
       body = f"[supervise-issues nudge] ping {k} — worker, F39B-{nonce}"
       c.execute("INSERT INTO task_comments (task_id,author,body,created_at) VALUES (?,?,?,?)",
                 (f"f39b-{nonce}", "orchestrator", body, blocked_at + k*60))          # kanban_show reads here
       c.execute("INSERT INTO task_events (task_id,kind,payload,created_at) VALUES (?,?,?,?)",
                 (f"f39b-{nonce}", "commented", json.dumps({"author": "orchestrator", "len": len(body)}),
                  blocked_at + k*60))
   # no assignee-authored comment/event on either card → the assignee has not acted
   c.commit(); c.close(); print("seeded", nonce)
   PY
   ```
   Then assert card B can take the in-place owner-input classification: `kanban_block` on an already-blocked
   card sets `block_kind` only when the card has none and no live run (`hermes_cli/kanban_db.py` `block_task`).
   A miss is **FAIL(setup)**, raised here, before the fire:
   ```bash
   python3 - "$KANBAN_DB" "$NONCE" <<'PY'
   import sqlite3, sys
   c = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
   row = c.execute("SELECT block_kind, current_run_id FROM tasks WHERE id=?", (f"f39b-{sys.argv[2]}",)).fetchone()
   assert row == (None, None), f"FAIL(setup): card B block_kind/current_run_id = {row}, both must be NULL"
   print("card B seed OK: block_kind NULL, current_run_id NULL")
   PY
   ```
   **Kanban-tab probe ($0, before the fire).** The testbed points `HERMES_BUNDLED_PLUGINS` at an empty dir, so
   a dashboard launched from it has no Kanban tab, and `step-3b.png` needs that tab. Prove the dashboard launched
   with `HERMES_BUNDLED_PLUGINS="$WT/plugins"` discovers the bundled `kanban` dashboard plugin and would mount
   its API (`hermes_cli/web_server_dashboard.py` `_discover_dashboard_plugins`, `_plugin_api_mount_skip_reason`).
   A non-zero exit is **FAIL(setup)**, reported RED before the fire:
   ```bash
   HERMES_BUNDLED_PLUGINS="$WT/plugins" "$WT/.venv/bin/python" - <<'PY'
   from hermes_cli.web_server_dashboard import _discover_dashboard_plugins, _plugin_api_mount_skip_reason
   kanban = [p for p in _discover_dashboard_plugins() if p["name"] == "kanban"]
   assert len(kanban) == 1, "FAIL(setup): no kanban dashboard plugin under $WT/plugins"
   assert _plugin_api_mount_skip_reason(kanban[0], set(), set()) is None, "FAIL(setup): kanban API would not mount"
   print("kanban tab + API OK")
   PY
   ```
   The rendered tab itself is proven only by `step-3b.png`.

## Steps

1. **Baseline** (db-local, BEFORE firing — never reuse a baseline across profiles). Write
   `$TB/f62a-baselines.json`:
   ```bash
   python3 - "$HERMES_HOME" "$JOB_ID" "$NONCE" "$KANBAN_DB" > "$TB/f62a-baselines.json" <<'PY'
   import sqlite3, os, sys, json
   root, job, nonce, kdb = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
   def ro(p): return sqlite3.connect(f"file:{p}?mode=ro", uri=True) if os.path.exists(p) else None
   def maxid(p):
       d = ro(p); return d.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0] if d else 0
   d = ro(os.path.join(root, "profiles", "orchestrator", "cron", "executions.db"))
   exec_base = [r[0] for r in d.execute("SELECT id FROM executions WHERE job_id=?", (job,))] if d else []
   kb = ro(kdb); carda, cardb = f"f39a-{nonce}", f"f39b-{nonce}"
   def nudge_ids(cid):
       return [r[0] for r in kb.execute(
           "SELECT id FROM task_comments WHERE task_id=? AND author='orchestrator' "
           "AND body LIKE '[supervise-issues nudge]%'", (cid,))] if kb else []
   def event_ids(cid):
       return [r[0] for r in kb.execute("SELECT id FROM task_events WHERE task_id=?", (cid,))] if kb else []
   def state(cid):
       row = (kb.execute("SELECT status, assignee FROM tasks WHERE id=?", (cid,)).fetchone()
              or ["", ""]) if kb else ["", ""]
       wc = (kb.execute("SELECT COUNT(*) FROM task_comments WHERE task_id=? AND author='worker'",
                        (cid,)).fetchone() or [0])[0] if kb else 0
       return {"status": row[0], "assignee": row[1], "worker_comments": wc}
   a, b = state(carda), state(cardb)
   b_kind = (kb.execute("SELECT block_kind FROM tasks WHERE id=?", (cardb,)).fetchone() or [None])[0] if kb else None
   print(json.dumps({"base_orch": maxid(os.path.join(root,"profiles","orchestrator","state.db")),
                     "base_worker": maxid(os.path.join(root,"profiles","worker","state.db")),
                     "exec_base": exec_base,
                     "card_a_nudge_ids": nudge_ids(carda), "card_a_status": a["status"],
                     "card_a_assignee": a["assignee"], "card_a_worker_comments": a["worker_comments"],
                     "card_b_nudge_ids": nudge_ids(cardb), "card_b_status": b["status"],
                     "card_b_assignee": b["assignee"], "card_b_worker_comments": b["worker_comments"],
                     "card_b_block_kind": b_kind,
                     "card_a_event_ids": event_ids(carda), "card_b_event_ids": event_ids(cardb)}))
   PY
   ```
   (The baseline must run AFTER the step-5 seed and BEFORE the fire, so `card_b_nudge_ids` captures exactly
   the two seeded pre-fire nudges — the oracle proves this set is unchanged post-fire, i.e. no third nudge —
   and `card_a_event_ids` / `card_b_event_ids` capture every pre-fire `task_events` row, so only an event the
   fire appends can count as the escalation. `card_b_block_kind` must read `null`.)
   Also record the `session_model_usage` call counter for `orchestrator` + `worker` (the budget checkpoint).
2. **Force the cron due ONCE through the scheduler** (do NOT call `hermes cron run`, `hermes chat`, or the
   skill directly — those bypass the scheduler / are a direct invocation):
   ```bash
   curl --fail-with-body -sS -X POST \
     "http://127.0.0.1:$PORT/p/orchestrator/api/jobs/$JOB_ID/run" \
     -H "Authorization: Bearer $API_SERVER_KEY" -H "Content-Type: application/json" -d '{}'
   ```
   → expect HTTP 200 (`trigger_job` sets `next_run_at=now`). The orchestrator profile's scheduler tick
   (≤60s) then fires the rendered job via `run_one_job`: the sweep, in the cron session admitted for this
   execution, escalates card B with `kanban_block(kind="needs_input")`, and `deliver: bot-chat` wakes the
   orchestrator's own `Bot Chat` turn, which sends card A's `message_agent` nudge.
3. **Bounded wait, then run the oracle** (≤ `timeout_s`; poll until `oracle.py` passes or the bound is hit).
   The tester's live tier (the `hermes-testbed` harness) sets and ENFORCES `LIVE_MODEL_CALLS_MAX=40` /
   `LIVE_BUDGET_USD=5` as a hard stop — it aborts the scenario when the summed `session_model_usage` call
   count crosses 40 (the call count is the operative bound; cost may read `unknown_pricing`/$0, so record it
   but do not rely on the dollar figure). **NO re-drive**: an inconclusive run (fire did not complete,
   orchestrator turn errored, or the oracle never reaches PASS within the bound) is a **FAIL** escalated to
   hermes-architect — never retried.
   ```bash
   python3 "$SCN/oracle.py" --home "$HERMES_HOME" --kanban-db "$KANBAN_DB" \
     --job-id "$JOB_ID" --nonce "$NONCE" --baselines "$TB/f62a-baselines.json"
   ```

## Pass

The criterion holds **iff BOTH** of these hold; a missing or non-conforming screenshot means no PASS, reported
RED to the Orchestrator the same turn:
- `oracle.py` exits 0 and prints exactly:
  ```
  PASS cron=1 cron_inbound=1 message_agent_calls=1 sent_results=1 worker_bot_chat_inbounds=1 escalations=1 card_a_new_comments=1 card_b_nudges=2 card_b_calls=0 card_b_inbounds=0 required_nudges=1 sent_nudges=1 card_a_escalations=0
  ```
- all four screenshots `step-1.png`, `step-2.png`, `step-3.png` and `step-3b.png` are captured as §Evidence
  specifies.

`oracle.py` is the authoritative state gate; it enforces, all against the post-baseline current run (the
nudge half on the `sessions.title='Bot Chat'` join, the escalation on the admitted cron session):
1. **cron fired through the scheduler** — exactly one NEW `completed` execution for `$JOB_ID` in
   `profiles/orchestrator/cron/executions.db` (`cron=1`).
2. **deliver:bot-chat woke the orchestrator's own Bot Chat** — exactly one post-baseline `role='user'`
   inbound with the `Cronjob "supervise-issues" output` envelope (`cron_inbound=1`).
3. **(a) message_agent nudge to card A's assignee** — exactly one parsed `message_agent` call in a
   post-baseline assistant `tool_calls` array targeting `worker` and carrying `F39A-$NONCE`
   (`message_agent_calls=1`), correlated by `tool_call_id` to exactly one `status:"sent"` tool result
   (`sent_results=1`).
4. **(b) the nudge landed in the assignee's Bot Chat** — exactly one post-baseline `role='user'` inbound in
   `worker`'s `Bot Chat` with `Message from 🤖 orchestrator (@orchestrator): …F39A-$NONCE…`
   (`worker_bot_chat_inbounds=1`).
5. **(c) card B escalated to the human as one recorded owner-input act, no third nudge** (`escalations=1`):
   - a post-baseline orchestrator `kanban_block` call with `task_id=f39b-$NONCE` and `kind="needs_input"`,
     correlated by `tool_call_id` to a later `role='tool'` result in the same session whose JSON has
     `ok: true`, `task_id=f39b-$NONCE` and `block_kind="needs_input"`;
   - that session is the cron session admitted for the one new execution E
     (`session_admissions.target_session_id` WHERE `request_id='cron:$JOB_ID:<E.id>'` AND
     `principal_id='cron-owner'`), and no other: a call in the Bot Chat turn or an unrelated session does not
     count;
   - exactly one post-baseline `task_events` row on card B with kind `blocked` and payload kind `needs_input`,
     whose `created_at` is in `[floor(E.claimed_at), ceil(E.finished_at)]`, and card B's `tasks.block_kind`
     still `needs_input`;
   - no post-baseline needs_input `blocked` event on card A (`card_a_escalations=0`), and the baseline
     `card_b_block_kind` was `null`;
   - for card B: zero `message_agent` calls (`card_b_calls=0`), zero `worker` inbounds (`card_b_inbounds=0`),
     and its on-card `[supervise-issues nudge]` `task_comments` count still exactly two (`card_b_nudges=2`).

   A refused extra `kanban_block` call is ignored. No reply text is gated: neither the wording of the reason or
   the reply, nor where `F39-ESC:` sits in either.
6. **on-card durable nudge + reconciliation** — card A gained exactly one `[supervise-issues nudge]`
   `task_comments` row (`card_a_new_comments=1`), and `sent_nudges == required_nudges == 1`.

Any metric ≠ its target, a non-zero exit, or an inconclusive fire is a **FAIL** (no PASS-by-substitution,
no re-drive). Verdict classes for the escalation (ADR §Acceptance criteria):
- **counted FAIL:** no recorded act (`escalations=0` and no `kanban_block` call for card B in any orchestrator
  session), a third nudge, or any act on card A;
- **WATCH (timing):** the act is recorded on card B, but in a later turn instead of the sweep — the same
  execution's cron-delivered Bot Chat turn or a later one, as shown by the call row's session and id. An
  unrelated session or a stale delivery is not WATCH;
- the nudge-half clauses and FAIL(env) / FAIL(setup) keep their meanings.

## Evidence

**Semantic gate (headless — the state that decides PASS/FAIL):** the api_server platform serves the fire
route but NOT the web SPA, so the semantic pass gate is on disk (`oracle.py` + state.db); the required UI
screenshots below corroborate it and are captured from a separately-launched dashboard post-sweep.
- `oracle.py`'s stdout (the `PASS …` line) captured to `scenario-AC-LOOP-F39-6/oracle.txt` — the semantic gate.
- The three oracle clauses as stdlib `python3 -c "import sqlite3 …"` (read-only `file:<path>?mode=ro`)
  dumps against the live stores, each labelled with what it proves: (a) the orchestrator `Bot Chat`
  assistant `tool_calls` row carrying the `message_agent` nudge to `worker` + `F39A-$NONCE` and its
  `status:"sent"` result; (b) the `worker` `Bot Chat` `role='user'` inbound `Message from 🤖 orchestrator
  (@orchestrator): …F39A-$NONCE…`; (c) the cron-session `kanban_block` call row and its result, the card B
  needs_input `task_events` row and `tasks.block_kind`, the `session_admissions` row for
  `cron:$JOB_ID:<E.id>`, E's `claimed_at` / `finished_at` + card B's unchanged two-nudge `task_comments` set.
- `execution.txt` (the one `source='builtin'` cron execution, `status=completed`), the `gateway.log`
  excerpt for the fired turn, `onboard.log`, the rendered `orchestrator/cron/jobs.json`
  (`deliver:bot-chat` + interval 720 + `JOB_ID`), `f62a-baselines.json`, and the `session_model_usage`
  call counter before/after (each < 40; cost recorded, may read `unknown_pricing`).

**Required UI corroboration (post-sweep):** the screenshots require the web SPA, which is a SEPARATE server
(`hermes dashboard`, `hermes_cli/main.py:11995`) — it does not serve the fire route, so it is launched only
AFTER `oracle.py` passes, from the same sourced harness env (so `HERMES_KANBAN_HOME` resolves the same board),
against the same `$HERMES_HOME` on its own port, with the bundled plugins pointed at the worktree so the
Kanban tab exists:
`HERMES_BUNDLED_PLUGINS="$WT/plugins" hermes dashboard --host 127.0.0.1 --port "$DASH_PORT" --no-open`
(resume the exact session by id, mirroring AC-FLEET-F62-7's UI + state.db pairing). `oracle.py` remains the
semantic state gate, but the AC row remains **FAIL** if any of the four required screenshots below cannot be
captured (the live-scenario contract requires user-visible artifacts). On the 503-blocked run there were no
nudge/escalation bubbles to capture, which is consistent with the FAIL.
- `scenario-AC-LOOP-F39-6/step-1.png` — the orchestrator's `Bot Chat` showing the card-A `message_agent`
  nudge to `worker` carrying `F39A-$NONCE` (observation a).
- `scenario-AC-LOOP-F39-6/step-2.png` — `worker`'s `Bot Chat` showing the `Message from 🤖 orchestrator
  (@orchestrator): …F39A-$NONCE…` inbound (observation b).
- `scenario-AC-LOOP-F39-6/step-3.png` — the admitted cron session (`target_session_id` of
  `cron:$JOB_ID:<E.id>`), opened by id, showing the card-B `kanban_block` call and NO third nudge to
  `worker` (observation c).
- `scenario-AC-LOOP-F39-6/step-3b.png` (**required, no "page absent" escape**) — card B's Kanban **drawer** on
  the dashboard's `/kanban` tab: the title with `F39B-$NONCE` and, under Events (newest first), the NEW
  `blocked` event whose payload line reads `"kind":"needs_input"`. The blocked column alone proves nothing,
  because card B was blocked before the fire.
