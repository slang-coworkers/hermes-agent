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
plan+spec) → builder applies the change in its sandbox and replies `P6-BUILT:<nonce>`,
forwarded to tester AND relayed back up to the orchestrator via the architect.

The live provider block is the fixed OneCLI tier: `base_url
https://inference-api.nvidia.com`, `api_mode anthropic_messages`, model
`aws/anthropic/bedrock-claude-opus-4-8`, `key_env ANTHROPIC_API_KEY` (dummy value in
the fixture — the proxy injects the real key). Bounded `LIVE_MODEL_CALLS_MAX=40` /
`LIVE_BUDGET_USD=5`.

All state.db oracles below run via the stdlib `python3 -c "import sqlite3 …"` form (the
image has no `sqlite3` CLI); the SQL shown is the query to execute against the named
profile's `$HERMES_HOME/profiles/<profile>/state.db`.

## Setup (after the fixtures are installed)

- ONE gateway boots on the DEFAULT home serving default + the five (`systemctl --user
  start` or the testbed's gateway launch); confirm ONE `/api/health`, no second port.
- The five OneCLI identities have a container-config and are ALL granted the inference
  secret BEFORE any live model call (§ Deployment item 2 ordering — AC-CRED-F28-2 ran
  its ungranted checks FIRST, then all five were granted; probe
  `GET /v1/container-config?agent=<id>`), else the pipeline 401s. The DASHBOARD SPA opens
  the DEFAULT profile's chat regardless of `-p`, so any step that talks to a named
  coworker first selects it in the profile combobox.
- **DRIVE vs OBSERVE (load-bearing).** `message_agent` requires the SENDER's own session
  title to equal `Bot Chat` EXACTLY (`tools/bot_mode_dm.py:152`,
  `agent/turn_context.py:867-874`); a numbered `Bot Chat #N` fails the gate — the round-6
  harness defect. So every DRIVE (the pre-step probe, the orchestrator kickoff, the one
  re-drive) resolves the unique `sessions.title='Bot Chat'` row for that profile and
  RESUMES that stored session over `ws://127.0.0.1:<port>/api/ws`; NEVER drive
  `Bot Chat #N`, `session.create`, dashboard new-chat, or `hermes -z` (those mint a
  fresh/numbered session that fails the gate). `Bot Chat #N` is OBSERVATION-ONLY — it may
  appear only in a state.db session predicate, never as a drive target.
- **Live-call budget + hard stop.** `LIVE_MODEL_CALLS_MAX=40` / `LIVE_BUDGET_USD=5`.
  Record the `session_model_usage` call counter (summed across the profiles the scenario
  drives) BEFORE and AFTER the probe, the cascade, and the re-drive; HARD-STOP before 40.
  The re-drive is ONE GLOBAL allowance — consumed by EITHER the pre-step (inconclusive
  harness) OR the cascade (return relay not observed first pass), never both. Canonical-
  only driving keeps each drive at the ~16-call baseline: probe (~2-4) + one cascade
  (~16) + one global re-drive (~16) ≈ 36.

## Provisioning (onboarding + wiring — completes before the pre-step, so wiring is intact)

1. `hermes onboard coworker <spec> --gateway-url ws://127.0.0.1:<port>/api/ws?token=…`
   (spec = `spec/podman/coworker-types.yaml`) → expect: `ui_meta['hermes-bots']` for all
   five, five canonical Bot Chats (each `sessions.title='Bot Chat'`), fleet-room(5) +
   review-room(builder,tester,reviewer).
2. `hermes wire add` the five edges: orchestrator↔architect, architect↔builder,
   builder↔tester, tester↔reviewer, reviewer↔orchestrator → expect: `hermes wire list`
   shows exactly those five, INCLUDING builder↔architect (the return edge the pre-step
   probes).

## Pre-step (delivery discriminator — MUST land before the cascade)

Isolates the builder→architect `message_agent` delivery (the round-6 AC-7b gap, ruled
FAIL(env), 65c0df50). It gates the current run; it does not by itself re-prove the
historical cause.

1. Baseline: in the ARCHITECT state.db,
   `ARCH_PROBE_BASELINE = SELECT COALESCE(MAX(id), 0) FROM messages` BEFORE the probe send
   (COALESCE so an empty db yields 0, not NULL — `id > NULL` never matches). Baselines are
   database-LOCAL; never reuse one across profiles' state.dbs.
2. Resolve the BUILDER's unique `sessions.title='Bot Chat'` session and RESUME it over
   `ws://127.0.0.1:<port>/api/ws`; submit a prompt that makes the builder emit exactly one
   `message_agent(target="architect", message="P6-BUILT:<probe-nonce> — delivery probe")`
   → expect: a builder assistant row carrying the `message_agent(target="architect")`
   tool_call AND its tool-result row `{"status":"sent","to":"@architect"}`. Record BOTH
   (they decide the not-land classification below).
3. Poll the ARCHITECT state.db (bounded wait ≤ the pre-step budget) with the STRICT
   current-run oracle, session predicate widened ONLY:
   `SELECT m.id FROM messages m JOIN sessions s ON s.id=m.session_id
    WHERE m.id > :ARCH_PROBE_BASELINE AND m.role='user'
      AND (s.title='Bot Chat' OR s.title GLOB 'Bot Chat #*')
      AND m.content GLOB 'Message from 🤖 builder (@builder): P6-BUILT:<probe-nonce>*'`
   - **LANDS ⇒ probe passes: current builder→architect delivery works → PROCEED to
     `## Steps`.**
   - **DOES NOT LAND — classify by the builder side:**
     - builder EMITTED the `message_agent(target="architect")` call AND got
       `{"status":"sent","to":"@architect"}`, yet no architect row landed ⇒ **PRODUCT
       delivery defect** (intermittent fan-out race). STOP the counted run; capture the
       builder tool-result (status:sent), the strict SQL returning NONE over the widened
       scope, `hermes wire list`, and the live-call counter; escalate UP to hermes-architect
       for the PRODUCT decision. Do NOT run `## Steps`; do NOT patch `plugins/**`.
     - builder NEVER emitted the call / never received a `status:sent` result ⇒
       **inconclusive harness/model failure** (NOT a product defect): spend the ONE global
       re-drive (resume the EXACT `Bot Chat`), still within the call bound; if it still does
       not emit, STOP and escalate UP as `inconclusive (harness/model)`. Do NOT classify
       PRODUCT; do NOT patch `plugins/**`.

## Steps (the cascade — drive EXACT `Bot Chat` sessions only)

0. IMMEDIATELY before the cascade, capture two db-LOCAL baselines (COALESCE, own db each):
   `BUILDER_CASCADE_BASELINE = SELECT COALESCE(MAX(id),0) FROM messages` in the BUILDER
   state.db, and `ORCH_CASCADE_BASELINE = SELECT COALESCE(MAX(id),0) FROM messages` in the
   ORCHESTRATOR state.db. Never reuse `ARCH_PROBE_BASELINE` (a different db).
1. Human → orchestrator (RESUME the orchestrator's EXACT `Bot Chat`) with change
   `P6-<nonce>` → orchestrator → architect → architect's `hermes -p architect chat`
   delivery child writes plan+spec into the scratch mount (sandboxed, uid_map echoed) →
   builder.
2. Builder applies the change in ITS sandbox (uid_map echoed) → replies `P6-BUILT:<nonce>`
   forward to tester AND back up to architect, which relays it to the orchestrator. The
   SOLE global re-drive (only if the return relay is not observed first pass AND it was NOT
   already spent in the pre-step) also RESUMES the EXACT `Bot Chat` sessions — never a
   fresh/numbered one.

## Pass

The frozen criterion is an AND — assert BOTH, each with its own db-local current-run
baseline, only the session predicate widened to the Bot Chat lineage:

- **AC-7a (builder emission):** in the BUILDER state.db, a row with
  `role='assistant' AND id > :BUILDER_CASCADE_BASELINE AND instr(tool_calls,'P6-BUILT:<nonce>')>0`
  (the builder's own P6-BUILT emission — the shape the round-6 oracle proved at builder id=4).
- **AC-7b (return relay to the orchestrator via the architect):** in the ORCHESTRATOR
  state.db,
  `SELECT m.id FROM messages m JOIN sessions s ON s.id=m.session_id
   WHERE m.id > :ORCH_CASCADE_BASELINE AND m.role='user'
     AND (s.title='Bot Chat' OR s.title GLOB 'Bot Chat #*')
     AND m.content GLOB 'Message from 🤖 architect (@architect): *P6-BUILT:<nonce>*'`
  — the return relay arrives FROM the architect (exact `Message from 🤖 architect
  (@architect): ` sender envelope), nonce-bound, direction `role='user'`, current-run
  `m.id > :ORCH_CASCADE_BASELINE`.
- Every tool-bearing turn's evidence shows uid_map `0 1001 1`.

## Evidence

`scenario-AC-FLEET-F62-7/step-1..n.png` (a UI screenshot of the orchestrator Bot Chat
showing the returned marker) + `evidence.txt` + the state.db query outputs (probe: the
architect `ARCH_PROBE_BASELINE` row; cascade: the builder AC-7a row + the orchestrator
AC-7b row) captured via the stdlib `python3 -c "import sqlite3 …"` form + the live-call-
counter checkpoints (before/after the probe, the cascade, and the single global re-drive;
each < 40). On a not-land classification, also `hermes wire list` + the builder tool-result.
