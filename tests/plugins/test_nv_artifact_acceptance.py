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
from hermes_cli.plugins import PluginManager
from tools.registry import registry, invalidate_check_fn_cache

PLUGIN_KEY = "nv-artifact"
PLUGIN_SRC = Path(__file__).parents[2] / "plugins" / PLUGIN_KEY

OWNER_PROFILE = "gov-f25-owner"
ORCH_PROFILE = "gov-f25-orch"
NOTIFY_ROUTE = "gh-pr"

# The rendered webhook summary the observer forwards verbatim as the notification
# message (ADR §Design: message = event.text). A literal lets the test assert the
# enqueued payload is the exact event, not merely "some notification".
EVENT_DIGEST = "review submitted on o/r#7 @beefcafe"


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
        text=EVENT_DIGEST,
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


def _notifications(task_id: str) -> list:
    """Raw payload text of every `notification` event on the owning task (kanban task_events).

    `_notification_payloads` JSON-decodes these; the plugin's cross-profile WAKE of the
    events is proven in the core test, not here.
    """
    import hermes_cli.kanban_db as kb
    with kb.connect() as conn:
        try:
            rows = list(conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='notification' ORDER BY id",
                (task_id,)))
        except sqlite3.OperationalError:
            return []
    return [(r[0] or "") for r in rows]


def _notification_payloads(task_id: str) -> list[dict]:
    """The JSON-decoded payload dict of every `notification` event (message + idempotency_key)."""
    return [json.loads(p) for p in _notifications(task_id)]


def _seen(base: Path, repo: str, pr: int, delivery_id: str) -> bool:
    """True iff the plugin recorded a seen_deliveries dedup row for this delivery."""
    db = _plugin_db(base)
    if not db.exists():
        return False
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT 1 FROM seen_deliveries WHERE repo=? AND pr=? AND delivery_id=?",
            (repo, pr, delivery_id)).fetchone()
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()
    return row is not None


def _wake_subs(task_id: str) -> list[tuple]:
    """Every notify sub on the owning task as (platform, notifier_profile, delivery_mode, retry_policy)."""
    import hermes_cli.kanban_db as kb
    with kb.connect() as conn:
        # kb.connect() sets row_factory=sqlite3.Row; coerce to plain tuples so the
        # equality against the expected tuple compares by value, not object.
        return [tuple(r) for r in conn.execute(
            "SELECT platform, notifier_profile, delivery_mode, retry_policy "
            "FROM kanban_notify_subs WHERE task_id=? ORDER BY rowid",
            (task_id,))]


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
    # report_pr_created is a mutation tool the observer dispatches internally; its
    # check_fn=lambda: False must keep it OUT of the model-visible tool schema.
    invalidate_check_fn_cache()
    assert registry.get_definitions({"report_pr_created"}, quiet=True) == []
    # register_cli_command verbs land in manager._cli_commands (kind "cli_command"),
    # NOT loaded.commands_registered (which is register_command slash commands only).
    cli_verbs = {name for name, e in manager._cli_commands.items() if e.get("plugin_key") == PLUGIN_KEY}
    assert {"pr", "outcomes"} <= cli_verbs

    return SimpleNamespace(manager=manager, loaded=loaded, root=root, home=home)


def test_ac_gov_f25_1(artifact):
    """A first claim of (repo, pr) inserts one ownership row into the single fleet ledger and the resolver returns that owner."""
    card = _bare_card(OWNER_PROFILE, "gh-pr-o-r-7")
    out = _dispatch("report_pr_created",
                    {"repo": "o/r", "pr": 7, "task_id": card, "session_id": "S1", "profile": OWNER_PROFILE})
    assert out["status"] == "claimed"

    assert _ownership(artifact.root, repo="o/r", pr=7) == [("o/r", 7, card, OWNER_PROFILE)]
    assert not _plugin_db(artifact.home).exists()  # pinned to the ledger owner, not the caller
    assert _ownership_columns(artifact.root) == ["repo", "pr", "task_id", "profile"]

    owner = artifact.loaded.module.resolve_pr_owner("o/r", 7)
    assert owner["task_id"] == card and owner["profile"] == OWNER_PROFILE


def test_ac_gov_f25_2(artifact):
    """A foreign-profile claim is refused naming the holder without overwrite; a same-holder claim is an idempotent refresh."""
    card_a = _bare_card(OWNER_PROFILE, "gh-pr-o-r-9")
    assert _dispatch("report_pr_created",
                     {"repo": "o/r", "pr": 9, "task_id": card_a, "session_id": "S9", "profile": OWNER_PROFILE})["status"] == "claimed"

    card_x = _bare_card("other-worker", "gh-pr-o-r-9-x")
    refused = _dispatch("report_pr_created",
                        {"repo": "o/r", "pr": 9, "task_id": card_x, "session_id": "SX", "profile": "other-worker"})
    assert refused["status"] == "refused"
    assert refused["holder"] == OWNER_PROFILE
    assert _ownership(artifact.root, repo="o/r", pr=9) == [("o/r", 9, card_a, OWNER_PROFILE)]

    card_b = _bare_card(OWNER_PROFILE, "gh-pr-o-r-9-b")
    refreshed = _dispatch("report_pr_created",
                          {"repo": "o/r", "pr": 9, "task_id": card_b, "session_id": "S9", "profile": OWNER_PROFILE})
    assert refreshed["status"] == "refreshed"
    assert _ownership(artifact.root, repo="o/r", pr=9) == [("o/r", 9, card_b, OWNER_PROFILE)]


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
    """A claimed-PR webhook durably enqueues exactly one `notification` on the owning task carrying the verbatim event digest and a stable (repo#pr, delivery-id) idempotency key onto a durable-retry wake sub, and the observer returns skip; an unclaimed / off-route / non-webhook event returns None with no enqueue; a re-delivered delivery-id does not double-enqueue; and a claimed enqueue-failure is fail-closed (skip, never None, nothing committed) then recovers on retry."""
    # Claim (o/r, 7) via the real post_tool_call path: ensures the owning card,
    # binds S1, and registers the durable wake sub.
    artifact.manager.invoke_hook("post_tool_call", tool_name="terminal",
                                 args={"command": "gh pr create --title x --body y"},
                                 result=_terminal_result("https://github.com/o/r/pull/7"),
                                 status="ok", task_id="", session_id="S1")
    card = _task_id_for(artifact.root, "o/r", 7)
    assert card and _card_session_id(card) == "S1"

    # The claim registered exactly one durable-retry wake sub, scoped to the owner profile.
    assert _wake_subs(card) == [("api_server", OWNER_PROFILE, "wake", "durable")]

    def _dispatch_hook(delivery, **kw):
        return artifact.manager.invoke_hook(
            "pre_gateway_dispatch", event=_webhook_event(delivery, **kw), gateway=None, session_store=None)

    # CLAIMED -> skip + exactly one notification carrying the verbatim digest and the exact key.
    assert _dispatch_hook("D1", pr=7) == [{"action": "skip"}]
    payloads = _notification_payloads(card)
    assert len(payloads) == 1
    assert payloads[0]["message"] == EVENT_DIGEST
    assert payloads[0]["idempotency_key"] == "o/r#7:D1"
    assert _seen(artifact.root, "o/r", 7, "D1")

    # UNCLAIMED / off-route / non-webhook -> None (empty invoke_hook result), no new notification.
    assert _dispatch_hook("D2", pr=999) == []
    assert _dispatch_hook("D3", pr=7, route="other") == []
    assert _dispatch_hook("D4", pr=7, platform=Platform.SLACK, chat_type="slack") == []
    assert len(_notification_payloads(card)) == 1

    # Re-delivery of the same X-GitHub-Delivery id -> no second enqueue (seen_deliveries dedup).
    assert _dispatch_hook("D1", pr=7) == [{"action": "skip"}]
    assert len(_notification_payloads(card)) == 1

    # Claimed enqueue-failure -> FAIL CLOSED: skip (never None) with NOTHING committed, so a
    # claimed PR never re-opens the default orphan even when the durable enqueue cannot commit.
    # (The plugin references publish_task_notification as a module-level name, so patching the
    # symbol on the plugin module reaches the observer's call site.)
    real = artifact.loaded.module.publish_task_notification
    state = {"fail": True, "calls": 0}

    def _maybe_boom(*a, **k):
        state["calls"] += 1
        if state["fail"]:
            raise RuntimeError("kanban down")
        return real(*a, **k)

    monkeypatch.setattr(artifact.loaded.module, "publish_task_notification", _maybe_boom, raising=True)
    assert _dispatch_hook("D5", pr=7) == [{"action": "skip"}]
    assert state["calls"] >= 1
    assert len(_notification_payloads(card)) == 1
    assert not _seen(artifact.root, "o/r", 7, "D5")

    # kanban recovers -> the same delivery now enqueues durably (publish-then-seen).
    state["fail"] = False
    assert _dispatch_hook("D5", pr=7) == [{"action": "skip"}]
    payloads = _notification_payloads(card)
    assert len(payloads) == 2
    assert payloads[1]["idempotency_key"] == "o/r#7:D5"
    assert _seen(artifact.root, "o/r", 7, "D5")


def test_ac_gov_f25_6(artifact, monkeypatch, capsys):
    """`hermes pr remap` re-points ownership as the orchestrator profile and refuses (no mutation) as a worker profile."""
    card = _bare_card(OWNER_PROFILE, "gh-pr-o-r-7")
    _dispatch("report_pr_created", {"repo": "o/r", "pr": 7, "task_id": card,
                                    "session_id": "S1", "profile": OWNER_PROFILE})
    remap = artifact.manager._cli_commands["pr"]["handler_fn"]

    _set_profile(monkeypatch, OWNER_PROFILE)
    rc = remap(SimpleNamespace(pr_command="remap", repo="o/r", pr=7, to="new-owner", task=None, json=True))
    assert (rc or 0) != 0
    assert json.loads(capsys.readouterr().out or "{}").get("status") == "refused"
    assert _ownership(artifact.root, repo="o/r", pr=7) == [("o/r", 7, card, OWNER_PROFILE)]

    # A cross-profile remap targets a DISTINCT task bound to the new owner's
    # session, so its own deliverable route can be installed before the re-point.
    import hermes_cli.kanban_db as kb
    card2 = _bare_card("new-owner", "gh-pr-o-r-7-remap")
    with kb.connect() as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET session_id=? WHERE id=?", ("S-new", card2))
    _set_profile(monkeypatch, ORCH_PROFILE)
    rc = remap(SimpleNamespace(pr_command="remap", repo="o/r", pr=7, to="new-owner", task=card2, json=True))
    assert (rc or 0) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "remapped"
    assert _ownership(artifact.root, repo="o/r", pr=7) == [("o/r", 7, card2, "new-owner")]


def test_ac_obs_f48_1(artifact):
    """kanban terminal + on_session_end append one outcomes row per artifact with cost summed from session_model_usage (actual-else-estimated, aux included), idempotent across repeated on_session_end, reflecting late usage; and a claimed pull_request closed+merged webhook derives terminal_outcome='merged' (retaining cost/profile) — the signal winrate/cost-per-merge depend on."""
    card = _bare_card(OWNER_PROFILE, "gh-pr-o-r-7")
    _dispatch("report_pr_created", {"repo": "o/r", "pr": 7, "task_id": card,
                                    "session_id": "S1", "profile": OWNER_PROFILE})
    _seed_session_usage(artifact.home, "S1", [
        {"model": "opus", "actual_cost_usd": 2.00, "cost_status": "actual"},
        {"model": "haiku", "task": "aux", "estimated_cost_usd": 0.50, "cost_status": "estimated"},
    ])

    artifact.manager.invoke_hook("kanban_task_completed", task_id=card)
    assert _outcomes(artifact.root, "o/r#7")[0][1] == "completed"

    artifact.manager.invoke_hook("on_session_end", session_id="S1", task_id=card, completed=True)
    artifact.manager.invoke_hook("on_session_end", session_id="S1", task_id=card, completed=True)
    rows = _outcomes(artifact.root, "o/r#7")
    assert len(rows) == 1
    assert rows[0][2] == pytest.approx(2.50)
    assert rows[0][3] == OWNER_PROFILE

    _seed_session_usage(artifact.home, "S1", [
        {"model": "review", "task": "background-review", "actual_cost_usd": 1.00, "cost_status": "actual"}])
    artifact.manager.invoke_hook("on_session_end", session_id="S1", task_id=card, completed=True)
    assert _outcomes(artifact.root, "o/r#7")[0][2] == pytest.approx(3.50)

    merged_event = SimpleNamespace(
        text="pr merged",
        source=SimpleNamespace(platform=Platform.WEBHOOK, chat_type="webhook",
                               chat_id=f"webhook:{NOTIFY_ROUTE}:M1"),
        raw_message={"action": "closed",
                     "repository": {"full_name": "o/r"},
                     "pull_request": {"number": 7, "merged": True, "head": {"sha": "beefcafe"}}},
        message_id="M1")
    artifact.manager.invoke_hook("pre_gateway_dispatch", event=merged_event,
                                 gateway=None, session_store=None)
    row = _outcomes(artifact.root, "o/r#7")[0]
    assert row[1] == "merged"
    assert row[2] == pytest.approx(3.50) and row[3] == OWNER_PROFILE


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
    assert json.loads(capsys.readouterr().out)["cost_per_merge"] == pytest.approx((1.00 + 2.00 + 3.00) / 3)

    _set_profile(monkeypatch, OWNER_PROFILE)
    rc = outcomes(SimpleNamespace(outcomes_command="funnel", json=True))
    assert (rc or 0) != 0
    refused = json.loads(capsys.readouterr().out or "{}")
    assert refused.get("status") == "refused"
    assert not ({"merged", "closed", "abandoned"} & set(refused))


# ---------------------------------------------------------------------------
# Additional regression coverage (not new acceptance criteria — the ADR ids
# are fixed and minted only by the architect).
# ---------------------------------------------------------------------------

def _fail_wake_sub_until(monkeypatch, mod):
    """Patch the module-level ``_register_wake_sub`` so it fails while
    ``state['fail']`` is True, returning the real function otherwise. Returns the
    ``state`` dict (flip ``fail`` to False to let a retry succeed). Mirrors the
    module-symbol patch in test_ac_gov_f25_5 (avoids monkeypatch.undo(), which
    would also undo the fixture's HERMES_HOME setenv on the shared instance)."""
    real = mod._register_wake_sub
    state = {"fail": True}
    monkeypatch.setattr(
        mod, "_register_wake_sub",
        lambda *a, **k: False if state["fail"] else real(*a, **k),
        raising=True,
    )
    return state


def test_claim_registration_atomic(artifact, monkeypatch):
    """A claim whose durable-wake-route registration fails commits NO
    ownership row (no route-less claim is ever visible) and installs no sub; a
    webhook for the still-unclaimed PR routes normally with no orphan enqueue; a
    retry with a working route claims, installs exactly one sub, and the next
    webhook is deliverable onto it."""
    mod = artifact.loaded.module
    state = _fail_wake_sub_until(monkeypatch, mod)
    card = _bare_card(OWNER_PROFILE, "gh-pr-o-r-7")
    out = _dispatch("report_pr_created", {"repo": "o/r", "pr": 7, "task_id": card,
                                          "session_id": "S1", "profile": OWNER_PROFILE})
    assert out["status"] == "error"
    assert _ownership(artifact.root, repo="o/r", pr=7) == []
    assert _wake_subs(card) == []
    # The PR is still UNCLAIMED, so its webhook routes normally (returns None) and
    # enqueues nothing — there is no stranded, undeliverable event.
    assert artifact.manager.invoke_hook(
        "pre_gateway_dispatch", event=_webhook_event("D1"), gateway=None, session_store=None) == []
    assert _notification_payloads(card) == []

    state["fail"] = False
    assert _dispatch("report_pr_created", {"repo": "o/r", "pr": 7, "task_id": card,
                                           "session_id": "S1", "profile": OWNER_PROFILE})["status"] == "claimed"
    assert _wake_subs(card) == [("api_server", OWNER_PROFILE, "wake", "durable")]
    # A webhook injected after the successful retry is deliverable onto the sub.
    assert artifact.manager.invoke_hook(
        "pre_gateway_dispatch", event=_webhook_event("D2"), gateway=None, session_store=None) == [{"action": "skip"}]
    assert len(_notification_payloads(card)) == 1


def test_refresh_registration_atomic(artifact, monkeypatch):
    """A same-holder refresh whose route registration fails does NOT change
    the visible task_id (the prior route/ownership stay intact); a working retry
    refreshes."""
    card_a = _bare_card(OWNER_PROFILE, "gh-pr-o-r-9")
    assert _dispatch("report_pr_created", {"repo": "o/r", "pr": 9, "task_id": card_a,
                                           "session_id": "S9", "profile": OWNER_PROFILE})["status"] == "claimed"
    mod = artifact.loaded.module
    state = _fail_wake_sub_until(monkeypatch, mod)
    card_b = _bare_card(OWNER_PROFILE, "gh-pr-o-r-9-b")
    out = _dispatch("report_pr_created", {"repo": "o/r", "pr": 9, "task_id": card_b,
                                          "session_id": "S9", "profile": OWNER_PROFILE})
    assert out["status"] == "error"
    assert _ownership(artifact.root, repo="o/r", pr=9) == [("o/r", 9, card_a, OWNER_PROFILE)]

    state["fail"] = False
    assert _dispatch("report_pr_created", {"repo": "o/r", "pr": 9, "task_id": card_b,
                                           "session_id": "S9", "profile": OWNER_PROFILE})["status"] == "refreshed"
    assert _ownership(artifact.root, repo="o/r", pr=9) == [("o/r", 9, card_b, OWNER_PROFILE)]


def test_remap_registration_atomic(artifact, monkeypatch):
    """A remap to an owner WITH a bound session whose route registration
    fails leaves ownership unchanged; a working retry re-points."""
    import hermes_cli.kanban_db as kb

    card = _bare_card(OWNER_PROFILE, "gh-pr-o-r-7")
    _dispatch("report_pr_created", {"repo": "o/r", "pr": 7, "task_id": card,
                                    "session_id": "S1", "profile": OWNER_PROFILE})
    card2 = _bare_card("new-owner", "gh-pr-o-r-7-remap")
    with kb.connect() as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET session_id=? WHERE id=?", ("S2", card2))
    remap = artifact.manager._cli_commands["pr"]["handler_fn"]
    _set_profile(monkeypatch, ORCH_PROFILE)
    mod = artifact.loaded.module
    state = _fail_wake_sub_until(monkeypatch, mod)

    rc = remap(SimpleNamespace(pr_command="remap", repo="o/r", pr=7, to="new-owner", task=card2, json=True))
    assert (rc or 0) != 0
    assert _ownership(artifact.root, repo="o/r", pr=7) == [("o/r", 7, card, OWNER_PROFILE)]

    state["fail"] = False
    rc = remap(SimpleNamespace(pr_command="remap", repo="o/r", pr=7, to="new-owner", task=card2, json=True))
    assert (rc or 0) == 0
    assert _ownership(artifact.root, repo="o/r", pr=7) == [("o/r", 7, card2, "new-owner")]


def test_session_end_gateway_session_attributes_cost(artifact):
    """A gateway/API turn's on_session_end passes the SESSION id as task_id
    (not the owning card, which is what ``api_server._run_agent`` does with
    ``effective_task_id``); cost must still reach the artifact via the
    session→card fallback, not silently drop."""
    artifact.manager.invoke_hook("post_tool_call", tool_name="terminal",
                                 args={"command": "gh pr create --title x --body y"},
                                 result=_terminal_result("https://github.com/o/r/pull/7"),
                                 status="ok", task_id="", session_id="S1")
    card = _task_id_for(artifact.root, "o/r", 7)
    assert card and _card_session_id(card) == "S1"
    _seed_session_usage(artifact.home, "S1", [
        {"model": "opus", "actual_cost_usd": 4.00, "cost_status": "actual"}])

    # The API turn passes the session id — NOT the card — as task_id.
    artifact.manager.invoke_hook("on_session_end", session_id="S1", task_id="S1", completed=True)
    rows = _outcomes(artifact.root, "o/r#7")
    assert len(rows) == 1
    assert rows[0][2] == pytest.approx(4.00)
    assert rows[0][3] == OWNER_PROFILE


def test_remap_without_bound_session_is_refused(artifact, monkeypatch, capsys):
    """A remap to a task with no bound session is refused with no
    mutation — re-pointing to an undeliverable owner would strand a webhook that
    arrives before that owner binds a session."""
    card = _bare_card(OWNER_PROFILE, "gh-pr-o-r-7")
    _dispatch("report_pr_created", {"repo": "o/r", "pr": 7, "task_id": card,
                                    "session_id": "S1", "profile": OWNER_PROFILE})
    sessionless = _bare_card("new-owner", "gh-pr-o-r-7-nosession")  # distinct, no bound session
    remap = artifact.manager._cli_commands["pr"]["handler_fn"]
    _set_profile(monkeypatch, ORCH_PROFILE)

    rc = remap(SimpleNamespace(pr_command="remap", repo="o/r", pr=7, to="new-owner", task=sessionless, json=True))
    assert (rc or 0) != 0
    assert json.loads(capsys.readouterr().out)["status"] == "refused"
    assert _ownership(artifact.root, repo="o/r", pr=7) == [("o/r", 7, card, OWNER_PROFILE)]


def test_same_task_cross_profile_remap_is_refused(artifact, monkeypatch, capsys):
    """A cross-profile remap that keeps the SAME task is refused —
    honouring it would mutate the current owner's own route, which a mid-remap
    ledger commit failure could strand (new route paired with the old owner)."""
    card = _bare_card(OWNER_PROFILE, "gh-pr-o-r-7")
    _dispatch("report_pr_created", {"repo": "o/r", "pr": 7, "task_id": card,
                                    "session_id": "S1", "profile": OWNER_PROFILE})
    remap = artifact.manager._cli_commands["pr"]["handler_fn"]
    _set_profile(monkeypatch, ORCH_PROFILE)

    # task=None keeps the current task; a different `to` profile → refused.
    rc = remap(SimpleNamespace(pr_command="remap", repo="o/r", pr=7, to="new-owner", task=None, json=True))
    assert (rc or 0) != 0
    assert json.loads(capsys.readouterr().out)["status"] == "refused"
    assert _ownership(artifact.root, repo="o/r", pr=7) == [("o/r", 7, card, OWNER_PROFILE)]


def test_remap_task_not_owned_by_target_profile_is_refused(artifact, monkeypatch, capsys):
    """A remap whose target task is owned by a DIFFERENT profile than `--to` is
    refused: the task's bound session lives in the owner's state.db, so recording
    `to` while routing to another profile's task would misroute the wake to a
    session that does not exist under `to`'s scope."""
    import hermes_cli.kanban_db as kb

    card = _bare_card(OWNER_PROFILE, "gh-pr-o-r-7")
    _dispatch("report_pr_created", {"repo": "o/r", "pr": 7, "task_id": card,
                                    "session_id": "S1", "profile": OWNER_PROFILE})
    # A session-bound task assigned to someone OTHER than the --to profile.
    other = _bare_card("someone-else", "gh-pr-o-r-7-other")
    with kb.connect() as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET session_id=? WHERE id=?", ("S-other", other))
    remap = artifact.manager._cli_commands["pr"]["handler_fn"]
    _set_profile(monkeypatch, ORCH_PROFILE)

    rc = remap(SimpleNamespace(pr_command="remap", repo="o/r", pr=7, to="new-owner", task=other, json=True))
    assert (rc or 0) != 0
    assert json.loads(capsys.readouterr().out)["status"] == "refused"
    assert _ownership(artifact.root, repo="o/r", pr=7) == [("o/r", 7, card, OWNER_PROFILE)]
