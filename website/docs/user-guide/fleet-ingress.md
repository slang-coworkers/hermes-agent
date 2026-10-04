---
sidebar_position: 75
title: "Fleet inbound gateway (nv-ingress)"
description: "How an OpenShell fleet receives GitHub and GitLab events with no platform secret and no public listener inside a sandbox: a signed host edge, a loopback forward, a secret-free loopback route that stages each event durably, and exactly-once delivery into the PR owner's existing session."
---

# Fleet inbound gateway (`nv-ingress`)

The `nv-ingress` plugin is the one inbound path for external events into a
Bot-Mode fleet running under the [OpenShell substrate](./fleet-openshell.md). It
keeps NemoClaw's boundary: **no platform secret inside a sandbox, and no public
listener inside a sandbox.** It is a plugin plus a compose render: it changes no
core file.

This page covers the inbound path, the envelope contract, the trust model, the
operator lane steps, restart behaviour, the Slack route, and the polling
backstop. The older per-route-secret ingress on the podman substrate is described
in [Fleet GitHub ingress](./fleet-github-ingress.md).

## Inbound path

```
platform ──signed POST──▶ host edge (verifies, normalizes, spools)
          ──host loopback──▶ OpenShell forward service
          ──sandbox loopback──▶ webhook route "ingress" (no-auth, loopback-gated)
          ──route script──▶ fleet ledger (staged before the 202)
          ──drain──▶ the target session (release CLI turn)
```

1. **Host edge.** The edge receiver runs on the host as a user unit. It holds each
   platform's signing secret in a host file outside every sandbox mount, and it
   verifies every request before doing anything else. An unsigned request, a bad
   signature or a stale timestamp gets `401`, and nothing is spooled or forwarded.
   A verified request is normalized to envelope v1 and written to the edge spool
   (fsync) before the platform gets its `202`.
2. **Forward.** The edge forwards each spooled envelope to
   `http://127.0.0.1:<port>/webhooks/ingress` through OpenShell's
   `forward service`. The forward is a host-loopback TCP listener relayed to the
   loopback webhook port inside the gateway sandbox. The forwarded request
   carries the envelope as its body, with the transport event type `ingress.v1`
   in the body. It carries no platform event, delivery or signature header; the
   envelope's delivery id travels as the request-id header that the release
   adapter dedupes on. A spool entry is deleted only after the route accepts it
   (or reports it a duplicate). Anything else is retried with backoff (capped at
   60 s); after 24 h the entry moves to `dead-letter/` and is logged.
3. **Route.** The DEFAULT multiplexer serves one webhook route, `ingress`, bound
   to `127.0.0.1`. It uses the release adapter's no-auth mode, which the adapter
   allows only on a loopback bind and refuses to start on any other host. The
   route admits only the `ingress.v1` event type. Its route script
   (`ingress_stage.py`) stages the envelope in the fleet ledger in one transaction,
   before the adapter answers `202`.
4. **Bridge.** The plugin's `pre_gateway_dispatch` hook skips the ingress route,
   so no one-shot webhook session is minted, and kicks the drain. The drain runs
   only in the DEFAULT home. It resolves each staged event and delivers it exactly
   once:
   - a new issue carrying a routing label → the orchestrator's canonical Bot Chat
     (deduped on the issue, since `opened` and `labeled` fire together);
   - a PR, review, comment or CI event of a **claimed** PR → the owner's
     **existing** session, resumed at its compression tip
     (`hermes -p <owner> chat -Q --resume <session> --query-file <prompt>`); a
     session is never created, and an unknown id fails instead;
   - a PR event with no owner → the orchestrator's Bot Chat, flagged as unowned.
   - A CI event that names no PR waits (`awaiting_association` in
     `hermes ingress status`, no time-out) until a PR event records a matching head
     SHA or branch. It goes to the orchestrator only once it is shown to belong to
     no claimed PR.
5. **Ownership.** A successful PR-create in a coworker session claims the PR for
   that profile, session and thread (first claim wins). A later claim of the same
   PR is accepted only from the claimed session or a verified compression
   continuation of it, and every other claim is refused and listed. A
   `Supersedes #<n>` / `Replaces #<n>` line, or the `ingress_claim_replacement`
   tool, makes a replacement PR inherit the owner and thread. `hermes ingress
   remap` and the `ingress_remap` tool are orchestrator-only and always go to the
   human-approval gate first.

Every staged event carries a delivery marker line. Before running a turn, the
drain looks for that marker in the target session's whole compression lineage,
so a delivery whose turn already committed is never run twice.

`hermes ingress status --json` prints the operational view: `pending`, `stuck`
(an owned delivery after three failed resumes, still pending for the same owner),
`runaway` (a PR over its hourly delivery budget; every event is still delivered
to the owner), claim `refusals`, `unclaimable` claims, and the `webhook_guard`
state. It never prints a secret, a header value or a transcript.

## Envelope v1

Every platform maps to one versioned key set. `event` is always in GitHub's
vocabulary, and the raw platform event name is kept in `platform_event`.

| key | meaning |
|---|---|
| `schema` | always `ingress.v1` |
| `source` | `github` or `gitlab` |
| `delivery_id` | the platform's retry-stable delivery id (GitHub's delivery id; GitLab's `webhook-id`) |
| `event` | canonical event name (`issues`, `issue_comment`, `pull_request`, `pull_request_review`, `pull_request_review_comment`, `check_run`, `check_suite`, `workflow_run`) |
| `platform_event` | the platform's own event name, unmapped |
| `action` | the event action (`opened`, `labeled`, `completed`, …) |
| `repo` | `owner/name` (GitLab: the project's `path_with_namespace`) |
| `kind` | `issue`, `pr`, `ci`, `review`, `comment` or `other` |
| `number` | the issue, PR or MR number the event names |
| `pr_numbers` | the fan-out set: every PR the event belongs to |
| `head_branch` | the PR head ref or the CI run's branch |
| `head_sha` | the PR head or the CI run's commit |
| `conclusion` | CI conclusion (or the review state) |
| `name` | check, job or workflow name |
| `labels` | issue labels |
| `sender` | the acting login or username |
| `sender_type` | `User` or `Bot` |
| `url` | link to the object |
| `received_at` | edge receive time (Unix seconds) |
| `title` | issue, PR or MR title |
| `body` | the first 2 000 characters of a comment or review body |

Bodies are cut so the envelope stays bounded; nothing beyond `title` and `body`
is forwarded from the payload.

## GitLab mapping

GitLab events take the same path through `POST /gitlab` on the edge.

**Prerequisite:** the GitLab instance must be GitLab 19.0+ with a signing token
configured on the project hook. In 19.0 that sits behind the
`webhook_signing_token` feature flag; it is generally available from 19.1, per
the GitLab webhook documentation. The edge verifies only the signing-token
signature, which follows the Standard Webhooks scheme:

- the signature is computed over the message id, the timestamp and the raw body;
- the timestamp must be within a 300 s replay window;
- the host secret file holds the signing token.

The plain token header is not a signature. The edge ignores it, so a hook that
sends only a token is refused with `401`. An operator GitLab that cannot sign
needs an operator ruling; this page does not narrow the requirement. The
delivery id is GitLab's retry-stable `webhook-id`, so a GitLab retry stages once
and a distinct delivery stages separately. A request with no id is refused and
not forwarded.

| GitLab event | `event` | notes |
|---|---|---|
| `Issue Hook` | `issues` | `object_attributes.iid` → `number`; `open`/`close`/`reopen` → `opened`/`closed`/`reopened` |
| `Merge Request Hook` | `pull_request` | `iid` → `number` and `pr_numbers`; `source_branch` → `head_branch`; `update` → `synchronize` |
| `Note Hook` on a merge request | `issue_comment` | `merge_request.iid` → `pr_numbers`; a note on anything else is `kind: other` |
| `Pipeline Hook`, `Job Hook` | `workflow_run` | `status` `failed`/`success`/`canceled` → `failure`/`success`/`cancelled`, others `pending`; `ref` → `head_branch`; `merge_request.iid` → `pr_numbers` |

## Trust model

The in-sandbox route authenticates nothing; that is the release's
local-testing mode, used here on purpose. Trust moves **from the signature to
reachability**, so only the host forward and the gateway's own processes may
reach the route. Four points hold that boundary:

1. **The in-sandbox bind is loopback.** The render pins the webhook host to
   `127.0.0.1`. The release adapter refuses to start a no-auth route on any other
   host, and an unset host counts as non-loopback. Under `ingress:` the render
   replaces every static route with the one ingress route and drops the adapter's
   global secret; `ingress.replace_routes: false` is refused. Agent-created
   dynamic routes are guarded: `nv-ingress` replaces the `webhook` adapter with a
   same-name subclass that differs only in how it loads the DEFAULT home's
   subscriptions file. If that file holds any route that carries a signing
   secret, or cannot be parsed, every webhook route (the ingress route included)
   is refused with `403` from the next request until the file is removed, and
   `hermes ingress status` shows the breach by route name.
2. **The host side of the forward binds host loopback only.** The forward unit
   listens on `127.0.0.1:<port>`. Any process on the host can reach host
   loopback; that is a host-compromise case outside the sandbox boundary.
3. **No model-driven tool in the gateway sandbox can reach the port.** Shell,
   code, file and patch tools run in each profile's worker sandbox over ssh
   (`nv-fleet-gates` enforces it). MCP is remote-only under enforcement. The
   in-process URL tools refuse loopback unless one of the three private-URL
   switches is on, and the fleet render enables none of them.
4. **The edge is the only signer.** A POST straight to the forwarded port skips
   the signature check. That is the recorded residual risk, bounded by points 2
   and 3. A forged CI failure only puts text into an owner's session, and the
   owner's next step reads the checks API through the `ci-gate` skill in its own
   worker sandbox, so a forged red check can cost work but never an approval.

The reviewer and the approver read check runs, never the legacy commit-status
endpoint, which can be empty while a check run is red. The reviewer reads
through its attached GitHub provider. The approver has no provider: it reads
credential-free through curl, with a GET-only egress rule to the API host, so
its repository must be public.

## Operator lane

These steps are host or live-platform actions; the fleet render cannot perform
them. The Orchestrator obtains each one from the operator and relays the
evidence.

- **L-HOOK** — the repository hook subscribes the rendered event list
  (`hooks/github-events.json`, which includes `check_run`, `check_suite` and
  `workflow_run`) and points at the edge's public URL.
- **L-EDGE** — each platform's secret file exists at the path `host/edge.yaml`
  names (mode `0600`, owned by the operator user, outside every sandbox mount).
  The three user units are installed and enabled, with linger, so they start at
  boot without a login.
- **L-FWD** — the forward unit is `active` and a loopback connection to the
  rendered port reaches the route's health endpoint. If the broker refuses a
  loopback bind, stop and report it; never substitute a non-loopback bind.
- **L-VAL** — the NemoClaw environment validator passes in the gateway sandbox
  with ingress enabled.
- **L-RST-SB** — restart the gateway sandbox through the broker (the restart
  drill).
- **L-RST-HOST** — a real host reboot, inside an operator-scheduled window
  (the reboot drill). A user-manager restart is not a substitute.
- **L-CHK** — confirm once per worker sandbox that the reviewer reads check
  runs through its provider and the approver reads them credential-free.

## Slack route

Slack is not an inbound path here. NemoClaw's Slack channel is an outbound
socket connection (Socket Mode) that needs no inbound endpoint, so no Slack
ingress is built in `nv-ingress`; it stays not built.

## Polling backstop

When the hook or the edge is down, an operator (or a cron job on the
orchestrator) lists the fleet's open PRs and their check runs read-only and
hands the red ones to their owners by hand. That restores visibility with no
inbound path at all. It is documented here only, not built.

## Restart behaviour

- The three host units (`nv-ingress-edge.service`, `nv-ingress-forward.service`,
  `nv-ingress-gateway-boot.service`) are rendered with `Restart=always`,
  `RestartSec=5` and `WantedBy=default.target`. With linger they come back after
  a crash, after the forward's relay drops (it exits rather than reconnect), and
  after a host reboot.
- While the gateway sandbox is down, the forward exits and systemd retries it
  every 5 s.
- The gateway-boot unit checks gateway health inside the sandbox every 15 s,
  independent of the forward, so an outage of the forward alone never starts
  anything. When the check fails it runs the operator-approved start command
  from `ingress.gateway_start`, serialized by a lock and a 120 s in-flight
  window.
- Events are durable on both sides of the forward. The edge spool keeps
  everything not yet acknowledged and re-forwards it after a restart. The fleet
  ledger keeps everything staged, and a restarted gateway drains it on plugin
  load with no new inbound.
- A delivery whose turn already committed is recognised by its marker, even
  when the session was compacted since, so it is not run twice.

## Render keys

The `ingress:` block of a fleet spec (all optional except where noted):

| key | default | meaning |
|---|---|---|
| `route` | `ingress` | the in-sandbox route name |
| `port` | `8644` | the in-sandbox webhook port, and the forward's host port |
| `platforms.<github\|gitlab>.secret_file` | required | absolute host path of the signing secret file (never read by the render) |
| `replace_routes` | `true` | must stay `true`; `false` is refused |
| `issue_labels` | required | labels that route a new issue to the orchestrator |
| `self_logins` | `[]` | the fleet's own platform identities; their events are marked self-authored |
| `per_pr_hourly_budget` | `30` | deliveries per PR and hour before the runaway flag |
| `gateway_sandbox` | required | the gateway sandbox name |
| `gateway_start` | required | the operator-approved gateway start argv |
| `anonymous_reads` | `{}` | per role, credential-free GET-only egress targets (`host:port`) |
| `host_checkout`, `host_python`, `host_dir`, `openshell_bin`, `edge_listen`, `edge_state_dir` | see the plugin | host paths the units use |

`hermes ingress render-host <spec> --out <dir>` renders the same `host/` and
`hooks/` artefacts as `hermes coworker compose`.
