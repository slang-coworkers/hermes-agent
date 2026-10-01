---
ac: AC-OSH-F64-4
kind: live
model: live
base_url: inference-api.nvidia.com
api_mode: anthropic_messages
model_id: aws/anthropic/bedrock-claude-opus-4-8
key_env: ANTHROPIC_API_KEY
fixtures:
  - fixtures/osh-f64c-gateway
  - fixtures/osh-f64c-worker-orchestrator
  - fixtures/osh-f64c-worker-builder
LIVE_MODEL_CALLS_MAX: 40
LIVE_BUDGET_USD: 5
LIVE_ROUND_CALLS_MAX: 120
LIVE_ROUND_BUDGET_USD: 15
timeout_s: 600
---

# AC-OSH-F64-4 (evidence clause only) — the room screenshots, captured from the canonical hidden Bot Chats through the nv-bot-chat tab (live)

Carried from OSH-F64 by operator ruling 3758, **evidence clause only**. OSH-F64 could not capture the
room screenshots `scenario-AC-OSH-F64-4/step-1..5.png`, for the same reason as AC-5: the stock web
dashboard cannot open a hidden Bot Chat. This scenario re-runs the parent's two-bot exchange on an
`osh-f64c-` fleet, then captures each event from the canonical hidden Bot Chats through the nv-bot-chat
tab, on ONE forwarded dashboard URL. Steps 1–3 land in the orchestrator's Bot Chat, steps 4–5 in the
builder's. AC-4's behaviour clauses (delivery, wiring gate, sandbox routing/isolation, veto, one gateway)
stay PASS on OSH-F64 and are **not re-graded here**.

The front-matter provider block is the parent's (`tests/e2e-scenarios/OSH-F64/AC-OSH-F64-4.md`) unchanged:
`inference-api.nvidia.com` / `anthropic_messages` / `aws/anthropic/bedrock-claude-opus-4-8`, dummy
`ANTHROPIC_API_KEY` (the OneCLI proxy injects the real one), per-scenario cap 40 calls / $5.

**Lane.** Serialized behind the OSH-F64 lane: never run while the OSH-F64 tester holds it. Every sandbox and
policy name uses the `osh-f64c-` prefix (gateway `osh-f64c-gw`, workers `osh-f64c-orchestrator` /
`osh-f64c-builder`). Host-side writes go only under `/workspace/extra/hermes-fleet-testbed/osh-f64c`;
in-sandbox renders go under `$HERMES_HOME/.osh-f64c/render`. Nothing is written under the OSH-F64 lane's
`/workspace/extra/hermes-fleet-testbed/osh-f64`. If the run uses the `brev-hermes` fleet host instead of a
throwaway `osh-f64c-gw`, the same lane-local roots apply.

## Setup

The `fixtures:` list is installed before this section, exactly as for the parent's three fixtures
(`osh-f64c-gateway` is the gateway/default seed, the two workers the served coworkers); nothing here
re-installs one. The fixtures carry only the ADR's deltas: `osh-f64-` → `osh-f64c-` names, and
`nv-bot-chat` added to the gateway's `plugins.enabled`, which the installer UNIONs with the spine set.

```bash
SCN=$WT/tests/e2e-scenarios/OSH-F64.c; AC=AC-OSH-F64-4; mkdir -p $ART/scenario-$AC
ROOT=/workspace/extra/hermes-fleet-testbed/osh-f64c        # lane-local host policy/render root
SB=osh-f64c-gw; P=<free port in 29000-29999>
# Lane-local spec: the parent spec at this head with ONLY the project name changed. `project:` is what
# names the worker sandboxes (<project>-<role>), so this keeps them osh-f64c-*.
mkdir -p $ART/scenario-$AC/spec && cp -r $WT/tests/e2e-scenarios/OSH-F64/spec/. $ART/scenario-$AC/spec/
sed -i 's/^project: osh-f64$/project: osh-f64c/' $ART/scenario-$AC/spec/coworker-types.yaml
grep -n '^project:' $ART/scenario-$AC/spec/coworker-types.yaml      # must print: project: osh-f64c
SPEC='"$HERMES_HOME/.osh-f64c/spec/coworker-types.yaml"'   # in-sandbox path; staged in step 2, expands inside the sandbox
stage() {  # as in AC-OSH-F64-5: copy a small local dir into the sandbox through exec argv
  b64=$(tar -C "$(dirname "$1")" --exclude=__pycache__ -czf - "$(basename "$1")" | base64 -w0)
  openshell sandbox exec $SB -- sh -lc "mkdir -p $2 && echo $b64 | base64 -d | tar -C $2 -xzf -"
}
```

1. **Lane-local gateway policy root.** Create it and seed the gateway policy from the parent lane's copy
   (a read; nothing is written under `osh-f64/`):
   `mkdir -p $ROOT && cp /workspace/extra/hermes-fleet-testbed/osh-f64/policy-gateway.yaml $ROOT/policy-gateway.yaml`.
   That source file is a lane-host prerequisite: the parent AC-OSH-F64-4 Setup creates its gateway from the same
   file. If it is missing, this scenario cannot provision; record it as `FAIL(env)`.
2. **Provision and onboard the fleet exactly as the parent's `## Setup` does**, with the prefix and root
   substitutions and nothing else:
   - `openshell sandbox create --name osh-f64c-gw --from osh-f64-gateway:pinned --policy $ROOT/policy-gateway.yaml`
     (the image tag is the parent's pinned GATEWAY image, not a sandbox name, so it is unchanged).
     Then stage the lane-local spec: `stage $ART/scenario-$AC/spec '"$HERMES_HOME/.osh-f64c"'`. Every `<SPEC>`
     below is `$SPEC`, used inside a single-quoted remote command.
   - Parent step 1: the managed-dir scan guard, inside `osh-f64c-gw`.
   - Parent step 2: the broker host-policy mirror. Phase-a with the lane-local spec, then
     `openshell sandbox exec osh-f64c-gw -- sh -lc 'hermes coworker compose <SPEC> --out "$HERMES_HOME/.osh-f64c/render"'`.
     Mirror each role's policy with `mkdir -p $ROOT/render/<role>` and
     `openshell sandbox exec osh-f64c-gw -- sh -lc 'cat "$HERMES_HOME/.osh-f64c/render/<role>/policy-<role>.yaml"' > $ROOT/render/<role>/policy-<role>.yaml`,
     for every role the installer provisions.
   - Parent step 3: `HERMES_DASHBOARD_SESSION_TOKEN` + `hermes serve` + `$GW_URL`.
   - The install: `install-into-sandbox.sh <SPEC> --ref <sha> --gateway-url "$GW_URL" --policy-root $ROOT`.
   - The grant preflight.
   - The post-install `hermes -p default onboard coworker <SPEC> --gateway-url "$GW_URL"`, asserting that
     `orchestrator` and `builder` each hold a canonical hidden `Bot Chat` plus a `ui_meta['hermes-bots']`
     entry.
   - Keep the parent's `gateway-restart.log` capture.
3. **Stage and confirm the PR head's nv-bot-chat** in the gateway's user plugins (the installer installs only
   the fleet plugins): `stage $WT/plugins/nv-bot-chat '"$HERMES_HOME/plugins"'`; `stage $SCN/helpers '"$HERMES_HOME/.osh-f64c"'`;
   then `openshell sandbox exec $SB -- sh -lc 'grep -A6 "^plugins:" "$HERMES_HOME/config.yaml" | grep -q nv-bot-chat || hermes -p default plugins enable nv-bot-chat'`.
4. **Dashboard and the ONE forward**, as AC-OSH-F64-5 Setup steps 6–8:
   - Start the dashboard: `openshell sandbox exec $SB -- sh -lc "setsid nohup hermes dashboard --host 127.0.0.1 --port $P --no-open --skip-build > \"\$HERMES_HOME/osh-f64c-dashboard.log\" 2>&1 < /dev/null &"`.
     `--skip-build` is needed because the sandbox has no npm egress.
   - Wait until `/api/health` reports `"ok": true`.
   - Log the forward command, then run it: `echo "openshell forward start $P $SB -d" | tee $ART/scenario-$AC/forwards.txt` followed by `openshell forward start $P $SB -d`.
   - Bridge the Host guard from loopback: `socat TCP-LISTEN:$P,fork,reuseaddr,bind=127.0.0.1 TCP:172.17.0.1:$P & echo $! > $ART/scenario-$AC/socat.pid`.
   - `URL=http://127.0.0.1:$P/`.
   - The session token for API reads is `T=$(curl -s $URL | grep -o '__HERMES_SESSION_TOKEN__="[^"]*"' | cut -d'"' -f2)`.

## Steps

Drive each step as the parent scenario's step of the same number does, except where stated. Then capture it
from the nv-bot-chat tab on the one forwarded `$URL`, using agent-browser (bounded waits, one screenshot per
step). Before each evidence query, re-read the listing (step 0's `curl`) and use that profile's current
`resolved_id` (`<tip>`); a compression mid-run moves the tip.

```bash
NONCE=$(python3 -c 'import secrets; print(secrets.token_hex(4))'); echo "nonce $NONCE" >> $ART/scenario-$AC/evidence.txt
listing() { curl -s -H "Authorization: Bearer $T" ${URL}api/plugins/nv-bot-chat/bot-chats > $ART/scenario-$AC/bot-chats.json; }
field() { python3 -c "import json,sys; d={r['profile']: r for r in json.load(open(sys.argv[1]))['bot_chats']}; print(d[sys.argv[2]][sys.argv[3]])" $ART/scenario-$AC/bot-chats.json "$1" "$2"; }
row() {  # row <profile> <session> <role> [tool_name] [needle] -> evidence.txt; %q keeps an empty tool_name as ''
  printf -v quoted_args ' %q' "$@"
  openshell sandbox exec "$SB" -- sh -lc "python3 \"\$HERMES_HOME/.osh-f64c/helpers/evidence_row.py\"$quoted_args" | tee -a "$ART/scenario-$AC/evidence.txt"
}
```

0. **The canonical ids.** Run `listing`; `ORCH_SID=$(field orchestrator session_id)`; `BUILD_SID=$(field builder session_id)`.
   Then `echo step-0 >> evidence.txt; row orchestrator $ORCH_SID assistant` → expect: the first line reads
   `session <ORCH_SID> hidden=1 title=Bot Chat` (the hidden canonical row, not a fresh session). Every later
   `row` call gets the tip; the helper walks `parent_session_id` up from it and prints the canonical ancestor on
   that line. Run
   `row builder $BUILD_SID assistant` → expect the same `hidden=1 title=Bot Chat` shape for the builder.
1. **Human → the orchestrator's canonical Bot Chat.** In agent-browser, select `orchestrator` in the
   dashboard profile combobox → expect the `orchestrator »` prompt. Then open `${URL}chat?profile=orchestrator&resume=$ORCH_SID`,
   keeping `?profile=` in the URL because the SPA's profile scope is initialised from it
   (`web/src/contexts/ProfileProvider.tsx:37-45`). Use the same URL whenever you return to the chat in steps 2–3.
   The SPA forwards both to `/api/pty?profile=orchestrator&resume=$ORCH_SID` (`web/src/pages/ChatPage.tsx:1165,1174`).
   A bare `?profile=` without `resume` can open the active-session file or a fresh session instead
   (`hermes_cli/web_server.py:17475-17494`), and `message_agent` exists only in the canonical Bot Chat. Send the parent's step-1 message, `run change P7-$NONCE`, and wait (bounded) for the orchestrator's
   reply. Then open the **Bot Chat** tab and click `orchestrator` → expect: the human line and the orchestrator's
   acceptance turn are visible → `step-1.png`. Evidence: `echo step-1 >> evidence.txt; row orchestrator <tip> assistant`.
   Steps 2–3 continue in this same resumed chat.
2. **Unwired `message_agent` → builder.** Drive the orchestrator to message the builder before any wire, as
   parent step 2 does. In the tab, click **← All Bot Chats**, then `orchestrator` → expect: a `tool` row
   carrying the wiring-gate refusal → `step-2.png`. Evidence: `echo step-2 >> evidence.txt; row orchestrator <tip> tool message_agent`.
3. **Wire, then deliver.** Run `openshell sandbox exec $SB -- sh -lc 'hermes wire add orchestrator builder'`,
   then ask the orchestrator in the same chat to send to the builder again → refresh `orchestrator` in the tab →
   expect: a newer `tool` row carrying the delivery confirmation → `step-3.png`. Evidence: `echo step-3 >> evidence.txt;
   row orchestrator <tip> tool message_agent`. Its `row <msg-id>` must be newer than step 2's.
4. **The builder's delivery turn.** This is parent step 4 with the prefix substitution: a `write_file` plan to
   `.hermes/plans/builder.md`, then the `terminal` change inside `osh-f64c-builder`, writing
   `/tmp/osh-f64c-builder.marker`. The delivery child runs `-c "Bot Chat"` (`tools/bot_mode_dm.py:361-378`).
   Open the tab, **← All Bot Chats**, then `builder` → `timeout 600 agent-browser wait --text "P7-BUILT:$NONCE"` →
   expect: the builder's `P7-BUILT:<nonce>` reply in its canonical Bot Chat → `step-4.png`. Evidence:
   `echo step-4 >> evidence.txt; row builder <tip> assistant "" "P7-BUILT:$NONCE"`. The parent's routing and
   isolation probes may be recorded too, but they are not graded here.
5. **The veto, in the builder's Bot Chat.** Run the worker turn inside `osh-f64c-gw` through the local-teammate
   transport (`tools/bot_mode_dm.py:31-33`), so the attempt lands in that Bot Chat:
   `openshell sandbox exec $SB -- sh -lc 'printf "%s\n" "Use the cronjob_manage tool to create an hourly cron job named osh-f64c-probe that says hello." > /tmp/osh-f64c-q5.txt && hermes -p builder chat --in ~ -c "Bot Chat" -Q --query-file /tmp/osh-f64c-q5.txt'`.
   Then refresh `builder` in the tab → expect: a `tool` row carrying the `cronjob_manage` denial that names
   `orchestrator-only` → `step-5.png`. Evidence: `echo step-5 >> evidence.txt; row builder <tip> tool cronjob_manage orchestrator-only`.
6. **Teardown.** `kill $(cat $ART/scenario-$AC/socat.pid)`, then delete every `osh-f64c-*` sandbox and policy
   (`openshell sandbox delete osh-f64c-gw`, `osh-f64c-orchestrator`, `osh-f64c-builder`, plus their policies
   as the parent's teardown does). Nothing outside `osh-f64c-*` and `$ROOT` is touched.

## Pass

All five events are visible in the canonical hidden Bot Chats through the nv-bot-chat tab, on the one
forwarded dashboard URL: steps 1–3 in the orchestrator's, 4–5 in the builder's. Each screenshot matches a
`state.db` row in `evidence.txt`. A step whose expected event never happens leaves nothing to capture, and
FAILS the criterion. AC-4's other behaviour gates are not re-graded here; they PASS on OSH-F64.

## Evidence

All files are under `$ART/scenario-AC-OSH-F64-4/`:

- `step-1..5.png`: tab screenshots, one per step.
- `evidence.txt`: the nonce and, per step, the `evidence_row.py` output (stdlib `sqlite3`; the coworker image
  has no `sqlite3` CLI; columns `role` / `content` / `tool_name`, `hermes_state_common.py:459-463`). Each step's
  output is a `session … hidden=1 title=Bot Chat` line plus the matching row:
  - step 1: an `assistant` row;
  - steps 2 and 3: `tool` rows with `tool_name` `message_agent`, step 3's newer;
  - step 4: an `assistant` row containing `P7-BUILT:<nonce>`;
  - step 5: a `tool` row with `tool_name` `cronjob_manage` whose content names `orchestrator-only`.
  A `row none` on any step is a FAIL for that step.
- **One forward:** `forwards.txt` holds exactly one `openshell forward start $P osh-f64c-gw -d`.
- **One gateway:** append `openshell sandbox exec $SB -- sh -lc 'python3 "$HERMES_HOME/.osh-f64c/helpers/gateway_count.py"'`
  to `evidence.txt`. It must print `gateway_processes 1`; the `serve_processes` line it also prints is recorded,
  not gated.
- **Call / $ totals** within the per-scenario cap (40 calls / $5). Append this, run inside `osh-f64c-gw`, to
  `evidence.txt`:
  `python3 -c "import sqlite3, os; h = os.environ['HERMES_HOME']; t = [sqlite3.connect(p).execute('select coalesce(sum(api_call_count),0), coalesce(sum(case when actual_cost_usd > 0 then actual_cost_usd else estimated_cost_usd end),0) from session_model_usage').fetchone() for p in (h + '/state.db', h + '/profiles/orchestrator/state.db', h + '/profiles/builder/state.db') if os.path.exists(p)]; print('calls', sum(c for c, _ in t), 'usd', round(sum(u for _, u in t), 4))"`.
- `bot-chats.json` (the last listing), plus the parent's `gateway-restart.log`.
