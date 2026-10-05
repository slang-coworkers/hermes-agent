"""Acceptance test for COST-F30.b — approval-gated ESTOP resume card for nv-cost-cap.

ADR: reports/cost-f30-b.md (§Acceptance criteria AC-COST-F30.b-1..18). Drop-in target:
tests/plugins/test_nv_cost_cap_resume_card_acceptance.py (repo-root discovery assumes that path).

Loaded through the REAL discovery path (shape: tests/hermes_cli/test_plugin_api_compat.py) from an
isolated HOME whose fleet root is <HOME>/.hermes and whose plugin-serving profile is
<HOME>/.hermes/profiles/<name>, so the profile-local sentinel and the fleet-root sentinel are DISTINCT
paths. Core `agent.estop` is real; `disengage` is spied with a wrapper that calls the real function.
The plugin's delivery goes through core's send engine `tools.send_message_tool.send_message_tool` (the
`hermes send` path), replaced by a recorder (no network). Every test except test_ac_cost_f30_b_10 (the explicit non-regression) fails on d0402936
because `resume.py` and the resume tables do not exist there.

Behaviour contract only: never reads source files, no network, nothing under the real ~/.hermes,
asserts structure (field presence, equality to stored values), never a secret value.
"""

import builtins
import contextlib
import io
import json
import os
import pathlib
import shutil
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from hermes_cli.plugins import PluginManager

PLUGIN_KEY = "nv-cost-cap"
REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_SRC = REPO_ROOT / "plugins" / PLUGIN_KEY

PROFILE = "costbot"          # the profile that serves the stopped session
APPROVER_PROFILE = "approver"  # a different profile that serves the approver channel (AC-17)
OPERATOR = "gateway:slack:ws-1:op-1"
STRANGER = "gateway:slack:ws-1:stranger"
APPROVER_ONLY_OP = "gateway:slack:ws-1:approver-only"
TARGET = "slack:C0APPROVERS"
TTL = 3600
NOTICE_TAIL = "if you had just paused, pause again"


# --- fixture ---------------------------------------------------------------

def _settings(**extra):
    base = {
        "operators": [OPERATOR],
        "profile_ceiling_usd": 1.0,
        "escalation_increment_usd": 10.0,
        "resume_card_target": TARGET,
        "resume_card_ttl_seconds": TTL,
        "resume_card_drain_seconds": 1,
    }
    base.update(extra)
    return base


def _write_profile(home, settings):
    (home / "plugins").mkdir(parents=True, exist_ok=True)
    dest = home / "plugins" / PLUGIN_KEY
    if not dest.exists():
        shutil.copytree(PLUGIN_SRC, dest)
    cfg = {"plugins": {"enabled": [PLUGIN_KEY], "entries": {PLUGIN_KEY: {"settings": settings}}}}
    (home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")


class Env(SimpleNamespace):
    """Handles for one test: module, manager, homes, recorder, spy."""


@pytest.fixture
def env(tmp_path, monkeypatch):
    if not PLUGIN_SRC.is_dir():
        pytest.fail(f"plugin source not found at {PLUGIN_SRC}")
    root = tmp_path / ".hermes"
    home = root / "profiles" / PROFILE
    approver_home = root / "profiles" / APPROVER_PROFILE
    _write_profile(root, _settings())
    _write_profile(home, _settings())
    _write_profile(approver_home, _settings(operators=[APPROVER_ONLY_OP]))
    bundled = tmp_path / "empty_bundled"
    bundled.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")

    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled is True, loaded.error
    assert loaded.error is None
    mod = loaded.module
    assert mod is not None

    from agent import estop

    import tools.send_message_tool as smt

    sent = []

    def _send(args, **kwargs):
        sent.append({"tool": "send_message", "args": dict(args)})
        return json.dumps({"success": True, "platform": "slack", "chat_id": "C0APPROVERS"})

    # core's send engine, exactly what `hermes send` calls (send_message is not a registry tool)
    monkeypatch.setattr(smt, "send_message_tool", _send)

    calls = []
    real_disengage = estop.disengage

    def _spy_disengage(*a, **k):
        calls.append({"present_before": [p.exists() for p in estop._candidate_sentinel_paths()]})
        result = real_disengage(*a, **k)
        calls[-1]["result"] = result
        return result

    monkeypatch.setattr(estop, "disengage", _spy_disengage)
    return Env(mod=mod, manager=manager, estop=estop, home=home, root=root, approver_home=approver_home,
               sent=sent, disengage_calls=calls, real_disengage=real_disengage, tmp=tmp_path,
               monkeypatch=monkeypatch)


# --- helpers ---------------------------------------------------------------

def _resume(env):
    return env.mod.resume


def _seed_state_db(home, sid, cost):
    conn = sqlite3.connect(home / "state.db")
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, parent_session_id TEXT, "
            "estimated_cost_usd REAL DEFAULT 0, cost_status TEXT, started_at REAL NOT NULL DEFAULT 0, "
            "ended_at REAL, end_reason TEXT)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS session_model_usage (session_id TEXT, model TEXT, task TEXT DEFAULT '', "
            "billing_provider TEXT DEFAULT '', billing_base_url TEXT DEFAULT '', billing_mode TEXT DEFAULT '', "
            "estimated_cost_usd REAL NOT NULL DEFAULT 0, cost_status TEXT, PRIMARY KEY (session_id, model, "
            "billing_provider, billing_base_url, billing_mode, task))")
        conn.execute("INSERT OR REPLACE INTO sessions (id, estimated_cost_usd) VALUES (?, ?)", (sid, cost))
        conn.execute("INSERT OR REPLACE INTO session_model_usage (session_id, model, task, estimated_cost_usd, "
                     "cost_status) VALUES (?, 'opus', '', ?, 'estimated')", (sid, cost))
        conn.commit()
    finally:
        conn.close()


def _event(text, *, user="op-1", scope="ws-1", profile=PROFILE, route="rk-approver", session_id=None):
    source = SimpleNamespace(platform=SimpleNamespace(value="slack"), scope_id=scope, guild_id=None,
                             user_id=user, chat_id="C0APPROVERS", thread_id=None, profile=profile)
    is_cmd = text.startswith("/")
    parts = text.lstrip("/").split(None, 1)
    event = SimpleNamespace(source=source, text=text,
                            get_command=lambda: parts[0] if is_cmd else None,
                            get_command_args=lambda: (parts[1] if len(parts) > 1 else "") if is_cmd else "")
    store = SimpleNamespace(peek_session_id=lambda key: session_id if key == route else None)
    gateway = SimpleNamespace(_session_key_for_source=lambda s: route, session_store=store)
    return event, gateway, store


def _pgd(env, text, **kw):
    """Drive one inbound through the REAL pre_gateway_dispatch hook; return (hook results, reply notices).

    The plugin replies over the gateway rail (_deliver_notice -> ctx.spawn_task(deliver(...))); the test
    gateway's deliver coroutine is run to completion so its text is captured. Any other task the plugin
    spawns (its reconcile / drain loops) is closed unstarted — the test drives those passes directly.
    """
    import asyncio

    event, gateway, store = _event(text, **kw)
    replies = []

    async def _deliver_platform_notice(source, content):
        replies.append(content)

    gateway._deliver_platform_notice = _deliver_platform_notice

    def _spawn(coro, name=None):
        if getattr(getattr(coro, "cr_code", None), "co_name", "") == "_deliver_platform_notice":
            asyncio.run(coro)
        else:
            coro.close()

    env.monkeypatch.setattr(env.mod._CTX, "spawn_task", _spawn)
    results = env.manager.invoke_hook("pre_gateway_dispatch", event=event, gateway=gateway, session_store=store)
    return results, replies


@contextlib.contextmanager
def _pinned(profile):
    from hermes_cli.profiles import get_profile_dir
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(str(get_profile_dir(profile)))
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def _own_sp(env):
    return env.home / "ESTOP"


def _root_sp(env):
    return env.root / "ESTOP"


def _clear_all(env):
    """Test-side operator `hermes resume` (core, outside any plugin path) so the next stop is fresh."""
    env.real_disengage()
    assert not _own_sp(env).exists() and not _root_sp(env).exists()


def _cards(env, status=None):
    return env.mod.store.resume_cards(status=status)


def _card(env, card_id):
    matches = [c for c in _cards(env) if c["card_id"] == card_id]
    assert len(matches) == 1, (card_id, matches)
    return matches[0]


def _decisions(env, card_id):
    return env.mod.store.resume_decisions(card_id=card_id)


def _new_stop(env, sid, *, blocked):
    """Publish one OWNED stop through the plugin's real engage seam and return its new pending card.

    blocked=True leaves a Tier-2 money block (effective 6.2 over the 1.00 ceiling, blocked=1, a pending
    breach episode); blocked=False leaves a session that is runnable apart from the belt.
    """
    store = env.mod.store
    before = {c["card_id"] for c in _cards(env)}
    spend = 6.2 if blocked else 0.5
    _seed_state_db(env.home, sid, spend)
    store.set_state(sid, effective_usd=spend, window_start_total=0.0, day_start_total=0.0, budget_gen=1,
                    blocked=1 if blocked else 0, immortal=0)
    if blocked:
        store.record_episode(sid, "breach", 1, "2026-10-05", dedup_key=f"{sid}:breach:1")
    store.engage_owned(sid)
    new = [c for c in _cards(env) if c["card_id"] not in before]
    assert len(new) == 1, f"an owned publish must raise exactly one card, got {new!r}"
    assert new[0]["status"] == "pending"
    return new[0]


def _approve(env, card_id, principal=OPERATOR):
    return _resume(env).approve_resume(card_id, principal)


def _deny(env, card_id, principal=OPERATOR):
    return _resume(env).deny_resume(card_id, principal)


def _drain(env):
    return _resume(env).drain_outbox_once()


def _messages(env):
    return [c["args"].get("message", "") for c in env.sent if c["tool"] == "send_message"]


def _snapshot(env):
    """Everything a resolution could mutate: card rows, decision rows, outbox rows, sentinel bytes."""
    store = env.mod.store
    def _b(p):
        return p.read_bytes() if p.exists() else None
    return (repr(store.resume_cards()), repr(store.resume_decisions()), repr(store.resume_outbox()),
            _b(_own_sp(env)), _b(_root_sp(env)))


# --- acceptance tests ------------------------------------------------------


def test_ac_cost_f30_b_1(env):
    """AC-COST-F30.b-1: an owned ESTOP publish raises exactly ONE pending resume card bound to that stop, for a Tier-2 breach through the real boundary path and for a mortal Stop resolution; a foreign or EEXIST-left engage raises none; a one-shot drain kicked right after the publish delivers it exactly once to resume_card_target through core's send engine (the hermes send path), with no later inbound and no manual drain, carrying session, spend, cap, ceiling, reason, card id and both action commands; a second drain sends nothing; the delivered Deny command, replayed through pre_gateway_dispatch, resolves that card."""
    mod, estop = env.mod, env.estop

    # (a) Tier-2 breach through the REAL boundary path: on_session_start then a pre_gateway_dispatch whose
    # fire delta crosses the 1.00 ceiling engages the owned belt; exactly one card is raised for it.
    env.manager.invoke_hook("on_session_start", session_id="s-breach", resumed=False, model="opus", platform="cli")
    _seed_state_db(env.home, "s-breach", 6.2)
    results, _ = _pgd(env, "hi", route="rk-breach", session_id="s-breach")
    assert any(isinstance(r, dict) and r.get("action") == "skip" for r in results), results
    assert _own_sp(env).exists(), "the breach must engage the profile-local belt"
    cards = _cards(env)
    assert len(cards) == 1, cards
    card = cards[0]
    assert card["status"] == "pending" and card["origin"] == "breach" and card["session_id"] == "s-breach"
    assert card["profile"] == PROFILE and card["card_id"].startswith(f"{PROFILE}:")
    assert card["published_body"] == _own_sp(env).read_bytes(), "the card binds the exact published bytes"
    assert card["receipt"] == mod.store.estop_receipt(), "the card binds the plugin's own receipt"
    assert sorted(card["candidates"]) == sorted(str(p) for p in estop._candidate_sentinel_paths())

    # delivery: the one-shot kick after the publish already sent it — no later inbound, no manual drain.
    msgs = _messages(env)
    assert len(msgs) == 1, env.sent
    assert env.sent[0]["args"].get("target") == TARGET
    text = msgs[0]
    for needle in ("s-breach", "$6.20", "$1.00", card["reason"], card["card_id"],
                   f"/cost resume approve {card['card_id']}", f"/cost resume deny {card['card_id']}"):
        assert needle in text, (needle, text)
    assert "cap " in text and "ceiling " in text, text
    assert _drain(env) == 0 and len(_messages(env)) == 1, "a second drain must send nothing"

    # the DELIVERED Deny command, replayed from the approver channel, resolves exactly that card.
    deny_cmd = f"/cost resume deny {card['card_id']}"
    _, replies = _pgd(env, deny_cmd)
    assert _card(env, card["card_id"])["status"] == "denied", replies
    assert replies, "the approver gets a reply on the gateway rail"
    assert _own_sp(env).exists()

    # (b) mortal Stop resolution engages through the same owned seam and raises its own card.
    _clear_all(env)
    mod.store.set_state("s-stop", effective_usd=0.5, window_start_total=0.0, day_start_total=0.0,
                        budget_gen=1, blocked=0, immortal=0)
    ep = mod.store.record_episode("s-stop", "escalation", 1, "2026-10-05", dedup_key="s-stop:esc:1")
    sent_before = len(_messages(env))
    res = mod.resolve_escalation("s-stop", ep, 1, "stop", OPERATOR)  # off-gateway: no loop, no inbound
    assert res["granted"] is True, res
    stop_cards = [c for c in _cards(env) if c["session_id"] == "s-stop"]
    assert len(stop_cards) == 1 and stop_cards[0]["origin"] == "stop", stop_cards
    out = [r for r in mod.store.resume_outbox() if r["card_id"] == stop_cards[0]["card_id"] and r["kind"] == "card"]
    assert len(out) == 1 and out[0]["status"] == "sent", out
    assert len(_messages(env)) == sent_before + 1 and stop_cards[0]["card_id"] in _messages(env)[-1]

    # (c) a FOREIGN pre-existing pause: the plugin's engage EEXIST-leaves it and raises no card.
    _clear_all(env)
    estop.engage(reason="operator maintenance")
    n = len(_cards(env))
    mod.store.engage_owned("s-foreign")
    assert len(_cards(env)) == n, "no card for a stop the plugin does not own"


def test_ac_cost_f30_b_2(env):
    """AC-COST-F30.b-2: when delivery is unavailable the stop is kept and the error is recorded on the card's delivery row — no resume_card_target configured (no-target, no send attempted), or send_message returns an error."""
    mod = env.mod

    # (a) send_message returns an error: recorded, not retried, stop kept, card still pending.
    import tools.send_message_tool as smt

    env.monkeypatch.setattr(smt, "send_message_tool",
                            lambda args, **k: env.sent.append({"tool": "send_message", "args": args})
                            or json.dumps({"error": "platform not connected"}))
    card = _new_stop(env, "s-deliv", blocked=True)
    before = _own_sp(env).read_bytes()
    rows = [r for r in mod.store.resume_outbox() if r["card_id"] == card["card_id"] and r["kind"] == "card"]
    assert len(rows) == 1 and rows[0]["status"] == "failed" and rows[0]["error"], rows
    attempts = len(env.sent)
    _drain(env)
    assert len(env.sent) == attempts, "a failed delivery is never retried"
    assert _own_sp(env).read_bytes() == before and _card(env, card["card_id"])["status"] == "pending"

    # (b) no target configured: recorded no-target, nothing dispatched.
    _clear_all(env)
    cfg = yaml.safe_load((env.home / "config.yaml").read_text(encoding="utf-8"))
    cfg["plugins"]["entries"][PLUGIN_KEY]["settings"].pop("resume_card_target")
    (env.home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    env.sent.clear()
    card2 = _new_stop(env, "s-notarget", blocked=True)
    rows2 = [r for r in mod.store.resume_outbox() if r["card_id"] == card2["card_id"] and r["kind"] == "card"]
    assert len(rows2) == 1 and rows2[0]["status"] == "failed" and rows2[0]["error"] == "no-target", rows2
    assert env.sent == [], "no send is attempted without a target"
    assert _own_sp(env).exists() and _card(env, card2["card_id"])["status"] == "pending"


def test_ac_cost_f30_b_3(env):
    """AC-COST-F30.b-3: an authorized Approve on a matching, runnable stop resumes exactly that stop through core's resume API — agent.estop.disengage called exactly once (spy wrapping the real function), sentinel gone afterwards, card approved, reply says it was resumed."""
    card = _new_stop(env, "s-ok", blocked=False)
    assert _own_sp(env).exists()
    _, replies = _pgd(env, f"/cost resume approve {card['card_id']}")
    assert len(env.disengage_calls) == 1, env.disengage_calls
    assert env.disengage_calls[0]["result"] is True
    assert not _own_sp(env).exists(), "the stop is lifted by core disengage()"
    assert _card(env, card["card_id"])["status"] == "approved"
    assert replies and any("Resumed" in r and card["card_id"] in r for r in replies), replies


def test_ac_cost_f30_b_4(env):
    """AC-COST-F30.b-4: Deny keeps the stop and records deny with the namespaced principal, time and stop identity; disengage is never called."""
    card = _new_stop(env, "s-deny", blocked=True)
    before = _own_sp(env).read_bytes()
    res = _deny(env, card["card_id"])
    assert res["ok"] is True and res["reason"] == "denied", res
    assert _own_sp(env).read_bytes() == before and env.disengage_calls == []
    assert _card(env, card["card_id"])["status"] == "denied"
    rows = [d for d in _decisions(env, card["card_id"]) if d["decision"] == "deny"]
    assert len(rows) == 1, _decisions(env, card["card_id"])
    row = rows[0]
    assert row["actor"] == OPERATOR and row["ts"]
    assert row["stop"]["receipt"] == card["receipt"] and row["stop"]["sentinel_path"] == str(_own_sp(env))
    assert row["stop"]["profile"] == PROFILE and row["stop"]["engaged_at"] == card["engaged_at"]


def test_ac_cost_f30_b_5(env):
    """AC-COST-F30.b-5: a pending card past resume_card_ttl_seconds is recorded expired and the stop is kept — on the reconcile_once() sweep (including after an earlier money-block refusal) and on a late resolve; a later Approve never calls disengage."""
    resume = _resume(env)
    t0 = resume._clock()
    # (a) sweep after a money-block refusal (which keeps the card pending).
    card = _new_stop(env, "s-exp", blocked=True)
    assert _approve(env, card["card_id"])["reason"] == "money-block-active"
    assert _card(env, card["card_id"])["status"] == "pending"
    env.monkeypatch.setattr(resume, "_clock", lambda: t0 + TTL + 10)
    env.mod.reconcile_once()
    assert _card(env, card["card_id"])["status"] == "expired"
    assert [d for d in _decisions(env, card["card_id"]) if d["decision"] == "expired"]
    assert _own_sp(env).exists()
    assert _approve(env, card["card_id"])["reason"] == "already-resolved" and env.disengage_calls == []

    # (b) late resolve with no sweep in between.
    _clear_all(env)
    env.monkeypatch.setattr(resume, "_clock", lambda: t0)
    card2 = _new_stop(env, "s-exp2", blocked=False)
    env.monkeypatch.setattr(resume, "_clock", lambda: t0 + TTL + 10)
    res = _approve(env, card2["card_id"])
    assert res["ok"] is False and res["reason"] == "expired", res
    assert _card(env, card2["card_id"])["status"] == "expired" and _own_sp(env).exists()
    assert env.disengage_calls == []


def test_ac_cost_f30_b_6(env):
    """AC-COST-F30.b-6: an operator re-pause between card and Approve (agent.estop.engage(reason=...) overwrites the body in place) makes Approve refuse with reason repaused and its own text; the sentinel stays byte-identical, disengage is never called, the card is consumed."""
    card = _new_stop(env, "s-repause", blocked=False)
    env.estop.engage(reason="operator: hold everything")
    repaused = _own_sp(env).read_bytes()
    res = _approve(env, card["card_id"])
    assert res["ok"] is False and res["reason"] == "repaused", res
    assert res["text"] == _resume(env).REFUSAL_TEXT["repaused"]
    assert _own_sp(env).read_bytes() == repaused and env.disengage_calls == []
    assert _card(env, card["card_id"])["status"] == "refused"


def test_ac_cost_f30_b_7(env):
    """AC-COST-F30.b-7: a fleet-root operator pause plus the plugin's profile-local belt makes Approve refuse with fleet-pause-present; both sentinels stay byte-identical, disengage is never called, and the plugin's candidate set equals core's _candidate_sentinel_paths() under the pin."""
    card = _new_stop(env, "s-fleet", blocked=False)
    assert sorted(card["candidates"]) == sorted(str(p) for p in env.estop._candidate_sentinel_paths())
    assert str(_root_sp(env)) in card["candidates"], "the fleet root is one of core's candidates"
    _root_sp(env).write_text(json.dumps({"engaged_at": "2026-10-05T00:00:00+00:00",
                                         "reason": "fleet maintenance"}) + "\n", encoding="utf-8")
    own, root = _own_sp(env).read_bytes(), _root_sp(env).read_bytes()
    res = _approve(env, card["card_id"])
    assert res["ok"] is False and res["reason"] == "fleet-pause-present", res
    assert res["text"] == _resume(env).REFUSAL_TEXT["fleet-pause-present"]
    assert _own_sp(env).read_bytes() == own and _root_sp(env).read_bytes() == root
    assert env.disengage_calls == []


# -- AC-8 runtime mutation probe --

_REMOVE, _CREATE, _WRITE = "remove", "create", "write"


@contextlib.contextmanager
def _sentinel_probe(env):
    """Record every mutation primitive that targets a candidate sentinel path while the block runs.

    Each event is (kind, primitive, path, inside_core_disengage). Primitives run for real: the probe
    observes, it never suppresses. inside_core_disengage is True only while the REAL agent.estop.disengage
    is on the stack (the spy installed by the fixture wraps it), so a plugin call of a raw primitive is
    distinguishable from core's own resume path.
    """
    import shutil as _shutil

    estop = env.estop
    paths = {str(p) for p in (_own_sp(env), _root_sp(env))}
    with _pinned(APPROVER_PROFILE):
        paths |= {str(p) for p in estop._candidate_sentinel_paths()}
    events = []
    depth = {"n": 0}
    action = {"tag": "setup"}
    spy = estop.disengage

    def _hit(p):
        try:
            return os.fspath(p) in paths
        except TypeError:
            return False

    def _rec(kind, prim, p):
        events.append((kind, prim, os.fspath(p), depth["n"] > 0, action["tag"]))

    def _wrap_disengage(*a, **k):
        depth["n"] += 1
        try:
            return spy(*a, **k)
        finally:
            depth["n"] -= 1

    real = {
        "os.unlink": os.unlink, "os.remove": os.remove, "os.rename": os.rename, "os.replace": os.replace,
        "os.open": os.open, "os.link": os.link, "os.symlink": os.symlink, "os.truncate": os.truncate,
        "open": builtins.open, "io.open": io.open, "shutil.move": _shutil.move,
        "Path.unlink": pathlib.Path.unlink, "Path.rename": pathlib.Path.rename,
        "Path.replace": pathlib.Path.replace, "Path.write_text": pathlib.Path.write_text,
        "Path.write_bytes": pathlib.Path.write_bytes, "Path.touch": pathlib.Path.touch,
        "Path.open": pathlib.Path.open, "Path.hardlink_to": pathlib.Path.hardlink_to,
        "Path.symlink_to": pathlib.Path.symlink_to,
    }

    def _mode_writes(mode):
        return any(c in (mode or "r") for c in "wax+")

    def _flags_write(flags):
        return bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))

    def p1(kind, name):
        def f(path, *a, **k):
            if _hit(path):
                _rec(kind, name, path)
            return real[name](path, *a, **k)
        return f

    def p2(name):
        def f(src, dst, *a, **k):
            for x in (src, dst):
                if _hit(x):
                    _rec(_REMOVE if x is src else _CREATE, name, x)
            return real[name](src, dst, *a, **k)
        return f

    def os_open(path, flags, *a, **k):
        if _hit(path) and _flags_write(flags):
            _rec(_WRITE, "os.open", path)
        return real["os.open"](path, flags, *a, **k)

    def os_link(src, dst, *a, **k):
        # recorded only when the link lands: an EEXIST no-replace attempt (the plugin finding a stop already
        # engaged, e.g. a second breach) leaves the sentinel untouched and is not a mutation.
        out = real["os.link"](src, dst, *a, **k)
        if _hit(dst):
            _rec(_CREATE, "os.link", dst)
        return out

    def b_open(file, mode="r", *a, **k):
        if not isinstance(file, int) and _hit(file) and _mode_writes(mode):
            _rec(_WRITE, "open", file)
        return real["open"](file, mode, *a, **k)

    def path_open(self, mode="r", *a, **k):
        if _hit(self) and _mode_writes(mode):
            _rec(_WRITE, "Path.open", self)
        return real["Path.open"](self, mode, *a, **k)

    def path_m(kind, name):
        def f(self, *a, **k):
            if _hit(self):
                _rec(kind, name, self)
            return real[name](self, *a, **k)
        return f

    def path_link(name):
        def f(self, target, *a, **k):
            if _hit(self):
                _rec(_CREATE, name, self)
            return real[name](self, target, *a, **k)
        return f

    def path_mv(name):
        def f(self, target, *a, **k):
            if _hit(self):
                _rec(_REMOVE, name, self)
            if _hit(target):
                _rec(_CREATE, name, target)
            return real[name](self, target, *a, **k)
        return f

    patches = [
        (os, "unlink", p1(_REMOVE, "os.unlink")), (os, "remove", p1(_REMOVE, "os.remove")),
        (os, "rename", p2("os.rename")), (os, "replace", p2("os.replace")), (os, "open", os_open),
        (os, "link", os_link),
        (os, "symlink", lambda s, d, *a, **k: (_rec(_CREATE, "os.symlink", d) if _hit(d) else None)
         or real["os.symlink"](s, d, *a, **k)),
        (os, "truncate", p1(_WRITE, "os.truncate")), (builtins, "open", b_open), (io, "open", b_open),
        (_shutil, "move", p2("shutil.move")),
        (pathlib.Path, "unlink", path_m(_REMOVE, "Path.unlink")), (pathlib.Path, "rename", path_mv("Path.rename")),
        (pathlib.Path, "replace", path_mv("Path.replace")),
        (pathlib.Path, "write_text", path_m(_WRITE, "Path.write_text")),
        (pathlib.Path, "write_bytes", path_m(_WRITE, "Path.write_bytes")),
        (pathlib.Path, "touch", path_m(_WRITE, "Path.touch")), (pathlib.Path, "open", path_open),
        (pathlib.Path, "hardlink_to", path_link("Path.hardlink_to")),
        (pathlib.Path, "symlink_to", path_link("Path.symlink_to")),
        (estop, "disengage", _wrap_disengage),
    ]
    with pytest.MonkeyPatch.context() as mp:
        for obj, attr, fn in patches:
            mp.setattr(obj, attr, fn)
        yield events, action


def _probe_violations(events, *, sanctioned=()):
    """Violations: any write; any removal unless it happened INSIDE core disengage while the driven action was
    one of ``sanctioned`` (the authorized matching Approve); any creation other than the owned os.link."""
    bad = []
    for kind, prim, path, in_core, tag in events:
        if kind == _REMOVE and in_core and tag in sanctioned:
            continue
        if kind == _CREATE and prim == "os.link" and not in_core and tag == "owned-publish":
            continue
        bad.append((kind, prim, path, in_core, tag))
    return bad


class _FakeCtx:
    """Records what register(ctx) registers, so AC-8 drives the entry points the plugin itself declares."""

    def __init__(self, real_ctx):
        self._real = real_ctx
        self.hooks, self.middleware, self.cli = [], [], []

    def register_hook(self, name, cb):
        self.hooks.append((name, cb))

    def register_middleware(self, kind, cb):
        self.middleware.append((kind, cb))

    def register_cli_command(self, **kw):
        self.cli.append(kw)

    def __getattr__(self, name):
        if name.startswith("register_"):
            raise AssertionError(f"register(ctx) called {name}: AC-8 must learn to drive this new entry point")
        return getattr(self._real, name)


def _hook_kwargs(name, sid):
    """Generic payloads for each hook the plugin registers; unknown names get session_id only."""
    base = {"session_id": sid}
    if name == "post_api_request":
        return {**base, "model": "opus", "usage": {"input_tokens": 10, "output_tokens": 10},
                "api_request_id": f"{sid}-a1", "provider": "anthropic", "base_url": ""}
    if name in ("pre_tool_call", "post_tool_call"):
        return {**base, "tool_name": "terminal", "args": {}, "task_id": "t", "tool_call_id": "tc",
                "turn_id": "tn", "api_request_id": "a", "result": "{}"}
    if name == "on_session_start":
        return {**base, "resumed": False, "model": "opus", "platform": "cli"}
    if name == "on_session_end":
        return {**base, "completed": True, "failed": False, "interrupted": False, "model": "opus",
                "platform": "cli"}
    if name == "pre_gateway_dispatch":
        event, gateway, store = _event("hi", route="rk-probe", session_id=sid)
        return {"event": event, "gateway": gateway, "session_store": store}
    return base


def _drive_everything(env, fake, action, card, *, approve_is_sanctioned):
    """Drive every entry point register(ctx) declared, plus the /cost family, card actions, the dashboard
    router, the CLI and reconcile_once — each under its own action tag, so a probe event names its cause.
    Callback errors fail the test. Hooks, money commands and CLI act on a SEPARATE scratch session (closed
    afterwards) so they can never legitimately resolve the card's money question."""
    import argparse

    mod = env.mod
    sid = f"{card['session_id']}-hooks"
    for name, cb in fake.hooks:
        action["tag"] = f"hook:{name}"
        cb(**_hook_kwargs(name, sid))
    for kind, cb in fake.middleware:
        action["tag"] = f"middleware:{kind}"
        cb(request={"model": "opus"}, next_call=lambda payload=None: {"ok": True}, session_id=sid,
           model="opus", api_mode="anthropic_messages", provider="anthropic", base_url="",
           api_request_id="x", turn_id="t")
    for text in ("/cost continue", "/cost stop", "/cost ceiling 12.50", "/cost resume bogus"):
        action["tag"] = f"cmd:{text}"
        _pgd(env, text, route="rk-probe", session_id=sid)
    for spec in fake.cli:
        parser = argparse.ArgumentParser()
        spec["setup_fn"](parser)
        for argv in (["show"], ["resolve", "--profile", PROFILE, "--session", sid, "--episode", "none",
                                "--budget-gen", "1", "--decision", "continue"]):
            action["tag"] = f"cli:{argv[0]}"
            spec["handler_fn"](parser.parse_args(argv))
    action["tag"] = "dashboard"
    _dashboard_round_trip(env)
    mod._on_session_finalize(session_id=sid)
    action["tag"] = "reconcile"
    mod.reconcile_once()
    action["tag"] = "drain"
    _drain(env)
    action["tag"] = "approve:unauthorized"
    _pgd(env, f"/cost resume approve {card['card_id']}", user="stranger")
    action["tag"] = "approve:authorized" if approve_is_sanctioned else "approve:refused-expected"
    _pgd(env, f"/cost resume approve {card['card_id']}")
    action["tag"] = "deny:after"
    _pgd(env, f"/cost resume deny {card['card_id']}")
    action["tag"] = "teardown"


def _dashboard_round_trip(env):
    import importlib.util
    import sys

    from fastapi import FastAPI
    from starlette.testclient import TestClient

    api_path = env.home / "plugins" / PLUGIN_KEY / "dashboard" / "plugin_api.py"
    name = f"hermes_dashboard_plugin_{PLUGIN_KEY}_probe"
    spec = importlib.util.spec_from_file_location(name, api_path)
    api_mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = api_mod
    spec.loader.exec_module(api_mod)
    app = FastAPI()
    app.include_router(api_mod.router, prefix=f"/api/plugins/{PLUGIN_KEY}")
    client = TestClient(app)
    client.get(f"/api/plugins/{PLUGIN_KEY}/escalations?profile={PROFILE}")
    client.post(f"/api/plugins/{PLUGIN_KEY}/escalations/none/resolve?profile={PROFILE}", json={"decision": "continue"})


def test_ac_cost_f30_b_8(env):
    """AC-COST-F30.b-8: no plugin code path mutates any candidate sentinel outside core's resume API — a runtime probe drives every entry point register(ctx) declares (from a FAKE ctx) plus the /cost commands, card Approve/Deny, the dashboard router, the CLI and reconcile_once, intercepting every write/truncate/link/remove primitive on every candidate path; the only removals are inside agent.estop.disengage from an authorized matching Approve and the only creation is the owned publish; with disengage patched to raise, the sentinel survives; plugin doubles that unlink or truncate directly fail the probe."""
    mod = env.mod
    real_ctx = mod._CTX
    env.monkeypatch.setattr(mod, "_CTX", real_ctx)  # restored at teardown; register(fake) rebinds it below
    fake = _FakeCtx(real_ctx)
    mod.register(fake)
    mod._CTX = real_ctx
    assert fake.hooks and fake.middleware and fake.cli, "register(ctx) must declare its entry points"

    with _sentinel_probe(env) as (events, action):
        action["tag"] = "owned-publish"
        card_ok = _new_stop(env, "s-probe-ok", blocked=False)
        _drive_everything(env, fake, action, card_ok, approve_is_sanctioned=True)
    creations = [e for e in events if e[0] == _CREATE]
    assert creations == [(_CREATE, "os.link", str(_own_sp(env)), False, "owned-publish")], creations
    removals = [e for e in events if e[0] == _REMOVE]
    assert removals and all(e[3] and e[4] == "approve:authorized" for e in removals), events
    assert _probe_violations(events, sanctioned=("approve:authorized",)) == [], events
    assert _card(env, card_ok["card_id"])["status"] == "approved"

    # a blocked stop: every entry point runs, NOTHING may remove the sentinel (no runnable matching Approve).
    card_blk = _new_stop(env, "s-probe-blk", blocked=True)
    with _sentinel_probe(env) as (events2, action2):
        _drive_everything(env, fake, action2, card_blk, approve_is_sanctioned=False)
    assert _probe_violations(events2) == [], events2
    assert _own_sp(env).exists() and _card(env, card_blk["card_id"])["status"] == "denied"

    # disengage raises: the matching Approve fails closed and no plugin path removes the sentinel anyway.
    mod._on_session_finalize(session_id="s-probe-blk")
    _clear_all(env)
    card_r = _new_stop(env, "s-probe-raise", blocked=False)

    def _boom(*a, **k):
        raise OSError("disengage failed")

    env.monkeypatch.setattr(env.estop, "disengage", _boom)
    with _sentinel_probe(env) as (events3, action3):
        _drive_everything(env, fake, action3, card_r, approve_is_sanctioned=False)
    assert _probe_violations(events3) == [], events3
    assert _own_sp(env).exists(), "the sentinel survives a raising disengage"
    assert _card(env, card_r["card_id"])["status"] == "resume-failed"


def _double_ctx_with(env, bad_hook):
    """A fake ctx whose registration also carries a plugin double's extra hook, driven like any other."""
    fake = _FakeCtx(env.mod._CTX)
    env.monkeypatch.setattr(env.mod, "_CTX", env.mod._CTX)
    env.mod.register(fake)
    env.mod._CTX = fake._real
    fake.hooks.append(("on_session_start", bad_hook))
    return fake


def test_probe_flags_direct_unlink_double(env):
    """Negative control: a plugin double whose registered callback unlinks the sentinel directly fails the AC-8 probe."""
    card = _new_stop(env, "s-neg-unlink", blocked=True)
    fake = _double_ctx_with(env, lambda **kw: _own_sp(env).unlink(missing_ok=True))
    with _sentinel_probe(env) as (events, action):
        _drive_everything(env, fake, action, card, approve_is_sanctioned=False)
    bad = _probe_violations(events, sanctioned=("approve:authorized",))
    assert any(e[0] == _REMOVE and e[4] == "hook:on_session_start" for e in bad), events


def test_probe_flags_truncate_double(env):
    """Negative control: a plugin double whose registered callback truncates the sentinel (open 'w') fails the AC-8 probe."""
    card = _new_stop(env, "s-neg-trunc", blocked=True)

    def _truncate(**kw):
        with open(_own_sp(env), "w", encoding="utf-8"):
            pass

    fake = _double_ctx_with(env, _truncate)
    with _sentinel_probe(env) as (events, action):
        _drive_everything(env, fake, action, card, approve_is_sanctioned=False)
    bad = _probe_violations(events, sanctioned=("approve:authorized",))
    assert any(e[0] == _WRITE and e[4] == "hook:on_session_start" for e in bad), events


def test_ac_cost_f30_b_9(env):
    """AC-COST-F30.b-9: an unauthorized Approve or Deny mutates nothing (card status, decision rows, outbox, sentinel unchanged; no disengage); a second resolution of a resolved card gets already-resolved; two concurrent authorized Approves call disengage exactly once."""
    card = _new_stop(env, "s-authz", blocked=False)
    before = _snapshot(env)
    for principal in (STRANGER, None, "op-1", "gateway:slack:ws-2:op-1"):
        assert _approve(env, card["card_id"], principal)["reason"] == "unauthorized"
        assert _deny(env, card["card_id"], principal)["reason"] == "unauthorized"
    assert _snapshot(env) == before and env.disengage_calls == []

    results = []
    barrier = threading.Barrier(2)

    def _go():
        barrier.wait(5)
        results.append(_approve(env, card["card_id"]))

    threads = [threading.Thread(target=_go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(results) == 2, results
    assert sorted(r["reason"] for r in results) == ["already-resolved", "resumed"], results
    assert len(env.disengage_calls) == 1, env.disengage_calls
    assert _deny(env, card["card_id"])["reason"] == "already-resolved"
    assert len(env.disengage_calls) == 1


def test_ac_cost_f30_b_10(env):
    """AC-COST-F30.b-10: (derived, non-regression) COST-F30 outcomes are unchanged on a carded stop — Continue, runnable ceiling, Stop and reconcile_once() each leave the owned sentinel byte- and inode-identical, report estop_disposition="left" / manual_resume_required=true, and render _MANUAL_RESUME_NOTICE / _CEILING_BELT_LEFT_SUFFIX verbatim. Passes on d0402936 too."""
    mod, store = env.mod, env.mod.store
    manual = ("Continue applied — the per-session cost block is cleared, but the profile ESTOP belt remains "
              "engaged (gates cron/kanban/new inbounds); resume via `hermes resume` or the UA-28 upstream lock")
    ceiling_suffix = ("The per-session cost block is cleared, but the profile ESTOP belt remains engaged; "
                      "resume via `hermes resume` or the UA-28 upstream lock.")

    def _breach(sid):
        store.set_state(sid, effective_usd=6.2, window_start_total=0.0, day_start_total=0.0, budget_gen=1,
                        blocked=1, immortal=0)
        ep = store.record_episode(sid, "breach", 1, "2026-10-05", dedup_key=f"{sid}:breach:1")
        if not _own_sp(env).exists():
            store.engage_owned(sid)
        return ep

    def _same(fn):
        b, ino = _own_sp(env).read_bytes(), _own_sp(env).stat().st_ino
        out = fn()
        assert _own_sp(env).read_bytes() == b and _own_sp(env).stat().st_ino == ino
        return out

    ep = _breach("s-nr-cont")
    res = _same(lambda: mod.resolve_escalation("s-nr-cont", ep, 1, "continue", OPERATOR))
    assert res["granted"] and res["estop_disposition"] == "left" and res["manual_resume_required"] is True
    assert manual in mod._cost_outcome_text(res)

    ep = _breach("s-nr-ceil")
    res = _same(lambda: mod.set_episode_ceiling(PROFILE, "s-nr-ceil", ep, 1, "100.00", OPERATOR))
    assert res["granted"] and res["estop_disposition"] == "left"
    assert ceiling_suffix in mod._cost_outcome_text(res)

    store.set_state("s-nr-stop", effective_usd=0.5, window_start_total=0.0, day_start_total=0.0, budget_gen=1,
                    blocked=0, immortal=0)
    ep = store.record_episode("s-nr-stop", "escalation", 1, "2026-10-05", dedup_key="s-nr-stop:esc:1")
    res = _same(lambda: mod.resolve_escalation("s-nr-stop", ep, 1, "stop", OPERATOR))
    assert res["granted"] and store.get_state("s-nr-stop")["blocked"] == 1

    _breach("s-nr-rec")
    mod._on_session_finalize(session_id="s-nr-rec")
    assert isinstance(_same(mod.reconcile_once), int)


def test_ac_cost_f30_b_11(env):
    """AC-COST-F30.b-11: Approve is refused money-block-active while the card's session (or any other open session in the profile) is still blocked/unpriced/over-ceiling after a fresh refresh and not stopped by an applied Stop; the text names the open session and /cost continue or /cost ceiling <usd>, then Approve on this card; the card stays pending and disengage is not called; after an authorized /cost continue an authorized Approve on the SAME card resumes it through disengage()."""
    card = _new_stop(env, "s-money", blocked=True)
    res = _approve(env, card["card_id"])
    assert res["ok"] is False and res["reason"] == "money-block-active", res
    for needle in ("s-money", "/cost continue", "/cost ceiling", card["card_id"]):
        assert needle in res["text"], (needle, res["text"])
    assert _card(env, card["card_id"])["status"] == "pending" and env.disengage_calls == []
    assert [d for d in _decisions(env, card["card_id"]) if d["decision"] == "refused:money-block-active"]

    # refresh: clearing blocked by hand is NOT enough — the spend in state.db (fresh reconcile) keeps it refused.
    store = env.mod.store
    store.set_state("s-money", blocked=0)
    assert _approve(env, card["card_id"])["reason"] == "money-block-active", "spend over ceiling is re-read"
    store.set_state("s-money", blocked=1)

    # ANOTHER open blocked session in the same profile also keeps the card pending.
    _seed_state_db(env.home, "s-money-2", 6.2)
    store.set_state("s-money-2", effective_usd=6.2, window_start_total=0.0, day_start_total=0.0, budget_gen=1,
                    blocked=1, immortal=0)
    store.record_episode("s-money-2", "breach", 1, "2026-10-05", dedup_key="s-money-2:breach:1")

    # the human's next step, as a GATEWAY command in the stopped session's chat.
    _pgd(env, "/cost continue", route="rk-money", session_id="s-money")
    assert store.get_state("s-money")["blocked"] == 0, "the gateway /cost continue applied"
    res2 = _approve(env, card["card_id"])
    assert res2["reason"] == "money-block-active" and "s-money-2" in res2["text"], res2
    assert _card(env, card["card_id"])["status"] == "pending" and env.disengage_calls == []
    env.mod._on_session_finalize(session_id="s-money-2")
    res3 = _approve(env, card["card_id"])
    assert res3["ok"] is True and res3["reason"] == "resumed", res3
    assert len(env.disengage_calls) == 1 and not _own_sp(env).exists()
    assert _card(env, card["card_id"])["status"] == "approved"

    # a BLOCKED session whose stop was re-paused: the stop question wins — repaused, consumed.
    card4 = _new_stop(env, "s-money-repause", blocked=True)
    env.estop.engage(reason="operator re-pause while still blocked")
    res4 = _approve(env, card4["card_id"])
    assert res4["reason"] == "repaused" and _card(env, card4["card_id"])["status"] == "refused", res4
    assert len(env.disengage_calls) == 1


def test_ac_cost_f30_b_12(env):
    """AC-COST-F30.b-12: (mitigation 1) Approve re-reads every candidate sentinel immediately before its single disengage() call and refuses changed-during-approve unless that final read is still the carded stop (reason, receipt, engaged_at, byte equality); no DB / delivery / logging / other file I/O between that read and the call; one attempt — a disengage returning False or raising is never retried."""
    import logging

    resume, store = _resume(env), env.mod.store
    card = _new_stop(env, "s-order", blocked=False)
    trace = []
    state = {"after_final": False}
    real_read = resume._read_candidates

    def _rd(*a, **k):
        out = real_read(*a, **k)
        trace.append("read")
        return out

    def _io(name, fn):
        def f(*a, **k):
            trace.append(name)
            return fn(*a, **k)
        return f

    spy = env.estop.disengage

    def _dis(*a, **k):
        trace.append("disengage")
        return spy(*a, **k)

    mp = env.monkeypatch
    mp.setattr(resume, "_read_candidates", _rd)
    for obj, attr in ((pathlib.Path, "open"), (pathlib.Path, "read_bytes"), (pathlib.Path, "exists"),
                      (builtins, "open"), (io, "open"), (os, "open"), (sqlite3, "connect"),
                      (logging.Logger, "_log")):
        mp.setattr(obj, attr, _io(f"{getattr(obj, '__name__', obj)}.{attr}", getattr(obj, attr)))
    import tools.send_message_tool as smt

    mp.setattr(smt, "send_message_tool", _io("send_message_tool", smt.send_message_tool))
    mp.setattr(env.estop, "disengage", _dis)

    def _between_final_read_and_call():
        i = trace.index("disengage")
        j = max(k for k in range(i) if trace[k] == "read")
        return trace[j + 1:i]

    assert _approve(env, card["card_id"])["reason"] == "resumed"
    assert trace.count("disengage") == 1 and trace.count("read") >= 2, trace
    assert _between_final_read_and_call() == [], ("no I/O between the final read and disengage", trace)

    # negative control: an extra Path.exists() inside the compare, after the final read, is detected.
    _clear_all(env)
    card_n = _new_stop(env, "s-order-neg", blocked=False)
    real_compare = resume._compare
    trace.clear()

    def _compare_with_io(card_, snap):
        pathlib.Path(card_["sentinel_path"]).exists()
        return real_compare(card_, snap)

    mp.setattr(resume, "_compare", _compare_with_io)
    assert _approve(env, card_n["card_id"])["reason"] == "resumed"
    assert _between_final_read_and_call(), ("the trace must catch I/O inside the window", trace)
    mp.setattr(resume, "_compare", real_compare)

    # a change landing AFTER the first compare (between first and final read) is refused.
    _clear_all(env)
    card2 = _new_stop(env, "s-late", blocked=False)
    reads = {"n": 0}

    def _rd_mutating(*a, **k):
        reads["n"] += 1
        if reads["n"] == 2:
            env.estop.engage(reason="operator re-pause during approve")
        return real_read(*a, **k)

    env.monkeypatch.setattr(resume, "_read_candidates", _rd_mutating)
    trace.clear()
    res = _approve(env, card2["card_id"])
    assert res["reason"] == "changed-during-approve", res
    assert "disengage" not in trace and _own_sp(env).exists()

    # one attempt only: a disengage returning False is not retried.
    _clear_all(env)
    env.monkeypatch.setattr(resume, "_read_candidates", real_read)
    card3 = _new_stop(env, "s-false", blocked=False)
    n = {"c": 0}

    def _false(*a, **k):
        n["c"] += 1
        return False

    env.monkeypatch.setattr(env.estop, "disengage", _false)
    res3 = _approve(env, card3["card_id"])
    assert n["c"] == 1 and res3["ok"] is False, (n, res3)
    assert _approve(env, card3["card_id"])["reason"] == "already-resolved" and n["c"] == 1

    # one attempt only: a raising disengage is not retried either.
    _clear_all(env)
    card4 = _new_stop(env, "s-raise", blocked=False)
    m = {"c": 0}

    def _raise(*a, **k):
        m["c"] += 1
        raise OSError("unlink failed")

    env.monkeypatch.setattr(env.estop, "disengage", _raise)
    res4 = _approve(env, card4["card_id"])
    assert m["c"] == 1 and res4["ok"] is False and res4["reason"] == "resume-failed", (m, res4)
    assert _card(env, card4["card_id"])["status"] == "resume-failed"


def test_ac_cost_f30_b_13(env):
    """AC-COST-F30.b-13: (mitigation 2) every resolution appends audit rows to resume_decisions — who, when, decision, stop identity, and the full first-read content of every candidate path; the attempt row is committed BEFORE disengage so a crash right after the unlink still leaves it; rows cannot be updated or deleted; a card left approving is finalised interrupted by reconcile_once() with the attempt row kept."""
    resume = _resume(env)
    card = _new_stop(env, "s-audit", blocked=False)
    seen_at_call = {}
    spy = env.estop.disengage

    def _crash_after_unlink(*a, **k):
        seen_at_call["rows"] = [d["decision"] for d in _decisions(env, card["card_id"])]
        spy(*a, **k)
        raise KeyboardInterrupt("process died right after the unlink")

    env.monkeypatch.setattr(env.estop, "disengage", _crash_after_unlink)
    with pytest.raises(KeyboardInterrupt):
        _approve(env, card["card_id"])
    assert seen_at_call["rows"] == ["attempt"], seen_at_call
    rows = _decisions(env, card["card_id"])
    attempt = [d for d in rows if d["decision"] == "attempt"][0]
    assert attempt["actor"] == OPERATOR and attempt["ts"]
    assert attempt["stop"]["receipt"] == card["receipt"] and attempt["stop"]["reason"] == card["reason"]
    import base64

    own = [c for c in attempt["candidates"] if c["path"] == str(_own_sp(env))][0]
    assert own["present"] is True and base64.b64decode(own["content_b64"]) == card["published_body"]
    assert {c["path"] for c in attempt["candidates"]} == set(card["candidates"])
    absent = [c for c in attempt["candidates"] if c["path"] != str(_own_sp(env))]
    assert absent and all(c["present"] is False and c["content_b64"] is None for c in absent), absent
    assert _card(env, card["card_id"])["status"] == "approving"

    t0 = resume._clock()
    env.monkeypatch.setattr(resume, "_clock", lambda: t0 + 301)
    env.mod.reconcile_once()
    assert _card(env, card["card_id"])["status"] == "interrupted"
    decisions = [d["decision"] for d in _decisions(env, card["card_id"])]
    assert "attempt" in decisions and "interrupted" in decisions

    # the snapshot is EXACT and uncapped: a >64 KiB binary fleet-root pause is recorded byte-for-byte on refusal.
    env.real_disengage()
    card_big = _new_stop(env, "s-audit-big", blocked=False)
    blob = bytes(range(256)) * 300  # 76.8 KiB, not valid UTF-8
    _root_sp(env).write_bytes(blob)
    assert _approve(env, card_big["card_id"])["reason"] == "fleet-pause-present"
    refusal = [d for d in _decisions(env, card_big["card_id"]) if d["decision"] == "refused:fleet-pause-present"][0]
    root_entry = [c for c in refusal["candidates"] if c["path"] == str(_root_sp(env))][0]
    assert base64.b64decode(root_entry["content_b64"]) == blob
    _root_sp(env).unlink()

    db = env.home / "plugin-data" / PLUGIN_KEY / "data.db"
    conn = sqlite3.connect(db)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("UPDATE resume_decisions SET actor = 'x'")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("DELETE FROM resume_decisions")
    finally:
        conn.close()


def _notices(env):
    return [m for m in _messages(env) if NOTICE_TAIL in m]


def test_ac_cost_f30_b_14(env):
    """AC-COST-F30.b-14: (mitigation 3) after a resume that actually happened (disengage() returned True) the operator channel receives "session <X> resumed by approval of <who> at <time>; if you had just paused, pause again"; no such notice follows a refusal, a Deny, an expiry, a disengage that raised, or one that returned False."""
    resume = _resume(env)

    # negatives first: none of these may enqueue the post-resume notice.
    c_ref = _new_stop(env, "s-n-ref", blocked=False)
    env.estop.engage(reason="operator re-pause")
    _approve(env, c_ref["card_id"])
    _clear_all(env)
    c_deny = _new_stop(env, "s-n-deny", blocked=False)
    _deny(env, c_deny["card_id"])
    _clear_all(env)
    t0 = resume._clock()
    c_exp = _new_stop(env, "s-n-exp", blocked=False)
    env.monkeypatch.setattr(resume, "_clock", lambda: t0 + TTL + 10)
    env.mod.reconcile_once()
    env.monkeypatch.setattr(resume, "_clock", lambda: t0)
    _clear_all(env)
    spy = env.estop.disengage
    c_raise = _new_stop(env, "s-n-raise", blocked=False)
    env.monkeypatch.setattr(env.estop, "disengage", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    _approve(env, c_raise["card_id"])
    _clear_all(env)
    c_false = _new_stop(env, "s-n-false", blocked=False)
    env.monkeypatch.setattr(env.estop, "disengage", lambda *a, **k: False)
    _approve(env, c_false["card_id"])
    _drain(env)
    assert _notices(env) == [], _notices(env)

    # positive: a real resume enqueues exactly one notice, delivered by the drain to the target.
    _clear_all(env)
    env.monkeypatch.setattr(env.estop, "disengage", spy)
    c_ok = _new_stop(env, "s-n-ok", blocked=False)
    res = _approve(env, c_ok["card_id"])
    assert res["reason"] == "resumed" and not _own_sp(env).exists(), res
    notes = _notices(env)
    assert len(notes) == 1, notes
    note = notes[0]
    assert note.startswith("session s-n-ok resumed by approval of ") and OPERATOR in note, note
    approve_row = [d for d in _decisions(env, c_ok["card_id"]) if d["decision"] == "approve"][0]
    assert f" at {approve_row['ts']}; " in note, (note, approve_row["ts"])
    assert [c["args"]["target"] for c in env.sent if NOTICE_TAIL in c["args"].get("message", "")] == [TARGET]


def test_ac_cost_f30_b_15(env):
    """AC-COST-F30.b-15: (mitigation 4) a granted Approve reply states the residual window — a pause written in the same instant can be lifted too, and core closes it only with upstream UA-28."""
    card = _new_stop(env, "s-window", blocked=False)
    res = _approve(env, card["card_id"])
    assert res["ok"] is True and res["reason"] == "resumed", res
    text = res["text"]
    assert "UA-28" in text and "same instant" in text and "pause again" in text, text


def test_ac_cost_f30_b_16(env):
    """AC-COST-F30.b-16: each other refusal has its own reason code and human text and none calls disengage — consuming: already-resumed, unreadable-stop, reason-changed, stop-changed (extra #121054-style fields), superseded; not consuming: unknown-card, core-api-unavailable."""
    resume, estop = _resume(env), env.estop
    texts = resume.REFUSAL_TEXT
    codes = ["already-resumed", "unreadable-stop", "reason-changed", "stop-changed", "superseded",
             "unknown-card", "core-api-unavailable", "repaused", "fleet-pause-present", "money-block-active",
             "changed-during-approve", "expired"]
    assert all(texts.get(c) for c in codes), {c: texts.get(c) for c in codes}
    assert len({texts[c] for c in codes}) == len(codes), "each refusal has its own text"

    def _body():
        return json.loads(_own_sp(env).read_text(encoding="utf-8"))

    def _rewrite(obj):
        _own_sp(env).write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")

    def _check(card, code):
        res = _approve(env, card["card_id"])
        assert res["ok"] is False and res["reason"] == code and res["text"] == texts[code], (code, res)
        assert _card(env, card["card_id"])["status"] == "refused", code
        assert env.disengage_calls == [], code

    # Each case is approved right after it is arranged: a later owned publish would supersede it.
    c = _new_stop(env, "s-r-gone", blocked=False)
    env.real_disengage()
    _check(c, "already-resumed")
    c = _new_stop(env, "s-r-corrupt", blocked=False)
    _own_sp(env).write_bytes(b"\x00not json")
    _check(c, "unreadable-stop")
    _clear_all(env)
    c = _new_stop(env, "s-r-reason", blocked=False)
    b = _body()
    b["reason"] = b["reason"] + " (edited)"
    _rewrite(b)
    _check(c, "reason-changed")
    _clear_all(env)
    c = _new_stop(env, "s-r-extra", blocked=False)
    b = _body()
    b["expires_at"] = "2099-01-01T00:00:00+00:00"
    b["allow"] = {"user_ids": ["someone"]}
    _rewrite(b)
    _check(c, "stop-changed")

    # superseded: an out-of-band resume, then a NEW owned stop -> the older card names the newer one.
    _clear_all(env)
    old = _new_stop(env, "s-r-old", blocked=False)
    env.real_disengage()
    new = _new_stop(env, "s-r-new", blocked=False)
    res = _approve(env, old["card_id"])
    assert res["reason"] == "superseded" and new["card_id"] in res["text"], res
    assert _card(env, old["card_id"])["superseded_by"] == new["card_id"]

    # not consuming: unknown card; core API unavailable keeps the card pending.
    assert _approve(env, f"{PROFILE}:000000000000")["reason"] == "unknown-card"
    assert _approve(env, "no-such-profile:abc")["reason"] == "unknown-card"
    env.monkeypatch.delattr(estop, "_candidate_sentinel_paths")
    res = _approve(env, new["card_id"])
    assert res["reason"] == "core-api-unavailable" and _card(env, new["card_id"])["status"] == "pending", res
    assert env.disengage_calls == []


def test_ac_cost_f30_b_17(env):
    """AC-COST-F30.b-17: (derived) the card id pins its owning profile — an approver served by ANOTHER profile with no active chat session approves <profile>:<id>; authz uses the owning profile's operators; compare and disengage act on the owning profile's candidates only, leaving the approver profile's own sentinel untouched; an operator of only the approver profile is refused unauthorized."""
    card = _new_stop(env, "s-xprof", blocked=False)
    approver_sp = env.approver_home / "ESTOP"
    approver_sp.write_text(json.dumps({"reason": "approver profile's own pause"}) + "\n", encoding="utf-8")
    approver_bytes = approver_sp.read_bytes()

    with _pinned(APPROVER_PROFILE):
        res = _approve(env, card["card_id"], APPROVER_ONLY_OP)
        assert res["reason"] == "unauthorized", res
        _, replies = _pgd(env, f"/cost resume approve {card['card_id']}", profile=APPROVER_PROFILE,
                          route="rk-no-session", session_id=None)
    assert len(env.disengage_calls) == 1, env.disengage_calls
    assert not _own_sp(env).exists(), "the owning profile's stop is lifted"
    assert approver_sp.read_bytes() == approver_bytes, "the approver profile's own sentinel is untouched"
    assert _card(env, card["card_id"])["status"] == "approved"


def test_ac_cost_f30_b_18(env):
    """AC-COST-F30.b-18: (derived) a stop is approvable once every open session in its profile is money-resolved by an applied Stop (Stop-origin card, or a breach card followed by /cost stop) or closed; Approve then lifts the belt through disengage(), the stopped session stays blocked=1, and both the reply and the resume notice say that session itself stays stopped."""
    mod, store = env.mod, env.mod.store
    store.set_state("s-stopped", effective_usd=0.5, window_start_total=0.0, day_start_total=0.0, budget_gen=1,
                    blocked=0, immortal=0)
    ep = store.record_episode("s-stopped", "escalation", 1, "2026-10-05", dedup_key="s-stopped:esc:1")
    assert mod.resolve_escalation("s-stopped", ep, 1, "stop", OPERATOR)["granted"] is True
    card = [c for c in _cards(env) if c["session_id"] == "s-stopped" and c["origin"] == "stop"][0]
    res = _approve(env, card["card_id"])
    assert res["ok"] is True and res["reason"] == "resumed", res
    assert len(env.disengage_calls) == 1 and not _own_sp(env).exists()
    assert store.get_state("s-stopped")["blocked"] == 1, "the stopped session itself stays stopped"
    assert "s-stopped" in res["text"] and "stays stopped" in res["text"], res["text"]
    notes = _notices(env)
    assert len(notes) == 1 and "stays stopped" in notes[0], notes

    # a BREACH card whose session is then stopped through the gateway `/cost stop` becomes approvable too.
    card_b = _new_stop(env, "s-breach-stop", blocked=True)
    assert _approve(env, card_b["card_id"])["reason"] == "money-block-active"
    _pgd(env, "/cost stop", route="rk-bstop", session_id="s-breach-stop")
    assert store.get_state("s-breach-stop")["blocked"] == 1
    res_b = _approve(env, card_b["card_id"])
    assert res_b["ok"] is True and res_b["reason"] == "resumed" and "stays stopped" in res_b["text"], res_b
    assert len(env.disengage_calls) == 2 and not _own_sp(env).exists()
