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
# Each sx call is a fresh login shell, so every variable a later in-sandbox command reads is exported here,
# not in the step that first sets it. HERMES_HOME: the image leaves it unset and Hermes uses $HOME/.hermes
# (= /sandbox/.hermes, hermes_constants.py:59, :71-74). HERMES_MANAGED_DIR: read by phase-a, compose, serve,
# install and onboard; Hermes resolves it only from the env var or /etc/hermes (managed_scope.py:65-71), and the
# image has no /etc/hermes. PY: the Hermes venv's python.
HX='export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"; export HERMES_MANAGED_DIR="$HERMES_HOME/managed"; PY=/opt/hermes/.venv/bin/python; [ -x "$PY" ] || PY=$(sed -n "1s/^#!//p" "$(command -v hermes)" 2>/dev/null);'
sx() {  # sx [-t <secs>] '<command>': run it in $SB after $HX; -t bounds it (timeout 0 = no bound)
  local t=0; [ "$1" = -t ] && { t=$2; shift 2; }
  timeout "$t" openshell sandbox exec "$SB" -- sh -lc "$HX $1"
}
stage() {  # as in AC-OSH-F64-5: copy a small local dir into the sandbox through exec argv
  b64=$(tar -C "$(dirname "$1")" --exclude=__pycache__ -czf - "$(basename "$1")" | base64 -w0)
  sx "mkdir -p $2 && echo $b64 | base64 -d | tar -C $2 -xzf -"
}
```

**Every command run inside `osh-f64c-gw` goes through `sx`.** That includes the parent's in-sandbox
steps (1, 2a, 3), the install and the onboard. `install-into-sandbox.sh` itself refuses an unset
`HERMES_HOME` (`plugins/nv-coworker-compose/openshell/install-into-sandbox.sh:66-71`). Host-side
commands (`mkdir`, `cp`, `curl`, `socat`, the host redirections, `openshell sandbox create|delete`,
`openshell forward`) stay outside `sx`.

1. **Lane-local gateway policy root.** Create it and seed the gateway policy from the parent lane's copy
   (a read; nothing is written under `osh-f64/`):
   `mkdir -p $ROOT && cp /workspace/extra/hermes-fleet-testbed/osh-f64/policy-gateway.yaml $ROOT/policy-gateway.yaml`.
   That source file is a lane-host prerequisite: the parent AC-OSH-F64-4 Setup creates its gateway from the same
   file. If it is missing, this scenario cannot provision; record it as `FAIL(env)`.
2. **Provision and onboard the fleet exactly as the parent's `## Setup` does**, with the prefix and root
   substitutions, every in-sandbox command run through `sx`, and nothing else:
   - `openshell sandbox create --name osh-f64c-gw --from osh-f64-gateway:pinned --policy $ROOT/policy-gateway.yaml`
     (the image tag is the parent's pinned GATEWAY image, not a sandbox name, so it is unchanged).
     Then confirm the home resolves:
     `sx 'echo "home=$HERMES_HOME py=$PY"; ls -d "$HERMES_HOME" && "$PY" -c "print(1)"' | tee $ART/scenario-$AC/env.txt` →
     expect `home=/sandbox/.hermes`, the directory listed, and `1`.
     Then stage the lane-local spec and the helpers, which serve's readiness wait below already reads:
     `stage $ART/scenario-$AC/spec '"$HERMES_HOME/.osh-f64c"'`; `stage $SCN/helpers '"$HERMES_HOME/.osh-f64c"'`. Every `<SPEC>`
     below is `$SPEC` in a DOUBLE-quoted `sx` command: `$SPEC` expands on the tester side to the literal text
     `"$HERMES_HOME/.osh-f64c/spec/coworker-types.yaml"`, whose `$HERMES_HOME` then expands in the sandbox. Inside
     those double quotes, write every other in-sandbox `$` as `\$`.
   - `REF` is the PR head under test, resolved at run time. Phase-a and the install both install the fleet plugins
     from the fork at `--ref $REF`, so the image's fork mirror (`/opt/hermes/fork.git`, observed on the lane image) must
     carry it; if it does not, the lane image predates this head and the run stops as `FAIL(env)`:
     ```bash
     REF=$(git -C "$WT" rev-parse HEAD)
     sx "git -C /opt/hermes/fork.git cat-file -e $REF^{commit}" || { echo "image mirror lacks $REF: FAIL(env)" >&2; exit 1; }
     ```
   - Parent step 1, the managed-dir scan guard. `HX` already exports `HERMES_MANAGED_DIR`:
     `sx 'mkdir -p "$HERMES_MANAGED_DIR" && cp /tmp/osh-lane/managed/config.yaml "$HERMES_MANAGED_DIR/"'`.
   - Parent step 2: the broker host-policy mirror. Phase-a with the lane-local spec, from the image's own copy of the
     fleet plugin (observed on the lane image), then compose:
     ```bash
     sx "cd \"\$HERMES_HOME/plugins/nv-coworker-compose/openshell\" && \"\$PY\" installer.py phase-a --spec $SPEC --ref $REF"
     sx "hermes coworker compose $SPEC --out \"\$HERMES_HOME/.osh-f64c/render\""
     ```
     Mirror each role's policy with `mkdir -p $ROOT/render/<role>` and
     `sx 'cat "$HERMES_HOME/.osh-f64c/render/<role>/policy-<role>.yaml"' > $ROOT/render/<role>/policy-<role>.yaml`,
     for every role the installer provisions.
   - Parent step 3. The token is generated at run time and exported in the SAME `sx` call that starts `hermes serve`
     (a separate remote shell would not keep it). `serve` listens on its release defaults, `127.0.0.1:9119`
     (`hermes_cli/subcommands/dashboard.py:26-31`), which the dashboard's `$P` does not collide with:
     ```bash
     TOK=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
     sx "export HERMES_DASHBOARD_SESSION_TOKEN=$TOK; setsid nohup hermes serve --host 127.0.0.1 --port 9119 > \"\$HERMES_HOME/osh-f64c-serve.log\" 2>&1 < /dev/null &"
     sx -t 300 "until \"\$PY\" \"\$HERMES_HOME/.osh-f64c/helpers/health_ok.py\" 9119; do sleep 2; done"
     GW_URL="ws://127.0.0.1:9119/api/ws?token=$TOK"
     ```
   - The install, from the same image copy of the fleet plugin, with the `REF` resolved above. It runs in the
     BACKGROUND and is polled, as the parent lane ran it. The image has no service manager, so the installer's final
     `hermes gateway restart` runs the gateway in the foreground (release `hermes_cli/gateway.py:8888-8896`, `:6426`)
     and the install never returns, while the broker cuts any one exec at about 300 s. The wrapper records the
     install's exit code in `osh-f64c-install.rc` if it ever exits:
     ```bash
     W='w() { f="$HERMES_HOME/gateway_state.json"; [ -e "$f" ] || { echo absent; return; }; "$PY" -c "import json,sys; d=json.load(open(sys.argv[1])); print(d[\"pid\"], d[\"start_time\"], d[\"gateway_state\"])" "$f" 2>/dev/null || echo ambiguous; };'
     BASE=$(sx "$W"' touch "$HERMES_HOME/osh-f64c-install.start"; sleep 1; for k in 1 2 3 4 5; do b=$(w); [ "$b" != ambiguous ] && break; sleep 1; done; echo "$b"') || BASE=ambiguous
     echo "baseline: $BASE" | tee $ART/scenario-$AC/install-poll.txt
     [ "$BASE" != ambiguous ] || { echo "baseline gateway_state.json unreadable: FAIL" >&2; exit 1; }
     B0=$(echo "$BASE" | cut -d' ' -f1,2)
     sx "setsid nohup sh -c '\"\$0\" \"\$@\"; echo \$? > \"\$HERMES_HOME/osh-f64c-install.rc\"' \"\$HERMES_HOME/plugins/nv-coworker-compose/openshell/install-into-sandbox.sh\" $SPEC --ref $REF --gateway-url '$GW_URL' --policy-root $ROOT > \"\$HERMES_HOME/osh-f64c-install.log\" 2>&1 < /dev/null &"
     RC='if [ -s "$HERMES_HOME/osh-f64c-install.rc" ]; then echo "exited rc=$(cat "$HERMES_HOME/osh-f64c-install.rc")"; exit 0; fi;'
     POLL="i=0; while [ \$i -lt 70 ]; do $RC"
     POLL+=' if [ "$HERMES_HOME/gateway_state.json" -nt "$HERMES_HOME/osh-f64c-install.start" ]; then s=$(w); [ -e "$HERMES_HOME/osh-f64c-install.first-state.json" ] || cp "$HERMES_HOME/gateway_state.json" "$HERMES_HOME/osh-f64c-install.first-state.json";'
     POLL+=" case \"\$s\" in ambiguous|absent) ;; *' running') if [ \"\${s% *}\" != \"\$B0\" ]; then $RC echo \"ready \$s\"; exit 0; fi;; esac; fi;"
     POLL+=' sleep 3; i=$((i+1)); done; echo waiting'
     INSTALL=waiting
     for slice in 1 2 3 4 5 6 7 8 9 10; do
       INSTALL=$(sx -t 240 "$W B0='$B0'; $POLL") || INSTALL=waiting
       echo "slice $slice: $INSTALL" | tee -a $ART/scenario-$AC/install-poll.txt
       [ "${INSTALL%% *}" = waiting ] || break
     done
     sx -t 60 'cat "$HERMES_HOME/osh-f64c-install.first-state.json" 2>/dev/null || echo none' | sed 's/^/first post-start gateway_state.json: /' >> $ART/scenario-$AC/install-poll.txt
     sx -t 60 'cat "$HERMES_HOME/osh-f64c-install.log"' > $ART/scenario-$AC/install.log
     { if [ "${INSTALL%% *}" = ready ]; then echo 'restart_exit=pending (foreground gateway running)'; else echo "restart_exit=none (install $INSTALL)"; fi
       cat $ART/scenario-$AC/install.log; } > $ART/scenario-$AC/gateway-restart.log
     [ "${INSTALL%% *}" = ready ] || { echo "install not ready ($INSTALL): FAIL" >&2; tail -40 $ART/scenario-$AC/install.log >&2; exit 1; }
     ```
     The broker rejects any exec argument that contains a newline, so the poll body is assembled into `POLL` as ONE line.
     Before the launch, `BASE` records the writer `(pid, start_time)` of any existing `gateway_state.json`, or `absent`;
     every write stamps its writer's own pair (release `gateway/status.py:1206-1209`, `:666-669`). Ready means the file is
     newer than the start marker (the one-second gap keeps `-nt` exact on whole-second timestamps), reports
     `"gateway_state":"running"`, and names a writer other than `BASE`. A snapshot that does not parse is inconclusive,
     never ready. The exit marker is checked before the status is read and again after it: once the install has exited,
     its in-process gateway has exited with it, so `exited rc=<n>` fails Setup whatever the code. So does the overall
     bound expiring: ten slices of at most 240 s, about 40 min, well above the parent lane's install, which was still
     running 13 min after launch. `install-poll.txt` records the baseline, each slice and the first post-start snapshot.
     The launch
     argument is double-quoted on purpose: `$SPEC`, `$REF`, `$GW_URL` and `$ROOT` are tester-side values, and the single
     quotes keep the URL's `?token=` intact in the sandbox shell.
   - The grant preflight.
   - The post-install onboard, `sx "hermes -p default onboard coworker $SPEC --gateway-url '$GW_URL'"`, asserting
     that `orchestrator` and `builder` each hold a canonical hidden `Bot Chat` plus a `ui_meta['hermes-bots']`
     entry.
   - The parent's `gateway-restart.log` capture is written by the block above: while the gateway runs, the restart has
     no exit code yet, so the file records `restart_exit=pending` followed by the install's output (on a failed install,
     `restart_exit=none` and the install's state).
3. **Stage and confirm the PR head's nv-bot-chat** in the gateway's user plugins (the installer installs only
   the fleet plugins): `stage $WT/plugins/nv-bot-chat '"$HERMES_HOME/plugins"'` (the helpers were staged in step 2);
   then `sx 'grep -A6 "^plugins:" "$HERMES_HOME/config.yaml" | grep -q nv-bot-chat || hermes -p default plugins enable nv-bot-chat'`.
4. **Dashboard and the ONE forward**, as AC-OSH-F64-5 Setup steps 6–8:
   - Start the dashboard: `sx "export HERMES_WEB_DIST=/opt/hermes/hermes_cli/web_dist; setsid nohup hermes dashboard --host 127.0.0.1 --port $P --no-open --skip-build > \"\$HERMES_HOME/osh-f64c-dashboard.log\" 2>&1 < /dev/null &"`.
     `--skip-build` is needed because the sandbox has no npm egress, and `HERMES_WEB_DIST` because the image's
     prebuilt SPA is not at the default `PROJECT_ROOT/hermes_cli/web_dist` (see AC-OSH-F64-5 Setup 6).
   - Wait until `/api/health` reports `"ok": true`:
     `sx -t 300 "until \"\$PY\" \"\$HERMES_HOME/.osh-f64c/helpers/health_ok.py\" $P; do sleep 2; done"`.
   - Open the ONE forward exactly as AC-OSH-F64-5 Setup 7 does: log the command to `forwards.txt`, start it in the
     background (`> $ART/scenario-$AC/forward-start.log 2>&1 &`, pid to `forward-client.pid`), then wait up to 300 s
     until the colour-stripped `openshell forward list` shows the `osh-f64c-gw 172.17.0.1 $P <pid> running` row,
     with the same `sed` + `grep -E` (saved to `forward-list.txt`). A timeout is a
     Setup failure; the start is never retried.
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
  sx "\"\$PY\" \"\$HERMES_HOME/.osh-f64c/helpers/evidence_row.py\"$quoted_args" | tee -a "$ART/scenario-$AC/evidence.txt"
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
3. **Wire, then deliver.** Run `sx 'hermes wire add orchestrator builder'`,
   then ask the orchestrator in the same chat to send to the builder again → refresh `orchestrator` in the tab →
   expect: a newer `tool` row carrying the delivery confirmation → `step-3.png`. Evidence: `echo step-3 >> evidence.txt;
   row orchestrator <tip> tool message_agent '"status": "sent"'`. The needle selects the delivery RESULT by its content: a
   successful send returns `{"status": "sent", "to": …}` (release `tools/bot_mode_dm.py:705-714`), while step 2's refusal
   is an `{"error": …}` payload (`:233-238`). Its `row <msg-id>` must also be newer than step 2's.
4. **The builder's delivery turn.** This is parent step 4 with the prefix substitution: a `write_file` plan to
   `.hermes/plans/builder.md`, then the `terminal` change inside `osh-f64c-builder`, writing
   `/tmp/osh-f64c-builder.marker`. The delivery child runs `-c "Bot Chat"` (`tools/bot_mode_dm.py:361-378`).
   Open the tab, **← All Bot Chats**, then `builder` → `timeout 600 agent-browser wait --text "P7-BUILT:$NONCE"` →
   expect: the builder's `P7-BUILT:<nonce>` reply in its canonical Bot Chat → `step-4.png`. Evidence:
   `echo step-4 >> evidence.txt; row builder <tip> assistant "" "P7-BUILT:$NONCE"`. The parent's routing and
   isolation probes may be recorded too, but they are not graded here.
5. **The veto, in the builder's Bot Chat.** Run the worker turn inside `osh-f64c-gw` through the local-teammate
   transport (`tools/bot_mode_dm.py:31-33`), so the attempt lands in that Bot Chat:
   `sx 'printf "%s\n" "Use the cronjob_manage tool to create an hourly cron job named osh-f64c-probe that says hello." > /tmp/osh-f64c-q5.txt && hermes -p builder chat --in ~ -c "Bot Chat" -Q --query-file /tmp/osh-f64c-q5.txt'`.
   Then refresh `builder` in the tab → expect: a `tool` row carrying the `cronjob_manage` denial that names
   `orchestrator-only` → `step-5.png`. Evidence: `echo step-5 >> evidence.txt; row builder <tip> tool cronjob_manage orchestrator-only`.
6. **Teardown.** `kill $(cat $ART/scenario-$AC/socat.pid)`; `kill $(cat $ART/scenario-$AC/forward-client.pid) 2>/dev/null`; then delete every `osh-f64c-*` sandbox and policy
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
  - steps 2 and 3: `tool` rows with `tool_name` `message_agent`, step 3's newer and containing `"status": "sent"`;
  - step 4: an `assistant` row containing `P7-BUILT:<nonce>`;
  - step 5: a `tool` row with `tool_name` `cronjob_manage` whose content names `orchestrator-only`.
  A `row none` on any step is a FAIL for that step.
- **One forward:** `forwards.txt` holds exactly one `openshell forward start $P osh-f64c-gw -d`.
- **One gateway:** append `sx '"$PY" "$HERMES_HOME/.osh-f64c/helpers/gateway_count.py"'`
  to `evidence.txt`. It must print `gateway_processes 1`; the `serve_processes` line it also prints is recorded,
  not gated.
- **Call / $ totals** within the per-scenario cap (40 calls / $5). Append the stdlib totals over the gateway's
  default, orchestrator and builder stores to `evidence.txt`:
  `sx '"$PY" "$HERMES_HOME/.osh-f64c/helpers/usage_totals.py"' | tee -a $ART/scenario-$AC/evidence.txt` → `calls <n> usd <x>`.
- `bot-chats.json` (the last listing), plus the parent's `gateway-restart.log`.
