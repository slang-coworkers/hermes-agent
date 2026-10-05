"""COST-F30.b resume-card hardening, beyond the acceptance criteria.

Reuses the acceptance module's real-discovery fixture (isolated HOME, empty bundled dir, real
``agent.estop`` with a spied ``disengage``, recorded ``send_message_tool``) so every case runs the
plugin as loaded on a stock tree.
"""

import pathlib

from tests.plugins.test_nv_cost_cap_resume_card_acceptance import (  # noqa: F401  (env is a fixture)
    NOTICE_TAIL,
    _approve,
    _card,
    _decisions,
    _messages,
    _new_stop,
    _own_sp,
    env,
)


def test_money_read_unavailable_keeps_stop(env):
    """With state.db gone, the spend readers degrade to "no new spend"; Approve must treat the session as open."""
    card = _new_stop(env, "s-noread", blocked=False)
    (env.home / "state.db").unlink()
    before = _own_sp(env).read_bytes()
    res = _approve(env, card["card_id"])
    assert res["ok"] is False and res["reason"] == "money-block-active", res
    assert "s-noread" in res["text"]
    assert _card(env, card["card_id"])["status"] == "pending"
    assert _own_sp(env).read_bytes() == before and env.disengage_calls == []


def test_interrupt_during_disengage_still_records_the_resume(env):
    """A disengage() that outlives the interrupted-after window still lands as approved, with its notice."""
    resume = env.mod.resume
    card = _new_stop(env, "s-slow", blocked=False)
    t0 = resume._clock()
    spy = env.estop.disengage

    def _slow(*a, **k):
        env.monkeypatch.setattr(resume, "_clock", lambda: t0 + resume.INTERRUPTED_AFTER_SECONDS + 30)
        env.mod.reconcile_once()
        assert _card(env, card["card_id"])["status"] == "interrupted"
        return spy(*a, **k)

    env.monkeypatch.setattr(env.estop, "disengage", _slow)
    res = _approve(env, card["card_id"])
    assert res["ok"] is True and res["reason"] == "resumed", res
    assert len(env.disengage_calls) == 1 and not _own_sp(env).exists()
    assert _card(env, card["card_id"])["status"] == "approved"
    assert [d["decision"] for d in _decisions(env, card["card_id"])] == ["attempt", "interrupted", "approve"]
    assert len([m for m in _messages(env) if NOTICE_TAIL in m]) == 1


def test_post_lift_probe_failure_still_records_the_resume(env):
    """A failing existence probe after a real lift must not turn the resume into a reported failure."""
    card = _new_stop(env, "s-probe-err", blocked=False)
    candidates = set(card["candidates"])
    spy = env.estop.disengage
    real_exists = pathlib.Path.exists

    def _exists(self, *a, **k):
        if str(self) in candidates:
            raise PermissionError("stat denied")
        return real_exists(self, *a, **k)

    def _lift_then_break_stat(*a, **k):
        out = spy(*a, **k)
        env.monkeypatch.setattr(pathlib.Path, "exists", _exists)
        return out

    env.monkeypatch.setattr(env.estop, "disengage", _lift_then_break_stat)
    res = _approve(env, card["card_id"])
    env.monkeypatch.setattr(pathlib.Path, "exists", real_exists)
    assert res["ok"] is True and res["reason"] == "resumed", res
    assert _card(env, card["card_id"])["status"] == "approved"
    approve = [d for d in _decisions(env, card["card_id"]) if d["decision"] == "approve"]
    assert len(approve) == 1 and all(e["present"] is None for e in approve[0]["outcome"]["after"])
    assert len([m for m in _messages(env) if NOTICE_TAIL in m]) == 1


def test_money_reader_fails_after_preflight_keeps_stop(env):
    """state.db opens and lists sessions, but the spend columns are unreadable: Approve treats the session as open."""
    import sqlite3

    card = _new_stop(env, "s-badschema", blocked=False)
    db = env.home / "state.db"
    db.unlink()
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT)")
        conn.execute("INSERT INTO sessions (id) VALUES ('s-badschema')")
        conn.commit()
    finally:
        conn.close()
    before = _own_sp(env).read_bytes()
    res = _approve(env, card["card_id"])
    assert res["ok"] is False and res["reason"] == "money-block-active", res
    assert _card(env, card["card_id"])["status"] == "pending"
    assert _own_sp(env).read_bytes() == before and env.disengage_calls == []
