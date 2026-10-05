"""Unit tests for nv-cost-cap internals not isolated by a single acceptance row.

These exercise behaviours the acceptance criteria imply but do not isolate: the
fire-boundary double-count guard (a pre_gateway_dispatch + on_session_end pair on
one gateway turn must not open a second generation at unchanged spend); the
unpriced-boundary path (an unknown-priced call reaching a boundary is a
fail-closed breach, idempotent per unpriced event); the additive cap_state
migration of the recon_unpriced column on a pre-existing data.db; the
column-presence-defensive lineage_unknown on a state.db that predates the
cost_status column; and the authoritative-path reconcile-only unknown breach and
its idempotence. Loaded through the real PluginManager discovery path from an
isolated HERMES_HOME, like the acceptance test.
"""

from __future__ import annotations

import shutil
import sqlite3
import types
from pathlib import Path

import yaml

from hermes_cli.plugins import PluginManager

PLUGIN_KEY = "nv-cost-cap"
PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / PLUGIN_KEY


def _load(tmp_path, monkeypatch, *, settings):
    hermes_home = tmp_path / "hermes-home"
    plugins_dir = hermes_home / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    dest = plugins_dir / PLUGIN_KEY
    if not dest.exists():
        shutil.copytree(PLUGIN_DIR, dest)
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump(
            {"plugins": {"enabled": [PLUGIN_KEY], "entries": {PLUGIN_KEY: {"settings": settings}}}}
        ),
        encoding="utf-8",
    )
    empty_bundled = tmp_path / "bundled"
    empty_bundled.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(empty_bundled))
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled and loaded.error is None and loaded.module is not None
    return hermes_home, manager, loaded


def _seed(hermes_home, sessions, usage):
    conn = sqlite3.connect(hermes_home / "state.db")
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, "
            "parent_session_id TEXT, estimated_cost_usd REAL DEFAULT 0, "
            "started_at REAL NOT NULL DEFAULT 0, ended_at REAL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS session_model_usage (session_id TEXT, "
            "model TEXT, task TEXT DEFAULT '', estimated_cost_usd REAL NOT NULL DEFAULT 0, "
            "PRIMARY KEY (session_id, model, task))"
        )
        conn.executemany(
            "INSERT OR REPLACE INTO sessions (id, parent_session_id, estimated_cost_usd, ended_at) "
            "VALUES (?, ?, ?, ?)",
            sessions,
        )
        conn.executemany(
            "INSERT OR REPLACE INTO session_model_usage (session_id, model, task, estimated_cost_usd) "
            "VALUES (?, ?, ?, ?)",
            usage,
        )
        conn.commit()
    finally:
        conn.close()


def _seed_with_status(hermes_home, sessions, usage):
    """Seed state.db WITH the runtime cost_status column on both tables.

    sessions rows: (id, parent_session_id, estimated_cost_usd, ended_at, cost_status)
    usage rows:    (session_id, model, task, estimated_cost_usd, cost_status)
    """
    conn = sqlite3.connect(hermes_home / "state.db")
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, "
            "parent_session_id TEXT, estimated_cost_usd REAL DEFAULT 0, cost_status TEXT, "
            "started_at REAL NOT NULL DEFAULT 0, ended_at REAL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS session_model_usage (session_id TEXT, "
            "model TEXT, task TEXT DEFAULT '', estimated_cost_usd REAL NOT NULL DEFAULT 0, "
            "cost_status TEXT, PRIMARY KEY (session_id, model, task))"
        )
        conn.executemany(
            "INSERT OR REPLACE INTO sessions "
            "(id, parent_session_id, estimated_cost_usd, ended_at, cost_status) "
            "VALUES (?, ?, ?, ?, ?)",
            sessions,
        )
        conn.executemany(
            "INSERT OR REPLACE INTO session_model_usage "
            "(session_id, model, task, estimated_cost_usd, cost_status) VALUES (?, ?, ?, ?, ?)",
            usage,
        )
        conn.commit()
    finally:
        conn.close()


def _pgd(manager, route_key, session_id):
    source = types.SimpleNamespace(platform="cli")
    event = types.SimpleNamespace(source=source, text="hi")
    store = types.SimpleNamespace(
        peek_session_id=lambda key: session_id if key == route_key else None
    )
    gateway = types.SimpleNamespace(
        _session_key_for_source=lambda s: route_key, session_store=store
    )
    return manager.invoke_hook(
        "pre_gateway_dispatch", event=event, gateway=gateway, session_store=store
    )


def _has(results, action):
    return any(isinstance(r, dict) and r.get("action") == action for r in results)


def _pgd_platform(manager, route_key, session_id, platform):
    source = types.SimpleNamespace(platform=platform)
    event = types.SimpleNamespace(source=source, text="hi")
    store = types.SimpleNamespace(
        peek_session_id=lambda key: session_id if key == route_key else None
    )
    gateway = types.SimpleNamespace(
        _session_key_for_source=lambda s: route_key, session_store=store
    )
    return manager.invoke_hook(
        "pre_gateway_dispatch", event=event, gateway=gateway, session_store=store
    )


def _llm_cb(manager):
    cbs = manager._middleware.get("llm_execution", [])
    assert cbs
    return cbs[-1]


def test_boundary_no_double_count(tmp_path, monkeypatch):
    """A boundary at unchanged spend opens no new generation and records no duplicate episode."""
    hermes_home, manager, loaded = _load(
        tmp_path, monkeypatch,
        settings={"profile_ceiling_usd": 100.0, "p90_window_sessions": 2},
    )
    # One completed session (total 2.0) => Tier-1 cap = 2.0; an active session over it.
    _seed(
        hermes_home,
        sessions=[("h1", None, 2.0, 1.7e9), ("s", None, 5.0, None)],
        usage=[("h1", "opus", "", 2.0), ("s", "opus", "", 5.0)],
    )
    # First boundary: over Tier-1 (under Tier-2) -> exactly one escalation episode.
    manager.invoke_hook(
        "on_session_end", session_id="s", completed=True, failed=False,
        interrupted=False, model="opus", platform="cli",
    )
    eps = loaded.module.episodes("s")
    assert len(eps) == 1
    gen = eps[0]["budgetGen"]

    # Second boundary at UNCHANGED spend (the pre_gateway_dispatch that follows
    # on_session_end on one gateway turn): no new episode, no generation bump.
    assert not _has(_pgd(manager, "rk", "s"), "skip")
    eps2 = loaded.module.episodes("s")
    assert len(eps2) == 1
    assert eps2[0]["budgetGen"] == gen


def test_unpriced_boundary_is_failclosed_and_idempotent(tmp_path, monkeypatch):
    """An unknown-priced call reaching a boundary is a fail-closed breach, recorded once."""
    import agent.estop as estop

    hermes_home, manager, loaded = _load(
        tmp_path, monkeypatch, settings={"profile_ceiling_usd": 1.0},
    )
    usage = {"input_tokens": 10, "output_tokens": 5, "request_count": 1}
    # An unpriced model: recorded unpriced (never as free).
    loaded.module.record_api_request(
        "s-up", "definitely-not-a-real-model-zzz", usage, "req-1",
        provider="anthropic", base_url="",
    )
    assert not estop.is_engaged()

    manager.invoke_hook(
        "on_session_end", session_id="s-up", completed=True, failed=False,
        interrupted=False, model="x", platform="cli",
    )
    eps = loaded.module.episodes("s-up")
    assert len(eps) == 1
    assert eps[0]["kind"] == "unknown_pricing"
    assert estop.is_engaged()

    # Same unpriced event, repeated boundary: no duplicate episode.
    manager.invoke_hook(
        "on_session_end", session_id="s-up", completed=True, failed=False,
        interrupted=False, model="x", platform="cli",
    )
    assert len(loaded.module.episodes("s-up")) == 1
    estop.disengage()


def test_set_state_cannot_undo_atomic_effective_bump(tmp_path, monkeypatch):
    """A set_state that updates other columns (or a stale effective) never lowers effective spend."""
    _, _, loaded = _load(tmp_path, monkeypatch, settings={})
    store = loaded.module.store
    assert store.bump_effective("s", 10.0) == 10.0
    # Updating an unrelated column must not rewrite effective back down.
    store.set_state("s", blocked=1, budget_gen=3)
    assert store.get_state("s")["effective_usd"] == 10.0
    # Even an explicit stale/lower effective is clamped by MAX and cannot lower it.
    store.set_state("s", effective_usd=0.0)
    assert store.get_state("s")["effective_usd"] == 10.0


def test_immortal_platform_enum_never_stops(tmp_path, monkeypatch):
    """A platform-immortal session (Platform enum, not str) is never skipped, blocked, refused, or ESTOP'd."""
    import agent.estop as estop
    from gateway.config import Platform

    hermes_home, manager, loaded = _load(
        tmp_path, monkeypatch,
        settings={"profile_ceiling_usd": 1.0, "immortal_platforms": ["slack"]},
    )
    # Well over the ceiling — a non-immortal session here would be stopped.
    _seed(
        hermes_home,
        sessions=[("s-slack", None, 9.0, None)],
        usage=[("s-slack", "opus", "", 9.0)],
    )
    # pre_gateway_dispatch carries the Platform enum and must classify immortal
    # (persisting the flag for the platform-less callbacks below).
    assert not _has(_pgd_platform(manager, "rk", "s-slack", Platform.SLACK), "skip")
    # pre_tool_call / llm_execution receive no platform — they read the persisted flag.
    assert not _has(
        manager.invoke_hook(
            "pre_tool_call", tool_name="terminal", args={}, session_id="s-slack",
            task_id="t", tool_call_id="tc", turn_id="tn", api_request_id="a",
        ),
        "block",
    )
    calls = {"n": 0}

    def next_call(payload=None):
        calls["n"] += 1
        return "ok"

    out = _llm_cb(manager)(
        request={"model": "opus"}, next_call=next_call, session_id="s-slack",
        model="opus", api_mode="anthropic_messages", provider="anthropic",
        base_url="", api_request_id="x", turn_id="t",
    )
    assert calls["n"] == 1 and out == "ok"
    assert not estop.is_engaged()


def test_immortal_policy_revocation_takes_effect(tmp_path, monkeypatch):
    """Removing a platform from the immortal config makes an over-ceiling session enforce again."""
    from gateway.config import Platform

    hermes_home, manager, _ = _load(
        tmp_path, monkeypatch,
        settings={"profile_ceiling_usd": 1.0, "immortal_platforms": ["slack"]},
    )
    _seed(hermes_home, sessions=[("s-slack", None, 9.0, None)],
          usage=[("s-slack", "opus", "", 9.0)])
    # Classify immortal via the platform enum (persists the platform).
    assert not _has(_pgd_platform(manager, "rk", "s-slack", Platform.SLACK), "skip")

    # Reload at the SAME home with immortality REVOKED (empty immortal_platforms).
    _, manager2, _ = _load(tmp_path, monkeypatch, settings={"profile_ceiling_usd": 1.0})
    # A platform-less pre_tool_call now recomputes against live config -> block.
    assert _has(
        manager2.invoke_hook(
            "pre_tool_call", tool_name="terminal", args={}, session_id="s-slack",
            task_id="t", tool_call_id="tc", turn_id="tn", api_request_id="a",
        ),
        "block",
    )


def test_moa_unpriced_remains_fail_closed(tmp_path, monkeypatch):
    """A MoA (provider='moa') unpriceable turn stays fail-closed unpriced — never a free $0."""
    _, manager, loaded = _load(tmp_path, monkeypatch, settings={"profile_ceiling_usd": 1.0})
    usage = {"input_tokens": 10, "output_tokens": 5, "request_count": 1}
    total = loaded.module.record_api_request(
        "s-moa", "moa", usage, "req-moa", provider="moa", base_url="",
    )
    assert total == 0.0
    # Recorded unpriced (never a benign $0), so the middleware must refuse.
    assert loaded.module.store.unpriced_count("s-moa") == 1

    calls = {"n": 0}

    def next_call(payload=None):
        calls["n"] += 1
        return "ok"

    out = _llm_cb(manager)(
        request={"model": "moa"}, next_call=next_call, session_id="s-moa",
        model="moa", api_mode="anthropic_messages", provider="moa",
        base_url="", api_request_id="z", turn_id="t",
    )
    assert calls["n"] == 0
    assert getattr(out, "stop_reason", None) == "end_turn"


def test_new_day_first_fire_crosses(tmp_path, monkeypatch):
    """An immortal session that crosses the ceiling on the new day's first boundary records a day-two episode."""
    import agent.estop as estop

    hermes_home, manager, loaded = _load(
        tmp_path, monkeypatch,
        settings={"profile_ceiling_usd": 1.0, "immortal_session_ids": ["s-im"]},
    )

    def day_eps(day):
        return [e for e in loaded.module.episodes("s-im") if e.get("day") == day]

    # Day 1: 0.5 < 1.0 ceiling -> no crossing.
    _seed(hermes_home, sessions=[("s-im", None, 0.5, None)], usage=[("s-im", "opus", "", 0.5)])
    manager.invoke_hook(
        "on_session_end", session_id="s-im", completed=True, failed=False,
        interrupted=False, model="opus", platform="cli", _today="2026-09-14",
    )
    assert day_eps("2026-09-14") == []

    # Day 2, first boundary already carries new spend (0.5 -> 2.0). The new-day
    # baseline is the previous day's ending total (0.5), so delta 1.5 crosses.
    _seed(hermes_home, sessions=[("s-im", None, 2.0, None)], usage=[("s-im", "opus", "", 2.0)])
    manager.invoke_hook(
        "on_session_end", session_id="s-im", completed=True, failed=False,
        interrupted=False, model="opus", platform="cli", _today="2026-09-15",
    )
    assert len(day_eps("2026-09-15")) == 1
    assert not estop.is_engaged()


def test_nonfinite_ceiling_refused(tmp_path, monkeypatch):
    """A non-finite ceiling is refused and never persisted (would silently disable Tier-2)."""
    _, _, loaded = _load(tmp_path, monkeypatch, settings={})
    loaded.module.set_fleet_ceiling(10.0)
    assert loaded.module.resolve_ceiling() == 10.0
    for bad in (float("nan"), float("inf")):
        try:
            loaded.module.set_fleet_ceiling(bad)
            raise AssertionError("expected ValueError for a non-finite fleet ceiling")
        except ValueError:
            pass
    # The earlier valid owner-pinned value is unchanged (no stray write).
    assert loaded.module.resolve_ceiling() == 10.0


def test_recon_unpriced_column_additive_migration(tmp_path, monkeypatch):
    """An existing cap_state without recon_unpriced is migrated additively (default 0), not rebuilt."""
    from plugins.plugin_storage import plugin_data_dir

    _, _, loaded = _load(tmp_path, monkeypatch, settings={})
    db_path = plugin_data_dir(PLUGIN_KEY) / "data.db"
    conn = sqlite3.connect(db_path)
    try:
        # The cap_state schema as it was BEFORE recon_unpriced existed.
        conn.execute(
            "CREATE TABLE cap_state (session_id TEXT PRIMARY KEY, "
            "effective_usd REAL NOT NULL DEFAULT 0, window_start_total REAL NOT NULL DEFAULT 0, "
            "last_evaluated_total REAL NOT NULL DEFAULT 0, last_unpriced_count INTEGER NOT NULL DEFAULT 0, "
            "budget_gen INTEGER NOT NULL DEFAULT 0, blocked INTEGER NOT NULL DEFAULT 0, "
            "immortal INTEGER NOT NULL DEFAULT 0, platform TEXT, day_key TEXT, "
            "day_start_total REAL NOT NULL DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO cap_state (session_id, effective_usd, blocked) VALUES ('old', 4.0, 1)"
        )
        conn.commit()
    finally:
        conn.close()

    # First store access migrates in place (ALTER TABLE ADD COLUMN), no rebuild.
    state = loaded.module.store.get_state("old")
    assert state["recon_unpriced"] == 0
    assert state["effective_usd"] == 4.0
    assert state["blocked"] == 1


def test_lineage_unknown_legacy_schema_is_false_and_crash_safe(tmp_path, monkeypatch):
    """policy.lineage_unknown returns False (never raises) when state.db predates the cost_status column."""
    hermes_home, _, loaded = _load(tmp_path, monkeypatch, settings={})
    # _seed builds the pre-cost_status schema (no cost_status column on either table).
    _seed(hermes_home, sessions=[("s", None, 0.0, None), ("c", "s", 0.0, None)],
          usage=[("c", "opus", "", 0.0)])
    db = str(hermes_home / "state.db")
    assert loaded.module.policy.lineage_unknown("s", state_db_path=db) is False


def test_authoritative_unknown_boundary_failclosed_idempotent(tmp_path, monkeypatch):
    """A reconcile-only unknown row ($0, cost_status='unknown') on a descendant is a fail-closed breach, recorded once across repeated boundaries."""
    import agent.estop as estop

    hermes_home, manager, loaded = _load(
        tmp_path, monkeypatch, settings={"profile_ceiling_usd": 100.0},
    )
    # Ceiling 100 >> the $0 the unknown row sums to, so any breach is unknown-driven,
    # not a numeric crossing. The unknown marker sits on a DESCENDANT (parent 's'),
    # proving the reconcile scans cost_status across the whole lineage.
    _seed_with_status(
        hermes_home,
        sessions=[("s", None, 0.0, None, None), ("c", "s", 0.0, None, None)],
        usage=[("c", "opus", "", 0.0, "unknown")],
    )
    assert _has(_pgd(manager, "rk", "s"), "skip")
    eps = loaded.module.episodes("s")
    assert len(eps) == 1 and eps[0]["kind"] == "unknown_pricing"
    gen = eps[0]["budgetGen"]
    assert estop.is_engaged()

    # Repeated boundary at the same unknown signal: no duplicate episode, no gen bump.
    assert _has(_pgd(manager, "rk", "s"), "skip")
    eps2 = loaded.module.episodes("s")
    assert len(eps2) == 1 and eps2[0]["budgetGen"] == gen
    estop.disengage()
