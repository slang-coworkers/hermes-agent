# Fleet deployment (P6): five sandboxed profiles on one gateway

This runbook stands up the coworker fleet shape the port targets: **one** Bot-Mode
Hermes gateway on the DEFAULT profile serving five coworkers — orchestrator,
architect, builder, tester and reviewer — each with its tool calls in its own
rootless podman sandbox, supervised as a systemd service, wired for gated bot-to-bot
coordination, with credential-free sandboxes. It is the deployment counterpart of the
`nv-coworker-compose`, `nv-fleet-gates` and `podman-onecli` plugins; nothing here is a
second gateway, a second port, or a2a between gateways.

## 1. Render the fleet

`hermes coworker compose <spec> --out <dir>` renders six distributions — `default`
plus one per coworker type — and a machine-wide `managed/` fragment:

```bash
hermes coworker compose fleet/coworker-types.yaml --out /srv/fleet/render
```

The spec's top-level **`substrate:`** field selects the sandbox backend. `podman`
(the default when the key is absent, for back-compat with existing specs) renders
`terminal.backend: docker` driven through the podman-onecli wrapper; an unknown value
fails closed before any distribution is written. `openshell` is the documented FUTURE
value (P7, OSH-F63/F64): where podman cannot run nested, each profile gets an
OpenShell-native sandbox via `terminal.backend: ssh` with egress policed by
`openshell policy`. Only `podman` ships today.

The render enforces the fleet invariants regardless of what the spec declares:

- **DEFAULT is the multiplexer**: `gateway.multiplex_profiles: true`,
  `gateway.multiplex_profile_allowlist` = exactly the five coworkers.
- **Supervision floor**: `gateway.systemd_watchdog_seconds` is floored to **≥ 30 s** on
  the DEFAULT profile (so `hermes gateway install` emits `Type=notify` + `WatchdogSec`,
  not `Type=simple`). A below-floor or absent spec value is raised to 30; a higher value
  is kept.
- **Per-profile sandbox**: every coworker renders `terminal.backend: docker`, a pinned
  `terminal.docker_image`, policed `terminal.docker_volumes` (every host source outside
  `$HERMES_HOME` and every profile dir), `terminal.docker_shared_container_key: ""`
  (per-profile container identity), the OneCLI egress env, and `--memory`/`--pids-limit`.
- **The veto's settings**: `plugins.entries.nv-fleet-gates.settings` carries
  `enforce_sandbox: true`, `expected_backend` (render-derived from the substrate, so it
  cannot drift from `terminal.backend`), an absolute `edges_db_path` outside every home,
  and `admin_tools: [onboard_coworker, onboard_project, create_agent]` (unioned by the
  veto with its vendored `{cronjob_manage}`).
- **The role map** lives in `managed/config.yaml` at
  `plugins.entries.nv-fleet-gates.settings.profile_roles` — `{orchestrator: orchestrator,
  <others>: worker}` — at the operator-owned managed scope, so a worker cannot rewrite
  its own role by editing its per-profile config.
- **Per-profile OneCLI identity**: `secrets.onecli.enabled: true` on each coworker (the
  DEFAULT launch/multiplexer host makes no direct model call, so it is left disabled).

## 2. Install and supervise the one gateway

```bash
# The DEFAULT (launch/multiplexer) profile IS the gateway home: install its rendered
# config.yaml into HERMES_HOME, or the gateway boots your existing default config with
# no multiplexing and no watchdog.
install -D -m 0600 /srv/fleet/render/default/config.yaml "${HERMES_HOME:-$HOME/.hermes}/config.yaml"
[ -f /srv/fleet/render/default/SOUL.md ] && \
  install -D -m 0644 /srv/fleet/render/default/SOUL.md "${HERMES_HOME:-$HOME/.hermes}/SOUL.md"

# Install the five coworker distributions as named profiles.
hermes profile install /srv/fleet/render/orchestrator --name orchestrator -y
# … architect, builder, tester, reviewer likewise …

# Install the managed fragment so the veto reads the role map.
sudo install -D -m 0644 /srv/fleet/render/managed/config.yaml /etc/hermes/config.yaml
# (or point HERMES_MANAGED_DIR at a dir holding it, and invalidate the managed cache)

# Supervise the ONE gateway as a systemd Type=notify service on the DEFAULT home.
hermes gateway install     # emits Type=notify + WatchdogSec=<n>s + Restart=always …

# `hermes gateway install` emits a FIXED Environment= set (HOME/USER/PATH/VIRTUAL_ENV/
# HERMES_HOME/HERMES_SUPERVISED_CHILD) and passes no other vars through, so the
# podman-onecli wrapper coordinates — deployment config, never rendered into a profile —
# are applied as a systemd drop-in override, not expected on the generated unit:
dropin="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/hermes-gateway.service.d"   # system unit: /etc/systemd/system/hermes-gateway.service.d
install -d -m 0700 "$dropin"
cat > "$dropin/10-podman-onecli.conf" <<'EOF'
[Service]
Environment="HERMES_DOCKER_BINARY=/opt/hermes/plugins/podman-onecli/bin/podman-onecli-wrap"
Environment="PODMAN_ONECLI_PODMAN=/usr/bin/podman"
Environment="CONTAINER_HOST=unix:///run/user/1001/podman/podman.sock"
Environment="PODMAN_ONECLI_EXPECTED_PROXY=172.17.0.1:10255"
Environment="PODMAN_ONECLI_EXPECTED_CA=/etc/ssl/certs/hermes-egress-ca.crt"
# ANTHROPIC_API_KEY is the provider placeholder every coworker forwards; systemd has no
# inline comments, so this note stays on its own line above the assignment.
Environment="PODMAN_ONECLI_ALLOWED_ENV=ANTHROPIC_API_KEY"
EOF
chmod 0600 "$dropin/10-podman-onecli.conf"
systemctl --user daemon-reload
systemctl --user enable --now hermes-gateway

# Confirm the wrapper coordinates actually landed on the running unit (expect 6):
systemctl --user cat hermes-gateway.service | \
  grep -cE '^Environment="(HERMES_DOCKER_BINARY|PODMAN_ONECLI_[A-Z_]+|CONTAINER_HOST)='
```

`reconcile_profile_gateways` (with `GATEWAY_MULTIPLEX_PROFILES=1`) starts **only** the
default gateway and registers each coworker as served-never-started, even if a
coworker's own state says running — one process serves the whole fleet.

Linux uses systemd (`Type=notify`, `WatchdogSec`, `Restart=always`,
`RestartForceExitStatus=75`, `RestartPreventExitStatus=78`); macOS uses launchd
`KeepAlive`. The base profile is always served under the literal name `default`, never
its `-p` name.

### Shared-filesystem `$HERMES_HOME` for a remote rootless podman daemon

When the podman daemon runs rootless as a **separate host user over a socket** (the
fleet's sandbox topology), a `-v host:container` bind **source resolves on the server's
filesystem, not the gateway's**. `DockerEnvironment` emits `$HERMES_HOME`-relative bind
sources — the per-profile sandbox `home`/`workspace` via `get_sandbox_dir()`
(`tools/environments/docker.py:1016-1030`), the cache mounts
(`tools/credential_files.py:437-470`), the local skills mount (`:247-273`) — none
config-gated, and there is no remote-daemon source-root remap. So the gateway user's
`$HERMES_HOME` must resolve at the **same absolute path** on both the gateway and the
rootless-podman-server user (a shared / ACL'd filesystem, both uids rwx), and
`TERMINAL_SANDBOX_DIR` must be left **unset** so the sandbox dirs stay under that shared
`$HERMES_HOME`. Put `$HERMES_HOME` on the shared path (e.g. an ACL-shared
`/var/lib/hermes/fleet/home`), not a gateway-only home; a home the podman-server user
cannot see fails `podman run` with an exit-125 `statfs` on the first sandbox bind. The
operator-provisioned `docker_volumes` sources (`/data/hermes-fleet/<role>/workspace`,
`/opt/hermes-fleet/shared-learnings`, `/opt/onecli/ca.crt`; §§1, 3) live outside
`$HERMES_HOME` and are provisioned server-side directly.

## 3. Per-profile OneCLI identity + credential-free sandboxes

The podman sandbox reaches the network only through the fleet's OneCLI egress proxy.
`hermes onboard coworker` installs the distributions but does **not** provision OneCLI
identities; each per-profile identity + container-config is provisioned by
`hermes onecli-onboard`, which reads
`plugins.entries.podman-onecli.settings.profile_secret_sets` (declared in the spine) to
decide the grant. An empty list `[]` onboards the identity WITHOUT a secret, so the agent
starts ungranted:

```bash
HERMES_HOME=$HOME/.hermes/profiles/architect hermes onecli-onboard --profile architect
```

Verify the container-config resolved with the probe `GET /v1/container-config?agent=<id>`
(a "not configured" response means the onboard did not complete against the control
plane). **Grant the inference secret as a SEPARATE, later step — never at onboard —
because grant order matters for the acceptance run:** `AC-CRED-F28-2` requires its two
profiles (architect, builder) UNGRANTED at entry, so grant the full fleet only AFTER that
check has run (the scenario files encode this ordering):

```bash
onecli agents set-secrets architect ANTHROPIC_API_KEY   # staged by the scenarios, post-AC-CRED-F28-2
```

Each sandbox then carries only proxy swap tokens and the read-only CA mount — no real
provider credential. A `curl` through the proxy reaches the model API; a direct
`--noproxy` call is refused at connect (the operator uid-1001 egress rule); the OneCLI
control plane and tenant API are unreachable from the sandbox.

### The keyless bridge control plane

When the OneCLI control plane runs on an **unauthenticated** bridge endpoint
(`http://172.17.0.1:10256`), set both the base URL and the exact-origin keyless
allowance, and leave the bootstrap key unset:

```yaml
plugins:
  entries:
    podman-onecli:
      settings:
        gateway_api_base_url: "http://172.17.0.1:10256"
        insecure_no_auth_origins: ["172.17.0.1:10256"]   # exact host:port; keyless only
```

See the [podman-onecli plugin](../developer-guide/podman-onecli-plugin.md) for why the
allowance is exact-origin (the proxy `aoc_` token rides as URL userinfo).

## 4. Coordination + the single veto

Bot-to-bot coordination is `message_agent` **inside** the one gateway (Bot Chat, rooms,
`@mentions`); a2a is only for cross-gateway peering and is not used here. `hermes wire
add` provisions the review-pipeline edges (orchestrator↔architect, architect↔builder,
builder↔tester, tester↔reviewer, reviewer↔orchestrator).

The single `nv-fleet-gates` `pre_tool_call` veto denies, per profile: `local` and every
non-sandbox backend; the fleet-admin tools to any non-orchestrator profile; and any
`message_agent`/`kanban_create` to an unwired target. `message_agent` itself is exempt
from the sandbox predicate (its delivery is host-local and consumes no sandbox), so
served (non-launch) profiles can coordinate on the one gateway while all tool-bearing
work still runs sandboxed as each profile's own subprocess.

## 5. The fleet edges store (`edges_db_path`)

The render pins an absolute `edges_db_path` outside every profile home so the
process-shared edges DB has one owner. For a persistent production deployment set it to
an operator-owned durable path (e.g. `/var/lib/hermes/fleet/edges.db`) rather than an
ephemeral `/tmp` location, which is cleared on reboot. The committed test fixtures use a
`/tmp` path because the hermetic tests and the sandbox scenarios need it writable and
outside `$HERMES_HOME`; that is a test/scenario choice, not a production recommendation.

## 6. Substrate pluggability

The substrate is a data switch (§1). The veto, the per-profile identity, and the mount
and egress **policy** are substrate-agnostic; only the `terminal.*` key **names** the
render serializes into, and the podman wrapper, are substrate-specific. Adding a future
column (e.g. `openshell`) is a new substrate-descriptor entry plus its fixtures — the
veto, identities, and mount/egress policy are unchanged.
