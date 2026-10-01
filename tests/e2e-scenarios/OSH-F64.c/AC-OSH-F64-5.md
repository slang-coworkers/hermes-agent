---
ac: AC-OSH-F64-5
kind: ui
model: stub
fixtures:
  - fixtures/osh-f64c-gw
timeout_s: 300
---

# AC-OSH-F64-5 — through one `openshell forward`, the dashboard's Bot Chat tab lists the served profiles and OPENS the orchestrator's hidden canonical Bot Chat (ui)

Carried from OSH-F64 (operator ruling 2582). Through an `openshell forward`, the hermes
dashboard (`model: stub`) lists the served profiles and OPENS the orchestrator's hidden
canonical Bot Chat in the `nv-bot-chat` tab — its transcript renders in the browser (not
merely a profile-name list); one gateway connection for the whole app — the list and the
opened transcript reuse the same forwarded multiplex-gateway attachment.

The stock web dashboard cannot do this: `GET /api/sessions` has no `include_hidden`, so the
born-hidden Bot Chat never reaches the SPA. The `nv-bot-chat` plugin closes the gap with plugin
files only (ADR OSH-F64.c §Design). No model call is made: the Bot Chats are seeded, so
`model: stub` only guarantees that nothing here can reach a live endpoint.

**Lane.** The throwaway gateway sandbox is `osh-f64c-gw` (prefix `osh-f64c-`). Never touch
`osh-f64-gw*` / `osh-f64-<role>`, and never run while the OSH-F64 tester holds the lane (broker
cap 18). One port `P` in 29000–29999 is the dashboard's in-sandbox port, the forward's port and
the loopback bridge's port (the forward exposes the sandbox port of the same number).

## Setup

The `fixtures:` list is installed before this section; nothing here re-installs it. The installed
fixture's files are the ROOT config of the sandbox: `osh-f64c-gw` is a default/multiplexer seed,
the same shape as the parent's `osh-f64-gateway`. Every `openshell sandbox exec` below wraps its
remote command in single quotes, so `$HERMES_HOME` expands INSIDE the sandbox.

```bash
SCN=$WT/tests/e2e-scenarios/OSH-F64.c; AC=AC-OSH-F64-5; mkdir -p $ART/scenario-$AC
FIX=$HERMES_HOME/profiles/osh-f64c-gw        # the installed fixture (tester's testbed home)
P=<free port in 29000-29999>
SB=osh-f64c-gw
# Copy a small local dir into the sandbox through exec argv; needs only `sandbox exec`.
stage() {  # stage <local-dir> <remote-parent, single-quoted so it expands in the sandbox>
  b64=$(tar -C "$(dirname "$1")" --exclude=__pycache__ -czf - "$(basename "$1")" | base64 -w0)
  openshell sandbox exec $SB -- sh -lc "mkdir -p $2 && echo $b64 | base64 -d | tar -C $2 -xzf -"
}
```

1. **Create the sandbox** from the lane's pinned gateway image, the parent AC-OSH-F64-5 `--from`:
   `openshell sandbox create --name osh-f64c-gw --from osh-f64-gateway:pinned --policy /workspace/extra/hermes-fleet-testbed/osh-f64/policy-gateway.yaml`
   (nothing in this scenario needs egress; the parent's gateway policy is reused as is).
2. **Stage the PR head's plugin and the fixture into the sandbox root.** The plugin goes in as a
   USER plugin (`$HERMES_HOME/plugins/nv-bot-chat`), so the dashboard serves it only because the
   fixture lists it in `plugins.enabled`. The image's own config is kept as `config.yaml.image`:
   ```bash
   stage $WT/plugins/nv-bot-chat '"$HERMES_HOME/plugins"'
   mkdir -p $ART/scenario-$AC/root-seed && cp $FIX/config.yaml $FIX/SOUL.md $ART/scenario-$AC/root-seed/
   stage $ART/scenario-$AC/root-seed '"$HERMES_HOME/.osh-f64c"'
   stage $SCN/helpers '"$HERMES_HOME/.osh-f64c"'     # seed + evidence scripts used below
   openshell sandbox exec $SB -- sh -lc 'cd "$HERMES_HOME" && { [ ! -f config.yaml ] || mv config.yaml config.yaml.image; } && cp .osh-f64c/root-seed/config.yaml .osh-f64c/root-seed/SOUL.md . && ls plugins/nv-bot-chat/dashboard/manifest.json'
   ```
   Only the fixture's own `config.yaml` + `SOUL.md` are copied (not the whole installed profile
   dir, which can hold seeded skills too large for one exec argument).
3. **Create the two served named profiles** (the fixture allowlists them):
   `openshell sandbox exec $SB -- sh -lc 'hermes profile create orchestrator --no-alias --no-skills && hermes profile create builder --no-alias --no-skills'`.
4. **Seed each served profile's hidden canonical Bot Chat** with two lines (operator + assistant).
   The canonical row is the exact-title `Bot Chat` session, `hidden=1`. `default`'s store is
   `$HERMES_HOME/state.db` (always served); the named ones live at `$HERMES_HOME/profiles/<name>/state.db`:
   ```bash
   openshell sandbox exec $SB -- sh -lc 'PY=/opt/hermes/.venv/bin/python; [ -x "$PY" ] || PY=$(head -1 "$(command -v hermes)" | sed "s/^#!//"); "$PY" "$HERMES_HOME/.osh-f64c/helpers/seed_bot_chats.py"' | tee $ART/scenario-$AC/seeded.txt
   ```
   The seed runs under the Hermes venv's python because it imports `hermes_state`
   (`create_session` → `set_session_title(…, "Bot Chat")` → `set_session_hidden(…, True)` →
   `append_message` ×2).
   `seeded.txt` must name all three profiles; the orchestrator's id is `<orch-sid>` below.
5. **Boot the ONE multiplex gateway** (no messaging platform is configured; the gateway keeps
   running for cron, `gateway/run.py:14185-14186`), and wait until its runtime state says running:
   ```bash
   openshell sandbox exec $SB -- sh -lc 'setsid nohup hermes gateway run > "$HERMES_HOME/osh-f64c-gateway.log" 2>&1 < /dev/null &'
   timeout 300 openshell sandbox exec $SB -- sh -lc 'until grep -q "\"gateway_state\": *\"running\"" "$HERMES_HOME/gateway_state.json" 2>/dev/null; do sleep 2; done'
   ```
   An exit at the api_server key guard (exit 78) comes from the image's own environment, not from
   this plugin, which enables no platform. Capture `osh-f64c-gateway.log` and report it as `FAIL(env)`.
6. **Start the dashboard inside the sandbox.** It is a separate process from the gateway.
   `--skip-build` serves the image's prebuilt SPA (`hermes_cli/web_dist`) rather than attempting an npm
   build the sandbox has no egress for (`hermes_cli/main.py:12207-12225`):
   ```bash
   openshell sandbox exec $SB -- sh -lc "setsid nohup hermes dashboard --host 127.0.0.1 --port $P --no-open --skip-build > \"\$HERMES_HOME/osh-f64c-dashboard.log\" 2>&1 < /dev/null &"
   timeout 300 openshell sandbox exec $SB -- sh -lc "until curl -fsS http://127.0.0.1:$P/api/health | grep -q '\"ok\": *true'; do sleep 2; done"
   ```
7. **Open exactly ONE forward**, and log the command as you run it:
   ```bash
   echo "openshell forward start $P $SB -d" | tee $ART/scenario-$AC/forwards.txt
   openshell forward start $P $SB -d          # brokered; binds 172.17.0.1:$P
   ```
   No other `openshell forward start` runs in this scenario. If `openshell forward --help` lists a
   listing verb, append its output to `forwards.txt`.
8. **Loopback bridge for the dashboard Host guard.** The broker binds the forward to `172.17.0.1`,
   and the dashboard's Host guard rejects that Host with HTTP 400. Bridge it from loopback so the
   browser sends a loopback `Host`, while the forward stays the only transport (`socat` is in the
   coworker image; `command -v socat` to verify, do not install):
   `socat TCP-LISTEN:$P,fork,reuseaddr,bind=127.0.0.1 TCP:172.17.0.1:$P & echo $! > $ART/scenario-$AC/socat.pid`.
   The forwarded dashboard URL is `URL=http://127.0.0.1:$P/`.

This scenario needs no profile-combobox selection and no `ui_meta.hermes-bots` write. The Bot
Chat tab lists every served profile whatever profile the SPA has selected, and it reads the session
stores directly. The roster metadata is not involved.

## Steps

1. `agent-browser open "$URL"` → `agent-browser wait --load networkidle` → `snapshot -i` → expect:
   the dashboard loads through the bridge onto the forward, and a **Bot Chat** tab (the nv-bot-chat
   tab, `/bot-chat`) is present in the sidebar nav. Screenshot `step-1.png`.
2. Click the **Bot Chat** tab (by `@ref`; or open `$URL/bot-chat`) → re-snapshot →
   `timeout 300 agent-browser wait --text "orchestrator"` → expect: the **Bot Chats** list shows
   one entry per served profile, `default`, `orchestrator` and `builder`, each titled `Bot Chat`.
   These are the hidden canonical sessions surfaced, and they are at least two served profiles
   through this one URL. Screenshot `step-2.png`.
3. Click the `orchestrator` entry (the button whose text starts `orchestrator Bot Chat`) →
   re-snapshot → `timeout 300 agent-browser wait --text "osh-f64c assistant line from orchestrator"` →
   expect: the opened view, headed `orchestrator · Bot Chat`, renders that session's transcript,
   with both seeded lines visible verbatim: `osh-f64c operator line for orchestrator` and
   `osh-f64c assistant line from orchestrator`. Read the opened session id from the view
   (`agent-browser eval "document.querySelector('.nv-bot-chat-transcript').dataset.sessionId"`) into
   `opened-session-id.txt`. Screenshot `step-3.png`.

## Pass

The criterion holds when the orchestrator's hidden canonical Bot Chat is opened from the web
dashboard through the forward and its seeded transcript renders, which stock web cannot do. At
least two served profiles are listed through the one forwarded dashboard URL. The evidence records
exactly one `openshell forward start` and exactly one gateway process in `osh-f64c-gw`, with no
profile-specific forward or gateway attachment.

## Evidence

All files are under `$ART/scenario-AC-OSH-F64-5/`:

- `step-1.png` (tab present), `step-2.png` (served-profile list), `step-3.png` (orchestrator
  transcript rendered); `seeded.txt`; `opened-session-id.txt`.
- **The opened row is the hidden canonical one, not a fresh PTY.** Run this stdlib `sqlite3` query
  (the coworker image has no `sqlite3` CLI) over the orchestrator's store, and save the output to
  `evidence.txt`. It must print `<orch-sid> 1 Bot Chat`, with the id equal to `opened-session-id.txt`:
  ```bash
  SID=$(cat $ART/scenario-AC-OSH-F64-5/opened-session-id.txt)
  openshell sandbox exec $SB -- sh -lc "python3 \"\$HERMES_HOME/.osh-f64c/helpers/bot_chat_row.py\" $SID" | tee -a $ART/scenario-AC-OSH-F64-5/evidence.txt
  ```
- **One forward:** `forwards.txt` holds exactly one `openshell forward start $P osh-f64c-gw -d`
  line, onto the dashboard's `$P`.
- **One gateway process.** Append this `/proc/*/cmdline` scan of the sandbox to `evidence.txt`
  (it works whether or not `procps` is installed). It must print `gateway_processes 1`:
  ```bash
  openshell sandbox exec $SB -- sh -lc 'python3 "$HERMES_HOME/.osh-f64c/helpers/gateway_count.py"' | tee -a $ART/scenario-AC-OSH-F64-5/evidence.txt
  ```
- `osh-f64c-gateway.log` and `osh-f64c-dashboard.log`, copied out with
  `openshell sandbox exec $SB -- sh -lc 'cat "$HERMES_HOME/osh-f64c-gateway.log"'` (and likewise for
  the dashboard log).

## Teardown

`kill $(cat $ART/scenario-AC-OSH-F64-5/socat.pid)`, then `openshell sandbox delete osh-f64c-gw`, which
also drops its forward. Nothing outside `osh-f64c-*` is touched.
