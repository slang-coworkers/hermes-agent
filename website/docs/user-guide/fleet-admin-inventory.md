---
title: Fleet-admin inventory and managed immutability
description: The complete fleet-admin tool inventory the nv-fleet-gates pre_tool_call veto reaches, and how pinning it in machine-wide managed scope makes it immutable to every non-orchestrator profile — a worker cannot un-gate the admin surface by emptying its own admin_tools.
---

# Fleet-admin inventory and managed immutability

[Fleet-admin scope](./fleet-admin-scope.md) documents the *enforcement mechanism*: the
`nv-fleet-gates` `pre_tool_call` veto denies an admin-shaped tool **by its canonical name** to
any non-orchestrator profile. That page proved deny-by-name with the fleet-admin set supplied
directly. This page closes the complementary property: with the **complete** fleet-admin
inventory pinned in the machine-wide managed layer, the set is **immutable** to a
non-orchestrator profile — a worker cannot un-gate the admin surface by emptying or overriding
its own `admin_tools`. Both halves are already present: the veto is the merged `nv-fleet-gates`
plugin, and the immutability is a **native property of the stock config loader** (managed-scope
merge — managed-wins, list-replace). This page documents that surface and the single operator
action that arms it; nothing here is new behaviour.

## 1. What NanoClaw did

NanoClaw's `ncl groups config cli-scope` refuses admin-shaped host commands — onboard/render a
coworker, board and room administration, cron for another group, cost-cap control, ledger
writes — by the caller's **group**, and the refusal is **not widenable by the group's own
config**: a group cannot grant itself an elevated scope by editing its own settings. (Context —
this is the behaviour being ported; no Hermes citation.)

## 2. The complete fleet-admin inventory

The fleet-admin inventory is the set of orchestrator-only **gateway tools** the `pre_tool_call`
veto can reach. There are six:

| tool | provided by | role |
|---|---|---|
| `onboard_coworker` | `nv-coworker-compose` (`plugins/nv-coworker-compose/__init__.py:944-955`) | coworker composition |
| `onboard_project` | `nv-coworker-compose` (`:944-955`) | coworker composition |
| `create_agent` | `nv-coworker-compose` (`:944-955`) | coworker composition |
| `record_decision` | `nv-approval-ledger` (`plugins/nv-approval-ledger/__init__.py:514-529`) | ledger write |
| `record_human_verdict` | `nv-approval-ledger` (`:514-529`) | ledger write |
| `cronjob_manage` | `nv-fleet-gates` vendored floor (`plugins/nv-fleet-gates/__init__.py:68`) | cron for another profile |

Five of the six are **config-supplied**: the operator lists them in
`plugins.entries.nv-fleet-gates.settings.admin_tools`. The sixth, `cronjob_manage`, is the
**vendored floor** `_VENDORED_ADMIN = {"cronjob_manage"}` (`plugins/nv-fleet-gates/__init__.py:68`),
unioned into the denied set regardless of config (§4) — a worker owns no cron in this fleet.

**Explicitly out of the inventory, by design — not an omission.** These surfaces are handled
outside `admin_tools` by their own controls, or are not tools at all, so folding them in would be
redundant or inapplicable:

- **Board-routing operations (`kanban_list`, `kanban_unblock`)** are hidden from dispatcher-owned
  task workers by `_check_kanban_orchestrator_mode` (`tools/kanban_tools.py:122-135`; registered at
  `:2365-2371` and `:2464-2471`) and refused by `_require_orchestrator_tool` if routed from a task
  context (`:467-482`) — this is **task-context gating**, not role authorization through
  `admin_tools`. The task-lifecycle tools (`kanban_complete`, `kanban_block`, `kanban_heartbeat`,
  `kanban_comment`) use `_check_kanban_mode` (`:103-119`) and remain worker-available.
- **Cost-cap control** is the `cost-cap` **CLI command** (registered at
  `plugins/nv-cost-cap/__init__.py:1063-1072`); its setter is orchestrator-only, resolved via
  `_orchestrator_profile()` (`:892-905`). A CLI command is not a gateway tool, so `pre_tool_call`
  cannot reach it at all.
- **`render`** is not a tool — rendering a profile happens *inside* `onboard_coworker`, which is
  already in the inventory.

FLEET-F62's `FLEET_ADMIN_TOOLS` — a 4-name tuple (`cronjob_manage`, `onboard_coworker`,
`onboard_project`, `create_agent`, `tests/plugins/test_fleet_f62_acceptance.py:37`) asserted as a
`<=` subset (`:301`) — is contained in this six-name inventory, so the two suites remain
compatible.

## 3. The Hermes feature that makes the inventory immutable

`nv-fleet-gates` reads the inventory through `ctx.get_config("admin_tools")` (§4), which resolves
to the **managed-merged effective config**, not the profile's raw file. Two stock behaviours of
the config loader make a managed-pinned list immutable to a profile-local override:

1. **Managed scope wins at the leaf.** `PluginContext.get_config` reads `load_config_readonly()`
   (tag `hermes_cli/plugins.py:1492`, read at `:1508` | main `hermes_cli/plugins.py:257`, read at
   `:261`); that loader merges the managed overlay last and managed-wins (tag
   `hermes_cli/config.py:4063` — `_deep_merge(expanded, managed_expanded)`, comment "Managed scope
   wins at the leaf" `:4044`, managed load `:4049` | main `hermes_cli/config.py:2253`, managed load
   `:2244`).
2. **A list value is replaced wholesale.** `_deep_merge` recurses **only** when both sides are
   dicts; for every other type — a list included — the override replaces the base value outright
   (tag `hermes_cli/config.py:2820`, replace branch `:2844` | main `hermes_cli/config.py:1563`).

Because `admin_tools` is a **list**, a managed pin replaces a profile-local `admin_tools: []`
outright. Contrast `profile_roles`, a **dict**: a per-key merge would let a profile-local entry
survive, which is exactly why `nv-fleet-gates` reads `profile_roles` from the managed layer
*directly* via `_managed_profile_roles()` (`plugins/nv-fleet-gates/__init__.py:147-169`) rather
than through `get_config`. For the list-valued `admin_tools`, `get_config` alone suffices once the
list is pinned — no dedicated accessor is needed.

## 4. How the veto consumes the inventory

The plugin captures the inventory **once, at load time** — the block is headed "config is
immutable mid-session" (`plugins/nv-fleet-gates/__init__.py:226`):

```python
admin_tools = set(ctx.get_config("admin_tools", []) or []) | _VENDORED_ADMIN
```

(`:234`) — the managed-merged config value (§3) unioned with the vendored floor. The FLEET-ADMIN
predicate `_fleet_admin_block` then denies the call when the caller is not the orchestrator and
the tool's canonical name is in that set (`:342-350`). The incoming `tool_name` is
alias-canonicalised first (`plugins/nv-fleet-gates/aliases.py:17`, `cronjob → cronjob_manage`), so
the legacy spelling `cronjob` is denied identically to `cronjob_manage`. Because the value is
captured at load from the *managed-merged* config, a profile that later empties its own
`admin_tools` cannot shrink the set the running gate checks.

## 5. Operator deployment (the one action)

Add the complete inventory to the machine-wide managed config — `$HERMES_MANAGED_DIR/config.yaml`
(or `/etc/hermes/config.yaml` when the environment override is unset):

```yaml
plugins:
  entries:
    nv-fleet-gates:
      settings:
        admin_tools:
          - onboard_coworker
          - onboard_project
          - create_agent
          - record_decision
          - record_human_verdict
          - cronjob_manage
```

List all six names. `cronjob_manage` stays redundantly protected by the vendored floor (§2), but
pinning it keeps the centrally managed inventory identical to the six-name acceptance fixture (§7).

The managed file is a **required fleet deployment artifact**. `managed_scope.load_managed_config()`
is fail-open — an absent or malformed managed `config.yaml` resolves to `{}` (tag
`hermes_cli/managed_scope.py:115` | main `:101`) — so if the pin is missing, *only* the vendored
`cronjob_manage` is guaranteed denied and the five config-supplied names revert to profile-mutable.
Validate the managed file's presence at deploy time. The `$HERMES_MANAGED_DIR` override is honoured
before the pytest guard (tag `hermes_cli/managed_scope.py:52`, env read `:65` | main `:39`, `:46`).

## 6. Why a profile cannot widen it

Managed-wins + list-replace (§3): a profile-local `admin_tools` — any value, including `[]` — is
discarded by the merge, and the veto sees only the managed inventory (∪ the vendored floor). The
caller's *profile name* is not plugin-writable either: `_current_profile()` derives it from
`Path(get_hermes_home()).name` (`plugins/nv-fleet-gates/__init__.py:136-144`), the active per-turn
profile home, which a bot cannot forge through config.

Role authorization is a **separate prerequisite**, not something this row establishes. When
managed `profile_roles` is pinned, `_managed_profile_roles()` wins and a self-added local role is
ignored (`:147-169`, `:272-275`); when it is absent, `_role()` falls back to the profile-local
`profile_roles` map, so a worker could self-promote by editing its own config. This row's
guarantee is therefore precisely scoped: with `admin_tools` pinned managed, the **inventory** a
worker faces is immutable — a worker cannot un-gate a tool by emptying its own `admin_tools`. It
relies on the fleet's managed `profile_roles` map remaining installed (see the enforcement page
[§5](./fleet-admin-scope.md)); pinning `admin_tools` alone does not prevent local role
self-promotion.

## 7. Adopt-proof

This immutability is proven against the baseline by the hermetic acceptance test
[`tests/plugins/test_gov_f26a_managed_admin_immutability_acceptance.py`](https://github.com/slang-coworkers/hermes-agent/blob/release/v2026.8.31-e2e-fixed/tests/plugins/test_gov_f26a_managed_admin_immutability_acceptance.py),
which loads `nv-fleet-gates` from an isolated `HERMES_HOME` through the real
`PluginManager().discover_and_load()` path, pins the six-name inventory in a `HERMES_MANAGED_DIR`
managed `config.yaml`, and drives the one `pre_tool_call` gate:

- **AC-GOV-F26.a-1** (the mandatory fail-closed escalation-attempt control) — with the complete
  inventory pinned in the managed layer, a worker profile that sets its own local `admin_tools: []`
  still has **every** inventory member blocked.
- **AC-GOV-F26.a-2** — under the same pin and the same local empty override, the orchestrator
  profile is admitted for all six names — the immutability does not over-block the elevated role.
- **AC-GOV-F26.a-3** — without a managed pin, the same worker's local `admin_tools: []` leaves a
  config-supplied member (`onboard_coworker`) admitted while the vendored `cronjob_manage` stays
  blocked — proving the managed-scope pin is the load-bearing mechanism.

The test fails on stock `v2026.8.31` (the plugin directory is absent) and passes on the baseline
where `nv-fleet-gates` is merged — that asymmetry is the adopt proof.

## 8. See also

- [Fleet-admin scope](./fleet-admin-scope.md) — the enforcement mechanism: how the one
  `pre_tool_call` veto denies an admin-shaped tool by canonical name to a non-orchestrator profile.

**Forward note.** Auto-emitting `admin_tools` from `nv-coworker-compose`'s managed-fragment render
(`_build_managed_fragment`, the same step that already pins `profile_roles` and the GOV-F23
approvals) would remove the manual operator step above. That is a possible future CONFIGURE
enhancement and is out of scope for this adopt-verify, which keeps the inventory operator-supplied.

## Covering surface (code references)

The covering plugin is a fork addition present only in the baseline
(`release/v2026.8.31-e2e-fixed`); the stock primitives it rides are cited for the pinned tag
`v2026.8.31` (`29112bef`) with the upstream `main` (`c62bd9f207`) line alongside.

**Covering plugin (baseline `release/v2026.8.31-e2e-fixed`):**

- `plugins/nv-fleet-gates/__init__.py:234` — `admin_tools = set(ctx.get_config("admin_tools", []) or []) | _VENDORED_ADMIN` (captured once, "config is immutable mid-session" `:226`); `:68` `_VENDORED_ADMIN = {"cronjob_manage"}`; `:342-350` `_fleet_admin_block`; `:136-144` `_current_profile()`; `:147-169` `_managed_profile_roles()` (the dict-valued contrast) and `:272-275` `_role()` (managed-first, profile-local fallback); `:620` `register_hook("pre_tool_call", …)`.
- `plugins/nv-fleet-gates/aliases.py:17` — `cronjob → cronjob_manage` canonicalisation.
- Inventory registrations: `plugins/nv-coworker-compose/__init__.py:944-955` (`onboard_coworker`, `onboard_project`, `create_agent`); `plugins/nv-approval-ledger/__init__.py:514-529` (`record_decision`, `record_human_verdict`).
- Out-of-inventory gates: `tools/kanban_tools.py:122-135` (`_check_kanban_orchestrator_mode`, guarding `kanban_list` `:2365-2371` and `kanban_unblock` `:2464-2471`), `:103-119` (`_check_kanban_mode`, worker-available lifecycle tools), `:467-482` (`_require_orchestrator_tool` backstop); `plugins/nv-cost-cap/__init__.py:1063-1072` (`cost-cap` CLI command), `:892-905` (`_orchestrator_profile`).

**Stock primitives (tag `v2026.8.31` / `main`):**

- `PluginContext.get_config` → `load_config_readonly` — `hermes_cli/plugins.py:1492` (read `:1508`) / `:257` (read `:261`).
- Managed-wins merge — `hermes_cli/config.py:4063` (comment `:4044`, managed load `:4049`) / `:2253` (managed load `:2244`).
- `_deep_merge` list-replace (only dicts recurse) — `hermes_cli/config.py:2820` (replace branch `:2844`) / `:1563`.
- `load_managed_config` fail-open + `$HERMES_MANAGED_DIR` override — `hermes_cli/managed_scope.py:115` (`get_managed_dir` `:52`, env `:65`) / `:101` (`:39`, `:46`).
- `pre_tool_call` in `VALID_HOOKS` — `hermes_cli/plugins.py:164` / `:109`.
