"""nv-bot-chat dashboard backend — mounted at /api/plugins/nv-bot-chat/.

``GET /bot-chats`` lists every served profile's hidden canonical Bot Chat;
``GET /bot-chats/{profile}/{session_id}/messages`` opens one. The transcript
route takes the session store from the served set, never from the request
path: a profile the gateway does not serve has no store to open, and an id that
is not that served profile's canonical Bot Chat is a 404 that never reaches the
transcript read.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException


def _load_resolver():
    # The dashboard imports this file standalone (no package context), so the
    # resolver shared with the CLI verb is loaded from the plugin root by path.
    path = Path(__file__).resolve().parent.parent / "resolver.py"
    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_nv_bot_chat_resolver", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


resolver = _load_resolver()
router = APIRouter()


@router.get("/bot-chats")
def list_bot_chats() -> Dict[str, Any]:
    return {"bot_chats": resolver.list_canonical_bot_chats()}


@router.get("/bot-chats/{profile}/{session_id}/messages")
def bot_chat_messages(profile: str, session_id: str) -> Dict[str, Any]:
    found = resolver.find_canonical(profile, session_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Bot Chat not found")
    entry, db_path = found
    return {
        "profile": entry["profile"],
        "session_id": entry["session_id"],
        "resolved_id": entry["resolved_id"],
        "messages": resolver.read_transcript(entry, db_path),
    }
