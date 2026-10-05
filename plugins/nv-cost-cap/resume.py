"""nv-cost-cap — approval-gated ESTOP resume card (COST-F30.b).

Every owned ESTOP publish (``store.engage_owned``) raises ONE card bound to that exact stop: the
published bytes, the ownership receipt, the reason, ``engaged_at`` and core's candidate sentinel
paths at publish time. The card is queued in ``resume_outbox`` and delivered to
``settings.resume_card_target`` through core's send engine (``tools.send_message_tool``, the
``hermes send`` path), at most once per card.

The ONLY release is ``approve_resume``: an authorized operator of the OWNING profile, a card that is
still pending, every candidate sentinel re-read and still the carded stop, no open money question,
then exactly one call to core ``agent.estop.disengage()`` (what ``hermes resume`` runs). Deny,
expiry, any mismatch and an open money block keep the stop. Every answer is appended to
``resume_decisions`` (append-only), and a resume that actually happened notifies the approver
channel to pause again if they had just paused: core has no ESTOP lock, so a pause written between
the final re-read and ``disengage()`` is lifted too (residual window, closed upstream by UA-28).
"""

from __future__ import annotations

import base64
import json
import logging
import math
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

PLUGIN_KEY = "nv-cost-cap"
DEFAULT_TTL_SECONDS = 86400
INTERRUPTED_AFTER_SECONDS = 300
NOTICE_MAX_ATTEMPTS = 3


def _sibling(basename):
    """Path-load a sibling module on the ctx-free dashboard path (no parent package)."""
    import importlib.util

    name = f"nv_cost_cap_{basename}"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / f"{basename}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


try:
    from . import escalation, policy, store
except ImportError:  # pragma: no cover - the ctx-free dashboard import path
    escalation = _sibling("escalation")
    store = escalation.store
    policy = _sibling("policy")


def _clock() -> float:
    return time.time()


REFUSAL_TEXT = {
    "unauthorized": "⚠️ Cost cap: you are not an authorized operator of the profile that owns this resume card.",
    "unknown-card": "Cost cap: no such resume card (expected `<profile>:<12 hex>`); nothing changed.",
    "already-resolved": "Cost cap: this resume card was already resolved.",
    "superseded": "Cost cap: this resume card was superseded by a newer stop; use card {superseded_by}.",
    "expired": "Cost cap: this resume card expired, so the stop is kept. Release it with `hermes resume`.",
    "core-api-unavailable": ("Cost cap: core's resume API is unavailable in this install, so the stop is kept "
                             "and the card stays open. Release it with `hermes resume`."),
    "already-resumed": "Cost cap: the stop on this card is already gone (resumed elsewhere); nothing to release.",
    "fleet-pause-present": ("Cost cap: a fleet-wide operator pause is also engaged and a resume would lift it too, "
                            "so the stop is kept and this card is closed."),
    "unreadable-stop": ("Cost cap: the stop on disk is empty or unreadable and cannot be matched to this card, "
                        "so it is kept and this card is closed."),
    "repaused": "Cost cap: an operator paused again after this card was raised; that pause is kept and this card is closed.",
    "reason-changed": "Cost cap: the stop's reason changed after this card was raised; the stop is kept and this card is closed.",
    "stop-changed": "Cost cap: the stop on disk no longer matches this card byte for byte; it is kept and this card is closed.",
    "changed-during-approve": ("Cost cap: the stop changed while this approval was being checked; it is kept and "
                               "this card is closed."),
    "money-block-active": ("Cost cap: the money question is still open for session(s) {sessions}, so the stop is kept "
                           "and this card stays open. First run `/cost continue` or `/cost ceiling <usd>` (in that "
                           "session's chat, the dashboard cost card, or `hermes cost-cap resolve`), then "
                           "`/cost resume approve {card_id}`."),
    "resume-failed": ("Cost cap: core's resume call failed, so the profile may still be paused; check it and use "
                      "`hermes resume` if needed."),
    "bad-command": "Cost cap: usage — `/cost resume approve <card_id>` or `/cost resume deny <card_id>`.",
}

_RESUMED_TEXT = (
    "▶️ Resumed {profile} (card {card_id}) via hermes resume. If an operator paused in this same instant, "
    "that pause was lifted too; pause again if so. Core closes this window only with the upstream ESTOP "
    "owner lock (UA-28)."
)
_DENIED_TEXT = "⏹️ Denied resume card {card_id}: the stop on {profile} is kept. Release it later with `hermes resume`."
_NOTICE_TEXT = "session {session_id} resumed by approval of {actor} at {ts}; if you had just paused, pause again"
_STAYS_STOPPED = "session {session_id} itself stays stopped (Stop decision)"


def _iso(epoch) -> str:
    return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat()


def _result(ok, reason, card_id, text=None, **extra):
    return {"ok": bool(ok), "reason": reason, "card_id": card_id,
            "text": text if text is not None else REFUSAL_TEXT.get(reason, f"Cost cap: {reason}."), **extra}


def _pkg_attr(name):
    """An attribute of the loaded plugin package (``_CTX``, ``_refresh_effective``); None when ctx-free."""
    package = sys.modules.get(__package__) if __package__ else None
    return getattr(package, name, None)


def _setting(key, default):
    value = escalation._settings().get(key)
    return default if value is None else value


def _ttl_seconds() -> float:
    try:
        ttl = float(_setting("resume_card_ttl_seconds", DEFAULT_TTL_SECONDS))
    except (TypeError, ValueError):
        return float(DEFAULT_TTL_SECONDS)
    return ttl if ttl > 0 else float(DEFAULT_TTL_SECONDS)


def _finite_or_none(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _money(value) -> str:
    return "n/a" if value is None else f"${value:.2f}"


def _stop_identity(card) -> dict:
    return {k: card.get(k) for k in ("profile", "sentinel_path", "receipt", "reason", "engaged_at",
                                     "session_id", "origin")}


def _card_text(card, ttl) -> str:
    return (
        f"⏸️ Cost cap stopped {card['profile']} — session {card['session_id']}: "
        f"spend {_money(card['spend_usd'])}, cap {_money(card['cap_usd'])}, "
        f"ceiling {_money(card['ceiling_usd'])}; reason: {card['reason']}. Resume card {card['card_id']}. "
        f"Approve: /cost resume approve {card['card_id']} · Deny: /cost resume deny {card['card_id']} · "
        f"expires {_iso(card['created_at'] + ttl)}."
    )


def _cap_snapshot():
    from hermes_constants import get_hermes_home

    try:
        window = int(_setting("p90_window_sessions", 50))
    except (TypeError, ValueError):
        window = 50
    return policy.p90(policy.completed_session_totals(str(get_hermes_home()), window))


# --- card creation (called by store.engage_owned after an owned publish) ----

def raise_card(session_id, *, origin, sentinel, published_body, receipt, reason, engaged_at) -> str | None:
    """Record ONE pending card bound to the stop just published, queue its delivery and kick the drain.

    The card id pins the owning profile (``<profile>:<12 hex>``), so a home that does not map back to a
    named profile gets no card: it could never be pinned for Approve, and ``hermes resume`` stays the
    release. Returns the card id, or None.
    """
    from agent import estop
    from hermes_cli.profiles import get_active_profile_name, get_profile_dir
    from hermes_constants import get_hermes_home

    profile = get_active_profile_name()
    try:
        pinnable = get_profile_dir(profile).resolve() == get_hermes_home().resolve()
    except (OSError, ValueError):
        pinnable = False
    if not pinnable:
        logger.warning("nv-cost-cap: HERMES_HOME %s maps to no named profile; no resume card for session %s "
                       "(release via `hermes resume`)", get_hermes_home(), session_id)
        return None
    state = store.get_state(session_id)
    card = {
        "card_id": f"{profile}:{secrets.token_hex(6)}",
        "profile": profile,
        "session_id": session_id,
        "origin": origin,
        "sentinel_path": str(sentinel),
        "candidates": [str(p) for p in estop._candidate_sentinel_paths()],
        "published_body": bytes(published_body),
        "receipt": receipt,
        "reason": reason,
        "engaged_at": engaged_at,
        "spend_usd": _finite_or_none(state["effective_usd"] - state["window_start_total"]),
        "cap_usd": _finite_or_none(_cap_snapshot()),
        "ceiling_usd": _finite_or_none(store.resolved_ceiling()),
        "created_at": _clock(),
    }
    target = _setting("resume_card_target", None)
    store.record_resume_card(card, target=str(target) if target else None,
                             body=_card_text(card, _ttl_seconds()), ts=_iso(card["created_at"]))
    kick_drain()
    return card["card_id"]


# --- delivery ---------------------------------------------------------------

def _send(target, text):
    from tools.send_message_tool import send_message_tool

    return send_message_tool({"action": "send", "target": target, "message": text})


def _send_error(raw):
    """None when core's send engine reported success, else the error text it returned."""
    try:
        result = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    except ValueError:
        return f"unparseable send result: {str(raw)[:200]}"
    if isinstance(result, dict) and result.get("success") and not result.get("error"):
        return None
    if isinstance(result, dict) and result.get("error"):
        return str(result["error"])
    return f"unexpected send result: {str(raw)[:200]}"


def _drain_current_profile() -> int:
    claimed = 0
    for row in store.outbox_claimable(NOTICE_MAX_ATTEMPTS):
        attempts = store.claim_outbox_row(row["id"], row["status"], row["attempts"])
        if attempts is None:
            continue
        claimed += 1
        if not row["target"]:
            error = "no-target"
        else:
            try:
                error = _send_error(_send(row["target"], row["body"]))
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
        if error is None:
            store.finish_outbox_row(row["id"], "sent", None, sent_at=_clock())
            continue
        store.finish_outbox_row(row["id"], "failed", error)
        if row["kind"] == "notice" and error != "no-target" and attempts < NOTICE_MAX_ATTEMPTS:
            logger.warning("nv-cost-cap: resume notice for card %s failed (attempt %d/%d): %s; retrying on the "
                           "next drain", row["card_id"], attempts, NOTICE_MAX_ATTEMPTS, error)
        else:
            logger.warning("nv-cost-cap: delivering %s for resume card %s failed: %s; the stop is kept "
                           "(release via `hermes resume`)", row["kind"], row["card_id"], error)
    return claimed


def drain_outbox_once() -> int:
    """Deliver every claimable outbox row in every profile once; return how many rows were claimed.

    A row is claimed ``→sending`` by CAS before the send, so two drains never send it twice. A card is
    never re-sent (a crash mid-send leaves it ``sending``); a failed post-resume notice is re-claimed
    by later passes up to ``NOTICE_MAX_ATTEMPTS`` sends in total (erratum E1).
    """
    from hermes_cli.profiles import get_profile_dir, list_profile_names
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    claimed = 0
    try:
        names = list_profile_names()
    except Exception:
        logger.warning("nv-cost-cap resume drain: list_profile_names failed", exc_info=True)
        return claimed
    for name in names:
        try:
            home = get_profile_dir(name)
        except ValueError:
            continue
        # plugin_data_dir() would create the directory; a profile that never ran the plugin has nothing queued.
        if not (home / "plugin-data" / PLUGIN_KEY / "data.db").exists():
            continue
        token = set_hermes_home_override(str(home))
        try:
            claimed += _drain_current_profile()
        except Exception:
            logger.warning("nv-cost-cap resume drain failed for profile %s", name, exc_info=True)
        finally:
            reset_hermes_home_override(token)
    return claimed


def kick_drain() -> None:
    """One best-effort drain right after an enqueue, so a card goes out with no later inbound.

    Off any event loop (CLI, the agent worker thread, tests) it drains inline. On the gateway loop it
    must not block the loop the send engine hands its adapter call to, so it runs in a worker thread
    as a supervised plugin task (or an executor job on the ctx-free dashboard import). Never raises.
    """
    import asyncio

    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            drain_outbox_once()
            return
        ctx = _pkg_attr("_CTX")
        if ctx is not None:
            coro = asyncio.to_thread(drain_outbox_once)
            try:
                ctx.spawn_task(coro, name="nv-cost-cap-resume-kick")
            except Exception:
                coro.close()
                raise
            return
        import contextvars
        import functools

        loop.run_in_executor(None, functools.partial(contextvars.copy_context().run, drain_outbox_once))
    except Exception:
        logger.warning("nv-cost-cap: resume outbox kick failed; the periodic drain retries", exc_info=True)


# --- stop re-read + compare -------------------------------------------------

def _read_candidates(paths) -> list:
    """Every byte of every candidate path, read with ``Path.read_bytes`` only (no stat, no open)."""
    snap = []
    for p in paths:
        entry = {"path": str(p), "present": False, "content_b64": None, "error": None}
        try:
            data = Path(p).read_bytes()
        except FileNotFoundError:
            pass
        except OSError as exc:
            entry["present"] = True
            entry["error"] = type(exc).__name__
        else:
            entry["present"] = True
            entry["content_b64"] = base64.b64encode(data).decode("ascii")
        snap.append(entry)
    return snap


def _compare(card, snap):
    """None when ``snap`` still shows exactly the carded stop, else the refusal reason. Pure, in memory."""
    own = next((e for e in snap if e["path"] == card["sentinel_path"]), None)
    if own is None:
        return "stop-changed"
    if not own["present"]:
        return "already-resumed"
    if any(e["present"] for e in snap if e is not own):
        return "fleet-pause-present"
    if own["content_b64"] is None:
        return "unreadable-stop"
    data = base64.b64decode(own["content_b64"])
    try:
        body = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "unreadable-stop"
    if not isinstance(body, dict):
        return "unreadable-stop"
    if body.get("nv_cost_cap_receipt") != card["receipt"]:
        return "repaused"
    if body.get("reason") != card["reason"]:
        return "reason-changed"
    if body.get("engaged_at") != card["engaged_at"]:
        return "repaused"
    if data != card["published_body"]:
        return "stop-changed"
    return None


# --- money question ---------------------------------------------------------

def _stopped_by_applied_stop(session_id) -> bool:
    if not store.get_state(session_id)["blocked"]:
        return False
    rows = store.resolutions(session_id)
    return bool(rows) and rows[-1]["decision"] == "stop" and rows[-1]["status"] == "applied"


def open_money_sessions() -> list:
    """Open mortal sessions of the pinned profile whose money question is unresolved, after a fresh refresh.

    A session is resolved when it is runnable on its own (``session_resumes`` with the belt ignored) or
    when an applied Stop holds it. The belt is the only lever on profiles whose in-loop hooks are skipped
    (codex app-server), so lifting it while any session is still over budget would let it run past its
    cap. Fails closed: a session that cannot be refreshed counts as open.
    """
    refresh = _pkg_attr("_refresh_effective")
    open_ids = []
    readable = None
    for sid in store.open_mortal_session_ids():
        try:
            if _stopped_by_applied_stop(sid):
                continue
            if readable is None:
                readable = _state_db_readable()
            # The spend readers degrade a missing / unreadable state.db to "no new spend", which would
            # let a stale cached total read as runnable; Approve needs a real read.
            if refresh is None or not readable:
                raise LookupError("no authoritative spend read (plugin not loaded, or state.db unreadable)")
            refresh(sid)
            if store.session_resumes(sid, belt_engaged=False):
                continue
        except Exception:
            logger.warning("nv-cost-cap: money check for session %s failed; treated as open", sid, exc_info=True)
        open_ids.append(sid)
    return open_ids


def _state_db_readable() -> bool:
    from hermes_constants import get_hermes_home

    db_path = get_hermes_home() / "state.db"
    if not db_path.is_file():
        return False
    try:
        conn = policy._ro_connect(str(db_path))
        try:
            conn.execute("SELECT 1 FROM sessions LIMIT 1").fetchall()
        finally:
            conn.close()
    except Exception:
        logger.warning("nv-cost-cap: state.db at %s is unreadable", db_path, exc_info=True)
        return False
    return True


# --- resolution -------------------------------------------------------------

def _parse_card_id(card_id):
    """The canonical owning profile of a well-formed ``<profile>:<12 hex>`` id whose profile exists, else None."""
    import re

    from hermes_cli.profiles import get_profile_dir, normalize_profile_name, validate_profile_name

    if not isinstance(card_id, str) or card_id.count(":") != 1:
        return None
    profile, hex_part = card_id.split(":")
    if not re.fullmatch(r"[0-9a-f]{12}", hex_part):
        return None
    try:
        canon = normalize_profile_name(profile)
        validate_profile_name(canon)
        if canon != profile or not get_profile_dir(canon).is_dir():
            return None
    except (ValueError, OSError):
        return None
    return canon


def _decide(card, expected, new_status, decision, actor, *, snap=None, outcome=None, claimed_at=None,
            ts=None, outbox=None) -> bool:
    """CAS the card ``expected→new_status`` (None = status unchanged) and append its decision row, in one tx."""
    return store.transition_card(
        card["card_id"], expected, new_status, decision=decision, actor=actor,
        ts=ts or _iso(_clock()), stop=_stop_identity(card), candidates=snap, outcome=outcome or {},
        claimed_at=claimed_at, outbox=outbox,
    )


def _refuse(card, reason, actor, snap, *, expected="pending", consume=True):
    new_status = "refused" if consume else None
    if not _decide(card, expected, new_status, f"refused:{reason}", actor, snap=snap):
        return _result(False, "already-resolved", card["card_id"])
    return _result(False, reason, card["card_id"])


def _load_pending(card_id, actor):
    """``(card, None)`` for a pending, unexpired card, else ``(None, refusal)``. Expiry consumes the card."""
    card = store.resume_card(card_id)
    if card is None:
        return None, _result(False, "unknown-card", card_id)
    if card["status"] == "superseded":
        return None, _result(False, "superseded", card_id,
                             REFUSAL_TEXT["superseded"].format(superseded_by=card["superseded_by"]))
    if card["status"] != "pending":
        return None, _result(False, "already-resolved", card_id)
    if _clock() > card["created_at"] + _ttl_seconds():
        expired = _decide(card, "pending", "expired", "expired", actor)
        return None, _result(False, "expired" if expired else "already-resolved", card_id)
    return card, None


def _stays_stopped(card) -> str:
    sid = card.get("session_id")
    if sid and _stopped_by_applied_stop(sid):
        return _STAYS_STOPPED.format(session_id=sid)
    return ""


def approve_resume(card_id, principal) -> dict:
    """Release the carded stop through core ``disengage()``, at most once, only if it is still that stop.

    Order (ADR D4): parse → pin the owning profile → authorize → load (superseded / resolved / expired)
    → core API present → first read + compare (the stop question wins over the money question) → money
    question → claim ``pending→approving`` with the ``attempt`` audit row committed → recompute the
    candidates, final read, in-memory compare, ``disengage()``. Nothing but the pure compare runs between
    the final read and the call: no DB, no logging, no other file access.
    """
    profile = _parse_card_id(card_id)
    if profile is None:
        return _result(False, "unknown-card", card_id)
    with escalation._pinned_profile(profile):
        if not escalation._authorized(principal):
            return _result(False, "unauthorized", card_id)
        card, refusal = _load_pending(card_id, principal)
        if refusal is not None:
            return refusal

        from agent import estop

        if not (callable(getattr(estop, "_candidate_sentinel_paths", None))
                and callable(getattr(estop, "disengage", None))):
            return _refuse(card, "core-api-unavailable", principal, None, consume=False)

        paths = [str(p) for p in estop._candidate_sentinel_paths()]
        snap1 = _read_candidates(paths)
        mismatch = "stop-changed" if paths != card["candidates"] else _compare(card, snap1)
        if mismatch is not None:
            return _refuse(card, mismatch, principal, snap1)

        open_ids = open_money_sessions()
        if open_ids:
            if not _decide(card, "pending", None, "refused:money-block-active", principal, snap=snap1,
                           outcome={"open_sessions": open_ids}):
                return _result(False, "already-resolved", card_id)
            return _result(False, "money-block-active", card_id, REFUSAL_TEXT["money-block-active"].format(
                sessions=", ".join(open_ids), card_id=card_id), open_sessions=open_ids)

        stays = _stays_stopped(card)
        if not _decide(card, "pending", "approving", "attempt", principal, snap=snap1, claimed_at=_clock()):
            return _result(False, "already-resolved", card_id)

        if [str(p) for p in estop._candidate_sentinel_paths()] != paths:
            return _refuse(card, "changed-during-approve", principal, snap1, expected="approving")
        snap2 = _read_candidates(paths)
        if _compare(card, snap2) is not None:
            return _refuse(card, "changed-during-approve", principal, snap2, expected="approving")
        try:
            lifted = estop.disengage()
        except Exception as exc:
            _decide(card, _CLAIMED, "resume-failed", "resume-failed", principal, snap=snap1,
                    outcome={"error": type(exc).__name__})
            return _result(False, "resume-failed", card_id)
        if not lifted:
            _decide(card, _CLAIMED, "refused", "refused:already-resumed", principal, snap=snap1)
            return _result(False, "already-resumed", card_id)
        return _granted(card, principal, snap1, paths, stays)


# A slow call can outlive the reconciler's interrupted-after window; the outcome still lands on the card.
_CLAIMED = ("approving", "interrupted")


def _granted(card, principal, snap1, paths, stays) -> dict:
    """Record a resume that HAS happened. Nothing here may turn it into a reported failure."""
    ts = _iso(_clock())
    after = []
    for p in paths:
        try:
            after.append({"path": p, "present": Path(p).exists()})
        except OSError:
            after.append({"path": p, "present": None})
    target = _setting("resume_card_target", None)
    notice = _NOTICE_TEXT.format(session_id=card["session_id"], actor=principal, ts=ts)
    if stays:
        notice += f"; {stays}"
    outbox = {"kind": "notice", "target": str(target) if target else None, "body": notice, "created_at": _clock()}
    try:
        if not _decide(card, _CLAIMED, "approved", "approve", principal, snap=snap1, outcome={"after": after},
                       ts=ts, outbox=outbox):
            logger.warning("nv-cost-cap: resume card %s changed status during its resume; recording the "
                           "approve anyway", card["card_id"])
            store.append_decision(card["card_id"], decision="approve", actor=principal, ts=ts,
                                  stop=_stop_identity(card), candidates=snap1, outcome={"after": after},
                                  outbox=outbox)
    except Exception:
        logger.warning("nv-cost-cap: recording the resume of card %s failed; the stop WAS lifted",
                       card["card_id"], exc_info=True)
    kick_drain()
    text = _RESUMED_TEXT.format(profile=card["profile"], card_id=card["card_id"])
    if stays:
        text += f" Note: {stays}."
    return _result(True, "resumed", card["card_id"], text)


def deny_resume(card_id, principal) -> dict:
    """Keep the stop and close the card ``denied``, with the stop identity and a first read on record."""
    profile = _parse_card_id(card_id)
    if profile is None:
        return _result(False, "unknown-card", card_id)
    with escalation._pinned_profile(profile):
        if not escalation._authorized(principal):
            return _result(False, "unauthorized", card_id)
        card, refusal = _load_pending(card_id, principal)
        if refusal is not None:
            return refusal
        if not _decide(card, "pending", "denied", "deny", principal, snap=_read_candidates(card["candidates"])):
            return _result(False, "already-resolved", card_id)
        return _result(True, "denied", card_id, _DENIED_TEXT.format(card_id=card_id, profile=card["profile"]))


def handle_resume_command(text, principal) -> dict:
    """The gateway ``/cost resume approve|deny <card_id>`` seam; needs no chat session (card ids pin a profile)."""
    tokens = (text or "").split()
    if len(tokens) != 4 or tokens[0].lstrip("/").lower() != "cost" or tokens[1].lower() != "resume":
        return _result(False, "bad-command", None)
    action, card_id = tokens[2].lower(), tokens[3]
    if action == "approve":
        return approve_resume(card_id, principal)
    if action == "deny":
        return deny_resume(card_id, principal)
    return _result(False, "bad-command", card_id)


def sweep_cards() -> int:
    """Reconciler pass for the pinned profile: expire stale pending cards and finalise ``approving`` cards a
    crash left behind as ``interrupted`` (their ``attempt`` row stays). Never touches a sentinel."""
    now, ttl, acted = _clock(), _ttl_seconds(), 0
    for card in store.resume_cards(status="pending"):
        if now > card["created_at"] + ttl and _decide(card, "pending", "expired", "expired", "reconciler"):
            acted += 1
    for card in store.resume_cards(status="approving"):
        claimed = card.get("claimed_at") or card["created_at"]
        if now > claimed + INTERRUPTED_AFTER_SECONDS and _decide(card, "approving", "interrupted", "interrupted",
                                                                 "reconciler"):
            acted += 1
    return acted
