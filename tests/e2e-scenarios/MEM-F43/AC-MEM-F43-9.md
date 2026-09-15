---
ac: AC-MEM-F43-9
kind: ui
model: stub
fixtures:
  - fixtures/mem-f43-bot
timeout_s: 300
---

# AC-MEM-F43-9 — the shared-learnings skill is listed in the dashboard Skills tab

A profile configured **only** via `skills.external_dirs` pointed at the fleet's
learnings clone (no local copy of the skill) surfaces the shared skill in the dashboard
Skills tab. This proves the operator-visible half of MEM-F43: a learning that lives once
in the clone is discoverable per profile in the UI. The stub model never needs to run —
the Skills catalog is loaded from disk — so this is a `ui: model: stub` scenario with a
deterministic `GET /api/skills` backstop.

## Setup

The tester installs the `fixtures:` list first (`hermes profile install
fixtures/mem-f43-bot --name mem-f43-bot -y`). `mem-f43-shared/` is a **plain skill tree,
not a distribution**, so it is deliberately absent from `fixtures:` — this Setup copies it
into place. All steps below run AFTER the install:

1. Copy the shared skill tree into a plain directory in the testbed home:
   `mkdir -p "$TB/shared-learnings/skills" && cp -r "$SCN/fixtures/mem-f43-shared/skills/." "$TB/shared-learnings/skills/"`.
   `fleet-postmortem/SKILL.md` lands at `$TB/shared-learnings/skills/fleet-postmortem/SKILL.md`
   (`name: fleet-postmortem`); the profile keeps **no local copy** of it.
2. Rewrite the installed profile's RAW `config.yaml` so `skills.external_dirs` is the
   absolute clone path with no placeholder left:
   ```bash
   python3 - "$HERMES_HOME/profiles/mem-f43-bot/config.yaml" "$TB/shared-learnings/skills" <<'PY'
   import sys, yaml
   cfg_path, ext = sys.argv[1], sys.argv[2]
   cfg = yaml.safe_load(open(cfg_path)) or {}
   cfg.setdefault("skills", {})["external_dirs"] = [ext]
   yaml.safe_dump(cfg, open(cfg_path, "w"))
   PY
   ```
3. Make `mem-f43-bot` appear in the Bot-Mode roster / profile rail by writing its
   `profile.yaml` `ui_meta.hermes-bots` block (no CLI writes it; the dashboard's
   `profiles.configure` RPC path is what normally does, so the scenario seeds it directly):
   ```bash
   python3 - "$HERMES_HOME/profiles/mem-f43-bot/profile.yaml" <<'PY'
   import sys, yaml, os
   p = sys.argv[1]
   data = yaml.safe_load(open(p)) if os.path.exists(p) else {}
   data = data or {}
   data.setdefault("ui_meta", {})["hermes-bots"] = {
       "title": "MEM-F43 Bot", "description": "Reads the shared-learnings clone via external_dirs",
   }
   yaml.safe_dump(data, open(p, "w"))
   PY
   ```
4. Start the dashboard bound to loopback for this `HERMES_HOME` (per hermes-ui-driver §1a):
   `hermes dashboard --host 127.0.0.1 --port 9119 --no-open --isolated --skip-build`, and
   wait for the `HERMES_DASHBOARD_READY port=9119 | Hermes Web UI` ready line before opening the page.

## Steps

1. Open `http://127.0.0.1:9119/` and select the `mem-f43-bot` profile in the profile rail
   (the `ProfileSwitcher`) → expect: `mem-f43-bot` is the active profile in the rail.
2. Click the **Skills** tab (nav item labelled "Skills", route `/skills`) → expect: the
   Skills table renders with rows.
3. Locate the `fleet-postmortem` row → expect: it is listed by name (the shared-learnings
   skill surfaces for a profile that references it only via `external_dirs`, with no local
   copy).

## Pass

The criterion holds when the `fleet-postmortem` skill appears **by name** in the dashboard
Skills list for `mem-f43-bot`, a profile configured only with `external_dirs` (no local copy).

## Evidence

- `step-1.png` — the profile rail with `mem-f43-bot` selected.
- `step-2.png` — the Skills tab rendered with rows.
- `step-3.png` — the `fleet-postmortem` row visible.
- Deterministic backstop — a `python3` `urllib` GET of the skills API asserting a returned
  row named `fleet-postmortem` (`/api/skills` reports `provenance` = hub/bundled/agent, not
  a literal "external" badge, so assert presence by name, not by a badge):
  ```bash
  python3 - <<'PY'
  import json, urllib.request
  rows = json.load(urllib.request.urlopen("http://127.0.0.1:9119/api/skills?profile=mem-f43-bot", timeout=30))
  names = [r.get("name") for r in rows]
  assert "fleet-postmortem" in names, f"fleet-postmortem absent from /api/skills: {names}"
  print("OK fleet-postmortem present via external_dirs")
  PY
  ```
