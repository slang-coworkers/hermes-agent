---
ac: AC-OBS-F48-3
kind: ui
model: stub
fixtures:
  - fixtures/gov-f25-orchestrator
timeout_s: 300
---

# AC-OBS-F48-3 — the dashboard extension page renders the outcomes funnel, win-rate and cost-per-merge

The drop-in nv-artifact dashboard page (**Artifacts** tab, route `/nv-artifact`)
renders the outcome funnel, win-rate and cost-per-merge for the operator,
computed from the SAME fleet ledger the CLI verbs read. `model: stub` — the page
is read-only and drives no model; the ledger is seeded directly. Reads are
orchestrator-only, so the dashboard is served as the `gov-f25-orchestrator`
profile (its `orchestrator_profile` matches its own name).

## Setup

The tester installs the `fixtures:` profile (`gov-f25-orchestrator`) before this
section. Everything below is post-install.

1. Start the dashboard **as the `gov-f25-orchestrator` profile** so the page's
   server-side read is authorized (`-p` is pre-parsed and precedes the
   subcommand), per `/hermes-ui-driver` §1a:
   ```bash
   ( source $TB/harness.env && cd $WT
     export HERMES_WEB_DIST=/workspace/agent/build/web_dist-<sha7>
     hermes -p gov-f25-orchestrator dashboard --host 127.0.0.1 --port 9119 \
       --no-open --isolated --skip-build > $ART/dashboard.log 2>&1 &
     echo $! > $TB/dashboard.pid
     timeout 300 bash -c "until grep -qE 'HERMES_DASHBOARD_READY port=|Hermes Web UI' $ART/dashboard.log; do sleep 2; done" )
   ```
2. Seed the fleet outcomes ledger with a known set — 5 artifacts: 3 `merged`
   (costs 1.00 / 2.00 / 3.00), 1 `closed` (0.50), 1 `abandoned` (0.25) — into the
   file the page reads (`ledger_profile: gov-f25-orchestrator` pins it to this
   profile's home), then it is picked up on the next page load. The coworker
   image has no `sqlite3` CLI, so use the stdlib module:
   ```bash
   ( source $TB/harness.env
     python3 - <<'PY'
   import os, sqlite3, pathlib
   db = pathlib.Path(os.environ["HERMES_HOME"]) / "profiles" / "gov-f25-orchestrator" / "plugin-data" / "nv-artifact" / "data.db"
   db.parent.mkdir(parents=True, exist_ok=True)
   c = sqlite3.connect(str(db))
   c.execute("CREATE TABLE IF NOT EXISTS outcomes (artifact TEXT NOT NULL, terminal_outcome TEXT, cost_usd REAL NOT NULL DEFAULT 0, profile TEXT, UNIQUE(artifact))")
   for art, outcome, cost in [("o/r#1","merged",1.00),("o/r#2","merged",2.00),("o/r#3","merged",3.00),("o/r#4","closed",0.50),("o/r#5","abandoned",0.25)]:
       c.execute("INSERT OR REPLACE INTO outcomes (artifact, terminal_outcome, cost_usd, profile) VALUES (?,?,?,?)", (art, outcome, cost, "gov-f25-orchestrator"))
   c.commit(); c.close()
   print("seeded", db)
   PY
   )
   ```

## Steps

1. Open the dashboard nv-artifact page by route (the SPA is a `BrowserRouter`)
   and wait for it to render:
   ```bash
   agent-browser open "http://127.0.0.1:9119/nv-artifact"
   agent-browser wait --load networkidle
   timeout 300 agent-browser wait --text "Fleet artifact outcomes"
   agent-browser snapshot -i
   agent-browser screenshot $ART/scenario-AC-OBS-F48-3/step-1.png --full
   ```
   → expect: the **Artifacts** page loads without a `[role="alert"]`; the heading
   "Fleet artifact outcomes" and the three panels (Funnel, Win rate, Cost per
   merge) are present.
2. Read the funnel panel:
   ```bash
   agent-browser get text @e<funnel-panel>   # re-snapshot to resolve the ref
   agent-browser screenshot $ART/scenario-AC-OBS-F48-3/step-2.png --full
   ```
   → expect: funnel counts `merged 3`, `closed 1`, `abandoned 1`.
3. Read the win-rate and cost-per-merge panels:
   ```bash
   timeout 300 agent-browser wait --text "60%"
   timeout 300 agent-browser wait --text "2.00"
   agent-browser screenshot $ART/scenario-AC-OBS-F48-3/step-3.png --full
   ```
   → expect: win-rate shows `60%` (3 merged / 5 total) and cost-per-merge shows
   `2.00` ((1.00+2.00+3.00)/3 merged).

## Pass

The rendered page shows the funnel counts (merged 3, closed 1, abandoned 1),
win-rate `60%` and cost-per-merge `2.00`, all computed from the seeded ledger.

## Evidence

- `step-1.png` (page loaded), `step-2.png` (funnel), `step-3.png` (win-rate + cost-per-merge).
- The seed script above, echoed for reproducibility.
- Confirm the page read the same ledger the seed wrote (stdlib `sqlite3`, no CLI):
  ```bash
  ( source $TB/harness.env
    python3 -c "import os,sqlite3,pathlib; db=pathlib.Path(os.environ['HERMES_HOME'])/'profiles'/'gov-f25-orchestrator'/'plugin-data'/'nv-artifact'/'data.db'; c=sqlite3.connect(str(db)); print(sorted(c.execute('SELECT terminal_outcome, cost_usd FROM outcomes')))" )
  # expect: [('abandoned',0.25),('closed',0.5),('merged',1.0),('merged',2.0),('merged',3.0)]
  ```
