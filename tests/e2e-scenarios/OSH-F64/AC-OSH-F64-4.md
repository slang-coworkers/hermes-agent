---
ac: AC-OSH-F64-4
kind: live
model: live
base_url: inference-api.nvidia.com
api_mode: anthropic_messages
model_id: aws/anthropic/bedrock-claude-opus-4-8
key_env: ANTHROPIC_API_KEY
fixtures:
  - fixtures/osh-f64-gateway
  - fixtures/osh-f64-worker-orchestrator
  - fixtures/osh-f64-worker-builder
LIVE_MODEL_CALLS_MAX: 40
LIVE_BUDGET_USD: 5
LIVE_ROUND_CALLS_MAX: 120
LIVE_ROUND_BUDGET_USD: 15
timeout_s: 600
---

# AC-OSH-F64-4 — two-bot delivery over Bot Chat in one gateway; fleet-admin veto (live)

On the same throwaway fleet, human → orchestrator Bot Chat (`message_agent` only) runs
change `P7-<nonce>`; orchestrator → builder is refused when unwired and succeeds once
`hermes wire add orchestrator builder`; the builder's delivery turn applies the change
inside ITS worker sandbox and replies `P7-BUILT:<nonce>`; a worker profile's
`cronjob_manage` is denied by the fleet-admin veto (`orchestrator-only`); one gateway, one
port, no a2a; round total under `LIVE_ROUND_CALLS_MAX=120` / `LIVE_ROUND_BUDGET_USD=15`.
The per-scenario cap is `LIVE_MODEL_CALLS_MAX=40` / `LIVE_BUDGET_USD=5`; the round-wide cap
is enforced by the tester across the round's scenarios.

**Process boundary + independence** (as AC-3): Bot Chat is coordination only; every
terminal-bearing action runs in that role's OWN profile-scoped child (FLEET-F62 mechanism,
OSH-F63 §D2). This scenario provisions and tears down its OWN throwaway `osh-f64-gw` fleet
(same shape as AC-3's; it does NOT depend on AC-3 having left a fleet standing).

## Setup
Provision `osh-f64-gw` (`openshell sandbox create --name osh-f64-gw --from
osh-f64-gateway:pinned --policy /workspace/extra/hermes-fleet-testbed/osh-f64/policy-gateway.yaml`,
the pinned GATEWAY image, §D1 — built from base `nemoclaw-hermes-sandbox:local` + fork core
@95de5f78; the `orchestrator`/`builder` worker sandboxes create
`--from localhost/hermes-openshell-sandbox:pinned`). BEFORE the installer, in this ORDER — steps 1,
2a, and 3 run INSIDE `osh-f64-gw`; the render + host-mirror in 2b–2c are HOST-lane commands
(`openshell sandbox exec`, host `mkdir`, host redirection) that reach into the sandbox:
1. Seed the managed dir with the §D6 scan guard: `export HERMES_MANAGED_DIR=/sandbox/.hermes/managed
   && mkdir -p $HERMES_MANAGED_DIR && cp /tmp/osh-lane/managed/config.yaml $HERMES_MANAGED_DIR/` (the
   pre-seeded managed carries `plugins.scan_on_install: false`).
2. Make the per-worker policies HOST-readable for the broker (LANE v4, §D3 `--policy-root`):
   (a) run the staged `installer.py phase-a --spec <spec> --ref <sha>` so `hermes coworker` exists;
   (b) render gateway-internally WITHOUT provisioning —
   `openshell sandbox exec osh-f64-gw -- sh -lc 'hermes coworker compose <spec> --out "$HERMES_HOME/.osh-f64/render"'`;
   (c) mirror each rendered policy to the host testbed root via a QUOTED remote shell (so `$HERMES_HOME`
   expands INSIDE the sandbox, not on the host) — `mkdir -p /workspace/extra/hermes-fleet-testbed/osh-f64/render/<role>`
   then `openshell sandbox exec osh-f64-gw -- sh -lc 'cat "$HERMES_HOME/.osh-f64/render/<role>/policy-<role>.yaml"' > /workspace/extra/hermes-fleet-testbed/osh-f64/render/<role>/policy-<role>.yaml`
   for each of the five roles (orchestrator, architect, builder, tester, reviewer) the installer
   provisions from the full `types:` roster (the scenario then drives orchestrator + builder).
3. Bring the gateway up for Phase B: `export HERMES_DASHBOARD_SESSION_TOKEN=<token>` BEFORE starting
   `hermes serve` (web_server.py:589 — a bare `hermes serve` surfaces no token and the WS `/api/ws`
   upgrade still validates `?token` despite `auth_required:false`), start the sandbox's `hermes serve`
   messaging gateway, wait for readiness, and capture
   `$GW_URL=ws://127.0.0.1:<port>/api/ws?token=$HERMES_DASHBOARD_SESSION_TOKEN` (Phase B's live-WS
   preflight and room creation run against it, so the gateway MUST be up before the full install).

Then run `install-into-sandbox.sh <spec> --ref <sha> --gateway-url "$GW_URL" --policy-root
/workspace/extra/hermes-fleet-testbed/osh-f64` (Phase A idempotently re-skips the staged phase-a;
Phase B composes + provisions `orchestrator` + `builder` via `openshell sandbox create --policy
<root>/render/<role>/policy-<role>.yaml` + creates rooms against the live gateway + restarts) so both
are served with their worker sandboxes Ready; both have a live OneCLI agent; the fleet-admin veto
(`nv-fleet-gates`, `enforce_sandbox: true`, orchestrator-only admin) is live from the composed spec.
No fixture is re-installed here.

**Grant preflight (§D7.1):** confirm the `orchestrator` and `builder` `HERMES_HOME` basenames each
match a OneCLI identity GRANTED the inference secret. The openshell render populates the
default/gateway `profile_secret_sets[<role>]` = `["Anthropic-Dev"]` (from `egress.onecli_secret_ids`),
which the deploy's `onecli-onboard` reads to grant each identity. Do NOT run `onecli-onboard`
against a `profile_secret_sets` entry left `[]` — `set_secrets([])` REVOKES that identity's grant →
the served turn 401s. If (re)onboarding is needed, run `hermes -p default onecli-onboard --profile
<role>` from the gateway root (a bare `hermes onecli-onboard --profile <role>` is stripped by
hermes-main → wrong context + missing required flag). With the grant present the turns expect a
200; a 401 is a fault to diagnose (grant / identity / token-bearing route / CA), not tolerated.

**Post-install onboarding (Option 2 — architect ruling 2026-09-29; the documented post-install
operator step, NOT an installer change).** After the install brings up the gateway and BEFORE
step 1, run the fleet's onboard step from the DEFAULT gateway context — `hermes -p default
onboard coworker <spec> --gateway-url "$GW_URL"` from the gateway root after readiness (the
`nv-coworker-compose` onboard subaction — `__init__.py:495` `_ensure_canonical_bot_chat` / `:695`
`_configure_bot_meta` / `:734-799`). Run it as `-p default`, NOT profile-scoped (a profile-scoped
invocation would re-render SSH-key paths relative to the wrong `HERMES_HOME`). This onboard
force-re-renders all served distributions, so ASSERT exit 0 / `ok: true`, that `orchestrator`
and `builder` each gain a canonical "Bot Chat" session AND a `ui_meta['hermes-bots']` roster
entry, AND that the post-onboard per-role config is UNCHANGED (SSH host/key, OneCLI chain-dial,
`plugins.disabled` firecrawl, enabled plugins) — the onboard must NOT silently re-render away the
install's config. `message_agent`'s tool schema is injected ONLY into a canonical Bot Chat
(`/workspace/extra/hermes-release/tools/bot_mode_dm.py:18-27`, gate `:150-159`); the installer's
rooms (`groups.create`, titled "Group: …", EXCLUDED from the Bot-Chat gate `:23`) + wires do NOT
create it. Onboarding adds no AC-1 installer step — it is scenario-Setup, not the installer;
§D3/AC-1 stay UNCHANGED (the §D8 firecrawl default-diff is the separate AC-1 change).

**Gateway-restart capture (§D8):** retain the install's `hermes gateway restart` exit code + full
traceback as `scenario-AC-OSH-F64-4/gateway-restart.log` and confirm the gateway is usable —
EXPECTED: restart exit 0 + a served live 200; if restart exits 1, the route-(3) fresh-start
`hermes serve` workaround must be PROVED on the same image (restart exit 1 + `hermes serve` exit 0
+ served live 200) before the live criterion passes; firecrawl is disabled in the render +
installer default (§D8 route 1), so the misleading firecrawl `UnscopedSecretError` traceback
should be absent.

## Steps
1. Human → orchestrator Bot Chat: "run change P7-<nonce>" (`message_agent` in the one
   gateway; NO a2a) → expect: orchestrator accepts (coordination turn; no terminal yet).
2. Orchestrator attempts `message_agent` → builder BEFORE any wire → expect: refused by
   the wiring gate (unwired send blocked).
3. `hermes wire add orchestrator builder`, then orchestrator → builder again → expect:
   delivered; builder receives the task.
4. The builder's delivery turn runs in the BUILDER's own profile-scoped child and FIRST
   issues a `write_file` tool call to `.hermes/plans/builder.md` (satisfies + records the
   `nv-fleet-gates` plan-first gate — a `terminal` `>` is NOT exempt, §Scenario AC-3), THEN
   issues the change as a `terminal` call inside ITS worker sandbox (`osh-f64-builder`), then
   replies `P7-BUILT:<nonce>` visible in the room → expect: the reply carries the exact nonce
   and the change artifact was written in the builder's sandbox (`hostname`/`id -u` differ
   from the gateway).
5. A worker profile (e.g. builder) attempts `cronjob_manage` → expect: denied by the
   fleet-admin veto (`orchestrator-only`).
6. Teardown: delete every `osh-f64-*` sandbox and policy.

## Pass
The two-bot delivery completes over Bot Chat in ONE gateway/one port with no a2a, the
unwired→wired transition gates as specified, the builder's terminal work ran in its own
sandbox (via its profile-scoped child), `cronjob_manage` is orchestrator-only, the scenario
provisioned+tore down its own fleet, and the round stays within the round-wide 120 calls /
$15 cap.

## Evidence
`scenario-AC-OSH-F64-4/step-1..5.png` (room transcript screenshots) +
`scenario-AC-OSH-F64-4/evidence.txt` (the onboard confirmation that `orchestrator` + `builder`
each hold a canonical Bot Chat post-onboard, the `P7-BUILT:<nonce>` line, the unwired-refusal and
post-wire-success lines, the `cronjob_manage` denial, the one-gateway/one-port check, the call/$
totals) + `scenario-AC-OSH-F64-4/gateway-restart.log` (the retained `hermes gateway restart` exit
code + traceback, §D8).
