"""Acceptance tests for the GOV-F24 approval-decision ledger plugin."""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from gateway.config import Platform
from hermes_cli.plugins import PluginManager
from tools.registry import registry, invalidate_check_fn_cache

PLUGIN_KEY = "nv-approval-ledger"
PLUGIN_SRC = Path(__file__).parents[2] / "plugins" / PLUGIN_KEY


def _set_profile(monkeypatch, name):
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: name)
    invalidate_check_fn_cache()  # check_fn results are TTL-cached per callable


def _plugin_db(base: Path) -> Path:
    return base / "plugin-data" / PLUGIN_KEY / "data.db"


def _rows(base: Path, **where) -> list[tuple]:
    db = _plugin_db(base)
    if not db.exists():
        return []
    clause = " AND ".join(f"{k}=?" for k in where) or "1=1"
    conn = sqlite3.connect(db)
    try:
        return list(
            conn.execute(
                f"SELECT repo, pr, commit_sha, decision, provenance "
                f"FROM approval_decisions WHERE {clause}",
                tuple(where.values()),
            )
        )
    finally:
        conn.close()


def _dispatch(name: str, args: dict) -> dict:
    return json.loads(registry.dispatch(name, args))


def _review_event(delivery, *, route="pr-review", platform=Platform.WEBHOOK, chat_type="webhook", pr=21, sha="beefcafe", state="approved"):
    return SimpleNamespace(
        text="",
        source=SimpleNamespace(platform=platform, chat_type=chat_type, chat_id=f"webhook:{route}:{delivery}"),
        raw_message={
            "action": "submitted",
            "repository": {"full_name": "o/r"},
            "pull_request": {"number": pr, "head": {"sha": sha}},
            "review": {"state": state, "commit_id": sha},
        },
        message_id=delivery,
    )


def _human_full(base: Path, pr: int, sha: str) -> list[tuple]:
    # Every column, so a byte-for-byte survival check catches a rewritten detail/created_at too.
    db = _plugin_db(base)
    if not db.exists():
        return []
    conn = sqlite3.connect(db)
    try:
        return list(
            conn.execute(
                "SELECT repo, pr, commit_sha, decision, provenance, delivery_id, profile, detail, created_at "
                "FROM approval_decisions WHERE provenance='human' AND pr=? AND commit_sha=?",
                (pr, sha),
            )
        )
    finally:
        conn.close()


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    # Profile-mode HERMES_HOME: <root>/profiles/reviewer resolves get_active_profile_name()
    # to "reviewer" (a writer), while ledger_profile="default" pins the DB to <root>.
    root = tmp_path / "root"
    home = root / "profiles" / "reviewer"
    plugins_dir = home / "plugins"
    plugins_dir.mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, plugins_dir / PLUGIN_KEY)

    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "plugins": {
                    "enabled": [PLUGIN_KEY],
                    "entries": {
                        PLUGIN_KEY: {
                            "settings": {"writers": ["reviewer"], "review_route": "pr-review"}
                        }
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    empty_bundled = tmp_path / "bundled-plugins"
    empty_bundled.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(empty_bundled))

    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_KEY]

    assert loaded.enabled is True
    assert loaded.error is None
    assert loaded.module is not None
    assert "pre_gateway_dispatch" in loaded.hooks_registered
    for tool in ("record_decision", "record_human_verdict", "list_trusted_decisions"):
        assert tool in loaded.tools_registered

    return SimpleNamespace(manager=manager, loaded=loaded, root=root, home=home)


def test_ac_gov_f24_1(ledger):
    """A record_decision from a writer inserts one agent_verified row into the single fleet ledger and list_trusted_decisions returns it."""
    out = _dispatch(
        "record_decision",
        {"repo": "o/r", "pr": 7, "commit_sha": "abc123", "decision": "WOULD_APPROVE"},
    )
    assert out["status"] == "recorded"

    assert _rows(ledger.root, repo="o/r", pr=7, commit_sha="abc123") == [
        ("o/r", 7, "abc123", "WOULD_APPROVE", "agent_verified")
    ]
    assert not _plugin_db(ledger.home).exists()  # pinned to owner, not caller

    trusted = ledger.loaded.module.list_trusted_decisions(repo="o/r", pr=7)
    assert any(
        d["commit_sha"] == "abc123" and d["provenance"] == "agent_verified" for d in trusted
    )


def test_ac_gov_f24_2(ledger, caplog):
    """Identical repeat is a no-op; a conflicting verdict on the same key is refused without overwrite and logged."""
    key = {"repo": "o/r", "pr": 9, "commit_sha": "def456"}
    assert _dispatch("record_decision", {**key, "decision": "BLOCK"})["status"] == "recorded"

    assert _dispatch("record_decision", {**key, "decision": "BLOCK"})["status"] == "already_recorded"
    assert len(_rows(ledger.root, repo="o/r", pr=9, commit_sha="def456")) == 1

    with caplog.at_level(logging.WARNING):
        conflict = _dispatch("record_decision", {**key, "decision": "WOULD_APPROVE"})
    assert conflict["status"] == "refused"
    assert _rows(ledger.root, repo="o/r", pr=9, commit_sha="def456") == [
        ("o/r", 9, "def456", "BLOCK", "agent_verified")
    ]
    assert any(
        rec.levelno >= logging.WARNING and "def456" in rec.getMessage() for rec in caplog.records
    )


def test_ac_gov_f24_3(ledger, monkeypatch):
    """record_decision is refused with zero rows for a non-writer profile and when writers is unset (fail-closed)."""
    _set_profile(monkeypatch, "fixer")
    out = _dispatch(
        "record_decision",
        {"repo": "o/r", "pr": 11, "commit_sha": "f00", "decision": "BLOCK"},
    )
    assert out["status"] == "refused"
    assert _rows(ledger.root, repo="o/r", pr=11, commit_sha="f00") == []

    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"plugins": {"entries": {PLUGIN_KEY: {"settings": {"review_route": "pr-review"}}}}},
    )
    _set_profile(monkeypatch, "reviewer")
    out = _dispatch(
        "record_decision",
        {"repo": "o/r", "pr": 12, "commit_sha": "f01", "decision": "BLOCK"},
    )
    assert out["status"] == "refused"
    assert _rows(ledger.root, repo="o/r", pr=12, commit_sha="f01") == []


def test_ac_gov_f24_4(ledger, monkeypatch):
    """Model-facing schema is writer-scoped and record_human_verdict is in no schema."""
    def _names(profile):
        _set_profile(monkeypatch, profile)
        return {d["function"]["name"] for d in registry.get_definitions({"record_decision", "record_human_verdict"})}

    assert _names("reviewer") == {"record_decision"}
    assert _names("fixer").isdisjoint({"record_decision", "record_human_verdict"})


def test_ac_gov_f24_5(ledger):
    """A human verdict coexists with an agent_verified row at the same commit in either arrival order, is idempotent on re-delivery, ignores off-route/non-webhook events, and never alters dispatch."""
    # Case A — agent first, then human.
    assert _dispatch("record_decision", {"repo": "o/r", "pr": 21, "commit_sha": "beefcafe", "decision": "WOULD_APPROVE"})["status"] == "recorded"
    results = ledger.manager.invoke_hook("pre_gateway_dispatch", event=_review_event("D1"), gateway=None, session_store=None)
    assert all(not (isinstance(r, dict) and r.get("action") in {"skip", "rewrite"}) for r in results)
    # 4-col key: an agent_verified row and a human row coexist for the same commit (a 3-col key drops one).
    assert sorted(r[4] for r in _rows(ledger.root, repo="o/r", pr=21, commit_sha="beefcafe")) == ["agent_verified", "human"]

    # Case B — human first (separate commit), then agent; coexistence must hold regardless of order.
    ledger.manager.invoke_hook("pre_gateway_dispatch", event=_review_event("D2", pr=22, sha="cafe02"), gateway=None, session_store=None)
    assert _dispatch("record_decision", {"repo": "o/r", "pr": 22, "commit_sha": "cafe02", "decision": "BLOCK"})["status"] == "recorded"
    assert sorted(r[4] for r in _rows(ledger.root, repo="o/r", pr=22, commit_sha="cafe02")) == ["agent_verified", "human"]

    # Idempotent re-delivery; off-route and non-webhook events write nothing.
    ledger.manager.invoke_hook("pre_gateway_dispatch", event=_review_event("D1"), gateway=None, session_store=None)
    ledger.manager.invoke_hook("pre_gateway_dispatch", event=_review_event("D3", route="other", pr=23, sha="f00d03"), gateway=None, session_store=None)
    ledger.manager.invoke_hook("pre_gateway_dispatch", event=_review_event("D4", platform=Platform.SLACK, chat_type="slack", pr=24, sha="f00d04"), gateway=None, session_store=None)
    assert len(_rows(ledger.root, provenance="human")) == 2  # only the pr21 and pr22 human rows


def test_ac_gov_f24_6(ledger, monkeypatch):
    """An agent cannot mint a human verdict: a tokenless record_human_verdict is refused and record_decision always stamps agent_verified."""
    _set_profile(monkeypatch, "reviewer")  # even a writer
    out = _dispatch(
        "record_human_verdict",
        {"repo": "o/r", "pr": 41, "commit_sha": "d00d", "decision": "approved"},
    )
    assert out["status"] == "refused"
    assert _rows(ledger.root, repo="o/r", pr=41, commit_sha="d00d", provenance="human") == []
    assert registry.get_entry("record_human_verdict").check_fn() is False

    out = _dispatch(
        "record_decision",
        {"repo": "o/r", "pr": 31, "commit_sha": "c0ffee", "decision": "WOULD_APPROVE", "provenance": "human"},
    )
    assert out["status"] == "recorded"
    assert _rows(ledger.root, repo="o/r", pr=31, commit_sha="c0ffee") == [
        ("o/r", 31, "c0ffee", "WOULD_APPROVE", "agent_verified")
    ]


def test_ac_gov_f24_7(ledger, caplog):
    """The human path is first-write-wins and delivery-conflict-safe: a different delivery with a different state, a replayed delivery id with a mutated SHA, and a delivery id bound to another commit each leave existing human rows byte-for-byte unchanged and log a WARNING."""
    ledger.manager.invoke_hook("pre_gateway_dispatch", event=_review_event("D1", pr=31, sha="beef07", state="approved"), gateway=None, session_store=None)
    original = _human_full(ledger.root, 31, "beef07")
    assert len(original) == 1 and original[0][:6] == ("o/r", 31, "beef07", "approved", "human", "D1")

    # (a) second delivery (different id), different state, same commit -> first-write-wins + WARNING.
    with caplog.at_level(logging.WARNING):
        ledger.manager.invoke_hook("pre_gateway_dispatch", event=_review_event("D2", pr=31, sha="beef07", state="changes_requested"), gateway=None, session_store=None)
    assert _human_full(ledger.root, 31, "beef07") == original
    assert any(rec.levelno >= logging.WARNING and "beef07" in rec.getMessage() for rec in caplog.records)

    # (b) replayed delivery id D1 carrying a mutated SHA -> no new row, original survives, WARNING.
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        ledger.manager.invoke_hook("pre_gateway_dispatch", event=_review_event("D1", pr=31, sha="dead07", state="approved"), gateway=None, session_store=None)
    assert _human_full(ledger.root, 31, "dead07") == []
    assert _human_full(ledger.root, 31, "beef07") == original
    assert any(rec.levelno >= logging.WARNING and ("D1" in rec.getMessage() or "dead07" in rec.getMessage() or "beef07" in rec.getMessage()) for rec in caplog.records)

    # (c) delivery-id precedence: D3 is bound to beef08; submitting D3 against beef07 with the SAME
    #     decision as D1's row must still be refused (a natural-key 'approved' match must NOT swallow
    #     the delivery mismatch), leaving both rows intact.
    ledger.manager.invoke_hook("pre_gateway_dispatch", event=_review_event("D3", pr=31, sha="beef08", state="approved"), gateway=None, session_store=None)
    other = _human_full(ledger.root, 31, "beef08")
    assert len(other) == 1 and other[0][5] == "D3"
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        ledger.manager.invoke_hook("pre_gateway_dispatch", event=_review_event("D3", pr=31, sha="beef07", state="approved"), gateway=None, session_store=None)
    assert _human_full(ledger.root, 31, "beef07") == original
    assert _human_full(ledger.root, 31, "beef08") == other
    assert any(rec.levelno >= logging.WARNING and "D3" in rec.getMessage() for rec in caplog.records)


class _LockOnceConn:
    """Test double: raise 'database is locked' on the first N executes whose SQL
    starts with ``prefix`` (counter shared across reopened connections so the
    _ledger_db open/retry is exercised, not just the INSERT), then delegate."""

    def __init__(self, real, prefix, fail_state):
        self._real = real
        self._prefix = prefix
        self._fail_state = fail_state

    def execute(self, sql, *args, **kwargs):
        if sql.lstrip().upper().startswith(self._prefix) and self._fail_state["n"] > 0:
            self._fail_state["n"] -= 1
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(sql, *args, **kwargs)

    def commit(self):
        return self._real.commit()

    def close(self):
        return self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


def _install_lock(monkeypatch, prefix, count):
    import plugins.plugin_storage as ps

    real = ps.plugin_db
    fail_state = {"n": count}  # shared across reopens: a re-opened connection sees the decremented counter
    monkeypatch.setattr(
        ps, "plugin_db", lambda name, filename="data.db": _LockOnceConn(real(name, filename), prefix, fail_state)
    )


def test_mc1_record_decision_oversized_pr_returns_error_not_raise(ledger):
    """An out-of-range pr makes the writer handler return a JSON error, never raise, and write no row."""
    out = _dispatch(
        "record_decision",
        {"repo": "o/r", "pr": 2**100, "commit_sha": "big", "decision": "BLOCK"},
    )
    assert out["status"] in ("error", "refused")
    assert _rows(ledger.root, repo="o/r", commit_sha="big") == []


def test_mc1_human_verdict_retries_past_transient_insert_lock(ledger, monkeypatch):
    """A transient lock on the INSERT is retried, not dropped — one human row lands."""
    _install_lock(monkeypatch, "INSERT", 1)
    ledger.manager.invoke_hook(
        "pre_gateway_dispatch", event=_review_event("R1", pr=51, sha="ab51"), gateway=None, session_store=None
    )
    assert _rows(ledger.root, repo="o/r", pr=51, commit_sha="ab51", provenance="human") == [
        ("o/r", 51, "ab51", "approved", "human")
    ]


def test_mc1_human_verdict_retries_past_schema_open_lock(ledger, monkeypatch):
    """A first-use lock at schema init (CREATE) is retried by _ledger_db, not dropped — one human row lands."""
    _install_lock(monkeypatch, "CREATE", 1)
    ledger.manager.invoke_hook(
        "pre_gateway_dispatch", event=_review_event("R3", pr=53, sha="ab53"), gateway=None, session_store=None
    )
    assert _rows(ledger.root, repo="o/r", pr=53, commit_sha="ab53", provenance="human") == [
        ("o/r", 53, "ab53", "approved", "human")
    ]


def test_mc1_human_verdict_write_failure_is_logged_not_silently_dropped(ledger, monkeypatch, caplog):
    """A hard human-write failure writes no row, logs a terminal drop WARNING, and never returns skip/rewrite."""
    _install_lock(monkeypatch, "INSERT", 999)  # every INSERT fails → retries exhaust
    with caplog.at_level(logging.WARNING):
        results = ledger.manager.invoke_hook(
            "pre_gateway_dispatch", event=_review_event("R2", pr=52, sha="ab52"), gateway=None, session_store=None
        )
    assert all(not (isinstance(r, dict) and r.get("action") in {"skip", "rewrite"}) for r in results)
    assert _rows(ledger.root, repo="o/r", pr=52, commit_sha="ab52", provenance="human") == []
    assert any(
        rec.levelno >= logging.WARNING and "R2" in rec.getMessage() and "not durably recorded" in rec.getMessage().lower()
        for rec in caplog.records
    )


def test_sc1_traversal_ledger_profile_fails_closed(ledger, monkeypatch):
    """A traversal/absolute ledger_profile is rejected: the write fails closed with no row (no ledger outside the profile tree)."""
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"plugins": {"entries": {PLUGIN_KEY: {"settings": {"writers": ["reviewer"], "ledger_profile": "../evil"}}}}},
    )
    out = _dispatch(
        "record_decision",
        {"repo": "o/r", "pr": 61, "commit_sha": "trav", "decision": "BLOCK"},
    )
    assert out["status"] in ("error", "refused")
    assert _rows(ledger.root, repo="o/r", pr=61, commit_sha="trav") == []
