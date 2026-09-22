"""nv-cost-cap — dashboard backend routes (COST-F30).

Mounted at ``/api/plugins/nv-cost-cap/`` by the dashboard plugin system, BEHIND the
dashboard auth gate. This module is imported CTX-FREE — the web server loads it via
``spec_from_file_location`` and uses only its ``router`` attribute (no ``register(ctx)``,
no ``PluginContext``) — so it must not depend on ``_CTX``. It reaches the plugin's
escalation logic and store by path-loading the sibling modules (relative imports are
unavailable under ``spec_from_file_location``); both address the same per-profile SQLite
file as the package-loaded modules, so state is shared safely.

Every handler pins the request's ``?profile`` (``normalize_profile_name`` +
``validate_profile_name`` -> ``set_hermes_home_override`` token, reset in ``finally``) so
reads / authorization / CAS act on the OWNING profile, never the default. The resolve
principal is the SERVER-verified ``dashboard:<provider>:<org_id>:<user_id>`` derived from
``request.state.session`` — never the request body, never a bare user_id.

Security note: all ``/api/plugins/...`` routes pass through the dashboard's session-token
auth middleware, so an authenticated caller is guaranteed. In loopback/``--insecure`` mode
there is no per-user session, so ``principal_from_request`` returns ``None`` and a resolve
is refused unless the operator explicitly configures ``loopback_operator``.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request

log = logging.getLogger(__name__)

PLUGIN_KEY = "nv-cost-cap"
_PLUGIN_DIR = Path(__file__).resolve().parent.parent  # .../plugins/nv-cost-cap


def _load_sibling(basename: str):
    """Path-load a sibling plugin module (no package context under spec_from_file_location)."""
    name = f"nv_cost_cap_{basename}"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, _PLUGIN_DIR / f"{basename}.py")
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load nv-cost-cap sibling {basename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_escalation = _load_sibling("escalation")
_store = _escalation.store

router = APIRouter()


def _pin(profile: str):
    from hermes_cli.profiles import (
        get_profile_dir,
        normalize_profile_name,
        profile_exists,
        validate_profile_name,
    )
    from hermes_constants import set_hermes_home_override

    canon = normalize_profile_name(profile)
    validate_profile_name(canon)
    # Reject a syntactically-valid but non-existent profile with 404 BEFORE installing the home
    # override — otherwise a subsequent plugin_db/config read would materialise <profile>/plugin-data
    # dirs + a data.db for any name an authenticated caller passes (F30-REV, should-change).
    if not profile_exists(canon):
        raise HTTPException(status_code=404, detail="unknown profile")
    return canon, set_hermes_home_override(str(get_profile_dir(canon)))


@router.get("/escalations")
async def list_escalations(profile: str = Query(...)):
    """Pending escalation episodes for the target ``?profile`` (authenticated, not operator-gated)."""
    from hermes_constants import reset_hermes_home_override

    _canon, token = _pin(profile)
    try:
        return _store.pending_escalations()
    finally:
        reset_hermes_home_override(token)


@router.post("/escalations/{episode_id}/resolve")
async def resolve_escalation_route(episode_id: str, request: Request, profile: str = Query(...)):
    """Resolve one episode as the SERVER-verified operator; every effect scoped to ``?profile``."""
    from hermes_constants import reset_hermes_home_override

    principal = _escalation.principal_from_request(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    decision = body.get("decision")
    amount_usd = body.get("amount_usd")

    canon, token = _pin(profile)
    try:
        # Loopback fallback: gated/OAuth dashboards carry a per-user session; a loopback
        # dashboard does not, so principal is None here. Under the pinned profile, fall back
        # to the explicit (default-off) loopback_operator when the operator configured it.
        if principal is None:
            principal = _escalation.loopback_operator()
        episode = _store.episode_by_id(episode_id)
        if episode is None:
            return {"granted": False, "reason": "no-episode"}
        if decision == "ceiling":
            return _escalation.set_episode_ceiling(
                canon, episode["session_id"], episode_id, episode["budget_gen"], amount_usd, principal
            )
        return _escalation.resolve_escalation(
            episode["session_id"], episode_id, episode["budget_gen"], decision, principal
        )
    finally:
        reset_hermes_home_override(token)
