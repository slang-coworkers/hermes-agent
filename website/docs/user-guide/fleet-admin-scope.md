---
title: Fleet-admin scope (cli_scope and the host command gate)
description: How the nv-fleet-gates pre_tool_call veto realises NanoClaw's cli_scope and host command gate — an admin-shaped tool whose canonical name is in the fleet-admin set is denied to any non-orchestrator profile, with a managed profile_roles map a profile cannot self-elevate.
---

# Fleet-admin scope — cli_scope and the host command gate

In a one-gateway Bot-Mode fleet every coworker is a **profile** of the shared
gateway, and NanoClaw reserves fleet-admin operations — onboarding or rendering a
coworker, board and room administration, cron for another profile, cost-cap
control, ledger writes — for the elevated (orchestrator) scope. This page is the
NanoClaw→Hermes mapping for that boundary: how the `nv-fleet-gates` plugin's
single `pre_tool_call` veto denies an admin-shaped tool **by its canonical name**
to any non-orchestrator profile. The veto enforces this for every tool whose
canonical name is in the **fleet-admin set** — the configured `admin_tools` plus
the vendored `cronjob_manage` (§4); populating that set with the complete admin
inventory and pinning it immutable is a separate, tracked step (**GOV-F26.a**,
§7). It documents an already-merged surface; nothing here is new behaviour.

## 1. What NanoClaw did

NanoClaw's `ncl groups config cli-scope` gives each agent **group** a `cli_scope`
of `full`, `group`, or `none`. Admin-shaped host commands — onboard/render a
coworker, board and room administration, cron for another group, cost-cap
control, ledger writes — are refused unless the caller holds the elevated
(orchestrator) scope. The refusal is by **command identity**, keyed to the
caller's group, and it cannot be widened by the group editing its own config.
That is the "host command gate": one choke point that decides, per admin command
per caller, allow vs deny. (Context — this is the behaviour being ported; no
Hermes citation.)

## 2. The Hermes feature that covers it

The realisation is the `nv-fleet-gates` plugin's single `pre_tool_call` veto, and
specifically its **FLEET-ADMIN SCOPE** predicate `_fleet_admin_block`: an
admin-shaped tool whose canonical name is in the fleet-admin set (§4) is denied to
any profile whose resolved role is not `orchestrator`
(`plugins/nv-fleet-gates/__init__.py:312-320`). That predicate runs **inside**
`_gate`, the single `pre_tool_call` callback the plugin registers for the whole
fleet (`:590`) — the gate is the callback, `_fleet_admin_block` is one predicate
in its chain (§3). The tool is the "host command", the profile identity is the
caller's group, and the veto is the gate. The terms map one to one:

| NanoClaw | Hermes |
|---|---|
| agent *group* | *profile* (one `HERMES_HOME` under the shared gateway) |
| `cli_scope` elevated | resolved role `orchestrator` |
| admin host command | tool name in the fleet-admin set |
| the host command gate | the one `pre_tool_call` callback `_gate`, whose FLEET-ADMIN predicate is `_fleet_admin_block` |
| refusal by command identity | denial by canonical tool name |

## 3. How the decision is made (walk the chain)

The one `pre_tool_call` callback runs an ordered, **block-before-approve**
predicate chain and returns the first matching directive
(`plugins/nv-fleet-gates/__init__.py:437-454`):

```
canonicalise(tool_name)
  → _restricted_block   (restricted-core backstop; fail-closed)
  → SANDBOX             (per-session sandbox backend; inert unless enforce_sandbox)
  → FLEET-ADMIN         (admin-shaped tools are orchestrator-only)   ← this page
  → WIRING (unwired)    (peer messaging refused unless a wiring edge exists)
  → MCP-SCOPE           (an mcp_* tool absent from the profile's allow-list is denied)
  → GATES               (host-writer · plan gate · critique gate)
  → WIRING (approve)    (a gated edge escalates to human approval)
```

The **first** match wins (`:444`). FLEET-ADMIN returns a
`{"action": "block", "message": …}` directive when `canon in admin_tools` and
the caller is not the orchestrator, and returns `None` (deferring to the rest of
the chain) otherwise. The incoming `tool_name` is first run through
`canonicalise()` (`plugins/nv-fleet-gates/aliases.py:24-28`), so a legacy
spelling is matched identically to its canonical name. Per the stock contract,
the first valid `block` or `approve` directive wins for `pre_tool_call`
(`website/docs/user-guide/features/hooks.md:441`, tag `v2026.8.31`) — the chain's
own tail is an `approve` path (`_wiring_approve`) — and a `block` must carry a
non-empty `message` or core drops it (`hooks.md:555`, tag) — every block outcome
is minted with one.

## 4. The fleet-admin set

The set of names FLEET-ADMIN denies is a **union** of operator config and a
vendored floor:

```
admin_tools = set(ctx.get_config("admin_tools", []) or []) | _VENDORED_ADMIN
```

(`plugins/nv-fleet-gates/__init__.py:214`), where
`_VENDORED_ADMIN = {"cronjob_manage"}` (`:68`) is always orchestrator-only
fleet-wide — a worker owns no cron in this fleet, a rule stricter than "cron for
another group". Names are matched **after** alias-canonicalisation
(`aliases.py:24-28`, whose `_LEGACY_TOOL_ALIASES` maps `cronjob → cronjob_manage`
at `:15-21`), so the legacy spelling `cronjob` is denied identically to the
canonical `cronjob_manage`. The config half (`admin_tools`) is what the fleet
render populates per profile; the vendored half is denied regardless of config,
even when `admin_tools` is empty.

## 5. Why a profile cannot elevate its own role

Role identity is **not a plugin-writable setting**. `_current_profile()` is
`Path(get_hermes_home()).name` (`plugins/nv-fleet-gates/__init__.py:136-144`) —
the active per-turn profile home, which a bot cannot forge through config. Role
resolution then prefers the managed layer: when it pins `profile_roles`,
`_managed_profile_roles()` reads `managed_scope.load_managed_config()`
**directly** (`:147-169`), not the deep-merged `ctx.get_config()` map, so a role a
bot adds for itself in its own config is ignored. A profile absent from the
managed map defaults to `worker` (`_role()` / `_is_orch()`, `:252-258`). The net
effect: a worker that writes `profile_roles: {self: orchestrator}` into its own
`config.yaml` stays a worker, and the admin tool stays blocked.

State the boundary plainly. The **role map** is safe once pinned in managed
scope. The **set of admin tool names**, however, is read via
`ctx.get_config("admin_tools")` — the deep-merged config (`:214`) — so it is
immutable only when it too is pinned in managed scope; merely rendering it into a
profile's own (writable) config does not stop that profile from emptying it.
Only the vendored `cronjob_manage` survives an emptied config. Populating the
full admin surface and pinning it managed is tracked as the residual **GOV-F26.a**
(see §7); this page documents the enforcement mechanism, which is complete: given
the set, denial by name works.

## 6. Why the gate cannot be bypassed by breaking it

`pre_tool_call` is a `VALID_HOOKS` member
(`hermes_cli/plugins.py:164`, tag `v2026.8.31`), is timeout-bounded, and **fails
closed** on timeout in core: `_HOOK_TIMEOUT_FAIL_CLOSED_HOOKS = {"pre_tool_call"}`
(`hermes_cli/plugins.py:443`, tag; documented at
`website/docs/user-guide/features/hooks.md:573`, tag). A callback that *raises* is
otherwise swallowed by core and the tool would proceed, so the plugin wraps the
whole gate body in `try/except BaseException → _block`
(`plugins/nv-fleet-gates/__init__.py:452-454`) — an internal error refuses the
call rather than opening it. The restricted-core set is scanned (`:81-105`) and
asserted at load time (`:235-249`), and re-checked at gate time by the
`_restricted_block` backstop (`:418-435`), which fails closed until the running
registry is complete, so the fleet-admin decision can never be reached on a
half-built registry that would mask it.

## 7. How it is configured (operator note)

1. Enable the plugin: add `nv-fleet-gates` to `plugins.enabled`.
2. Set the role map: `plugins.entries.nv-fleet-gates.settings.profile_roles`,
   naming the orchestrator profile. **Pin it in managed scope** — a role a bot
   adds for itself in its own config is ignored only when the map is managed
   (§5). In this fleet the `nv-coworker-compose` render already emits
   `profile_roles` into managed scope
   (`plugins/nv-coworker-compose/compose.py:1383`, built and merged at
   `:3044-3048`).
3. Set the denied set: `plugins.entries.nv-fleet-gates.settings.admin_tools`.

The FLEET-ADMIN predicate denies **only** the tool names present in
`admin_tools ∪ {cronjob_manage}` (the rest of the chain independently denies
sandbox, wiring, MCP-scope and gate violations). Populating `admin_tools` with the full admin
surface for every non-orchestrator profile — and pinning it managed so a worker
cannot empty its own set — is the fleet render's job, tracked as **GOV-F26.a**
(CONFIGURE|BUILD). The vendored `cronjob_manage` is denied regardless of config.

## 8. Adopt-proof

This mapping is proven against the baseline by the hermetic acceptance test
[`tests/plugins/test_gov_f26_fleet_admin_scope_acceptance.py`](https://github.com/slang-coworkers/hermes-agent/blob/release/v2026.8.31-e2e-fixed/tests/plugins/test_gov_f26_fleet_admin_scope_acceptance.py),
which loads `nv-fleet-gates` from an isolated `HERMES_HOME` through the real
`PluginManager().discover_and_load()` path and drives the one `pre_tool_call`
gate:

- **AC-GOV-F26-1** — an admin tool in `admin_tools` is blocked by name for a
  worker profile and proceeds for the orchestrator profile.
- **AC-GOV-F26-2** — the vendored `cronjob_manage` is blocked for a worker with
  `admin_tools: []`, and its legacy alias `cronjob` is canonicalised to the same
  denial.
- **AC-GOV-F26-3** — a managed `profile_roles` pin naming only the orchestrator
  makes an omitted worker default to `worker`; a self-added local role survives
  config merging but is ignored by the managed-only lookup, so the admin tool
  stays blocked.

The test fails on stock `v2026.8.31` (the plugin directory is absent) and passes
on the baseline where it is merged — that asymmetry is the adopt proof.

## Covering surface (code references)

The covering plugin is a fork addition present only in the baseline
(`release/v2026.8.31-e2e-fixed`); the stock primitives it rides are cited for the
pinned tag `v2026.8.31` (`29112bef`) with the upstream `main` (`c62bd9f207`) line
alongside.

**Covering plugin (baseline `release/v2026.8.31-e2e-fixed`):**

- `plugins/nv-fleet-gates/__init__.py:312-320` — `_fleet_admin_block` (the
  FLEET-ADMIN predicate); `:214` `admin_tools` union; `:68`
  `_VENDORED_ADMIN = {"cronjob_manage"}`; `:136-144` `_current_profile()`;
  `:147-169` `_managed_profile_roles()`; `:252-258` `_role()` / `_is_orch()`;
  `:437-454` `_gate` ordered chain + fail-closed wrapper; `:81-105`
  restricted-set scan, `:235-249` load-time assertion, `:418-435` gate-time
  `_restricted_block` backstop; `:590` `register_hook("pre_tool_call", _gate)`.
- `plugins/nv-fleet-gates/aliases.py:15-28` — `_LEGACY_TOOL_ALIASES`
  (`cronjob → cronjob_manage`) and `canonicalise()`.

**Stock primitives (tag `v2026.8.31` / `main`):**

- `pre_tool_call` in `VALID_HOOKS` — `hermes_cli/plugins.py:164` / `:109`.
- `PluginContext.register_hook` — `hermes_cli/plugins.py:3387` / `:916`.
- `pre_tool_call` directive row ("first valid `block` or `approve` directive
  wins") — `website/docs/user-guide/features/hooks.md:441` / `:441`.
- Block directive shape `{"action": "block", "message": …}` —
  `hooks.md:550` / `:552`.
- Fail-closed contract — `hermes_cli/plugins.py:443`
  (`_HOOK_TIMEOUT_FAIL_CLOSED_HOOKS`) + `hooks.md:573` / `hooks.md:557`.
- Non-forgeable identity — `hermes_constants.py:114` (`get_hermes_home`) /
  `:101`, `:154` (`get_process_hermes_home`) / `:150`.
- Managed-scope pin — `hermes_cli/managed_scope.py:115`
  (`load_managed_config`) / `:101`.

## See also

- [Fleet MCP scope](./fleet-mcp-scope.md) — the companion per-profile MCP
  allow-list the same `pre_tool_call` veto enforces (MCP-SCOPE predicate).
- [nv-fleet-gates](../developer-guide/plugins/nv-fleet-gates.md) — the plugin's
  full predicate chain and developer contract.
