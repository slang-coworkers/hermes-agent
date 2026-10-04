"""D14: the release webhook adapter with a fail-closed guard on agent-created routes.

The release adapter merges routes from the DEFAULT home's subscriptions file at
connect and on every request, and it accepts a secret-bearing one. Such a route
would put a platform secret inside the gateway sandbox. This subclass overrides
only that load: everything else (signature checks, body caps, rate limits,
dedupe, route scripts, the 202) is the release class unchanged.
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Dict, List, Optional

from gateway.platforms.webhook import (
    _DYNAMIC_ROUTES_FILENAME,
    _INSECURE_NO_AUTH,
    WebhookAdapter,
)

logger = logging.getLogger(__name__)


def _breach(data: object, global_secret: str) -> Optional[str]:
    """The reason the subscriptions file breaches the boundary, or None."""
    if not isinstance(data, dict):
        return "subscriptions file is not a mapping"
    for name, route in data.items():
        if not isinstance(route, dict):
            return f"route {name!r} is not a mapping"
        # Effective secret as the release computes it; an empty one counts too.
        if route.get("secret", global_secret) != _INSECURE_NO_AUTH:
            return f"route {name!r} carries a signing secret"
    return None


class GuardedWebhookAdapter(WebhookAdapter):
    """``WebhookAdapter`` whose dynamic-route load refuses every route on a breach."""

    _guard_reason: Optional[str] = None

    def _reload_dynamic_routes(self) -> None:
        super()._reload_dynamic_routes()
        from hermes_constants import get_hermes_home

        path = get_hermes_home() / _DYNAMIC_ROUTES_FILENAME
        reason: Optional[str] = None
        names: List[str] = []
        if path.exists():
            # Read on every call, never mtime-gated, so a breach cannot hide
            # behind the release's own mtime check.
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data, reason = None, "subscriptions file is unreadable"
            if reason is None:
                reason = _breach(data, self._global_secret)
            if isinstance(data, dict):
                names = [str(k) for k in data]
        if reason is None:
            self._routes = {**self._dynamic_routes, **self._static_routes}
            self._set_guard(None, [])
            return
        dynamic = data if isinstance(data, dict) else {}
        refused: Dict[str, dict] = {}
        for name, route in {**dynamic, **self._static_routes}.items():
            disabled = copy.deepcopy(route) if isinstance(route, dict) else {}
            disabled["enabled"] = False
            refused[str(name)] = disabled
        self._routes = refused
        self._set_guard(reason, sorted(set(names) | set(self._static_routes)))

    def _set_guard(self, reason: Optional[str], routes: List[str]) -> None:
        if reason == self._guard_reason:
            return
        self._guard_reason = reason
        if reason:
            logger.warning("nv-ingress webhook guard: refusing every webhook route (%s); routes: %s",
                           reason, ", ".join(routes))
        else:
            logger.warning("nv-ingress webhook guard: subscriptions breach cleared; routes re-opened")
        try:
            from . import ledger

            ledger.record_guard(bool(reason), routes, reason)
        except Exception:
            logger.warning("nv-ingress webhook guard state could not be recorded", exc_info=True)
