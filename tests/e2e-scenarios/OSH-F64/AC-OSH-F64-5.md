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
down): `openshell sandbox create --name osh-f64-gw --from nemoclaw-hermes-sandbox:local` (the
GATEWAY image, §D1; the served workers create `--from localhost/hermes-openshell-sandbox:pinned`),
start its Hermes gateway and capture its loopback credential URL as `$GW_URL`, then
`install-into-sandbox.sh <spec> --ref <sha> --gateway-url "$GW_URL"` (a real run requires
`--gateway-url`), boot serving `default` + ≥2 coworkers under a stub model; `openshell forward
start <29xxx-port> osh-f64-gw` (brokered, `-d`, bind `172.17.0.1`, port in 29000–29999 — the
sandbox gateway listens on that same port) establishes the local forward; the dashboard is
reached at the forwarded address under agent-browser (hermes-ui-driver skill). No fixture is
re-installed here. Teardown deletes `osh-f64-*` at the end.

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
