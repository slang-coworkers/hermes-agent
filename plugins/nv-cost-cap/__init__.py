"""nv-cost-cap — two-tier per-session cost cap with immortal sessions + runtime policy (COST-F29).

Accrual has two sources, kept monotonic (``effective = max(previous, reconciled)``):
  * fast path — ``post_api_request`` prices ``usage`` via ``estimate_usage_cost``
    and accumulates per-session, idempotent by ``api_request_id``;
  * authoritative — a recursive ``parent_session_id`` lineage read of the
    READ-ONLY core ``state.db`` (subagent + aux + codex inclusive), run on
    ``post_tool_call`` / ``on_session_end`` / ``pre_gateway_dispatch``.

Two tiers, evaluated on the WINDOW DELTA (never the lifetime total):
  * Tier-1 ``capUsd`` = the profile's rolling p90 over completed session totals;
    a crossing (under Tier-2) records an escalation episode — never a hard stop.
  * Tier-2 ``ceilingUsd`` resolved: per-profile override -> managed-scope fleet
    ceiling -> owner-pinned fleet default -> plugin default.

A non-immortal session crossing Tier-2 within a fire is stopped WITHOUT raising:
the ``llm_execution`` middleware returns a synthetic terminal response,
``pre_tool_call`` blocks, and ``pre_gateway_dispatch`` skips ONLY that session
(recovery slash-commands exempt), plus the profile ESTOP belt when
``settings.estop_on_breach`` (default true) so cron/kanban are gated too.
Immortal sessions cap per UTC day and are never hard-stopped. Unknown pricing is
fail-CLOSED. Runtime policy is set through the orchestrator-only ``hermes
cost-cap`` CLI; no env var is a source of truth.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
from datetime import datetime, timezone
from types import SimpleNamespace

from . import policy, store
from .escalation import (  # noqa: F401  (re-exported as the plugin's public COST-F30 API)
    handle_cost_command,
    principal_from_event,
    principal_from_request,
    reconcile_once,
    resolve_escalation,
    resolve_via_cli,
    set_episode_ceiling,
)

logger = logging.getLogger(__name__)

PLUGIN_KEY = "nv-cost-cap"
DEFAULT_P90_WINDOW = 50
FLEET_CEILING_KEY = f"plugins.entries.{PLUGIN_KEY}.settings.fleet_ceiling_usd"

# Set at register(); each profile home loads a DISTINCT module object with its
# own _CTX (the plugin loader namespaces directory modules by HERMES_HOME), so
# this module-global is profile-safe rather than a cross-profile shared cell.
_CTX = None


class ManagedPinnedError(Exception):
    """Raised when a fleet-ceiling write targets a managed-pinned key."""


# --- config + small utilities ---------------------------------------------

def _finite(value) -> bool:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return False
    return not (math.isnan(f) or math.isinf(f))


def _cfg(key, default=None):
    if _CTX is None:
        return default
    try:
        value = _CTX.get_config(key, default)
    except Exception:
        logger.warning("nv-cost-cap config read failed for %r", key, exc_info=True)
        return default
    return default if value is None else value


def _cfg_int(key, default) -> int:
    try:
        return int(_cfg(key, default))
    except (TypeError, ValueError):
        return int(default)


def _cfg_bool(key, default: bool) -> bool:
    value = _cfg(key, default)
    if value is None:
        return default
    return bool(value)


def _cfg_list(key):
    value = _cfg(key, [])
    return value if isinstance(value, list) else []


def _today(kwargs) -> str:
    """The window day — an injected ``_today`` (tests) or today's UTC date."""
    injected = kwargs.get("_today")
    if isinstance(injected, str) and injected:
        return injected
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _state_db_path() -> str:
    from hermes_constants import get_hermes_home

    return str(get_hermes_home() / "state.db")


def _current_profile_home() -> str:
    from hermes_constants import get_hermes_home

    return str(get_hermes_home())


def _platform_value(platform):
    # event.source.platform is a Platform enum whose .value is the string form
    # ("slack", "cli", ...); config lists platforms by that string.
    value = getattr(platform, "value", platform)
    return str(value) if value is not None else None


def _resolve_immortal(session_id, platform=None) -> bool:
    """Recompute immortality against the CURRENT config on every call.

    ``pre_tool_call`` / ``llm_execution`` receive no ``platform``, so the
    session's platform is persisted by an earlier callback that DID carry it
    (``on_session_start`` / ``pre_gateway_dispatch`` / ``on_session_end``) and
    reused here. Membership is re-derived from live config each call — never a
    sticky cached flag — so removing a session id or platform from the config
    takes effect immediately (the cached ``immortal`` value is refreshed in both
    directions). Session-id immortality needs no platform.
    """
    try:
        state = store.get_state(session_id)
    except Exception:
        state = None
    stored_platform = state["platform"] if state else None
    effective_platform = _platform_value(platform) or stored_platform

    immortal = isinstance(session_id, str) and session_id in _cfg_list("immortal_session_ids")
    if not immortal and effective_platform is not None:
        immortal = effective_platform in {str(p) for p in _cfg_list("immortal_platforms")}

    # Persist the seen platform and refresh the cached classification (both ways).
    try:
        updates = {}
        if _platform_value(platform) is not None and _platform_value(platform) != stored_platform:
            updates["platform"] = _platform_value(platform)
        if state is None or state["immortal"] != int(immortal):
            updates["immortal"] = int(immortal)
        if updates:
            store.set_state(session_id, **updates)
    except Exception:
        logger.warning("nv-cost-cap immortal persist failed for %s", session_id, exc_info=True)
    return immortal


def _refresh_effective(session_id) -> float:
    """Reconcile from state.db and persist the monotone effective spend.

    Never raises: a reconcile failure keeps the previously-persisted effective.
    Also persists the authoritative-path unpriced marker — a lineage row core
    stamped ``cost_status='unknown'`` sums to 0.0, so the numeric total is
    fail-OPEN without it (the fast-path ``unpriced_count`` never sees a
    reconcile-only row). Idempotent: only lifts the marker 0 -> 1.
    """
    try:
        reconciled = policy.reconcile_session(session_id, state_db_path=_state_db_path())
    except Exception:
        logger.warning("nv-cost-cap reconcile failed for %s", session_id, exc_info=True)
        return float(store.get_state(session_id)["effective_usd"])
    try:
        if (policy.lineage_unknown(session_id, state_db_path=_state_db_path())
                and not store.get_state(session_id)["recon_unpriced"]):
            store.set_state(session_id, recon_unpriced=1)
    except Exception:
        logger.warning("nv-cost-cap unknown-pricing scan failed for %s", session_id, exc_info=True)
    return store.bump_effective(session_id, reconciled)


def _unpriced_signal(session_id) -> int:
    """Distinct unpriced signals across BOTH accrual paths.

    The fast-path count of ``priced=0`` calls plus the authoritative reconcile
    marker (a lineage row core stamped ``cost_status='unknown'``, which sums to
    0.0 and is otherwise invisible). Fed to the same fire-open / mid-fire-stop
    logic as the fast-path count so a reconcile-only unknown is fail-CLOSED too.
    """
    try:
        count = store.unpriced_count(session_id)
    except Exception:
        count = 0
    try:
        if store.get_state(session_id)["recon_unpriced"]:
            count += 1
    except Exception:
        pass
    return count


# --- public policy helpers (bound by the acceptance-test contract) ---------

def resolve_ceiling() -> float:
    """Tier-2 ceiling for the ACTIVE profile, read live on every call.

    Precedence: per-profile override -> managed-scope fleet ceiling -> owner-pinned fleet
    default -> plugin default. No ``HERMES_*`` env var. Delegates to the single ctx-free
    resolver in ``store`` so the enforcement path and the panel display never disagree.
    """
    return store.resolved_ceiling()


def set_fleet_ceiling(value: float) -> None:
    """Write the owner-pinned fleet default; refuse a managed-pinned key or a non-finite value."""
    if not _finite(value):
        raise ValueError("fleet ceiling must be a finite number")
    from hermes_cli import managed_scope

    if managed_scope.is_key_managed(FLEET_CEILING_KEY):
        raise ManagedPinnedError(
            "fleet ceiling is pinned by managed scope; edit the managed config"
        )
    store.set_fleet_default(float(value))


def tier1_cap(profile_home) -> float:
    """Rolling p90 over the profile's completed session totals (inf when none)."""
    window = _cfg_int("p90_window_sessions", DEFAULT_P90_WINDOW)
    return policy.p90(policy.completed_session_totals(profile_home, window))


def reconcile_session(session_id, *, state_db_path) -> float:
    return policy.reconcile_session(session_id, state_db_path=state_db_path)


def session_spend(session_id) -> float:
    return float(store.get_state(session_id)["effective_usd"])


def record_api_request(session_id, model, usage, api_request_id, *, provider=None, base_url=None) -> float:
    """Fast-path accrual: price ``usage`` and accumulate, idempotent by id.

    Builds ``CanonicalUsage`` from the SELECTED canonical token fields — never
    splats the whole dict, whose ``prompt_tokens``/``total_tokens`` are derived
    properties, not constructor args. Unknown pricing is fail-CLOSED: the call
    is recorded ``unpriced`` (never as free), which the middleware /
    ``pre_tool_call`` treat as a Tier-2 breach for a non-immortal session.
    Returns the running priced fast-path total.
    """
    from agent.usage_pricing import CanonicalUsage, estimate_usage_cost

    usage = usage or {}
    canonical = CanonicalUsage(
        input_tokens=int(usage.get("input_tokens", 0) or 0),
        output_tokens=int(usage.get("output_tokens", 0) or 0),
        cache_read_tokens=int(usage.get("cache_read_tokens", 0) or 0),
        cache_write_tokens=int(usage.get("cache_write_tokens", 0) or 0),
        reasoning_tokens=int(usage.get("reasoning_tokens", 0) or 0),
        request_count=int(usage.get("request_count", 1) or 1),
    )
    amount = None
    try:
        result = estimate_usage_cost(model, canonical, provider=provider, base_url=base_url)
        amount = result.amount_usd
    except Exception:
        logger.warning("nv-cost-cap pricing failed for model=%s", model, exc_info=True)
        amount = None

    priced = amount is not None
    total = store.record_call(
        session_id, api_request_id, float(amount) if priced else 0.0, priced
    )
    store.bump_effective(session_id, total)
    return total


def episodes(session_id):
    return store.episodes(session_id)


# --- enforcement decision --------------------------------------------------

def _session_stopped(session_id, *, today, platform=None):
    """Mid-fire decision for ``pre_tool_call`` / ``llm_execution``.

    Reconciles first (so a caller that seeded spend only in state.db is seen),
    then returns True for a non-immortal session that is blocked, unpriced, or
    whose current-fire delta has crossed the ceiling. Immortal sessions are
    never stopped.
    """
    if _resolve_immortal(session_id, platform):
        _refresh_effective(session_id)
        return False, "immortal"
    effective = _refresh_effective(session_id)
    state = store.get_state(session_id)
    if state["blocked"]:
        return True, "blocked"
    if _unpriced_signal(session_id) > 0:
        return True, "unpriced"
    ceiling = resolve_ceiling()
    delta = effective - state["window_start_total"]
    if _finite(ceiling) and delta >= ceiling:
        return True, "over_ceiling"
    return False, "under"


def _engage_estop(session_id) -> None:
    # Publish the profile-local ESTOP belt through the OWNED engage seam: an atomic no-replace
    # os.link that leaves any pre-existing sentinel (an operator pause, a concurrent engager)
    # byte-for-byte and records a receipt only for a sentinel this plugin itself published, so a
    # later authorized clear can tell its own stop from an operator pause. Engage is scoped to the
    # SERVING profile home; when that profile IS `default` the home is the fleet root, so the belt
    # is fleet-wide there (docs note this; estop_on_breach: false opts out).
    store.engage_owned(session_id)


def _record_escalation(session_id, budget_gen, today) -> None:
    store.record_episode(
        session_id, "escalation", budget_gen, today,
        dedup_key=f"{session_id}:esc:{budget_gen}",
    )


def _record_breach(session_id, budget_gen, today, *, unpriced) -> None:
    kind = "unknown_pricing" if unpriced else "breach"
    store.record_episode(
        session_id, kind, budget_gen, today,
        dedup_key=f"{session_id}:{kind}:{budget_gen}",
    )


def _evaluate_daily(session_id, *, today) -> None:
    """Immortal per-day windowing: record the crossing once per (session, day).

    First sight starts the day from 0; a new day rolls the baseline to the
    carried lifetime total, so an unchanged total on a new day records nothing
    until fresh same-day spend crosses. Never engages ESTOP / skip / block.
    """
    effective = _refresh_effective(session_id)
    state = store.get_state(session_id)
    day_key = state["day_key"]
    day_start = state["day_start_total"]
    budget_gen = state["budget_gen"]
    if day_key is None:
        day_key, day_start = today, 0.0
    elif day_key != today:
        # New day: the baseline is the total carried INTO the day (the previous
        # day's ending total), not the current — possibly already-increased —
        # effective, so spend on the new day's first boundary still counts. Advance the
        # generation too, so a PRIOR day's unresolved daily episode becomes stale — a
        # late Continue on it must not grant this day's baseline fresh headroom.
        day_key, day_start = today, state["last_evaluated_total"]
        budget_gen += 1

    unpriced_now = _unpriced_signal(session_id)
    if unpriced_now > state["last_unpriced_count"]:
        store.record_episode(
            session_id, "unknown_pricing", budget_gen, today,
            dedup_key=f"{session_id}:unpriced:{today}",
        )

    ceiling = resolve_ceiling()
    if _finite(ceiling) and (effective - day_start) >= ceiling:
        store.record_episode(
            session_id, "daily", budget_gen, today,
            dedup_key=f"{session_id}:daily:{today}",
        )

    store.set_state(
        session_id, day_key=day_key, day_start_total=day_start, budget_gen=budget_gen,
        last_evaluated_total=effective, last_unpriced_count=unpriced_now,
    )


def _evaluate_boundary(session_id, *, today, platform) -> bool:
    """Fire-boundary evaluation for a non-immortal session.

    Returns True when the just-ended fire crossed the ceiling (the gateway
    caller then skips the current inbound). A boundary at unchanged spend is a
    no-op — this prevents the pre_gateway_dispatch + on_session_end pair on ONE
    real gateway turn from double-counting a generation. Never raises.
    """
    if _resolve_immortal(session_id, platform):
        _evaluate_daily(session_id, today=today)
        return False

    effective = _refresh_effective(session_id)
    state = store.get_state(session_id)
    if state["blocked"]:
        return True  # still blocked from a prior fire; record nothing new

    unpriced_now = _unpriced_signal(session_id)
    spend_increased = effective > state["last_evaluated_total"] + 1e-12
    unpriced_new = unpriced_now > state["last_unpriced_count"]
    if not (spend_increased or unpriced_new):
        return False

    ceiling = resolve_ceiling()
    cap = tier1_cap(_current_profile_home())
    budget_gen = state["budget_gen"] + 1
    delta = effective - state["window_start_total"]
    crossed = unpriced_new or (_finite(ceiling) and delta >= ceiling)

    if crossed:
        _record_breach(session_id, budget_gen, today, unpriced=unpriced_new)
        store.set_state(
            session_id, blocked=1, budget_gen=budget_gen,
            last_evaluated_total=effective, last_unpriced_count=unpriced_now,
        )
        if _cfg_bool("estop_on_breach", True):
            _engage_estop(session_id)
        return True

    if _finite(cap) and effective > cap:
        _record_escalation(session_id, budget_gen, today)
    store.set_state(
        session_id, budget_gen=budget_gen, window_start_total=effective,
        last_evaluated_total=effective, last_unpriced_count=unpriced_now,
    )
    return False


# --- hook callbacks --------------------------------------------------------

def _post_api_request(session_id=None, model=None, usage=None, api_request_id=None,
                      provider=None, base_url=None, **kwargs):
    try:
        if not session_id or not api_request_id:
            return None
        return record_api_request(
            session_id, model, usage, api_request_id, provider=provider, base_url=base_url
        )
    except Exception:
        logger.warning("nv-cost-cap post_api_request failed", exc_info=True)
        return None


def _post_tool_call(session_id=None, **kwargs):
    try:
        if session_id:
            _refresh_effective(session_id)
    except Exception:
        logger.warning("nv-cost-cap post_tool_call failed", exc_info=True)
    return None


def _on_session_start(session_id=None, **kwargs):
    try:
        if session_id and not store.session_exists(session_id):
            store.set_state(
                session_id, window_start_total=0.0, last_evaluated_total=0.0,
                budget_gen=0, effective_usd=0.0,
            )
        if session_id:
            # Persist the immortal classification here (this callback carries the
            # platform) so the platform-less pre_tool_call / llm_execution see it.
            _resolve_immortal(session_id, kwargs.get("platform"))
    except Exception:
        logger.warning("nv-cost-cap on_session_start failed", exc_info=True)
    return None


def _on_session_end(session_id=None, **kwargs):
    try:
        if session_id:
            _evaluate_boundary(
                session_id, today=_today(kwargs), platform=kwargs.get("platform")
            )
    except Exception:
        logger.warning("nv-cost-cap on_session_end failed", exc_info=True)
    # COST-F30: a one-shot reconcile so a grant claimed but not applied (a resolver crash),
    # or an episode whose session closed mid-decision, is made consistent on the next turn
    # of any surface. Best-effort and idempotent; a no-op when nothing is pending.
    try:
        reconcile_once()
    except Exception:
        logger.warning("nv-cost-cap on_session_end reconcile failed", exc_info=True)
    return None


def _on_session_finalize(session_id=None, profile=None, **kwargs):
    """COST-F30: a session is truly closed — mark ``ended_at`` so a later resolve no-ops.

    The core hook (``gateway/run.py`` finalize, ``cli.py`` /new, idle expiry) carries no
    owning profile and runs off-loop under a copied context, so the ambient HERMES_HOME is
    NOT reliably the session's own. Marking under the wrong home would leave ``ended_at``
    unset in the owning DB and let a post-close resolve wrongly succeed. So when no ``profile``
    is given, mark across every profile: ``mark_session_closed`` is a bare UPDATE, a no-op in
    every DB except the one that actually holds this session's row. An explicit ``profile``
    (a direct caller / test) pins just that one. Marking sets ``ended_at`` only — the
    reconciler is the sole actor that finalises the open episode.
    """
    try:
        if not session_id:
            return None
        if profile:
            with _pinned_profile_home(profile):
                store.mark_session_closed(session_id)
            return None
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        from hermes_cli.profiles import get_profile_dir, list_profiles

        try:
            profiles = list_profiles()
        except Exception:
            logger.warning("nv-cost-cap finalize: list_profiles failed", exc_info=True)
            store.mark_session_closed(session_id)
            return None
        for info in profiles:
            name = getattr(info, "name", None)
            if not name:
                continue
            token = set_hermes_home_override(str(get_profile_dir(name)))
            try:
                store.mark_session_closed(session_id)
            except Exception:
                logger.warning("nv-cost-cap finalize mark failed for %s", name, exc_info=True)
            finally:
                reset_hermes_home_override(token)
    except Exception:
        logger.warning("nv-cost-cap on_session_finalize failed", exc_info=True)
    return None


@contextlib.contextmanager
def _pinned_profile_home(profile):
    """Pin ``profile``'s HERMES_HOME for the body (normalize + validate + token/reset)."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_cli.profiles import get_profile_dir, normalize_profile_name, validate_profile_name

    canon = normalize_profile_name(profile)
    validate_profile_name(canon)
    token = set_hermes_home_override(str(get_profile_dir(canon)))
    try:
        yield canon
    finally:
        reset_hermes_home_override(token)


def _pre_tool_call(session_id=None, **kwargs):
    try:
        if not session_id:
            return None
        stopped, _reason = _session_stopped(
            session_id, today=_today(kwargs), platform=kwargs.get("platform")
        )
        if stopped:
            return {
                "action": "block",
                "message": (
                    "nv-cost-cap: this session has reached its Tier-2 cost ceiling; "
                    "spending is paused. Start a new session (/new) or ask an operator "
                    "to raise the ceiling."
                ),
            }
        return None
    except Exception:
        logger.warning("nv-cost-cap pre_tool_call failed", exc_info=True)
        return None


def _resolve_recovery_command(event):
    """Return a recognized recovery/steering command name, or None.

    Only commands accepted by the built-in registry are exempt — an unknown
    ``/foo`` must NOT bypass the cap. Fails toward enforcement on any error.
    """
    try:
        get_command = getattr(event, "get_command", None)
        if callable(get_command):
            command = get_command()
        else:
            text = str(getattr(event, "text", "") or "").lstrip()
            token = text.split(maxsplit=1)[0] if text.startswith("/") else ""
            command = token[1:].split("@", 1)[0] if token else None
        if command:
            from hermes_cli.commands import resolve_command

            if resolve_command(command) is not None:
                return command
    except Exception:
        return None
    return None


def _resolve_session_id(event, gateway, session_store):
    try:
        key = gateway._session_key_for_source(event.source)
        store_obj = session_store if session_store is not None else getattr(gateway, "session_store", None)
        if store_obj is None:
            return None
        return store_obj.peek_session_id(key)
    except Exception:
        logger.warning("nv-cost-cap session-id resolution failed", exc_info=True)
        return None


def _event_platform(event):
    return getattr(getattr(event, "source", None), "platform", None)


def _cost_command_text(event):
    """Return a normalized ``/cost <args>`` string when this event is a ``/cost`` command, else None.

    Prefer the adapter's own command parser (``get_command``/``get_command_args`` on
    ``MessageEvent``): it strips a ``/cost@botname`` suffix, rejects a ``/path/like`` token,
    and repairs iOS em-dash-corrected flags — none of which raw ``text`` splitting handles.
    Fall back to raw splitting only for a minimal event object that lacks those methods.
    """
    get_command = getattr(event, "get_command", None)
    if callable(get_command):
        try:
            if get_command() != "cost":
                return None
            get_args = getattr(event, "get_command_args", None)
            args = (get_args() if callable(get_args) else "") or ""
            return f"/cost {args}".rstrip()
        except Exception:
            return None
    text = str(getattr(event, "text", "") or "").strip()
    if not text:
        return None
    first = text.split()[0].lstrip("/").lower()
    return text if first == "cost" else None


_COST_OUTCOME_MESSAGES = {
    "unauthorized": "⚠️ Cost cap: you are not an authorized operator for this profile.",
    "already-resolved": "Cost cap: this escalation was already resolved.",
    "no-episode": "Cost cap: no pending cost escalation for this session.",
    "immortal-continue-only": "Cost cap: this session is continue-only; Stop and set-ceiling are unavailable.",
    "invalid-amount": "Cost cap: not a valid exact USD amount. Use `/cost ceiling <number>`.",
    "managed": "Cost cap: the ceiling is administrator-managed and cannot be changed here.",
    "stale-generation": "Cost cap: that escalation is no longer current; a newer cost event superseded it.",
    "session-closed": "Cost cap: that session has closed; nothing to resolve.",
    "deferred": "Cost cap: the resolution is being retried; check again shortly.",
    "bad-command": "Cost cap: usage — `/cost continue | stop | ceiling <usd>`.",
    "bad-decision": "Cost cap: usage — `/cost continue | stop | ceiling <usd>`.",
}


# Verbatim operator notice a granted Continue surfaces while a profile ESTOP belt remains engaged.
# The plugin never unlinks the sentinel (manual-resume release), so the money block clears but the
# session stays paused until an operator lifts the belt.
_MANUAL_RESUME_NOTICE = (
    "Continue applied — the per-session cost block is cleared, but the profile ESTOP belt remains "
    "engaged (gates cron/kanban/new inbounds); resume via `hermes resume` or the UA-28 upstream lock"
)


def _cost_outcome_text(result) -> str:
    """A one-line operator-facing summary of a ``/cost`` resolution outcome."""
    if not isinstance(result, dict):
        return "Cost cap: could not process that /cost command."
    if result.get("granted"):
        decision = result.get("decision")
        if decision == "continue":
            if result.get("estop_disposition") == "left":
                # Surface the verbatim manual-resume notice so the reply is honest that the session
                # stays paused behind the belt and names how an operator lifts it.
                return f"⚠️ Cost cap: {_MANUAL_RESUME_NOTICE}."
            # "resumed" names a blocked→runnable transition, so it requires BOTH a prior block
            # (was_blocked) AND runnability now (resumes). A never-blocked but runnable Continue is
            # "runnable"; a still-paused one (resumes false) is a plain "Continue applied."
            if result.get("resumes"):
                if result.get("was_blocked"):
                    return "✅ Cost cap: session resumed (Continue applied)."
                return "✅ Cost cap: Continue applied; session is runnable."
            return "✅ Cost cap: Continue applied."
        if decision == "stop":
            return "🛑 Cost cap: session stopped."
        if decision == "ceiling":
            amount = result.get("amount_usd")
            try:
                return f"✅ Cost cap: ceiling set to ${float(amount):.2f}."
            except (TypeError, ValueError):
                return "✅ Cost cap: ceiling set."
        return "✅ Cost cap: resolution applied."
    return _COST_OUTCOME_MESSAGES.get(result.get("reason"), f"Cost cap: could not resolve ({result.get('reason')}).")


def _deliver_notice(gateway, event, text) -> None:
    """Best-effort one-line notice back to the event's source over the gateway's own outbound rail.

    A ``pre_gateway_dispatch`` ``skip`` drops the inbound with no reply, so this is the ONLY way
    to tell the operator the /cost outcome or that a turn is paused. Scheduled on the gateway loop
    via ``ctx.spawn_task``; a failure is swallowed (the durable state change already happened and
    the panel / the session's next turn still reflect it).
    """
    if gateway is None or _CTX is None or not text:
        return
    source = getattr(event, "source", None)
    deliver = getattr(gateway, "_deliver_platform_notice", None)
    if source is None or not callable(deliver):
        return
    try:
        _CTX.spawn_task(deliver(source, text), name="nv-cost-cap-notice")
    except Exception:
        logger.warning("nv-cost-cap notice delivery failed", exc_info=True)


_reconcile_loop_started = False


def _maybe_start_reconcile_loop() -> None:
    """Start the periodic reconciler ONCE, from the gateway loop thread (spawn_task needs one)."""
    global _reconcile_loop_started
    if _reconcile_loop_started or _CTX is None:
        return
    try:
        import asyncio

        interval = max(1, _cfg_int("escalation_reconcile_seconds", 3600))

        async def _loop():
            while True:
                try:
                    await asyncio.sleep(interval)
                    reconcile_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("nv-cost-cap reconcile loop iteration failed", exc_info=True)

        _CTX.spawn_task(_loop(), name="nv-cost-cap-reconcile")
        _reconcile_loop_started = True
    except Exception:
        logger.warning("nv-cost-cap reconcile loop start failed", exc_info=True)


def _pre_gateway_dispatch(event=None, gateway=None, session_store=None, **kwargs):
    try:
        _maybe_start_reconcile_loop()
        # COST-F30: intercept `/cost <continue|stop|ceiling ...>` and resolve it in-plugin
        # BEFORE the boundary/skip logic — a blocked session would otherwise skip its own
        # `/cost`. The command is dropped from dispatch (never a wasted model turn); the
        # outcome is delivered back on the gateway rail immediately (_deliver_notice). An authorized
        # Continue clears the per-session money block, but if a profile ESTOP belt is engaged the
        # plugin leaves it, so the notice reports manual_resume_required rather than a resume.
        cost_text = _cost_command_text(event)
        if cost_text is not None:
            session_id = _resolve_session_id(event, gateway, session_store)
            if session_id:
                profile = getattr(getattr(event, "source", None), "profile", None)
                try:
                    outcome = handle_cost_command(
                        session_id, cost_text, principal_from_event(event), profile=profile
                    )
                except Exception:
                    logger.warning("nv-cost-cap /cost handling failed", exc_info=True)
                    outcome = {"granted": False, "reason": "error"}
                _deliver_notice(gateway, event, _cost_outcome_text(outcome))
            else:
                _deliver_notice(gateway, event, "Cost cap: no active session for this chat.")
            return {"action": "skip", "reason": "nv-cost-cap: /cost handled"}

        # Recovery/steering commands are never skipped — checked BEFORE any
        # blocked/window state so an over-cap session can always be recovered.
        if _resolve_recovery_command(event) is not None:
            return None
        session_id = _resolve_session_id(event, gateway, session_store)
        if not session_id:
            return None
        stopped = _evaluate_boundary(
            session_id, today=_today(kwargs), platform=_event_platform(event)
        )
        if stopped:
            # A skipped inbound never reaches the llm_execution middleware, so this is the only
            # place the operator is told the session is paused and how to resolve it.
            _deliver_notice(gateway, event, _pause_notice_text())
            return {
                "action": "skip",
                "reason": "nv-cost-cap: session over the Tier-2 cost ceiling",
            }
        return None
    except Exception:
        logger.warning("nv-cost-cap pre_gateway_dispatch failed", exc_info=True)
        return None


# --- llm_execution middleware ----------------------------------------------

def _pause_notice_text() -> str:
    """The operator-facing pause notice naming the /cost commands — shared by the llm_execution
    synthetic response and the gateway skip path (a skipped inbound never reaches the middleware,
    so the gateway must deliver this itself)."""
    return (
        "Spending paused: this session reached its configured Tier-2 cost ceiling. "
        "An authorized operator can resume it with `/cost continue`, set an exact ceiling "
        "with `/cost ceiling <usd>`, or stop it with `/cost stop` — on this gateway or from "
        "the operator panel. Or start a new session (/new)."
    )


def _synthetic_response(api_mode):
    message = _pause_notice_text()
    if api_mode == "anthropic_messages":
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=message)],
            stop_reason="end_turn",
            model=None,
            usage=None,
        )
    if api_mode == "codex_responses":
        # codex_responses reaches this middleware (codex_app_server does not); its
        # transport validates a response only when ``.output`` is a non-empty
        # list, so the terminal must be Responses-shaped, not the chat shape below.
        return SimpleNamespace(
            status="completed",
            output=[
                SimpleNamespace(
                    type="message",
                    role="assistant",
                    status="completed",
                    content=[SimpleNamespace(type="output_text", text=message)],
                )
            ],
            output_text=message,
            model=None,
            usage=None,
        )
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                index=0,
                finish_reason="stop",
                message=SimpleNamespace(role="assistant", content=message, tool_calls=None),
            )
        ],
        model=None,
        usage=None,
    )


def _llm_execution(request=None, next_call=None, **kwargs):
    """Short-circuit a non-immortal over-cap provider call with a synthetic
    terminal response (never calling ``next_call``, never raising); otherwise
    run the provider call exactly once and return its result.
    """
    session_id = kwargs.get("session_id")
    api_mode = kwargs.get("api_mode")
    try:
        if session_id:
            stopped, _reason = _session_stopped(
                session_id, today=_today(kwargs), platform=kwargs.get("platform")
            )
            if stopped:
                return _synthetic_response(api_mode)
    except Exception:
        # A raise here is swallowed by core (downstream proceeds); prefer to let
        # the provider call run on an unexpected internal error rather than
        # wrongly refuse a legitimate call. Fail-CLOSED for unknown pricing is
        # handled deterministically in _session_stopped, not via this path.
        logger.warning("nv-cost-cap llm_execution decision failed", exc_info=True)
    if next_call is None:
        return None
    return next_call(request)


# --- CLI -------------------------------------------------------------------

def _orchestrator_profile() -> str:
    orch = _cfg("orchestrator_profile", None)
    if orch:
        return orch
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
        kanban = cfg.get("kanban") if isinstance(cfg, dict) else None
        pinned = kanban.get("orchestrator_profile") if isinstance(kanban, dict) else None
        if pinned:
            return pinned
    except Exception:
        logger.warning("nv-cost-cap orchestrator-profile read failed", exc_info=True)
    return "default"


def _write_profile_override(profile, value) -> None:
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_cli.profiles import get_profile_dir, normalize_profile_name, validate_profile_name

    if not _finite(value):
        raise ValueError("profile ceiling must be a finite number")
    canon = normalize_profile_name(profile)
    validate_profile_name(canon)
    token = set_hermes_home_override(str(get_profile_dir(canon)))
    try:
        _CTX.set_config("profile_ceiling_usd", float(value))
    finally:
        reset_hermes_home_override(token)


def _cli_cost_cap(ns, **kwargs):
    from hermes_cli.profiles import get_active_profile_name

    command = getattr(ns, "cost_cap_command", None)
    active = get_active_profile_name()

    if command == "show":
        result = {
            "ok": True,
            "active_profile": active,
            "resolved_ceiling_usd": resolve_ceiling(),
            "sessions": store.all_states(),
        }
        print(json.dumps(result))
        return result

    if command == "set":
        orchestrator = _orchestrator_profile()
        # Self-gate BEFORE any mutation (no built-in CLI access control exists).
        if active != orchestrator:
            result = {
                "ok": False,
                "reason": (
                    f"cost-cap set is orchestrator-only; active profile {active!r} "
                    f"is not the orchestrator {orchestrator!r}"
                ),
            }
            print(json.dumps(result))
            return result

        # Validate BOTH requested values before any mutation, so a non-finite
        # ceiling is never silently persisted as 0.0 or reported as applied.
        requested = {
            "fleet_ceiling_usd": getattr(ns, "fleet_ceiling_usd", None),
            "profile_ceiling_usd": getattr(ns, "profile_ceiling_usd", None),
        }
        invalid = [name for name, value in requested.items() if value is not None and not _finite(value)]
        if invalid:
            result = {"ok": False, "reason": f"{invalid[0]} must be a finite number"}
            print(json.dumps(result))
            return result

        profile = getattr(ns, "profile", None)
        profile_ceiling = getattr(ns, "profile_ceiling_usd", None)
        if (profile is None) != (profile_ceiling is None):
            result = {
                "ok": False,
                "reason": "--profile and --ceiling must be supplied together",
            }
            print(json.dumps(result))
            return result

        applied = {}
        fleet_ceiling = getattr(ns, "fleet_ceiling_usd", None)
        if fleet_ceiling is not None:
            try:
                set_fleet_ceiling(float(fleet_ceiling))
                applied["fleet_ceiling_usd"] = float(fleet_ceiling)
            except ManagedPinnedError as exc:
                result = {
                    "ok": False,
                    "reason": (
                        f"fleet ceiling is pinned by managed scope; edit the managed "
                        f"config ({exc})"
                    ),
                }
                print(json.dumps(result))
                return result

        if profile and profile_ceiling is not None:
            _write_profile_override(profile, float(profile_ceiling))
            applied["profile"] = profile
            applied["profile_ceiling_usd"] = float(profile_ceiling)

        result = {"ok": True, "applied": applied}
        print(json.dumps(result))
        return result

    if command == "resolve":
        orchestrator = _orchestrator_profile()
        # First of two gates: the operational gate — the resolve verb runs only from the
        # orchestrator profile. The second gate (the actor's operator-list membership) is
        # enforced inside resolve_via_cli under the target profile's pin.
        if active != orchestrator:
            result = {
                "ok": False,
                "reason": (
                    f"cost-cap resolve is orchestrator-only; active profile {active!r} "
                    f"is not the orchestrator {orchestrator!r}"
                ),
            }
            print(json.dumps(result))
            return result

        decision = getattr(ns, "decision", None)
        amount_usd = getattr(ns, "amount_usd", None)
        if decision == "set-ceiling" and amount_usd is None:
            result = {"ok": False, "reason": "--amount-usd is required for --decision set-ceiling"}
            print(json.dumps(result))
            return result

        # The recorded actor is the surface-namespaced fleet-admin id, consistent with the
        # dashboard:/gateway: principals so the audited actor is unambiguous.
        admin_id = _cfg("fleet_admin_id", None) or active
        actor = f"cli:{orchestrator}:{admin_id}"
        outcome = resolve_via_cli(
            getattr(ns, "profile", None),
            getattr(ns, "session_id", None),
            getattr(ns, "episode_id", None),
            getattr(ns, "budget_gen", None),
            decision,
            amount_usd,
            actor,
        )
        result = {"ok": bool(outcome.get("granted")), "actor": actor, "result": outcome}
        print(json.dumps(result))
        return result

    result = {"ok": False, "reason": "usage: hermes cost-cap {show|set [--fleet-ceiling X] [--profile P --ceiling Y]|resolve --profile P --session S --episode E --budget-gen G --decision continue|stop|set-ceiling [--amount-usd U]}"}
    print(json.dumps(result))
    return result


# --- registration ----------------------------------------------------------

def register(ctx) -> None:
    global _CTX
    _CTX = ctx

    from .cli import setup_cost_cap

    ctx.register_hook("post_api_request", _post_api_request)
    ctx.register_hook("post_tool_call", _post_tool_call)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("pre_gateway_dispatch", _pre_gateway_dispatch)
    ctx.register_hook("on_session_finalize", _on_session_finalize)
    ctx.register_middleware("llm_execution", _llm_execution)
    ctx.register_cli_command(
        name="cost-cap",
        help="Inspect and set the two-tier per-session cost cap policy",
        setup_fn=setup_cost_cap,
        handler_fn=_cli_cost_cap,
        description=(
            "Show the resolved cost-cap policy, or (orchestrator profile only) set the "
            "owner-pinned fleet ceiling and per-profile overrides."
        ),
    )
