"""Acceptance test for the nv-cost-cap plugin (requirement COST-F29).

Behavior-contract acceptance test. It mirrors the shape of
``tests/hermes_cli/test_plugin_api_compat.py``: it loads the plugin from an
isolated ``HERMES_HOME`` with an EMPTY bundled dir through the real
``PluginManager().discover_and_load()`` path, asserts registration, then drives
each acceptance criterion and asserts the observable outcome.

One ``test_ac_cost_f29_<n>`` per ``pytest:`` acceptance-criterion row in the ADR
(``reports/cost-f29.md`` §Acceptance criteria), contiguous 1..9, docstring line
1 summarizes the criterion. ``test_ac_cost_f29_5`` is parametrized over
``api_mode`` in {anthropic_messages, chat_completions, codex_responses} and
``test_ac_cost_f29_9`` over ``unknown_on`` in {usage, session} — each remains one
criterion id with multiple parametrizations.

Plugin public-helper contract the builder implements (fast-path accrual and
episodes live in ``plugin_db("nv-cost-cap")`` under the active ``HERMES_HOME`` —
NOT in the core ``state.db``, which the plugin only reads):
  * the ``post_api_request`` callback RETURNS the session's running fast-path total.
  * ``record_api_request(session_id, model, usage, api_request_id, *, provider=None, base_url=None)`` -> float
  * ``reconcile_session(session_id, *, state_db_path)`` -> float  recursive lineage read.
  * ``session_spend(session_id)`` -> float  effective spend persisted by the reconcile hooks
      (monotonic: max(previous, reconciled) — a stale async reconcile never lowers it).
  * ``tier1_cap(profile_home)`` -> float  rolling-p90 over completed session TOTALS
      (authoritative per-session read; active session + out-of-window outliers excluded).
  * ``resolve_ceiling()`` -> float  Tier-2 precedence resolution for the active profile.
  * ``set_fleet_ceiling(value)``  owner-pinned fleet-default write (refused when managed-pinned).
  * ``episodes(session_id)`` -> list[dict] carrying episodeId / budgetGen / day.
The effective ceiling in these hermetic tests is set via ``settings.profile_ceiling_usd``
(the per-profile override, which IS in the resolution precedence).

Must FAIL on the stock v2026.8.31 tree: ``plugins/nv-cost-cap`` is absent, so
``shutil.copytree`` raises ``FileNotFoundError`` before load; PASS once the
plugin exists.
"""

from __future__ import annotations

import shutil
import sqlite3
import types
from pathlib import Path

import pytest
import yaml

from hermes_cli.plugins import PluginManager

PLUGIN_KEY = "nv-cost-cap"
PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / PLUGIN_KEY
MODEL = "claude-opus-4-8"
PROVIDER = "anthropic"


def _load(tmp_path, monkeypatch, *, settings=None, profile_name=None,
          home_subpath="hermes-home", managed_dir=None):
    """Discover+load nv-cost-cap from an isolated HERMES_HOME; return handles.

    profile_name puts HERMES_HOME at <HOME>/.hermes/profiles/<name> so that
    get_active_profile_name() and get_profile_dir("default") resolve for the
    orchestrator-only and owner-pinned-fleet-default checks.
    """
    if profile_name is not None:
        hermes_home = tmp_path / ".hermes" / "profiles" / profile_name
        os_home = tmp_path
    else:
        hermes_home = tmp_path / home_subpath
        os_home = tmp_path / "os-home"

    plugins_dir = hermes_home / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    dest = plugins_dir / PLUGIN_KEY
    if not dest.exists():
        shutil.copytree(PLUGIN_DIR, dest)  # FileNotFoundError on the stock tree

    config = {"plugins": {"enabled": [PLUGIN_KEY]}}
    if settings is not None:
        config["plugins"]["entries"] = {PLUGIN_KEY: {"settings": settings}}
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump(config), encoding="utf-8"
    )

    empty_bundled = tmp_path / "bundled-plugins"
    empty_bundled.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(empty_bundled))
    if managed_dir is not None:
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_dir))
    else:
        monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)

    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled is True
    assert loaded.error is None
    assert loaded.module is not None
    return hermes_home, manager, loaded


def _seed_state_db(hermes_home, sessions, usage):
    """Seed the core state.db columns the plugin reads (READ-ONLY to it).

    Mirrors the release schema (hermes_state_common.py:411-431, :483-503):
    started_at/ended_at are REAL epoch seconds (a completed session has
    ended_at NOT NULL), and both tables carry a cost_status TEXT column
    (:430, :498). Core persists an unknown-priced call as estimated_cost_usd
    = float(estimated_cost_usd or 0.0) = 0.0 with cost_status='unknown'
    (agent/codex_runtime.py:210-211,227-229; hermes_state.py:9596-9598) — the
    authoritative unpriced signal AC-9 exercises, invisible to a bare
    estimated_cost_usd sum. PIN-MOVE RULE: re-verify these columns on any pin move.
      sessions: (id, parent_session_id, estimated_cost_usd[, ended_at[, cost_status]])
                sessions.cost_status defaults to NULL here when unsupplied. Core
                stamps cost_status on BOTH tables at runtime — sessions via
                update_token_counts (hermes_state.py:9380/:9401), session_model_usage
                via the per-model upsert (:9580) — and portability import also
                copies sessions.cost_status; so an unknown row can land on either.
      usage:    (session_id, model, task, estimated_cost_usd[, cost_status])
                task='' = main-loop; usage.cost_status defaults to 'estimated'
                (a priced row) here and is 'unknown' for an unpriced row.
    """
    db = hermes_home / "state.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            "id TEXT PRIMARY KEY, parent_session_id TEXT, "
            "estimated_cost_usd REAL DEFAULT 0, cost_status TEXT, "
            "started_at REAL NOT NULL DEFAULT 0, ended_at REAL, end_reason TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS session_model_usage ("
            "session_id TEXT, model TEXT, task TEXT DEFAULT '', "
            "billing_provider TEXT DEFAULT '', billing_base_url TEXT DEFAULT '', "
            "billing_mode TEXT DEFAULT '', "
            "estimated_cost_usd REAL NOT NULL DEFAULT 0, cost_status TEXT, "
            "PRIMARY KEY (session_id, model, billing_provider, "
            "billing_base_url, billing_mode, task))"
        )
        srows = [tuple(s) + (None,) * (5 - len(s)) for s in sessions]
        conn.executemany(
            "INSERT OR REPLACE INTO sessions "
            "(id, parent_session_id, estimated_cost_usd, ended_at, cost_status) "
            "VALUES (?, ?, ?, ?, ?)",
            srows,
        )
        urows = [tuple(u) if len(u) == 5 else (tuple(u) + ("estimated",))
                 for u in usage]
        conn.executemany(
            "INSERT OR REPLACE INTO session_model_usage "
            "(session_id, model, task, estimated_cost_usd, cost_status) "
            "VALUES (?, ?, ?, ?, ?)",
            urows,
        )
        conn.commit()
    finally:
        conn.close()
    return db


def _usage(**buckets):
    """A post_api_request-shaped usage dict (run_agent.py:2890-2904 keys)."""
    base = {
        "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
        "cache_write_tokens": 0, "reasoning_tokens": 0, "request_count": 1,
    }
    base.update(buckets)
    base["prompt_tokens"] = base["input_tokens"]
    base["total_tokens"] = base["input_tokens"] + base["output_tokens"]
    return base


def _post_api_kwargs(session_id, api_request_id, usage, model=MODEL):
    return dict(
        session_id=session_id, api_request_id=api_request_id, usage=usage,
        model=model, provider=PROVIDER, base_url="",
        turn_id="turn-1", task_id="task-1", api_mode="anthropic_messages",
        future_additive_field={"nested": "value"},  # additive-field tolerance
    )


def _hook_num(results):
    for r in results:
        if isinstance(r, (int, float)) and not isinstance(r, bool):
            return float(r)
    raise AssertionError("expected a numeric fast-path total from the hook")


def _llm_execution_cb(manager):
    cbs = manager._middleware.get("llm_execution", [])
    assert cbs, "nv-cost-cap must register an llm_execution middleware"
    return cbs[-1]


def _fake_inbound(route_key, session_id, profile="orchestrator", text="hi"):
    # Real SessionSource carries no session_id; the id is resolved via the store.
    source = types.SimpleNamespace(
        profile=profile, chat_id="c1", thread_id="t1", user_id="u1",
        platform="cli",
    )
    event = types.SimpleNamespace(source=source, text=text)
    store = types.SimpleNamespace(
        peek_session_id=lambda key: session_id if key == route_key else None
    )
    gateway = types.SimpleNamespace(
        _session_key_for_source=lambda s: route_key, session_store=store,
    )
    return event, gateway, store


def _pgd(manager, route_key, session_id, *, text="hi", today=None):
    event, gateway, store = _fake_inbound(route_key, session_id, text=text)
    kw = dict(event=event, gateway=gateway, session_store=store)
    if today is not None:
        kw["_today"] = today
    return manager.invoke_hook("pre_gateway_dispatch", **kw)


def _pre_tool(manager, session_id, *, today=None):
    kw = dict(tool_name="terminal", args={}, session_id=session_id, task_id="t",
              tool_call_id="tc", turn_id="tn", api_request_id="a")
    if today is not None:
        kw["_today"] = today
    return manager.invoke_hook("pre_tool_call", **kw)


def _end(manager, session_id, **extra):
    return manager.invoke_hook(
        "on_session_end", session_id=session_id, completed=True, failed=False,
        interrupted=False, model="opus", platform="cli", **extra,
    )


def _has_action(results, action):
    return any(isinstance(r, dict) and r.get("action") == action for r in results)


def _mw_over(cb, session_id, api_mode="anthropic_messages"):
    """Invoke the llm_execution middleware with a next_call that raises."""
    def next_call(payload=None):
        raise AssertionError("next_call must not run when refused")
    return cb(request={"model": "opus"}, next_call=next_call,
              session_id=session_id, model="opus", api_mode=api_mode,
              provider="anthropic", base_url="", api_request_id="x", turn_id="t")


def _mw_calls_next(cb, session_id, api_mode="anthropic_messages"):
    """Invoke the middleware asserting it calls next_call exactly once."""
    sentinel = object()
    n = {"c": 0}
    def next_call(payload=None):
        n["c"] += 1
        return sentinel
    out = cb(request={"model": "opus"}, next_call=next_call,
             session_id=session_id, model="opus", api_mode=api_mode,
             provider="anthropic", base_url="", api_request_id="y", turn_id="t")
    return out, n["c"], sentinel


# ---------------------------------------------------------------------------
# AC-COST-F29-1
# ---------------------------------------------------------------------------
def test_ac_cost_f29_1(tmp_path, monkeypatch):
    """post_api_request fast-path accrual is priced via Hermes pricing, idempotent by api_request_id, durable, and fail-closed on unknown pricing."""
    from agent.usage_pricing import CanonicalUsage, estimate_usage_cost

    _, manager, loaded = _load(
        tmp_path, monkeypatch, settings={"profile_ceiling_usd": 100.0}
    )
    for hook in ("post_api_request", "post_tool_call", "on_session_end",
                 "pre_tool_call", "on_session_start", "pre_gateway_dispatch"):
        assert hook in loaded.hooks_registered
    assert "llm_execution" in getattr(loaded, "middleware_registered", [])

    canon = CanonicalUsage(input_tokens=1000, output_tokens=500)
    priced = estimate_usage_cost(MODEL, canon, provider=PROVIDER, base_url="")
    exp = float(priced.amount_usd) if priced.amount_usd is not None else 0.0

    sid = "s-fast"
    u = _usage(input_tokens=1000, output_tokens=500)
    t1 = _hook_num(manager.invoke_hook("post_api_request",
                                       **_post_api_kwargs(sid, "req-A", u)))
    t2 = _hook_num(manager.invoke_hook("post_api_request",
                                       **_post_api_kwargs(sid, "req-A", u)))
    assert t1 == pytest.approx(exp)     # priced by Hermes' own estimate_usage_cost
    assert t2 == pytest.approx(t1)
    t3 = _hook_num(manager.invoke_hook("post_api_request",
                                       **_post_api_kwargs(sid, "req-B", u)))
    if exp > 0:
        assert t3 == pytest.approx(2 * exp)
    else:
        assert t3 == pytest.approx(t2)

    # Durable idempotency: reload (fresh PluginManager, same HERMES_HOME) and
    # re-deliver req-B -> the persisted total must not grow.
    hermes_home, manager2, loaded2 = _load(
        tmp_path, monkeypatch, settings={"profile_ceiling_usd": 100.0}
    )
    t3b = _hook_num(manager2.invoke_hook("post_api_request",
                                         **_post_api_kwargs(sid, "req-B", u)))
    assert t3b == pytest.approx(t3)
    assert (hermes_home / "plugin-data" / PLUGIN_KEY / "data.db").exists()

    # Unknown pricing is fail-CLOSED: an unpriced model must not read as free —
    # the llm_execution middleware refuses that session rather than calling next.
    up = "definitely-not-a-real-model-zzz"
    assert estimate_usage_cost(up, canon, provider=PROVIDER, base_url="").amount_usd is None
    manager2.invoke_hook("post_api_request",
                         **_post_api_kwargs("s-unpriced", "req-U", u, model=up))
    _assert_terminal(_mw_over(_llm_execution_cb(manager2), "s-unpriced"),
                     "anthropic_messages")


# ---------------------------------------------------------------------------
# AC-COST-F29-2
# ---------------------------------------------------------------------------
def test_ac_cost_f29_2(tmp_path, monkeypatch):
    """Reconciled spend = recursive parent_session_id lineage sum: max(sessions, SUM main) + SUM aux; effective spend is monotonic under a stale reconcile."""
    hermes_home, manager, loaded = _load(
        tmp_path, monkeypatch, settings={"profile_ceiling_usd": 100.0}
    )
    db = _seed_state_db(
        hermes_home,
        sessions=[("parent", None, 2.50), ("child", "parent", 1.00)],
        usage=[
            ("parent", "opus", "", 2.00),        # main; sessions row is 2.50 (0.50 residual)
            ("parent", "haiku", "aux", 0.30),    # aux; never in the sessions row
            ("child", "opus", "", 1.00),
        ],
    )
    _end(manager, "parent")
    assert loaded.module.session_spend("parent") == pytest.approx(3.80)
    assert loaded.module.reconcile_session(
        "parent", state_db_path=str(db)) == pytest.approx(3.80)

    # Fast-path-vs-stale-reconcile race: a fast-path accrual followed by a
    # reconcile that reads a LOWER state.db total must not discard the newer
    # fast-path spend (effective = max), which would re-permit a paid call.
    u = _usage(input_tokens=1000, output_tokens=500)
    fp = _hook_num(manager.invoke_hook("post_api_request",
                                       **_post_api_kwargs("s-race", "rr", u)))
    _seed_state_db(hermes_home, sessions=[("s-race", None, 0.0)],
                   usage=[("s-race", "opus", "", 0.0)])
    _end(manager, "s-race")
    assert loaded.module.session_spend("s-race") == pytest.approx(fp)


# ---------------------------------------------------------------------------
# AC-COST-F29-3
# ---------------------------------------------------------------------------
def test_ac_cost_f29_3(tmp_path, monkeypatch):
    """Tier-1 = deterministic p90 over completed session TOTALS (authoritative read); over-cap fires record episodes with monotonic budgetGen and no hard-stop."""
    import agent.estop as estop

    hermes_home, manager, loaded = _load(
        tmp_path, monkeypatch,
        settings={"profile_ceiling_usd": 100.0, "p90_window_sessions": 10},
    )
    # h1..h9 cost their index; h10's total comes ONLY from an aux row (its
    # sessions aggregate is 0) so p90==9.0 holds only if tier1_cap uses the
    # authoritative per-session read (main+aux), not the bare sessions column.
    hist = [(f"h{i}", None, float(i), 1.7e9 + i) for i in range(1, 10)]
    hist.append(("h10", None, 0.0, 1.7e9 + 10))          # aggregate 0 ...
    outlier = ("h-old", None, 1000.0, 1.0e9)             # out-of-window, excluded
    active = ("s-live", None, 0.0, None)                 # in-flight, excluded
    _seed_state_db(
        hermes_home, sessions=hist + [outlier, active],
        usage=[(f"h{i}", "opus", "", float(i)) for i in range(1, 10)]
             + [("h10", "opus", "aux", 10.0)]            # ... total 10.0 via aux
             + [("h-old", "opus", "", 1000.0)],
    )
    assert loaded.module.tier1_cap(str(hermes_home)) == pytest.approx(9.0)

    _seed_state_db(hermes_home, sessions=[("s-live", None, 9.5, None)],
                   usage=[("s-live", "opus", "", 9.5)])
    assert not _has_action(_pgd(manager, "r1", "s-live"), "skip")
    _seed_state_db(hermes_home, sessions=[("s-live", None, 19.0, None)],
                   usage=[("s-live", "opus", "", 19.0)])
    assert not _has_action(_pgd(manager, "r1", "s-live"), "skip")

    eps = loaded.module.episodes("s-live")
    assert len(eps) == 2
    assert eps[0]["episodeId"] and eps[1]["episodeId"]
    assert eps[0]["episodeId"] != eps[1]["episodeId"]
    assert eps[1]["budgetGen"] > eps[0]["budgetGen"]
    assert not estop.is_engaged()
    assert not _has_action(_pre_tool(manager, "s-live"), "block")


# ---------------------------------------------------------------------------
# AC-COST-F29-4
# ---------------------------------------------------------------------------
def test_ac_cost_f29_4(tmp_path, monkeypatch):
    """Non-immortal cap is per fire; a crossing fire refuses that session in-band, engages profile ESTOP when estop_on_breach (default) but not when false, and never skips recovery commands."""
    import agent.estop as estop

    # --- Part A: default estop_on_breach -> gateway per-fire + ESTOP belt ---
    hermes_home, manager, loaded = _load(
        tmp_path / "A", monkeypatch, settings={"profile_ceiling_usd": 1.0},
    )
    assert not estop.is_engaged()
    _seed_state_db(hermes_home, sessions=[("g", None, 0.6, None)],
                   usage=[("g", "opus", "", 0.6)])       # fire delta 0.6 < 1.0
    assert not _has_action(_pgd(manager, "rk", "g"), "skip")
    assert not estop.is_engaged()
    _seed_state_db(hermes_home, sessions=[("g", None, 1.2, None)],
                   usage=[("g", "opus", "", 1.2)])       # next fire delta 0.6 < 1.0
    assert not _has_action(_pgd(manager, "rk", "g"), "skip")
    _seed_state_db(hermes_home, sessions=[("g", None, 6.2, None)],
                   usage=[("g", "opus", "", 6.2)])       # fire delta 5.0 >= 1.0
    assert _has_action(_pgd(manager, "rk", "g"), "skip")
    assert estop.is_engaged()                            # row's cron/kanban belt
    assert _has_action(_pre_tool(manager, "g"), "block")
    # Recovery commands are exempt even while over cap / paused.
    assert not _has_action(_pgd(manager, "rk", "g", text="/new"), "skip")
    estop.disengage()

    # --- Part B: estop_on_breach false -> precise per-session, sibling allowed ---
    hermes_home_b, manager_b, _ = _load(
        tmp_path / "B", monkeypatch,
        settings={"profile_ceiling_usd": 1.0, "estop_on_breach": False},
    )
    _seed_state_db(hermes_home_b,
                   sessions=[("b1", None, 5.0, None), ("b2", None, 0.1, None)],
                   usage=[("b1", "opus", "", 5.0), ("b2", "opus", "", 0.1)])
    assert _has_action(_pgd(manager_b, "rk1", "b1"), "skip")
    assert not estop.is_engaged()
    assert not _has_action(_pgd(manager_b, "rk2", "b2"), "skip")

    # --- Part C: non-gateway boundary advances the per-fire baseline too ---
    hermes_home_c, manager_c, _ = _load(
        tmp_path / "C", monkeypatch, settings={"profile_ceiling_usd": 1.0},
    )
    manager_c.invoke_hook("on_session_start", session_id="n", resumed=False,
                          model="opus", platform="cli")
    _seed_state_db(hermes_home_c, sessions=[("n", None, 0.6, None)],
                   usage=[("n", "opus", "", 0.6)])
    _end(manager_c, "n")
    assert not estop.is_engaged()
    _seed_state_db(hermes_home_c, sessions=[("n", None, 6.2, None)],
                   usage=[("n", "opus", "", 6.2)])
    _end(manager_c, "n")
    assert estop.is_engaged()
    estop.disengage()


# ---------------------------------------------------------------------------
# AC-COST-F29-5
# ---------------------------------------------------------------------------
def _assert_terminal(resp, api_mode):
    assert resp is not None
    if api_mode == "anthropic_messages":
        assert getattr(resp, "stop_reason", None)
        content = getattr(resp, "content", None)
        assert isinstance(content, list) and content
        block = content[0]
        assert getattr(block, "type", None) == "text"
        assert isinstance(getattr(block, "text", None), str) and block.text
    elif api_mode == "codex_responses":
        # codex_responses reaches the middleware, so the synthetic must be a
        # clean assistant terminal the REAL Responses validator accepts. The
        # combined checks (validate_response + completed status + an assistant
        # message with output_text) reject a chat shape, a status='failed', or a
        # non-message/in-progress item that would not be a clean stop downstream.
        from agent.transports.codex import ResponsesApiTransport
        assert getattr(resp, "status", None) == "completed"
        assert ResponsesApiTransport().validate_response(resp) is True
        output = getattr(resp, "output", None)
        assert isinstance(output, list) and output
        item = output[0]
        assert getattr(item, "type", None) == "message"
        assert getattr(item, "role", None) == "assistant"
        assert getattr(item, "status", None) == "completed"
        texts = [getattr(c, "text", None)
                 for c in (getattr(item, "content", None) or [])
                 if getattr(c, "type", None) == "output_text"]
        assert any(isinstance(t, str) and t for t in texts)
        assert isinstance(getattr(resp, "output_text", None), str) and resp.output_text
    else:
        choices = getattr(resp, "choices", None)
        assert choices and getattr(choices[0], "message", None) is not None
        assert getattr(choices[0].message, "content", None)


@pytest.mark.parametrize(
    "api_mode", ["anthropic_messages", "chat_completions", "codex_responses"]
)
def test_ac_cost_f29_5(tmp_path, monkeypatch, api_mode):
    """llm_execution over Tier-2 returns a transport-valid synthetic response valid for the active api_mode (incl. codex_responses, accepted by ResponsesApiTransport.validate_response) without calling next_call and without raising; under Tier-2 it calls next_call once."""
    hermes_home, manager, loaded = _load(
        tmp_path, monkeypatch, settings={"profile_ceiling_usd": 1.0},
    )
    cb = _llm_execution_cb(manager)

    _seed_state_db(hermes_home, sessions=[("s-over", None, 5.0, None)],
                   usage=[("s-over", "opus", "", 5.0)])
    _assert_terminal(_mw_over(cb, "s-over", api_mode), api_mode)

    _seed_state_db(hermes_home, sessions=[("s-ok", None, 0.10, None)],
                   usage=[("s-ok", "opus", "", 0.10)])
    out, calls, sentinel = _mw_calls_next(cb, "s-ok", api_mode)
    assert calls == 1
    assert out is sentinel


# ---------------------------------------------------------------------------
# AC-COST-F29-6
# ---------------------------------------------------------------------------
def test_ac_cost_f29_6(tmp_path, monkeypatch):
    """Immortal sessions cap per calendar day on the window delta, are never hard-stopped (incl. mid-fire), record once per day, and roll the window over on a new day."""
    import agent.estop as estop

    hermes_home, manager, loaded = _load(
        tmp_path, monkeypatch,
        settings={"profile_ceiling_usd": 1.0,
                  "immortal_session_ids": ["s-immortal"]},
    )
    _seed_state_db(hermes_home, sessions=[("s-immortal", None, 5.0, None)],
                   usage=[("s-immortal", "opus", "", 5.0)])

    def day_eps(day):
        return [e for e in loaded.module.episodes("s-immortal")
                if isinstance(e, dict) and e.get("day") == day]

    _end(manager, "s-immortal", _today="2026-09-14")
    _end(manager, "s-immortal", _today="2026-09-14")
    assert len(day_eps("2026-09-14")) == 1
    assert not estop.is_engaged()
    assert not _has_action(_pgd(manager, "rk", "s-immortal", today="2026-09-14"), "skip")
    assert not _has_action(_pre_tool(manager, "s-immortal", today="2026-09-14"), "block")
    # Over the ceiling, the middleware still runs the provider call (never refused).
    out, calls, sentinel = _mw_calls_next(_llm_execution_cb(manager), "s-immortal")
    assert calls == 1 and out is sentinel

    # New day, lifetime total UNCHANGED -> window delta 0 -> no episode.
    _end(manager, "s-immortal", _today="2026-09-15")
    assert day_eps("2026-09-15") == []

    # New same-day spend crosses the day-2 window -> a fresh episode, still no stop.
    _seed_state_db(hermes_home, sessions=[("s-immortal", None, 12.0, None)],
                   usage=[("s-immortal", "opus", "", 12.0)])
    _end(manager, "s-immortal", _today="2026-09-15")
    assert len(day_eps("2026-09-15")) == 1
    assert not estop.is_engaged()


# ---------------------------------------------------------------------------
# AC-COST-F29-7
# ---------------------------------------------------------------------------
def test_ac_cost_f29_7(tmp_path, monkeypatch):
    """Ceiling resolves override > managed-scope fleet pin > owner-pinned fleet default; a HERMES_* env var does not change it."""
    # (a) per-profile override wins.
    _, _, loaded = _load(tmp_path / "a", monkeypatch,
                         settings={"profile_ceiling_usd": 12.0})
    assert loaded.module.resolve_ceiling() == pytest.approx(12.0)

    # (b) with an owner-pinned default set, a managed-scope pin wins over it.
    _, _, orch_b = _load(tmp_path / "b", monkeypatch, settings={},
                         profile_name="orchestrator")
    orch_b.module.set_fleet_ceiling(50.0)
    managed = tmp_path / "b" / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"entries": {PLUGIN_KEY: {
            "settings": {"fleet_ceiling_usd": 7.0}}}}}),
        encoding="utf-8",
    )
    _, _, orch_b2 = _load(tmp_path / "b", monkeypatch, settings={},
                          profile_name="orchestrator", managed_dir=managed)
    assert orch_b2.module.resolve_ceiling() == pytest.approx(7.0)

    # (c) owner-pinned fleet default set by the orchestrator is observed by a
    # worker profile (propagation); a HERMES_* env var does not change it.
    _, _, orch = _load(tmp_path / "c", monkeypatch, settings={},
                       profile_name="orchestrator")
    orch.module.set_fleet_ceiling(5.0)
    _, _, worker = _load(tmp_path / "c", monkeypatch, settings={},
                         profile_name="worker")
    assert worker.module.resolve_ceiling() == pytest.approx(5.0)
    monkeypatch.setenv("HERMES_COST_CAP_CEILING_USD", "999")
    assert worker.module.resolve_ceiling() == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# AC-COST-F29-8
# ---------------------------------------------------------------------------
def test_ac_cost_f29_8(tmp_path, monkeypatch):
    """`hermes cost-cap set` applies fleet + per-profile writes only for the orchestrator, refuses a managed pin without a stray write, and refuses non-orchestrator profiles."""
    import argparse

    _load(tmp_path, monkeypatch, settings={"orchestrator_profile": "orchestrator"},
          profile_name="worker")   # worker home exists for the --profile write target

    _, manager, orch = _load(
        tmp_path, monkeypatch,
        settings={"orchestrator_profile": "orchestrator"},
        profile_name="orchestrator")
    handler = manager._cli_commands["cost-cap"]["handler_fn"]

    handler(argparse.Namespace(cost_cap_command="set", fleet_ceiling_usd=42.0,
                               profile=None, profile_ceiling_usd=None))
    assert orch.module.resolve_ceiling() == pytest.approx(42.0)

    handler(argparse.Namespace(cost_cap_command="set", fleet_ceiling_usd=None,
                               profile="worker", profile_ceiling_usd=8.0))
    worker_cfg = yaml.safe_load(
        (tmp_path / ".hermes" / "profiles" / "worker" / "config.yaml")
        .read_text(encoding="utf-8"))
    assert (worker_cfg["plugins"]["entries"][PLUGIN_KEY]["settings"]
            ["profile_ceiling_usd"]) == pytest.approx(8.0)

    # Managed pin: a fleet-ceiling write is refused with an explicit message and
    # makes NO stray write to the owner-pinned default (revealed after removal).
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"entries": {PLUGIN_KEY: {
            "settings": {"fleet_ceiling_usd": 3.0}}}}}),
        encoding="utf-8",
    )
    _, manager_m, orch_m = _load(
        tmp_path, monkeypatch,
        settings={"orchestrator_profile": "orchestrator"},
        profile_name="orchestrator", managed_dir=managed)
    handler_m = manager_m._cli_commands["cost-cap"]["handler_fn"]
    res_m = handler_m(argparse.Namespace(cost_cap_command="set",
                                         fleet_ceiling_usd=50.0, profile=None,
                                         profile_ceiling_usd=None))
    assert res_m is not None and "managed" in str(res_m).lower()
    assert orch_m.module.resolve_ceiling() == pytest.approx(3.0)
    # Remove the managed pin -> the owner-pinned default must still be 42.0.
    _, _, orch_reveal = _load(
        tmp_path, monkeypatch,
        settings={"orchestrator_profile": "orchestrator"},
        profile_name="orchestrator")
    assert orch_reveal.module.resolve_ceiling() == pytest.approx(42.0)

    # Worker profile: any set is refused with an explicit result, no write.
    _, manager_w, worker = _load(
        tmp_path, monkeypatch,
        settings={"orchestrator_profile": "orchestrator"},
        profile_name="worker")
    handler_w = manager_w._cli_commands["cost-cap"]["handler_fn"]
    res_w = handler_w(argparse.Namespace(cost_cap_command="set",
                                         fleet_ceiling_usd=999.0, profile=None,
                                         profile_ceiling_usd=None))
    assert res_w is not None and "orchestrator" in str(res_w).lower()
    assert worker.module.resolve_ceiling() == pytest.approx(42.0)


# ---------------------------------------------------------------------------
# AC-COST-F29-9
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("unknown_on", ["usage", "session"])
def test_ac_cost_f29_9(tmp_path, monkeypatch, unknown_on):
    """A reconciled lineage row with cost_status='unknown' (estimated_cost_usd=0.0) is fail-CLOSED on the authoritative path: a non-immortal session is a Tier-2 breach, while a same-0.0-total priced control is not refused."""
    import agent.estop as estop

    # Ceiling 100 >> the 0.0 an unknown row sums to numerically, so a refusal
    # here can ONLY come from unknown-pricing detection on the reconcile path,
    # not from a numeric cap crossing.
    hermes_home, manager, loaded = _load(
        tmp_path, monkeypatch, settings={"profile_ceiling_usd": 100.0},
    )
    # The unknown marker sits on a DESCENDANT (parent_session_id=s-unknown), so a
    # refusal proves the reconcile scans cost_status across the whole lineage,
    # not just the requested session — on EITHER column core stamps it on:
    # session_model_usage.cost_status ("usage") or sessions.cost_status
    # ("session"). The priced control mirrors depth+column so the discriminator
    # is the status, not the placement.
    if unknown_on == "usage":
        sessions = [("s-unknown", None, 0.0, None),
                    ("s-unknown-child", "s-unknown", 0.0, None),
                    ("s-zero", None, 0.0, None),
                    ("s-zero-child", "s-zero", 0.0, None)]
        usage = [("s-unknown-child", "opus", "", 0.0, "unknown"),
                 ("s-zero-child", "opus", "", 0.0, "estimated")]
    else:
        sessions = [("s-unknown", None, 0.0, None, None),
                    ("s-unknown-child", "s-unknown", 0.0, None, "unknown"),
                    ("s-zero", None, 0.0, None, None),
                    ("s-zero-child", "s-zero", 0.0, None, "estimated")]
        usage = []
    _seed_state_db(hermes_home, sessions=sessions, usage=usage)

    assert not _has_action(_pgd(manager, "rk-zero", "s-zero"), "skip")
    assert not estop.is_engaged()

    assert _has_action(_pgd(manager, "rk-unk", "s-unknown"), "skip")
    assert estop.is_engaged()
    assert _has_action(_pre_tool(manager, "s-unknown"), "block")
    _assert_terminal(_mw_over(_llm_execution_cb(manager), "s-unknown"),
                     "anthropic_messages")
    estop.disengage()
