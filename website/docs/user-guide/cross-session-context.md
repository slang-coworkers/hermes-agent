---
sidebar_position: 8
title: "Cross-session context: the room, the board, and search"
description: "How Hermes provides cross-session context — the shared room transcript, the durable kanban board, and the session_search pull — mapped from NanoClaw's session-echo fan-out."
---

# Cross-session context: the room, the board, and search

Hermes already provides cross-session context. It does **not** do it the way NanoClaw does — there is no *session-echo fan-out* that broadcasts each turn into the sibling sessions of a shared chat. Instead, Hermes gives you **three native surfaces**, each already built into the core (no plugin required):

- **The room** — a shared, ordered transcript every member reads, for live cross-member visibility (see [Bot Mode](./bot-mode.md)).
- **The board** — a durable [kanban](./features/kanban.md) card that carries hand-off text across sessions and profiles.
- **Search** — the built-in `session_search` tool, which pulls older context from a *different* session on demand (see [Sessions › Session Search Tool](./sessions.md#session-search-tool)).

This page maps NanoClaw's "cross-session context" onto those three surfaces and documents the one place the analogy breaks: Hermes performs **no sibling-session fan-out** (see the warning below).

## NanoClaw → Hermes mapping

| NanoClaw behaviour | Hermes surface | Core mechanism (tag tree `file:line`) |
|---|---|---|
| A turn is echoed / fanned out so every participant in the shared chat sees it | **Room transcript** — one shared, ordered event log every member reads | `gateway/hosted_rooms.py:1414` (`create_room`), `:1713` (`append_event`), `:2351` (`read_events`); session title `tui_gateway/hosted_room_driver.py:1506` (`room_session_title`) |
| Hand-off text that must outlive a turn | **Kanban card** — a row on the shared, cross-profile board | `hermes_cli/kanban_db.py:2338` (`connect`), `:3169` (`create_task`), `:3642` (`get_task`) |
| Pulling older context from another session on demand | **`session_search`** — FTS5 pull over the local session store | `tools/session_search_tool.py:1082` (handler), `:1145` (`SESSION_SEARCH_SCHEMA`); DB layer `hermes_state_search.py:1417` (`search_messages`) |
| (no equivalent — see the warning) | **not** a delivery mirror | `gateway/mirror.py:25` (`mirror_to_session`), `:115` (`_find_session_id`) |

The dual `tag: … | main: …` line references (numbers on the pinned `v2026.8.31` tag and on upstream `main`) are listed under [Citations](#citations), where their literal form is preserved.

## The room — live cross-member visibility

A Bot Mode **room** (a group chat of 2–6 Bots) is one **shared, ordered event log**. Every member's turn is appended to the same per-room log, and every member reads that same log — this is what replaces NanoClaw's fan-out. In the core, `gateway.hosted_rooms` owns the log: `create_room` (`gateway/hosted_rooms.py:1414`) registers the room and its members, `append_event` (`:1713`) appends each turn as an ordered event, and `read_events` (`:2351`) replays the shared log to any member.

Each member still keeps **its own per-profile session**; members address the room through a shared, room-scoped session title `Group: <room-id>`, produced by `tui_gateway.hosted_room_driver.room_session_title` (`tui_gateway/hosted_room_driver.py:1506`). As the Bot Mode guide puts it, "Each member keeps its own persistent `Group: <name>` session, so room context survives like any other conversation" (`website/docs/user-guide/bot-mode.md:89`), and rooms "follow your gateways, not one Desktop" (`website/docs/user-guide/bot-mode.md:80`) — the recent transcript is mirrored into every connected gateway's profile metadata.

So a turn posted by one member is present in the same log another member reads (**AC-RT-F08-1**). Visibility comes from the shared log's readability, not from any push into a sibling session.

## The board — durable hand-off

Text that must **outlive a turn** — a hand-off from one profile to another — is a row on the shared [kanban](./features/kanban.md) board, not an echoed message. Hermes Kanban is "a durable task board, shared across all your Hermes profiles … every handoff is a row anyone can read and write" (`website/docs/user-guide/features/kanban.md:11`; the NanoClaw comparator is at `website/docs/user-guide/features/kanban.md:30`).

The board is a core module, `hermes_cli.kanban_db` — a function API over a `sqlite3.Connection`, **not** the `plugins/kanban/` dashboard plugin. `connect` (`hermes_cli/kanban_db.py:2338`) opens (and, on first use, initialises) the shared board; `create_task` (`hermes_cli/kanban_db.py:3169`) writes a hand-off row and commits it durably; `get_task` (`hermes_cli/kanban_db.py:3642`) reads a row back by id. Because each write commits and the board file is shared across profiles, a card written by one profile survives a close/reopen and is read back intact by an independent reader — another profile's view of the same board (**AC-RT-F08-2**).

## Search — pull older context

Anything older than the room's recent window is **pulled** with the built-in `session_search` tool (see [Sessions › Session Search Tool](./sessions.md#session-search-tool)). It "performs full-text search across all past conversations using SQLite's FTS5 engine … [and] makes no LLM calls" (`website/docs/user-guide/sessions.md:674`). It is a **core, built-in** tool — unconditionally in `_HERMES_CORE_TOOLS` and in its own `session_search` toolset (`toolsets.py:66`, `:231-234`), subject only to a lightweight requirements check (`check_session_search_requirements`) — with four calling shapes (discovery by `query`; scroll by `session_id` + `around_message_id`; read by `session_id` alone; browse with no arguments), and it resolves `@session:<profile>/<id>` links (`website/docs/user-guide/sessions.md:720`).

**Two layers, two scopes** — state this precisely:

- **DB layer.** `SessionDB.search_messages` (`hermes_state_search.py:1417`) runs the FTS5 query over the database-wide message index (`messages_fts` joined to `messages` and `sessions`), and by default excludes rewound / inactive rows (`include_inactive=False`).
- **Tool discovery shape.** The `session_search` discovery path (`tools/session_search_tool.py`) narrows that database-wide reach to **eligible past sessions**: it excludes `kanban`, `subagent`, and `tool` sources; it **demotes** — rather than excludes — high-volume `cron` rows below interactive ones; and it suppresses the searcher's **own current live lineage** *unless* that content has already left live context (a session ended by compression, a `/new`-reset predecessor, or an in-place-compacted row), which stays discoverable (`tools/session_search_tool.py:832-860`).

The net effect is a genuine **cross-session pull**: a query issued while working in one session surfaces a matching message that lives in a *different* session (returning that session's id), and it does **not** surface the searcher's own current live session (**AC-RT-F08-3**).

## ⚠️ No sibling-session fan-out

Hermes does **not** broadcast a turn into sibling sessions. Cross-member visibility comes from the shared room log above, **not** from echoing a turn into other sessions. Hermes' delivery-path push is a **single-target delivery mirror** in `gateway/mirror.py`, not a broadcast: when `send_message` or a cron delivery lands in a chat, `mirror_to_session` appends **one** `delivery-mirror` record into the **one** session that owns that chat (module docstring at `gateway/mirror.py:4`; `mirror_to_session` defined at `gateway/mirror.py:25`, whose docstring at `gateway/mirror.py:52` notes the mirrored text is the agent's *own* outgoing reply — a single turn, not a broadcast).

Its resolver, `_find_session_id` (`gateway/mirror.py:115`), targets a single session rather than fanning out. The primary lookup is the `state.db` origin query (`find_session_by_origin`); the cited fallback scan of `sessions.json` for pre-migration databases (`gateway/mirror.py:184-206`) **resolves at most one session** — it returns `None` when it cannot resolve a single target (no matching candidate at all, or an ambiguous multi-user set: a supplied user with no exact match among multiple candidates, or no supplied user when the candidates span more than one distinct user id), and otherwise selects the **newest** matching candidate (`max(updated_at)`). The `mirror` / `mirror_source` metadata is also dropped at the SQLite boundary — only role and content persist (`gateway/mirror.py:56-58`).

The takeaway for a plugin author: **use the room transcript for visibility, the card for durable hand-off, and `session_search` for the pull** — do not expect the delivery mirror to fan a turn out to siblings.

## Persistence

Sessions are saved to the SQLite store (`state.db`) under the Hermes home — resolved via `get_hermes_home()`, never a hard-coded `~/.hermes` — and survive restarts: "Sessions enable conversation resume, cross-session search, and full conversation history management" (`website/docs/user-guide/sessions.md:11`), backed by "structured session metadata with FTS5 full-text search, plus full message history" (`website/docs/user-guide/sessions.md:17`). Cross-session context is therefore durable across restarts, not merely within one live process.

## Proof

All three surfaces are proven hermetically by `tests/tools/test_cross_session_context_acceptance.py`, one test per criterion. These exercise **stock** behaviour, so they are green on the pinned tree and act as regression guards:

- **AC-RT-F08-1** — room transcript (`gateway.hosted_rooms`): both members' turns appear in the one shared `read_events` log, and `room_session_title("room-1") == "Group: room-1"`.
- **AC-RT-F08-2** — durable kanban hand-off (`hermes_cli.kanban_db`): a card survives a `connect_closing` close/reopen and is read back intact by an independent reader; a missing id returns `None`.
- **AC-RT-F08-3** — `session_search` cross-session pull (`tools.session_search_tool`): a query from session B surfaces a match in session A and excludes B's own current live lineage.

## Citations

Dual `tag: … | main: …` line references (pinned `v2026.8.31` tag, then upstream `main`), preserved in literal form. The pinned-tag line numbers are authoritative; the `main:` values are the snapshot recorded in the shared gap-matrix evidence when this page was authored and drift as `main` moves, so they are advisory:

- tag: website/docs/user-guide/sessions.md:11 | main: website/docs/user-guide/sessions.md:11 — cross-session search is a documented session capability
- tag: website/docs/user-guide/bot-mode.md:80 | main: website/docs/user-guide/bot-mode.md:91 — rooms follow your gateways (the recent transcript is mirrored per gateway)
- tag: tools/session_search_tool.py:1145 | main: tools/session_search_tool.py:562 — `SESSION_SEARCH_SCHEMA`, the pull tool's schema
- tag: gateway/mirror.py:4 | main: gateway/mirror.py:1 — the delivery-mirror module docstring
- tag: gateway/mirror.py:52 | main: gateway/mirror.py:25 — `mirror_to_session()` mirrors the agent's own single reply, not a fan-out
- tag: gateway/mirror.py:115 | main: gateway/mirror.py:65 — `_find_session_id()` targets a single session

Tag-only references (tree-authoritative; the shared evidence carries no `main:` value). The real FTS content pull is `SessionDB.search_messages` (`hermes_state_search.py:1417`) — **not** the metadata-only `search_sessions`, which is a plain listing over the `sessions` table with no full-text search. Room transcript: `gateway/hosted_rooms.py:1414` / `:1713` / `:2351`. Kanban board: `hermes_cli/kanban_db.py:2338` / `:3169` / `:3642`. Room session title: `tui_gateway/hosted_room_driver.py:1506`.
