---
ac: AC-OSH-F64.b-6
kind: live
model: live
base_url: https://inference-api.nvidia.com/v1
api_mode: chat_completions
model_id: aws/anthropic/bedrock-claude-opus-5-5
api_key: openshell:resolve:env:NV_INFERENCE_KEY
credential_provider: f64e-inference
spec: spec/coworker-types.yaml
fixtures:
  - fixtures/osh-f64e-gateway
  - fixtures/osh-f64e-worker-orchestrator
  - fixtures/osh-f64e-worker-architect
LIVE_MODEL_CALLS_MAX: 40
LIVE_BUDGET_USD: 5
timeout_s: 600
carried_from: tests/e2e-scenarios/OSH-F64.b/AC-OSH-F64.b-6.md
---

# AC-OSH-F64.b-6 (carried, step 5 OpenShell-log half only — UA-37) — per-profile attribution in the OpenShell logs

Carried from OSH-F64.b: ONLY the OpenShell-log half of its step 5. The other steps (and the D5
`session_model_usage` attribution of record) stayed PASS on OSH-F64.b and are not re-run. This file
is the sole source of the OSH-F64.e result row for this id; the frozen
`tests/e2e-scenarios/OSH-F64.b/AC-OSH-F64.b-6.md` is OSH-F64.b's record and is not re-run.

Two served profiles' turns resolve to two DISTINCT CORRECT-PROFILE records in the OpenShell/inference
logs (A→A, B→B) via the per-request `X-Hermes-Profile` header or an operator-confirmed equivalent; if
no per-profile-attributable non-secret field is log-visible for the shared-credential gateway, the row
is recorded `CARRIED — UA-37` per the operator's G2 ruling (option 1, erratum E2): non-blocking, not
BLOCKED, not FAIL, and no merge hold. D5 `session_model_usage` stays the attribution of record. This is
not closable plugin-only on OpenShell v0.0.72 (ADR §Carried criteria); it runs as a diagnostic so the
row is evidence-backed, and `CARRIED — UA-37` is the expected outcome on v0.0.72. Report it as written;
do not rule it.

Profiles that must exist before step 1: A = `architect`, B = `orchestrator`, both served by the one
gateway (direct mode, so each turn has its own `L7_REQUEST` and `CONNECT_L7` record).

## Setup
Shared by all three OSH-F64.e scenarios (one lane, run once, SERIALISED with OSH-F64.c's live tier;
never concurrent). Live tier only after LANE READY.

**Lane preconditions (operator, before step 1):** the image `osh-f64-gateway:f64e-<sha7>` built at the
PR head (never `:pinned`), with its broker `--image` entry, and its LANE v8 `.fork-sha` equal to the PR
head; OpenShell provider `f64e-inference` (type `generic`, credential key
`NV_INFERENCE_KEY`; the user placed the value) created BEFORE the gateway create;
`providers_v2_enabled` unset. Sandboxes (prefix `osh-f64b-`, admitted): `osh-f64b-gw`,
`osh-f64b-orchestrator`, `osh-f64b-architect` — 3 in total, shared by the three scenarios.

1. Render the direct spec: `hermes coworker compose tests/e2e-scenarios/OSH-F64.e/spec/coworker-types.yaml
   --out <render> --provision-dry-run`, with `gateway_image` set to the lane tag. Record the plan.
2. Create `osh-f64b-gw` with EXACTLY the plan's gateway line (the AC-OSH-F64.e-4 shape):
   `openshell sandbox create --name osh-f64b-gw --from osh-f64-gateway:f64e-<sha7> --policy
   <render>/default/policy-gateway.yaml --provider f64e-inference`.
3. Inside it run `install-into-sandbox.sh tests/e2e-scenarios/OSH-F64.e/spec/coworker-types.yaml --ref
   <head>` (lane-driving rules: per-call `sx` preamble, single-line commands, every long start
   `setsid nohup`-backgrounded, poll slices ≤ 240 s).
4. Capture `openshell policy get osh-f64b-gw --full` to `effective-policy.yaml`. Its
   `inference-api.nvidia.com:443` endpoint must carry exactly the four rendered rules (POST
   `/v1/chat/completions`, POST `/v1/messages`, POST `/v1/responses`, GET `/v1/models`); an extra
   `_provider_*` rule there is a Setup FAIL, not a criterion result.

RUN with `HTTPS_PROXY=10.200.0.1:3128` set (the real launch env). Never write a credential value into any
file, log or evidence; read env and config names-only (`python3 -c`; the image has no `sqlite3` CLI).
Spend: e-7 ~2 model calls, b-7 ~4 requests, b-6 ~2 model calls — about 8 calls, under $2 in total.

## Steps
1. Drive one live turn as `architect`, then one as `orchestrator` (sequential, ≥ 10 s apart) → expect:
   two HTTP 200 replies.
2. Read `openshell logs` across both turns, at the highest level the CLI exposes → capture two records
   and determine whether they resolve to A and to B via (a) the `X-Hermes-Profile` value
   (`osh-f64b-architect`, `osh-f64b-orchestrator`) or (b) another per-profile non-secret field the
   operator names as logged. Record every field each record carries.
3. If neither (a) nor (b) is log-visible, record `CARRIED — UA-37` with the two records verbatim and
   the field inventory → this is the expected outcome on v0.0.72; it is not a pass and not a failure.

## Pass
With two HTTP 200 turns and two captured records, the result cell is `PASS` when the records attribute
A→A and B→B via `X-Hermes-Profile` or an operator-confirmed equivalent. If no attributable non-secret
field is log-visible, it is literally `CARRIED — UA-37` (non-blocking, operator G2 ruling option 1;
erratum E2), with both records verbatim and their per-record field inventories as evidence. If a turn
fails, a record is missing, or logged attribution contradicts the known turn, it is `FAIL`.

## Evidence
- `scenario-AC-OSH-F64.b-6/openshell.log`: both records verbatim.
- `scenario-AC-OSH-F64.b-6/evidence.txt`: the field inventory per record, plus the D5
  `session_model_usage` rows via `python3 -c` over each profile's `state.db`, as context only (D5 is the
  OSH-F64.b waiver, not this bar).
