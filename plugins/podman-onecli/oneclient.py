"""OneCLI control-plane client for the podman-onecli plugin (CRED-F28).

One injectable transport seam and no import-time network. When a transport is
installed via :func:`set_transport` (tests), it is used; otherwise the real
HTTPS client runs. The three public functions interpret status codes
themselves — that interpretation (409 -> created:false, GET 404 -> None) is the
behaviour under test, not something a stub re-implements.

The bootstrap key named by ``api_key_env`` (default ``ONECLI_API_KEY``) is read
from the host env only for the real transport's Authorization header; it is
never rendered into a profile and never returned to a caller.
"""
from __future__ import annotations

import json as _json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Optional, Tuple
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

# Transport contract: (method, path, *, json=None) -> (status: int, body: dict | None)
Transport = Callable[..., Tuple[int, Any]]

_TRANSPORT: Optional[Transport] = None
_GATEWAY_API_BASE_URL: Optional[str] = None
_API_KEY_ENV: str = "ONECLI_API_KEY"


class OneCLIError(RuntimeError):
    """A OneCLI control-plane call returned an unexpected status."""


def configure(*, gateway_api_base_url: Optional[str], api_key_env: str = "ONECLI_API_KEY") -> None:
    """Store the real-transport coordinates. Makes no network call."""
    global _GATEWAY_API_BASE_URL, _API_KEY_ENV
    _GATEWAY_API_BASE_URL = gateway_api_base_url
    _API_KEY_ENV = api_key_env or "ONECLI_API_KEY"


def set_transport(fn: Optional[Transport]) -> None:
    """Install a stub transport, or ``None`` to fall back to the real client."""
    global _TRANSPORT
    _TRANSPORT = fn


def _request(method: str, path: str, *, json: Any = None) -> Tuple[int, Any]:
    fn = _TRANSPORT
    if fn is not None:
        return fn(method, path, json=json)
    return _real_request(method, path, json=json)


def _real_request(method: str, path: str, *, json: Any = None) -> Tuple[int, Any]:
    base = _GATEWAY_API_BASE_URL
    if not base:
        raise OneCLIError("gateway_api_base_url is not configured for the OneCLI client")
    url = base.rstrip("/") + path
    headers = {"Accept": "application/json"}
    api_key = os.environ.get(_API_KEY_ENV, "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data = None
    if json is not None:
        data = _json.dumps(json).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - base is operator config
            status = int(resp.getcode())
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        raw = exc.read()
    body: Any = None
    if raw:
        try:
            body = _json.loads(raw.decode("utf-8"))
        except ValueError:
            body = None
    return status, body


def ensure_agent(*, identifier: str) -> dict:
    """POST /api/agents. Idempotent: 201/200 -> created, 409 -> already exists."""
    status, _ = _request("POST", "/api/agents", json={"identifier": identifier})
    if status in (200, 201):
        return {"created": True}
    if status == 409:
        return {"created": False}
    raise OneCLIError(f"ensure_agent({identifier!r}) failed: HTTP {status}")


def set_secrets(*, identifier: str, secrets) -> Optional[dict]:
    """POST /api/agents/<identifier>/secrets with the selective provider set."""
    status, body = _request(
        "POST", f"/api/agents/{identifier}/secrets", json={"secrets": list(secrets)}
    )
    if status == 200:
        return body
    raise OneCLIError(f"set_secrets({identifier!r}) failed: HTTP {status}")


def get_container_config(*, agent: str) -> Optional[dict]:
    """GET /v1/container-config?agent=<id>. 200 -> body, 404 -> None (unprovisioned)."""
    status, body = _request(
        "GET", "/v1/container-config?" + urlencode({"agent": agent}), json=None
    )
    if status == 200:
        return body
    if status == 404:
        return None
    raise OneCLIError(f"get_container_config({agent!r}) failed: HTTP {status}")
