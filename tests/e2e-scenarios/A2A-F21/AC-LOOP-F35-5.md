---
ac: AC-LOOP-F35-5
kind: live
model: live
base_url: https://inference-api.nvidia.com
model_id: aws/anthropic/bedrock-claude-opus-4-8
fixtures:
  - fixtures/a2a-f21-loopf35-a
  - fixtures/a2a-f21-loopf35-b
timeout_s: 600
---

# AC-LOOP-F35-5 — two rendered bots exchange a message via message_agent inside one gateway

Two Bot-Mode-managed profiles exchange a message through `message_agent` inside the
ONE multiplexer gateway (never a2a, no second `serve`); the recipient's reply is
visible in the sender's Bot Chat. `model: live` — a real model answers behind the
OneCLI proxy (dummy `api_key`, real base URL). This criterion is carried from
LOOP-F35 (operator ruling) under its original id; it ships here as a self-contained
copy under `tests/e2e-scenarios/A2A-F21/`, NOT an edit of the merged `loop-f35`
files. Budget bound per `/hermes-ui-driver` §1f: `LIVE_MODEL_CALLS_MAX=40`,
`LIVE_BUDGET_USD=5`.

The oracle is a **distinct acknowledgement marker**, not the nonce itself: bot-a's
message carries only the nonce `$NONCE`, and bot-b (per its identity) replies with
`F35-ACK:<nonce>`. The `F35-ACK:` prefix never appears in the sender's prompt, so
observing `F35-ACK:$NONCE` proves bot-b actually generated the reply — a bare
`$NONCE` match would be satisfied by the sender's own message.

**What this scenario proves beyond delivery: the provider config survives `hermes
onboard`.** The earlier LOOP-F35 live FAIL was `hermes onboard` force-rendering the
recipient from a spine that carried only the bare `api:`/`key_env:` shape, clobbering
the fixtures' working `providers.coworkers-live` block into that bare shape → the
recipient's `message_agent` delivery subprocess (whose env is stripped of
`ANTHROPIC_*`) then resolved no provider and 404'd. The fix is entirely under
`tests/**`: this scenario's own spine (`spec/spines/live-base.yaml`) carries the FULL
`providers.coworkers-live` block with an inline dummy `api_key` and the literal remote
`base_url`, so the deep-merged render leaves a working block on disk after onboard.
The §Setup regression oracle reads the POST-onboard config to prove it survived.

## Setup

The tester installs the `fixtures:` profiles (`a2a-f21-loopf35-a`,
`a2a-f21-loopf35-b`) before this section; do not re-install them here. Both carry
the full live provider block in their `config.yaml`. `$SCN=$WT/tests/e2e-scenarios/A2A-F21`,
`AC=AC-LOOP-F35-5`, `mkdir -p $ART/scenario-$AC`. Everything below is post-install,
with the **live** env sourced (`$TB/harness.live.env`; the OneCLI egress proxy
substitutes the real credential at the `inference-api.nvidia.com` hop):

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
   `multiplex_profile_allowlist: [a2a-f21-loopf35-a, a2a-f21-loopf35-b]`
   (`/hermes-ui-driver` §1a launch; wait for the
   `HERMES_DASHBOARD_READY port=9119|Hermes Web UI` line). Both bots run as profiles
   OF THIS gateway via the multiplexer — there is no second `serve` process, because
   `message_agent` delivers inside the one gateway. The sender turn is driven by an
   INTERACTIVE real-PTY `hermes chat --cli` in bot-a's canonical `Bot Chat` (Step 1) —
   an interactive session's own loop drains the async completion queue (`cli.py:20912`
   idle / `:21146` post-turn), so held alive it surfaces bot-b's reply into bot-a's
   `Bot Chat`. NOT the `-Q` one-shot (which answers and exits WITHOUT draining —
   `cli.py:22258-22259` return → never `cli.run()`/`process_loop`; its finalize waits for
   delivery but never drains, `cli.py:1452` — the round-2 defect), and NOT the web composer
   (a fresh non-`Bot Chat` session where `message_agent` is absent —
   `tools/bot_mode_dm.py:152`, the round-1 defect). The dashboard is used only to OBSERVE
   that ack by exact-resume of the sender's canonical `Bot Chat` (Step 2).
3. Write each bot's Bot-Mode identity and materialise its canonical Bot Chat by
   running the plugin's onboard against THIS scenario's spec, over the loopback WS
   (standalone path, so it reaches the running gateway):
   ```bash
   ( source $TB/harness.live.env && cd $WT
     hermes onboard coworker tests/e2e-scenarios/A2A-F21/spec/coworker-types.yaml \
       --gateway-url "ws://127.0.0.1:9119/api/ws?token=$GATEWAY_TOKEN" \
       > $ART/scenario-AC-LOOP-F35-5/onboard.log 2>&1 ; echo onboard_exit=$? )
   ```
   This dispatches `profiles.configure` (ui_meta['hermes-bots'] per-key CAS) and the
   `session.list`/`session.create`/`session.title` Bot Chat lifecycle for each bot,
   and force-replaces each `SOUL.md` from the spec `identity:` (which is why bot-b's
   ack protocol lives in its spec identity, not only in its fixture SOUL.md).
   `$GATEWAY_TOKEN` is the loopback session token the testbed holds.
4. Mint a nonce for this run: `export NONCE="f35-$(date +%s)"` (exported so the subshell
   `python3 -c` reads it via `os.environ`).
5. **Regression oracle for the onboard-clobber fix.** Confirm each profile's
   POST-onboard `config.yaml` STILL carries `providers.coworkers-live` with the
   literal remote `base_url` — exactly what the clobber destroyed before the spine
   fix (PyYAML ships with Hermes; the image has no yaml CLI):
   ```bash
   ( source $TB/harness.live.env && python3 -c "
   import yaml, os
   home = os.environ['HERMES_HOME']
   for bot in ('a2a-f21-loopf35-a', 'a2a-f21-loopf35-b'):
       c = yaml.safe_load(open(home + '/profiles/' + bot + '/config.yaml'))
       prov = (c.get('providers') or {}).get('coworkers-live') or {}
       expected = {'base_url': 'https://inference-api.nvidia.com', 'api_mode': 'anthropic_messages', 'api_key': 'dummy-a2a-f21-not-a-real-key', 'default_model': 'aws/anthropic/bedrock-claude-opus-4-8'}
       assert all(prov.get(k) == v for k, v in expected.items()), (bot, 'providers.coworkers-live did NOT survive onboard: ' + repr(prov))
       assert 'key_env' not in prov, (bot, 'provider regressed to key_env: ' + repr(prov))
       model = c.get('model') or {}
       assert model.get('provider') == 'coworkers-live', (bot, 'model.provider lost: ' + repr(model))
       assert model.get('default') == 'aws/anthropic/bedrock-claude-opus-4-8', (bot, 'model.default lost: ' + repr(model))
       print(bot, 'post-onboard full providers.coworkers-live survived:', repr(prov))" )
   ```
6. **PREFLIGHT (no model call, before Step 1 — orchestrator P3 requirement): prove the
   exact-resume surface renders for BOTH profiles.** For `a2a-f21-loopf35-a` (Step 2's
   round-trip screenshot) AND `a2a-f21-loopf35-b` (Step 3's recipient-UI screenshot), read the
   canonical `Bot Chat` session id (`title='Bot Chat'`, `hidden=1`), seed a sentinel `assistant`
   row via stdlib `sqlite3` (NO model call), open it by exact-resume, confirm the sentinel
   RENDERS, then delete it and drop the PTY attach token so the later resumes spawn fresh. `SID`
   (bot-a) and `SID_B` (bot-b) are exported for Steps 2 and 3. If EITHER profile's sentinel does
   NOT render, STOP — do not spend the round; bounce to hermes-architect.
   ```bash
   SID=$( source $TB/harness.live.env && python3 -c "import sqlite3, os; d=sqlite3.connect(os.environ['HERMES_HOME']+'/profiles/a2a-f21-loopf35-a/state.db'); r=d.execute(\"select id from sessions where title='Bot Chat' and hidden=1 order by started_at desc limit 1\").fetchone(); assert r, 'no hidden Bot Chat session for a2a-f21-loopf35-a'; print(r[0])" ) || { echo "PREFLIGHT FAILED — no hidden Bot Chat session for a2a-f21-loopf35-a; STOP" >&2; exit 1; }
   export SID
   SENTINEL_ROWID=$( source $TB/harness.live.env && python3 -c "import sqlite3, os, time; d=sqlite3.connect(os.environ['HERMES_HOME']+'/profiles/a2a-f21-loopf35-a/state.db'); cur=d.execute(\"insert into messages (session_id, role, content, timestamp, active) values (?, 'assistant', ?, ?, 1)\", (os.environ['SID'], 'PREFLIGHT-'+os.environ['NONCE'], time.time())); d.commit(); print(cur.lastrowid)" ) || { echo "PREFLIGHT FAILED — could not seed sentinel row; STOP" >&2; exit 1; }
   export SENTINEL_ROWID
   agent-browser open "http://127.0.0.1:9119/chat?profile=a2a-f21-loopf35-a&resume=$SID"
   agent-browser wait --load networkidle
   if ! timeout 120 agent-browser wait --text "PREFLIGHT-$NONCE"; then
     agent-browser screenshot "$ART/scenario-AC-LOOP-F35-5/preflight-failed.png" --full || true
     echo "PREFLIGHT FAILED — exact-resume did not render the seeded sentinel; STOP, do not run Step 1, bounce to hermes-architect" >&2
     exit 1
   fi
   agent-browser screenshot $ART/scenario-AC-LOOP-F35-5/preflight.png --full
   ( source $TB/harness.live.env && python3 -c "import sqlite3, os; d=sqlite3.connect(os.environ['HERMES_HOME']+'/profiles/a2a-f21-loopf35-a/state.db'); cur=d.execute('delete from messages where id=?', (int(os.environ['SENTINEL_ROWID']),)); d.commit(); assert cur.rowcount == 1, ('sentinel delete removed %d rows' % cur.rowcount); print('deleted sentinel', os.environ['SENTINEL_ROWID'])" ) || { echo "PREFLIGHT FAILED — sentinel cleanup did not remove exactly one row; STOP, do not run Step 1" >&2; exit 1; }
   agent-browser eval "localStorage.removeItem('hermes.pty.token.chat')"
   agent-browser open about:blank
   SID_B=$( source $TB/harness.live.env && python3 -c "import sqlite3, os; d=sqlite3.connect(os.environ['HERMES_HOME']+'/profiles/a2a-f21-loopf35-b/state.db'); exact=d.execute(\"select id, hidden from sessions where title='Bot Chat'\").fetchall(); conts=d.execute(\"select id from sessions where title like 'Bot Chat #%'\").fetchall(); assert len(exact)==1 and exact[0][1]==1 and not conts, ('bot-b Bot Chat ambiguous/missing: exact=%r conts=%r' % (exact, conts)); print(exact[0][0])" ) || { echo "PREFLIGHT FAILED — bot-b Bot Chat missing or ambiguous (a 'Bot Chat #N' continuation would receive the delivery turn while Step 3/state inspect SID_B — bot_mode_dm.py:362 resolves -c 'Bot Chat' by title); STOP" >&2; exit 1; }
   export SID_B
   SENTINEL_ROWID_B=$( source $TB/harness.live.env && python3 -c "import sqlite3, os, time; d=sqlite3.connect(os.environ['HERMES_HOME']+'/profiles/a2a-f21-loopf35-b/state.db'); cur=d.execute(\"insert into messages (session_id, role, content, timestamp, active) values (?, 'assistant', ?, ?, 1)\", (os.environ['SID_B'], 'PREFLIGHT-'+os.environ['NONCE'], time.time())); d.commit(); print(cur.lastrowid)" ) || { echo "PREFLIGHT FAILED — could not seed bot-b sentinel row; STOP" >&2; exit 1; }
   export SENTINEL_ROWID_B
   agent-browser open "http://127.0.0.1:9119/chat?profile=a2a-f21-loopf35-b&resume=$SID_B"
   agent-browser wait --load networkidle
   if ! timeout 120 agent-browser wait --text "PREFLIGHT-$NONCE"; then
     agent-browser screenshot "$ART/scenario-AC-LOOP-F35-5/preflight-b-failed.png" --full || true
     echo "PREFLIGHT FAILED — bot-b exact-resume did not render the seeded sentinel; STOP, do not run Step 1, bounce to hermes-architect" >&2
     exit 1
   fi
   agent-browser screenshot $ART/scenario-AC-LOOP-F35-5/preflight-b.png --full
   ( source $TB/harness.live.env && python3 -c "import sqlite3, os; d=sqlite3.connect(os.environ['HERMES_HOME']+'/profiles/a2a-f21-loopf35-b/state.db'); cur=d.execute('delete from messages where id=?', (int(os.environ['SENTINEL_ROWID_B']),)); d.commit(); assert cur.rowcount == 1, ('bot-b sentinel delete removed %d rows' % cur.rowcount); print('deleted bot-b sentinel', os.environ['SENTINEL_ROWID_B'])" ) || { echo "PREFLIGHT FAILED — bot-b sentinel cleanup did not remove exactly one row; STOP, do not run Step 1" >&2; exit 1; }
   agent-browser eval "localStorage.removeItem('hermes.pty.token.chat')"
   agent-browser open about:blank
   ```
   BOTH sentinels render → both exact-resume surfaces are proven, deletes done, proceed to Step 1.
   EITHER sentinel does NOT render (`No sessions yet`/404) → STOP: do not run Step 1, do not spend
   the authorized round; bounce to hermes-architect with the `preflight-*-failed.png`, and the
   architect escalates to the orchestrator for a dashboard CORE-CHANGE or an operator ADR bar-change.
   `state.db`/CLI never substitute for this render proof.

## Steps

1. **Drive `a2a-f21-loopf35-a`'s sender turn in its canonical `Bot Chat` via an INTERACTIVE
   real-PTY `hermes chat`.** The session titled exactly `Bot Chat` is the only one where
   `message_agent` is injected (`tools/bot_mode_dm.py:152` gates the tool on that title), and
   an INTERACTIVE session (no `-Q`/`--oneshot`) is the only CLI drive whose own loop DRAINS the
   async completion queue (`cli.py:20912` idle / `:21146` post-turn) so bot-b's reply round-trips
   back in. Two paths are explicitly NOT used: the round-2 `-Q` one-shot answered and exited
   WITHOUT draining (`cli.py:22258-22259` → never `cli.run()`/`process_loop`; its finalize waits
   for delivery but never drains, `cli.py:1452`); the round-1 web composer opens a NEW auto-titled
   session where `message_agent` is absent (`Tool 'message_agent' does not exist`). The prompt is
   delivered by `-q`, which on a real TTY is SEEDED into the interactive session rather than run
   one-shot (`_should_seed_interactive`, `cli.py:5159-5180` — one-shot needs `-Q`/`--oneshot` or a
   non-TTY), and `--cli` pins the classic REPL so the drain loop runs even if config would select
   the TUI (`hermes_cli/_parser.py`). The stdlib-only PTY driver below opens a pty (child stdin+stdout
   a TTY → interactive), refuses to launch unless `$SID` (the exact `Bot Chat` the PREFLIGHT proved
   and Step 2 screenshots) is the sole session `-c "Bot Chat"` can resolve to, holds the session alive
   while continuously draining the pty, and STOPS when `F35-ACK:$NONCE` lands in that session's
   `state.db` (bound to `$SID`, `active=1`, `id>` a pre-send baseline — never a stale row), then sends
   `/exit` (best-effort graceful exit). It exits 0 when the ack round-tripped AND the child process
   reached exit 0 by any means — a submitted `/exit`, else EOF/Ctrl-D (Amendment 6 no longer requires
   a clean `/exit` specifically; only child rc 0).

```bash
( source $TB/harness.live.env
  export SID NONCE
  export DRIVE_PROFILE=a2a-f21-loopf35-a
  export DRIVE_PROMPT="Use message_agent to send a2a-f21-loopf35-b exactly: \"$NONCE please reply\". Then wait for and report bot-b's reply back to me."
  export DRIVE_LOG="$ART/scenario-AC-LOOP-F35-5/drive-a.log"
  export DRIVE_META="$ART/scenario-AC-LOOP-F35-5/drive-a.meta"
  export DRIVE_TIMEOUT=600
  python3 - <<'PYDRIVE'
import os, sys, pty, select, subprocess, time, sqlite3, signal, fcntl, termios, struct

home = os.environ["HERMES_HOME"]
profile = os.environ.get("DRIVE_PROFILE", "a2a-f21-loopf35-a")
sid = os.environ["SID"]
nonce = os.environ["NONCE"]
marker = "F35-ACK:" + nonce
prompt = os.environ["DRIVE_PROMPT"]
logpath = os.environ["DRIVE_LOG"]
deadline_s = float(os.environ.get("DRIVE_TIMEOUT", "600"))
db_path = home + "/profiles/" + profile + "/state.db"

# A bare-marker match would false-pass, so the sender prompt must never carry the ack text.
if "F35-ACK:" in prompt or marker in prompt:
    sys.stderr.write("DRIVE ABORT: sender prompt contains the ack prefix/marker\n")
    sys.exit(2)


def ro_conn():
    c = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=5)
    c.execute("PRAGMA busy_timeout=4000")
    return c


# -c "Bot Chat" resolves the title independently of $SID and may prefer a "Bot Chat #N"
# continuation, so refuse to launch unless $SID is the sole session the title can select.
c = ro_conn()
try:
    exact = c.execute("select id, hidden from sessions where title='Bot Chat'").fetchall()
    conts = c.execute("select id from sessions where title like 'Bot Chat #%'").fetchall()
finally:
    c.close()
if exact != [(sid, 1)] or conts:
    sys.stderr.write("DRIVE ABORT ambiguous -c 'Bot Chat': sid=%r exact=%r continuations=%r\n"
                     % (sid, exact, conts))
    sys.exit(2)

# Only rows newer than this baseline count as the ack (excludes any already-persisted match).
c = ro_conn()
try:
    baseline = c.execute("select coalesce(max(id),0) from messages where session_id=?", (sid,)).fetchone()[0]
finally:
    c.close()

logf = None


def marker_in_db():
    try:
        cc = ro_conn()
    except sqlite3.Error:
        return False
    try:
        return cc.execute(
            "select 1 from messages where session_id=? and active=1 and id>? and instr(content, ?)>0 limit 1",
            (sid, baseline, marker)).fetchone() is not None
    except sqlite3.Error:
        return False
    finally:
        cc.close()


def drain_master(fd):
    try:
        r, _, _ = select.select([fd], [], [], 0.2)
    except (OSError, ValueError):
        return
    if fd in r:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            return
        if chunk and logf is not None:
            try:
                logf.write(chunk)
            except OSError:
                pass


argv = ["hermes", "-p", profile, "chat", "--cli", "-c", "Bot Chat", "-q", prompt]

# One absolute monotonic deadline, computed BEFORE the child starts, bounds startup, the marker
# wait, AND every cleanup phase; the child is always reaped inside it, so the drive never
# outlives the step ceiling nor leaves a live model process behind.
hard = time.monotonic() + deadline_s
reserve = min(60.0, max(8.0, deadline_s * 0.12))
reserve = min(reserve, deadline_s * 0.5)
poll_until = hard - reserve
t_exit = hard - reserve * 0.5     # graceful /exit
t_ctrld = hard - reserve * 0.25   # Ctrl-D
t_term = hard - reserve * 0.10    # SIGTERM reap; SIGKILL reap is bounded by `hard`

master = slave = None
proc = None
found = False
clean_exit = False
try:
    logf = open(logpath, "wb", buffering=0)
    master, slave = pty.openpty()
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    except Exception:
        pass
    env = dict(os.environ, TERM="xterm-256color", COLUMNS="120", LINES="40")
    proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave,
                            start_new_session=True, env=env, close_fds=True)
    os.close(slave)
    slave = None
    last_poll = 0.0
    while time.monotonic() < poll_until:
        drain_master(master)
        if not found and time.monotonic() - last_poll >= 2.0:
            last_poll = time.monotonic()
            found = marker_in_db()
        if found:
            try:
                os.write(master, b"/exit\r")  # routes through the command handler, never the model
            except OSError:
                pass
            while proc.poll() is None and time.monotonic() < t_exit:
                drain_master(master)
            clean_exit = proc.poll() == 0
            break
        if proc.poll() is not None:
            break
finally:
    # /exit is best-effort; EOF is accepted when the child exits with status 0.
    # SIGTERM/SIGKILL are bounded last-resort reaps; the final child rc controls PASS/FAIL.
    if proc is not None and proc.poll() is None and master is not None:
        try:
            os.write(master, b"\x04")
        except OSError:
            pass
        while proc.poll() is None and time.monotonic() < t_ctrld:
            drain_master(master)
    if proc is not None and proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:
            pass
        while proc.poll() is None and time.monotonic() < t_term:
            time.sleep(0.05)
    if proc is not None and proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass
        while proc.poll() is None and time.monotonic() < hard:
            time.sleep(0.05)
    for fd in (master, slave):
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    if logf is not None:
        try:
            logf.close()
        except OSError:
            pass

child_rc = proc.returncode if proc is not None else None
metapath = os.environ.get("DRIVE_META")
if metapath:
    try:
        with open(metapath, "w") as mf:
            mf.write("pid=%s\nexit=%s\nmarker_in_statedb=%d\n"
                     % (proc.pid if proc is not None else -1, child_rc, int(found)))
    except OSError:
        pass
sys.stderr.write("DRIVE marker=%d clean_exit=%d child_rc=%s\n" % (int(found), int(clean_exit), child_rc))
sys.exit(0 if (found and child_rc == 0) else 1)
PYDRIVE
  rc=$?; echo "sender_drive_exit=$rc"; exit "$rc" )
```

   → expect `sender_drive_exit=0`: `message_agent` delivered to `a2a-f21-loopf35-b` inside the one
   gateway (never a2a, no second `serve`); bot-b ran a turn and replied `F35-ACK:$NONCE`; the
   interactive drain surfaced that reply into `a2a-f21-loopf35-a`'s `Bot Chat` `state.db` and the child
   reached exit 0 (by any means — a submitted `/exit`, else EOF/Ctrl-D; Amendment 6). A non-zero
   `sender_drive_exit` means the ack never round-tripped or the child process did not exit with status
   0 — STOP, do not run Step 2, and record per §Env fallback (exit 2 = a pre-launch guard tripped:
   ambiguous `Bot Chat` title or a prompt that leaked the marker).
2. **Observe the round-tripped ack in the web UI by exact-resume** — run this ONLY after Step 1
   reported `sender_drive_exit=0`. Reuse the exact `$SID` the PREFLIGHT (Setup step 6) exported and
   the driver bound to (the session titled exactly `Bot Chat`, `hidden=1`); do NOT re-derive it, so
   the screenshot, the driver's stop-poll, and the Evidence all assert the same session. Open it BY ID
   in the dashboard — the by-id resume route renders the hidden canonical session even though the
   session *list* excludes it (`web_routers/sessions.py:596-616,642-693` apply no hidden filter;
   `ChatPage.tsx:350-426,1164-1186` → `/api/pty`):
   ```bash
   test -n "$SID" || { echo "SID unset — PREFLIGHT (Setup step 6) must run before Step 2"; exit 1; }
   agent-browser open "http://127.0.0.1:9119/chat?profile=a2a-f21-loopf35-a&resume=$SID"
   agent-browser wait --load networkidle
   timeout 600 agent-browser wait --text "F35-ACK:$NONCE" || { echo "Step 2: F35-ACK:$NONCE did not render in bot-a Bot Chat within 600s"; exit 1; }   # the round-tripped ack, absent from the sender's prompt
   agent-browser screenshot $ART/scenario-AC-LOOP-F35-5/step-2.png --full
   ```
   → expect: `a2a-f21-loopf35-a`'s canonical `Bot Chat` transcript renders at
   `/chat?profile=a2a-f21-loopf35-a&resume=$SID` with `F35-ACK:$NONCE` visible.
3. **Observe the RECIPIENT UI by exact-resume** (Amendment 5, PASS item 2) — reuse the exact
   `$SID_B` the PREFLIGHT (Setup step 6) exported for `a2a-f21-loopf35-b`'s canonical `Bot Chat`
   (`title='Bot Chat'`, `hidden=1`); do NOT re-derive it. Open it BY ID and screenshot the live ack
   bot-b GENERATED in its OWN Bot Chat — distinct from Step 2 (which shows the ack round-tripping
   into bot-a), so the two screenshots together prove generation AND round-trip:
   ```bash
   test -n "$SID_B" || { echo "SID_B unset — PREFLIGHT (Setup step 6) must run before Step 3"; exit 1; }
   agent-browser open "http://127.0.0.1:9119/chat?profile=a2a-f21-loopf35-b&resume=$SID_B"
   agent-browser wait --load networkidle
   timeout 600 agent-browser wait --text "F35-ACK:$NONCE" || { echo "Step 3: F35-ACK:$NONCE did not render in bot-b Bot Chat within 600s"; exit 1; }   # the ack bot-b generated in its own Bot Chat
   agent-browser screenshot $ART/scenario-AC-LOOP-F35-5/step-3.png --full
   ```
   → expect: `a2a-f21-loopf35-b`'s canonical `Bot Chat` transcript renders at
   `/chat?profile=a2a-f21-loopf35-b&resume=$SID_B` with `F35-ACK:$NONCE` visible.

## Pass

The criterion holds against the operator-tightened THREE-ITEM hard-evidence bar (Amendment 5,
round 3/3 — NEVER a `state.db`-only pass) when `message_agent` carries `a2a-f21-loopf35-a` →
`a2a-f21-loopf35-b` inside the one gateway (never a2a, no second `serve`), `a2a-f21-loopf35-b`
generates `F35-ACK:$NONCE` (the distinct marker proving it replied), and ALL THREE hold, each
carrying the SAME `$NONCE`:

- **(1) sender-side completion evidence (Step 1)** — the interactive drive process has a captured
  pid, exits 0 (`drive-a.meta` shows `exit=0` — the child process exit code, reached by any means
  incl. EOF; **Amendment 6 waived the earlier "on its clean `/exit`" conjunct**), and its PTY transcript
  `drive-a.log` contains the exact `F35-ACK:$NONCE` (the ack surfaced in the sender's own live turn).
- **(2) retained recipient UI (Step 3)** — a `step-3.png` screenshot of `a2a-f21-loopf35-b`'s
  canonical `Bot Chat` (exact-resume `/chat?profile=a2a-f21-loopf35-b&resume=$SID_B`) showing the
  live `F35-ACK:$NONCE` bot-b generated in its OWN Bot Chat.
- **(3) round-trip UI (Step 2)** — a `step-2.png` screenshot of `a2a-f21-loopf35-a`'s canonical
  `Bot Chat` (exact-resume `/chat?profile=a2a-f21-loopf35-a&resume=$SID`) showing `F35-ACK:$NONCE`
  round-tripped back into the sender.

Corroborating only (NEVER substituting for the three above): both profiles' `state.db` ack rows
(bot-b's `assistant` row + the round-tripped row in bot-a's `Bot Chat`) and the §Setup step-5
regression oracle showing `providers.coworkers-live` survived `hermes onboard` for both bots.

The sender turn is driven by an INTERACTIVE real-PTY `hermes chat --cli` (no `-Q`) whose own loop
DRAINS the async completion queue (Step 1, where `message_agent` injects); the `-Q` one-shot is NOT
the drive (it never drains — the round-2 defect) and the web composer is NOT used (a non-`Bot Chat`
session — the round-1 defect). Neither tester nor builder may drop an item, substitute a different
surface, or pass on `state.db` alone; the bar is judged as written. **This is the LAST counted round
the operator will grant (round 3/3); there is no round 4.**

**Amendment 6 (operator A-narrowed, 2026-09-17):** round 3 @83be5feb PROVED the round-trip (step-2/step-3,
same nonce), so item (1)'s "clean `/exit`" conjunct is WAIVED — item (1) now = pid + exit-0 (child rc,
`drive-a.meta`) + ACK-in-`drive-a.log`. The round-trip and both UI items STAY; nothing else is dropped.
The tester RE-ATTESTS the existing @83be5feb run against this bar — no new live round.

## Evidence

- **(1) sender-side completion evidence** — `drive-a.log` (the Step-1 PTY transcript) and its
  `drive-a.meta` sidecar under `$ART/scenario-AC-LOOP-F35-5/`: `drive-a.meta` MUST record `exit=0`
  (the child process exit code, reached by any means incl. EOF — **Amendment 6**, not the driver's
  `clean_exit` self-check) and `drive-a.log` MUST contain the exact `F35-ACK:$NONCE` (the ack surfaced
  in the sender's own live turn). Assert both:
  ```bash
  D=$ART/scenario-AC-LOOP-F35-5
  grep -Eq '^pid=[1-9][0-9]*$' "$D/drive-a.meta" || { echo "drive-a.meta did not record a captured pid:"; cat "$D/drive-a.meta"; exit 1; }
  grep -qx 'exit=0' "$D/drive-a.meta" || { echo "drive-a.meta did not record exit=0:"; cat "$D/drive-a.meta"; exit 1; }
  grep -qF "F35-ACK:$NONCE" "$D/drive-a.log" || { echo "drive-a.log missing F35-ACK:$NONCE"; exit 1; }
  echo "sender-side OK:"; cat "$D/drive-a.meta"
  ```
- **(2) retained recipient UI — not substitutable** — `step-3.png` under `$ART/scenario-AC-LOOP-F35-5/`,
  an agent-browser screenshot of `a2a-f21-loopf35-b`'s canonical `Bot Chat` (`title='Bot Chat'`,
  `hidden=1`; the id in `/chat?profile=a2a-f21-loopf35-b&resume=$SID_B`) showing the live
  `F35-ACK:$NONCE` bot-b generated in its own Bot Chat.
- **(3) round-trip UI — not substitutable** — `step-2.png` under `$ART/scenario-AC-LOOP-F35-5/`, an
  agent-browser screenshot of `a2a-f21-loopf35-a`'s canonical `Bot Chat` (`title='Bot Chat'`,
  `hidden=1`; the id in `/chat?profile=a2a-f21-loopf35-a&resume=$SID`) showing `F35-ACK:$NONCE`
  round-tripped back.
- **Corroborating (NEVER substituting for items 1-3):** the §Setup step-5 post-onboard oracle (both
  bots' full `providers.coworkers-live` survived onboard), plus a `python3 -c` read (stdlib sqlite3 —
  the image has no `sqlite3` CLI) of BOTH profiles' `state.db`, each bound to its exported session id:
  (a) bot-b's Bot Chat (`$SID_B`) holds the generated ack, and (b) bot-a's Bot Chat (`$SID`) holds the
  round-tripped ack:
  ```bash
  python3 -c "
  import sqlite3, os
  home = os.environ['HERMES_HOME']; sid = os.environ['SID']; sid_b = os.environ['SID_B']
  db_b = sqlite3.connect(home + '/profiles/a2a-f21-loopf35-b/state.db')
  meta_b = db_b.execute(\"select id, title, hidden from sessions where id=? and title='Bot Chat' and hidden=1\", (sid_b,)).fetchone()
  assert meta_b, 'exported SID_B is not bot-b hidden Bot Chat: ' + sid_b
  rows_b = [r for r in db_b.execute(\"select role, substr(content,1,160) from messages where session_id=? and role='assistant' and instr(content, 'F35-ACK:$NONCE') > 0 order by timestamp desc limit 5\", (sid_b,))]
  assert rows_b, 'no bot-b Bot Chat F35-ACK:$NONCE row'
  db_a = sqlite3.connect(home + '/profiles/a2a-f21-loopf35-a/state.db')
  meta_a = db_a.execute(\"select id, title, hidden from sessions where id=? and title='Bot Chat' and hidden=1\", (sid,)).fetchone()
  assert meta_a, 'exported SID is not bot-a hidden Bot Chat: ' + sid
  rows_a = [r for r in db_a.execute(\"select role, substr(content,1,160) from messages where session_id=? and instr(content, 'F35-ACK:$NONCE') > 0 order by timestamp desc limit 5\", (sid,))]
  assert rows_a, 'ack did not round-trip into bot-a Bot Chat ' + sid
  print('bot-b Bot Chat (SID_B) meta:', meta_b); print('bot-b generated:', rows_b)
  print('bot-a Bot Chat (SID) meta:', meta_a); print('bot-a round-trip:', rows_a)"
  ```
- **Correlation (the three-item bar):** the SAME `$NONCE` value appears in `drive-a.log`, `step-3.png`,
  `step-2.png`, AND both profiles' `state.db` rows above — one nonce tying all three required artifacts
  and the corroborating state together. Separately (not a nonce carrier), the §Setup step-5 oracle proves
  the complete `providers.coworkers-live` block survived `hermes onboard`. `state.db`/CLI/REST corroborate
  only; they never substitute for items (1)-(3).
- The round did not run away — summed across BOTH bots' sessions (sender and recipient
  each make model calls), the model-call count is non-zero and both it and cost stay
  under the live caps (`LIVE_MODEL_CALLS_MAX=40` / `LIVE_BUDGET_USD=5`):
  ```bash
  python3 -c "
  import sqlite3, os
  home = os.environ['HERMES_HOME']
  calls = 0; cost = 0.0
  for bot in ('a2a-f21-loopf35-a', 'a2a-f21-loopf35-b'):
      db = home + '/profiles/' + bot + '/state.db'
      if not os.path.exists(db):
          continue
      c, u = sqlite3.connect(db).execute('select coalesce(sum(api_call_count),0), coalesce(sum(case when actual_cost_usd > 0 then actual_cost_usd else estimated_cost_usd end),0) from session_model_usage').fetchone()
      calls += c or 0; cost += u or 0
  cost = round(cost, 4)
  print('both bots: calls=', calls, 'cost=', cost)
  assert 0 < calls < 40 and cost < 5, (calls, cost)"
  ```

## Env fallback (does not count against the round caps, per "FAIL (env) and ESCALATE never count")

If `message_agent` delivery still fails for an environmental reason distinct from the
onboard clobber this fix addresses (for example the bare-`hermes`-PATH delivery issue
of `tools/bot_mode_dm.py:364` recurs), the tester records `FAIL(env)`/`ESCALATE` with
the §Setup step 5 post-onboard config read attached as evidence of whether the
provider block survived — separating the fix under test (the block survives onboard)
from any residual harness gap. The tester must not rewrite the spec or fixtures to
force a pass.
