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

The fleet authors these six routes into the DEFAULT profile's
`default_config.platforms.webhook.extra.routes`. The machine-checked source of
truth for this contract is the `CANONICAL_ROUTES` constant in
`tests/plugins/test_ch_f52_github_routing_acceptance.py`; the acceptance fixture
`tests/plugins/fixtures/ch-f52/coworker-types.yaml` is the runnable configuration,
and the test asserts the fixture's *rendered* output against that constant. The
table below is explanatory only — it is not what the test inspects, so keep it in
sync with `CANONICAL_ROUTES` during review:

| route name | `profile` | `events` | `action` filter admits | route extras |
|---|---|---|---|---|
| `gh-issues` | orchestrator | `[issues]` | `[opened, reopened]` | prompt + skills |
| `gh-issue-comment` | orchestrator | `[issue_comment]` | `[created]` | mention filter + prompt + skills |
| `gh-pull-request` | reviewer | `[pull_request]` | `[opened, reopened, synchronize, ready_for_review]` | **`script: pr_ci_gate.py`, no prompt** (CI-gated — parks a card, stays `[SILENT]`) |
| `gh-pr-review` | fixer | `[pull_request_review]` | `[submitted]` | prompt + skills |
| `gh-check-suite` | fixer | `[check_suite]` | `[completed]` | **`script: check_gate.py`** + prompt (promotes on green, wakes the fixer on red) |
| `gh-pr-review-invite` | reviewer | `[issue_comment]` | `[created]` | mention filter + `deliver: github_comment` + `toolsets: [terminal, hermes-webhook]` |

The two `pull_request`/`check_suite` routes are **CI-gated** (LOOP-F40): a
`pull_request` delivery does not dispatch a reviewer directly — it parks a per-head
review card and stays silent until a `check_suite` success promotes it. The
`gh-pr-review-invite` route is the **human-invited post-back**. Both are detailed
in [The CI-gate](#the-ci-gate-per-head-park--promote-loop-f40) below.

Each route additionally carries:

- **Its own `secret`** — a per-route, **pairwise-distinct** HMAC secret; never a
  shared or global secret (the compose renderer refuses a route without its own
  non-empty secret). In production, use `${env:NAME}` references and provision
  every variable in the DEFAULT profile's `.env` **before** gateway startup. Note
  the fail-open trap: an **unset** `${env:NAME}` reference is kept as the literal
  placeholder string (config expansion does not blank it), so the route's secret
  becomes that non-empty literal and the missing-secret `403` guard does **not**
  fire. Treat any unresolved reference as a deployment failure — verify every
  secret variable resolves at startup; never rely on the missing-secret guard to
  catch an unprovisioned one.
- **A `prompt` template** over the payload, referencing fields such as
  `{repository.full_name}`, `{issue.number}`, or `{pull_request.title}`. Payload
  fields are dot-addressable; `{__raw__}` dumps the whole payload. The one
  exception is the CI-gated `gh-pull-request` route: it carries **no** prompt,
  because its `script` returns `[SILENT]` and it never dispatches on the event.
- **A non-empty `skills` list** — the fleet skills injected for that event class
  (triage, review, CI investigation). The exact skill names are fleet policy;
  substitute your own. (The CI-gated `gh-pull-request` route carries neither
  prompt nor skills.)

The `action` filter is a declarative `filters` entry
(`{field: action, in: [...]}`); the mention gate on `gh-issue-comment` adds a
second filter `{field: comment.body, contains: "@<your-bot-handle>"}`. Filters in
a list are ANDed. The event→profile split is the fleet's canonical mapping;
per-bot comment routes follow the same shape (one route per bot at its
`/p/<bot>/` prefix, each filtering on that bot's own mention).

## The CI-gate: per-head park → promote (LOOP-F40)

The reviewer is **queued, not spawned**, on a `pull_request` event, and only
released once CI is green for the PR's current head. Two rendered route scripts
under the DEFAULT profile's `scripts/` (installed to `$HERMES_HOME/scripts/`)
drive this; they share a small SQLite gate store opened via
`plugin_db("nv-coworker-compose", "loop-f40-ci-gate.db")` with two tables:
`pr_gate(repo, pr, latest_head_sha, current_task_id, …)` (one row per PR, the
current head and its card) and `green(repo, pr, head_sha)` (heads CI has passed).
Each script acquires the gate DB write lock with `BEGIN IMMEDIATE` before it reads
or mutates, so concurrent duplicate deliveries cannot create two cards or lose a
head update. Route scripts run **before** the `X-GitHub-Delivery` de-dup, so this
serialization — not delivery de-dup — is what makes the gate idempotent.

- **`pr_ci_gate.py`** (on `gh-pull-request`) — for `opened`/`reopened`/`synchronize`/
  `ready_for_review` at head `H`: it upserts `pr_gate.latest_head_sha = H`, creates
  a **blocked** review card assigned to the reviewer, idempotency-keyed
  `loop-f40-review:{repo}#{pr}:{H}` (a duplicate delivery reuses the same card), and
  returns `[SILENT]` — no dispatch. If a `check_suite` success for `H` already
  arrived (recorded in `green`), it promotes the new card immediately (out-of-order
  webhooks). **Re-arm per head:** when the PR re-syncs to a new head, the prior
  head's card is retired so the dispatcher cannot review a stale head — a `blocked`
  card is archived directly, a `ready` card is *atomically claimed then archived*
  (if the dispatcher already claimed it, it is left running), and a card already
  `running`/`done` is preserved (its review is in flight or finished).
- **`check_gate.py`** (on `gh-check-suite`) — for a `completed` suite at head `H`:
  on `conclusion == success` it records `H` in `green` and, if `H` is the PR's
  current head, promotes that head's card `blocked → ready` (the ordinary kanban
  dispatcher then spawns `hermes -p reviewer chat -q` in the reviewer's sandbox),
  staying `[SILENT]`. A **stale-head** success (H is not the current head) does not
  promote. A **non-success** deletes any earlier `green` for that exact head (so a
  `success → failure → PR` sequence leaves the card blocked) and **returns the
  payload**, which wakes the fixer through this route's prompt. The route is thus
  silent on green (no needless fixer wake) and wakes the fixer only on red.

**Single-authoritative-`check_suite` assumption.** The gate treats
`check_suite.conclusion == success` as the aggregate CI-green signal — GitHub's
`check_suite` aggregates its `check_run`s. Where a repo runs multiple independent
suites, narrow the route's filter to the authoritative app/suite; `check_run` is
intentionally **not** the promote trigger.

**Ordering.** Out-of-order `check_suite`-vs-`pull_request` is handled by the
`green` table (a success recorded before the card exists promotes it on creation).
Out-of-order `pull_request`-vs-`pull_request` is handled best-effort: real GitHub
payloads carry `pull_request.updated_at`, and a delivery that is strictly older
than the last one processed for that PR is ignored (it cannot reset the head or
retire the current card). Payloads without `updated_at` fall back to
last-write-wins.

**This is a review QUEUE, not a merge gate.** LOOP-F40 only decides *when* to
queue the reviewer (once CI is green for the current head). Merge correctness
remains the Orchestrator's gate; nothing here merges, and the reviewer's verdict
routes through the board as usual.

### Human-invited post-back (`gh-pr-review-invite`)

A human can pull the reviewer into a PR by mentioning it in a comment. The
`gh-pr-review-invite` route fires only when a comment on a PR
(`issue.pull_request` present) is authored by a real person
(`comment.user.type == User`), is not the reviewer bot's own comment
(`comment.user.login != <reviewer-bot-login>`), and boundary-mentions the reviewer
handle (a `regex` filter, so `@slang-reviewer-evil` does **not** match). It grants
`toolsets: [terminal, hermes-webhook]` because the webhook default toolset
`hermes-webhook` carries no `terminal`, and the invoked reviewer needs to shell the
**read-only** `gh pr diff` to read the change. The review is posted back by the
route's own `deliver: github_comment`.

The authorization boundary here is the **declarative route filter** (a human's
mention), not LOOP-F37's `pre_tool_call` veto. The veto governs the separate
*autonomous* shell write path (`gh pr comment|review` invoked by a model); granting
this route `terminal` does not open that path — the read-only `gh pr diff` is
unaffected, and the human-visible post-back is the route's host-side delivery, not
the reviewer shelling `gh pr comment`.

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

Static routes are loaded from the DEFAULT profile's `config.yaml` when the gateway
starts; only *dynamic* subscriptions hot-reload per request. So a change to the
rendered route set (re-running the compose render, or editing `config.yaml`) takes
effect only on the next DEFAULT-gateway restart/redeploy.

**Deploying the DEFAULT profile (and its route scripts).** `hermes onboard
coworker` installs **coworker** profiles only; it does not deploy the DEFAULT
(multiplexer) profile. The operator must place the rendered `default/config.yaml`
**and** `default/scripts/*` (the CI-gate route scripts) into the gateway's
`$HERMES_HOME` and restart the gateway. The compose renderer declares `scripts` in
the DEFAULT profile's `distribution.yaml` `distribution_owned` set, so
`hermes profile install` copies `scripts/` into `$HERMES_HOME/scripts/`, where the
webhook adapter's script resolver looks up a route's `script:` by name. The DEFAULT
profile owns `scripts/` **wholesale** — keep any custom operator scripts under a
different path (or merge them in by hand after install), since a re-install
overwrites the owned `scripts/` entries.

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
the fleet ingress ships the **mention gate only**.

Mind the evaluation order when GOV-F25 lands: declarative `filters` run **before**
a route `script` (the adapter drops a filtered event before it ever invokes the
script). So the exemption cannot be added as an *additional* filter alongside the
mention filter — a standalone mention filter would reject an unmentioned owned-PR
comment before any script could admit it. The future implementation must **replace**
the standalone mention filter with a single route-level `script:` (under
`~/.hermes/scripts/`) that consults the ownership card and admits the comment when
it is mentioned **or** owned. This runbook documents that seam and defers the
positive path to GOV-F25.

## Post-backs: use the owning bot's own identity, not `github_comment`

The webhook platform's `deliver: github_comment` shells `gh` **on the gateway
host** to post a comment. Do **not** use it for *autonomous* attributable fleet
post-backs: a comment posted from the gateway host is attributed to the gateway's
principal, not to the coworker that did the work. Attributable **autonomous**
post-backs are the owning bot's **own** `gh` call, made inside that bot's sandbox
under its own identity (and gated by LOOP-F37's veto).

`github_comment` is reserved for the **human-requested** post-back — the single
`gh-pr-review-invite` route (LOOP-F40) — and for unattributed notices. The
human's mention is the authorization, so gateway-host attribution is acceptable
there: a person explicitly asked for this specific review. **This is the one
canonical route permitted to carry `deliver: github_comment`**; the autonomous O1
ingress routes (`gh-issues`, `gh-issue-comment`, `gh-pull-request`, `gh-pr-review`,
`gh-check-suite`) remain barred from it, and the acceptance test asserts exactly
that split.

## See also

- [Webhooks](./messaging/webhooks.md) — the webhook platform reference (route
  schema, HMAC, filters, delivery modes, idempotency).
- [Multi-profile gateways](./multi-profile-gateways.md) — the `/p/<profile>/`
  multiplex model and why the DEFAULT profile owns the single listener.
- [Fleet transcript retention (on-call runbook)](./fleet-transcript-retention.md)
  — the retention invariant that keeps session evidence on disk.
