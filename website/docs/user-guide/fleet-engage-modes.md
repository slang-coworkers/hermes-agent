---
title: Fleet engage modes
description: How nv-coworker-compose renders a NanoClaw engage mode into per-platform Hermes gate keys.
---

# Fleet engage modes

When you compose a fleet with `hermes coworker compose`, each coworker type (and
the DEFAULT multiplexer profile) can declare **how it engages** on each messaging
platform. `nv-coworker-compose` translates that declaration into the exact
per-platform keys the platform adapters already read under
`platforms.<platform>.extra`. Engagement stays **per-profile adapter config** —
the renderer registers no routing hook and no adapter is ever set to
blanket-forward. This is a mapping/convenience PORT of NanoClaw's router engage
modes onto stock Hermes gating, not a new gateway capability.

## Declaring engage in a coworker spec

Under a coworker type's (or the fleet's `default_config`'s) `config:` block:

```yaml
config:
  engage:
    slack: { mode: mention-sticky }                     # mention | mention-sticky | pattern | always-on
    discord: { mode: always-on, channels: ["C123456"] } # always-on needs a bounded, non-empty channels list
    telegram: { mode: pattern, patterns: ["\\bhermes\\b"], sender_scope: ["-1001234567890"] }
```

Per platform:

- `mode` — **required**. One of `mention`, `mention-sticky`, `pattern`, `always-on`.
- `patterns` — **required iff** `mode: pattern`. A non-empty list of wake-word
  regexes (each a non-empty string; a blank pattern would match everything).
- `channels` — **required iff** `mode: always-on`. A bounded, non-empty allowlist
  of channel/chat ids (never empty and never `*` — see the invariant below).
- `sender_scope` — **optional**, any mode. A bounded, non-empty list of
  channel/chat ids rendered to the platform's scope key. Omitting it **preserves**
  any inherited scope restriction; declaring it **replaces** that restriction.

The `engage` block is the plugin's own vocabulary; it has no core reader, so the
renderer removes it from every rendered `config.yaml` after translating it.

## Mode → rendered key mapping

Each row names the exact keys written to `platforms.<platform>.extra`. `pattern`
fills `mention_patterns` from your `patterns`; `always-on` fills the platform's
free-response key from your `channels`. The scope key (`allowed_channels` /
`allowed_chats`) is written **separately**, and only when you declare
`sender_scope`, so it is not part of the mode key sets below.

| platform | engage mode | rendered `platforms.<p>.extra` keys |
|---|---|---|
| slack | mention | `require_mention`, `strict_mention`, `thread_require_mention`, `mention_patterns`, `free_response_channels`, `require_mention_channels` |
| slack | mention-sticky | `require_mention`, `strict_mention`, `thread_require_mention`, `mention_patterns`, `free_response_channels`, `require_mention_channels` |
| slack | pattern | `require_mention`, `strict_mention`, `thread_require_mention`, `mention_patterns`, `free_response_channels`, `require_mention_channels` |
| slack | always-on | `require_mention`, `strict_mention`, `thread_require_mention`, `mention_patterns`, `free_response_channels`, `require_mention_channels` |
| discord | mention | `require_mention`, `thread_require_mention`, `free_response_channels` |
| discord | mention-sticky | `require_mention`, `thread_require_mention`, `free_response_channels` |
| discord | always-on | `require_mention`, `thread_require_mention`, `free_response_channels` |
| telegram | mention | `require_mention`, `mention_patterns`, `free_response_chats`, `observe_unmentioned_group_messages` |
| telegram | pattern | `require_mention`, `mention_patterns`, `free_response_chats`, `observe_unmentioned_group_messages` |
| telegram | always-on | `require_mention`, `mention_patterns`, `free_response_chats`, `observe_unmentioned_group_messages` |

What each mode means:

- **mention** — the bot answers only when @mentioned. Slack keeps sticky OFF
  (`strict_mention` and `thread_require_mention` both true), so a follow-up in the
  thread that does not re-mention the bot is ignored.
- **mention-sticky** — like `mention`, but once mentioned in a thread the bot stays
  engaged for that thread. On Slack this is `require_mention: true` with
  `strict_mention: false` and `thread_require_mention: false`, which enables the
  native mentioned-thread wake.
- **pattern** — the bot also wakes on a message matching one of the `patterns`
  wake-word regexes, in addition to explicit mentions.
- **always-on** — the bot answers every message in the bounded `channels`
  free-response list. Outside that list, always-on renders `strict_mention:false`
  (a superset of `mention-sticky`), so it wakes on a mention **or** a native
  sticky-thread / active-session — bounded, still requires prior engagement, never
  blanket. This is native breadth, not gateway fan-out; see the never-blanket-forward
  invariant below.

### Scope key (`sender_scope`)

`sender_scope` renders to `allowed_channels` (Slack/Discord) or `allowed_chats`
(Telegram) — the channel/chat allowlist the adapter checks. Telegram uses the
chat-scoped key names; Discord's spellings match Slack's.

## Unsupported combinations (fail closed)

The renderer is fail-closed: any platform/mode pair not in the table above raises
a `CompositionError` at compose time rather than emitting an unrecognised or
unrestricting gate.

- Discord `pattern` is **unsupported** — Discord has no `mention_patterns`
  resolver (raises `CompositionError`).
- Telegram `mention-sticky` is **unsupported** — Telegram has no strict/thread
  sticky model (raises `CompositionError`).
- Any platform other than `slack`, `discord`, `telegram` is **unsupported**
  (raises `CompositionError`).
- `pattern` with no (or a blank) `patterns` list; `always-on` with no bounded
  `channels`; and an empty or `*` `sender_scope` are all rejected.

## Invariants and caveats

- **No router, never blanket-forward.** The renderer registers no
  `pre_gateway_dispatch` hook and no routing subsystem; it only writes per-profile
  adapter keys. Because an empty allowlist means "no restriction" to the adapters,
  an `always-on` with no bounded `channels`, or a `sender_scope` that is empty or
  `*`, would silently widen access to **blanket-forward** — the render refuses
  those rather than emitting an unrestricting value.
- **`sender_scope` is channel/chat scope only**, not user authorization.
  Per-**user** admission (`allow_from` / pairing) is owned by RT-F01 / RT-F05, not
  this row, so `allowed_channels` / `allowed_chats` here should never be read as
  user authz.
- **Room deliberation is A2A-F18.** Several coworkers deliberating on one message
  (@mentions scoping a round) is a Bot-Mode room concern (A2A-F18), not gateway
  fan-out; this row is strictly per-profile engagement.
- **Discord env precedence.** On Discord, only `allowed_channels` is env-first:
  `_get_allowed_channels` resolves through `_gate_raw` (env / snapshot before
  `.extra`), so a `DISCORD_ALLOWED_CHANNELS` environment value overrides the
  rendered `allowed_channels`. The other Discord engage keys (`require_mention`,
  `thread_require_mention`, `free_response_channels`) read `.extra` first, so the
  rendered values take effect without an env override.
- **Native breadth, not exact NanoClaw parity.** Slack `mention-sticky` maps to
  Slack's native mentioned-thread wake, which also re-engages on bot-authored
  threads and active sessions — **broader** than NanoClaw's remembered-mention, so
  it is the native equivalent, not exact parity. Discord lacks Slack's
  `strict_mention` gate, but `mention-sticky` uses its native
  `thread_require_mention: false` bot-thread wake. Telegram has no thread-sticky
  mode; its `mention` and `pattern` modes also wake on a reply addressed to the
  bot, so those mappings are broader than literal mention/pattern matching.
