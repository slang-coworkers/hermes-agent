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
  `pre_tool_call` veto admits an own-profile ssh call and denies `local`,
  cross-profile, an unready backend, and stdio MCP — unchanged.
- a per-profile ssh block: `terminal.ssh_host` (the sandbox name `<fleet>-<profile>`,
  distinct per profile), `terminal.ssh_user`, `terminal.ssh_port`, and
  `terminal.ssh_key` — a **path** under the gateway `$HERMES_HOME` (never key
  material; the builtin ssh backend passes it as `ssh -i <path>`).
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

The render emits one `policy-<profile>.yaml` per coworker profile. Its allow-set is
**exactly three endpoints**, taken from the spec's `egress` block:

```yaml
egress:
  proxy_addr: "172.17.0.1:10255"          # the OneCLI request hop (allowed)
  inference_route: "inference-api.nvidia.com:443"   # the model inference route (allowed)
  ssh_control_path: "172.17.0.1:22"        # the ssh control path (allowed)
  sandbox_image: "localhost/hermes-openshell-sandbox:pinned"   # the --from image for provisioning
```

renders, per profile:

```yaml
# policy-builder.yaml
egress:
  allow:
    - "172.17.0.1:10255"
    - "inference-api.nvidia.com:443"
    - "172.17.0.1:22"
```

Nothing else is allowed: **no wildcard**, and the OneCLI control plane
(`172.17.0.1:10256`) is never in the allow-set (only the request hop `:10255` is).
`OPENSHELL_ENDPOINT` is added only if the policy grammar requires it for the sandbox's
own control connection (an on-box question, established at provisioning), and even then
never as a tool-egress allow. The render writes this `egress.allow` file at
`<profile>/policy-<profile>.yaml` — the **canonical allow-set** the provisioning plan
names on its `--policy` argument. Where the pinned `openshell policy` CLI expects a
different on-box grammar, the operator regenerates the file **at that same path** with
the **same three endpoints** before running the plan, so the `--policy
<profile>/policy-<profile>.yaml` argument always names the file that is applied.

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
   by `terminal.ssh_key` provisioned with the sandbox's key.

## Provisioning plan and teardown

`hermes coworker compose <spec> --provision-dry-run` renders the fleet and then prints
a deterministic provisioning plan (identical across runs, `--policy` paths relative to
the render `--out` root) — the create, policy-set, and ssh-config lines per profile,
then the teardown lines:

```
openshell sandbox create --name <fleet>-<profile> --from <pinned image> --policy <profile>/policy-<profile>.yaml
...
openshell policy set <fleet>-<profile> --policy <profile>/policy-<profile>.yaml
...
openshell sandbox ssh-config <fleet>-<profile>
...
openshell sandbox delete <fleet>-<profile>
openshell policy delete <fleet>-<profile>
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
