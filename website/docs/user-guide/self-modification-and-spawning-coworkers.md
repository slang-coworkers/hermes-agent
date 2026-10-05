---
title: Self-modification and spawning coworkers (coming from NanoClaw)
description: How NanoClaw's SELF-F54 — agent-spawned agents (create_agent) plus self-modification tier 1 (install_packages / add_mcp_server held for admin approval) — maps onto stock Hermes and the merged fleet carrier. Coworkers are profiles created by an orchestrator-only create_agent tool over create_profile; a runtime nv-fleet-gates veto decides who may spawn; protected-instruction and staged write-approval gates cover self-edits; package installation is contained by the per-profile sandbox rather than an approval-gated installer.
---

# Self-modification and spawning coworkers

This page is a **NanoClaw → Hermes mapping runbook**. It does not add behaviour;
it maps NanoClaw's SELF-F54 — *self-modification tier 1 and agent-spawned agents*
— onto features that stock Hermes plus the merged one-gateway fleet carrier
already ship, and points at the pages that document each one in depth. Citations
are given in dual form: `tag:` lines are `file:line` in the pinned release
(**v2026.8.31**, commit `29112bef`); `main:` lines resolve the same symbol against
`main` at commit `08b140d14` for readers tracking the moving branch. The two fleet
carrier plugins (`nv-coworker-compose`, `nv-fleet-gates`) are a **fork-baseline
artifact absent from the release tag**, so their seams are cited **symbolically by
function name** — their line numbers drift as sibling changes merge and a pinned
line would rot.

## 1. Coming from NanoClaw

NanoClaw's SELF-F54 bundles two things a self-improving agent wants:

- **Agent-spawned agents** — a `create_agent` tool that stands up a new coworker.
- **Self-modification tier 1** — `install_packages` and `add_mcp_server` MCP tools
  held for **admin approval**; on approve the handler updates the container config,
  rebuilds the image when needed, and kills/restarts the container.

Neither needs new Hermes code. In a one-gateway Bot-Mode fleet a coworker is a
**profile** of the shared gateway, so "spawn an agent" is "create a profile", and
Hermes already gates who may do that and how a running agent may edit itself. The
rest of this page is the map, criterion by criterion (`AC-SELF-F54-1` …
`AC-SELF-F54-5`, proved in `tests/plugins/test_self_f54_acceptance.py`).

## 2. Agent-spawned agents

Coworkers are **profiles** — the source the desktop Bots list is built from. The
stock primitive that creates one is `create_profile` (tag:
`hermes_cli/profiles.py:1177` | main: `hermes_cli/profiles.py:813`); the set of
profiles is enumerated by `list_profiles` (tag: `hermes_cli/profiles.py:1029` |
main: `hermes_cli/profiles.py:688`). Named profiles live under the default Hermes
home's `profiles/` directory, not inside the active profile's own home (tag:
`hermes_cli/profiles.py:282-304` | main: `hermes_cli/profiles.py:129-139`), so a
spawned coworker is a sibling of its creator, not a child nested inside it.

`create_profile` is a library function, not a tool the model can call. The
**agent-invocable** spawn surface is the orchestrator-only `create_agent` tool
(alongside `onboard_coworker` and `onboard_project`) that the `nv-coworker-compose`
plugin registers over `create_profile` with `ctx.register_tool(..., check_fn=…)`
(`ctx.register_tool` — tag: `hermes_cli/plugins.py:1778` | main:
`hermes_cli/plugins.py:449`; the plugin seam is
`plugins/nv-coworker-compose/__init__.py`, symbolic). The `check_fn` is the
orchestrator gate: it returns `True` only for the orchestrator profile, so
`create_agent` appears in the orchestrator's tool schema and is **omitted from a
worker profile's schema** — a worker never sees the tool it is not allowed to call
(**AC-SELF-F54-1**). When the orchestrator does call it, the handler dispatches a
single `profiles.create` gateway RPC carrying the requested profile fields
(**AC-SELF-F54-2**); the in-process dispatch seam is the plugin's `_dispatch_rpc`.

For the full plugin registration contract — `check_fn`, `register_tool`, manifest
fields — see [the plugin developer guide](../developer-guide/plugins/index.md);
for what a profile is and where it lives, [Profiles](./profiles.md) and
[Multi-profile gateways](./multi-profile-gateways.md).

## 3. Who may spawn — the runtime veto

Schema omission (`check_fn`) hides the tool from a worker's prompt, but it is
**necessary, not sufficient**: `registry.dispatch` does not re-check `check_fn`, so
a worker that names the tool anyway must still be refused at call time. That
authoritative refusal is the fleet's single `pre_tool_call` veto in the
`nv-fleet-gates` plugin (seam `plugins/nv-fleet-gates/__init__.py`, functions
`_gate` / `_fleet_admin_block`, symbolic). When a non-orchestrator profile calls
`create_agent` or `onboard_coworker`, the veto returns an `{"action": "block", …}`
directive; the orchestrator's identical call is not blocked (**AC-SELF-F54-3**).

This is the **SELF-F54-specific instance of the GOV-F26 fleet-admin mechanism**,
not a new gate — the same `pre_tool_call` veto that realises `cli_scope`, applied
to the spawn/onboard tool names. It is the runtime layer the LOOP-F35 carrier
deferred. The mechanism and why a profile cannot self-elevate are documented in
[Fleet-admin scope](./fleet-admin-scope.md).

## 4. Self-modification tier 1 needs no new safety surface

NanoClaw contains a self-improving agent by mounting its own source read-only.
Hermes's equivalent is **narrower in target but stronger in policy**: instead of a
blanket read-only mount it ships three surface-specific controls, two of which are
cross-platform and hermetically proved here.

- **Protected instruction files (default-ON, fail-closed).** A write through the
  file-write tool to a protected instruction basename — `SOUL.md`, `AGENTS.md`,
  `CLAUDE.md`, `.cursorrules` (matched case-insensitively via
  `_PROTECTED_INSTRUCTION_BASENAMES`, tag: `tools/file_tools.py:736`) — is held for
  approval, and with no approver present it is **blocked and the file is never
  written**. The public entry point is `write_file_tool` (tag:
  `tools/file_tools.py:2239`; registered as the `write_file` tool at
  `tools/file_tools.py:2900`); it routes through `_check_protected_instruction_write`
  (tag: `tools/file_tools.py:942`) → `_protected_instruction_reason` (tag:
  `tools/file_tools.py:790` | main: `tools/file_tools_write_guards.py:131`). The
  gate is on by default via the config key `security.protected_instruction_files`
  (default `True` — tag: `tools/file_tools.py:777` | main:
  `tools/file_tools_write_guards.py:120`); when no approval transport can resolve
  the hold, `_request_protected_instruction_approval` (tag:
  `tools/file_tools.py:843`) fails closed and the tool returns a `BLOCKED` error
  (tag: `tools/file_tools.py:937`). The `~/.hermes` tree itself is exempt (tag:
  `tools/file_tools.py:817`), so this gate protects **project-local** persona files,
  not a profile's own home. A write to a non-protected file in the same directory
  still succeeds — the block is the protected-file rule, not a catch-all
  (**AC-SELF-F54-4**).
- **Write approval (opt-in, OFF by default).** With `memory.write_approval` or
  `skills.write_approval` enabled, a self-edit is routed through the decision
  matrix `evaluate_gate` (tag: `tools/write_approval.py:253` | main:
  `tools/write_approval.py:170`) instead of being applied blindly, and the outcome
  depends on subsystem and origin. A **skill** edit always **stages** — a `SKILL.md`
  is too large to review inline. A **memory** edit from an **interactive foreground
  CLI** gets an inline approve/deny prompt: approve applies it, deny blocks it. A
  memory edit from the gateway, a script, a background thread, or with no approval
  listener present **stages** — held as a pending record under
  `get_hermes_home()/pending/memory/` by `stage_write` (tag:
  `tools/write_approval.py:114` | main: `tools/write_approval.py:73`) rather than
  forced to a blind deny. The enable check is `write_approval_enabled` (tag:
  `tools/write_approval.py:74` | main: `tools/write_approval.py:43`, default
  `False`); the memory path is driven from `memory_tool` (tag:
  `tools/memory_tool.py:1086`) through `_apply_write_gate` (tag:
  `tools/memory_tool.py:949`). It is **opt-in and off by default**: with the setting
  off the edit applies directly to the store. **AC-SELF-F54-5** proves both ends —
  the default-off apply path, and (with no approval callback registered) the
  non-interactive staged path. See [Security](./security.md) for the write-approval
  and protected-instruction settings.
- **Self-repo git guard.** `detect_self_repo_git_mutation` (tag:
  `tools/self_repo_guard.py:711` | main: `tools/self_repo_guard.py:445`) blocks a
  shell git operation that would rewrite the running checkout. Its enforcement is
  gated by `guard_active` (tag: `tools/self_repo_guard.py:698` | main:
  `tools/self_repo_guard.py:438`), which is **Windows-only** (`os.name == "nt"`,
  because NTFS locks loaded modules). The terminal tool invokes the guard only when
  `guard_active()` is true — the direct call is at tag: `tools/terminal_tool.py:3217`;
  on main the check was extracted to the helper `self_repo_block`, called from main:
  `tools/terminal_tool.py:1136` (which in turn runs `detect_self_repo_git_mutation`
  at `tools/terminal_tool_guards.py:265`). It is documented here for completeness
  but, being platform-gated, is not exercised on the Linux acceptance box.

## 5. Package installation — a deliberate difference, not a gap

NanoClaw approval-gates `install_packages` and rebuilds the image. Hermes does
**not**: there is no pip/apt/npm pattern in the dangerous-command set (only
credential-file writes at tag: `tools/approval.py:418` and pipe-to-shell at tag:
`tools/approval.py:1057`). Package execution is instead **contained by the
per-profile podman sandbox** (P4; ISO-F13/F14). Every file, shell, and
code-execution tool call of a profile runs inside that profile's sandbox, whose
backend documents exactly this: "the container itself is the security boundary —
the filesystem inside is writable so agents can install packages (pip, npm, apt)"
(`DockerEnvironment`, tag: `tools/environments/docker.py:883` | main:
`tools/environments/docker.py:484`), with bind-mount persistence across restarts.
So a sandbox-local install is a contained side effect, not a fleet-level event:
Hermes rebuilds no image and restarts no shared gateway for it. This is an
architectural difference, not a missing safety surface — if per-install approval
is ever wanted, the same `nv-fleet-gates` veto predicate can carry a pip/apt rule
(a future `SELF-F54.a` configure step, not this proof). See
[Multi-profile gateways](./multi-profile-gateways.md) for the per-profile sandbox
model.

## 6. Adding an MCP server

NanoClaw's `add_mcp_server` is approval-gated. In Hermes, adding an MCP server is a
**config write** picked up by the hot-reload watcher `_check_config_mcp_changes`
(tag: `cli.py:14396` | main: `hermes_cli/cli_info_mixin.py:789`) — GOV-F27
territory. A bot editing its own `mcp_servers` block buys nothing in this fleet:
the `nv-fleet-gates` veto denies `mcp_*` tools outside the profile's rendered
allow-list, and no profile reaches its own config from inside its sandbox. The
desktop inline-consent path is `setup_mcp` (tag: `tools/setup_mcp_tool.py:25`,
registered `name="setup_mcp"` at `tools/setup_mcp_tool.py:107` | main:
`tools/setup_mcp_tool.py:20`); non-desktop use falls back to `hermes mcp install`.
The scoping guarantee is cross-referenced to GOV-F27 and is not proved on this
page. See [MCP servers](./features/mcp.md).

## 7. Gateway lifecycle commands are approval-classified

For completeness: `hermes … gateway (stop|restart)` is classified dangerous by
`detect_dangerous_command` (tag: `tools/approval.py:2541` | main:
`tools/approval_detection.py:1208`) via the pattern at tag: `tools/approval.py:1094`
| main: `tools/approval_detection.py:306` — the pattern tolerates an intervening
`-p <profile>` flag so it cannot be slipped past. This is an operational-command
gate, not one of the three tier-1 self-modification safety surfaces; it is noted
here only so the mapping is complete.

## 8. See also / Acceptance

- Acceptance proof: `tests/plugins/test_self_f54_acceptance.py` re-proves
  `AC-SELF-F54-1` … `AC-SELF-F54-5` against the merged fleet baseline; the doc
  contract is `tests/plugins/test_self_f54_doc_contract.py`.
- [Profiles](./profiles.md) · [Profile distributions](./profile-distributions.md)
  · [Multi-profile gateways](./multi-profile-gateways.md) ·
  [Security](./security.md) · [MCP servers](./features/mcp.md) ·
  [Fleet-admin scope](./fleet-admin-scope.md).
- Plugin contract: [the plugin developer guide](../developer-guide/plugins/index.md)
  and the [nv-fleet-gates plugin reference](../developer-guide/plugins/nv-fleet-gates.md).
