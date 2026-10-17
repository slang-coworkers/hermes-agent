"""Acceptance test for OSH-F64.c — the nv-bot-chat dashboard plugin.

Ships as: tests/plugins/test_nv_bot_chat_acceptance.py

The web dashboard cannot open a profile's hidden canonical "Bot Chat" on the stock tree
(GET /api/sessions has no include_hidden; hidden rows are filtered in hermes_state). This plugin
surfaces and opens it through the stock dashboard-plugin mechanism only.

One test function per `pytest:` criterion (AC-OSH-F64.c-1..5). AC-OSH-F64-5 is a `ui:` criterion
proven by tests/e2e-scenarios/OSH-F64.c/AC-OSH-F64-5.md.

On the stock v2026.8.31 tree plugins/nv-bot-chat does not exist, so setup fails in copytree.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import pytest
import yaml

from hermes_cli.plugins import PluginManager
from hermes_state import SessionDB

PLUGIN_KEY = "nv-bot-chat"
PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / PLUGIN_KEY
API_PREFIX = "/api/plugins/" + PLUGIN_KEY
CANONICAL_TITLE = SessionDB.CANONICAL_BOT_CHAT_TITLE

# A multiplex gateway always serves the default profile too (its home is HERMES_HOME itself).
DEFAULT = "default"
SERVED = ("orchestrator", "builder")
# Served, but its "Bot Chat"-titled session is visible: hidden is what marks the canonical one.
VISIBLE_TITLE = "reviewer"
# Installed with its own Bot Chat but outside the gateway allowlist: must never be listed or opened.
NOT_SERVED = "stray"


def _install_plugin(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "hermes-home"
    (home / "plugins").mkdir(parents=True)
    shutil.copytree(PLUGIN_DIR, home / "plugins" / PLUGIN_KEY)
    config = {
        "plugins": {"enabled": [PLUGIN_KEY]},
        "gateway": {
            "multiplex_profiles": True,
            "multiplex_profile_allowlist": [*SERVED, VISIBLE_TITLE],
        },
    }
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    empty_bundled = tmp_path / "bundled-plugins"
    empty_bundled.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(empty_bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    return home


def _open_db(home: Path, name: str) -> SessionDB:
    profile_home = home if name == DEFAULT else home / "profiles" / name
    profile_home.mkdir(parents=True, exist_ok=True)
    return SessionDB(db_path=profile_home / "state.db")


def _seed_profile(home: Path, name: str, *, hidden: bool = True) -> str:
    """One visible session plus a session titled "Bot Chat"; returns the Bot Chat id."""
    db = _open_db(home, name)
    try:
        visible = "visible-" + name
        db.create_session(visible, source="cli")
        db.append_message(visible, "user", "visible " + name)
        bot = "botchat-" + name
        db.create_session(bot, source="tui")
        db.set_session_title(bot, CANONICAL_TITLE)
        db.set_session_hidden(bot, hidden)
        db.append_message(bot, "user", "ping " + name)
        db.append_message(bot, "assistant", "pong " + name)
        return bot
    finally:
        db.close()


def _compress(home: Path, name: str, bot: str) -> str:
    """End the Bot Chat by compression and continue it in a child holding a new reply."""
    db = _open_db(home, name)
    try:
        db.end_session(bot, "compression")
        child = bot + "-cont"
        db.create_session(child, source="tui", parent_session_id=bot)
        db.append_message(child, "assistant", "after compression " + name)
        return child
    finally:
        db.close()


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    home = _install_plugin(tmp_path, monkeypatch)
    ids = {name: _seed_profile(home, name) for name in (DEFAULT, *SERVED, NOT_SERVED)}
    ids[VISIBLE_TITLE] = _seed_profile(home, VISIBLE_TITLE, hidden=False)
    return home, ids


@pytest.fixture
def client(fleet, monkeypatch):
    """The plugin's router mounted by the stock discovery + mount path, on a fresh app."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from hermes_cli import web_server

    monkeypatch.setattr(web_server, "app", FastAPI())
    monkeypatch.setattr(web_server, "_dashboard_plugins_cache", None)
    web_server._get_dashboard_plugins(force_rescan=True)
    web_server._mount_plugin_api_routes()
    return TestClient(web_server.app)


def test_ac_osh_f64_c_1(fleet, capsys):
    """The plugin loads on a stock tree through real plugin discovery, registers the `bot-chats` CLI verb, and its resolver returns each SERVED profile's canonical "Bot Chat" session — rows the stock non-hidden session listing omits — while excluding an installed profile the gateway does not serve and a served profile whose only "Bot Chat"-titled session is visible."""
    home, ids = fleet
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled is True
    assert loaded.error is None
    assert loaded.module is not None

    entry = manager._cli_commands["bot-chats"]
    assert entry["plugin_key"] == PLUGIN_KEY
    capsys.readouterr()
    rc = entry["handler_fn"](argparse.Namespace())
    assert rc in (0, None)
    listed = json.loads(capsys.readouterr().out)["bot_chats"]
    by_profile = {row["profile"]: row["session_id"] for row in listed}

    for name in (DEFAULT, *SERVED):
        assert by_profile.get(name) == ids[name]
    assert NOT_SERVED not in by_profile
    assert VISIBLE_TITLE not in by_profile

    db = SessionDB(db_path=home / "profiles" / "orchestrator" / "state.db", read_only=True)
    try:
        visible_ids = {row["id"] for row in db.list_sessions_rich(limit=100)}
    finally:
        db.close()
    assert "visible-orchestrator" in visible_ids
    assert ids["orchestrator"] not in visible_ids


def test_ac_osh_f64_c_2(client):
    """The plugin contributes a web-dashboard tab + HTTP API through the STOCK dashboard-plugin mechanism: discovery reports its tab and an importable api, and the stock mount serves GET /api/plugins/nv-bot-chat/bot-chats listing the served profiles' canonical Bot Chats."""
    from hermes_cli import web_server

    entry = next(
        (p for p in web_server._get_dashboard_plugins() if p.get("name") == PLUGIN_KEY), None
    )
    assert entry is not None
    assert entry["tab"]["path"] == "/bot-chat"
    assert entry["has_api"] is True
    assert entry["_api_file"] == "plugin_api.py"

    r = client.get(API_PREFIX + "/bot-chats")
    assert r.status_code == 200
    listed = {row["profile"] for row in r.json()["bot_chats"]}
    assert {DEFAULT, *SERVED} <= listed
    assert NOT_SERVED not in listed


def test_ac_osh_f64_c_3(fleet, client):
    """Opening a listed Bot Chat returns its transcript from the live compression tip — the same resolution the stock messages route and the desktop use — so a reply written after context compression is shown."""
    home, ids = fleet
    root = ids["orchestrator"]
    _compress(home, "orchestrator", root)

    r = client.get(f"{API_PREFIX}/bot-chats/orchestrator/{root}/messages")
    assert r.status_code == 200
    contents = [m.get("content") for m in r.json()["messages"]]
    assert "after compression orchestrator" in contents


def test_ac_osh_f64_c_4(fleet, client):
    """The transcript route is an access boundary: it refuses (404) a profile the gateway does not serve and a session id that is not that profile's canonical Bot Chat, without returning any message content."""
    _, ids = fleet
    # Positive control: the route exists and serves the canonical chat of a served profile.
    ok = client.get(f"{API_PREFIX}/bot-chats/orchestrator/{ids['orchestrator']}/messages")
    assert ok.status_code == 200
    assert "pong orchestrator" in ok.text

    stray = client.get(f"{API_PREFIX}/bot-chats/{NOT_SERVED}/{ids[NOT_SERVED]}/messages")
    assert stray.status_code == 404
    assert "ping stray" not in stray.text
    assert "pong stray" not in stray.text

    other = client.get(f"{API_PREFIX}/bot-chats/orchestrator/visible-orchestrator/messages")
    assert other.status_code == 404
    assert "visible orchestrator" not in other.text


def test_ac_osh_f64_c_5(fleet, client):
    """Opening a Bot Chat whose earlier turns were compacted in place still shows those turns — the transcript read includes compaction-preserved display history."""
    home, ids = fleet
    bot = ids["orchestrator"]
    db = _open_db(home, "orchestrator")
    try:
        db.archive_and_compact(bot, [{"role": "assistant", "content": "summary of earlier turns"}])
    finally:
        db.close()

    r = client.get(f"{API_PREFIX}/bot-chats/orchestrator/{bot}/messages")
    assert r.status_code == 200
    contents = [m.get("content") for m in r.json()["messages"]]
    assert "ping orchestrator" in contents
    assert "pong orchestrator" in contents
