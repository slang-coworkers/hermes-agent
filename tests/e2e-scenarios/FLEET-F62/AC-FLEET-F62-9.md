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

# AC-FLEET-F62-9 — the whole fleet on one dashboard, one gateway (ui)

`hermes dashboard` over the same testbed home, one gateway / one `/api/health`: the
profile switcher lists exactly default + the five; Bots shows the five with rendered
role descriptions; Rooms shows fleet-room(5) + review-room(3); Sessions lists one
canonical `Bot Chat` per coworker and the orchestrator's carries the `P6-<nonce>`
turns; Cron and Kanban load under the same gateway; no second gateway/port anywhere.

## Setup (after the fixtures are installed)

- `hermes dashboard` over the testbed home with the ONE stub-model gateway running
  (no live budget).
- The orchestrator Bot Chat is seeded with the `P6-<nonce>` turns (or a stub
  equivalent) so the Sessions view has content to show.
- The dashboard SPA opens the DEFAULT profile's chat by default; the profile switcher
  is used to reach each coworker.

## Steps

1. Open the dashboard, profile switcher → expect: exactly default + the five listed.
2. Bots view → expect: the five with their rendered role descriptions
   (`ui_meta['hermes-bots']`).
3. Rooms view → expect: fleet-room (5 members) and review-room (3 members).
4. Sessions view → expect: one canonical `Bot Chat` per coworker; the orchestrator's
   carries the `P6-<nonce>` turns.
5. Cron and Kanban pages load; the whole app shows ONE `/api/health` / one gateway, no
   second port.

## Pass

The operator sees the whole fleet — switcher, bots, rooms, sessions, cron, kanban —
driven from one gateway on one panel.

## Evidence

`scenario-AC-FLEET-F62-9/step-1..n.png` + `evidence.txt`.
