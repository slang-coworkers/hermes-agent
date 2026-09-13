"""Acceptance tests for the GOV-F25 nv-artifact plugin (PR->session ownership
index, first-claim-wins) and carried OBS-F48 (fleet outcomes analytics).

One test_ac_<req_id>_<n> per pytest: acceptance-criterion row, in ADR order.
AC-GOV-F25-7 (live) and AC-OBS-F48-3 (ui) are scenario files under
tests/e2e-scenarios/GOV-F25/, not functions here.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from gateway.config import Platform
from hermes_cli.plugins import PluginContext, PluginManager
from tools.registry import registry, invalidate_check_fn_cache

PLUGIN_KEY = "nv-artifact"
PLUGIN_SRC = Path(__file__).parents[2] / "plugins" / PLUGIN_KEY

OWNER_PROFILE = "gov-f25-worker"
ORCH_PROFILE = "gov-f25-orch"
NOTIFY_ROUTE = "gh-pr"


def _set_profile(monkeypatch, name):
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: name)
    invalidate_check_fn_cache()


def _plugin_db(base: Path) -> Path:
    # ledger_profile='default' pins the single fleet DB to <root>, not the caller.
    return base / "plugin-data" / PLUGIN_KEY / "data.db"


def _ownership(base: Path, **where) -> list[tuple]:
    db = _plugin_db(base)
    if not db.exists():
        return []
    clause = " AND ".join(f"{k}=?" for k in where) or "1=1"
    conn = sqlite3.connect(db)
    try:
        return list(conn.execute(
            f"SELECT repo, pr, task_id, profile FROM ownership WHERE {clause}",
            tuple(where.values())))
    finally:
        conn.close()


def _task_id_for(base: Path, repo: str, pr: int):
    rows = _ownership(base, repo=repo, pr=pr)
    return rows[0][2] if rows else None


def _ownership_columns(base: Path) -> list[str]:
    conn = sqlite3.connect(_plugin_db(base))
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(ownership)")]
    finally:
        conn.close()


def _outcomes(base: Path, artifact: str) -> list[tuple]:
    db = _plugin_db(base)
    if not db.exists():
        return []
    conn = sqlite3.connect(db)
    try:
        return list(conn.execute(
            "SELECT artifact, terminal_outcome, cost_usd, profile FROM outcomes WHERE artifact=?",
            (artifact,)))
    finally:
        conn.close()


def _card_session_id(task_id: str):
    import hermes_cli.kanban_db as kb
    with kb.connect() as conn:
        row = conn.execute("SELECT session_id FROM tasks WHERE id=?", (task_id,)).fetchone()
    return row[0] if row else None


def _dispatch(name: str, args: dict, **kwargs) -> dict:
    return json.loads(registry.dispatch(name, args, **kwargs))


def _terminal_result(url: str) -> str:
    return f"Creating pull request for feature into main\n{url}\n"


def _webhook_event(delivery, *, route=NOTIFY_ROUTE, pr=7, repo="o/r",
                   platform=Platform.WEBHOOK, chat_type="webhook"):
    return SimpleNamespace(
        text="",
        source=SimpleNamespace(platform=platform, chat_type=chat_type,
                               chat_id=f"webhook:{route}:{delivery}"),
        raw_message={
            "action": "submitted",
            "repository": {"full_name": repo},
            "pull_request": {"number": pr, "head": {"sha": "beefcafe"}},
            "review": {"state": "commented", "commit_id": "beefcafe"},
        },
        message_id=delivery,
    )


def _bare_card(profile: str, idem: str) -> str:
    # A card with NO session_id, so a test can prove the claim path binds one.
    import hermes_cli.kanban_db as kb
    with kb.connect() as conn:
        task = kb.create_task(conn, title=idem, assignee=profile, idempotency_key=idem)
        return task if isinstance(task, str) else getattr(task, "id", task)


def _wake_card(home: Path, profile: str, idem: str, *, session_id: str, session_key: str) -> str:
    # The card (tasks.*) lives in kanban.db; the session row (sessions.session_key)
    # lives in the OWNER profile's state.db — distinct DBs. The fallback resolves
    # tasks.session_id -> <owner>/state.db.sessions.session_key, so seed each side
    # in its own store.
    import hermes_cli.kanban_db as kb
    with kb.connect() as conn:
        task = kb.create_task(conn, title=idem, assignee=profile, idempotency_key=idem)
        task_id = task if isinstance(task, str) else getattr(task, "id", task)
        conn.execute("UPDATE tasks SET session_id=? WHERE id=?", (session_id, task_id))
        kb.add_notify_sub(conn, task_id=task_id, platform="api_server",
                          chat_id=session_id, delivery_mode="wake", notifier_profile=profile)
    state = sqlite3.connect(home / "state.db")
    try:
        state.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, source TEXT, session_key TEXT)")
        state.execute("INSERT OR REPLACE INTO sessions (id, source, session_key) VALUES (?,?,?)",
                      (session_id, "api_server", session_key))
        state.commit()
    finally:
        state.close()
    return task_id


def _seed_session_usage(home: Path, session_id: str, rows: list[dict]) -> None:
    db = home / "state.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS session_model_usage ("
            "session_id TEXT NOT NULL, model TEXT NOT NULL, billing_provider TEXT DEFAULT '',"
            "billing_base_url TEXT DEFAULT '', billing_mode TEXT DEFAULT '', task TEXT DEFAULT '',"
            "estimated_cost_usd REAL DEFAULT 0, actual_cost_usd REAL DEFAULT 0, cost_status TEXT,"
            "PRIMARY KEY (session_id, model, billing_provider, billing_base_url, billing_mode, task))"
        )
        for r in rows:
            conn.execute(
                "INSERT OR REPLACE INTO session_model_usage"
                "(session_id, model, task, estimated_cost_usd, actual_cost_usd, cost_status)"
                " VALUES (?,?,?,?,?,?)",
                (session_id, r["model"], r.get("task", ""), r.get("estimated_cost_usd", 0.0),
                 r.get("actual_cost_usd", 0.0), r.get("cost_status")))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def artifact(tmp_path, monkeypatch):
    root = tmp_path / "root"
    home = root / "profiles" / OWNER_PROFILE
    plugins_dir = home / "plugins"
    plugins_dir.mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, plugins_dir / PLUGIN_KEY)

    (home / "config.yaml").write_text(
        yaml.safe_dump({
            "plugins": {
                "enabled": [PLUGIN_KEY],
                "entries": {
                    PLUGIN_KEY: {
                        # SIBLING of settings: core reads plugins.entries.<id>.allow_gateway_injection.
                        "allow_gateway_injection": True,
                        "settings": {"ledger_profile": "default",
                                     "orchestrator_profile": ORCH_PROFILE,
                                     "notify_route": NOTIFY_ROUTE},
                    }
                },
            }
        }),
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
    for hook in ("post_tool_call", "pre_gateway_dispatch", "on_session_end", "kanban_task_completed"):
        assert hook in loaded.hooks_registered
    assert {"report_pr_created", "resolve_pr_owner"} <= set(loaded.tools_registered)
    # register_cli_command verbs land in manager._cli_commands (kind "cli_command"),
    # NOT loaded.commands_registered (which is register_command slash commands only).
    cli_verbs = {name for name, e in manager._cli_commands.items() if e.get("plugin_key") == PLUGIN_KEY}
    assert {"pr", "outcomes"} <= cli_verbs

    return SimpleNamespace(manager=manager, loaded=loaded, root=root, home=home)


def test_ac_gov_f25_1(artifact):
    """A first claim of (repo, pr) inserts one ownership row into the single fleet ledger and the resolver returns that owner."""
    out = _dispatch("report_pr_created",
                    {"repo": "o/r", "pr": 7, "task_id": "T1", "session_id": "S1", "profile": OWNER_PROFILE})
    assert out["status"] == "claimed"

    assert _ownership(artifact.root, repo="o/r", pr=7) == [("o/r", 7, "T1", OWNER_PROFILE)]
    assert not _plugin_db(artifact.home).exists()  # pinned to the ledger owner, not the caller
    assert _ownership_columns(artifact.root) == ["repo", "pr", "task_id", "profile"]

    owner = artifact.loaded.module.resolve_pr_owner("o/r", 7)
    assert owner["task_id"] == "T1" and owner["profile"] == OWNER_PROFILE


def test_ac_gov_f25_2(artifact):
    """A foreign-profile claim is refused naming the holder without overwrite; a same-holder claim is an idempotent refresh."""
    assert _dispatch("report_pr_created",
                     {"repo": "o/r", "pr": 9, "task_id": "T9", "session_id": "S9", "profile": OWNER_PROFILE})["status"] == "claimed"

    refused = _dispatch("report_pr_created",
                        {"repo": "o/r", "pr": 9, "task_id": "TX", "session_id": "SX", "profile": "other-worker"})
    assert refused["status"] == "refused"
    assert refused["holder"] == OWNER_PROFILE
    assert _ownership(artifact.root, repo="o/r", pr=9) == [("o/r", 9, "T9", OWNER_PROFILE)]

    refreshed = _dispatch("report_pr_created",
                          {"repo": "o/r", "pr": 9, "task_id": "T9b", "session_id": "S9", "profile": OWNER_PROFILE})
    assert refreshed["status"] == "refreshed"
    assert _ownership(artifact.root, repo="o/r", pr=9) == [("o/r", 9, "T9b", OWNER_PROFILE)]


def test_ac_gov_f25_3(artifact):
    """A `terminal` gh pr create claims the parsed (repo, pr), binding the owning session onto the card; a non-create command echoing a PR URL claims nothing."""
    card = _bare_card(OWNER_PROFILE, "card-o-r-7")
    artifact.manager.invoke_hook("post_tool_call", tool_name="terminal",
                                 args={"command": "gh pr create --title x --body y"},
                                 result=_terminal_result("https://github.com/o/r/pull/7"),
                                 status="ok", task_id=card, session_id="S1")
    assert _ownership(artifact.root, repo="o/r", pr=7) == [("o/r", 7, card, OWNER_PROFILE)]
    assert _card_session_id(card) == "S1"

    artifact.manager.invoke_hook("post_tool_call", tool_name="terminal",
                                 args={"command": "gh pr create --title x"},
                                 result=_terminal_result("https://github.com/o/r/pull/8"),
                                 status="error", task_id="T8", session_id="S8")
    artifact.manager.invoke_hook("post_tool_call", tool_name="terminal",
                                 args={"command": "gh pr view 8 --web"},
                                 result=_terminal_result("https://github.com/o/r/pull/8"),
                                 status="ok", task_id="T8", session_id="S8")
    assert _ownership(artifact.root) == [("o/r", 7, card, OWNER_PROFILE)]


def test_ac_gov_f25_4(artifact):
    """The same create-intent detection fires for `execute_code`, establishing a card keyed by (repo, pr) so same-number PRs in different repos get distinct cards."""
    artifact.manager.invoke_hook("post_tool_call", tool_name="execute_code",
                                 args={"code": "import subprocess; subprocess.run(['gh','pr','create'])"},
                                 result=_terminal_result("https://github.com/o/r/pull/12"),
                                 status="ok", task_id="", session_id="S12")
    tid1 = _task_id_for(artifact.root, "o/r", 12)
    assert tid1
    assert _card_session_id(tid1) == "S12"

    # (repo, pr) keying: same PR number, different repo -> a distinct card, not a collision.
    artifact.manager.invoke_hook("post_tool_call", tool_name="execute_code",
                                 args={"code": "subprocess.run(['gh','pr','create'])"},
                                 result=_terminal_result("https://github.com/o2/r2/pull/12"),
                                 status="ok", task_id="", session_id="S12b")
    tid2 = _task_id_for(artifact.root, "o2/r2", 12)
    assert tid2 and tid2 != tid1


def test_ac_gov_f25_5(artifact, monkeypatch):
    """A claimed-PR webhook delivers to the owner (native notify+wake, else inject_message on failure) and returns None; unclaimed/off-route/non-webhook -> no delivery; re-delivery is idempotent."""
    import hermes_cli.kanban_db as kb
    task_id = _wake_card(artifact.home, OWNER_PROFILE, "gh-pr-o-r-7", session_id="S1", session_key="owner-session-key")
    _dispatch("report_pr_created", {"repo": "o/r", "pr": 7, "task_id": task_id,
                                    "session_id": "S1", "profile": OWNER_PROFILE})

    def _events():
        with kb.connect() as conn:
            _o, _c, evs = kb.claim_unseen_events_for_sub(
                conn, task_id=task_id, platform="api_server", chat_id="S1", thread_id="",
                kinds=("notification",))
        return [e for e in evs if e.kind == "notification"]

    calls = []
    monkeypatch.setattr(PluginContext, "inject_message",
                        lambda self, content, role="user", *, session_key=None: calls.append(session_key) or True,
                        raising=False)

    assert artifact.manager.invoke_hook("pre_gateway_dispatch", event=_webhook_event("D1", pr=7),
                                        gateway=None, session_store=None) == []
    assert len(_events()) == 1 and calls == []

    # Native publication fails -> fallback resolves the owner key via tasks.session_id -> sessions.session_key.
    monkeypatch.setattr(artifact.loaded.module, "publish_task_notification",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("notify down")), raising=False)
    assert artifact.manager.invoke_hook("pre_gateway_dispatch", event=_webhook_event("D2", pr=7),
                                        gateway=None, session_store=None) == []
    assert calls == ["owner-session-key"]

    inj, ev = len(calls), len(_events())
    for e in (_webhook_event("D3", pr=999), _webhook_event("D4", route="other", pr=7),
              _webhook_event("D5", pr=7, platform=Platform.SLACK, chat_type="slack")):
        assert artifact.manager.invoke_hook("pre_gateway_dispatch", event=e, gateway=None, session_store=None) == []
    assert len(calls) == inj and len(_events()) == ev

    assert artifact.manager.invoke_hook("pre_gateway_dispatch", event=_webhook_event("D2", pr=7),
                                        gateway=None, session_store=None) == []
    assert len(calls) == inj  # a seen delivery id delivers at most once


def test_ac_gov_f25_6(artifact, monkeypatch, capsys):
    """`hermes pr remap` re-points ownership as the orchestrator profile and refuses (no mutation) as a worker profile."""
    _dispatch("report_pr_created", {"repo": "o/r", "pr": 7, "task_id": "T1",
                                    "session_id": "S1", "profile": OWNER_PROFILE})
    remap = artifact.manager._cli_commands["pr"]["handler_fn"]

    _set_profile(monkeypatch, OWNER_PROFILE)
    rc = remap(SimpleNamespace(pr_command="remap", repo="o/r", pr=7, to="new-owner", task=None, json=True))
    assert (rc or 0) != 0
    assert json.loads(capsys.readouterr().out or "{}").get("status") == "refused"
    assert _ownership(artifact.root, repo="o/r", pr=7) == [("o/r", 7, "T1", OWNER_PROFILE)]

    _set_profile(monkeypatch, ORCH_PROFILE)
    rc = remap(SimpleNamespace(pr_command="remap", repo="o/r", pr=7, to="new-owner", task="T2", json=True))
    assert (rc or 0) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "remapped"
    assert _ownership(artifact.root, repo="o/r", pr=7) == [("o/r", 7, "T2", "new-owner")]


def test_ac_obs_f48_1(artifact):
    """kanban terminal + on_session_end append one outcomes row per artifact with cost summed from session_model_usage (actual-else-estimated, aux included), idempotent across repeated on_session_end, reflecting late usage."""
    _dispatch("report_pr_created", {"repo": "o/r", "pr": 7, "task_id": "T1",
                                    "session_id": "S1", "profile": OWNER_PROFILE})
    _seed_session_usage(artifact.home, "S1", [
        {"model": "opus", "actual_cost_usd": 2.00, "cost_status": "actual"},
        {"model": "haiku", "task": "aux", "estimated_cost_usd": 0.50, "cost_status": "estimated"},
    ])

    artifact.manager.invoke_hook("kanban_task_completed", task_id="T1")
    assert _outcomes(artifact.root, "o/r#7")[0][1] == "completed"

    artifact.manager.invoke_hook("on_session_end", session_id="S1", task_id="T1", completed=True)
    artifact.manager.invoke_hook("on_session_end", session_id="S1", task_id="T1", completed=True)
    rows = _outcomes(artifact.root, "o/r#7")
    assert len(rows) == 1
    assert rows[0][2] == pytest.approx(2.50)  # actual+estimated, no double-count on re-fire
    assert rows[0][3] == OWNER_PROFILE

    _seed_session_usage(artifact.home, "S1", [
        {"model": "review", "task": "background-review", "actual_cost_usd": 1.00, "cost_status": "actual"}])
    artifact.manager.invoke_hook("on_session_end", session_id="S1", task_id="T1", completed=True)
    assert _outcomes(artifact.root, "o/r#7")[0][2] == pytest.approx(3.50)  # late usage folded in


def test_ac_obs_f48_2(artifact, monkeypatch, capsys):
    """`hermes outcomes funnel|winrate|cost-per-merge` compute from the outcomes table and refuse (no aggregate leak) for a non-orchestrator profile."""
    mod = artifact.loaded.module
    for art, outcome, cost in [("o/r#1", "merged", 1.00), ("o/r#2", "merged", 2.00), ("o/r#3", "merged", 3.00),
                               ("o/r#4", "closed", 0.50), ("o/r#5", "abandoned", 0.25)]:
        mod._upsert_outcome(art, terminal_outcome=outcome, cost_usd=cost, profile=OWNER_PROFILE)
    outcomes = artifact.manager._cli_commands["outcomes"]["handler_fn"]

    _set_profile(monkeypatch, ORCH_PROFILE)
    outcomes(SimpleNamespace(outcomes_command="funnel", json=True))
    funnel = json.loads(capsys.readouterr().out)
    assert funnel["merged"] == 3 and funnel["closed"] == 1 and funnel["abandoned"] == 1

    outcomes(SimpleNamespace(outcomes_command="winrate", json=True))
    assert json.loads(capsys.readouterr().out)["winrate"] == pytest.approx(3 / 5)

    outcomes(SimpleNamespace(outcomes_command="cost-per-merge", json=True))
    # cost-per-merge divides over MERGED artifacts only, excluding closed/abandoned spend.
    assert json.loads(capsys.readouterr().out)["cost_per_merge"] == pytest.approx((1.00 + 2.00 + 3.00) / 3)

    _set_profile(monkeypatch, OWNER_PROFILE)
    outcomes(SimpleNamespace(outcomes_command="funnel", json=True))
    refused = json.loads(capsys.readouterr().out or "{}")
    assert refused.get("status") == "refused"
    assert not ({"merged", "closed", "abandoned"} & set(refused))
