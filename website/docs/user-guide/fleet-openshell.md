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
- a per-profile ssh block: `terminal.ssh_host` (the sandbox name `<fleet>-<profile>`,
  distinct per profile), `terminal.ssh_user`, `terminal.ssh_port`, and
  `terminal.ssh_key` — a **path** under the gateway `$HERMES_HOME` (never key material;
  in the fleet testbed that root is under `/workspace/extra/hermes-fleet-testbed/`, never
  `/opt`). On the OpenShell lane `openshell sandbox ssh-config` emits `User sandbox` + a
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
the OpenShell gateway endpoint (`OPENSHELL_ENDPOINT`) for the worker's tool egress.**
The mounted mTLS identity is gateway-admin over *all* siblings, so a worker that could
reach `OPENSHELL_ENDPOINT` could list/create/connect to sibling sandboxes — breaking
profile isolation (topology rule 3, a MUST). The per-profile policy therefore allows
**exactly** the OneCLI request hop, the inference route, and the ssh control path, and
**`OPENSHELL_ENDPOINT` is never a tool-egress allow.** AC-OSH-F63-5 proves this at
runtime with a worker-side negative control: from inside a policy-constrained sandbox,
an attempt to `sandbox list` / connect to a sibling via `OPENSHELL_ENDPOINT` must be
**denied** (nonzero exit, no sibling metadata). A worker that *can* reach a sibling is
an unconditional isolation failure — the row falls back to route (b) or a genuinely
`<prefix>-*`-scoped OpenShell identity, which must be *implemented* (not merely
documented) before the row can pass.

**Operator gateway credentials must never be placed inside a worker sandbox** (nor in
`<prefix>-gw` / `brev-hermes`): they are admin over every sibling. Route (a)'s safety
rests on the policy denying the gateway endpoint, not on the identity being scoped.

## Egress policy: one `openshell policy` per profile

The render emits one `policy-<profile>.yaml` per coworker profile. Its allow-set is the
endpoints the spec's `egress` block names — the OneCLI request hop, the inference route
and the ssh control path — plus, on OpenShell 0.0.72, the optional `broker_addr` when the
spec declares it:

```yaml
egress:
  proxy_addr: "172.17.0.1:18255"          # the OneCLI request hop (allowed) — OpenShell 0.0.72; podman keeps 10255
  inference_route: "inference-api.nvidia.com:443"   # the model inference route (allowed)
  ssh_control_path: "172.17.0.1:22"        # the ssh control path (allowed)
  broker_addr: "172.17.0.1:18777"          # OPTIONAL (OpenShell 0.0.72): the broker hop, appended to the allow-set
  filesystem_read_only: ["/opt/osh-lane"]  # OPTIONAL: write-denied lanes -> filesystem_policy.read_only
  sandbox_image: "localhost/hermes-openshell-sandbox:pinned"   # the --from image for provisioning
```

renders, per profile:

```yaml
# policy-builder.yaml
egress:
  allow:
    - "172.17.0.1:18255"
    - "inference-api.nvidia.com:443"
    - "172.17.0.1:22"
    - "172.17.0.1:18777"
filesystem_policy:
  read_only:
    - "/opt/osh-lane"
```

The two OpenShell-0.0.72 fields are **additive and default-absent**: a spec without
`broker_addr` and `filesystem_read_only` renders the OSH-F63 shape unchanged — exactly the
three-slot `egress.allow` and no `filesystem_policy` block (the behavioural back-compat
guarantee). Nothing else is allowed: **no wildcard**, and the OneCLI control plane
(`172.17.0.1:10256`) is never in the allow-set (only the request hop — `:18255` on
OpenShell 0.0.72, `:10255` on podman — and, when declared, the broker hop `:18777`, are).
`OPENSHELL_ENDPOINT` is added only if the policy grammar requires it for the sandbox's
own control connection (an on-box question, established at provisioning), and even then
never as a tool-egress allow. The render writes this `egress.allow` file as
`policy-<profile>.yaml` beside the profile's config (carried into the installed profile
via `distribution_owned`) — the **canonical allow-set** the provisioning plan names on
its bare `--policy policy-<profile>.yaml` argument. **AC-OSH-F63-5 validates this emitted
file unchanged:** if the pinned OpenShell CLI rejects it (`openshell policy set` /
`prove`), the criterion FAILS and the renderer must be corrected — do not regenerate or
hand-edit the policy during verification.

## Operator prerequisites

Before a fleet can be provisioned, the operator must satisfy these prerequisites:

1. A **name-prefixed OpenShell access path** reachable from the provisioning host — a
   scoped way to run `openshell sandbox create|exec|delete|ssh-config`,
   `openshell policy set|get`, and `openshell logs` against the gateway, restricted to
   `<prefix>-*` sandboxes. The gateway admin credentials are admin over ALL sandboxes
   and **must never** be mounted into the provisioning host, a worker sandbox,
   `<prefix>-gw`, or `brev-hermes`.
2. Confirmation of which `openshell logs --source` (gateway vs sandbox) records a
   policy denial, and whether `openshell policy` governs the sandbox's own mTLS control
   connection to `OPENSHELL_ENDPOINT` (this decides whether route (a) is admissible).
3. A **pinned `--from` image**: replace the placeholder `sandbox_image` with the exact
   immutable image reference (a digest) for the target platform. For route (a) a
   community image suffices; for route (b) the image must ship `sshd`.
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
`--out` path appears) — the create, policy-set, and ssh-config lines per profile, then
the teardown lines. Each `--policy` names the bare `policy-<profile>.yaml` (the operator
materialises it at that name from the profile's `distribution_owned` before running the
plan), and each create `--name` is the sandbox alias == that profile's rendered
`terminal.ssh_host` (so create / ssh-config / delete all target the same sandbox):

```
openshell sandbox create --name <fleet>-<profile> --from <pinned image> --policy policy-<profile>.yaml
...
openshell policy set policy-<profile>.yaml
...
openshell sandbox ssh-config <fleet>-<profile>
...
openshell sandbox delete <fleet>-<profile>
openshell policy delete policy-<profile>.yaml
```

The plan runs **no** container engine. The teardown lines delete each per-profile
sandbox and its policy, freeing that profile's resources.

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
  endpoints the spec's `egress` names — the OneCLI request hop, the inference route, the
  ssh control path, and the OpenShell-0.0.72 broker hop — plus the write-denied
  `/opt/osh-lane` read-only lane, and nothing else; the control plane `:10256` is never
  allowed).
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
- **Delete** the `osh-f64-*` worker sandboxes and their policies (`openshell sandbox
  delete` + `openshell policy delete`), and the `osh-f64-gw` gateway sandbox when it was
  a throwaway.

### What the NemoClaw dashboard / APF shows afterwards

The NemoClaw dashboard shows one running worker sandbox per served coworker, and its APF
view shows one `openshell policy` per sandbox — the four allowed endpoints per bot plus the
`/opt/osh-lane` read-only lane, with an off-policy egress attempt denied and logged. The whole fleet is one gateway on one
bound port; the per-profile `hermes -p <profile> chat` helper processes bind no external
port.
