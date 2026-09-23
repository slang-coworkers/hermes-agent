"""nv-cost-cap — escalation resolution chokepoint.

ONE authz + compare-and-set + apply path (`resolve_escalation`) that every surface
funnels through: the web dashboard route, the Electron desktop half, the gateway
`/cost` command, and the orchestrator CLI. Two invariants hold across all of them:
a resolution applies its effect at most once (a durable exactly-once CAS on the
resolution row keyed by the episode), and every resolution is authorized against
structured `operators` using a SURFACE-NAMESPACED principal, never a bare or
client-supplied id.

CTX-FREE by construction: operators + the exact-ceiling write are read/written via
`load_config_readonly()` / `config.save_config` (honouring the current/overridden
HERMES_HOME), so the dashboard's `spec_from_file_location` import — which has no
`register(ctx)` / `PluginContext` — authorizes and writes identically to the
in-loop hook path.
"""

from __future__ import annotations

import contextlib
import logging
import math

logger = logging.getLogger(__name__)

PLUGIN_KEY = "nv-cost-cap"
DEFAULT_INCREMENT_USD = 5.0

# The three surfaces that can mint a principal. A well-formed operator/actor id is always
# one of these namespaces; anything else (a bare user_id, a client-supplied string) is
# rejected before the operator-set membership test, so a cross-surface id collision cannot
# authorize a click.
_PRINCIPAL_PREFIXES = ("dashboard:", "gateway:", "cli:")

# ``store`` must import BOTH as a package member (the PluginManager loads the directory
# plugin as a package, so ``from . import store`` works) AND when this module is loaded
# standalone by the ctx-free dashboard ``plugin_api.py`` via ``spec_from_file_location``
# (no parent package, so a relative import raises). Both paths address the SAME per-profile
# sqlite file, and ``store`` holds no connection/authoritative state at module scope, so a
# second module object is state-safe.
try:
    from . import store
except ImportError:  # pragma: no cover - exercised by the dashboard-import path (AC-9)
    import importlib.util as _il
    import sys as _sys
    from pathlib import Path as _P

    _STORE_NAME = "nv_cost_cap_store"
    if _STORE_NAME in _sys.modules:
        store = _sys.modules[_STORE_NAME]
    else:
        _spec = _il.spec_from_file_location(_STORE_NAME, _P(__file__).resolve().parent / "store.py")
        store = _il.module_from_spec(_spec)
        _sys.modules[_STORE_NAME] = store
        _spec.loader.exec_module(store)


# --- config (context-free) -------------------------------------------------

def _settings() -> dict:
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
        entry = cfg.get("plugins", {}).get("entries", {}).get(PLUGIN_KEY, {})
        settings = entry.get("settings") if isinstance(entry, dict) else None
        return settings if isinstance(settings, dict) else {}
    except Exception:
        logger.warning("nv-cost-cap settings read failed", exc_info=True)
        return {}


def _operators() -> set:
    ops = _settings().get("operators")
    if not isinstance(ops, list):
        return set()
    return {str(o) for o in ops if o}


def loopback_operator():
    """The configured loopback operator principal, or None (default off).

    Loopback/``--insecure`` dashboards have only a shared token and no per-user session,
    so ``principal_from_request`` returns None there. The resolve route may fall back to
    THIS explicit, default-off namespaced id — treating the single trusted local token as
    that operator — but only when the operator opts in by setting it. Fail-closed otherwise.
    """
    val = _settings().get("loopback_operator")
    return str(val) if val else None


def _increment() -> float:
    try:
        value = float(_settings().get("escalation_increment_usd"))
        if math.isfinite(value) and value > 0:
            return value
    except (TypeError, ValueError):
        pass
    return DEFAULT_INCREMENT_USD


def _authorized(principal) -> bool:
    """A resolution is authorized ONLY when the namespaced principal is a configured operator.

    Fail-closed: ``None`` (loopback / no session), a bare un-namespaced id, and a
    principal absent from ``operators`` all return False. The operator set is read under
    the CURRENT (pinned) HERMES_HOME, so authz is per-profile.
    """
    if not principal or not isinstance(principal, str):
        return False
    if not principal.startswith(_PRINCIPAL_PREFIXES):
        return False
    return principal in _operators()


def _parse_exact_usd(amount):
    """Return the EXACT positive-finite float, or None for a multiplier/invalid/≤0/non-finite arg."""
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value


# --- principal seams (never the client body) -------------------------------

def principal_from_request(request):
    """Dashboard principal: ``dashboard:<provider>:<org_id>:<user_id>`` from the SERVER session.

    Reads only ``request.state.session`` (set by the dashboard auth gate); returns None in
    loopback mode where no per-user session exists. NEVER reads the request body.
    """
    session = getattr(getattr(request, "state", None), "session", None)
    if session is None:
        return None
    try:
        return f"dashboard:{session.provider}:{session.org_id}:{session.user_id}"
    except AttributeError:
        return None


def principal_from_event(event):
    """Gateway principal: ``gateway:<platform.value>:<scope>:<user_id>`` from the event source.

    ``platform`` stringifies to its enum ``.value`` (``slack``, not ``Platform.SLACK``);
    ``scope`` is ``scope_id or guild_id or "-"`` so the same ``user_id`` in a different
    workspace is a DIFFERENT principal. Returns None when no platform user id is present.
    """
    source = getattr(event, "source", None)
    if source is None:
        return None
    platform = getattr(source, "platform", None)
    platform_value = getattr(platform, "value", platform)
    if platform_value is None:
        return None
    scope = getattr(source, "scope_id", None) or getattr(source, "guild_id", None) or "-"
    user_id = getattr(source, "user_id", None)
    if not user_id:
        return None
    return f"gateway:{platform_value}:{scope}:{user_id}"


# --- profile pin -----------------------------------------------------------

@contextlib.contextmanager
def _pinned_profile(profile):
    """Pin the owning profile's HERMES_HOME for the body (normalize + validate + token/reset)."""
    from hermes_cli.profiles import get_profile_dir, normalize_profile_name, validate_profile_name
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    canon = normalize_profile_name(profile)
    validate_profile_name(canon)
    token = set_hermes_home_override(str(get_profile_dir(canon)))
    try:
        yield canon
    finally:
        reset_hermes_home_override(token)


def _current_profile_name() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name

        return get_active_profile_name()
    except Exception:
        return "default"


# --- the resolution chokepoint ---------------------------------------------

def _finish(status, decision, episode_id, session_id, **extra):
    """Map ``store.apply_effect`` status to the surface reply dict.

    A claim can win the CAS yet still not apply — the session closed under it
    (``session-closed``), a newer budget generation superseded it
    (``stale-generation``), or a required side-effect (the ceiling write) failed and
    the row stays pending for the reconciler (``deferred``). Only ``APPLIED`` is a grant;
    every other status is a fail-closed ``granted: False`` so no surface reports success
    for an effect that did not land.

    On a granted resolution the reply carries ``resumes`` — whether the JUST-RESOLVED session actually
    runs now (``store.session_resumes`` = own-runnability AND not ``estop.is_engaged()``) — so a surface
    never claims a still-paused (foreign pause / engaged belt / unknown-pricing) session resumed. A Stop
    keeps the session blocked, so it never resumes. When a profile ESTOP belt is engaged the plugin
    LEAVES it (manual-resume release — the plugin never removes the sentinel), so the reply also carries
    ``estop_disposition='left'`` + ``manual_resume_required=True``: a granted Continue/ceiling clears the
    per-session money block, but the session becomes runnable only after an operator lifts the belt
    (``hermes resume`` / UA-28); a Stop leaves the session blocked outright.
    """
    if status == store.APPLIED:
        result = {"granted": True, "decision": decision, "episode_id": episode_id, **extra}
        # Sample the ESTOP belt ONCE and derive both resumes and the disposition from it, so a
        # concurrent pause/resume can never return contradictory fields. The plugin never lifts the
        # belt: report its disposition on EVERY granted resolution — "left" (with
        # manual_resume_required) while a sentinel is engaged, else "absent".
        belt_engaged = store.estop_engaged()
        result["resumes"] = (
            store.session_resumes(session_id, belt_engaged=belt_engaged)
            if decision in ("continue", "ceiling") else False
        )
        result["estop_disposition"] = "left" if belt_engaged else "absent"
        result["manual_resume_required"] = belt_engaged
        # Whether the per-session MONEY block actually cleared (blocked→0), independent of the belt. A
        # Stop leaves it set, and a set-ceiling AT/BELOW current spend is granted (the ceiling is written)
        # yet deliberately LEAVES blocked=1 — so a surface must not claim the block cleared there.
        result["money_block_cleared"] = not bool(store.get_state(session_id).get("blocked"))
        return result
    reason = {
        store.CANCELLED_CLOSED: "session-closed",
        store.CANCELLED_STALE: "stale-generation",
        store.DEFERRED: "deferred",
        store.NOOP: "already-resolved",
    }.get(status, "not-applied")
    return {"granted": False, "reason": reason, "episode_id": episode_id}


def resolve_escalation(session_id, episode_id, budget_gen, decision, principal, *, amount_usd=None):
    """Authorize -> immortal-guard -> CAS -> apply. The ONE path every surface funnels through.

    Returns ``{"granted": bool, "reason"?: str, ...}``. Authorization happens FIRST and
    fail-closed, so an unauthorized principal mutates nothing (no resolution row). A mortal
    Stop blocks the session; an immortal Stop is refused. Continue advances the kind's
    baseline (``day_start_total`` for ``daily``, else ``window_start_total``) by
    ``escalation_increment_usd`` and clears ``blocked``. A losing CAS is "already-resolved".
    """
    if not _authorized(principal):
        return {"granted": False, "reason": "unauthorized"}
    return _resolve_core(session_id, episode_id, budget_gen, decision, principal)


def _resolve_core(session_id, episode_id, budget_gen, decision, actor):
    """Immortal-guard -> compute absolute target -> CAS -> apply. Authorization is the CALLER's job.

    The dashboard/gateway surfaces call it through ``resolve_escalation`` (operator-list authz);
    the orchestrator CLI calls it after its own operational (orchestrator-profile) gate, so this
    core never re-checks a principal against ``operators``.
    """
    decision = (decision or "").strip().lower()
    if decision not in ("continue", "stop"):
        return {"granted": False, "reason": "bad-decision"}

    state = store.get_state(session_id)
    if decision == "stop" and state.get("immortal"):
        return {"granted": False, "reason": "immortal-continue-only"}

    if decision == "continue":
        episode = store.episode_by_id(episode_id)
        kind = episode["kind"] if episode else None
        baseline = state["day_start_total"] if kind == "daily" else state["window_start_total"]
        target = baseline + _increment()
    else:
        target = None

    if not store.claim_resolution(episode_id, session_id, budget_gen, decision, actor, target):
        return {"granted": False, "reason": "already-resolved"}
    # Capture the PRIOR blocked state so the notice can distinguish a true blocked→runnable resume
    # from a Continue on a session that was never blocked (which must not read "session resumed").
    return _finish(
        store.apply_effect(episode_id), decision, episode_id, session_id,
        was_blocked=bool(state.get("blocked")),
    )


def set_episode_ceiling(profile, session_id, episode_id, budget_gen, amount, principal):
    """Set an EXACT USD ceiling scoped to ``profile`` only, resolving the episode.

    Pins ``profile`` for the whole operation. Authorizes (against the pinned profile's
    operators), parses the exact amount (rejecting a multiplier / invalid / ≤0 / non-finite),
    then — only when the write is possible — claims + applies, so the config write and the
    resolution row stay consistent. The exact ceiling is written CONTEXT-FREE inside
    ``apply_effect`` (``store.write_profile_ceiling`` -> ``config.save_config``).
    """
    with _pinned_profile(profile):
        if not _authorized(principal):
            return {"granted": False, "reason": "unauthorized"}
        return _ceiling_after_pin(session_id, episode_id, budget_gen, amount, principal)


def _ceiling_after_pin(session_id, episode_id, budget_gen, amount, actor):
    """Immortal-guard -> parse -> managed-check -> CAS -> apply, target profile ALREADY pinned.

    Authorization is the caller's job (``set_episode_ceiling`` operator-list; the CLI its
    operational gate). An immortal session is Continue-only, so a ceiling on it is refused
    like a Stop. The managed-key check precedes the claim so a rejected write never leaves a
    permanently-pending claim that would block a later Continue/Stop. A non-``APPLIED`` status
    (a closed/superseded episode, or a failed config write left for the reconciler) is a
    fail-closed refusal, never a false ``granted``.
    """
    if store.get_state(session_id).get("immortal"):
        return {"granted": False, "reason": "immortal-continue-only"}
    value = _parse_exact_usd(amount)
    if value is None:
        return {"granted": False, "reason": "invalid-amount"}
    if store.ceiling_setting_is_managed():
        return {"granted": False, "reason": "managed"}
    if not store.claim_resolution(episode_id, session_id, budget_gen, "ceiling", actor, value):
        return {"granted": False, "reason": "already-resolved"}
    return _finish(store.apply_effect(episode_id), "ceiling", episode_id, session_id, amount_usd=value)


def resolve_via_cli(profile, session_id, episode_id, budget_gen, decision, amount, actor):
    """Orchestrator CLI entry: pin ``profile`` around the FULL authorize/lookup/CAS/apply.

    Two independent gates, both required. The caller (`hermes cost-cap resolve`) is first gated
    to the orchestrator profile (an OPERATIONAL gate in ``_cli_cost_cap``); then — because the
    requirement is that EVERY resolution is authorized against the structured ``operators`` — the
    surface-namespaced ``cli:<orchestrator-profile>:<fleet-admin-id>`` ``actor`` must itself be a
    configured operator of the TARGET profile, checked here under the pin. The operational gate is
    additive, not a substitute. Pinning the target profile for the whole operation is what makes a
    mis-targeted resolution impossible, mirroring the dashboard route's pin.
    """
    decision = (decision or "").strip().lower()
    with _pinned_profile(profile):
        if not _authorized(actor):
            return {"granted": False, "reason": "unauthorized"}
        if decision == "set-ceiling":
            return _ceiling_after_pin(session_id, episode_id, budget_gen, amount, actor)
        return _resolve_core(session_id, episode_id, budget_gen, decision, actor)


def handle_cost_command(session_id, text, principal, *, profile=None):
    """The gateway ``/cost continue|stop|ceiling <exact-usd>`` seam.

    Resolves the session's latest pending episode: continue/stop through
    ``resolve_escalation``, ceiling through ``set_episode_ceiling``. When ``profile`` is
    given (the gateway passes ``event.source.profile``) the whole lookup+resolve runs pinned
    to it; otherwise it inherits the ambient ``_profile_runtime_scope`` the gateway set.
    """
    def _run():
        tokens = (text or "").split()
        if len(tokens) < 2 or tokens[0].lstrip("/").lower() != "cost":
            return {"granted": False, "reason": "bad-command"}
        sub = tokens[1].lower()
        # Prefer the pending episode; fall back to the latest terminal one so a repeat command
        # after a resolution reports already-resolved (via the CAS) rather than no-episode.
        episode = store.latest_pending_episode(session_id) or store.latest_episode(session_id)
        if episode is None:
            return {"granted": False, "reason": "no-episode"}
        if sub in ("continue", "stop"):
            return resolve_escalation(session_id, episode["episode_id"], episode["budget_gen"], sub, principal)
        if sub == "ceiling":
            if len(tokens) < 3:
                return {"granted": False, "reason": "invalid-amount"}
            target_profile = profile if profile is not None else _current_profile_name()
            return set_episode_ceiling(
                target_profile, session_id, episode["episode_id"], episode["budget_gen"], tokens[2], principal
            )
        return {"granted": False, "reason": "bad-command"}

    if profile:
        with _pinned_profile(profile):
            return _run()
    return _run()


def reconcile_once() -> int:
    """Make mid-decision episodes consistent across ALL profiles; return the action count.

    Iterates the known profiles (``list_profiles`` enumerates from the default root, immune
    to any ambient override) and pins each so it never inherits the caller's home: re-applies
    claimed-but-unapplied grants (idempotent — the DB effect is absolute) and finalises
    closed-but-unresolved episodes. Safe to run repeatedly.
    """
    from hermes_cli.profiles import get_profile_dir, list_profiles
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    acted = 0
    try:
        profiles = list_profiles()
    except Exception:
        logger.warning("nv-cost-cap reconcile: list_profiles failed", exc_info=True)
        return acted
    for info in profiles:
        name = getattr(info, "name", None)
        if not name:
            continue
        token = set_hermes_home_override(str(get_profile_dir(name)))
        try:
            for row in store.claimed_unapplied():
                try:
                    # Count a real terminal transition — applied OR cancelled (closed/stale).
                    # A DEFERRED (retry next pass) or a NOOP (a racing caller already handled it)
                    # is not an action taken.
                    if store.apply_effect(row["episode_id"]) in (
                        store.APPLIED, store.CANCELLED_CLOSED, store.CANCELLED_STALE,
                    ):
                        acted += 1
                except Exception:
                    logger.warning("nv-cost-cap reconcile apply failed for %s", row.get("episode_id"), exc_info=True)
            try:
                acted += store.finalise_closed_unresolved()
            except Exception:
                logger.warning("nv-cost-cap reconcile finalise failed", exc_info=True)
            # Manual-resume release: the plugin NEVER lifts a profile ESTOP belt, not even for an
            # owned sentinel whose sole blocker closed unresolved. The belt is left for an operator
            # (`hermes resume`) or the UA-28 upstream owner-scoped disengage; the reconciler only
            # finalises the money-side episode state.
        finally:
            reset_hermes_home_override(token)
    return acted
