"""``plugins.entries.nv-delivery-redrive.settings`` (ADR D9), read through the plugin context of the profile in
whose turn or tick the code runs, plus the tracking-liveness rule (D8)."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Optional

from . import store

logger = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    "redrive_window_seconds": 14400.0,
    "max_attempts": 48,
    "retry_backoff_seconds": 300.0,
    "heartbeat_stale_seconds": 180.0,
    "orchestrator_profile": None,
    "retention_seconds": 604800.0,
}

_ctx: Any = None
_warned: set[str] = set()


def bind(ctx: Any) -> None:
    global _ctx
    _ctx = ctx


def get(key: str) -> Any:
    default = DEFAULTS[key]
    value = default
    if _ctx is not None:
        try:
            value = _ctx.get_config(key, default)
        except Exception:
            logger.debug("nv-delivery-redrive: settings read failed for %s", key, exc_info=True)
    if key == "orchestrator_profile":
        return str(value).strip() or None if value is not None else None
    try:
        return int(value) if key == "max_attempts" else float(value)
    except (TypeError, ValueError):
        return default


def orchestrator(root: Path) -> Optional[tuple[str, Path]]:
    """``(profile, home)`` of a configured orchestrator that exists in the roster, else None (tracking off)."""
    from tools.bot_mode_dm import _resolve_local_name
    from tools.bot_mode_probe import _roster

    name = get("orchestrator_profile")
    if not name:
        return None
    roster = dict(_roster(root))
    resolved = _resolve_local_name(name, list(roster))
    if resolved is None:
        if name not in _warned:
            _warned.add(name)
            logger.error("nv-delivery-redrive: orchestrator_profile %r is not a profile on this install; "
                         "tracking is off", name)
        return None
    return resolved, roster[resolved]


def heartbeat_fresh(now: Optional[float] = None) -> bool:
    beat = store.heartbeat_at()
    if beat is None:
        return False
    return ((time.time() if now is None else now) - beat) <= get("heartbeat_stale_seconds")


def tracking_live(root: Path) -> bool:
    return orchestrator(root) is not None and heartbeat_fresh()
