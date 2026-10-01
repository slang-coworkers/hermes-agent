---
title: OpenShell fleet substrate
description: Run a Hermes coworker fleet with one OpenShell sandbox per profile, reached over ssh, under per-profile openshell policy — no nested podman.
---

# OpenShell fleet substrate (`substrate: openshell`)

The fleet baseline (topology rule 3) is a MUST: **every coworker's tool calls run
in that coworker's own per-profile sandbox.** On a plain host the `podman` substrate
realises this as one rootless container per profile. On a NemoClaw / OpenShell box
that substrate cannot be reused — see [Why not nested podman](#why-not-nested-podman) —
so the `openshell` substrate renders **one OpenShell sandbox per profile, reached over
the builtin `ssh` backend**, under a per-profile `openshell policy`. This page is the
runbook for rendering, provisioning, and tearing down that fleet.

Select it with the top-level spec key:

```yaml
substrate: openshell
sandbox_scope: profile   # the default; see Sandbox scope below
```

## Architecture: one sandbox per profile over ssh

The gateway process itself stays shared (topology rule 1): rooms, kanban, cron,
approvals and the LLM loops all run in the one gateway. What is isolated is each
profile's **tool** execution. `hermes coworker compose <spec>` with
`substrate: openshell` renders, for every coworker profile:

- `terminal.backend: ssh` — the builtin ssh terminal backend, so each tool call is
  executed inside that profile's own OpenShell **worker sandbox**, never on the
  gateway host. The render-derived `plugins.entries.nv-fleet-gates.settings.expected_backend`
  is `ssh` (the same source that writes `terminal.backend`), so the single
  `pre_tool_call` veto admits an own-profile ssh call — **bound to that profile's OWN
  sandbox host** via the worker-unforgeable managed `expected_ssh_host` map (a sibling
  host, an absent host, or an absent/malformed map is refused *before* the readiness
  gate connects) — and denies `local`, cross-profile, an unready backend, and stdio MCP.
- a per-profile ssh block: `terminal.ssh_host` (the ssh-config **alias**
  `openshell-osh-f63-<profile>`, distinct per profile, which resolves through the
  OpenShell `ProxyCommand` to the sandbox created under the bare `--name osh-f63-<profile>`
  — the alias and the bare sandbox name are deliberately *different* strings, not equal),
  `terminal.ssh_user` (`sandbox`), `terminal.ssh_port`, and
  `terminal.ssh_key` — a **path** under the gateway `$HERMES_HOME` (never key material —
  the render creates it as an empty mode-0600 placeholder file; in the fleet testbed that
  root is under `/workspace/extra/hermes-fleet-testbed/`, never `/opt`). On the OpenShell lane `openshell sandbox ssh-config` emits `User sandbox` + a
  `ProxyCommand` and **no `IdentityFile`** (auth rides the mTLS proxy), so the key file
  need only EXIST — present for the builtin `ssh -i <path>` contract, not used for auth.
- a per-profile `policy-<profile>.yaml` — the `openshell policy` allow-set for that
  sandbox (see [Egress policy](#egress-policy-one-openshell-policy-per-profile)).

There is **one OpenShell sandbox per profile** and no shared container: the fleet's
isolation is the OpenShell sandbox boundary plus the per-profile policy.

## Why not nested podman

A 2026-09-17 operator probe measured that **nested podman inside an OpenShell
sandbox is blocked**: `pivot_root` returns `EPERM`, `/sys/fs/cgroup` is read-only,
there is no `/dev/net/tun`, and `/proc/sys` is read-only. So the `podman` substrate's
"one rootless container per profile" cannot run inside an OpenShell box. The
`openshell` substrate therefore reaches a **sibling** OpenShell sandbox per profile
over ssh instead of nesting a container — the provisioning plan runs **no** container
engine (`podman`/`docker`) at all.

## The two ssh routes, and the chosen route

Reaching a per-profile sandbox over ssh has two candidate routes:

- **Route (a) — the mounted OpenShell mTLS identity + `ProxyCommand openshell
  ssh-proxy`.** The OpenShell docker driver bind-mounts an mTLS identity
  (`OPENSHELL_TLS_*`) into every sandbox; with it, the `openshell` CLI can
  `sandbox list`/`create` and ssh+scp into a sibling sandbox via
  `ProxyCommand openshell ssh-proxy … --name <sibling>`. This is the shape the builtin
  ssh backend needs, so it is the **chosen route**.
- **Route (b) — an `sshd`-in-image + host `openshell forward`.** A worker image ships
  `sshd` and the host forwards a port to it. This was inconclusive on 2026-09-18 (a
  listener started via `sandbox exec` dies with the session), so it is the documented
  fallback, not the default.

### Route (a) admissibility

Route (a) is **admissible only if the rendered per-profile `openshell policy` denies
the OpenShell gateway control plane (`host.openshell.internal:8080`) for the worker's
tool egress.** The mounted mTLS identity is gateway-admin over *all* siblings, so a
worker that could reach `host.openshell.internal:8080` could list/create/connect to
sibling sandboxes — breaking profile isolation (topology rule 3, a MUST). The
per-profile policy therefore allows a small fixed base of **two endpoints** — the OneCLI
request hop and the inference route (the OSH-F64 extension below may add a validated third
broker endpoint) — and **`host.openshell.internal:8080` is never an endpoint.**
AC-OSH-F63-5 proves this at runtime with a worker-side negative control: from inside a
policy-constrained sandbox, an attempt to `sandbox list` / connect to a sibling via
`host.openshell.internal:8080` must be **denied** (nonzero exit, no sibling metadata).
A worker that *can* reach a sibling is
an unconditional isolation failure — the row falls back to route (b) or a genuinely
`<prefix>-*`-scoped OpenShell identity, which must be *implemented* (not merely
documented) before the row can pass.

**Operator gateway credentials must never be placed inside a worker sandbox** (nor in
`<prefix>-gw` / `brev-hermes`): they are admin over every sibling. Route (a)'s safety
rests on the policy denying the gateway endpoint, not on the identity being scoped.

## Egress policy: one `openshell policy` per profile

The render emits one `policy-<profile>.yaml` per coworker profile, in the OpenShell policy
grammar the `openshell sandbox create --policy` CLI accepts — top-level `version`,
`filesystem_policy`, `landlock`, `process`, and `network_policies` (an earlier flat
`egress: {allow: […]}` shape is rejected with `unknown field egress`). Its base
`network_policies` allow-set is **two endpoints**, taken from the spec's `egress` block.

### Plain (pre-OSH-F64.b) base posture — the pinned-lane egress

This block is the **plain / pre-OSH-F64.b (pinned-lane) base posture** a bare openshell spec
renders — the base the single-authority overlay below extends, never the OSH-F64.b inference
path. Read the `172.17.0.1:18255` hop **here** as the plain OneCLI **data-plane** base egress,
**not** an OSH-F64.b inference route (OSH-F64.b drops it from the inference path — see the
overlay). The spec `egress` block carries the data-plane hop and the inference route directly:

```yaml
egress:
  proxy_addr: "172.17.0.1:18255"                    # the OneCLI DATA-PLANE hop (allowed; DNAT alias of onecli-lego:10255)
  inference_route: "inference-api.nvidia.com:443"   # the model inference route (allowed)
  sandbox_image: "osh-f63-base:trixie"              # the --from image (broker-pinned; Debian trixie, glibc 2.41)
```

**In-sandbox egress goes through the substrate proxy.** Every OpenShell sandbox has
`HTTP_PROXY=http://10.200.0.1:3128` set inside it, and in-sandbox clients reach the declared
hops (the OneCLI data-plane hop `172.17.0.1:18255`, the inference route) **only** through that
proxy — a direct connection to `172.17.0.1` is **not routable** from a sandbox. The policy's
declared endpoints stay the target addresses (`172.17.0.1:18255` + the inference route) with
`protocol: rest`; the proxy is the egress *path*, not a policy endpoint. `172.17.0.1:18255` is
the OneCLI **data-plane** hop (a source-preserving DNAT alias of `onecli-lego:10255`): the
OpenShell substrate (0.0.72) hard-blocks `10255` as a control-plane port, so the data plane is
`18255`. Per profile the plain base policy renders both endpoints as `protocol: rest`:

```yaml
# policy-builder.yaml
version: 1
filesystem_policy: {include_workdir: true, read_only: [/usr, /bin, /lib, /etc], read_write: [/tmp]}
landlock: {compatibility: best_effort}
process: {run_as_user: sandbox, run_as_group: sandbox}
network_policies:
  worker-egress:
    name: worker-egress
    binaries: [{path: /usr/bin/curl}]
    endpoints:
      - {host: 172.17.0.1, port: 18255, protocol: rest, enforcement: enforce, rules: [{allow: {method: POST, path: /**}}]}
      - {host: inference-api.nvidia.com, port: 443, protocol: rest, enforcement: enforce, rules: [{allow: {method: POST, path: /**}}]}
```

Nothing else is allowed in the base posture: **no wildcard host**, the OneCLI control plane
(`172.17.0.1:10256`) is never an endpoint, and **outbound** ssh (port 22) is never an endpoint
(ssh arrives INBOUND through the OpenShell proxy socket, not outbound). **AC-OSH-F63-5 validates
the emitted file unchanged:** if the pinned OpenShell CLI rejects it (the sandbox never reaches
Ready under `openshell sandbox create --policy`) or fails to enforce it, the criterion FAILS and
the renderer must be corrected; do not regenerate or hand-edit the policy during verification.

### OSH-F64.b single-authority overlay (gated on `egress.inference_provider`)

When the spec carries an `egress.inference_provider` block (ADR §D4, spec-gated — a spec
**without** it renders its own legacy posture byte-unchanged: the plain F63 base pictured above
when it declares no `broker_addr`, or the §D7 broker posture when it does), the render switches
the fleet to the **single-authority** inference posture and the worker/gateway split below. The
`172.17.0.1:18255` OneCLI hop is **dropped from the inference path** — it is no longer a
model-call route — and the only raw `tls: skip` hop that remains anywhere is the lane
control-plane broker `172.17.0.1:18777`:

- **Per served coworker — `policy-<profile>.yaml`.** A worker sandbox issues **no** model call
  (the served model turn runs in the shared gateway sandbox, never in a worker), so its policy
  carries **no inference endpoint**. Its only `worker-egress` endpoint is the broker hop
  `172.17.0.1:18777`, rendered as a raw passthrough `{host, port, tls: skip}` — the one permitted
  raw hop (0.0.72 tunnels the sandbox's outbound TLS through it, so a `protocol: rest` shape would
  try to parse and enforce inside an opaque tunnel and break the hop). No second raw hop, and
  inference is **not** a worker endpoint.
- **For the default / multiplexer profile — `policy-gateway.yaml`.** The default profile is the
  **model-call sandbox**: every served coworker's model turn is multiplexed through it, so this
  policy — and only this policy — carries the inference endpoint. Its `worker-egress` endpoints
  become the broker raw hop plus exactly one REST endpoint, `inference.local:443` with
  `protocol: rest`, `enforcement: enforce`, a `provider: compatible-endpoint` credential-rewrite
  reference (**no `tls: skip`**), and EXACTLY these four allow rules and nothing else:

```yaml
# policy-gateway.yaml — worker-egress endpoints (OSH-F64.b single-authority posture)
endpoints:
  - host: inference.local
    port: 443
    protocol: rest
    enforcement: enforce
    provider: compatible-endpoint          # fires the OpenShell L7 credential rewrite
    rules:
      - {allow: {method: POST, path: /v1/chat/completions}}
      - {allow: {method: POST, path: /v1/messages}}
      - {allow: {method: POST, path: /v1/responses}}
      - {allow: {method: GET,  path: /v1/models}}
  - {host: 172.17.0.1, port: 18777, tls: skip}   # broker hop — the one permitted raw passthrough
```

The gateway policy's `binaries` allow-list adds the in-process Hermes model-call interpreter
`/opt/hermes/.venv/bin/python` (the served model call is a Python HTTP request, not a `curl`
shell-out) on top of the base `/usr/bin/curl`. Nothing is inferred: the `tls: skip` shape is
used only for the broker hop, and only the inference endpoint is ever `protocol: rest`. The
render writes each file as `policy-<profile>.yaml` / `policy-gateway.yaml` beside the profile's
config (carried into the installed profile via `distribution_owned`); AC-OSH-F63-5's validation
applies to each emitted file unchanged.

## OpenShell provider: single-authority inference credential rewrite (OSH-F64.b)

Under `substrate: openshell` the fleet's model calls do not transit a OneCLI tunnel; they
go to a single **OpenShell inference provider** that OpenShell + APF alone governs.

**How the operator configures it.** The provider sits behind `https://inference.local` —
its `base_url` carries the `/v1` suffix and its `api_mode` is `chat_completions`. The render
writes each served profile's model-provider config as a `providers.<name>` block (the `<name>`
is the spec's `egress.inference_provider.provider`, e.g. `compatible-endpoint`) that `model`
selects, holding ONLY a fixed, non-secret rewrite-trigger placeholder in `api_key`:

```yaml
model:
  provider: compatible-endpoint        # the render selects the OpenShell provider
providers:
  compatible-endpoint:
    base_url: "https://inference.local/v1"
    api_mode: chat_completions
    api_key: sk-OPENSHELL-PROXY-REWRITE   # non-secret trigger, identical across all profiles
    default_model: aws/anthropic/bedrock-claude-opus-5-5
    extra_headers:
      X-Hermes-Profile: osh-f64b-architect  # distinct per profile — the attribution carrier
```

`sk-OPENSHELL-PROXY-REWRITE` is **identical across every profile** — it is the token that
fires the OpenShell L7 **credential rewrite**, NOT an identity. The real credential lives
OpenShell-side as `COMPATIBLE_API_KEY` and **never appears in any sandbox config**. So
OpenShell + APF is the sole authority for destination, method, path, and credential: a
per-profile badge can never supersede APF.

**Per-profile attribution.** Both profile children share the one gateway (model-call)
sandbox, so a per-request header — not a per-sandbox identity — distinguishes them in the
OpenShell / inference logs: the render sets a distinct non-secret
`X-Hermes-Profile: osh-f64b-<role>` request header per served profile. It is applied under
`chat_completions`, the mode in which a custom provider's `extra_headers` reach the request.

**COST-F30 metering (replacement path).** With the OneCLI tunnel dropped, the model call
goes direct to the OpenShell provider, whose response carries token usage that Hermes
accrues natively; the plugin-observable meter is the `llm_execution` middleware. Two known
coverage gaps remain: `codex_app_server` early-returns and bypasses all hooks and
middleware, and the auxiliary / aux-model path issues its own client call that bypasses
plugin middleware — neither is metered by the `llm_execution` hook.

**Supersession (openshell substrate only).** OSH-F64.b **supersedes OSH-F64 §D7**
(`podman-onecli` is inert under `substrate: openshell`). For the openshell
substrate only — the **podman** substrate keeps OneCLI unchanged — the OneCLI-identity ADRs
are superseded: **FLEET-F62**, **ISO-F14**, and **CRED-F28**. Under OSH-F64.b the
per-profile OneCLI identity / badge is replaced by the OpenShell provider credential rewrite
governed by APF. The podman substrate path is unchanged.

**`egress.pinned_offline_lane` (boolean, default off).** Set it to `true` when the fleet runs in
the pinned offline lane: a sha-pinned offline fork mirror and closed GitHub egress. Only then does
the render emit its lane-specific settings — the disabled firecrawl providers on the default and
every served coworker, and the managed `plugins.scan_on_install: false`. Absent or `false`, an
`openshell` fleet renders none of them (FLEET-F62.c and OSH-F63 are unchanged), and the worker
policies are identical either way. Any value other than `true`/`false` is refused at compose time.

Whether the lane flag ALSO emits the per-coworker OneCLI chain-dial
(`secrets.onecli.override_existing` plus `podman-onecli` `proxy_rewrite`/`ca_bundle`) depends on
the single-authority gate:

- **With `egress.inference_provider` (OSH-F64.b single-authority posture):** NO chain-dial. The
  per-coworker OneCLI dial is superseded (§D7 inert) — the render takes the OpenShell
  inference-provider path for each profile instead, so the lane flag emits ONLY the firecrawl
  disable and `scan_on_install: false`, never `secrets.onecli.override_existing` or a
  `proxy_rewrite`/`ca_bundle`.
- **Without it (legacy §D7, ungated):** the lane flag additionally emits each served coworker's
  OneCLI chain-dial loopback — the sha-pinned offline fork mirror with closed GitHub egress is the
  lane that legacy posture exists for.

The installer (`hermes coworker install-openshell` and `install-into-sandbox.sh`) is lane-only: it
refuses a spec without `pinned_offline_lane: true` before it touches any state, so render a
non-lane `openshell` fleet with `coworker compose`.

## Operator prerequisites

Before a fleet can be provisioned, the operator must satisfy these prerequisites:

1. A **name-prefixed OpenShell access path** reachable from the provisioning host — a
   scoped way to run `openshell sandbox create|exec|delete|ssh-config` and
   `openshell logs` against the gateway, restricted to
   `<prefix>-*` sandboxes. The gateway admin credentials are admin over ALL sandboxes
   and **must never** be mounted into the provisioning host, a worker sandbox,
   `<prefix>-gw`, or `brev-hermes`.
2. Confirmation of which `openshell logs --source` (gateway vs sandbox) records a
   policy denial, and whether `openshell policy` governs the sandbox's own mTLS control
   connection to the gateway control plane `host.openshell.internal:8080` (this decides
   whether route (a) is admissible).
3. The **pinned `--from osh-f63-base:trixie`** image (Debian trixie, glibc 2.41):
   OpenShell injects PID 1 `/opt/openshell/bin/openshell-sandbox`, linked against
   glibc ≥ 2.39, so an alpine (musl) or Debian bookworm (glibc 2.36) base restart-loops
   and never reaches Ready. This `--from` tag is broker-pinned — not an operator-swappable
   placeholder; re-pinning it means updating both the broker's allowed `--from` and the
   spec's `egress.sandbox_image`. For route (b) the image must additionally ship `sshd`.
4. The rendered per-profile ssh config installed into the gateway user's OpenSSH
   configuration (the `openshell sandbox ssh-config <name>` output), so `terminal.ssh_host`
   resolves through the OpenShell `ProxyCommand`, and the per-profile key path referenced
   by `terminal.ssh_key` present (it need only exist; the ssh-config carries no
   `IdentityFile` — auth rides the mTLS proxy).
5. The **managed fragment** (`managed/config.yaml`) installed to `$HERMES_MANAGED_DIR`
   (or `/etc/hermes/config.yaml`) — it carries the veto's per-profile `expected_ssh_host`
   anchor (and the fleet role map); **without it the veto refuses every ssh terminal
   call** (fail-closed), so install it before starting the gateway (see the
   [fleet deployment runbook](./fleet-deployment.md) §2).

## Provisioning plan and teardown

`hermes coworker compose <spec> --provision-dry-run` renders the fleet and then prints
a deterministic provisioning plan (identical across runs and across `--out` dirs — no
`--out` path appears) — the create and ssh-config lines per profile, then
the teardown lines. Each `--policy` names the bare `policy-<profile>.yaml` (the operator
materialises it at that name from the profile's `distribution_owned` before running the
plan). Each `openshell sandbox create --name` uses the **bare sandbox name**
`osh-f63-<profile>`, while that profile's rendered `terminal.ssh_host` is the distinct
ssh-config **alias** `openshell-osh-f63-<profile>` that resolves to it through the
OpenShell `ProxyCommand` — the two are deliberately *different* strings. `create`,
`ssh-config`, and `delete` all take the bare name, so they target the same sandbox
(shown below for the `builder` profile: alias `openshell-osh-f63-builder`, sandbox
`osh-f63-builder`):

```
openshell sandbox create --name osh-f63-builder --from osh-f63-base:trixie --policy policy-builder.yaml
...
openshell sandbox ssh-config osh-f63-builder
...
openshell sandbox delete osh-f63-builder
```

The plan runs **no** container engine. The teardown lines delete each per-profile
sandbox, freeing that profile's resources.

## Sandbox scope

The top-level `sandbox_scope` key selects how many OpenShell sandboxes back the fleet:

- **`profile` (the default, and the delivered scope):** one OpenShell sandbox per
  profile, rendered as the per-profile ssh block above. This is the operator's
  2026-09-17 ruling (Option 1: one sandbox per profile).
- **`session`:** one OpenShell sandbox per coworker *session*. This value is
  **recognised but deferred** — the render **fails closed** with a `CompositionError`
  rather than silently rendering as `profile`. Per-session sandboxes need a custom
  `register_terminal_environment_provider` `openshell` provider (a lazy
  `create_environment` that mints one sandbox per session and reaps it on idle) with
  `terminal.backend: openshell` — a CORE-CHANGE-free plugin path that OSH-F63 does not
  build. Until that provider ships, use `sandbox_scope: profile`.

## install into an existing NemoClaw sandbox

The demo runs the FLEET-F62 five-profile fleet on the OpenShell substrate INSIDE an
existing NemoClaw Hermes sandbox — the shape of the user's `brev-hermes` — without ever
touching that user box by hand. The two-phase entrypoint is
`plugins/nv-coworker-compose/openshell/install-into-sandbox.sh`, staged into the sandbox
at create time (`openshell sandbox create --upload`) or via `openshell sandbox exec`.

### What the operator runs (on `brev-hermes`)

Preview first — `install-into-sandbox.sh <spec> --ref <40-char-sha> --dry-run` prints the
full ordered plan and changes nothing. Then run it for real, adding the sandbox's loopback
gateway credential URL: `install-into-sandbox.sh <spec> --ref <40-char-sha> --gateway-url <ws-url>`.
Phase B probes and creates the fleet rooms against the live gateway over that authenticated
WebSocket, so the real run — unlike `--dry-run` — requires `--gateway-url`.

On a lane where the OpenShell broker validates `--policy` as a HOST path it reads itself, add
`--policy-root <host dir>`: each worker `sandbox create --policy` is then rebased to
`<host dir>/render/<role>/policy-<role>.yaml` (the operator mirrors the rendered per-worker
policies under that root first); with it unset, the gateway-internal render path is used and the
plan is byte-for-byte unchanged. The script:

- **Phase A** installs the fleet plugins (`nv-coworker-compose`, `nv-fleet-gates`,
  `podman-onecli`) at the pinned commit so `hermes coworker` exists.
- **Before Phase A's first `plugins install`**, the script writes `plugins.scan_on_install:
  false` into the managed config (the §D6 install-scan guard). Without it the install-time
  scanner returns a DANGEROUS verdict on these first-party plugins — false positives that
  `--force` cannot override — and refuses the offline install. Turning the scan off is a
  deliberate, sound trust decision **only** under this lane's boundary: the fleet plugins are
  40-char-sha-pinned first-party artifacts installed offline from the operator-controlled
  `/opt/hermes/fork` mirror with GitHub egress closed. It is an **openshell-only** managed
  delta — the plain-host / container substrate keeps plugin scanning ON, since those installs
  run from GitHub with egress open.
- **Phase B** (`hermes coworker install-openshell`) composes the fleet under
  `substrate: openshell` and provisions **five worker sandboxes** — one per served
  coworker — each Ready under a per-bot `openshell policy` (that bot's APF). Under OSH-F64.b a
  worker issues no model call (workers ship no Hermes runtime), so its policy allows ONLY the
  OpenShell-0.0.72 broker hop (`172.17.0.1:18777`, the single raw `tls: skip` endpoint) — the
  18777 broker aside, no raw hop remains and inference is NOT a worker endpoint — plus the
  interpreter binaries appended to `worker-egress.binaries` (`/usr/bin/python3*`,
  `/opt/hermes/.venv/bin/python`) on top of the inherited `/usr/bin/curl`, and the write-denied
  `/opt/osh-lane` read-only lane, and nothing else; neither the control plane `:10256` nor
  outbound ssh `:22` is ever allowed. The **gateway** policy is where inference lives — the
  `inference.local:443` `protocol: rest` endpoint (its four allow rules + the
  `provider: compatible-endpoint` reference) plus the same broker raw hop; the operator creates
  the gateway sandbox `--policy` from the rendered `policy-gateway.yaml`.
- It also installs the fleet plugins under **each** served profile (a profile
  distribution excludes plugins, so the veto must be installed per profile or it is
  absent there).
- **Inference credential (OSH-F64.b — single authority, no OneCLI grants).** Model calls are
  governed by the OpenShell inference provider above, NOT by a per-profile OneCLI identity. Under
  the single-authority posture (`egress.inference_provider` present) the render leaves
  `plugins.entries.podman-onecli.settings.profile_secret_sets` the spine `[]`, and the openshell
  installer no longer populates it from `egress.onecli_secret_ids` — so there is **no OneCLI
  onboarding step for inference**. Each served profile's sandbox holds only the fixed
  `sk-OPENSHELL-PROXY-REWRITE` placeholder in its provider `api_key`; the real
  `COMPATIBLE_API_KEY` the rewrite reads lives OpenShell-side and appears in no rendered config.
  (A legacy OSH-F64 spec that carries no `egress.inference_provider` keeps the pre-OSH-F64.b
  grant path unchanged — the posture is spec-gated.)
- It **edits the existing default profile in place**, taking a deterministic **backup**
  of the original `$HERMES_HOME/config.yaml` FIRST — before any `plugins install --enable`
  rewrites it — and writing the managed fragment to the managed dir as a separate file.
  It must **never run `profile install default`**: `get_profile_dir("default")` resolves
  to `$HERMES_HOME` itself (the user's running Hermes), so the default is edited, never
  reinstalled.
- It creates the fleet rooms and wires, then restarts the gateway via the shipped
  `hermes gateway restart`.
- **Post-install Bot-Mode onboarding (required for `message_agent`).** After the gateway is
  up, the operator runs the fleet onboard step from the default gateway context —
  `hermes -p default onboard coworker <spec> --gateway-url "$GW_URL"` — which creates each
  served coworker's **canonical Bot Chat** session and its `ui_meta['hermes-bots']` roster
  entry. `message_agent`'s tool schema is injected ONLY into a coworker's canonical Bot Chat,
  so without this step the served bots hold no Bot Chat and `message_agent` (bot-to-bot
  delivery) is unreachable fleet-wide; the installer's rooms (`groups.create`) and wires do
  NOT create it. Run it as `-p default`, never profile-scoped (a profile-scoped invocation
  would re-render SSH-key paths relative to the wrong `HERMES_HOME`); it force-re-renders the
  served distributions, so confirm the post-onboard per-role config is unchanged (SSH, the
  OpenShell inference provider config, the disabled firecrawl providers, the enabled plugins).
- The dashboard or desktop app then attaches to the running gateway through the desktop
  forward (`openshell forward start <29xxx-port> osh-f64-gw` — brokered, `-d`, bind
  `172.17.0.1`, port in 29000–29999) — one gateway connection for the whole app, listing
  the served profiles.

**Gateway-sandbox preflight (shared learnings).** Each served profile's process runs in
the gateway sandbox and reads `skills.external_dirs`, so `/opt/hermes-fleet/shared-learnings/skills`
must exist IN the gateway sandbox before boot (stage or mount it there). The remote-ssh
substrate binds no container mount, so this is a gateway-sandbox prerequisite, not a
worker-image one; the orchestrator wiki-fold stays an operator opt-in under openshell.

### Rollback

To back the change out and leave the existing default exactly as before:

- **Restore the default `config.yaml` backup** and the managed-config backup taken in
  Phase A (this reverts the in-place default edit). If the managed config did NOT exist
  before the install — a `config.yaml.osh-f64.absent` marker sits beside it, recording that the
  §D6 scan-guard write created it — **delete both the managed `config.yaml` and that
  `config.yaml.osh-f64.absent` marker** instead of restoring a backup, so the install-scan guard
  does not linger with plugin scanning left disabled and no stale marker survives to make a
  later operator-created managed config look originally-absent (which would skip its backup).
- **Remove the five coworker profiles** (`hermes profile remove <role>` per role) — the
  profiles the install added, never the default profile.
- **Uninstall the fleet plugins** that were newly installed (default + per-profile); a
  plugin that pre-existed at the pinned ref is left as the operator had it.
- **Restart the gateway** so it re-reads the restored default.
- **Delete** the `osh-f64-*` worker sandboxes (`openshell sandbox delete`) — each sandbox's
  policy is bound inline by `sandbox create --policy` and is removed with the sandbox, so
  there is no separate teardown command — and the `osh-f64-gw` gateway sandbox when it was
  a throwaway.

### What the NemoClaw dashboard / APF shows afterwards

The NemoClaw dashboard shows one running worker sandbox per served coworker, and its APF
view shows one `openshell policy` per sandbox — the three allowed endpoints per bot plus
the `/opt/osh-lane` read-only lane and the appended interpreter binaries, with an
off-policy egress attempt denied and logged. The whole fleet is one gateway on one bound
port; the per-profile `hermes -p <profile> chat` helper processes bind no external port.

### The `hermes gateway restart` child and the wrapper `[SECURITY]` guard

The install ends by restarting the gateway so it re-reads the edited default. On a NemoClaw
sandbox the `hermes` entrypoint is a wrapper whose `[SECURITY]` startup guard refuses to
start a process whose environment carries raw secret-shaped values — the deploy exports
`HERMES_DASHBOARD_SESSION_TOKEN` for the live-WS preflight, and a supervised host may also
carry `API_SERVER_KEY` or a `*_TOKEN_FILE`. The restart child needs none of them, so the
installer scrubs `HERMES_DASHBOARD_SESSION_TOKEN`, `API_SERVER_KEY`, and every `*_TOKEN_FILE`
(for example `OSH_BROKER_TOKEN_FILE`) from that child's environment before spawning it; the
gateway process itself keeps its own configured secrets.

That env-scrub is scoped to the restart child only — it does **not** waive the core guard.
The `API_SERVER_KEY` guard is separate: a gateway that enables `api_server` and is supervised
still requires a strong key configured for the listener; scrubbing an inherited name off the
short-lived restart child does not enable an unauthenticated API server. Configure a strong
`API_SERVER_KEY` on the supervised gateway when `api_server` is enabled, or leave the
platform disabled.

### Live-lane preconditions for a served turn

Before a served coworker can complete a live turn over the OpenShell lane, four
preconditions must hold on `brev-hermes`:

1. **OpenShell Sandbox CA trusted.** The gateway CA bundle must carry the **OpenShell
   Sandbox CA** (`/etc/openshell-tls/ca-bundle.pem`), or TLS to `inference.local` fails
   (curl exit 60) and no served turn can complete.
2. **Rewrite-trigger placeholder only.** Each served profile's model-provider config holds
   only the `sk-OPENSHELL-PROXY-REWRITE` placeholder — **never** `COMPATIBLE_API_KEY`,
   which lives OpenShell-side and must not reach any sandbox config.
3. **No `API_SERVER_*` in a profile `.env`.** Do not seed a served profile's `.env` with any
   `API_SERVER_*` value — the multiplexed gateway skips a profile whose `.env` carries one.
4. **Start from the in-sandbox watchdog.** `openshell sandbox exec` runs in a different
   network namespace than the gateway, so start the fleet processes from the in-sandbox
   watchdog, never via `sandbox exec`.
