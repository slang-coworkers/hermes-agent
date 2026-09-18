---
ac: AC-FLEET-F62-7
kind: live
model: live
base_url: https://inference-api.nvidia.com
model_id: aws/anthropic/bedrock-claude-opus-4-8
fixtures:
  - fixtures/podman/fleet-f62-default
  - fixtures/podman/fleet-f62-orchestrator
  - fixtures/podman/fleet-f62-architect
  - fixtures/podman/fleet-f62-builder
  - fixtures/podman/fleet-f62-tester
  - fixtures/podman/fleet-f62-reviewer
timeout_s: 600
---

# AC-FLEET-F62-7 — one gateway serves five; Half A of the real workflow (live)

The ONE gateway boots on the DEFAULT profile serving the five (no second serve, no
a2a). Onboarding writes each coworker's `ui_meta['hermes-bots']`, materialises five
canonical Bot Chats, and creates fleet-room(5) + review-room(3); `hermes wire add`
provisions exactly the five review-pipeline edges. Half A of the workflow then runs:
human → orchestrator → architect → (architect's sandboxed delivery child writes the
plan+spec) → builder applies the change in its sandbox and replies `P6-BUILT:<nonce>`.

The live provider block is the fixed OneCLI tier: `base_url
https://inference-api.nvidia.com`, `api_mode anthropic_messages`, model
`aws/anthropic/bedrock-claude-opus-4-8`, `key_env ANTHROPIC_API_KEY` (dummy value in
the fixture — the proxy injects the real key). Bounded `LIVE_MODEL_CALLS_MAX=40` /
`LIVE_BUDGET_USD=5`.

## Setup (after the fixtures are installed)

- ONE gateway boots on the DEFAULT home serving default + the five (`systemctl --user
  start` or the testbed's gateway launch); confirm ONE `/api/health`, no second port.
- The five OneCLI identities have a container-config and are ALL granted the inference
  secret BEFORE any live model call (§ Deployment item 2 ordering — AC-CRED-F28-2 ran
  its ungranted checks FIRST, then all five were granted; probe
  `GET /v1/container-config?agent=<id>`), else the pipeline 401s. The DASHBOARD SPA opens
  the DEFAULT profile's chat regardless of `-p`, so any step that talks to a named
  coworker first selects it in the profile combobox.
- `LIVE_MODEL_CALLS_MAX=40` / `LIVE_BUDGET_USD=5`.

## Steps

1. `hermes onboard coworker <spec> --gateway-url ws://127.0.0.1:<port>/api/ws?token=…`
   (spec = `spec/podman/coworker-types.yaml`) → expect: `ui_meta['hermes-bots']` for
   all five, five canonical Bot Chats, fleet-room(5) + review-room(builder,tester,
   reviewer).
2. `hermes wire add` the five edges: orchestrator↔architect, architect↔builder,
   builder↔tester, tester↔reviewer, reviewer↔orchestrator → expect: `hermes wire list`
   shows exactly those five.
3. Human → orchestrator Bot Chat (`message_agent`) with change `P6-<nonce>` →
   orchestrator → architect → architect's `hermes -p architect chat` delivery child
   writes plan+spec into the scratch mount (sandboxed, uid_map echoed) → builder.
4. Builder applies the change in ITS sandbox (uid_map echoed) → replies
   `P6-BUILT:<nonce>` forward to tester and back up to architect.

## Pass

`P6-BUILT:<nonce>` is an assistant row in builder `state.db` AND reaches the
orchestrator's Bot Chat via the architect return relay (the O↔A edge); every
tool-bearing turn's evidence shows `0 1001 1`.

## Evidence

`scenario-AC-FLEET-F62-7/step-1..n.png` (a UI screenshot of the orchestrator Bot Chat
showing the returned marker) + `evidence.txt` + the `state.db` row query.
