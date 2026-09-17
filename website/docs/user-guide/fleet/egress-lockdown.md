---
title: "Fleet egress lockdown (ISO-F14)"
description: "How nv-coworker-compose renders the OPTION-A egress posture so a coworker sandbox reaches the outside world only through the OneCLI credential gateway and never holds a raw credential."
---

# Fleet egress lockdown

Every profile that `nv-coworker-compose` renders **from a spec carrying the required
`egress:` block** (see below) carries a single, uniform **egress posture**: a coworker's
tool sandbox reaches the outside world **only through the OneCLI credential gateway**, and
the sandbox **never holds a raw credential**. This ports the NanoClaw egress guarantee onto
the Hermes fleet. A fleet deployment spec MUST carry the `egress:` block; a spec that omits
it (a dev/test spec) renders with no egress enforcement.

## The OPTION-A topology

At the pinned release (`v2026.8.31` = 0.21.0) a chained Hermes egress hop is not
realizable (iron-proxy has no parent-proxy seam, the sandbox proxy vars are stripped when
the hop is on, and `--add-host …:host-gateway` is rejected by podman 3.4.4). So the render
supplies the egress posture **directly** into each profile's config instead of enabling the
stock iron-proxy apparatus:

- `proxy.enabled: false` — the built-in egress apparatus is a no-op, and its collision
  guards are inert; the render carries its own posture and its own fail-closed assertion.
- `terminal.docker_env` — the OneCLI proxy address on `HTTPS_PROXY` / `https_proxy` /
  `HTTP_PROXY` / `http_proxy` (`http://<proxy_addr>`; a `host.docker.internal` address is
  normalized to the literal `172.17.0.1` docker0 bridge IP, which podman 3.4.4 can route),
  `NO_PROXY` / `no_proxy` = `127.0.0.1,localhost,::1`, and the six CA-bundle variables
  (`SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`,
  `HERMES_CA_BUNDLE`, `DENO_CERT`) = the sandbox CA path. Each provider name (e.g.
  `ANTHROPIC_API_KEY`) carries a **non-secret placeholder** (`onecli-injects-at-request-time`),
  never a real credential — OneCLI injects the real value at request time. The removed v1
  `HERMES_PROXY_TOKEN_*` swap tokens are forbidden outright.
- `terminal.docker_volumes` — the render appends exactly **one** canonical
  `<ca_host_path>:<ca_container_path>:ro` read-only mount for the OneCLI CA, after ISO-F13's
  coworker mount composition has built the profile's closed mount set. The render owns that
  mount: **no** profile may pre-declare a `docker_volumes` entry targeting the CA container
  path, and no composed mount (e.g. an `install_surface` equal to the CA path) may resolve to
  it — such a mount is refused. For a coworker, ISO-F13 already rejects any spec-declared
  `docker_volumes` entry except the shared-learnings clone; the egress pre-pass projects that
  composition onto a copy of every coworker, so a CA-target mount fails before any profile is
  written.
- `terminal.docker_persist_across_processes: false` — no cross-process container reuse (the
  reuse probe uses a `{{.Label …}}` `ps` format broken on podman 3.4.4).
- `terminal.docker_image`, `terminal.container_memory`, `terminal.container_cpu` — the
  pinned fleet sandbox image and cgroup limits.

`proxy.enabled: false` is **also** written into the machine-wide managed-scope fragment
(`out_root/managed/config.yaml`), which the managed layer deep-merges managed-wins onto every
profile at load — so the egress topology cannot be overridden per profile.

## Every fleet spec MUST carry an `egress:` block

The egress parameters live in a top-level `egress:` block in the authoritative fleet
`coworker-types.yaml` (per-substrate, so no new config key or `HERMES_*` env var is
introduced):

```yaml
egress:
  proxy_addr: "172.17.0.1:10255"                           # OneCLI proxy bridge IP (per substrate)
  ca_host_path: "/opt/onecli/ca.crt"                       # host CA copy readable by uid 1001 (deployment provides the FILE)
  ca_container_path: "/etc/ssl/certs/hermes-egress-ca.crt" # sandbox CA path
  provider_env: ["ANTHROPIC_API_KEY"]                      # provider env NAMES -> non-secret placeholder values
  sandbox_image: "hermes-sandbox:latest"                   # ships bash + curl
  container_memory: 1024                                   # MiB pin
  container_cpu: 0.5                                        # cpu pin
```

An **absent** `egress:` block leaves enforcement off (existing dev/test specs render
unchanged). A **fleet deployment** spec MUST carry it — a spec that drops it renders profiles
with no egress posture.

## Fail-closed collision errors

The render owns the egress env, network and CA mount. A spec that tries to set a competing
control is refused with a `CompositionError` **before any profile config is written** (all or
nothing), so no partial fleet lands on disk. The operator-visible messages are of the form
`<profile>: egress collision …`:

- `terminal.docker_env[<KEY>]` — a proxy / no-proxy / CA-bundle / provider name, or any
  `HERMES_PROXY_TOKEN_*` key, present in `docker_env` (matched after a whitespace strip, since
  the runtime strips env names). Remove it; the render owns it.
- `docker_forward_env` / `env_passthrough forwards a controlled egress var (<NAME>)` — a
  controlled env name in either host-env forwarding list.
- `egress collision on the CA mount — a mount targets <ca_container_path>` — a
  `terminal.docker_volumes` entry (or a composed mount, e.g. an `install_surface` equal to the
  CA path) that lands at the CA container path. The render owns the CA mount, so no profile may
  declare one.
- `docker_extra_args must not mount at the CA path <ca_container_path>` — ANY
  `-v`/`--volume`/`--mount` in `docker_extra_args` targeting the CA path, canonical or
  divergent (the render owns that mount via `docker_volumes`, so a duplicate is refused too).
- `docker_extra_args flag <FLAG> is not on the mount-safe allowlist` — **every profile's**
  `docker_extra_args` (the DEFAULT profile included) is default-denied against the mount-safe
  allowlist: only vetted non-mount flags (`--shm-size`, `--memory`, `--cpus`, `--ulimit`,
  `--read-only`, `--init`, …) pass, so an engine-socket `-v`, `--tmpfs`, `--use-api-socket`,
  `--privileged`, `--rootfs`, `--device`, `--cap-add`, or a positional image token is refused —
  closing a second egress route / isolation & image-pin escape on the DEFAULT profile, whose
  runtime egress guards are inert under OPTION A.
- `docker_extra_args …` (egress-specific, on top of the allowlist) — a non-string entry (the
  backend discards it, shifting the argv into a valid override); a `-e`/`--env` in any form
  (separate, `--env=`, or attached `-eKEY`) naming a controlled var; an `--env-file` (its contents
  are opaque at render time, rejected unconditionally); a `--network`/`--net` override; or a
  `--memory`/`-m`/`--cpus` override of the pinned resource limits (`--memory`/`--cpus` ARE on the
  allowlist, so the egress belt is what pins them).

Unrelated `docker_env` keys (a legitimate `LANG`) and mount-safe allowlisted `docker_extra_args`
(e.g. `--shm-size 64m`) are preserved. Mounts are **not** carried in `docker_extra_args` on any
profile — the allowlist rejects every `-v`/`--volume`/`--mount` there; declare mounts
through the fleet's mount surfaces (`workspace_root`, `install_surfaces`, the shared-learnings
clone) instead.

The `egress:` block itself is validated: a provider name that is malformed or names a reserved
egress control is refused (`egress.provider_env must not name a reserved egress control`).

## Scope and what this render does NOT do

- **The sandbox holds no credential, and there is no second *configured* proxy topology.**
- **Host-side egress and network-level exclusivity are deferred to P7/APF.** The single-host
  direct-connect check (a `--noproxy` request is refused) relies on the operator's host egress
  rule; the comprehensive "no packet can leave except via the proxy" proof is a P7 concern.
- **The fail-closed *spawn*, the per-profile OneCLI identity, and the podman-3.4.4 flag /
  `ps`-label normalization are CRED-F28's podman wrapper** (`HERMES_DOCKER_BINARY`), a separate
  plugin. This render guarantees only what it emits into the profile config.
- **ISO-F14 renders the CA mount *entry*, not the CA *file*.** The CA file at `ca_host_path`
  is provided by operator deployment (below) and refreshed by CRED-F28's onboard.

## Operator deployment steps (not row code, ADDENDUM item 9)

These are host actions the operator performs before the fleet runs; they are not produced by
the render:

1. A host egress rule for uid `hermes-sandbox` allowing **only** the OneCLI proxy
   (`172.17.0.1:10255`).
2. cgroup delegation for `user@1001` (`Delegate=cpu cpuset io memory pids`) so the pinned
   limits reach the kernel.
3. The CA copy at `ca_host_path`, readable by uid 1001.
4. `container-init` (catatonit) on the server host.
5. The ppid-sweep cron for the podman wrapper.
