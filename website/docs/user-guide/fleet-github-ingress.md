---
sidebar_position: 74
title: "Fleet GitHub ingress (on-call runbook)"
description: "How a Bot-Mode fleet routes GitHub webhook deliveries to the right coworker: the canonical issue/PR/review/check/comment route set rendered into the DEFAULT multiplexer profile by nv-coworker-compose, why routes are static rendered config (not `hermes webhook subscribe`), the /p/<profile>/webhooks/<route> endpoints, fail-closed HMAC auth, the mention gate, X-GitHub-Delivery idempotency, and the github_comment identity caveat."
---

# Fleet GitHub ingress (on-call runbook)

This runbook covers how a Bot-Mode fleet receives GitHub webhook deliveries and
routes each one to the coworker that owns that kind of event: issues to the
orchestrator, pull requests to the reviewer, reviews and checks to the fixer. It
is scoped to fleets rendered by the `nv-coworker-compose` plugin (the DEFAULT
multiplexer profile plus every coworker profile).

GitHub ingress is Hermes-native. The gateway already ships a first-class
**webhook platform** that does per-route HMAC verification, event and payload
filtering, per-route profile binding onto the multiplex URL prefix, delivery
de-duplication, prompt templating, and skill injection — see
[Webhooks](./messaging/webhooks.md) for the platform reference. This runbook does
not add a mechanism; it documents the **canonical GitHub ingress route set** a
Slang-style fleet authors into the DEFAULT profile, and the invariants that keep
that ingress correct and isolated.

## Where the routes live (and why only on the DEFAULT profile)

Webhook routes are authored under the DEFAULT (multiplexer) profile's
`config.yaml` at:

```
platforms.webhook.enabled: true
platforms.webhook.extra.routes:      # a MAPPING keyed by route name
  <route-name>:
    secret: ...
    profile: ...
    events: [...]
    filters: [...]
    prompt: ...
    skills: [...]
```

They belong **only** on the DEFAULT profile. In a multiplexer the DEFAULT
profile owns the single shared HTTP listener and serves every profile through the
`/p/<profile_name>/` URL prefix. A secondary (coworker) profile that enables a
port-binding platform such as `webhook` is a misconfiguration: the gateway
**skips that whole profile at startup** rather than binding a second listener. So
a coworker profile never carries a webhook route or an enabled `webhook` platform
— the compose renderer both refuses coworker webhook routes and forbids a
coworker enabling any port-binding platform.

## The canonical O1 route set

The fleet authors these five routes into the DEFAULT profile's
`default_config.platforms.webhook.extra.routes`. The **executable source of
truth** for this contract is the acceptance fixture
`tests/plugins/fixtures/ch-f52/coworker-types.yaml` (the `CANONICAL_ROUTES`
constant in `tests/plugins/test_ch_f52_github_routing_acceptance.py` pins it and
the test asserts the *rendered* config against it, so this table can never drift
from what actually renders):

| route name | `profile` | `events` | `action` filter admits | mention filter |
|---|---|---|---|---|
| `gh-issues` | orchestrator | `[issues]` | `[opened, reopened]` | — |
| `gh-issue-comment` | orchestrator | `[issue_comment]` | `[created]` | `comment.body` contains the bot mention |
| `gh-pull-request` | reviewer | `[pull_request]` | `[opened, reopened, synchronize, ready_for_review]` | — |
| `gh-pr-review` | fixer | `[pull_request_review]` | `[submitted]` | — |
| `gh-check-suite` | fixer | `[check_suite]` | `[completed]` | — |

Each route additionally carries:

- **Its own `secret`** — a per-route HMAC secret, **pairwise distinct** across
  routes. In production these are env-references resolved from the DEFAULT
  profile's `.env`; never a shared or global secret (the compose renderer refuses
  a route without its own non-empty secret).
- **A `prompt` template** over the payload, referencing fields such as
  `{repository.full_name}`, `{issue.number}`, or `{pull_request.title}`. Payload
  fields are dot-addressable; `{__raw__}` dumps the whole payload.
- **A non-empty `skills` list** — the fleet skills injected for that event class
  (triage, review, CI investigation). The exact skill names are fleet policy;
  substitute your own.

The `action` filter is a declarative `filters` entry
(`{field: action, in: [...]}`); the mention gate on `gh-issue-comment` adds a
second filter `{field: comment.body, contains: "@<your-bot-handle>"}`. Filters in
a list are ANDed. The event→profile split is the fleet's canonical mapping;
per-bot comment routes follow the same shape (one route per bot at its
`/p/<bot>/` prefix, each filtering on that bot's own mention).

## Install path — static rendered config, not `hermes webhook subscribe`

The canonical routes are **static configuration** rendered by
`nv-coworker-compose` into the DEFAULT profile's `config.yaml` from the fleet
spec's `default_config`. They are **not** created with `hermes webhook
subscribe`.

The `hermes webhook subscribe` CLI creates a *dynamic* subscription and is the
right tool for an ad-hoc route on the DEFAULT profile only. On the pinned release
it exposes **no `--profile` flag and no declarative `--filters`**: a dynamic
subscription records `description/events/secret/prompt/skills/deliver` but carries
no `profile` binding and no `action`/mention filters (its `--script` hook can
drop or reshape a payload but cannot bind a profile). A multi-profile, filtered
GitHub ingress therefore **must** be static rendered config. Static routes
override same-name dynamic ones on merge, so the rendered contract is
authoritative.

## Endpoints

A rendered route `<route>` bound to `<profile>` is reachable only at:

```
POST /p/<profile>/webhooks/<route>
```

on the DEFAULT profile's shared listener. A request to a different profile prefix
(`/p/<other>/webhooks/<route>`) or to the bare `/webhooks/<route>` path (no
profile prefix, which resolves to `default`) is rejected `404` — the route is
bound to exactly one profile.

## Authentication fails closed

Every delivery is authenticated before it can dispatch:

- **No effective secret** on the route → `403` (the route is refusing to run
  unauthenticated). A fleet route always has its own secret, so this only fires on
  a misconfiguration.
- **Invalid or absent `X-Hub-Signature-256`** → `401`. GitHub signs the raw body
  with the route's secret (`sha256=` HMAC); a mismatch or a missing header is
  rejected and never dispatches.
- A validly-signed delivery whose **event** is not in the route's `events`, or
  whose payload does not pass the route's `filters` (wrong `action`, missing
  mention), returns `200 ignored` — accepted as authentic, deliberately not acted
  on.

Only a delivery that is signed, event-matched, and filter-matched dispatches an
agent run (`202 accepted`) on the bound profile.

> **Authenticated does not mean trusted.** A valid signature proves the delivery
> came from a party holding the route secret; it does **not** make the payload
> trusted input. Treat every payload field interpolated into a `prompt` (issue
> titles, comment bodies, branch names) as untrusted text.

## Idempotency and per-PR continuity

GitHub retries deliveries. The webhook platform de-duplicates on
`X-GitHub-Delivery`: the first delivery dispatches (`202`); a retry with the same
delivery id returns `200 duplicate` and dispatches nothing. Each delivery is a
one-shot session by design.

**Durable per-PR continuity is not a rewritten session key or an owner table.**
A bot that must remember state across events for the same PR reads and writes the
fleet's kanban ownership card keyed `<repo>#<pr>` (GOV-F25). This runbook builds
no per-PR session mapping and no owner table; that is deliberate.

## The mention gate (and the deferred ownership exemption)

The `gh-issue-comment` route is mention-gated: an `issue_comment` delivery is
dispatched only when the comment body contains the bot's mention
(`comment.body contains @<your-bot-handle>`). An unmentioned comment is authentic
but `200 ignored` — no dispatch. This keeps the orchestrator from waking on every
comment in a thread.

A related **ownership exemption** — a bot that already owns a PR responds to a
comment event *even without* a mention — depends on querying the GOV-F25
ownership card, which the exemption's positive path needs. Until that card ships,
the fleet ingress ships the **mention gate only**. The exemption is implemented as
a route-script seam: a `filters`/`script` hook under `~/.hermes/scripts/` that
consults the ownership card and admits an owned-PR comment. This runbook documents
the seam and defers the positive path to GOV-F25.

## Post-backs: use the owning bot's own identity, not `github_comment`

The webhook platform's `deliver: github_comment` shells `gh` **on the gateway
host** to post a comment. Do **not** use it for attributable fleet post-backs: a
comment posted from the gateway host is attributed to the gateway's principal, not
to the coworker that did the work. Attributable post-backs are the owning bot's
**own** `gh` call, made inside that bot's sandbox under its own identity.
`github_comment` is reserved for **unattributed** notices (a plain status ping to
a chat), and none of the five canonical O1 routes uses it.

## See also

- [Webhooks](./messaging/webhooks.md) — the webhook platform reference (route
  schema, HMAC, filters, delivery modes, idempotency).
- [Multi-profile gateways](./multi-profile-gateways.md) — the `/p/<profile>/`
  multiplex model and why the DEFAULT profile owns the single listener.
- [Fleet transcript retention (on-call runbook)](./fleet-transcript-retention.md)
  — the retention invariant that keeps session evidence on disk.
