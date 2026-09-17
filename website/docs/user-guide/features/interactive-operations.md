---
sidebar_position: 40
title: "Interactive operations: cards, questions, reactions, and files"
description: "How Hermes asks the user multiple-choice questions, presents dangerous-command approval cards, adds emoji reactions, and attaches files — with native buttons where the surface supports them and documented text fallbacks where it does not."
---

# Interactive operations: cards, questions, reactions, and files

Hermes agents can do more than send plain text. They can ask the user a
multiple-choice question, present a dangerous-command approval as an
interactive card, react to a message with an emoji, and attach a file. All four
are **native Hermes features** — there is no plugin to install and no tool to
register. This page documents each operation, the surface it renders on, and
the fallback used when a surface cannot render buttons.

## Overview

The four interactive operations map onto existing Hermes capabilities as
follows:

| Operation | How the agent invokes it | Rendered by |
|---|---|---|
| **Question** (multiple choice) | the `clarify` tool | native choice buttons on button-capable adapters; a numbered text list on adapters that cannot render buttons |
| **Card** (dangerous-command approval) | the approval gate (automatic on a dangerous command) | Allow-once / Allow-session / Always-allow / Deny buttons on button-capable messaging adapters; interactive buttons on the desktop panel; a `/approve` … `/deny` text card on messaging adapters without buttons |
| **Reaction** (emoji) | `send_message(action="react", emoji=…)`; the desktop `react_to_message` tool | the adapter's reaction API; reaction metadata on the message row + a desktop event |
| **File** (attachment) | `send_message` with `MEDIA:<path>` in the message text | the adapter's native document API; a sanitized error on adapters without one |

Questions and approval cards degrade gracefully: where a surface cannot render
buttons, the agent still reaches the user through a numbered text list or a
slash-command card, and the reply is captured and resolved exactly as a button
click would be. Messaging reactions and file attachments have no text fallback —
they require native adapter support and otherwise return an error (the desktop
`react_to_message` path is separate and DB-backed; see below).

## Questions (clarify)

The `clarify` tool lets the agent ask the user a question and wait for the
answer. It supports up to **four** predefined choices per question
(`MAX_CHOICES = 4`) across up to **five** batched questions per call
(`MAX_QUESTIONS = 5`).

- **Button-capable adapters** (for example Telegram or Discord) render one
  inline button per choice and append a fifth **"Other (type your answer)"**
  control. Picking a choice resolves the clarify with that choice's text;
  picking "Other" flips the pending entry to text capture, so the user's next
  message in the session becomes the answer.
- **Adapters that cannot render buttons** fall back to a numbered text list and
  mark the session as awaiting a typed reply. The user answers with the number,
  the option text, or free prose, and the gateway's text-intercept resolves the
  clarify. For a two-choice question the fallback looks like:

  ```
  ❓ Pick a fruit

    1. apple
    2. banana

  Reply with the number, the option text, or your own answer.
  ```

The fifth "Other" control is a property of the button/GUI renderers, not of the
base text fallback — the numbered list already accepts free prose, so no extra
option is needed. A configurable clarify timeout bounds the wait; a value of
`<= 0` means "wait indefinitely" and a timeout returns a no-response result so
the agent can proceed on its own judgement rather than blocking forever. This
timeout is the equivalent of a question's answer deadline.

*Implementation:* tag `v2026.8.31` — `tools/clarify_tool.py:23` (`MAX_CHOICES`),
`:26` (`MAX_QUESTIONS`), `:329` (`clarify_tool`); base numbered-text fallback +
`mark_awaiting_text` at `gateway/platforms/base.py:4468`–`:4540`; native choice
buttons come from an adapter override, e.g. `gateway/relay/adapter.py:3000`–`:3049`
(`RelayAdapter.send_clarify` — one option per choice plus an `other` control);
the clarify timeout in `tools/clarify_gateway.py`. main —
`tools/clarify_tool.py::clarify_tool`,
`gateway/platforms/base.py::BasePlatformAdapter.send_clarify`.

## Cards (approvals)

When the agent tries to run a dangerous command, Hermes presents an **approval
card** offering four actions: **Allow once**, **Allow for the session**,
**Always allow**, and **Deny**. `Allow once` re-prompts the next time the same
command appears; `session` and `always` suppress the prompt for the rest of the
session or permanently. The card renders on three surfaces.

1. **Button-capable messaging adapters.** When the messaging adapter in front of
   the agent implements a structured approval renderer, the gateway shows four
   buttons (Allow-once / Allow-session / Always-allow / Deny). Telegram and Slack
   are examples.

2. **Messaging adapters without buttons.** The gateway falls back to a plain-text
   card that carries the command, the reason, and the slash-command
   instructions. The command prefix is the adapter's own (`/` by default, `!` on
   Slack and Matrix):

````text
⚠️ **Dangerous command requires approval:**
```
rm -rf /tmp/x
```
Reason: deletes files

Reply `/approve` to execute this one operation, `/approve session` to approve this pattern for the session, `/approve always` to approve permanently, or `/deny` to cancel.
````

3. **The desktop panel — interactive, not text-plus-command.** The Hermes
   desktop app runs the headless `hermes serve` backend (a JSON-RPC/WebSocket
   gateway, not a UI). On this surface the approval is **not** rendered through a
   platform adapter at all: the tui_gateway emits an `approval.request` event
   carrying structured `choices` — `["once", "session", "always", "deny"]`, or
   `["once", "deny"]` when the command is smart-denied — and the app renders those
   as Run / Allow-session / Always-allow / Reject buttons that resolve through the
   `approval.respond` RPC. The `hermes dashboard` web UI is a **separate** surface
   (the desktop app spawns `serve`, never `dashboard`, and neither launches the
   other): its Chat tab embeds `hermes --tui` over a `/api/pty` WebSocket, and
   that embedded terminal — driven by the same tui_gateway backend — presents the
   same approval interactively for in-terminal selection. Either way the panel is
   fully interactive; there is no "text plus a typed command" step.

Plain (non-desktop) API-server sessions instead follow the
`approvals.unattended_mode` setting, and the `/v1/runs` streaming API carries its
own structured `approval.request` event with the same `choices`.

**Parity note.** Hermes scopes interactive cards to **approvals**. There is no
generic "display card with arbitrary URL-link buttons" tool; an agent that needs
to offer the user a labelled set of links does so through message text.

*Implementation:* tag `v2026.8.31` — messaging button check on the status
adapter `gateway/run.py:6687`; text fallback `_format_exec_approval_fallback`
`gateway/run.py:889`–`:915`; desktop structured payload
`tui_gateway/server.py:2867` (`_approval_request_payload`), emitted at `:2941`,
resolved by the `approval.respond` RPC `tui_gateway/methods_prompt.py:1853`;
desktop button UI `apps/desktop/src/components/assistant-ui/tool/approval.tsx:156`,
`:204`–`:247`; `hermes serve` → tui_gateway `hermes_cli/main.py:12188`;
api_server `approvals.unattended_mode` `hermes_cli/config_defaults.py:2562`,
`/v1/runs` event `gateway/platforms/api_server_runs.py:746`; dashboard and serve
are independent surfaces sharing one backend
`hermes_cli/subcommands/dashboard.py:87`–`:95`, and the web dashboard embeds
`hermes --tui` over `/api/pty` `web/src/pages/ChatPage.tsx:2`. main —
`gateway/run.py::_format_exec_approval_fallback`,
`tui_gateway/server.py::_approval_request_payload`,
`tui_gateway/methods_prompt.py` (RPC route `"approval.respond"`).

## Reactions

An agent can attach an emoji reaction to a message on two surfaces.

- **Messaging (the direct analog).** `send_message(action="react", emoji=…)`
  dispatches the emoji to the live adapter's `add_reaction` method (duck-typed;
  it requires a running gateway with a connected adapter). `action="unreact"`
  removes one. This is the platform-native tapback. In the pinned release the
  Photon adapter is the bundled adapter that implements `add_reaction`; any other
  adapter exposing the same public `add_reaction` API participates too, while an
  adapter without it returns an error rather than a reaction.
- **Desktop.** The separate `react_to_message` tool records the agent's reaction
  (`author="agent"`) as reaction metadata on the target message row — Hermes
  stores reactions under the message's `display_metadata` JSON, not a side table,
  so they survive rewind and compaction — and emits a `message.reaction` desktop
  event so the reaction shows up in the desktop UI. The tool is exposed only to
  desktop (GUI) sessions via the `desktop_ui` toolset, and is further gated by the
  opt-in `display.message_reactions` toggle (default `false`).

*Implementation:* tag `v2026.8.31` — messaging `tools/send_message_tool.py:230`
(react/unreact schema), `:261` (dispatch → `_handle_react`), `:335`–`:361`
(resolves the adapter via `gateway.run._gateway_runner_ref()` and awaits
`add_reaction`); desktop `tools/react_to_message_tool.py:34`
(`_react_to_message_with_db`), `:90` (handler), `:119`/`:130`
(`check_react_requirements` reads `display.message_reactions`),
`toolsets.py:264` (`desktop_ui` toolset); reaction storage under
`display_metadata` `hermes_state.py:11803` (`REACTIONS_METADATA_KEY`), `:11805`
(`set_message_reaction`). main — `tools/send_message_tool.py::_handle_react`,
`tools/react_to_message_tool.py::react_to_message_tool`,
`hermes_state.py::SessionDB.set_message_reaction`.

## Files

An agent attaches a file by putting `MEDIA:<path>` in the message text it sends
through `send_message`; adding `[[as_document]]` forces document delivery rather
than letting the engine pick an image/video/voice method by extension. The media
send engine delivers the file **natively** — it calls the adapter's
`send_document` (or `send_image_file` / `send_video` / `send_voice`) — but only
when the adapter actually **overrides** that method.

If the adapter only inherits the base method, the engine refuses to send and
returns a **sanitized** error that names the missing capability but never the
host filesystem path:

```text
Live adapter does not implement native document delivery; media file 1/1 was not sent
```

This is deliberate: the base `send_document` on `BasePlatformAdapter` produces a
text notice, but the send engine rejects the inherited method rather than
invoking it, so an unsupported adapter fails **closed** — the user is told the
delivery is not supported, and the local path of the file is never leaked into a
chat.

*Implementation:* tag `v2026.8.31` — `tools/send_message_tool.py:238`
(`MEDIA:` schema), `:854` (`_send_live_adapter_media`, called `:1010`),
`:911`–`:919` (override check + sanitized error), `:921` (native call); base
`send_document` text notice `gateway/platforms/base.py:4923`. main —
`tools/send_message_tool.py::_send_live_adapter_media`.

## Graceful degradation

The text fallbacks on the messaging surface — the numbered choice list for
questions and the `/approve` … `/deny` card for approvals — are **documented,
supported behaviour, not a bug or a missing feature**. Every platform, however
limited, can present a question or an approval and capture the reply: a number,
the option text, free prose, or a slash command. An adapter can upgrade these to
native buttons by overriding `send_clarify` or `send_exec_approval`, and the
resolution path is identical whether the answer came from a button or from typed
text.

Messaging reactions and file attachments are different: they have **no** text
fallback and require native adapter support (`add_reaction`, `send_document`). On
an adapter that lacks it, a messaging reaction returns an error and a file send
fails closed with the sanitized message shown above — the interaction is not
degraded to text. The desktop `react_to_message` path is independent: it writes
reaction metadata to the session database and never goes through a messaging
adapter.

## Known limitation (upstream ask)

Parked **clarify questions** are held only in in-memory registries — the
messaging clarify registry and the tui_gateway clarify registry that backs the
desktop panel. A desktop or TUI client that disconnects and reconnects to a
**still-running** gateway is replayed the pending question by the tui_gateway
registry (it stays authoritative), but
there is **no cross-restart persistence**: if the gateway process restarts while
a clarify question is parked, the in-memory registry is cleared, the question is
lost, and there is no rehydration path. This is scoped to clarify **questions**;
approvals use a distinct seam.

This gap is filed as an upstream ask against `NousResearch/hermes-agent`: give
the two clarify registries one shared durable/rehydrated representation (persist
each pending clarify entry, re-arm the in-memory index on gateway start, and
reconnect a late answer to a resumed agent turn). It is a core concern in the
clarify path, not something a plugin can add, so nothing in Hermes today
promises automatic recovery of a clarify question across a restart.

*Implementation:* tag `v2026.8.31` — messaging clarify registry
`tools/clarify_gateway.py:71` (`_entries`, in-memory), `:107`
(`wait_for_response` blocks on an in-process event); desktop/`hermes serve`
clarify registry `tui_gateway/server.py:146`–`:152`, populated by `_block` at
`:4677`; the within-process reconnect replay (not restart persistence)
`tui_gateway/server.py:2888` (`_pending_clarify_request_payload`). main —
`tools/clarify_gateway.py::_entries`.
