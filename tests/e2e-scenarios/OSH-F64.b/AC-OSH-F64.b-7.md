---
ac: AC-OSH-F64.b-7
kind: live
model: live
base_url: https://inference.local/v1
api_mode: chat_completions
model_id: aws/anthropic/bedrock-claude-opus-5-5
api_key: sk-OPENSHELL-PROXY-REWRITE
fixtures:
  - fixtures/osh-f64b-gateway
  - fixtures/osh-f64b-worker-architect
LIVE_MODEL_CALLS_MAX: 40
LIVE_BUDGET_USD: 5
timeout_s: 600
---

# AC-OSH-F64.b-7 — OpenShell + APF is the sole egress authority (a badge cannot supersede it)

OpenShell + APF is the sole authority for destination, method AND path: (a) a worker sandbox is
refused an off-policy host; (b) the GATEWAY sandbox with the served profile's FULL env (the "badge")
is refused an off-policy host; and (c) a disallowed inference PATH (omitted from the render's gateway
policy) is refused by an APF policy denial — all logged as policy denials. The OneCLI-tunnel bypass is
gone.

INFRA-GATED (§Gating): same dual gate as AC-6. No model turn is strictly required for the denials, but
it rides the same live lane/budget as AC-6.

## Setup
The same provisioned `osh-f64b-*` fleet as AC-6, built STACKED on `plugin/osh-f64-landing` (operator
msg 112; PROVIDER READY in place). The gateway sandbox carries the served profile's FULL env (the
"badge"); the worker sandbox carries its worker policy.

- **RUN with `HTTPS_PROXY=10.200.0.1:3128` set** (the real launch env).
- **(a)** the gateway CA bundle must carry the "OpenShell Sandbox CA" (`/etc/openshell-tls/ca-bundle.pem`)
  or TLS to `inference.local` fails curl exit 60.
- **(b)** no profile `.env` with `API_SERVER_*` (the multiplexed gateway would SKIP that profile).
- **(c)** `openshell sandbox exec` is a different netns than the gateway — start fleet processes from
  the in-sandbox watchdog.

## Steps
1. **(a) worker → off-policy:** from a served worker sandbox, `curl https://github.com/` (a host NOT in
   the worker policy) → expect: refused, and the denial appears in `openshell logs` (with `--source` =
   the worker sandbox).
2. **(b) gateway/badge → off-policy:** from the GATEWAY sandbox with the served profile's full env
   loaded (the badge-holding process), `curl https://registry.npmjs.org/` (a host NOT in the gateway
   policy) → expect: refused, and the denial appears in `openshell logs` — proving the badge/identity
   does NOT let the gateway sandbox reach an off-policy host (the OneCLI-tunnel bypass is gone).
3. **Contrast control:** the SAME gateway sandbox reaching the inference REST endpoint is ALLOWED (the
   ALLOW line from AC-6) → expect: inference allowed, npmjs refused, from the same sandbox — APF is the
   sole authority.
4. **(c) path-level APF enforcement.** Use the operator-VERIFIED path `POST /v1/chat/completions` (a
   known 200 when allowed — avoids an upstream-support confound). In a throwaway gateway policy VARIANT
   that applies the OSH-F64.b RENDER's policy but OMITS `POST /v1/chat/completions` (keeping the render's
   other allowed paths), send an otherwise-valid `POST <provider>/v1/chat/completions` with the served
   profile's full env → expect: an APF POLICY DENIAL in `openshell logs` correlated with
   `/v1/chat/completions` (a policy-decision denial, NOT an upstream router 401/404). Then RESTORE the
   full rendered policy and confirm the same request returns 200. **Apply the OSH-F64.b RENDER's policy
   to the throwaway gateway sandbox — NOT the operator's pre-existing broader `managed_inference` rule**
   (the point is to prove the RENDER's tighter four-path allow-set is what APF enforces).
   **Substrate-ordering caveat:** if `/v1/chat/completions` still returns 200 (or an UPSTREAM error
   rather than an APF denial) while the render's rule omits it, the OpenShell managed provider route is
   evaluating inference BEFORE APF path-policy — the path guarantee is NOT delivered as-configured, a
   PROVIDER-READY finding to ESCALATE to the operator, never a silent pass.

## Pass
_Amended to the operator ruling 05:22Z, §Gating 1 option 3 (accept) as adjudicated in OSH-F64.b ADR 5367ee0d; the carried half is in `tests/e2e-scenarios/OSH-F64.e/`._

AC-7 passes on steps 1-3 — both off-policy hosts are refused and logged as APF policy denials
(worker→github, gateway→npmjs), and the inference control `GET /v1/models` succeeds. Step 4's
path-level APF denial (the `/v1/chat/completions`-omitted probe) is NOT required here: OpenShell
0.0.72 has no setting that governs path rules on the managed inference route, so the live finding
(omitted path still 200, no APF denial) is the ACCEPTED behaviour and step 4 is CARRIED to OSH-F64.e
(UA-36). DESTINATION + CREDENTIAL remain OpenShell-enforced; the badge never supersedes OpenShell/APF.
The render's four-path allow-set remains proven by AC-2 (rendered policy), which is NOT a claim of
runtime path enforcement. The frozen scenario file is adjudicated under this ADR's ruling.

## Evidence
- `scenario-AC-OSH-F64.b-7/openshell.log`: the two host-denial lines with their `--source`, the
  `/v1/chat/completions`-omitted APF denial line, and the restored-policy inference ALLOW line.
- `scenario-AC-OSH-F64.b-7/evidence.txt`: the `curl` exit codes/outputs (incl. the omitted-path probe
  and the restored 200), and the gateway-env confirmation that the served profile's full env was loaded.
  If the omitted-path request returns 200 or an upstream error rather than an APF denial, the recorded
  PROVIDER-READY escalation.
