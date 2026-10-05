# Fleet MCP scope — per-role allow-list & server registry

In a one-gateway Bot-Mode fleet, every coworker is a **profile** of the shared
gateway, and MCP servers are discovered **once at startup from the launch
(DEFAULT) profile** into a process-global registry. That means a secondary
profile's own `mcp_servers` block is never discovered in-gateway, and
`tools.include` in a coworker's config is a registration-time filter of the
launch profile — not a per-message, per-profile filter. GOV-F27 gives each role
its own MCP scope anyway, and makes it **non-bypassable**, with two cooperating
pieces the `nv-coworker-compose` plugin renders and the `nv-fleet-gates` veto
enforces:

1. **Declaration** — compose renders, per profile, an `mcp_servers` block
   containing only that role's servers, and a validated **union** of all roles'
   servers on the DEFAULT/launch profile so the shared gateway can register every
   tool the fleet needs.
2. **Enforcement** — compose also renders a profile-keyed **`mcp_scope`** policy
   into every profile's `nv-fleet-gates` settings, and the single
   `pre_tool_call` predicate denies any `mcp_*` call whose tool is absent from
   the calling profile's allow-list. Widening your own `config.yaml` buys
   nothing: the veto reads the rendered policy keyed by the **process identity**
   (`HERMES_HOME`'s basename), not a value a bot can raise — and in this fleet a
   coworker cannot reach its own `config.yaml` from its sandbox anyway.

## Declaring a role's MCP servers

Add an `mcp:` block to a coworker **type** in `coworker-types.yaml`. Each entry
is a canonical server id (which becomes the `config.yaml mcp_servers` key) mapping
to a stanza:

```yaml
types:
  reviewer:
    mcp:
      docs-ro:                                   # canonical server id
        url: https://<onecli-gateway-host>/mcp/docs   # remote ⇒ transport "remote"
        include: [search_docs]                   # the EXACT server-native tool set
        # trust: full        # OPTIONAL — omit ⇒ rendered trust: untrusted (fail-safe)
        # resources: true     # OPTIONAL — omit ⇒ tools.resources: false
        # prompts: true       # OPTIONAL — omit ⇒ tools.prompts: false
  fixer:
    mcp:
      repo-rw:
        url: https://<onecli-gateway-host>/mcp/repo
        include: [create_issue, create_pr]
```

A **stdio** server declares `command`/`args` instead of `url` (transport derived
as `stdio`). Exactly one of `url` or `command` must be present.

### What the render guarantees

For every scoped server, the rendered stanza carries:

- the **connection fields verbatim** — `url`/`headers`/`transport`/`ssl_verify`
  for remote, `command`/`args`/`env` for stdio (so SSE, auth, TLS and lifecycle
  survive unchanged; only the four policy fields below are render-managed);
- `tools.include` = the role's exact tool list (**glob metacharacters are
  rejected** at render — the veto's exact-name map cannot represent a glob);
- `tools.resources: false` and `tools.prompts: false` unless the role explicitly
  set them `true` (core enables these utility tools by default, so writing them
  false closes the utility bypass);
- `trust: untrusted` unless the role explicitly declared `trust: full` (core
  defaults an **absent** trust to the permissive `full`, so writing `untrusted`
  explicitly is what makes write-capable tools escalate to the approval gate).

The **DEFAULT** profile receives the union: the union of includes for a server
declared by more than one role, the OR of `resources`/`prompts`, and `trust: full`
only if **every** declaration is `full`. Each coworker keeps its exact subset,
while the DEFAULT registers the union so every needed tool exists process-wide.

### Fail-closed render validation

`hermes coworker compose` raises `CompositionError` (nothing is written) on:

- a sanitized-name **collision** (two server ids that sanitize to the same
  `mcp__…` prefix);
- a **shared server id with divergent connection identity** across roles (use
  distinct ids, or align the connection);
- a **glob** metacharacter (`* ? [ ]`) in any `include`;
- a missing/non-list `include`, or a non-string include entry;
- an **ambiguous transport** — neither/both `url`+`command`, or a declared
  `transport:` whose class disagrees with the connection;
- a non-global **`${VAR}`/`${env:VAR}`** env-ref in any `mcp_servers` value. A
  non-global secret read raises at unscoped multiplex startup and turns the
  **entire** MCP registry empty process-wide, so it is refused. Native context
  refs (`${workspaceFolder}`, `${userHome}`, `${workspaceFolderBasename}`,
  `${pathSeparator}`, `${/}`) and refs that resolve to a genuinely-global
  deployment var are exempt. Authenticated remote MCP uses an OneCLI-proxied base
  URL whose real per-profile credential is injected at egress — it needs no
  startup secret read.

## Enforcement at call time

The `nv-fleet-gates` `pre_tool_call` predicate, keyed by the calling profile:

- **blocks** an `mcp_*` tool whose sanitized name is not in that profile's
  `mcp_scope` (and blocks entirely when the profile is absent from the map —
  fail-closed);
- under `enforce_sandbox: true`, additionally **blocks** an allow-listed server's
  tool whose transport is not `remote` (stdio or unknown): only remote MCP,
  controlled by the OneCLI egress proxy, may proceed under sandbox enforcement;
- otherwise **permits** the call, which then proceeds to the stock trust gate —
  where a write-capable tool on an `untrusted` server escalates to approval.

## Mapping `ncl`'s mcp-tools policy states

| operator intent | how GOV-F27 expresses it |
|---|---|
| grant a role a read verb | list the exact read tool in that role's `include` |
| grant a role a write verb | list the exact write tool in that role's `include`; leave `trust` unset so it renders `untrusted` and the write escalates to approval |
| a fully-trusted first-party server | declare `trust: full` on that server for that role |
| deny a tool | omit it from `include` — the veto denies any `mcp_*` not listed |
| deny a whole role all MCP | give the role no `mcp:` block — its scope renders `{}` and every `mcp_*` is denied |

## Deploying a policy change

`hermes profile update` preserves an existing `config.yaml` unless
`--force-config`. A changed MCP policy therefore takes effect only after
`hermes profile update --force-config` (or a reinstall). Re-run
`hermes coworker compose` and reinstall after a pin move so the rendered scope is
re-asserted against the pinned tree.

## Residuals (honest limits)

- **Per-profile managed scope.** Managed (OS-immutable) scope is machine-wide
  only — it has no per-profile dimension — so per-role MCP scope cannot be pinned
  OS-immutable per profile today. Its immutability rests on the per-profile
  sandbox (a bot cannot reach its own `config.yaml`) plus this veto. A per-profile
  managed dimension is an upstream ask.
- **Gateway-process MCP egress.** MCP clients run **in the gateway process**, so
  their egress does not traverse the profile's tool sandbox hop. Attribution of a
  remote MCP call to the right profile's credential rides the context-local
  secret scope (fail-closed under multiplex) carrying that profile's OneCLI agent
  token; the credential itself is injected by the OneCLI substrate at egress, not
  read from config at startup.
- **`readOnlyHint` is server-supplied.** The trust gate's write/read
  classification of an untrusted server's tools uses the server's own
  `readOnlyHint` annotation captured at discovery; a server that mislabels a
  write tool as read-only is a residual the trust tier cannot close on its own.
