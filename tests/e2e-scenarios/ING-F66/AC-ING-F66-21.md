---
ac: AC-ING-F66-21
kind: ui
model: stub
fixtures:
  - fixtures/ing-f66-ui-owner
timeout_s: 300
---

# AC-ING-F66-21 — the delivered CI failure shows in the owner's existing session (ui)

The operator sees a forwarded `check_run` failure for an owned PR in the web
dashboard. In the cross-profile **Sessions** view, the owner bot's pre-existing
session carries the delivery text, and the owner's session count does not change
(derived: the behaviour is user-visible).

The forward and the edge are not under test here. Setup POSTs one envelope-v1
body straight to the loopback ingress route, as the forward would.

The fixture files have these roles:

- `fixtures/ing-f66-ui-owner/`: the owner bot `ing-f66-ui-owner`, installed by the
  tester from `fixtures:`.
- `fixtures/ing-f66-ui-default/`: the DEFAULT multiplexer's ingress overlay. It is
  merged into the testbed home in Setup, not installed: the DEFAULT home can never
  be a `hermes profile install` target, because `plan_install` refuses the name
  `default`.

All state.db reads use the stdlib `python3 -c "import sqlite3 …"` form; the image
has no `sqlite3` CLI.

## Setup (after the fixtures are installed)

Every command runs inside `( source $TB/harness.env && cd $WT && … )`. `SCN` is
`$WT/tests/e2e-scenarios/ING-F66`, and `ART` is `$ART/scenario-AC-ING-F66-21`.

1. **The plugin in both homes.** Copy `$WT/plugins/nv-ingress` to
   `$HERMES_HOME/plugins/nv-ingress` AND to
   `$HERMES_HOME/profiles/ing-f66-ui-owner/plugins/nv-ingress`. Each profile
   discovers user plugins under its own home only
   (release `hermes_cli/plugins.py:4571`). Merge the stub provider block
   (testbed §3a) into `$HERMES_HOME/profiles/ing-f66-ui-owner/config.yaml`, keeping
   its `plugins.enabled: [nv-ingress]`.
2. **The DEFAULT overlay.**
   - Pick a free loopback port: `WP=$(python3 -c "import socket;s=socket.socket();s.bind(('127.0.0.1',0));print(s.getsockname()[1])")`.
   - Deep-merge `$SCN/fixtures/ing-f66-ui-default/config.yaml` into
     `$HERMES_HOME/config.yaml` with one `python3 -c` (`yaml.safe_load` both, merge
     the mappings, keep the stub model block, set
     `platforms.webhook.extra.port` = `$WP`, `yaml.safe_dump` back).
   - Copy `$SCN/fixtures/ing-f66-ui-default/scripts/ingress_stage.py` to
     `$HERMES_HOME/scripts/`.
3. **The owner session.**
   - Seed one session for the owner with a seed marker that differs from every
     token asserted below:
     `hermes -p ing-f66-ui-owner -z "ING-F66 seed turn: hold for CI results"` (stub reply).
   - Record its id:
     `S=$(python3 -c "import sqlite3,os;d=sqlite3.connect(os.environ['HERMES_HOME']+'/profiles/ing-f66-ui-owner/state.db');print(d.execute('select id from sessions order by started_at desc limit 1').fetchone()[0])")`.
   - Record the session count as `N0` (`select count(*) from sessions`). Expect `N0 = 1`.
4. **The claim.** `hermes -p ing-f66-ui-owner ingress claim o/ing-f66-ui 7 --session "$S" --json`.
   Expect `"status": "claimed"`. The row lands in the DEFAULT home's fleet ledger.
5. **The gateway and the dashboard.**
   - Start the gateway that runs the webhook adapter:
     `hermes gateway run > $ART/gateway.log 2>&1 &`.
     `hermes serve` runs no platform adapters, so it cannot serve the route.
   - Wait, bounded, for the route:
     `timeout 300 bash -c "until curl -fsS http://127.0.0.1:$WP/health; do sleep 2; done"`.
   - Start `hermes dashboard --host 127.0.0.1 --port 9119 --no-open --isolated --skip-build`
     per the UI driver §1a, and wait for its ready line.
6. **The forwarded failure.**
   - Build the body with `python3 -c`: load
     `$SCN/fixtures/ing-f66-ui-default/envelope-check-run-failure.json` and set a
     fresh `delivery_id` (`ing-f66-ui-<epoch>`). The file already carries
     `event_type: ingress.v1`.
   - POST it with `curl -sS -X POST http://127.0.0.1:$WP/webhooks/ingress -H 'Content-Type: application/json' -H "X-Request-ID: <that id>" --data-binary @<body file>`.
     Expect HTTP 202 `accepted`.
   - Wait, bounded (`timeout 120`), until the outbox delivery is `done`. Poll with
     `python3 -c` over `$HERMES_HOME/plugin-data/nv-ingress/data.db`:
     `select state from deliveries where outbox_delivery = '<that id>'`.

## Steps

1. Open the dashboard. In the profile combobox select `ing-f66-ui-owner`
   (expect the `ing-f66-ui-owner »` context), then open **Sessions** → expect:
   exactly one session listed for that profile, the seeded one, its id `$S`
   visible in the row.
2. Click that session → expect: the transcript shows the seed turn and, after it,
   a message containing `check_run`, the check name `ing-f66-ui-check`,
   `concluded failure`, and the line `ingress-delivery: github/<that id>/7`.
3. Return to **Sessions** and refresh → expect: still exactly one session for
   `ing-f66-ui-owner` (no new session was minted).
4. Switch the profile to `default` and open **Sessions** → expect: no session
   titled with the webhook route or the delivery id; the bridge skipped the
   one-shot webhook session.

## Pass

The criterion holds when the delivery text renders inside the pre-existing owner
session `$S` and no new session appears for either profile.

## Evidence

- `step-<n>.png` per step.
- The before/after counts and the outbox row state from these `python3 -c` reads:
  - `select count(*) from sessions` over `profiles/ing-f66-ui-owner/state.db`,
    before (Setup 3) and after step 3 → equal (`N0`);
  - `select count(*) from sessions where source = 'webhook'` over the DEFAULT
    `state.db` → `0`;
  - `select target_profile, target_session, state from deliveries where outbox_delivery = '<that id>'`
    over `$HERMES_HOME/plugin-data/nv-ingress/data.db` → `('ing-f66-ui-owner', '$S', 'done')`.
- `gateway.log` and the curl response (202 body).
- Teardown: stop the dashboard and the gateway.
