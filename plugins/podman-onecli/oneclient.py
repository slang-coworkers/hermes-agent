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
import re
import urllib.error
import urllib.request
from typing import Any, Callable, Optional, Tuple
from urllib.parse import urlencode, urlsplit

from agent.secret_sources.base import get_source_environment

logger = logging.getLogger(__name__)

# The identifier becomes a OneCLI agent name and a URL path segment. cli.py
# already validates it, but the client re-checks (defense in depth) so no
# unvalidated value can reach a request path or JSON body from any caller.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# Transport contract: (method, path, *, json=None) -> (status: int, body: dict | None)
Transport = Callable[..., Tuple[int, Any]]

_TRANSPORT: Optional[Transport] = None
_GATEWAY_API_BASE_URL: Optional[str] = None
_API_KEY_ENV: str = "ONECLI_API_KEY"
# Exact `host:port` origins allowed to use http:// WHEN no bootstrap key is set
# (FLEET-F62). Empty by default, so the TLS requirement is unchanged unless an
# operator opts a specific keyless endpoint in.
_INSECURE_NO_AUTH_ORIGINS: Tuple[str, ...] = ()


class OneCLIError(RuntimeError):
    """A OneCLI control-plane call returned an unexpected status."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse HTTP redirects. urllib copies the Authorization header onto the
    redirect target, so a redirecting (or malicious) control plane could
    exfiltrate the bootstrap bearer key to another origin. The OneCLI API does
    not redirect, so any redirect is treated as an error rather than followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise urllib.error.HTTPError(
            req.full_url, code,
            "OneCLI: refusing HTTP redirect (would forward the bootstrap key to another origin)",
            headers, fp,
        )


_OPENER = urllib.request.build_opener(_NoRedirect())


def configure(
    *,
    gateway_api_base_url: Optional[str],
    api_key_env: str = "ONECLI_API_KEY",
    insecure_no_auth_origins: Optional[list] = None,
) -> None:
    """Store the real-transport coordinates. Makes no network call."""
    global _GATEWAY_API_BASE_URL, _API_KEY_ENV, _INSECURE_NO_AUTH_ORIGINS
    _GATEWAY_API_BASE_URL = gateway_api_base_url
    _API_KEY_ENV = api_key_env or "ONECLI_API_KEY"
    _INSECURE_NO_AUTH_ORIGINS = tuple(insecure_no_auth_origins or ())


def set_transport(fn: Optional[Transport]) -> None:
    """Install a stub transport, or ``None`` to fall back to the real client."""
    global _TRANSPORT
    _TRANSPORT = fn


def _validate_identifier(identifier: str) -> str:
    # fullmatch, not match: `match(...$)` accepts a trailing newline, so a value
    # like "agent\n" would pass and reach a request path/body.
    if not isinstance(identifier, str) or _IDENTIFIER_RE.fullmatch(identifier) is None:
        raise OneCLIError(f"unsafe OneCLI identifier {identifier!r}")
    return identifier


def _require_secure_base(base: str) -> None:
    """The bootstrap key rides an Authorization: Bearer header, so the base must
    be TLS. https:// is always allowed and a loopback host may use http://
    (local-dev gateway). FLEET-F62: an operator-blessed EXACT ``host:port`` origin
    may also use http:// WHEN no bootstrap key is set — there is then nothing
    secret in the request — for the unauthenticated OneCLI bridge control plane.
    Exact-origin, not host-only, is deliberate: ``get_container_config`` returns the
    aoc_ proxy token as userinfo, so a host-only allowance would leak it on any
    other port of the same host; a base carrying userinfo is refused outright."""
    parts = urlsplit(base)
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    # Userinfo in the base is refused outright, before any allow branch: a
    # user:pass@ authority carries a cleartext secret regardless of scheme.
    if parts.username is not None or parts.password is not None:
        raise OneCLIError("gateway_api_base_url must not carry userinfo (user:pass@)")
    if scheme == "https":
        return
    if scheme == "http" and host in _LOOPBACK_HOSTS:
        return
    if scheme == "http" and host and not _api_key():
        origin = f"{host}:{parts.port}" if parts.port is not None else host
        if origin in {o.strip().lower() for o in _INSECURE_NO_AUTH_ORIGINS}:
            return
    raise OneCLIError(
        "gateway_api_base_url must be https:// (http:// is allowed only for a "
        f"loopback host, or an allowlisted keyless origin); refusing to send the "
        f"OneCLI bootstrap key over {scheme or '?'}://{host or '?'}"
    )


def _api_key() -> str:
    """Read the bootstrap key from the per-fetch environment view (falling back
    to os.environ), so a profile-local key is honored over another profile's
    process value (agent/secret_sources/base.py:58-70)."""
    return get_source_environment().get(_API_KEY_ENV, "")


def _request(method: str, path: str, *, json: Any = None) -> Tuple[int, Any]:
    fn = _TRANSPORT
    if fn is not None:
        return fn(method, path, json=json)
    return _real_request(method, path, json=json)


def _real_request(method: str, path: str, *, json: Any = None) -> Tuple[int, Any]:
    base = _GATEWAY_API_BASE_URL
    if not base:
        raise OneCLIError("gateway_api_base_url is not configured for the OneCLI client")
    _require_secure_base(base)
    url = base.rstrip("/") + path
    headers = {"Accept": "application/json"}
    api_key = _api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data = None
    if json is not None:
        data = _json.dumps(json).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=30) as resp:  # noqa: S310 - base is https-checked operator config
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
    _validate_identifier(identifier)
    status, _ = _request("POST", "/api/agents", json={"identifier": identifier})
    if status in (200, 201):
        return {"created": True}
    if status == 409:
        return {"created": False}
    raise OneCLIError(f"ensure_agent({identifier!r}) failed: HTTP {status}")


def _resolve_agent_uuid(identifier: str) -> str:
    """Map a OneCLI agent identifier to its server-assigned UUID via GET /api/agents.
    The secrets endpoint is keyed by that UUID, not the identifier."""
    status, body = _request("GET", "/api/agents", json=None)
    if status != 200 or not isinstance(body, list):
        raise OneCLIError(
            f"resolve agent {identifier!r}: GET /api/agents returned HTTP {status}"
        )
    for agent in body:
        if isinstance(agent, dict) and agent.get("identifier") == identifier:
            uuid = agent.get("id")
            if isinstance(uuid, str) and uuid:
                # The resolved id flows into a request path, so re-validate its grammar.
                return _validate_identifier(uuid)
    raise OneCLIError(
        f"resolve agent {identifier!r}: no matching agent id in GET /api/agents"
    )


def set_secrets(*, identifier: str, secrets) -> Optional[dict]:
    """Replace a profile's agent secret grants with exactly ``secrets`` (OneCLI
    secret ids); an empty list revokes all. The secrets endpoint is keyed by the
    server-assigned agent UUID, so resolve identifier->uuid first, then
    PUT /api/agents/<uuid>/secrets {"secretIds":[...]}."""
    _validate_identifier(identifier)
    uuid = _resolve_agent_uuid(identifier)
    status, body = _request(
        "PUT", f"/api/agents/{uuid}/secrets", json={"secretIds": list(secrets)}
    )
    if status == 200:
        return body
    raise OneCLIError(f"set_secrets({identifier!r}) failed: HTTP {status}")


def get_container_config(*, agent: str) -> Optional[dict]:
    """GET /v1/container-config?agent=<id>. 200 -> body, 404 -> None (unprovisioned)."""
    _validate_identifier(agent)
    status, body = _request(
        "GET", "/v1/container-config?" + urlencode({"agent": agent}), json=None
    )
    if status == 200:
        return body
    if status == 404:
        return None
    raise OneCLIError(f"get_container_config({agent!r}) failed: HTTP {status}")
