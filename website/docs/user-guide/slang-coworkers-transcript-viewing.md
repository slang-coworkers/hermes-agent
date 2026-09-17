---
title: slang-coworkers Transcript Viewing
description: Mapping the NanoClaw transcript-viewing verbs (show-transcript, ncl sessions messages, /upload-trace) onto the native Hermes session export, search, save, and HF trace surface.
---

# slang-coworkers Transcript Viewing

NanoClaw viewed transcripts three ways: `/show-transcript` rendered every
group's Claude Code and Codex JSONL logs into a hierarchical HTML archive
(groups → sessions → transcripts) served on `:8080`; `ncl sessions messages
<id>` listed a session's messages from the CLI; and `/upload-trace` pushed a
transcript to an external viewer.

Every one of these is already **native Hermes** behaviour, and for the HTML
archive and the redacted, fail-closed HF upload it is stronger than the
NanoClaw equivalent. This row therefore **adopts** the native surface — there
is **no slang-coworkers plugin and no core patch** — and this page maps the
three NanoClaw verbs onto the Hermes verbs that replace them. The hermetic
acceptance test `tests/hermes_cli/test_obs_f47_transcript_viewing_acceptance.py`
proves the load-bearing behaviours.

All `file:line` references are relative to the pinned release tree
(`NousResearch/hermes-agent`, tag `v2026.8.31` @ `29112bef`). Each mapping row
carries the `tag:` line and, where the symbol also exists on the fork's
`origin/main`, a `main:` line. The transcript renderer modules (`session_export_html.py`,
`session_export_md.py`, `session_export.py`, `tools/session_search_tool.py`,
`agent/trace_upload.py`) are line-stable between the pinned tag and `main`;
`main:` line numbers here are verified against `slang-coworkers/hermes-agent`
`origin/main` @ `0f25fec7`.

## Verb mapping

The three NanoClaw transcript-viewing verbs and the native Hermes verbs that
replace them (`/save` is a native Hermes verb, not a NanoClaw one — it is
documented [separately below](#the-native-save-verb)):

| NanoClaw verb | Hermes verb | Citation (tag: / main:) |
|---|---|---|
| `/show-transcript` HTML archive (groups → sessions → transcripts, served on `:8080`) | `hermes sessions export --format html transcript.html` produces one HTML document with inline application CSS/JS and a multi-session sidebar; tool-call arguments are **collapsible** and collapsed by default, tool results render inline. Renderer `generate_html_export` (multi-session archive `generate_multi_session_html_export`). | tag: `hermes_cli/session_export_html.py:868` `generate_html_export` · main: `hermes_cli/session_export_html.py:868` `generate_html_export` |
| `ncl sessions messages <id>` | The Sessions REST API (`GET /api/sessions`, `GET /api/sessions/{id}/messages`, `PATCH /api/sessions/{id}`), the dashboard Sessions page, and the in-agent `session_search` tool over FTS5. | tag: `tools/session_search_tool.py:1082` `session_search` · main: `tools/session_search_tool.py:1082` `session_search` |
| `/upload-trace` | `hermes sessions export --format trace --upload` emits Claude Code JSONL for the HF Agent Trace Viewer, **private** by default, redacted by default and fail-closed. Primitive `build_trace_jsonl`. | tag: `agent/trace_upload.py:135` `build_trace_jsonl` · main: `agent/trace_upload.py:135` `build_trace_jsonl` |

## `show-transcript` → the HTML session archive

`hermes sessions export --format html transcript.html` is the
`/show-transcript` archive analogue (the output path is required for
`--format html` — `hermes_cli/sessions_cmd.py:507`). `generate_html_export`
(`hermes_cli/session_export_html.py:868`) renders a session to **one** HTML
document: the application CSS and JS are inlined, so the file needs no local
asset directory. It is not entirely asset-free — the template links Google Fonts
remotely under its Content-Security-Policy
(`hermes_cli/session_export_html.py:30`, `style-src … https://fonts.googleapis.com; font-src https://fonts.gstatic.com`) — so treat it as a single self-contained document that fetches web fonts, not an offline bundle.

Tool-call arguments are **collapsed by default**: the
`.tool-call-content` block is styled `display: none`
(`hermes_cli/session_export_html.py:431`) and a click adds the
`.tool-call.active` class that reveals it
(`hermes_cli/session_export_html.py:436`). Tool *results* render inline (they
are not collapsed). For the groups → sessions → transcripts archive that
`/show-transcript` served, `generate_multi_session_html_export`
(`hermes_cli/session_export_html.py:761`) emits a multi-session document with a
sidebar and a `showSession` switcher script
(`hermes_cli/session_export_html.py:573`). See
[Sessions › HTML export](./sessions.md) (`website/docs/user-guide/sessions.md:377`,
"single self-contained HTML … collapsible tool output").

## `ncl sessions messages` → the Sessions API, dashboard, and `session_search`

Listing a session's messages is served three ways:

- **The Sessions REST API** — `GET /api/sessions` (paginated), `GET
  /api/sessions/{id}/messages`, and `PATCH /api/sessions/{id}` to rename
  (`gateway/platforms/api_server.py:2237`; documented in
  `website/docs/user-guide/features/api-server.md:540`).
- **The dashboard Sessions page** — browse, filter, and open any session in the
  web dashboard (`website/docs/user-guide/features/web-dashboard.md:251`).
- **`session_search`** — the in-agent tool that runs FTS5 over the message store
  in DISCOVERY / SCROLL / READ / BROWSE modes
  (`tools/session_search_tool.py:1082`). The FTS5 index and tables live in each
  profile's `state.db` (`website/docs/developer-guide/session-storage.md:17`).

## `/upload-trace` → `hermes sessions export --format trace --upload`

`--format trace` emits Claude Code JSONL for the Hugging Face Agent Trace
Viewer. The export is **private** by default and **redacted by default, and
fail-closed**:

- `build_trace_jsonl(..., redact=True)` (`agent/trace_upload.py:135`) scrubs
  secrets through the forced redactor; if the redactor itself fails it raises
  `TraceRedactionError` (`agent/trace_upload.py:46`) rather than emit
  plaintext.
- The user-facing wrapper `upload_session_trace` (`agent/trace_upload.py:351`)
  **catches** that failure and returns the "Trace upload blocked" status
  (`_REDACTION_BLOCKED_MESSAGE`, `agent/trace_upload.py:39`) at the
  `except TraceRedactionError` handler (`agent/trace_upload.py:393`) — it does
  **not** upload and does **not** re-raise, so a redactor failure can never leak.
- `--no-redact` (`hermes_cli/main.py:14245`) is an explicit, reviewed opt-out;
  `--public` opts out of privacy. See
  `website/docs/user-guide/sessions.md:403` (`--upload`, `HF_TOKEN`) and
  `website/docs/user-guide/sessions.md:416`
  ("secret-redacted by default … `--upload` is private unless `--public`").

## The native `/save` verb

`/save` is a **Hermes** in-chat verb (not a NanoClaw one): it drops a transcript
of the current session straight into the chat. The format is required — the
canonical usage is `/save <format> [filename] [redact]`
(`SAVE_USAGE`, `hermes_cli/session_export.py:326`); the accepted formats are
`SAVE_FORMATS = ("json", "md", "html")`
(`hermes_cli/session_export.py:324`), and `filename` and `redact` are optional.
The async handler `_handle_save_command` delegates the document it sends to the
pure renderer `render_session_for_save`
(`hermes_cli/session_export.py:361`), after `normalize_save_format`
(`hermes_cli/session_export.py:347`) validates the format. That pure renderer is
what the acceptance test exercises.

## Formats

`hermes sessions export` accepts `jsonl`, `md`, `qmd`, `html`, and `trace`
(`hermes_cli/main.py:14224`). Markdown and Quarto (`md` / `qmd`) come from
`render_session_markdown` (`hermes_cli/session_export_md.py:167`); the JSONL and
multi-session Markdown exports come from `render_sessions_export`
(`hermes_cli/session_export.py:41`). See
`website/docs/user-guide/sessions.md:348` for the format table.

## Redaction

`--redact` deep-copies the session and scrubs every message and tool-call
argument via `agent.redact.redact_sensitive_text(force=True)`, wired through
`redact_session_data` (`hermes_cli/session_export_md.py:219`, applied at
`hermes_cli/sessions_cmd.py:435`). Trace exports force this redaction on by
default (see the `--format trace` section above); the other formats redact only
when `--redact` is passed.

## Search

`session_search` (`tools/session_search_tool.py:1082`) runs the FTS5 search over
the message store. Pass a `query` for DISCOVERY, page with SCROLL, read a hit
with READ, and list sessions with BROWSE. The FTS5 tables live in each profile's
`state.db` (`website/docs/developer-guide/session-storage.md:17`).

## Storage

Each profile keeps one `state.db` (`website/docs/developer-guide/session-storage.md:3`).
The public class is `SessionDB` (`hermes_state.py:4678`); its `export_session`
returns the `{**session, "messages": [...]}` dict every renderer above consumes.

## Fleet-wide viewing

On the one-gateway slang-coworkers fleet, viewing every coworker's transcripts
is a shell loop over `hermes -p <bot> sessions export …`, kept in the on-call
runbook rather than shipped as a plugin CLI — the deliberate adopt boundary for
this row. A future `hermes fleet-transcripts` plugin CLI (iterating
`~/.hermes/profiles/*/state.db` and calling the same HTML renderer) is possible
but out of scope here.

## How to verify

The hermetic acceptance test
`tests/hermes_cli/test_obs_f47_transcript_viewing_acceptance.py` proves the
load-bearing behaviours: multi-format export, the collapsible-by-default HTML
archive and its multi-session switcher, `--redact` scrubbing, FTS5
`session_search`, the fail-closed HF trace export, the `/save` renderer, and
this page's verb mapping.
