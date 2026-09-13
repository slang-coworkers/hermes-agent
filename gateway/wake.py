"""Wake an existing agent session from a background completion event.

Two delivery strategies, selected by the target adapter's
``supports_async_delivery`` capability flag:

* Push-capable adapters (telegram, discord, plugin platforms, ...): inject a
  synthetic ``MessageEvent(internal=True)`` through ``adapter.handle_message``
  — the pre-existing wake path, preserved exactly.

* Stateless request/response adapters (the API server,
  ``supports_async_delivery = False``): ``handle_message`` would run the wake
  turn under a ``build_session_key()``-derived key
  (``agent:main:api_server:group:<sid>``) that NEVER matches the raw
  ``X-Hermes-Session-Id`` key real gateway/HQ turns run under
  (``_bind_api_server_session``), so the wake lands in a parallel, invisible
  session. Instead we self-POST ``/v1/chat/completions`` on the in-pod API
  server with the raw session id in the ``X-Hermes-Session-Id`` header — the
  exact entry point real turns use — so the wake turn resumes the REAL
  session, with full history, and its result is visible the next time the
  client polls/reopens the conversation.

Failures RAISE (after bounded retries on transient errors) so callers can
rewind cursors / retry instead of silently losing the event.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# A wake self-post runs the entire agent turn synchronously (stream=false);
# generous ceiling so long tool-using turns aren't killed mid-flight.
WAKE_TURN_TIMEOUT_SECONDS = 600.0

# Backoff delays between retries on transient failures (429 concurrency cap,
# connection errors). The API server has no per-session lock — concurrent
# turns on one session are last-writer-wins — but it DOES enforce a global
# max_concurrent_runs cap via HTTP 429, which is worth waiting out.
_RETRY_DELAYS_SECONDS = (2.0, 5.0, 10.0)


def adapter_supports_push(adapter: Any) -> bool:
    """Whether this adapter can push a message to the user after a turn ends.

    Mirrors ``gateway.session_context.async_delivery_supported`` but reads the
    capability off the adapter class (``supports_async_delivery``) instead of
    the request-scoped contextvar — background watchers run outside any bound
    session context. Adapters that don't declare the flag are push-capable.
    """
    return bool(getattr(adapter, "supports_async_delivery", True))


async def deliver_wake(
    adapter: Any,
    *,
    text: str,
    session_id: str = "",
    source: Any = None,
    owner_profile: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> None:
    """Deliver a wake turn to the session behind ``adapter``.

    ``session_id`` is the RAW session id (the ``X-Hermes-Session-Id`` value /
    ``state.db`` key) — required for non-push adapters. ``source`` is the
    ``SessionSource`` used to build the synthetic event — required for
    push-capable adapters.

    ``owner_profile`` (keyword-only, defaulted): when set, the non-push
    self-post targets the EXISTING profile-scoped mirror
    ``POST /p/<owner_profile>/v1/chat/completions`` so the wake resumes a
    SECONDARY-profile session (whose store the middleware scopes to
    ``profiles/<owner>/state.db``); when ``None`` the unprefixed same-profile
    ``/v1/chat/completions`` is used — every existing caller omits it and is
    unchanged. ``idempotency_key`` (keyword-only, defaulted): when set, sent as
    the ``Idempotency-Key`` request header so a same-key retry is deduped by the
    api_server's in-flight/completed cache.

    Raises on failure (bad arguments, exhausted retries, HTTP error, or a
    response that does not confirm persistence via ``X-Hermes-Turn-Persisted``)
    so the caller can rewind/retry instead of treating the wake as delivered.
    """
    if adapter_supports_push(adapter):
        if source is None:
            raise ValueError(
                "deliver_wake: push-capable adapter requires a SessionSource"
            )
        from gateway.platforms.base import MessageEvent, MessageType

        synth_event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            internal=True,
        )
        await adapter.handle_message(synth_event)
        return

    if not session_id:
        raise ValueError(
            "deliver_wake: non-push adapter (supports_async_delivery=False) "
            "requires the raw session id to self-post the wake turn"
        )
    await _self_post_chat_completion(
        adapter,
        text=text,
        session_id=session_id,
        owner_profile=owner_profile,
        idempotency_key=idempotency_key,
    )


async def _self_post_chat_completion(
    adapter: Any,
    *,
    text: str,
    session_id: str,
    owner_profile: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> None:
    """POST the wake text to the in-pod API server as a normal session turn.

    Uses the adapter's own bind host/port/key (``ApiServerAdapter.__init__``).
    Session continuation via ``X-Hermes-Session-Id`` is 403-gated on
    ``API_SERVER_KEY`` being configured, so a missing key is a hard error —
    raise loudly rather than run the wake in a fresh fingerprint-derived
    session nobody is looking at.

    When ``owner_profile`` is set the request targets the profile-scoped mirror
    ``/p/<owner_profile>/v1/chat/completions`` (the middleware scopes the
    session store to that profile's ``state.db``), so a wake can resume a
    SECONDARY-profile session; otherwise the unprefixed same-profile endpoint
    is used. A 2xx alone is NOT success — the response must carry
    ``X-Hermes-Turn-Persisted: true`` (a server-generated ack that the target
    profile's final turn actually committed); a missing / malformed / ``false``
    ack RAISES so the caller does not treat an unpersisted turn as delivered.
    """
    import aiohttp

    host = str(getattr(adapter, "_host", "") or "127.0.0.1")
    if host in ("0.0.0.0", "::", "*"):
        # Wildcard bind address — connect over loopback.
        host = "127.0.0.1"
    port = int(getattr(adapter, "_port", 0) or 8642)
    api_key = str(getattr(adapter, "_api_key", "") or "")
    if not api_key:
        raise RuntimeError(
            "wake self-post requires API_SERVER_KEY: session continuation via "
            "X-Hermes-Session-Id is rejected (403) on an unauthenticated API "
            "server, so the wake cannot reach the target session"
        )

    if ":" in host and not host.startswith("["):
        host = f"[{host}]"  # bare IPv6 literal
    # Profile-scoped mirror resumes a secondary-profile session; the unprefixed
    # path (owner_profile=None) is the pre-existing same-profile behaviour.
    path = "/v1/chat/completions"
    if owner_profile:
        path = f"/p/{owner_profile}{path}"
    url = f"http://{host}:{port}{path}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-Hermes-Session-Id": session_id,
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    payload = {
        "model": str(getattr(adapter, "_model_name", "") or "hermes-agent"),
        "messages": [{"role": "user", "content": text}],
        "stream": False,
    }

    last_err: Optional[BaseException] = None
    attempts = 1 + len(_RETRY_DELAYS_SECONDS)
    for attempt in range(attempts):
        if attempt:
            await asyncio.sleep(_RETRY_DELAYS_SECONDS[attempt - 1])
        try:
            timeout = aiohttp.ClientTimeout(total=WAKE_TURN_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as http:
                async with http.post(url, json=payload, headers=headers) as resp:
                    if resp.status == 429:
                        # Global concurrency cap (max_concurrent_runs) —
                        # transient; back off and retry.
                        last_err = RuntimeError(
                            f"wake self-post got HTTP 429 (concurrency cap) "
                            f"for session {session_id}"
                        )
                        logger.warning(
                            "%s; attempt %d/%d", last_err, attempt + 1, attempts
                        )
                        continue
                    if resp.status >= 400:
                        body = (await resp.text())[:300]
                        # Non-transient (auth/validation) — fail immediately.
                        raise RuntimeError(
                            f"wake self-post failed for session {session_id}: "
                            f"HTTP {resp.status}: {body}"
                        )
                    await resp.read()
                    # A 2xx does not prove the turn committed: the handler 200s
                    # even when the session-db write failed. Require the
                    # server-generated persist ack; a missing / malformed /
                    # false value is a delivery failure (the caller must not
                    # advance its cursor). This is NOT retried inline — the turn
                    # already ran; the durable notifier re-delivers later under
                    # the same Idempotency-Key so the api_server dedups it.
                    persisted = str(
                        resp.headers.get("X-Hermes-Turn-Persisted", "")
                    ).strip().lower()
                    if persisted != "true":
                        raise RuntimeError(
                            f"wake self-post for session {session_id} did not "
                            f"confirm persistence (X-Hermes-Turn-Persisted="
                            f"{persisted!r}); treating as undelivered"
                        )
                    logger.info(
                        "wake self-post delivered for session %s (attempt %d)",
                        session_id,
                        attempt + 1,
                    )
                    return
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            last_err = exc
            logger.warning(
                "wake self-post transient failure for session %s "
                "(attempt %d/%d): %s",
                session_id,
                attempt + 1,
                attempts,
                exc,
            )
            continue
    raise RuntimeError(
        f"wake self-post gave up for session {session_id} after "
        f"{attempts} attempts: {last_err}"
    ) from last_err
