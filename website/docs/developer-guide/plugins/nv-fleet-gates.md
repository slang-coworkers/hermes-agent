# nv-fleet-gates — the fleet's single pre_tool_call veto

`nv-fleet-gates` is a standalone directory plugin that installs **exactly one**
`pre_tool_call` callback for the whole fleet. That callback runs one ordered,
**block-before-approve** predicate chain and returns the first matching directive:

```
canonicalise(tool_name)
  → SANDBOX          (per-session sandbox backend; inert unless enforce_sandbox)
  → FLEET-ADMIN      (admin-shaped tools are orchestrator-only)
  → WIRING (unwired) (peer messaging is refused unless a wiring edge exists)
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
| `enforce_sandbox` | when true, the SANDBOX predicate checks the resolved backend, denies stdio `mcp_*` calls, brings up the container task env, and re-asserts the dangerous-command floor. Off by default. |
| `expected_backend` | the container backend a profile's tool calls must resolve to when `enforce_sandbox` is true. |
| `plan_gate` | when true, `write_file`/`patch`/mutating `terminal` are refused until the session has itself created a plan under `.hermes/plans/*.md`. |
| `required_stages` | the critique stages a session must have recorded (fresh) before a marked delivery or a `gh pr` egress. |
| `stage_markers` | the delivery-marker prefixes (`[Fix Report]`, `[Review Verdict]`, …) that make a `message_agent` a gated delivery. |
| `admin_tools` | the admin-shaped tool names that are orchestrator-only (unioned with a vendored default). |

The caller's identity is resolved from `HERMES_HOME` (the profile the process
runs in), never from a plugin-writable setting, so a worker cannot forge it.

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
