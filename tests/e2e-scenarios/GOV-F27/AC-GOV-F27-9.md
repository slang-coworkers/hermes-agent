---
ac: AC-GOV-F27-9
kind: ui
model: stub
timeout_s: 300
fixtures: []
spec: fixtures/gov-f27-ui/coworker-types.yaml
---

# AC-GOV-F27-9 — dashboard MCP command center shows per-role server scope

**Criterion:** In the dashboard MCP command center, the reviewer profile lists
exactly its scoped read server(s) and the fixer profile lists exactly its scoped
write server(s) — the operator sees per-role MCP server scope.

No live MCP connection is required: the panel lists configured servers from
config (`GET /api/mcp/servers?profile=<name>` reads config, `hermes_cli/web_routers/mcp.py`),
not a live probe. Per-server **trust** is not asserted here — the pinned web MCP
panel (`web/src/pages/McpPage.tsx`) renders no trust field; trust is proven
hermetically in AC-GOV-F27-2 (rendered config) and AC-GOV-F27-7 (registration
annotation).

## Setup

The `fixtures:` list is empty on purpose: the fixture is a **compose spec**
(`fixtures/gov-f27-ui/`, a full `coworker-types.yaml` + supporting
`spines/skills/workflows/overlays`), not a pre-rendered profile, so it is
composed and installed here rather than auto-installed.

1. Compose the UI spec into a scratch dir:
   `hermes coworker compose tests/e2e-scenarios/GOV-F27/fixtures/gov-f27-ui/coworker-types.yaml --out "$OUT"`
   (`$OUT` a scratch dir; the type names `gov-f27-reviewer`/`gov-f27-fixer` are the
   rendered profile names).
2. Install the two rendered coworker profiles into the testbed `$HERMES_HOME`, one
   profile dir per type, keeping the dir basename equal to the type name. Pass `-y`
   so the install runs unattended (without it the `Proceed with install? [y/N]`
   prompt cancels under non-interactive stdin):
   `hermes profile install -y "$OUT/gov-f27-reviewer"` and
   `hermes profile install -y "$OUT/gov-f27-fixer"` (the DEFAULT/orchestrator profiles
   need not be installed for this panel check).
3. **Precondition assert (before launching the dashboard):** for each installed
   profile, its dir basename is an exact key in that profile's rendered
   `plugins.entries.nv-fleet-gates.settings.mcp_scope`:
   ```
   python3 -c '
   import sys, yaml, pathlib
   name = sys.argv[1]
   cfg = yaml.safe_load((pathlib.Path(sys.argv[2]) / "config.yaml").read_text())
   scope = cfg["plugins"]["entries"]["nv-fleet-gates"]["settings"]["mcp_scope"]
   assert name in scope, (name, sorted(scope))
   print("OK", name, "in mcp_scope")
   ' gov-f27-reviewer "$HERMES_HOME/profiles/gov-f27-reviewer"
   ```
   (repeat for `gov-f27-fixer`).
4. Launch the dashboard against the testbed home: `hermes dashboard` (or
   `hermes serve` + the web SPA), and open the MCP command center.

## Steps

1. In the MCP command center, scope to profile `gov-f27-reviewer` (the panel's
   profile selector; the SPA opens the DEFAULT profile by default, so select
   `gov-f27-reviewer` explicitly) → expect: the server list shows exactly
   `docs-ro` and no `repo-rw`.
2. Switch the MCP command center to profile `gov-f27-fixer` → expect: the server
   list shows exactly `repo-rw` and no `docs-ro`.

## Pass

The criterion holds when each profile's MCP panel lists exactly its role's scoped
server set and no other role's server (`gov-f27-reviewer` → `{docs-ro}`,
`gov-f27-fixer` → `{repo-rw}`).

## Evidence

- `step-1.png` — the MCP command center for `gov-f27-reviewer` showing `docs-ro`
  and not `repo-rw`.
- `step-2.png` — the MCP command center for `gov-f27-fixer` showing `repo-rw` and
  not `docs-ro`.
- The authoritative config-side proof (the fact lives in `config.yaml`, not
  `state.db`; the coworker image ships no `sqlite3` CLI):
  ```
  python3 -c '
  import yaml, pathlib
  for name, want in (("gov-f27-reviewer", {"docs-ro"}), ("gov-f27-fixer", {"repo-rw"})):
      cfg = yaml.safe_load((pathlib.Path("'"$HERMES_HOME"'/profiles") / name / "config.yaml").read_text())
      got = set((cfg.get("mcp_servers") or {}).keys())
      assert got == want, (name, got, want)
      print("OK", name, got)
  '
  ```
