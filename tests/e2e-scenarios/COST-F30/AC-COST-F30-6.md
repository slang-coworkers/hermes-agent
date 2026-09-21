---
ac: AC-COST-F30-6
kind: ui
model: stub
fixtures:
  - fixtures/cost-f30-operator
timeout_s: 300
---

# AC-COST-F30-6 — the web dashboard renders the escalation card and resolves Continue exactly once

The merged nv-cost-cap plugin's dashboard half renders a shell-wide (`header-banner`)
cost-escalation card whenever the active profile has a pending episode. Resolving Continue
there applies exactly one increment through the plugin's auth-gated backend
(`/api/plugins/nv-cost-cap/…/resolve?profile=`); resolving the same episode again is a
reported no-op.

## Setup
- Install the `fixtures:` list (the tester does this before `## Setup`). Do NOT re-install
  it here.
- Start `hermes dashboard` for `cost-f30-operator` **in gated/OAuth mode** (so
  `request.state.session` carries `provider`/`org_id`/`user_id`), authenticated as the OAuth
  operator whose derived principal is `dashboard:oauth:org-1:op-1` (already in the fixture's
  `operators`). Point the model at the hermetic **stub** (the fixture's live provider block
  is for AC-8).
- Load the dashboard under agent-browser. Because the SPA opens the DEFAULT profile's view
  regardless of `-p`, first **select `cost-f30-operator` in the dashboard profile combobox**
  → expect the `cost-f30-operator »` context before any assertion.
- Seed one Tier-2 crossing on the next turn (the fixture's `default_ceiling_usd: 0.02` is low
  enough that any priced stub turn crosses; or seed a Tier-2 `cap_state` + an `escalation`/`breach`
  episode directly in `$HERMES_HOME/profiles/cost-f30-operator/plugin-data/nv-cost-cap/data.db`).

## Steps
1. Trigger one stub turn that crosses Tier-2 (or apply the seed) and reload the dashboard →
   expect: the plugin's escalation card appears in the shell-wide slot, naming the session,
   spend, and ceiling, with **Continue**, **Stop**, and a set-ceiling input + button (the card
   is fed by `GET /api/plugins/nv-cost-cap/escalations?profile=cost-f30-operator`).
2. Click **Continue** → expect: the `POST /api/plugins/nv-cost-cap/escalations/<id>/resolve?profile=cost-f30-operator`
   returns granted and the card clears (the pending list is now empty).
3. Reload and, if the card still shows for the same episode, click Continue again (or re-issue
   the resolve for that episode) → expect: a reported no-op ("already-resolved"); the spend/ceiling
   are unchanged from step 2.

## Pass
The card renders in the panel (1), the first Continue applies exactly one increment (2), and
the second resolution of the same episode is a reported no-op with no further mutation (3).

## Evidence
- `step-1.png` (card visible), `step-2.png` (card cleared after Continue), `step-3.png`
  (no-op on repeat).
- A `python3 -c` read over `$HERMES_HOME/profiles/cost-f30-operator/plugin-data/nv-cost-cap/data.db`
  (stdlib `sqlite3`) asserting exactly one `applied` row in `resolutions` for the episode and
  that `cap_state.window_start_total` advanced by `escalation_increment_usd` exactly once:
  ```
  python3 -c "import sqlite3;c=sqlite3.connect('$DB');print('applied=',c.execute(\"select count(*) from resolutions where status='applied'\").fetchone()[0]);print('window=',c.execute('select window_start_total,blocked from cap_state').fetchall())"
  ```
