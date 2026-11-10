"""Product-hook registry of nv-fleet-gates (API_VERSION 1).

A downstream product plugin plugs product-specific behaviour into the generic gate at a small, closed set of points.
The registry is empty here, and every point then takes its empty-registry default, which is this plugin's own
behaviour.

A product plugin declares ``requires_plugins: [nv-fleet-gates]`` and, inside its own ``register(ctx)`` once
``ctx.has_plugin("nv-fleet-gates")`` is true, does::

    import nv_fleet_gates_product_hooks as hooks
    if hooks.API_VERSION == 1:
        hooks.register_product_hook(ctx, "early_veto", fn)

Directory plugins import under a per-scope package name, so this module publishes itself under the stable name
``nv_fleet_gates_product_hooks``. The first copy imported in a process owns the registry; every later copy (another
profile's scope, a reload) delegates to it.

Rules:
- the key is (Hermes home, point), with the home resolved inside the call; one owner (``ctx.plugin_id``) per key;
- the same owner registering again replaces its callable; another owner gets ``ValueError`` and nothing is stored;
- an unknown point is ``ValueError``, a non-callable ``TypeError``;
- absence is never cached, so a late registration applies from the next call;
- unload (``ctx.on_unload``) removes the owner's registrations for its home.

A callable is called with keyword arguments only, the point's fields plus ``home``, and must accept ``**kwargs``
(fields may be added). One that raises or returns outside the point's contract is logged once per
(home, point, owner), and the point's on-error behaviour applies: the three vetoes block, ``own_post_threads``
raises so the gate blocks, the others fall back to their default path.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

API_VERSION = 1
PUBLISHED_NAME = "nv_fleet_gates_product_hooks"
VETO_POINTS = frozenset({"early_veto", "policy_veto", "delivery_veto"})
POINTS = VETO_POINTS | frozenset({"tool_result_rewrite", "room_session_kind", "own_post_threads", "forge_cli_parser"})
ROOM_KINDS = frozenset({"room", "unknown", "no"})

_lock = threading.RLock()
_hooks: dict[tuple[str, str], tuple[str, Callable[..., Any]]] = {}
_unload_armed: set[tuple[str, str]] = set()
_profiles: dict[str, tuple[str, bool]] = {}
_logged: set[tuple[str, str, str]] = set()


class ProductHookError(RuntimeError):
    """A failing ``own_post_threads`` hook; the gate turns it into a block."""


def _home_key(home: str | os.PathLike) -> str:
    return str(Path(os.fspath(home)).resolve())


def _current_home() -> str:
    return str(get_hermes_home().resolve())


def register_product_hook(ctx, point: str, fn: Callable[..., Any]) -> None:
    if point not in POINTS:
        raise ValueError(f"unknown nv-fleet-gates product-hook point {point!r}; known: {sorted(POINTS)}")
    if not callable(fn):
        raise TypeError(f"nv-fleet-gates product hook for {point!r} must be callable")
    owner = ctx.plugin_id
    home = _current_home()
    with _lock:
        held = _hooks.get((home, point))
        if held is not None and held[0] != owner:
            raise ValueError(
                f"nv-fleet-gates product-hook point {point!r} is already held by {held[0]!r} for this profile; "
                f"{owner!r} cannot register it too")
        _hooks[(home, point)] = (owner, fn)
        arm = (home, owner) not in _unload_armed
        _unload_armed.add((home, owner))
    if arm:
        ctx.on_unload(lambda: _drop(home, owner))


def unregister_product_hooks(ctx) -> int:
    return _drop(_current_home(), ctx.plugin_id)


def _drop(home: str, owner: str) -> int:
    with _lock:
        keys = [k for k, (who, _fn) in _hooks.items() if k[0] == home and who == owner]
        for key in keys:
            del _hooks[key]
        _unload_armed.discard((home, owner))
    return len(keys)


def registered(home: str | os.PathLike, point: str) -> str | None:
    with _lock:
        held = _hooks.get((_home_key(home), point))
    return held[0] if held else None


def is_orchestrator(*, home: str | os.PathLike) -> bool:
    with _lock:
        return bool(_profiles.get(_home_key(home), ("", False))[1])


def current_profile(*, home: str | os.PathLike) -> str:
    with _lock:
        return _profiles.get(_home_key(home), ("", False))[0]


def _publish_profile(ctx, *, home: str, profile: str, is_orchestrator: bool) -> None:
    """nv-fleet-gates' own register(ctx): the two facts the accessors read for ``home``, until unload."""
    with _lock:
        _profiles[home] = (profile, bool(is_orchestrator))

    def _forget() -> None:
        with _lock:
            if _profiles.get(home) == (profile, bool(is_orchestrator)):
                del _profiles[home]

    ctx.on_unload(_forget)


def _valid(point: str, value: Any) -> bool:
    if point in VETO_POINTS or point == "tool_result_rewrite":
        return value is None or isinstance(value, str)
    if point == "room_session_kind":
        return value in ROOM_KINDS
    if point == "own_post_threads":
        return isinstance(value, (set, frozenset)) and all(isinstance(v, str) for v in value)
    return value is None or isinstance(value, argparse.ArgumentParser)


def _on_error(point: str, owner: str, kwargs: dict[str, Any]) -> Any:
    if point in VETO_POINTS:
        return f"nv-fleet-gates: the {point} product hook of {owner} failed; refusing (fail closed)"
    if point == "room_session_kind":
        return "unknown" if kwargs.get("platform") == "api_server" else "no"
    if point == "own_post_threads":
        raise ProductHookError(f"the own_post_threads product hook of {owner} failed")
    return None


def call(point: str, *, home: str, default: Any, **kwargs: Any) -> Any:
    """Internal to nv-fleet-gates: the registered callable's result for (home, point), else ``default``."""
    if point not in POINTS:
        raise ValueError(f"unknown nv-fleet-gates product-hook point {point!r}")
    home = os.fspath(home)
    with _lock:
        held = _hooks.get((home, point))
    if held is None:
        return default
    owner, fn = held
    try:
        value = fn(**kwargs, home=home)
    except Exception:
        _log_once(home, point, owner, "raised", exc_info=True)
        return _on_error(point, owner, kwargs)
    if not _valid(point, value):
        _log_once(home, point, owner, f"returned {type(value).__name__}, outside the point's contract")
        return _on_error(point, owner, kwargs)
    if point == "own_post_threads":
        return frozenset(value)
    return value


def _log_once(home: str, point: str, owner: str, what: str, *, exc_info: bool = False) -> None:
    with _lock:
        if (home, point, owner) in _logged:
            return
        _logged.add((home, point, owner))
    logger.warning("nv-fleet-gates: product hook %s of %s %s (home %s)", point, owner, what, home,
                   exc_info=exc_info)


_canonical = sys.modules.setdefault(PUBLISHED_NAME, sys.modules[__name__])
if _canonical is not sys.modules[__name__]:
    for _name in ("API_VERSION", "POINTS", "VETO_POINTS", "ROOM_KINDS", "ProductHookError",
                  "register_product_hook", "unregister_product_hooks", "registered", "is_orchestrator",
                  "current_profile", "_publish_profile", "call"):
        # A copy from another plugin version may lack a name; keeping ours keeps this plugin loadable.
        globals()[_name] = getattr(_canonical, _name, globals()[_name])
