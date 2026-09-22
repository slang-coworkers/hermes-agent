# nv-fleet-gates — the fleet's single pre_tool_call veto

`nv-fleet-gates` is a standalone directory plugin that installs **exactly one**
`pre_tool_call` callback for the whole fleet. That callback runs one ordered,
**block-before-approve** predicate chain and returns the first matching directive:

```
canonicalise(tool_name)
  → SANDBOX          (per-session sandbox backend; inert unless enforce_sandbox)
  → FLEET-ADMIN      (admin-shaped tools are orchestrator-only)
  → WIRING (unwired) (peer messaging is refused unless a wiring edge exists)
  → MCP-SCOPE        (an mcp_* tool absent from the calling profile's rendered
                      allow-list is denied; transport-aware, fail-closed)
  → GATES            (host-writer path · plan gate · critique gate + PR-comment
                      human invitation)
  → WIRING (approve) (a gated edge escalates to the human-approval gate)
```

An **approve never pre-empts a block**: every blocking predicate is evaluated
before the gated-edge approve, so a marked delivery on a gated edge that still
lacks its critique is blocked, not approved.

## Fail-closed is the plugin's own obligation

Hermes swallows a raising `pre_tool_call` callback and lets the tool proceed
(only a hook *timeout* fails closed). So the entire callback body is wrapped in
`try/except BaseException` and any error returns a `block` directive. Every block
directive carries a non-empty `message` — core drops a message-less block, which
would be a silent fail-open.

## Configuration

All settings live under `plugins.entries.nv-fleet-gates.settings` and are
rendered per profile by the compose plugin; the fleet-uniform keys are pinned in
managed scope so a per-profile config cannot widen them.

| setting | meaning |
|---|---|
| `profile_roles` | map of profile name → role; a profile is the orchestrator when its role is `orchestrator`. Read from the managed-scope merge, so a per-profile override cannot escalate a worker. |
| `edges_db_path` | absolute path to the **fleet-shared** SQLite edges store (outside any profile home), written by `hermes wire` and read by every profile's predicate. |
| `enforce_sandbox` | when true, the SANDBOX predicate first hard-fails any turn whose active per-turn profile differs from the launch profile (cross-profile refusal, see below), then checks the resolved backend, brings up the container task env, and re-asserts the dangerous-command floor; and the MCP-SCOPE predicate additionally denies any non-`remote` (stdio/unknown-transport) `mcp_*` call. Off by default. |
| `expected_backend` | the container backend a profile's tool calls must resolve to when `enforce_sandbox` is true. |
| `mcp_scope` | the full profile-keyed MCP allow-list the compose plugin renders into **every** profile: `{profile: {mcp__<server>__<tool>: transport}}`. The MCP-SCOPE predicate selects the calling profile's entry by identity and denies any `mcp_*` tool absent from it (a profile absent from the map is denied all MCP — fail-closed); under `enforce_sandbox` a non-`remote` transport is also denied. The whole map is copied into each profile because `ctx.get_config` resolves against the active profile home. |
| `plan_gate` | when true, `write_file`/`patch`/mutating `terminal` are refused until the session has itself created a plan under `.hermes/plans/*.md`. |
| `required_stages` | the critique stages a session must have recorded (fresh) before a marked delivery or a `gh pr` egress. |
| `stage_markers` | the delivery-marker prefixes (`[Fix Report]`, `[Review Verdict]`, …) that make a `message_agent` a gated delivery. |
| `admin_tools` | the admin-shaped tool names that are orchestrator-only (unioned with a vendored default). |

The caller's identity is resolved from the **active per-turn profile home** — the
context-local `get_hermes_home()` the gateway multiplexer scopes each turn to (via
`set_hermes_home_override`, propagated into the tool-executor worker thread by
`copy_context()`) — never from a plugin-writable setting, so a worker cannot forge it.
That active identity is distinct from the **process launch profile**
(`get_process_hermes_home()`): the override-immune home the gateway was started under,
whose config was frozen into the process-global `terminal.*` sandbox settings. The
SANDBOX predicate's cross-profile hard-fail (below) compares the two.

## SANDBOX: cross-profile hard-fail (active profile ≠ launch profile)

When `enforce_sandbox` is true, the SANDBOX predicate's **first** check refuses any
gateway turn whose **active per-turn profile** differs from the **process launch
profile**, before any `TERMINAL_ENV` value is trusted:

- **active** — `get_hermes_home()`, the context-local profile the gateway multiplexer
  scoped this turn to.
- **launch** — `get_process_hermes_home()`, the override-immune home the gateway was
  started under.

The two are compared with `hermes_home_key()` (resolve + normcase — collision-free and
cross-platform), and a mismatch returns a `block` naming both profiles.

The check exists because `terminal.*` is frozen into the **process-global** `TERMINAL_*`
environment once at gateway startup, from the launch profile's config (there is no
per-turn terminal scope at this release). A gateway-served tool call for a **non-launch**
profile would therefore run inside the **launch** profile's container/egress config — a
silent cross-profile sandbox leak. The hard-fail is the defense-in-depth mitigation:
tool-bearing work for a non-launch profile is pushed to that profile's own kanban-board
subprocess, which bridges its own correct `terminal.*` at its own startup. The launch
profile's own turns (active == launch) are unaffected, and with `enforce_sandbox: false`
the whole predicate — this check included — is inert.

**`message_agent` is exempt from the entire SANDBOX predicate.** `message_agent`'s
delivery runs **host-local** (`bot_mode_dm.py`, `_host_local=True`) as a control-plane
child that never consumes the active profile's frozen `terminal.*` config, so it is not
a sandbox-consuming call: the cross-profile, backend and task-env checks all target
sandbox-consuming tools and none applies to it. If it were not exempt, the cross-profile
hard-fail above would fire for every **served** (non-launch) profile's `message_agent`
coordination turn on a multiplexed gateway — the fleet could not coordinate on one
gateway at all. The exemption is a bare early return for `message_agent`; coordination
still flows only along provisioned edges, because the separate **WIRING** predicate
continues to gate `message_agent` to wired targets. `terminal`/`execute_code`/`mcp_*`/
file tools keep the full cross-profile + backend + dangerous-command checks.

## Companion surfaces

- **`codex_critique` tool** (+ `/codex-critique` slash alias) — runs a critique
  through the plugin LLM lane and records a session-correlated critique row that
  the critique gate consumes. Correlated by `session_id`; a critique recorded for
  one session never unlocks another. A file mutation after a critique **restales**
  it (freshness), re-requiring a critique before the next marked delivery.
- **`hermes wire add|remove|list`** — administers the fleet-shared wiring edges.
  `add`/`remove` are bidirectional; `add --gated` marks an edge so its traversal
  escalates to the human-approval gate with a stable `rule_key` of the form
  `wire:<from>:<to>`.

## Restricted-set integrity: load-time refusal + gate-time backstop

The plugin checks, against the running tool registry, that the core tools it
floors are present (accepting both the legacy and canonical spellings so the
check holds across a tool rename) and that no shell-capable/dangerous-named
registry tool is left un-gated. Because raising in `register()` disables the
plugin — which removes the veto and is therefore **fail open** — the check runs
in two places:

- **Load-time refusal (best-effort).** If the registry is essentially complete
  (missing at most one restricted-core group — the shape of a genuine
  rename/removal) and a core tool is absent or a dangerous tool is un-gated, the
  plugin refuses to load (`enabled: false`, `error` set). This is the loud,
  early signal for a tampered toolset.
- **Deferral on a still-initializing registry.** `register()` can run before
  core tool discovery has completed — `hermes plugins doctor` and `hermes kanban
  dispatch` see an empty registry (0/12), an in-process dashboard profile-switch
  reload sees 1/12. A registry missing that many tools is indistinguishable from
  a genuine multi-tool removal, so refusing to load would fail open. Instead the
  veto is registered anyway and the integrity check is deferred to the gate.
- **Gate-time backstop (authoritative, fail-closed).** Each `pre_tool_call`
  re-checks and stays **blocked** until the registry is complete; if restricted
  core is genuinely absent or a dangerous tool is un-gated, no gated call is ever
  approved. Once the set is clean the verdict is cached and later calls skip the
  re-scan.

`message_agent` is the one documented exclusion from the presence scan — it is
injected, not registry-registered, so it is matched by constant in the gate.

## See also

- [Fleet MCP scope](../../user-guide/fleet-mcp-scope.md) — how the compose plugin
  renders each role's `mcp_servers` and the profile-keyed `mcp_scope` the
  MCP-SCOPE predicate enforces.
