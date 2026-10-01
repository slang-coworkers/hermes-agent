"""Behaviour tests for nv-bot-chat's canonical Bot Chat resolution beyond the OSH-F64.c criteria."""
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


def _home(tmp_path, monkeypatch, gateway: dict | None) -> Path:
    home = tmp_path / "hermes-home"
    (home / "plugins").mkdir(parents=True)
    shutil.copytree(PLUGIN_DIR, home / "plugins" / PLUGIN_KEY)
    config: dict = {"plugins": {"enabled": [PLUGIN_KEY]}}
    if gateway is not None:
        config["gateway"] = gateway
    (home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    bundled = tmp_path / "bundled-plugins"
    bundled.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    return home


def _db(home: Path, name: str) -> SessionDB:
    profile_home = home if name == "default" else home / "profiles" / name
    profile_home.mkdir(parents=True, exist_ok=True)
    return SessionDB(db_path=profile_home / "state.db")


def _bot_chat(home: Path, name: str, *, source: str = "tui") -> str:
    db = _db(home, name)
    try:
        sid = "botchat-" + name
        db.create_session(sid, source=source)
        db.set_session_title(sid, SessionDB.CANONICAL_BOT_CHAT_TITLE)
        db.set_session_hidden(sid, True)
        db.append_message(sid, "user", "hello " + name)
        return sid
    finally:
        db.close()


def _listed(capsys) -> dict:
    manager = PluginManager()
    manager.discover_and_load()
    capsys.readouterr()
    rc = manager._cli_commands["bot-chats"]["handler_fn"](argparse.Namespace())
    assert rc == 0
    return {row["profile"]: row for row in json.loads(capsys.readouterr().out)["bot_chats"]}


@pytest.fixture
def client_for(monkeypatch):
    """Mount the plugin router through the stock discovery + mount path, on a fresh app."""
    def _client():
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from hermes_cli import web_server

        monkeypatch.setattr(web_server, "app", FastAPI())
        monkeypatch.setattr(web_server, "_dashboard_plugins_cache", None)
        web_server._get_dashboard_plugins(force_rescan=True)
        web_server._mount_plugin_api_routes()
        return TestClient(web_server.app)

    return _client


MULTIPLEX = {"multiplex_profiles": True, "multiplex_profile_allowlist": ["orchestrator", "builder"]}


def test_archived_canonical_bot_chat_is_not_listed(tmp_path, monkeypatch, capsys):
    home = _home(tmp_path, monkeypatch, MULTIPLEX)
    _bot_chat(home, "orchestrator")
    archived = _bot_chat(home, "builder")
    db = _db(home, "builder")
    try:
        db.set_session_archived(archived, True)
    finally:
        db.close()

    listed = _listed(capsys)
    assert "orchestrator" in listed
    assert "builder" not in listed


def test_hidden_bot_chat_from_any_source_is_listed_and_opens(tmp_path, monkeypatch, capsys, client_for):
    home = _home(tmp_path, monkeypatch, MULTIPLEX)
    sid = _bot_chat(home, "orchestrator", source="tool")

    assert _listed(capsys)["orchestrator"]["session_id"] == sid
    r = client_for().get(f"{API_PREFIX}/bot-chats/orchestrator/{sid}/messages")
    assert r.status_code == 200
    assert "hello orchestrator" in [m.get("content") for m in r.json()["messages"]]


def test_served_profile_without_a_session_store_is_skipped(tmp_path, monkeypatch, capsys):
    home = _home(tmp_path, monkeypatch, MULTIPLEX)
    _bot_chat(home, "orchestrator")
    (home / "profiles" / "builder").mkdir(parents=True)

    listed = _listed(capsys)
    assert "orchestrator" in listed
    assert "builder" not in listed
    assert not (home / "profiles" / "builder" / "state.db").exists()


def test_without_multiplex_only_the_active_profile_is_served(tmp_path, monkeypatch, capsys):
    home = _home(tmp_path, monkeypatch, None)
    default_sid = _bot_chat(home, "default")
    _bot_chat(home, "orchestrator")

    listed = _listed(capsys)
    assert listed["default"]["session_id"] == default_sid
    assert "orchestrator" not in listed


def test_listing_reports_the_compression_tip_and_its_message_count(tmp_path, monkeypatch, capsys):
    home = _home(tmp_path, monkeypatch, MULTIPLEX)
    root = _bot_chat(home, "orchestrator")
    db = _db(home, "orchestrator")
    try:
        db.end_session(root, "compression")
        db.create_session(root + "-cont", source="tui", parent_session_id=root)
        for text in ("one", "two", "three"):
            db.append_message(root + "-cont", "assistant", text)
    finally:
        db.close()

    row = _listed(capsys)["orchestrator"]
    assert row["session_id"] == root
    assert row["resolved_id"] == root + "-cont"
    assert row["message_count"] == 3
