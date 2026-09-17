"""Acceptance test for OBS-F47 (Transcript viewing): Hermes' native transcript
export/search/save/trace surface, plus the doc page that maps the NanoClaw verbs
onto it. One test per acceptance criterion; the AC id lowercases and maps '-'->'_'
to the node id (AC-OBS-F47-<n> -> test_ac_obs_f47_<n>) so the criterion<->test join
is mechanical. test_ac_obs_f47_7 fails until the doc page exists; the other six are
conformance proofs of native behaviour and pass on the base tree by design.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

# Repo-relative (CWD = repo root under scripts/run_tests.sh).
DOC_PAGE = Path("website/docs/user-guide/slang-coworkers-transcript-viewing.md")

# A marker inserted into the transcript and one never inserted: the pair is the
# differential control every content assertion turns on.
MARKER = "quokka-transcript-marker-9317"
ABSENT_MARKER = "narwhal-never-inserted-8842"

# A GitHub-PAT-shaped token the native secret redactor recognizes, and a
# non-secret sentence that must survive redaction (redaction is scoped, not a wipe).
SECRET = "ghp_" + "A0b1C2d3E4f5G6h7I8j9K0l1M2n3O4p5Q6r7"
SENTINEL = "keep-this-visible-sentence"


def _seed_session(db_path: Path, session_id: str = "s-obs-f47"):
    """Build a SessionDB with one user + one assistant(tool_call) + one tool message.

    Returns (db, session_dict). The caller closes the db when done. The session
    dict is what every native renderer consumes (export_session ->
    {**session, "messages": [...]}).
    """
    from hermes_state import SessionDB

    db = SessionDB(db_path)
    db.create_session(session_id, source="cli")
    db.append_message(session_id, "user", f"please run the build {MARKER}")
    db.append_message(
        session_id,
        "assistant",
        "",
        tool_calls=[{"id": "call_1", "function": {"name": "terminal", "arguments": '{"cmd": "make"}'}}],
    )
    db.append_message(session_id, "tool", "build ok: artifact.bin", tool_call_id="call_1")
    session = db.export_session(session_id)
    assert session is not None and session.get("messages"), "export_session returned no messages"
    return db, session


def test_ac_obs_f47_1(tmp_path: Path) -> None:
    """Multi-format export is native: all five formats render; transcript renderers carry content.

    Each declared format (jsonl, md, qmd, html, trace) is produced by a native
    renderer; the four transcript renderers carry the seeded marker and never the
    absent marker (differential control).
    """
    from agent.trace_upload import build_trace_jsonl
    from hermes_cli.session_export import render_sessions_export
    from hermes_cli.session_export_html import generate_html_export
    from hermes_cli.session_export_md import render_session_markdown

    db, session = _seed_session(tmp_path / "state.db")
    try:
        jsonl = render_sessions_export([session], fmt="jsonl")
        md = render_session_markdown(session, fmt="md")
        qmd = render_session_markdown(session, fmt="qmd")
        html = generate_html_export(session)
        trace = build_trace_jsonl(session["messages"], session_id="s-obs-f47", redact=True)
    finally:
        db.close()

    for name, out in (("jsonl", jsonl), ("md", md), ("qmd", qmd), ("html", html), ("trace", trace)):
        assert isinstance(out, str) and out.strip(), f"{name} renderer produced no output"

    for name, out in (("jsonl", jsonl), ("md", md), ("qmd", qmd), ("html", html)):
        assert MARKER in out, f"{name} export missing the seeded transcript content"
        assert ABSENT_MARKER not in out, f"{name} export leaked a never-inserted marker"


def test_ac_obs_f47_2(tmp_path: Path) -> None:
    """The HTML export is the /show-transcript archive analogue.

    Single-session generate_html_export collapses tool-call arguments by default and
    renders the tool result inline; multi-session generate_multi_session_html_export
    produces the archive with a working session switcher. Web fonts load remotely
    under the page CSP, so the document is not asset-free.
    """
    from hermes_cli.session_export_html import (
        generate_html_export,
        generate_multi_session_html_export,
    )

    db, session = _seed_session(tmp_path / "state.db")
    try:
        html = generate_html_export(session)
    finally:
        db.close()

    # Collapsible tool-call arguments: the container class, the clickable header,
    # and the active-toggle class the JS flips to expand it
    # (session_export_html.py:431-438, :614-618, :717-732).
    assert "tool-call" in html, "no tool-call block in HTML export"
    assert "tool-call-content" in html, "no collapsible tool-call arguments container"
    assert "tool-call-header" in html, "no clickable tool-call header"
    assert "tool-call.active" in html, "no expand-toggle rule (not collapsible)"
    # Collapsed by default: the .tool-call-content rule sets display:none (the
    # .tool-call.active override flips it to block on click).
    assert re.search(r"\.tool-call-content\s*\{[^}]*display:\s*none", html, re.S), (
        "tool-call arguments are not collapsed by default"
    )
    # Application CSS/JS are inlined (web fonts load remotely under the page CSP,
    # session_export_html.py:29 -- so we do NOT assert absence of external refs).
    assert "<style" in html and "<script" in html, "HTML export lacks inline app CSS/JS"
    assert "terminal" in html, "tool name missing from HTML export"
    assert "make" in html, "tool-call arguments missing from HTML export"
    assert "build ok: artifact.bin" in html, "tool-result content missing from HTML export"

    # Multi-session archive with a session switcher (the /show-transcript
    # groups->sessions->transcripts analogue), mirroring
    # tests/hermes_cli/test_session_export_html.py:49-61.
    multi = generate_multi_session_html_export(
        [
            {"id": "aaaa1111", "title": "First", "started_at": 0,
             "messages": [{"role": "user", "content": "one"}]},
            {"id": "bbbb2222", "title": "Second", "started_at": 0,
             "messages": [{"role": "user", "content": "two"}]},
        ]
    )
    assert "function showSession" in multi, "multi-session export dropped the switcher script"
    assert 'data-id="aaaa1111"' in multi, "first session missing from the sidebar"
    assert 'id="view-bbbb2222"' in multi, "second session view missing from the archive"


def test_ac_obs_f47_3(tmp_path: Path) -> None:
    """--redact scrubs secrets from an exported transcript, preserving non-secret text.

    redact_session_data removes a planted secret from message content and
    tool-call arguments while the non-secret sentinel survives (differential).
    """
    from hermes_cli.session_export_html import generate_html_export
    from hermes_cli.session_export_md import redact_session_data

    session = {
        "id": "s-redact",
        "title": "redact",
        "messages": [
            {"role": "user", "content": f"my token is {SECRET} -- {SENTINEL}"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "http", "arguments": json.dumps({"auth": SECRET})}}
                ],
            },
        ],
    }

    redacted = redact_session_data(session)
    rendered = generate_html_export(redacted)

    assert SECRET not in rendered, "secret survived redaction in the export"
    assert SENTINEL in rendered, "redaction wiped non-secret text (should be scoped)"


def test_ac_obs_f47_4(tmp_path: Path) -> None:
    """In-agent session_search over FTS5 finds a session by a message term.

    A present term returns the owning session; a term in no message returns no
    hit (differential control). Hermetic: the seeded SessionDB is injected via
    db=, so no default store is opened.
    """
    from hermes_state import SessionDB
    from tools.session_search_tool import session_search

    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("s-quokka", source="cli")
        db.append_message("s-quokka", "user", f"the {MARKER} lives in the tundra")
        db.create_session("s-other", source="cli")
        db.append_message("s-other", "user", "an unrelated conversation about weather")

        present = session_search(query=MARKER, db=db)
        absent = session_search(query=ABSENT_MARKER, db=db)
    finally:
        db.close()

    assert isinstance(present, str) and isinstance(absent, str)
    assert "s-quokka" in present, "FTS5 search did not surface the matching session"
    assert "s-quokka" not in absent, "FTS5 search matched a term that is in no message"


def test_ac_obs_f47_5(monkeypatch: pytest.MonkeyPatch) -> None:
    """HF trace export redacts by default and is fail-closed.

    build_trace_jsonl(redact=True) scrubs a planted secret; redact=False is a real
    opt-out; a redactor failure raises TraceRedactionError; and the upload wrapper
    catches that and returns a blocked message WITHOUT uploading (never leaks).
    """
    from agent import trace_upload
    from agent.trace_upload import TraceRedactionError, build_trace_jsonl

    messages = [
        {"role": "user", "content": f"deploy with {SECRET} now"},
        {"role": "assistant", "content": "done"},
    ]

    jsonl = build_trace_jsonl(messages, session_id="s-trace", redact=True)
    assert isinstance(jsonl, str) and jsonl.strip()
    assert SECRET not in jsonl, "secret leaked into the trace export"

    # --no-redact opt-out is real, not ignored: the secret survives (differential).
    unredacted = build_trace_jsonl(messages, session_id="s-trace", redact=False)
    assert SECRET in unredacted, "redact=False did not pass content through (opt-out ignored)"

    def _boom(*_a, **_k):
        raise RuntimeError("redactor failure")

    # Patch the redactor at its source module (as tests/agent/test_trace_upload.py
    # does): the primitive raises rather than pass plaintext through.
    monkeypatch.setattr("agent.redact.redact_sensitive_text", _boom)
    with pytest.raises(TraceRedactionError):
        build_trace_jsonl(messages, session_id="s-trace", redact=True)

    # The user-facing upload verb blocks without uploading when redaction fails:
    # pass a token past the no-token guard, feed messages without a DB, and record
    # whether the network upload worker is reached (it must not be).
    uploaded: list = []
    monkeypatch.setattr(
        trace_upload, "load_session_messages", lambda *_a, **_k: (messages, {"model": ""})
    )
    monkeypatch.setattr(
        trace_upload, "_do_upload", lambda *_a, **_k: uploaded.append(True) or "uploaded"
    )
    status = trace_upload.upload_session_trace("s-trace", token="hf_test", redact=True)
    assert "Trace upload blocked" in status, f"upload did not block on redaction failure: {status!r}"
    assert not uploaded, "upload worker was reached despite redaction failure (leak risk)"


def test_ac_obs_f47_6(tmp_path: Path) -> None:
    """/save produces the document it sends to chat in json/md/html.

    normalize_save_format maps each format to itself; render_session_for_save (the
    pure renderer the async /save handler delegates to) gives an HTML document, a
    Markdown transcript, and a session-faithful JSON snapshot.
    """
    from hermes_cli.session_export import normalize_save_format, render_session_for_save

    db, session = _seed_session(tmp_path / "state.db")
    try:
        for fmt in ("json", "md", "html"):
            assert normalize_save_format(fmt) == fmt

        html = render_session_for_save(session, "html")
        md = render_session_for_save(session, "md")
        js = render_session_for_save(session, "json")
    finally:
        db.close()

    # HTML is verified semantically, not by byte equality with a second
    # generate_html_export() call: each render mints a fresh CSP nonce, so two
    # renders of the same session are never byte-identical.
    assert "<!doctype html>" in html.lower(), "/save html is not an HTML document"
    assert MARKER in html and ABSENT_MARKER not in html, "/save html dropped the transcript"
    assert MARKER in md, "/save md dropped the transcript content"
    parsed = json.loads(js)
    assert parsed["id"] == session["id"], "/save json is not this session's snapshot"
    assert any(MARKER in str(m.get("content", "")) for m in parsed["messages"]), (
        "/save json dropped the transcript content"
    )


def test_ac_obs_f47_7() -> None:
    """The doc page maps the three NanoClaw verbs onto the Hermes verbs.

    show-transcript -> HTML export; ncl sessions messages -> Sessions API /
    dashboard / session_search; /upload-trace -> --format trace --upload; each cited
    per mapping row. Separately, /save documents the native in-chat renderer.
    """
    assert DOC_PAGE.exists(), f"doc page missing: {DOC_PAGE}"
    low = DOC_PAGE.read_text(encoding="utf-8").lower()

    # The three NanoClaw verbs being mapped.
    assert "show-transcript" in low
    assert "ncl sessions messages" in low
    assert "upload-trace" in low

    # Hermes verbs they map onto.
    assert "hermes sessions export" in low
    for fmt in ("jsonl", "md", "qmd", "html", "trace"):
        assert fmt in low, f"format {fmt} not documented"
    assert "--redact" in low
    assert "session_search" in low
    assert "--format trace" in low
    assert "private" in low
    assert "collapsible" in low
    # The dashboard / REST surface (ncl sessions messages parity).
    assert "sessions page" in low or "/api/sessions" in low

    # The dispatch-required citation form, enforced per mapping row: the single
    # table row that maps a NanoClaw verb to its Hermes symbol must carry BOTH a
    # `tag:` and a `main:` `path:line` citation. Requiring verb AND symbol in the
    # same row (and a real file:line, not just the markers) rejects both a stray
    # dual citation on an uncited row and a verb mentioned only in prose. There
    # are exactly three NanoClaw verbs; /save is a Hermes verb documented
    # separately, not a mapping row (checked below).
    mapping = {
        "show-transcript": "generate_html_export",
        "ncl sessions messages": "session_search",
        "upload-trace": "build_trace_jsonl",
    }
    table_rows = [ln for ln in low.splitlines() if ln.strip().startswith("|")]
    for verb, symbol in mapping.items():
        rows = [ln for ln in table_rows if verb in ln and symbol in ln]
        assert len(rows) == 1, f"expected exactly one mapping row for {verb} -> {symbol}"
        row = rows[0]
        for label in ("tag", "main"):
            assert re.search(rf"{label}:\s*`[^`]+/[^`]+:\d+`", row), (
                f"{verb} -> {symbol} row lacks a {label}: `path:line` citation"
            )

    assert "/save" in low, "the native /save verb is not documented"
    assert "render_session_for_save" in low, "the /save renderer is not documented"
