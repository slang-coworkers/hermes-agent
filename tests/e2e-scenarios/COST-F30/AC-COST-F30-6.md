---
ac: AC-COST-F30-6
kind: ui
model: stub
fixtures:
  - fixtures/cost-f30-operator
timeout_s: 300
---

# AC-COST-F30-6 — the web dashboard renders the escalation card; Continue/Stop/set-ceiling resolve, and a left ESTOP belt is surfaced honestly

The merged nv-cost-cap plugin's dashboard half renders a shell-wide (`header-banner`)
cost-escalation card whenever the active profile has a pending episode, with **Continue**,
**Stop**, and a set-exact-ceiling input. Resolving posts to the plugin's auth-gated backend
(`/api/plugins/nv-cost-cap/…/resolve?profile=`). Under the DEFAULT `estop_on_breach=true` a
Tier-2 crossing engages the profile ESTOP belt, which the plugin NEVER unlinks: a granted
Continue clears the per-session money block (`blocked=0`, one increment) but the belt is LEFT,
so the card surfaces the verbatim manual-resume notice and the session does NOT auto-resume.

## Setup
- Install the `fixtures:` list (the tester does this before `## Setup`). Do NOT re-install it here.
- Start `hermes dashboard` for `cost-f30-operator` **in loopback mode** (the mode the testbed
  supports — no OAuth IDP in-container). Loopback carries no per-user session, so
  `principal_from_request` returns None; the fixture's `loopback_operator: dashboard:oauth:org-1:op-1`
  (already set) opts the single trusted local token in as that panel operator, which is exactly one
  of the fixture's `operators`. Point the model at the hermetic **stub** (the fixture's live provider
  block is for AC-8).
- Load the dashboard under agent-browser. Because the SPA opens the DEFAULT profile's view
  regardless of `-p`, first **select `cost-f30-operator` in the dashboard profile combobox** →
  expect the `cost-f30-operator »` context before any assertion.
- The fixture keeps the DEFAULT `estop_on_breach=true`, so a Tier-2 crossing engages the profile
  ESTOP sentinel at `$HERMES_HOME/profiles/cost-f30-operator/ESTOP`. Seed each crossing through the
  plugin's real boundary path (a low `default_ceiling_usd: 0.02` makes any priced stub turn cross)
  so the breach both records the episode AND engages the belt; confirm the sentinel exists before
  step 2 (`python3 -c "from agent import estop, ...; print(estop.sentinel_path().exists())"` under
  the profile home) — a scenario that clears a block with no belt engaged would false-green the
  belt-left assertion.

## Steps (all three decision controls resolvable on the web panel, plus the left-belt surface)
1. Trigger one stub turn that crosses Tier-2 (or apply the seed) and reload → expect: the escalation
   card appears in the shell-wide slot, naming the session, spend, and ceiling, with **Continue**,
   **Stop**, and a set-ceiling input + button (fed by
   `GET /api/plugins/nv-cost-cap/escalations?profile=cost-f30-operator`).
2. Click **Continue** → expect: `POST …/resolve?profile=cost-f30-operator` returns granted with
   `resumes=false`, `estop_disposition="left"`, `manual_resume_required=true`; the money block clears
   (`blocked=0`, exactly one increment) but — because the crossing engaged the belt, which the plugin
   never lifts — the card re-renders the verbatim manual-resume notice ("Continue applied — the
   per-session cost block is cleared, but the profile ESTOP belt remains engaged (gates
   cron/kanban/new inbounds); resume via `hermes resume` or the UA-28 upstream lock") and the ESTOP
   sentinel is STILL present on disk (the session does NOT auto-resume).
3. Re-issue the resolve for the same episode → expect: a reported no-op ("already-resolved"); the
   spend/ceiling are unchanged from step 2.
4. On a FRESH Tier-2 crossing, click **Stop** → expect: `POST …/resolve` returns granted `stop`, the
   session STAYS blocked (`blocked=1`, NOT resumed), and the sentinel remains.
5. On a FRESH Tier-2 crossing, type `12.50` into the set-ceiling input and submit → expect:
   `POST …/resolve` returns granted `ceiling`, the profile config's `profile_ceiling_usd` is exactly
   `12.50`, and because `12.50` > the session's spend the Tier-2 MONEY block clears (`blocked=0`) —
   but with the belt engaged the result carries `resumes=false` + `manual_resume_required=true` and
   the session does not auto-resume (the sentinel is LEFT).
6. On an IMMORTAL (`daily`) crossing, the card exposes ONLY Continue — NO Stop AND NO set-ceiling
   control (a set-ceiling attempt is refused `immortal-continue-only`).
7. Manual-resume affordance: the left-belt card surfaces the notice and does NOT claim the session
   resumed; the fleet is actually resumed only by an operator `hermes resume` (or the UA-28 upstream
   lock), never by the card.

## Pass
The card renders (1); Continue clears the money block with exactly one increment, returns
`resumes=false`/`manual_resume_required=true` + the verbatim notice while LEAVING the sentinel, and
the repeat is a no-op (2–3); Stop blocks the session with no resume (4); set-ceiling writes the exact
value and clears the money block above spend while LEAVING the belt (5); the immortal card offers
ONLY Continue (6); the card never claims resumption while the belt stands (7).

## Evidence
- `step-1.png`…`step-7.png` (card visible; Continue outcome note with the verbatim manual-resume
  text; no-op on repeat; Stop keeps it blocked; ceiling accepted; immortal Continue-only).
- `python3 -c` reads over `$HERMES_HOME/profiles/cost-f30-operator/plugin-data/nv-cost-cap/data.db`
  (stdlib `sqlite3`), the profile `config.yaml`, and the profile ESTOP sentinel path asserting: one
  `applied` `continue` row with `window_start_total` advanced once + `blocked=0` (2); the ESTOP
  sentinel STILL present at `$HERMES_HOME/profiles/cost-f30-operator/ESTOP` after the Continue (belt
  left, 2/7); a `stop` row with `blocked=1` (4); a `ceiling` `applied` row with
  `profile_ceiling_usd==12.50` and `blocked=0` (5):
  ```
  python3 -c "import sqlite3,os; DB=os.environ['HERMES_HOME']+'/profiles/cost-f30-operator/plugin-data/nv-cost-cap/data.db'; c=sqlite3.connect(DB); print('applied=', c.execute(\"select decision,count(*) from resolutions where status='applied' group by decision\").fetchall()); print('state=', c.execute('select window_start_total,blocked from cap_state').fetchall())"
  ```
