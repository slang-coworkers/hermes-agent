---
ac: AC-FLEET-F62-9
kind: ui
model: stub
fixtures:
  - fixtures/podman/fleet-f62-default
  - fixtures/podman/fleet-f62-orchestrator
  - fixtures/podman/fleet-f62-architect
  - fixtures/podman/fleet-f62-builder
  - fixtures/podman/fleet-f62-tester
  - fixtures/podman/fleet-f62-reviewer
timeout_s: 300
---

# AC-FLEET-F62-9 — the whole fleet on one web dashboard, one gateway (ui)

`hermes dashboard` (the **web** SPA, model: stub) over the same testbed home, connected to
ONE gateway (one `/api/health`, no second port): the profile switcher lists exactly default
+ the five coworkers; the Sessions page lists one canonical `Bot Chat` per coworker under
that one gateway, the orchestrator's carrying the seeded `P6-<nonce>` turns; the Cron page
renders under the same gateway.

> **Round-1 rescope (2026-09-18).** The web SPA registers no `/bots`, `/rooms` or `/kanban`
> route (`web/src/App.tsx:156-176` `BUILTIN_ROUTES_CORE`, `:186-224` `BUILTIN_NAV_REST`), so
> the Bots-roster + Rooms assertions this row originally placed on the web tier move to the
> desktop tier `AC-FLEET-F62-10` (roster of exactly five + the two rooms) and the live tier
> `AC-FLEET-F62-7/8` (functional room membership 5/3), and the Kanban-loads assertion moves
> to `AC-FLEET-F62-10` step 4. Net coverage is preserved — a re-tiering forced by an
> evidenced blocker, not scope shrinkage. Id and `ui:` kind unchanged.

## Setup (after the fixtures are installed)

- `hermes dashboard` over the testbed home with the ONE stub-model gateway running (no live
  budget).
- The orchestrator Bot Chat is seeded with the `P6-<nonce>` turns (or a stub equivalent) so
  the Sessions view has content to show.
- The dashboard SPA opens the DEFAULT profile's chat by default; the profile switcher is used
  to reach each coworker.

## Steps

1. Open the dashboard, profile switcher → expect: exactly default + the five coworkers listed
   (the whole fleet under one gateway).
2. Sessions view (`/sessions`) → expect: one canonical `Bot Chat` per coworker listed under
   the one gateway; the orchestrator's carries the seeded `P6-<nonce>` turns.
3. Cron view (`/cron`) → expect: the page loads under the same gateway.
4. Read `/api/health` / the app's connection target → expect: ONE gateway, one `/api/health`,
   no second port anywhere.

## Pass

The operator sees the whole fleet's profiles under one web panel on one gateway — the switcher
lists default + the five, Sessions lists each coworker's `Bot Chat` (orchestrator's carrying
the `P6-<nonce>` turns), Cron renders, a single `/api/health`. The Bots roster + the two rooms
+ the Kanban board are proven on the desktop tier `AC-FLEET-F62-10`, and functional room
membership on the live tier `AC-FLEET-F62-7/8`; none is a web SPA route.

## Evidence

`scenario-AC-FLEET-F62-9/step-1..n.png` (switcher, Sessions, Cron) + `evidence.txt` (the
`/api/health` body and the single-gateway/no-second-port check).
