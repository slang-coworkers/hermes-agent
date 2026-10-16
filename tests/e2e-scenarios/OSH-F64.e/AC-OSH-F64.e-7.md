---
ac: AC-OSH-F64.e-7
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
  - fixtures/osh-f64e-worker-architect
LIVE_MODEL_CALLS_MAX: 40
LIVE_BUDGET_USD: 5
timeout_s: 600
---

# AC-OSH-F64.e-7 — a served turn on the direct route is a 200, placeholder-only, logged as an APF L7 allow

On the live lane a served coworker's real model turn on the direct route returns HTTP 200 while its
rendered config and process env hold only the resolve placeholder (no credential value on disk or in
env), and `openshell logs` records it as an `L7_REQUEST allow` for
`POST inference-api.nvidia.com:443/v1/chat/completions` (an APF decision), with no `inference.local`
interception record for that turn.

Profiles that must exist before step 1: the gateway default and `architect`, served by the one
multiplexed gateway.

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
1. Drive one live model turn as `architect` through its profile-scoped chat child
   (`POST /v1/chat/completions`, model `aws/anthropic/bedrock-claude-opus-5-5`) → expect: HTTP 200, a
   real model reply.
2. Read `architect`'s rendered `config.yaml` and the chat child's environment (names-only, in-process
   capture as on OSH-F64.b, via `python3 -c`) → expect: `providers.<p>.api_key` is OpenShell's resolve
   placeholder `openshell:resolve:env:NV_INFERENCE_KEY`, `base_url` is
   `https://inference-api.nvidia.com/v1`, and no credential value appears on disk or in env (only the
   placeholder form).
3. Read `openshell logs` for the turn → expect: an
   `L7_REQUEST allow POST inference-api.nvidia.com:443/v1/chat/completions` line with
   `[policy:… engine:l7]`, and no `Intercepted inference request` (`inference.local`) line for that turn.

## Pass
The direct-route turn is 200, the sandbox held only the resolve placeholder, and the request was an
APF L7 allow on the upstream host, not a managed-route interception.

## Evidence
- `scenario-AC-OSH-F64.e-7/evidence.txt`: the reply, and the names-only env/config reads via `python3 -c`.
- `scenario-AC-OSH-F64.e-7/openshell.log`: the `L7_REQUEST allow` line for the turn.
- `scenario-AC-OSH-F64.e-7/effective-policy.yaml`: the Setup capture.
