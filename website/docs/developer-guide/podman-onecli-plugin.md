# podman-onecli: OneCLI credential gateway

The `podman-onecli` plugin gives every fleet **profile** its own OneCLI egress
identity. A profile's rootless-podman sandbox routes outbound HTTPS through the
OneCLI proxy, which injects that profile's credential **at request time** — so
no credential value ever lives in the sandbox. The plugin is a standalone
directory plugin and uses only the public plugin surface: a SecretSource, an
`on_session_start` re-assert, the `onecli-onboard` CLI command, and a shipped
podman wrapper. It adds no core hook, no `pre_tool_call` veto, and no
`TerminalEnvironmentProvider`.

## What it registers

| Surface | Purpose |
|---|---|
| `onecli` SecretSource | Resolves a profile's OneCLI **proxy coordinates** (proxy URLs carrying the `aoc_` agent token as userinfo, CA-trust vars, CA path) plus a placeholder provider key — never a real provider credential. |
| `on_session_start` hook | Idempotently re-asserts the **launch profile's** OneCLI identity. It acts only when the active profile is a real named profile (never `default` / `custom`) and fails open. It is a re-assert, not the bootstrap. |
| `onecli-onboard` CLI | Ensures a profile's OneCLI agent and its selective secret grants **before render**, in the order OneCLI requires. |
| `bin/podman-onecli-wrap` | The `HERMES_DOCKER_BINARY` shim that fails closed on a misconfigured long-lived sandbox launch (below). |

## Configuration

Settings live under `plugins.entries.podman-onecli.settings` (never a new
`HERMES_*` variable):

```yaml
secrets:
  onecli:
    enabled: true          # required — the SecretSource is inactive otherwise
plugins:
  enabled: [podman-onecli]
  entries:
    podman-onecli:
      settings:
        gateway_api_base_url: "https://onecli.example"   # OneCLI control-plane base URL
        api_key_env: "ONECLI_API_KEY"                      # host env var holding the bootstrap key
        profile_secret_sets:                               # selective grants; [] = ungranted
          research-bot: ["ANTHROPIC_API_KEY"]
          triage-bot: []
```

The SecretSource only runs at startup when `secrets.onecli.enabled: true` is in
the rendered config (shown above). The bootstrap key named by `api_key_env` (default
`ONECLI_API_KEY`) authenticates the onboard/render step to OneCLI; its **value**
stays in the host environment and is never rendered into a profile.

`gateway_api_base_url` must be `https://` — the bootstrap key rides an
`Authorization: Bearer` header, so the client refuses to build a request against
a cleartext-HTTP base. A loopback host (`127.0.0.1`, `localhost`, `::1`) may use
`http://` for local development only.

### Keyless exact-origin exception (`insecure_no_auth_origins`)

Some deployments run the OneCLI control plane on an **unauthenticated bridge
endpoint** (e.g. a fleet's `http://172.17.0.1:10256`), reached over the docker
bridge without a bootstrap key. Because there is then *no key in the request*, the
TLS requirement that exists to protect the key does not apply. `insecure_no_auth_origins`
is a list of exact `host:port` origins allowed to use `http://` **only when
`api_key_env` names an env var that is unset** (no bootstrap key):

```yaml
plugins:
  entries:
    podman-onecli:
      settings:
        gateway_api_base_url: "http://172.17.0.1:10256"
        insecure_no_auth_origins: ["172.17.0.1:10256"]   # exact host:port; keyless only
        # api_key_env: ONECLI_API_KEY  (left unset -> no Authorization header)
```

The allowance is deliberately **exact-origin, not host-only**: `get_container_config`
returns the proxy coordinates carrying the injected `aoc_` proxy token as URL
userinfo, so a host-only allowance would expose that token in cleartext on *any*
port of the same host. The exact `host:port` bound limits any cleartext to the one
operator-blessed bridge endpoint. The guard still **refuses** `http://` to any
non-loopback, non-allowlisted origin; `http://` to an allowlisted origin when a
bootstrap key *is* set (the key must never ride cleartext); and any base URL that
carries userinfo. `https://` is always accepted. Default is `[]` — the TLS
requirement is unchanged unless an operator opts a specific keyless endpoint in.

## Onboarding

`hermes onecli-onboard --profile <name|path>` runs, in order:

1. `ensureAgent(identifier=<profile>)` — idempotent (`409 → already exists`, no error).
2. `set_secrets(identifier=<profile>, secrets=profile_secret_sets[<profile>])` —
   selective, never mode `all`; an empty list leaves the agent ungranted so an
   unassigned sibling profile still gets `401`.
3. `getContainerConfig(agent=<profile>)` — verifies the identity now resolves.

Every onboarded profile must have a `profile_secret_sets` entry (use `[]` to
leave it ungranted); onboarding refuses to silently grant an unlisted profile.
The compose/onboard step runs this before the gateway process's first
`fetch()`, so a never-run profile does not fail its first render.

## The podman wrapper (fail-closed)

The substrate sets `HERMES_DOCKER_BINARY` to `bin/podman-onecli-wrap`. It is
driven by these environment variables:

| Variable | Meaning |
|---|---|
| `PODMAN_ONECLI_PODMAN` | Real podman binary (default `podman`). |
| `PODMAN_ONECLI_EXPECTED_PROXY` | `host:port` authority the proxy env must carry. |
| `PODMAN_ONECLI_EXPECTED_CA` | Container path the CA must be mounted at (read-only). |
| `PODMAN_ONECLI_EXPECTED_NO_PROXY` | Loopback bypass list a forwarded `NO_PROXY`/`no_proxy` must equal (default `127.0.0.1,localhost,::1`). |
| `PODMAN_ONECLI_ALLOWED_ENV` | Space-delimited extra name-only `-e` vars the render forwards — the **provider placeholder keys**. The proxy/CA/`NO_PROXY` names are always allowed; every other `-e NAME` is refused. |
| `PODMAN_ONECLI_FORBIDDEN_ENV` | Space-delimited host-only credential names that must **never** be forwarded (default `ONECLI_API_KEY`, the control-plane bootstrap key). |
| `PODMAN_ONECLI_LOG` | Optional audit path for accepted-launch and refusal lines. |

On a **long-lived launch** (`run -d` / `create` that starts a persistent
sandbox) the wrapper refuses unless the argv carries a name-only `-e` for every
rendered egress variable (the four proxy vars and the six CA-trust vars), every
proxy value's `host:port` authority equals `PODMAN_ONECLI_EXPECTED_PROXY`, and a
read-only mount to `PODMAN_ONECLI_EXPECTED_CA` is present. It parses the podman
flags **only up to the image** — `-e`/`-v` tokens in the container-command
position (after the image) are never counted as controls — and normalises the
dispatch so alternate invocation forms cannot skip validation: a
`podman container run|create …` sub-noun is treated as `run`/`create`, and a
leading global option before the verb (which Hermes never emits) is refused.
A pre-image `run`/`create` option that the wrapper recognises as neither
value-taking nor boolean is refused fail-closed (so an unknown value-taking flag
cannot consume the image token) — an operator adding an exotic flag via
`terminal.docker_extra_args` must therefore use a form the wrapper knows, and
`--secret` is refused outright.

Two rules keep host secrets out of the sandbox. **Allowlist:** a name-only `-e
NAME` is accepted only for a rendered egress/CA name, `NO_PROXY`/`no_proxy`, or a
provider placeholder the substrate declares in `PODMAN_ONECLI_ALLOWED_ENV`; any
other `-e NAME` is refused (a name-only `-e` pulls its value from the wrapper's
own process env, so an un-listed name could forward a host value). **Denylist:**
the control-plane bootstrap key (`PODMAN_ONECLI_FORBIDDEN_ENV`, default
`ONECLI_API_KEY`) is refused outright. A forwarded `NO_PROXY`/`no_proxy` must
equal `PODMAN_ONECLI_EXPECTED_NO_PROXY` — a broadened or `*` value that would let
the sandbox bypass the proxy is refused (it is never *required*, since the pinned
render may omit it).

> The render's provider placeholder names are the one thing the wrapper cannot
> know a priori; the substrate must list them in `PODMAN_ONECLI_ALLOWED_ENV`
> (e.g. `ANTHROPIC_API_KEY`). An un-declared placeholder fails closed — the
> launch is refused, never leaked.

A refusal is logged with proxy userinfo and any `aoc_` token unconditionally
redacted from every emitted line (stderr and the audit log), exits non-zero, and
never invokes the real podman. The wrapper passes through Hermes's capability
probes verbatim (the cgroup probe and the `--storage-opt` probe), adds `--label
nv.hermes-ppid=$PPID` to accepted launches (so a `sweep` verb can reap sandboxes
whose worker died), and rewrites the `ps` `{{.Label "K"}}` template to the
podman-3.4.4-compatible `{{index .Labels "K"}}`.

## Security posture

No real provider credential exists on the host or in any rendered profile — the
sandbox receives only proxy coordinates, and OneCLI substitutes the credential
at the proxy at request time. The `aoc_` agent token is a per-profile
attribution coordinate carried as proxy userinfo, not a provider credential; the
wrapper redacts it from logs and never persists it.
