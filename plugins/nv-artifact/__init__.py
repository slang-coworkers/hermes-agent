"""nv-artifact — fleet PR→session ownership index + outcome/cost analytics (GOV-F25, OBS-F48).

One fleet ``plugin_db`` (pinned to ``ledger_profile``) with three tables:

* ``ownership(repo, pr, task_id, profile)`` — ``INSERT OR IGNORE`` first-claim-wins.
  The first worker whose ``gh pr create`` produced a PR URL owns ``(repo, pr)``;
  a foreign claim is refused naming the holder, the same holder refreshes its
  own ``task_id`` in place. Fed by the ``post_tool_call`` observer (terminal AND
  execute_code); the resolver ``resolve_pr_owner`` returns the owner.
* ``outcomes(artifact, terminal_outcome, cost_usd, profile)`` — one idempotent
  ``UNIQUE(artifact)`` row per ``<repo>#<pr>``, appended by ``kanban_task_completed``
  and ``on_session_end`` (cost recomputed from ``session_model_usage``, SET not
  accumulated); ``merged`` is derived by the observer on a claimed ``pull_request``
  closed+merged webhook. Read by orchestrator-only ``hermes pr remap`` and
  ``hermes outcomes funnel|winrate|cost-per-merge`` and the dashboard page.
* ``seen_deliveries(repo, pr, delivery_id)`` — internal best-effort observer dedup,
  written AFTER the notification commit (publish-then-seen) so a publish failure
  is never suppressed.

Later webhook events for a claimed PR are delivered to the owning session by the
one ``pre_gateway_dispatch`` observer, which durably enqueues a ``notification``
on the owning kanban card's durable-retry wake sub and returns ``{"action":"skip"}``
so the default profile mints no orphan session; the kanban notifier then wakes the
owning profile (profile-aware ``wake.py`` self-POST). The observer returns ``None``
only for an unclaimed / off-route / non-webhook delivery, and is fail-closed
(``skip``, never ``None``) if a claimed enqueue cannot commit.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import re
import sqlite3
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

PLUGIN_KEY = "nv-artifact"
_TOOLSET = "nv_artifact"

# Bound in register(): lets the hook callbacks, CLI handlers, and module-level
# resolver/outcome helpers resolve the single fleet ledger through ctx config.
_CTX = None

# Public core widening (W1). Imported as a module-level name so the observer's
# call site resolves this symbol — AC-GOV-F25-5's fail-closed case patches
# module.publish_task_notification. Guarded so the plugin still imports on a
# tree that predates the widening.
try:  # pragma: no cover - exercised on the widened fork tree
    from hermes_cli.kanban_db import publish_task_notification
except Exception:  # pragma: no cover
    publish_task_notification = None

# gh pr create intent: the three tokens in order, tolerant of the argv-list form
# execute_code uses (`['gh','pr','create']`) as well as a bare shell command. A
# command that merely echoes a PR URL without creating (gh pr view/list) never
# matches, so it cannot win the irreversible first claim.
_CREATE_INTENT_RE = re.compile(r"\bgh\W+pr\W+create\b", re.IGNORECASE)
# Same shape as kanban_db._RESPAWN_GUARD_PR_URL_RE, with (owner/repo) and (pr) captured.
_PR_URL_RE = re.compile(
    r"https?://github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)", re.IGNORECASE
)

_BUSY_RETRIES = 5
_BUSY_SLEEP_S = 0.05


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_locked_error(exc: Exception) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and (
        "locked" in str(exc).lower() or "busy" in str(exc).lower()
    )


def _safe_handler(fn):
    """Guarantee a tool handler returns a JSON string and never raises."""

    @functools.wraps(fn)
    def _wrapped(args, **kwargs):
        try:
            return fn(args, **kwargs)
        except Exception:
            logger.warning("nv-artifact handler %s failed", fn.__name__, exc_info=True)
            return json.dumps({"status": "error", "reason": "nv-artifact operation failed"})

    return _wrapped


def _active_profile() -> str:
    try:
        from hermes_cli.profiles import get_active_profile_name

        return get_active_profile_name()
    except Exception:
        return "default"


def _is_orchestrator(ctx) -> bool:
    orch = ctx.get_config("orchestrator_profile", "orchestrator")
    return _active_profile() == orch


def _board(ctx):
    return ctx.get_config("board") or None


def _kb_connect(ctx):
    import hermes_cli.kanban_db as kb

    board = _board(ctx)
    return kb.connect(board=board) if board else kb.connect()


def _ensure_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ownership (
            repo    TEXT    NOT NULL,
            pr      INTEGER NOT NULL,
            task_id TEXT    NOT NULL,
            profile TEXT    NOT NULL,
            UNIQUE (repo, pr)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS outcomes (
            artifact         TEXT NOT NULL,
            terminal_outcome TEXT,
            cost_usd         REAL NOT NULL DEFAULT 0,
            profile          TEXT,
            UNIQUE (artifact)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_deliveries (
            repo        TEXT    NOT NULL,
            pr          INTEGER NOT NULL,
            delivery_id TEXT    NOT NULL,
            PRIMARY KEY (repo, pr, delivery_id)
        )
        """
    )
    conn.commit()


def _db(ctx):
    """Open the single fleet ledger, pinned to the ``ledger_profile`` owner's home.

    A context-local HERMES_HOME override scopes only ``plugin_db``'s path
    resolution (never ``os.environ``), so a worker profile, the gateway/default
    process, and the read verbs all resolve the identical file. Mirrors GOV-F24.
    """
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_cli.profiles import get_profile_dir, normalize_profile_name, validate_profile_name
    from plugins.plugin_storage import plugin_db

    owner = normalize_profile_name(ctx.get_config("ledger_profile", "default"))
    validate_profile_name(owner)
    home = str(get_profile_dir(owner))

    attempt = 0
    while True:
        conn = None
        try:
            token = set_hermes_home_override(home)
            try:
                conn = plugin_db(PLUGIN_KEY)
            finally:
                reset_hermes_home_override(token)
            _ensure_schema(conn)
            return conn
        except sqlite3.OperationalError as exc:
            if conn is not None:
                conn.close()
            attempt += 1
            if not _is_locked_error(exc) or attempt >= _BUSY_RETRIES:
                raise
            time.sleep(_BUSY_SLEEP_S * attempt)
        except Exception:
            if conn is not None:
                conn.close()
            raise


def _pr_key(repo: str, pr: int) -> str:
    """Idempotency key for the owning card, qualified by BOTH repo and pr so two
    repositories sharing a PR number get DISTINCT cards (a bare gh-pr-<n> would
    collide and mis-route the first repo's later events)."""
    digest = hashlib.sha256(f"{repo}#{pr}".encode()).hexdigest()[:20]
    return f"gh-pr-{digest}"


# ---------------------------------------------------------------------------
# Ownership: claim + resolve
# ---------------------------------------------------------------------------

def _register_wake_sub(ctx, task_id, session_id, profile) -> None:
    """Register the ONE durable-retry wake sub on the owning card (idempotent on
    the sub PK, so a refresh does not duplicate it). This is the subscription the
    kanban notifier later carries the PR event on."""
    if not (task_id and session_id and profile):
        return
    try:
        import hermes_cli.kanban_db as kb

        with _kb_connect(ctx) as conn:
            kb.add_notify_sub(
                conn, task_id=task_id, platform="api_server", chat_id=session_id,
                notifier_profile=profile, delivery_mode="wake", retry_policy="durable",
            )
    except Exception:
        logger.warning("nv-artifact wake-sub registration failed", exc_info=True)


@_safe_handler
def _report_pr_created(args, **kwargs) -> str:
    """Internal claim primitive (never model-facing): INSERT OR IGNORE the owner
    row. First claim wins; a foreign claim is refused naming the holder; the same
    holder refreshes its own task_id in place. On a claim/refresh it registers the
    owning card's durable-retry wake sub."""
    ctx = _CTX
    if ctx is None:
        return json.dumps({"status": "refused", "reason": "nv-artifact not initialized"})
    repo = str(args.get("repo") or "")
    try:
        pr = int(args["pr"])
    except (KeyError, TypeError, ValueError):
        return json.dumps({"status": "refused", "reason": "missing or invalid pr"})
    task_id = str(args.get("task_id") or "")
    profile = str(args.get("profile") or "")
    session_id = str(args.get("session_id") or "")
    if not (repo and task_id and profile):
        return json.dumps({"status": "refused", "reason": "missing repo/task_id/profile"})

    conn = _db(ctx)
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO ownership (repo, pr, task_id, profile) VALUES (?, ?, ?, ?)",
            (repo, pr, task_id, profile),
        )
        conn.commit()
        if cur.rowcount == 1:
            _register_wake_sub(ctx, task_id, session_id, profile)
            return json.dumps({"status": "claimed", "repo": repo, "pr": pr,
                               "task_id": task_id, "profile": profile})
        row = conn.execute(
            "SELECT profile, task_id FROM ownership WHERE repo = ? AND pr = ?", (repo, pr)
        ).fetchone()
        holder = row[0] if row else None
        if holder == profile:
            conn.execute(
                "UPDATE ownership SET task_id = ? WHERE repo = ? AND pr = ?",
                (task_id, repo, pr),
            )
            conn.commit()
            _register_wake_sub(ctx, task_id, session_id, profile)
            return json.dumps({"status": "refreshed", "repo": repo, "pr": pr,
                               "task_id": task_id, "profile": profile})
        return json.dumps({"status": "refused", "holder": holder, "repo": repo, "pr": pr})
    finally:
        conn.close()


def resolve_pr_owner(repo, pr):
    """Return ``{repo, pr, task_id, profile}`` for a claimed PR, else ``None``."""
    ctx = _CTX
    if ctx is None:
        return None
    conn = _db(ctx)
    try:
        row = conn.execute(
            "SELECT task_id, profile FROM ownership WHERE repo = ? AND pr = ?",
            (str(repo), int(pr)),
        ).fetchone()
        if row is None:
            return None
        return {"repo": str(repo), "pr": int(pr), "task_id": row[0], "profile": row[1]}
    finally:
        conn.close()


@_safe_handler
def _resolve_pr_owner_tool(args, **kwargs) -> str:
    owner = resolve_pr_owner(args.get("repo"), args.get("pr"))
    return json.dumps(owner if owner is not None else {"status": "unclaimed"})


# ---------------------------------------------------------------------------
# Claim feed: post_tool_call observer (terminal AND execute_code)
# ---------------------------------------------------------------------------

def _on_post_tool_call(tool_name=None, args=None, result=None, status=None,
                       task_id=None, session_id=None, **kwargs):
    """Claim (repo, pr) for the calling session when a successful terminal/
    execute_code ``gh pr create`` produced a PR URL. Observer: returns None."""
    try:
        ctx = _CTX
        if ctx is None or tool_name not in ("terminal", "execute_code"):
            return None
        if status not in ("ok", None):
            return None
        args = args or {}
        invocation = args.get("command") if tool_name == "terminal" else args.get("code")
        if not _CREATE_INTENT_RE.search(str(invocation or "")):
            return None
        m = _PR_URL_RE.search(str(result or ""))
        if not m:
            return None
        repo, pr = m.group(1), int(m.group(2))

        # Ensure an owning card and bind the calling session onto it. A claim
        # never lands without a resolvable owner (task_id is NOT NULL).
        import hermes_cli.kanban_db as kb

        card = str(task_id or "") or None
        conn = _kb_connect(ctx)
        try:
            if card is None:
                created = kb.create_task(
                    conn, title=f"{repo}#{pr}", assignee=_active_profile(),
                    idempotency_key=_pr_key(repo, pr), board=_board(ctx),
                )
                card = created if isinstance(created, str) else getattr(created, "id", created)
            if session_id:
                with kb.write_txn(conn, allow_nested=True):
                    conn.execute("UPDATE tasks SET session_id = ? WHERE id = ?", (session_id, card))
        finally:
            conn.close()

        ctx.dispatch_tool(
            "report_pr_created",
            {"repo": repo, "pr": pr, "task_id": card,
             "session_id": session_id or "", "profile": _active_profile()},
        )
    except Exception:
        logger.warning("nv-artifact post_tool_call claim failed", exc_info=True)
    return None


# ---------------------------------------------------------------------------
# Delivery: pre_gateway_dispatch observer (3-outcome, fail-closed)
# ---------------------------------------------------------------------------

def _extract_repo_pr(raw):
    repo = None
    pr = None
    repository = raw.get("repository")
    if isinstance(repository, dict):
        repo = repository.get("full_name")
    pull = raw.get("pull_request")
    if isinstance(pull, dict) and pull.get("number") is not None:
        pr = pull.get("number")
    else:
        issue = raw.get("issue")
        if isinstance(issue, dict) and issue.get("number") is not None:
            pr = issue.get("number")
    try:
        pr = int(pr) if pr is not None else None
    except (TypeError, ValueError):
        pr = None
    return repo, pr


def _synth_message(raw, repo, pr) -> str:
    return f"{raw.get('action') or 'update'} on {repo}#{pr}"


def _seen_delivery(ctx, repo, pr, delivery_id) -> bool:
    conn = _db(ctx)
    try:
        row = conn.execute(
            "SELECT 1 FROM seen_deliveries WHERE repo = ? AND pr = ? AND delivery_id = ?",
            (str(repo), int(pr), str(delivery_id)),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _mark_seen(ctx, repo, pr, delivery_id) -> None:
    conn = _db(ctx)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO seen_deliveries (repo, pr, delivery_id) VALUES (?, ?, ?)",
            (str(repo), int(pr), str(delivery_id)),
        )
        conn.commit()
    finally:
        conn.close()


def _maybe_derive_merged(ctx, raw, repo, pr, owner) -> None:
    """A claimed ``pull_request`` closed+merged webhook derives terminal_outcome
    ='merged' for the artifact (retaining its recorded cost/profile) — the merge
    signal winrate/cost-per-merge read, distinct from 'completed'."""
    pull = raw.get("pull_request")
    if raw.get("action") == "closed" and isinstance(pull, dict) and pull.get("merged"):
        _upsert_outcome(f"{repo}#{pr}", terminal_outcome="merged", profile=owner["profile"])


def _on_pre_gateway_dispatch(event=None, gateway=None, session_store=None, **kwargs):
    """Deliver a claimed PR's later webhook event to the owning session by a
    durable ``notification`` enqueue, returning ``{"action":"skip"}`` (claimed) so
    the default profile mints no orphan; ``None`` for unclaimed/off-route/non-webhook;
    and fail-closed ``skip`` (never ``None``) if a claimed enqueue cannot commit."""
    try:
        ctx = _CTX
        if ctx is None or event is None:
            return None
        route = ctx.get_config("notify_route", "gh-pr")
        source = getattr(event, "source", None)
        if source is None:
            return None
        from gateway.config import Platform

        if getattr(source, "platform", None) != Platform.WEBHOOK:
            return None
        if getattr(source, "chat_type", None) != "webhook":
            return None
        chat_id = getattr(source, "chat_id", None) or ""
        if not chat_id.startswith(f"webhook:{route}:"):
            return None
        raw = getattr(event, "raw_message", None)
        if not isinstance(raw, dict):
            return None
        repo, pr = _extract_repo_pr(raw)
        if not repo or pr is None:
            return None
        owner = resolve_pr_owner(repo, pr)
        if owner is None:
            return None  # UNCLAIMED — legitimate default routing
    except Exception:
        # Pre-resolution failure: ownership unknown, so the safe outcome is
        # normal routing (None). Fail-closed applies only to a CLAIMED PR below.
        logger.warning("nv-artifact pre_gateway_dispatch pre-resolve failed", exc_info=True)
        return None

    # CLAIMED from here — never return None (never re-open the default orphan).
    try:
        _maybe_derive_merged(ctx, raw, repo, pr, owner)
    except Exception:
        logger.warning("nv-artifact merged-derivation failed for %s#%s", repo, pr, exc_info=True)

    delivery_id = str(getattr(event, "message_id", "") or "")
    try:
        if not (delivery_id and _seen_delivery(ctx, repo, pr, delivery_id)):
            message = (getattr(event, "text", "") or "").strip() or _synth_message(raw, repo, pr)
            key = f"{repo}#{pr}:{delivery_id}"
            with _kb_connect(ctx) as conn:
                publish_task_notification(
                    conn, owner["task_id"], message, metadata={"idempotency_key": key},
                )
            # Publish-then-seen: only record the dedup row AFTER the notification
            # commit, so a publish failure is never suppressed (it re-delivers).
            if delivery_id:
                _mark_seen(ctx, repo, pr, delivery_id)
    except Exception:
        logger.warning(
            "nv-artifact durable enqueue failed for %s#%s delivery=%s — fail-closed skip",
            repo, pr, delivery_id, exc_info=True,
        )
    return {"action": "skip"}


# ---------------------------------------------------------------------------
# Outcomes: kanban terminal + on_session_end
# ---------------------------------------------------------------------------

def _upsert_outcome(artifact, *, terminal_outcome=None, cost_usd=None, profile=None) -> None:
    """Idempotent UNIQUE(artifact) upsert. Only the provided fields change;
    ``cost_usd`` is SET (never accumulated), so a repeated on_session_end does
    not double-count and a later read reflects the newest total."""
    ctx = _CTX
    if ctx is None:
        return
    conn = _db(ctx)
    try:
        row = conn.execute(
            "SELECT terminal_outcome, cost_usd, profile FROM outcomes WHERE artifact = ?",
            (artifact,),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO outcomes (artifact, terminal_outcome, cost_usd, profile) VALUES (?, ?, ?, ?)",
                (artifact, terminal_outcome, float(cost_usd or 0.0), profile),
            )
        else:
            new_outcome = terminal_outcome if terminal_outcome is not None else row[0]
            new_cost = float(cost_usd) if cost_usd is not None else row[1]
            new_profile = profile if profile is not None else row[2]
            conn.execute(
                "UPDATE outcomes SET terminal_outcome = ?, cost_usd = ?, profile = ? WHERE artifact = ?",
                (new_outcome, new_cost, new_profile, artifact),
            )
        conn.commit()
    finally:
        conn.close()


def _artifact_for_task(ctx, task_id):
    conn = _db(ctx)
    try:
        row = conn.execute(
            "SELECT repo, pr, profile FROM ownership WHERE task_id = ?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None, None
    return f"{row[0]}#{row[1]}", row[2]


def _session_cost(session_id) -> float:
    """Σ over session_model_usage for the session (actual when cost_status is
    'actual', else estimated) — includes auxiliary/subagent rows. Read from the
    session's own profile home (on_session_end fires in that profile's process)."""
    from hermes_constants import get_hermes_home

    state_db = Path(get_hermes_home()) / "state.db"
    if not state_db.exists():
        return 0.0
    conn = sqlite3.connect(str(state_db))
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN cost_status = 'actual' "
            "THEN actual_cost_usd ELSE estimated_cost_usd END), 0) "
            "FROM session_model_usage WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return float(row[0] or 0.0)
    except sqlite3.OperationalError:
        return 0.0
    finally:
        conn.close()


def _on_kanban_task_completed(task_id=None, **kwargs):
    """Record the terminal outcome for the artifact owned by ``task_id``."""
    try:
        ctx = _CTX
        if ctx is None or not task_id:
            return None
        artifact, profile = _artifact_for_task(ctx, task_id)
        if artifact is None:
            return None
        _upsert_outcome(artifact, terminal_outcome="completed", profile=profile)
    except Exception:
        logger.warning("nv-artifact kanban_task_completed failed", exc_info=True)
    return None


def _on_session_end(session_id=None, task_id=None, **kwargs):
    """Recompute the owning artifact's cumulative cost from session_model_usage."""
    try:
        ctx = _CTX
        if ctx is None or not session_id or not task_id:
            return None
        artifact, profile = _artifact_for_task(ctx, task_id)
        if artifact is None:
            return None
        _upsert_outcome(artifact, cost_usd=_session_cost(session_id), profile=profile)
    except Exception:
        logger.warning("nv-artifact on_session_end failed", exc_info=True)
    return None


# ---------------------------------------------------------------------------
# CLI verbs (orchestrator-only in the handler)
# ---------------------------------------------------------------------------

def _emit(payload, args) -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload))
    else:
        print(", ".join(f"{k}={v}" for k, v in payload.items()))


def _refuse(payload, args) -> None:
    # Refusals: JSON mode → stdout (one machine-parsable stream); human mode →
    # stderr. Both callers return a non-zero rc.
    if getattr(args, "json", False):
        print(json.dumps(payload))
    else:
        print(", ".join(f"{k}={v}" for k, v in payload.items()), file=sys.stderr)


def _cmd_pr(args):
    sub = getattr(args, "pr_command", None)
    if sub == "remap":
        return _cmd_pr_remap(args)
    _refuse({"status": "error", "reason": f"unknown pr subcommand: {sub}"}, args)
    return 2


def _cmd_pr_remap(args):
    ctx = _CTX
    if ctx is None:
        _refuse({"status": "error", "reason": "nv-artifact not initialized"}, args)
        return 1
    if not _is_orchestrator(ctx):
        _refuse({"status": "refused", "reason": "pr remap is orchestrator-only"}, args)
        return 1
    repo = str(args.repo)
    pr = int(args.pr)
    to = str(args.to)
    task = getattr(args, "task", None)
    conn = _db(ctx)
    try:
        row = conn.execute(
            "SELECT task_id FROM ownership WHERE repo = ? AND pr = ?", (repo, pr)
        ).fetchone()
        new_task = str(task) if task else (row[0] if row else None)
        if new_task is None:
            _refuse({"status": "error", "reason": "no owning task to re-point"}, args)
            return 1
        conn.execute(
            "INSERT INTO ownership (repo, pr, task_id, profile) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(repo, pr) DO UPDATE SET task_id = excluded.task_id, profile = excluded.profile",
            (repo, pr, new_task, to),
        )
        conn.commit()
    finally:
        conn.close()
    # Re-point delivery too: later events resolve the NEW owner, so the new
    # owning card needs a durable wake sub (chat_id = that card's bound session)
    # or the notifier has nothing to wake. Best-effort; the owner is re-subbed on
    # its next claim/refresh regardless.
    try:
        with _kb_connect(ctx) as kconn:
            srow = kconn.execute(
                "SELECT session_id FROM tasks WHERE id = ?", (new_task,)
            ).fetchone()
        new_sess = srow[0] if srow else None
        if new_sess:
            _register_wake_sub(ctx, new_task, new_sess, to)
    except Exception:
        logger.warning("nv-artifact remap: wake-sub re-point failed", exc_info=True)
    _emit({"status": "remapped", "repo": repo, "pr": pr, "task_id": new_task, "profile": to}, args)
    return 0


def _cmd_outcomes(args):
    ctx = _CTX
    if ctx is None:
        _refuse({"status": "error", "reason": "nv-artifact not initialized"}, args)
        return 1
    if not _is_orchestrator(ctx):
        _refuse({"status": "refused", "reason": "outcomes is orchestrator-only"}, args)
        return 1
    sub = getattr(args, "outcomes_command", None)
    agg = compute_outcomes(ctx, sub)
    if agg is None:
        _refuse({"status": "error", "reason": f"unknown outcomes subcommand: {sub}"}, args)
        return 2
    _emit(agg, args)
    return 0


def compute_outcomes(ctx, sub):
    """Compute one aggregate over the outcomes table. Shared by the CLI verb and
    the dashboard page so both read the same ledger the same way."""
    conn = _db(ctx)
    try:
        rows = conn.execute("SELECT terminal_outcome, cost_usd FROM outcomes").fetchall()
    finally:
        conn.close()
    total = len(rows)
    merged = [r for r in rows if r[0] == "merged"]
    if sub == "funnel":
        funnel: dict = {}
        for r in rows:
            key = r[0] or "unknown"
            funnel[key] = funnel.get(key, 0) + 1
        return funnel
    if sub == "winrate":
        return {"winrate": (len(merged) / total) if total else 0.0,
                "merged": len(merged), "total": total}
    if sub == "cost-per-merge":
        cost = sum(float(r[1] or 0.0) for r in merged)
        return {"cost_per_merge": (cost / len(merged)) if merged else 0.0,
                "merged": len(merged)}
    return None


def _setup_pr(parser) -> None:
    sub = parser.add_subparsers(dest="pr_command")
    remap = sub.add_parser("remap", help="Re-point PR ownership (orchestrator only)")
    remap.add_argument("repo", help="Repository in owner/name form")
    remap.add_argument("pr", type=int, help="Pull-request number")
    remap.add_argument("--to", required=True, help="New owning profile")
    remap.add_argument("--task", default=None, help="New owning task id (defaults to the current one)")
    remap.add_argument("--json", action="store_true", help="Emit JSON")


def _setup_outcomes(parser) -> None:
    sub = parser.add_subparsers(dest="outcomes_command")
    for name, help_text in (
        ("funnel", "Counts by terminal_outcome"),
        ("winrate", "merged / total artifacts"),
        ("cost-per-merge", "Σ cost over merged artifacts / merged count"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--json", action="store_true", help="Emit JSON")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

_REPORT_SCHEMA = {
    "description": (
        "Internal: claim (repo, pr) ownership for the calling session "
        "(first-claim-wins). Not model-callable — driven by the post_tool_call "
        "observer on a successful gh pr create."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "repo": {"type": "string"},
            "pr": {"type": "integer"},
            "task_id": {"type": "string"},
            "session_id": {"type": "string"},
            "profile": {"type": "string"},
        },
        "required": ["repo", "pr", "task_id", "profile"],
    },
}

_RESOLVE_SCHEMA = {
    "description": "Resolve the owning (task_id, profile) for a claimed PR.",
    "parameters": {
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "Repository in owner/name form."},
            "pr": {"type": "integer", "description": "Pull-request number."},
        },
        "required": ["repo", "pr"],
    },
}


def register(ctx) -> None:
    """Register the ownership/outcomes tools, the four observer hooks, and the
    orchestrator-only CLI verbs."""
    global _CTX
    _CTX = ctx

    ctx.register_tool(
        name="report_pr_created",
        toolset=_TOOLSET,
        schema=_REPORT_SCHEMA,
        handler=_report_pr_created,
        check_fn=lambda: False,  # never model-facing; claiming is observer-driven
        description=_REPORT_SCHEMA["description"],
    )
    ctx.register_tool(
        name="resolve_pr_owner",
        toolset=_TOOLSET,
        schema=_RESOLVE_SCHEMA,
        handler=_resolve_pr_owner_tool,
        check_fn=lambda: False,  # dispatchable by consumers; kept out of model schemas
        description=_RESOLVE_SCHEMA["description"],
    )

    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_hook("kanban_task_completed", _on_kanban_task_completed)

    ctx.register_cli_command(
        "pr",
        help="PR ownership index operations (orchestrator only)",
        setup_fn=_setup_pr,
        handler_fn=_cmd_pr,
        description="Re-point PR→session ownership.",
    )
    ctx.register_cli_command(
        "outcomes",
        help="Fleet outcome analytics (orchestrator only)",
        setup_fn=_setup_outcomes,
        handler_fn=_cmd_outcomes,
        description="funnel | winrate | cost-per-merge over the outcomes ledger.",
    )
