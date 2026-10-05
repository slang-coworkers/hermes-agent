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
in 29000–29999 — the sandbox gateway listens on that same port) establishes the local forward. No
fixture is re-installed here.

**Dashboard Host-guard reconciliation (architect ruling 2026-09-29 — blessed).** The broker forces
the forward to bind `172.17.0.1`, but the CORE dashboard Host-header guard admits a `172.17.0.1`
Host only when non-loopback-bound + auth-configured (`hermes_cli/web_server.py:914-947`, decision
`:943-944`; mismatch → HTTP 400, `:950-982`). So the Setup establishes a loopback TCP bridge on the
tester host — `socat TCP-LISTEN:<port>,fork,reuseaddr,bind=127.0.0.1 TCP:172.17.0.1:<port>` — and
points agent-browser at `127.0.0.1:<port>`: the browser sends a LOOPBACK `Host` (accepted, no auth
gate) while the `openshell forward` stays the transport and the dashboard WS still carries
`HERMES_DASHBOARD_SESSION_TOKEN` (kept from step 3). The core guard is BYTE-UNTOUCHED, not weakened;
§D5 (ui over desktop) stands. The `dashboard.public_url`+auth alternative is REJECTED (a
non-loopback/public host engages the auth gate, `web_server.py:798-836`; RFC1918 treated as public).
**Tester-runtime prereq (VERIFY — likely already present):** `socat` IS in the coworker image
(dpkg-owned `/usr/bin/socat`) — it is simply not in Hermes' own `Dockerfile:73` apt set; confirm
`command -v socat` on the tester runtime and `install_packages` ONLY if that group's image lacks it
(NOT a default prereq).

**Onboarding (Option 2, as AC-4):** before step 3's Bot Chat pane, run the same default-context
onboard step AC-4 uses — `hermes -p default onboard coworker <spec> --gateway-url "$GW_URL"` from
the gateway root, asserting exit 0 and unchanged per-role config — so each served profile's
`ui_meta` (incl. the `hermes-bots` roster metadata) is populated (`tui_gateway/methods_profiles.py:300-306`).
The `profiles.list` backend appends every served profile's row REGARDLESS of `ui_meta`
(`methods_profiles.py:328,334`), so step 2's profile list does NOT depend on onboarding; onboarding
is required HERE to prove the canonical Bot Chat (step 3) and the bot metadata.

**Gateway-restart capture (§D8):** retain the install's `hermes gateway restart` exit code +
traceback as `scenario-AC-OSH-F64-5/gateway-restart.log` and confirm the gateway is up (as
AC-3/AC-4; firecrawl disabled in the render + installer default). Teardown deletes `osh-f64-*` (and
stops the socat bridge) at the end.

## Steps
1. Open the dashboard through the forwarded URL (agent-browser → the loopback bridge
   `127.0.0.1:<port>`) → expect: it loads and reports connected to one gateway.
2. Read the profile rail / Bots roster → expect: it lists `default` + the served coworker
   profiles (screenshot).
3. Select `orchestrator` in the profile combobox (the web SPA opens the DEFAULT profile's
   view regardless of connection, so a named-profile view requires this explicit select —
   expect the `orchestrator »` prompt), then open the orchestrator's Bot Chat pane → expect:
   the chat view for the orchestrator profile renders (screenshot).

## Pass
The app, connected through a single `openshell forward` to the gateway sandbox, lists the
served profiles and opens the orchestrator's Bot Chat — one gateway connection for the
whole app.

## Evidence
`scenario-AC-OSH-F64-5/step-1..3.png` plus, where a served-profile fact is a row rather
than a pixel, a `python3 -c` query over the gateway sandbox's `$HERMES_HOME` state captured
to `evidence.txt` (the coworker image ships no `sqlite3` CLI; use the stdlib `sqlite3`
module in `python3 -c`) + `scenario-AC-OSH-F64-5/gateway-restart.log` (the retained `hermes
gateway restart` exit code + traceback, §D8, as AC-3/AC-4).
