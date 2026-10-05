---
ac: AC-OSH-F64.b-7
kind: live
model: live
base_url: https://inference-api.nvidia.com/v1
api_mode: chat_completions
model_id: aws/anthropic/bedrock-claude-opus-5-5
api_key: openshell:resolve:env:OSH_DIRECT_INFERENCE_KEY
credential_provider: hermes-direct-inference
spec: spec/coworker-types.yaml
fixtures:
  - fixtures/osh-f64e-gateway
LIVE_MODEL_CALLS_MAX: 40
LIVE_BUDGET_USD: 5
timeout_s: 600
carried_from: tests/e2e-scenarios/OSH-F64.b/AC-OSH-F64.b-7.md
---

# AC-OSH-F64.b-7 (carried, step 4 only — UA-36) — APF, not the managed route, governs the path

Carried from OSH-F64.b: ONLY its step 4 (path-level APF enforcement). Steps 1-3 stayed PASS on
OSH-F64.b and are not re-run. This file is the sole source of the OSH-F64.e result row for this id;
the frozen `tests/e2e-scenarios/OSH-F64.b/AC-OSH-F64.b-7.md` is OSH-F64.b's record and is not re-run.

With `POST /v1/chat/completions` omitted from the RENDER's gateway policy, that supported path is
refused by an APF policy denial in `openshell logs` (not an upstream 401/404), and with the full
rendered policy restored the same request returns 200 — so APF, not the managed route, governs the
path. The row PASSES only if, from the same gateway sandbox, the frozen known-valid managed-route
request to the residual `inference.local` route ends ONLY in an APF/CONNECT denial, a connection
refusal, or OpenShell's exact `cluster inference is not configured` 503; any route response (200, or
an upstream 401/404 or other status) is `FAIL — OPEN UA-36 (managed route reachable)`, never PASS.
The residual probe is the ADR's safety interpretation of the bar; the operator rules on it (ADR
§Gating G1). Report the outcome as written; do not rule it.

The gateway default profile (fixture `osh-f64e-gateway`) is the served env, the "badge".

## Setup
Shared by all three OSH-F64.e scenarios (one lane, run once, SERIALISED with OSH-F64.c's live tier;
never concurrent). Live tier only after LANE READY.

**Lane preconditions (operator, before step 1):** the image `osh-f64-gateway:f64e-<sha7>` built at the
PR head (never `:pinned`), with its broker `--image` entry, and its LANE v8 `.fork-sha` equal to the PR
head; OpenShell provider `hermes-direct-inference` (type `generic`, credential key
`OSH_DIRECT_INFERENCE_KEY`; the user placed the value) created BEFORE the gateway create;
`providers_v2_enabled` unset. Sandboxes (prefix `osh-f64b-`, admitted): `osh-f64b-gw`,
`osh-f64b-orchestrator`, `osh-f64b-architect` — 3 in total, shared by the three scenarios.

1. Render the direct spec: `hermes coworker compose tests/e2e-scenarios/OSH-F64.e/spec/coworker-types.yaml
   --out <render> --provision-dry-run`, with `gateway_image` set to the lane tag. Record the plan.
2. Create `osh-f64b-gw` with EXACTLY the plan's gateway line (the AC-OSH-F64.e-4 shape):
   `openshell sandbox create --name osh-f64b-gw --from osh-f64-gateway:f64e-<sha7> --policy
   <render>/default/policy-gateway.yaml --provider hermes-direct-inference`.
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

Then render a policy VARIANT: the RENDER's `<render>/default/policy-gateway.yaml` with exactly the
`POST /v1/chat/completions` rule removed, everything else byte-identical. Record the diff as
`variant.diff`.

## Steps
1. Apply the variant to `osh-f64b-gw` (the hot-reload path used on OSH-F64.b; wait for
   `CONFIG:LOADED` with the new `policy_hash`) → expect: `openshell policy get osh-f64b-gw --full` shows
   the `inference-api.nvidia.com:443` endpoint with three rules.
2. From the gateway sandbox, with the served profile's full env, send an otherwise-valid
   `POST https://inference-api.nvidia.com/v1/chat/completions`, exactly as the served profile's provider
   config would send it → expect: refused, with an
   `L7_REQUEST deny POST inference-api.nvidia.com:443/v1/chat/completions` APF line in `openshell logs`
   (a policy decision, NOT an upstream 401/404).
3. Residual probe, same sandbox, same env: send the frozen OSH-F64.b scenario's known-valid
   managed-route request unchanged — `POST /v1/chat/completions` to its front-matter `base_url`, with its
   front-matter `model_id` and its routing trigger, all read from the front matter of
   `tests/e2e-scenarios/OSH-F64.b/AC-OSH-F64.b-7.md` at run time (nothing is copied here) → expect ONLY
   one of:
   - (i) an APF/CONNECT policy denial: a `CONNECT denied inference.local:443` line carrying OPA policy
     evidence (`engine:opa`);
   - (ii) a connection refusal;
   - (iii) OpenShell's own HTTP 503 whose body carries the exact error string
     `cluster inference is not configured`.

   An `Inference interception denied` line (any reason: TLS handshake failed, I/O error, context not
   configured) is NOT accepted; it is emitted before any policy evaluation and proves neither APF
   governance nor an absent route. ANY other outcome — 200, or an upstream-shaped 401/404/4xx/5xx,
   which still means the managed route answered and APF was bypassed — records
   `FAIL — OPEN UA-36 (managed route reachable)` (ADR §Gating G1).
4. Restore the full rendered policy (wait for `CONFIG:LOADED`) and repeat step 2's request → expect:
   HTTP 200 and an `L7_REQUEST allow` line.

## Pass
With the render's `POST /v1/chat/completions` omitted, that path is refused by an APF policy denial on
the fleet's model route AND the residual `inference.local` probe (step 3) ends in one of its three
accepted outcomes; with the full rendered policy restored, the same request returns 200. APF, not the
managed route, governs the path.

## Evidence
- `scenario-AC-OSH-F64.b-7/openshell.log`: the deny and allow lines, and the residual probe's denial
  line if any.
- `scenario-AC-OSH-F64.b-7/variant.diff`: the one-rule policy diff.
- `scenario-AC-OSH-F64.b-7/evidence.txt`: the status codes for steps 2-4; for step 3 the HTTP status and
  the response body's `error` field verbatim, or the connection error. Never request headers.
