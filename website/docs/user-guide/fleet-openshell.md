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

The render emits one `policy-<profile>.yaml` per coworker profile, in the OpenShell
policy grammar the `openshell sandbox create --policy` CLI accepts — top-level
`version`, `filesystem_policy`, `landlock`, `process`, and `network_policies` (an
earlier flat `egress: {allow: […]}` shape is rejected with `unknown field egress`). Its
base `network_policies` allow-set is **two endpoints** (the OSH-F64 extension below may
append a validated third), taken from the spec's `egress` block:

```yaml
egress:
  proxy_addr: "172.17.0.1:18255"                    # the OneCLI DATA-PLANE hop (allowed; DNAT alias of onecli-lego:10255)
  inference_route: "inference-api.nvidia.com:443"   # the model inference route (allowed)
  sandbox_image: "osh-f63-base:trixie"              # the --from image (broker-pinned; Debian trixie, glibc 2.41)
```

**In-sandbox egress goes through the substrate proxy.** Every OpenShell sandbox has
`HTTP_PROXY=http://10.200.0.1:3128` set inside it, and in-sandbox clients reach the
declared hops (the OneCLI data-plane hop `172.17.0.1:18255`, the inference route) **only**
through that proxy — a direct connection to `172.17.0.1` is **not routable** from a
sandbox. The policy's declared endpoints stay the target addresses
(`172.17.0.1:18255` + the inference route) with `protocol: rest`; the proxy is the egress
*path*, not a policy endpoint. `172.17.0.1:18255` is the OneCLI **data-plane** hop (a
source-preserving DNAT alias of `onecli-lego:10255`): the OpenShell substrate (0.0.72)
hard-blocks `10255` as a control-plane port, so the data plane is `18255` (the **podman**
substrate is unchanged at `10255`).

The ssh path is **not** an egress target — ssh arrives INBOUND through the OpenShell
proxy socket, not outbound — so the ssh control endpoint is not in the allow-set. Per
profile the render emits:

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

Nothing else is allowed: **no wildcard host**, the OneCLI control plane
(`172.17.0.1:10256`) is never an endpoint, and the OpenShell gateway control plane
**`host.openshell.internal:8080`** is never an endpoint — denying that gateway endpoint
to the policy-constrained worker is exactly what makes route (a) admissible (a worker
must not reach the gateway to list or connect to sibling sandboxes; AC-OSH-F63-5 step 4
proves that denial on-box). The `POST` method is the coworker's real traffic to both
REST endpoints; the exact per-endpoint verb/path is proven on-box by AC-5, not frozen
into the hermetic AC-2 check. The render writes this file as `policy-<profile>.yaml`
beside the profile's config (carried into the installed profile via `distribution_owned`)
— the **canonical allow-set** the provisioning plan names on its bare
`--policy policy-<profile>.yaml` argument. **AC-OSH-F63-5 validates this emitted file
unchanged:** if the pinned OpenShell CLI rejects it (the sandbox never reaches Ready under
`openshell sandbox create --policy`) or fails to enforce it — admitting the in-policy POST while denying
`host.openshell.internal:8080` — the criterion FAILS and the renderer must be corrected;
do not regenerate or hand-edit the policy during verification.

**OSH-F64 / OpenShell 0.0.72 extension (additive, optional).** A spec MAY carry three
optional `egress` fields that extend — never replace — the base document above: a
`broker_addr` (`172.17.0.1:18777`) renders as a **third** `network_policies.worker-egress`
endpoint (the OpenShell 0.0.72 broker hop; the OneCLI request hop moves to `172.17.0.1:18255`,
podman keeps `10255`); a `filesystem_read_only` list of exact absolute paths (no wildcard,
no `..`) is **appended** to `filesystem_policy.read_only` as write-denied lanes (the demo
adds `/opt/osh-lane`); and a `binaries` list of absolute paths (a `*` glob permitted, e.g.
`/usr/bin/python3*`) is **appended** to `network_policies.worker-egress.binaries`, which
already carries `/usr/bin/curl` (the demo adds `/usr/bin/python3*` and
`/opt/hermes/.venv/bin/python`).

**Proxy hops render as raw `tls: skip`, the inference route stays `protocol: rest`.** When
the OSH-F64 delta is present (`broker_addr` set), the two OneCLI proxy hops —
`172.17.0.1:18255` (`proxy_addr`) and `172.17.0.1:18777` (`broker_addr`) — render as raw
passthrough endpoints `{host, port, tls: skip}` with **no** `protocol`/`enforcement`/`rules`.
Both are HTTPS-proxy `CONNECT`-tunnel hops (0.0.72 tunnels the sandbox's outbound TLS through
them), so a `protocol: rest` shape with a `POST /**` rule would try to parse and enforce
inside an opaque tunnel and break the hop — the raw `tls: skip` form lets the substrate pass
the tunnel through unmodified (the operator-validated 0.0.72 shape). `inference-api.nvidia.com:443`
is a real HTTP-API endpoint reached over that tunnel, so it **keeps** `protocol: rest`,
`enforcement: enforce`, and its `POST /**` rule. Only `worker-egress.endpoints` changes from
the base document above (the other four sections and the appended `binaries` are as shown
earlier); with the delta present it becomes:

```yaml
# network_policies.worker-egress.endpoints when the OSH-F64 delta (broker_addr) is present:
endpoints:
  - {host: 172.17.0.1, port: 18255, tls: skip}            # proxy_addr  — raw CONNECT tunnel
  - {host: inference-api.nvidia.com, port: 443, protocol: rest, enforcement: enforce, rules: [{allow: {method: POST, path: /**}}]}
  - {host: 172.17.0.1, port: 18777, tls: skip}            # broker_addr — raw CONNECT tunnel
```

The forbidden set is unchanged: **no wildcard host**, the OneCLI control plane
`172.17.0.1:10256` is never an endpoint, and **outbound** ssh (port 22) is never an endpoint
(ssh is inbound via the proxy socket) — the broker is checked the same as every other allow
entry. A spec absent all three optional fields renders exactly the base two-endpoint document
above with **both** endpoints `protocol: rest` (the `tls: skip` shape is keyed strictly on the
delta being present, never inferred) — the behavioural back-compat contract.

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
WebSocket, so the real run — unlike `--dry-run` — requires `--gateway-url`. The script:

- **Phase A** installs the fleet plugins (`nv-coworker-compose`, `nv-fleet-gates`,
  `podman-onecli`) at the pinned commit so `hermes coworker` exists.
- **Phase B** (`hermes coworker install-openshell`) composes the fleet under
  `substrate: openshell` and provisions **five worker sandboxes** — one per served
  coworker — each Ready under a per-bot `openshell policy` (that bot's APF: exactly the
  three endpoints the spec's `egress` names — the OneCLI request hop, the inference route,
  and the OpenShell-0.0.72 broker hop — the interpreter binaries appended to
  `worker-egress.binaries` (`/usr/bin/python3*`, `/opt/hermes/.venv/bin/python`) on top of
  the inherited `/usr/bin/curl`, and the write-denied `/opt/osh-lane` read-only lane, and
  nothing else; neither the control plane `:10256` nor outbound ssh `:22` is ever allowed).
- It also installs the fleet plugins under **each** served profile (a profile
  distribution excludes plugins, so the veto must be installed per profile or it is
  absent there).
- It **edits the existing default profile in place**, taking a deterministic **backup**
  of the original `$HERMES_HOME/config.yaml` FIRST — before any `plugins install --enable`
  rewrites it — and writing the managed fragment to the managed dir as a separate file.
  It must **never run `profile install default`**: `get_profile_dir("default")` resolves
  to `$HERMES_HOME` itself (the user's running Hermes), so the default is edited, never
  reinstalled.
- It creates the fleet rooms and wires, then restarts the gateway via the shipped
  `hermes gateway restart`.
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
  Phase A (this reverts the in-place default edit).
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
