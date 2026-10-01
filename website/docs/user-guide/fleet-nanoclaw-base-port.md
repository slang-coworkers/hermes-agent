---
title: Base-NanoClaw fleet port notes
description: How the NanoClaw base coworker fleet was ported to a Hermes fleet spec, and every NanoClaw-only behaviour that was dropped or changed in the port.
---

# Base-NanoClaw fleet port notes

The `nanoclaw-base` fleet spec (`tests/e2e-scenarios/FLEET-F62.c/spec/openshell/`)
ports the NanoClaw base coworker fleet from `slang-coworkers/nanoclaw@nv-main`
into Hermes fleet-spec form: five coworker types
(orchestrator/triager/fixer/reviewer/approver) plus a non-sandboxed DEFAULT
multiplexer, rendered by the merged `nv-coworker-compose` onto the `openshell`
substrate. The NanoClaw base building blocks are the source
(`container/spines/base` invariants and context, the `base-nanoclaw`,
`codex-critique` and `supervise-issues` skills, and the `base`, `plan`,
`implement` and `triage-issue` workflows), not the existing `nv-hermes` overlay.

Several NanoClaw constructs have no one-to-one Hermes equivalent. The table below
records every dropped or changed behaviour by its source; the prose sections
after it expand on the choices a reader needs to understand before deploying the
fleet.

## Dropped or changed behaviour

| source | changed behaviour |
|---|---|
| base-nanoclaw | The mcp__nanoclaw__* host-tool transport has no one-to-one Hermes equivalent; messaging, scheduling, elicitation and learnings map onto the Hermes gateway's own tools and the skill body is reframed for them. |
| ncl | The NanoClaw host CLI (tasks, sessions, cost-cap) is dropped; Hermes schedules via cron jobs and administers the fleet through the hermes CLI, so ncl-specific guidance is not carried into any SOUL. |
| gate-critique-on-deliver | The NanoClaw PostToolUse critique-on-deliver host hook is not ported as a host hook; the equivalent delivery gate is the nv-fleet-gates critique gate on the Hermes side. |
| gate-chain-routing | The NanoClaw chain-routing PreToolUse host hook is not ported; the equivalent routing veto is the nv-fleet-gates pre_tool_call gate over the bidirectional edge store. |
| track-critique | The NanoClaw critique-tracking host hook is not ported; recorded critique rounds are tracked by the Hermes critique gate rather than a standalone host hook. |
| message | The NanoClaw final-response dispatch semantics do not exist in Hermes; a coworker sends through the gateway's messaging tool, so the message-dispatch guidance is reworded. |
| approver | The approver role has no nv-main base counterpart; it is a new fleet role added for the off-chain review-harvest gate, documented here as an addition rather than a straight port. |
| bot-identity | The triage-issue bot identity is no longer parameterized through NanoClaw project vars; per-role identity comes from each type's ui_meta and the gateway bot identity applied at onboard time. |
| buddy | The buddy companion-monitor skill is not converted or bound; it depends on NanoClaw host hooks that Hermes does not run, so it is dropped from the fleet skill set. |
| explain-diff-html | The explain-diff-html skill is not bound in this fleet; its PR-created host hook is NanoClaw-specific, so its disposition is recorded here rather than shipped as a loadable skill. |
| f64.b-egress | The egress ships the merged F63 shape (proxy 172.17.0.1:18255 plus the inference route); the F64.b target is broker-18777-only with an OpenShell provider placeholder, deferred to the unmerged F64.b renderer. |
| github_comment | The three DEFAULT routes deliver via github_comment, a deliberate deviation from the merged CH-F52 AC-CH-F52-3, justified by the dedicated posting identity; per-event delivery is deferred to the live tier. |
| base | The generic base workflow is converted as a source SKILL.md and validated, but Hermes compose renders only role-bound workflows: it never binds or renders base, and does not inherit it or apply base-targeted overlays to other workflows. |
| plan | The generic plan workflow is converted to Hermes SKILL.md form and bound by the reviewer; its NanoClaw host-tool references are reworded for the Hermes gateway. |
| implement | The generic implement workflow is converted to Hermes SKILL.md form and bound by the fixer; its worktree and CI guidance is kept and its NanoClaw dispatch wording dropped. |
| triage-issue | The generic triage-issue workflow is converted to Hermes SKILL.md form and bound by the triager; its inline gh-api comment shell block is replaced with prose. |
| github-provider-egress | Only the triager, fixer and reviewer attach the OpenShell github provider; their policies gain the api.github.com and github.com endpoints plus the gh and git binaries, and their config declares GH_TOKEN as an unresolved placeholder. The orchestrator, approver and DEFAULT multiplexer attach nothing, and real gh authentication plus GitHub delivery are deferred to the live tier. |

## The `base` workflow is source-only

In NanoClaw the `base` workflow is the implicit splice-parent: a workflow that
declared no `extends` implicitly extended it, and an overlay targeting `base`
reached all of them. Hermes compose does not port that inheritance — it renders
only the workflows a role binds in its `workflows:` list, and no role binds
`base`, so it is never written into a profile and no base-targeted overlay is
spliced into another workflow. It is still converted as `workflows/base/SKILL.md`
and validated as a source deliverable, so the fleet carries a well-formed
splice-parent stub even though it produces no rendered skill.

## Egress: F63 shape now, F64.b later

This row ships the merged OSH-F63 egress: the two allowed endpoints are the
OneCLI data-plane hop `172.17.0.1:18255` and the inference route
`inference-api.nvidia.com:443`, and the render emits five per-coworker openshell
policies (the DEFAULT multiplexer is unsandboxed). The intended F64.b target —
recorded in a `# F64.b-target:` comment in `coworker-types.yaml` — is
broker-18777-only with no `18255` data-plane hop, an OpenShell provider
placeholder base URL `https://inference.local`, and the key rewrite marker
`sk-OPENSHELL-PROXY-REWRITE`. That change needs the unmerged F64.b renderer; on
its merge the egress block and golden regenerate and the policy count moves from
five to six (a gateway policy is added). Setting a different proxy port alone does
not achieve F64.b.

## GitHub ingress ships disabled

The DEFAULT profile declares three repo-level webhook routes (no GitHub App id):
`github-triager` (issues, issue_comment) → the triager, `github-fixer`
(pull_request, push) → the fixer, and `github-reviewer` (pull_request_review,
pull_request_review_comment) → the reviewer. Each delivers via `github_comment`
with a payload-templated `deliver_extra` and an unresolved `${env:...}` HMAC
secret placeholder — never a literal secret and no live URL, host, or port.

The ingress ships **disabled at two levels**: the platform is off
(`platforms.webhook.enabled: false`) and each route is off (`enabled: false`).
Both matter because `WEBHOOK_ENABLED=1` force-enables the platform listener
regardless of the YAML (`gateway/config.py:2348`); the per-route `enabled: false`
still rejects every event with a 403 (`gateway/platforms/webhook.py:673`), and no
env var overrides a route. An unresolved `${env:...}` reference survives config
expansion as a literal, so without the per-route guard a force-enabled listener
would accept a publicly-knowable HMAC key. The fleet therefore only *declares*
the routes; an operator re-enables each route only after provisioning its real
HMAC secret and a bind address. `github_comment` runs a PR comment and needs a PR
number, so it does nothing for the non-PR `issues` and `push` events until then —
per-event delivery and the outbound posting identity are live-tier concerns, not
proven by this hermetic row.

## GitHub provider egress is folded into three workers only

The webhook routes above are GitHub *events coming into* the DEFAULT; the provider
fold is the *egress out of the worker sandboxes* that call GitHub. The fleet spec
declares a generic, default-off `providers:` catalog, and only the three
github-using workers opt in with `providers: [github]`: the triager (posts issue
comments), the fixer (opens PRs and pushes with `gh`/`git`), and the reviewer
(posts PR reviews). This is the same route-bound set as the webhook ingress; the
orchestrator (admin), the approver (off-chain) and the DEFAULT multiplexer
(non-sandboxed) opt into nothing.

For each opted-in worker the renderer (1) emits an
`openshell sandbox provider attach <sandbox> github` line in the provisioning plan
and (2) folds the provider's endpoints (`api.github.com:443`, `github.com:443`),
binaries (`/usr/bin/gh`, `/usr/bin/git`) and HTTP methods into that worker's
`policy-<role>.yaml` — the methods render one allow-rule per verb, so the write
verbs (`POST`, `PATCH`, …) are admitted alongside the base inference route. The
worker's config declares `GH_TOKEN: "${env:GH_TOKEN}"` — an **unresolved
managed-config placeholder, never a literal token** and never a
`terminal.env_passthrough` entry (the sandbox child strips `GH_TOKEN` as a
provider-credential variable, so passthrough would be both ineffective and a
credential-scrubbing weakening). The real credential is injected by the attached
OpenShell provider at the proxy boundary at the **live tier**; this hermetic row
only proves the rendered attach lines, policy endpoints/binaries/methods and the
placeholder are byte-stable. Real `gh` authentication and GitHub delivery are the
later live-e2e row. The `providers:` widening is additive and default-off: a fleet
that declares no provider renders exactly as before.
