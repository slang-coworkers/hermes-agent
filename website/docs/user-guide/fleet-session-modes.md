---
sidebar_position: 73
title: "Fleet session modes (shared | per-thread) + the Bot Chat lane"
description: "How nv-coworker-compose renders a gateway-wide session-mode policy the multiplex SessionStore honors, why the mode is fleet-wide and serialized onto the DEFAULT profile, the always-on agent-shared Bot Chat lane, and why per-artifact ^gh continuity is a kanban card rather than a rewritten session key."
---

# Fleet session modes

This runbook covers how a Bot-Mode fleet controls **session isolation** — whether
the members of one group chat share a conversation or each get their own — and
where the always-on "one conversation per coworker" lane comes from. It is scoped
to fleets rendered by the `nv-coworker-compose` plugin (the DEFAULT multiplexer
profile plus every coworker profile).

NanoClaw assigns each wiring a session mode — `shared`, `per-thread`, or
`agent-shared`. Hermes already has the mechanisms for all three; the render
*configures* them rather than building a parallel router.

## The three modes

| NanoClaw mode | Hermes realization | Effect |
|---|---|---|
| `shared` | `group_sessions_per_user: false`, `thread_sessions_per_user: false` | Every sender in one non-thread group chat collapses to a single session. |
| `per-thread` (default) | `group_sessions_per_user: true`, `thread_sessions_per_user: false` | A group chat splits per sender; a thread is shared across its participants. |
| `agent-shared` | the native **Bot Chat** lane (see below) | One conversation per coworker, resolved by exact title — always on, independent of the flags. |

`shared` and `per-thread` are the two values of the `session_mode` field. Omitting
`session_mode` everywhere renders `per-thread` — the native default — but the
render writes both flags **explicitly** so a re-pin to a tree whose defaults have
moved cannot silently change fleet behavior.

## Declaring the mode

The session mode is a gateway-level field on the fleet spec:

```yaml
# coworker-types.yaml
default_config:
  session_mode: shared        # or: per-thread
```

A coworker type MAY restate `config.session_mode`, but it is a redundant
restatement of a **gateway-wide** property, not a per-coworker mode. The render
normalizes the effective declarations:

- Identical explicit declarations, and an explicit-vs-omitted pair, are accepted
  (an omission is not a conflicting opinion, so a lone coworker declaration sets
  the gateway-wide mode).
- Two genuinely divergent **explicit** declarations — any pair, `default_config`
  vs a coworker or two coworkers — raise a `CompositionError` before startup.
- An unknown value, or `session_mode: agent-shared`, each raise a
  `CompositionError` (see the Bot Chat lane below).

## Why the mode is gateway-wide

Under `gateway.multiplex_profiles`, the gateway builds **one** `SessionStore` from
the single gateway-process config (`gateway/run.py:7474`) and hands that same
store to every profile's adapter (`gateway/run.py:16823`). `SessionStore`'s key
function reads `group_sessions_per_user` / `thread_sessions_per_user` from that
one config (`gateway/session.py:2010-2017`); the per-profile hook only stamps the
`agent:<profile>:` namespace segment, never the isolation flags. So a second
profile in the same gateway cannot have a different mode — the mode is
gateway-wide by construction.

The render therefore serializes the resolved flags onto the **DEFAULT
(multiplexer) profile's top level**, the exact config the store reads. These flags
carry **gateway-global semantics**; they are serialized onto DEFAULT because
DEFAULT is the serialization owner, not because they belong to the default
profile's identity or lifecycle. Every non-default profile carries **no**
gateway-global copy.

To keep the adapter lane from diverging from the store lane, the render
**overwrites** every profile's canonical `platforms.<p>.extra` with the resolved
flag values (the adapter reads its flags from there,
`gateway/platforms/base.py:6270-6274`) and **strips** either flag from the
non-canonical alias locations the loader also merges —
`gateway.platforms.<p>.extra` and `gateway.<p>.extra` (merged at
`gateway/config.py:1599-1637`, where `gateway.<p>.extra` wins last). Stripping
the aliases prevents a hostile alias value from overriding the canonical one; the
canonical location is the single surviving source. The result: the store lane and
the effective merged adapter lane always carry the identical value.

### Not configurable this way: per-(profile, platform) modes

Giving profile A a different mode from profile B under one gateway is **not**
achievable by configuration — the shared store reads one config, so a divergent
per-profile flag would only split-brain the adapter and store lanes. That
capability is tracked as the open upstream ask **UA-5** and would require a
generic core seam (resolving the flags by `source.profile`), not a plugin. Until
then, a genuinely different mode means a different gateway (a tenant boundary).

## The agent-shared Bot Chat lane

The canonical **Bot Chat** (title `Bot Chat`) is created per profile by
onboarding and resolved by exact title. It is the "one conversation per coworker"
agent-shared mode: opening a coworker's Bot Chat resolves to the same session
across repeated opens (continuity within the profile), while each profile's Bot
Chat is a distinct session (isolation between profiles). This lane is **always
on** and is independent of `group_sessions_per_user` / `thread_sessions_per_user`
— the isolation flags neither select nor route into it. Because it is a lane, not
a flag, `session_mode: agent-shared` is rejected: the render fails closed so a
spec cannot silently encode agent-shared as isolation flags.

## The creation lock and the sole key authority

Concurrent inbound events for one key must produce one session, not a race of
duplicates. That is the native `_SessionFlight` per-key single-flight lock inside
`SessionStore.get_or_create_session`: the first caller creates the row while every
concurrent caller for the same key waits and then shares it, and a failed
in-flight creation releases the flight so a later call for that key still
succeeds. This plugin does not touch it.

Session identity remains owned entirely by core: `build_session_key` is the sole
key authority. This plugin registers **no** session-identity seam — it declares
no hooks and in particular does not register `pre_gateway_dispatch` or
`on_session_start` — so nothing rewrites a session address after the adapter has
already keyed the event. The render only sets the flags the existing key function
reads.

## Per-artifact continuity: a kanban card, not a rewritten key

NanoClaw's canonical `^gh-issue-N` / `^gh-pr-N` chain collapses every event for
one GitHub artifact onto a single conversation. In this fleet that per-artifact
continuity lives on the **kanban** board card for the artifact (CH-F52 / GOV-F25),
**not** on a rewritten session key. A webhook delivery is keyed per unique
delivery and closed on completion; tying successive deliveries for one issue or PR
together is the board's job. Do not attempt to encode `^gh` continuity by
mutating the session address — that would split-brain the adapter and store lanes
and is explicitly out of scope for the key authority.
