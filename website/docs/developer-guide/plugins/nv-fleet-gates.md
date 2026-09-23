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

## Wired-only agent messaging (A2A-F18)

**Ported behaviour.** In NanoClaw the operator's destinations list is at once the
**routing map** (which agent may reach which) and the **access-control list** (ACL):
an un-wired pair cannot exchange a message at all, and a *gated* wiring puts a human in
the loop before a message crosses that edge. Hermes reaches the same "destinations =
routing map + ACL" property in three layers; `nv-fleet-gates` supplies the middle,
per-pair layer, gating the stock `message_agent_tool` entry point through the single
`pre_tool_call` veto.

### The three layers

- **Base layer — stock coarse ACL (already in Hermes).** `message_agent` validates its
  `target` against the live roster / registered peers before any delivery and rejects an
  unknown target. This is an *any-roster-member → any-roster-member* check, not per-pair.
  Entry point `message_agent_tool`, documented at `website/docs/user-guide/bot-mode.md`
  ("The tool validates the target against the live roster"). Stock primitives are cited in
  `tag:`/`main:` form — the pinned release tag `v2026.8.31` is the citation of record; the
  `main:` anchors are pinned at upstream `main @08b140d14` (which has since moved past
  them, so they are not "current main" line numbers):
  - tag: `tools/bot_mode_dm.py:241` (message_agent_tool) | main: `tools/bot_mode_dm.py:174`
  - tag: `tools/bot_mode_dm.py:187` (_local_roster, the destinations map) | main: `tools/bot_mode_probe.py:71` (_roster)
- **Wired-only layer — this plugin (the A2A-F18 deliverable).** The WIRING predicate in the
  single `pre_tool_call` veto refuses a `message_agent` (or `kanban_create`) target that has
  **no edge** from the caller in the fleet-shared edges store, and lists the caller's wired
  teammates so the model can recover — `_wiring_block` returns a block whose message reads
  `wired-only: <from> is not wired to <to>; wired teammates: [...]` (an **unwired** / **not
  wired** pair is refused outright). A wired **ungated** edge is permitted with no directive.
  A wired **gated** edge escalates to Hermes' own **human approval** gate: `_wiring_approve`
  returns an `approve` directive carrying a stable `rule_key` of the form `wire:<from>:<to>`
  and the message `gated edge <from>-><to> requires human approval`. The edges live in a
  fleet-shared SQLite store at the managed-scope `edges_db_path` setting (outside any profile
  home), keyed `PRIMARY KEY (from_profile, to_profile)`. Because the covering surface is on
  the fork baseline `release/v2026.8.31-e2e-fixed` (merged by LOOP-F37 / PR #5, not present in
  the pinned release tree), its anchors are baseline-only:
  - baseline: `plugins/nv-fleet-gates/__init__.py:331` (_wiring_block, unwired → block)
  - baseline: `plugins/nv-fleet-gates/__init__.py:341` (_wiring_approve, gated → approve)
  - baseline: `plugins/nv-fleet-gates/__init__.py:604` (the `hermes wire` CLI registration)
  - baseline: `plugins/nv-fleet-gates/edges.py:36` (add_edge, bidirectional + idempotent)
- **Cross-gateway layer — complementary.** For peering *across* gateways the a2a platform's
  `is_trusted_peer` gates by authenticated identity / `A2A_TRUSTED_PEERS`
  (tag: `plugins/platforms/a2a/security.py:155`). It is not part of this row's proof; named
  here so the runbook maps the whole "who can reach whom" surface.

### Administering edges — `hermes wire`

Edges are provisioned with the CLI; `add`/`remove` are **bidirectional and idempotent**
(re-adding an edge is a no-op or flips its gated flag):

```
hermes wire add bot-a bot-b            # permit bot-a <-> bot-b (ungated)
hermes wire add bot-a bot-c --gated    # permit, but escalate every traversal to a human
hermes wire remove bot-a bot-b         # clear both directions
hermes wire list                       # list all edges and their gated flag
```

### Outcomes at the gate

| edge state (caller `<from>` → target `<to>`) | `message_agent(target=<to>)` outcome |
|---|---|
| no edge (unwired) | **block** — `wired-only: <from> is not wired to <to>; wired teammates: [...]` |
| wired, ungated | permitted (no directive) |
| wired, `--gated` | **approve** — escalates to human approval; `rule_key="wire:<from>:<to>"`; message `gated edge <from>-><to> requires human approval` |

**Worked example.** After `hermes wire add bot-a bot-c --gated`, a
`message_agent(target="bot-c")` from `bot-a` returns
`{"action": "approve", "rule_key": "wire:bot-a:bot-c", ...}`; Hermes' human approval gate
then holds the send until the operator approves. The ungated `bot-a <-> bot-b` edge is
delivered without escalation, and an entirely unwired target is blocked. Block always
precedes approve in the predicate chain, so a still-un-critiqued marked delivery on a gated
edge is blocked, not approved.

## See also

- [Fleet MCP scope](../../user-guide/fleet-mcp-scope.md) — how the compose plugin
  renders each role's `mcp_servers` and the profile-keyed `mcp_scope` the
  MCP-SCOPE predicate enforces.
