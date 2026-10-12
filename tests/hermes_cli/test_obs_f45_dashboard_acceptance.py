"""Acceptance coverage for Hermes dashboard surfaces adopted by OBS-F45."""
import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")


def _client():
    """A TestClient over the real module-global FastAPI app, authenticated."""
    try:
        from starlette.testclient import TestClient
    except ImportError:  # pragma: no cover - environment guard
        pytest.skip("fastapi/starlette not installed")
    import hermes_state
    from hermes_constants import get_hermes_home
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    client = TestClient(app, raise_server_exceptions=False)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    hermes_state.DEFAULT_DB_PATH = get_hermes_home() / "state.db"
    return client


@pytest.fixture(autouse=True)
def _isolated(_isolate_hermes_home):
    # Sandboxes HERMES_HOME and patches DEFAULT_DB_PATH to the temp home.
    yield


def _seed_session(session_id, *, input_tokens=0, output_tokens=0, estimated_cost_usd=None):
    from hermes_state import SessionDB

    db = SessionDB()
    try:
        db.create_session(
            session_id=session_id,
            source="cli",
            model="anthropic/claude-sonnet-4",
        )
        if input_tokens or output_tokens or estimated_cost_usd is not None:
            db.update_token_counts(
                session_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost_usd=estimated_cost_usd,
                model="anthropic/claude-sonnet-4",
            )
    finally:
        db.close()


def test_ac_obs_f45_1():
    """Pixel Office / Coworkers roster gate: ui_meta['hermes-bots'] marks a profile Bot-Mode-managed."""
    import yaml
    from hermes_constants import get_hermes_home
    from tools import bot_mode_probe

    home = get_hermes_home()
    meta = home / "profiles" / "alice" / "profile.yaml"
    meta.parent.mkdir(parents=True, exist_ok=True)

    meta.write_text(
        yaml.safe_dump({"ui_meta": {"hermes-bots": {"title": "Alice", "hidden": False}}}),
        encoding="utf-8",
    )
    assert bot_mode_probe.is_bot_mode_managed(home) is True

    meta.write_text(yaml.safe_dump({"model": "anthropic/claude-sonnet-4"}), encoding="utf-8")
    assert bot_mode_probe.is_bot_mode_managed(home) is False


def test_ac_obs_f45_2():
    """Coworkers view: the web dashboard serves the coworker roster the Profiles page renders."""
    client = _client()
    resp = client.get("/api/profiles")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body.get("profiles"), list)
    names = {(p.get("name") or p.get("id")) for p in body["profiles"]}
    assert "default" in names


def test_ac_obs_f45_3():
    """Timeline (Sessions): the dashboard lists a session and its archive verb moves it between views."""
    sid = "obs-f45-sessions-test"
    _seed_session(sid)
    client = _client()

    resp = client.get("/api/sessions")
    assert resp.status_code == 200
    assert sid in resp.text

    archived = client.patch(f"/api/sessions/{sid}", json={"archived": True})
    assert archived.status_code == 200
    assert archived.json()["archived"] is True

    only = client.get("/api/sessions?archived=only")
    assert only.status_code == 200 and sid in only.text
    excl = client.get("/api/sessions?archived=exclude")
    assert excl.status_code == 200 and sid not in excl.text


def test_ac_obs_f45_4():
    """Timeline (Analytics): the API backing the opt-in Analytics page reports tokens and cost by day and model."""
    _seed_session(
        "obs-f45-analytics-test",
        input_tokens=120,
        output_tokens=45,
        estimated_cost_usd=0.25,
    )
    client = _client()

    resp = client.get("/api/analytics/usage?days=7")
    assert resp.status_code == 200
    data = resp.json()
    for key in ("daily", "by_model", "totals"):
        assert key in data

    assert data["totals"]["total_input"] == 120
    assert data["totals"]["total_output"] == 45
    assert data["totals"]["total_estimated_cost"] == pytest.approx(0.25)

    daily_row = next(r for r in data["daily"] if r.get("input_tokens") == 120)
    assert daily_row["output_tokens"] == 45
    assert daily_row["estimated_cost"] == pytest.approx(0.25)

    model_row = next(r for r in data["by_model"] if r.get("model") == "anthropic/claude-sonnet-4")
    assert model_row["input_tokens"] == 120
    assert model_row["output_tokens"] == 45
    assert model_row["estimated_cost"] == pytest.approx(0.25)


def test_ac_obs_f45_5():
    """Admin view: an admission (pairing) request can be listed and approved through the panel."""
    from gateway.pairing import PairingStore

    client = _client()
    store = PairingStore()
    bot_code = store.generate_code("telegram", "user1", "Alice")

    pending = client.get("/api/pairing").json()["pending"]
    request_id = pending[0]["request_id"]
    assert request_id and request_id != bot_code

    approved = client.post(
        "/api/pairing/approve",
        json={"platform": "telegram", "request_id": request_id},
    )
    assert approved.status_code == 200
    assert approved.json()["user"]["user_id"] == "user1"
    assert client.get("/api/pairing").json()["pending"] == []


def test_ac_obs_f45_6():
    """Mapping doc contract: the shipped doc maps all four views onto named Hermes surfaces."""
    # Path assumes the shipped location tests/hermes_cli/<thisfile>: parents[2] == repo root.
    repo_root = Path(__file__).resolve().parents[2]
    doc = repo_root / "website" / "docs" / "user-guide" / "features" / "coworker-fleet-dashboard.md"
    text = doc.read_text(encoding="utf-8")
    lines = text.splitlines()

    assert "| NanoClaw view |" in text
    for view in ("Pixel Office", "Coworkers", "Timeline", "Admin"):
        assert view in text
        row = next((ln for ln in lines if ln.lstrip().startswith(f"| {view} |")), None)
        assert row is not None, f"no mapping-table row for {view}"
        assert re.search(r"tag:\s*\S+:\d+", row), f"{view} row lacks a tag: citation"
        assert re.search(r"main:\s*\S+:\d+", row), f"{view} row lacks a main: citation"

    assert "ui_meta['hermes-bots']" in text
    assert "groups.*" in text
    assert "dashboard.show_token_analytics" in text  # analytics UI is opt-in; doc must record it
    for not_ported in ("/api/hook-event", "dashboard app", "dashboard-as-a-channel", "codex hook bridge"):
        assert not_ported in text
