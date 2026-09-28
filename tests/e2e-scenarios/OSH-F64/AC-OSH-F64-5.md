---
ac: AC-OSH-F64-5
kind: ui
model: stub
fixtures:
  - fixtures/osh-f64-gateway
  - fixtures/osh-f64-worker-orchestrator
  - fixtures/osh-f64-worker-builder
timeout_s: 300
---

# AC-OSH-F64-5 — dashboard attached through `openshell forward` lists served profiles + opens orchestrator Bot Chat (ui)

`openshell forward start <29xxx-port> osh-f64-gw` (brokered, `-d`, bind `172.17.0.1`, port in
29000–29999; or `openshell service expose`)
makes the gateway sandbox reachable locally; the hermes dashboard (`model: stub`) connected
through that forward lists the served profiles and opens the orchestrator's Bot Chat — one
gateway connection for the whole app. `<prefix>` = `osh-f64`. §D5 of the ADR records why
`ui` (dashboard) over `desktop` (Playwright): the dashboard and desktop app are independent
clients of the same gateway backend, and the dashboard proves the "one app, one gateway"
attach with the lighter surface reachable through the forward.

## Setup
This scenario provisions its OWN `osh-f64-gw` (it does not reuse AC-3's, which is torn
down): `openshell sandbox create --name osh-f64-gw --from osh-f64-gateway:pinned --policy
/workspace/extra/hermes-fleet-testbed/osh-f64/policy-gateway.yaml` (the pinned GATEWAY image, §D1
— built from base `nemoclaw-hermes-sandbox:local` + fork core @95de5f78; the served workers create
`--from localhost/hermes-openshell-sandbox:pinned`). BEFORE the installer, in this ORDER — steps 1,
2a, and 3 run INSIDE `osh-f64-gw`; the render + host-mirror in 2b–2c are HOST-lane commands
(`openshell sandbox exec`, host `mkdir`, host redirection) that reach into the sandbox:
1. Seed the managed dir with the §D6 scan guard: `export HERMES_MANAGED_DIR=/sandbox/.hermes/managed
   && mkdir -p $HERMES_MANAGED_DIR && cp /tmp/osh-lane/managed/config.yaml $HERMES_MANAGED_DIR/` (the
   pre-seeded managed carries `plugins.scan_on_install: false`).
2. Make the per-worker policies HOST-readable for the broker (LANE v4, §D3 `--policy-root`):
   (a) run the staged `installer.py phase-a --spec <spec> --ref <sha>` so `hermes coworker` exists;
   (b) render gateway-internally WITHOUT provisioning —
   `openshell sandbox exec osh-f64-gw -- sh -lc 'hermes coworker compose <spec> --out "$HERMES_HOME/.osh-f64/render"'`;
   (c) mirror each rendered policy to the host testbed root via a QUOTED remote shell (so `$HERMES_HOME`
   expands INSIDE the sandbox, not on the host) — `mkdir -p /workspace/extra/hermes-fleet-testbed/osh-f64/render/<role>`
   then `openshell sandbox exec osh-f64-gw -- sh -lc 'cat "$HERMES_HOME/.osh-f64/render/<role>/policy-<role>.yaml"' > /workspace/extra/hermes-fleet-testbed/osh-f64/render/<role>/policy-<role>.yaml`
   for each of the five roles (orchestrator, architect, builder, tester, reviewer) the installer provisions.
3. Bring the gateway up for Phase B: `export HERMES_DASHBOARD_SESSION_TOKEN=<token>` BEFORE starting
   `hermes serve` (web_server.py:589 — a bare `hermes serve` surfaces no token and the WS `/api/ws`
   upgrade still validates `?token` despite `auth_required:false`), start the sandbox's `hermes serve`
   messaging gateway, wait for readiness, and capture
   `$GW_URL=ws://127.0.0.1:<port>/api/ws?token=$HERMES_DASHBOARD_SESSION_TOKEN` (Phase B's live-WS
   preflight and room creation run against it, so the gateway MUST be up before the full install).

Then run `install-into-sandbox.sh <spec> --ref <sha> --gateway-url "$GW_URL" --policy-root
/workspace/extra/hermes-fleet-testbed/osh-f64`, boot serving `default` + ≥2 coworkers under a stub
model; `openshell forward start <29xxx-port> osh-f64-gw` (brokered, `-d`, bind `172.17.0.1`, port
in 29000–29999 — the sandbox gateway listens on that same port) establishes the local forward; the
dashboard is reached at the forwarded address under agent-browser (hermes-ui-driver skill). No
fixture is re-installed here. Teardown deletes `osh-f64-*` at the end.

## Steps
1. Open the dashboard through the forwarded URL → expect: it loads and reports connected to
   one gateway.
2. In the profile combobox, select `orchestrator` → expect: the `orchestrator »` prompt (the
   web SPA opens the DEFAULT profile's view regardless of connection, so a named-profile
   view requires this explicit select).
3. Read the profile rail / Bots roster → expect: it lists `default` + the served coworker
   profiles (screenshot).
4. Open the orchestrator's Bot Chat pane → expect: the chat view for the orchestrator
   profile renders (screenshot).

## Pass
The app, connected through a single `openshell forward` to the gateway sandbox, lists the
served profiles and opens the orchestrator's Bot Chat — one gateway connection for the
whole app.

## Evidence
`scenario-AC-OSH-F64-5/step-1..4.png` plus, where a served-profile fact is a row rather
than a pixel, a `python3 -c` query over the gateway sandbox's `$HERMES_HOME` state captured
to `evidence.txt` (the coworker image ships no `sqlite3` CLI; use the stdlib `sqlite3`
module in `python3 -c`).
