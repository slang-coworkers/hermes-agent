"""nv-approval-ledger — immutable fleet approval-decision ledger (GOV-F24).

A single append-only SQLite ledger of PR-approval decisions for the whole fleet.
Agent decisions are recorded through the writer-gated ``record_decision`` tool
(provenance ``agent_verified``); human PR-review verdicts enter through exactly
one door — the ``pre_gateway_dispatch`` hook observing the configured
``pull_request_review`` webhook route — which invokes ``record_human_verdict``
with an in-process-only token. Both paths are first-write-wins per
``(repo, pr, commit_sha, provenance)``; the read side is ``list_trusted_decisions``.
"""

from __future__ import annotations

import functools
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

PLUGIN_KEY = "nv-approval-ledger"
_TOOLSET = "approval_ledger"

# Identity token for the one human-write door. It exists only inside this module,
# so a model-issued record_human_verdict call — which can carry only JSON — can
# never present it: the human write is unmintable by any agent.
_HUMAN_DOOR = object()

# Bound in register(): lets the handlers, the hook callback, and the module-level
# list_trusted_decisions() resolve the single fleet ledger through ctx config.
_CTX = None

# Bounded retry for transient SQLite write contention. WAL + the driver's default
# busy timeout usually absorb it; this belt covers a lock that outlasts the timeout
# so a legitimate write (notably an authenticated human verdict) is not dropped.
_BUSY_RETRIES = 5
_BUSY_SLEEP_S = 0.05


def _is_locked_error(exc: Exception) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and (
        "locked" in str(exc).lower() or "busy" in str(exc).lower()
    )


def _safe_handler(fn):
    """Guarantee a tool handler returns a JSON string and never raises.

    A raise both violates the tool contract and — on the human webhook path,
    whose caller only inspects the returned status — would silently drop a
    verdict; catch everything and surface it as a structured error instead.
    """

    @functools.wraps(fn)
    def _wrapped(args, **kwargs):
        try:
            return fn(args, **kwargs)
        except Exception:
            logger.warning("approval-ledger handler %s failed", fn.__name__, exc_info=True)
            return json.dumps({"status": "error", "reason": "ledger operation failed"})

    return _wrapped


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _detail_blob(detail):
    if detail is None:
        return None
    try:
        return json.dumps(detail, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return json.dumps(str(detail))


def _ensure_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS approval_decisions (
            repo        TEXT    NOT NULL,
            pr          INTEGER NOT NULL,
            commit_sha  TEXT    NOT NULL,
            decision    TEXT    NOT NULL,
            provenance  TEXT    NOT NULL CHECK (provenance IN ('agent_verified','human')),
            delivery_id TEXT,
            profile     TEXT,
            detail      TEXT,
            created_at  TEXT    NOT NULL,
            UNIQUE (repo, pr, commit_sha, provenance)
        )
        """
    )
    # One human row per X-GitHub-Delivery id: replaying a delivery (even with a
    # mutated SHA) can never mint a second human row.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_human_delivery "
        "ON approval_decisions(delivery_id) "
        "WHERE provenance='human' AND delivery_id IS NOT NULL"
    )
    conn.commit()


def _ledger_db(ctx):
    """Open the single fleet ledger, pinned to the ``ledger_profile`` owner's home.

    The owner home is resolved first, then a context-local HERMES_HOME override
    scopes only ``plugin_db``'s path resolution (never ``os.environ``), so any
    profile/process — and the webhook callback — resolve the identical file.
    """
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_cli.profiles import get_profile_dir, normalize_profile_name, validate_profile_name
    from plugins.plugin_storage import plugin_db

    # Fail closed on a malformed owner: get_profile_dir normalizes but does not
    # validate, so a traversal/absolute ledger_profile would otherwise place the
    # ledger outside the profile tree. validate_profile_name raises on those.
    owner = normalize_profile_name(ctx.get_config("ledger_profile", "default"))
    validate_profile_name(owner)
    home = str(get_profile_dir(owner))

    # Retry open and schema initialization because first-use contention can occur
    # before the INSERT path and must not drop an authenticated verdict.
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


def _insert_ignore(conn, *, repo, pr, commit_sha, decision, provenance, delivery_id, profile, detail) -> int:
    """INSERT OR IGNORE one row; return rowcount (1 inserted, 0 suppressed).

    Never UPDATE/DELETE — a suppressed insert leaves the existing row byte-for-byte.
    Retries a bounded number of times on transient lock/busy so contention does
    not drop the write; a non-lock error (e.g. an out-of-range bind) is re-raised
    at once for the caller's handler to surface as a structured error.
    """
    params = (repo, pr, commit_sha, decision, provenance, delivery_id, profile, detail, _now())
    attempt = 0
    while True:
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO approval_decisions "
                "(repo, pr, commit_sha, decision, provenance, delivery_id, profile, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                params,
            )
            conn.commit()
            return cur.rowcount
        except sqlite3.OperationalError as exc:
            attempt += 1
            if not _is_locked_error(exc) or attempt >= _BUSY_RETRIES:
                raise
            time.sleep(_BUSY_SLEEP_S * attempt)


def _writers(ctx):
    writers = ctx.get_config("writers")
    return writers if isinstance(writers, list) else []


def _is_writer(ctx) -> bool:
    return ctx.profile_name in _writers(ctx)


def _writer_check() -> bool:
    return _CTX is not None and _is_writer(_CTX)


def _reader_check() -> bool:
    if _CTX is None:
        return False
    readers = _CTX.get_config("readers")
    return isinstance(readers, list) and _CTX.profile_name in readers


def _human_check() -> bool:
    # record_human_verdict is never model-facing: it is not in any profile's schema.
    return False


@_safe_handler
def _record_decision(args, **kwargs) -> str:
    """Writer-gated agent decision (provenance always agent_verified)."""
    ctx = _CTX
    if ctx is None:
        return json.dumps({"status": "refused", "reason": "ledger not initialized"})
    # The enforced gate: registry.dispatch does not re-check check_fn, so schema
    # visibility alone is not a control. Re-read writers fresh; fail closed.
    if not _is_writer(ctx):
        return json.dumps({"status": "refused", "reason": "not a ledger writer"})

    repo = str(args.get("repo") or "")
    commit_sha = str(args.get("commit_sha") or "")
    decision = str(args.get("decision") or "")
    try:
        pr = int(args["pr"])
    except (KeyError, TypeError, ValueError):
        return json.dumps({"status": "refused", "reason": "missing or invalid pr"})
    if not (repo and commit_sha and decision):
        return json.dumps({"status": "refused", "reason": "missing repo/commit_sha/decision"})

    conn = _ledger_db(ctx)
    try:
        inserted = _insert_ignore(
            conn, repo=repo, pr=pr, commit_sha=commit_sha, decision=decision,
            provenance="agent_verified", delivery_id=None,
            profile=ctx.profile_name, detail=_detail_blob(args.get("detail")),
        )
        if inserted == 1:
            return json.dumps({"status": "recorded", "provenance": "agent_verified"})
        row = conn.execute(
            "SELECT decision FROM approval_decisions "
            "WHERE repo=? AND pr=? AND commit_sha=? AND provenance='agent_verified'",
            (repo, pr, commit_sha),
        ).fetchone()
        if row is not None and row[0] == decision:
            return json.dumps({"status": "already_recorded", "provenance": "agent_verified"})
        logger.warning(
            "approval-ledger agent conflict repo=%s pr=%s sha=%s existing=%s new=%s",
            repo, pr, commit_sha, (row[0] if row else None), decision,
        )
        return json.dumps(
            {"status": "refused", "reason": "conflicting decision already recorded; record against the new head"}
        )
    finally:
        conn.close()


@_safe_handler
def _record_human_verdict(args, **kwargs) -> str:
    """Human PR-review verdict (provenance=human), guarded by the in-process door."""
    if kwargs.get("_human_door") is not _HUMAN_DOOR:
        return json.dumps(
            {"status": "refused", "reason": "human verdicts enter only through the validated webhook door"}
        )
    ctx = _CTX
    if ctx is None:
        return json.dumps({"status": "refused", "reason": "ledger not initialized"})

    repo = str(args.get("repo") or "")
    commit_sha = str(args.get("commit_sha") or "")
    decision = str(args.get("decision") or "")
    delivery_id = args.get("delivery_id")
    try:
        pr = int(args["pr"])
    except (KeyError, TypeError, ValueError):
        return json.dumps({"status": "refused", "reason": "missing or invalid pr"})
    if not (repo and commit_sha and decision and delivery_id):
        return json.dumps({"status": "refused", "reason": "missing repo/commit_sha/decision/delivery_id"})
    delivery_id = str(delivery_id)

    conn = _ledger_db(ctx)
    try:
        inserted = _insert_ignore(
            conn, repo=repo, pr=pr, commit_sha=commit_sha, decision=decision,
            provenance="human", delivery_id=delivery_id,
            profile=None, detail=_detail_blob(args.get("detail")),
        )
        if inserted == 1:
            return json.dumps({"status": "recorded", "provenance": "human"})

        # The insert was suppressed by a UNIQUE constraint. Classify delivery-id
        # FIRST — a single X-GitHub-Delivery must never bind to two commits, and
        # a natural-key match must not mask a delivery mismatch — then natural key.
        byd = conn.execute(
            "SELECT repo, pr, commit_sha FROM approval_decisions "
            "WHERE provenance='human' AND delivery_id=?",
            (delivery_id,),
        ).fetchone()
        if byd is not None and (byd[0], byd[1], byd[2]) != (repo, pr, commit_sha):
            logger.warning(
                "approval-ledger human delivery conflict delivery_id=%s bound_to=%s/#%s@%s incoming=%s/#%s@%s",
                delivery_id, byd[0], byd[1], byd[2], repo, pr, commit_sha,
            )
            return json.dumps(
                {"status": "refused", "reason": "delivery id already recorded against a different commit"}
            )

        nat = conn.execute(
            "SELECT decision FROM approval_decisions "
            "WHERE repo=? AND pr=? AND commit_sha=? AND provenance='human'",
            (repo, pr, commit_sha),
        ).fetchone()
        if nat is not None and nat[0] == decision:
            return json.dumps({"status": "already_recorded", "provenance": "human"})
        if nat is not None:
            logger.warning(
                "approval-ledger human conflict repo=%s pr=%s sha=%s delivery_id=%s existing=%s new=%s",
                repo, pr, commit_sha, delivery_id, nat[0], decision,
            )
            return json.dumps(
                {"status": "refused", "reason": "human verdict already recorded for this commit"}
            )
        logger.warning(
            "approval-ledger human insert ignored without a matching row repo=%s pr=%s sha=%s delivery_id=%s",
            repo, pr, commit_sha, delivery_id,
        )
        return json.dumps({"status": "refused", "reason": "insert ignored without a matching row"})
    finally:
        conn.close()


def _on_gateway_dispatch(event=None, **kwargs):
    """Observe the configured pull_request_review webhook and record the human row.

    Always returns ``None`` (never skip/rewrite), so it can neither drop nor alter
    a legitimate delivery. Every failure is swallowed and logged.
    """
    try:
        ctx = _CTX
        if ctx is None or event is None:
            return None
        review_route = ctx.get_config("review_route")
        if not review_route:
            return None
        source = getattr(event, "source", None)
        if source is None:
            return None

        from gateway.config import Platform

        if getattr(source, "platform", None) != Platform.WEBHOOK:
            return None
        if getattr(source, "chat_type", None) != "webhook":
            return None
        delivery_id = getattr(event, "message_id", None)
        if not isinstance(delivery_id, str) or not delivery_id:
            return None
        # Core builds chat_id as webhook:{route}:{delivery_id}. Match it exactly so
        # a route whose name merely shares this route's prefix cannot smuggle in a
        # trusted human verdict.
        if getattr(source, "chat_id", None) != f"webhook:{review_route}:{delivery_id}":
            return None

        raw = getattr(event, "raw_message", None)
        if not isinstance(raw, dict) or not {"review", "pull_request", "repository"}.issubset(raw):
            return None
        review = raw["review"]
        pull_request = raw["pull_request"]
        repository = raw["repository"]
        if not (isinstance(review, dict) and isinstance(pull_request, dict) and isinstance(repository, dict)):
            return None

        repo = repository.get("full_name")
        pr = pull_request.get("number")
        commit_sha = review.get("commit_id") or (pull_request.get("head") or {}).get("sha")
        decision = review.get("state")
        if not (repo and pr is not None and commit_sha and decision):
            return None

        result = ctx.dispatch_tool(
            "record_human_verdict",
            {
                "repo": repo,
                "pr": pr,
                "commit_sha": commit_sha,
                "decision": decision,
                "delivery_id": delivery_id,
                "detail": review,
            },
            _human_door=_HUMAN_DOOR,
        )
        # The adapter has already returned HTTP 202 and will not redeliver, so a
        # write that neither recorded nor was intentionally refused (a conflict
        # refuse is itself already logged by the handler) is a lost verdict —
        # make that terminal drop diagnosable rather than silent.
        status = None
        if isinstance(result, str):
            try:
                status = json.loads(result).get("status")
            except (TypeError, ValueError):
                status = None
        if status not in ("recorded", "already_recorded", "refused"):
            logger.warning(
                "approval-ledger human verdict NOT durably recorded (status=%s) "
                "repo=%s pr=%s sha=%s delivery_id=%s — dropped after webhook 202, no redelivery",
                status, repo, pr, commit_sha, delivery_id,
            )
    except Exception:  # noqa: BLE001 - the dispatch seam must never raise into the gateway
        logger.warning("nv-approval-ledger pre_gateway_dispatch callback failed", exc_info=True)
    return None


def list_trusted_decisions(repo=None, pr=None):
    """Return recorded approval decisions (optionally filtered by repo/pr).

    The single sanctioned read path; each row carries its provenance so a
    consumer can join agent_verified against human on (repo, pr, commit_sha).
    """
    ctx = _CTX
    if ctx is None:
        return []
    conn = _ledger_db(ctx)
    try:
        clauses, params = [], []
        if repo is not None:
            clauses.append("repo=?")
            params.append(str(repo))
        if pr is not None:
            clauses.append("pr=?")
            params.append(int(pr))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        cols = ("repo", "pr", "commit_sha", "decision", "provenance", "delivery_id", "profile", "detail", "created_at")
        rows = conn.execute(
            f"SELECT {', '.join(cols)} FROM approval_decisions{where} ORDER BY created_at, rowid",
            tuple(params),
        ).fetchall()
        return [dict(zip(cols, row)) for row in rows]
    finally:
        conn.close()


@_safe_handler
def _list_trusted_decisions_tool(args, **kwargs) -> str:
    # Enforce the reader gate in the handler too: registry.dispatch bypasses
    # check_fn, so schema-hiding alone would let a non-reader force-read.
    if not _reader_check():
        return json.dumps({"status": "refused", "reason": "not a ledger reader"})
    return json.dumps(list_trusted_decisions(repo=args.get("repo"), pr=args.get("pr")))


_RECORD_DECISION_SCHEMA = {
    "description": (
        "Record this agent's PR-approval decision into the immutable fleet "
        "approval-decision ledger (provenance is always agent_verified). "
        "First-write-wins per (repo, pr, commit_sha): an identical repeat is a "
        "no-op, a conflicting decision is refused."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "Repository in owner/name form."},
            "pr": {"type": "integer", "description": "Pull-request number."},
            "commit_sha": {"type": "string", "description": "The reviewed (head) commit SHA."},
            "decision": {
                "type": "string",
                "description": "The decision, e.g. WOULD_APPROVE | BLOCK | ABSTAIN_POLICY.",
            },
            "detail": {"type": "object", "description": "Optional structured evidence for the decision."},
        },
        "required": ["repo", "pr", "commit_sha", "decision"],
    },
}

_RECORD_HUMAN_SCHEMA = {
    "description": (
        "Internal: record a human PR-review verdict (provenance=human). Not "
        "model-callable — it enters only through the validated pull_request_review "
        "webhook door and is absent from every model-facing schema."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "repo": {"type": "string"},
            "pr": {"type": "integer"},
            "commit_sha": {"type": "string"},
            "decision": {"type": "string"},
            "delivery_id": {"type": "string"},
        },
        "required": ["repo", "pr", "commit_sha", "decision", "delivery_id"],
    },
}

_LIST_SCHEMA = {
    "description": (
        "Read the approval-decision ledger: return recorded decisions (optionally "
        "filtered by repo/pr), each with its provenance (agent_verified | human)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "Filter by repository (owner/name)."},
            "pr": {"type": "integer", "description": "Filter by pull-request number."},
        },
        "required": [],
    },
}


def register(ctx) -> None:
    """Register the ledger tools and the one webhook-observation hook."""
    global _CTX
    _CTX = ctx

    # The writer/reader visibility predicates are per-profile config reads; keep
    # them out of the check_fn TTL cache so a profile switch is reflected at once.
    from tools.registry import no_cache_check_fn

    no_cache_check_fn(_writer_check)
    no_cache_check_fn(_reader_check)

    ctx.register_tool(
        name="record_decision",
        toolset=_TOOLSET,
        schema=_RECORD_DECISION_SCHEMA,
        handler=_record_decision,
        check_fn=_writer_check,
        description=_RECORD_DECISION_SCHEMA["description"],
    )
    ctx.register_tool(
        name="record_human_verdict",
        toolset=_TOOLSET,
        schema=_RECORD_HUMAN_SCHEMA,
        handler=_record_human_verdict,
        check_fn=_human_check,
        description=_RECORD_HUMAN_SCHEMA["description"],
    )
    ctx.register_tool(
        name="list_trusted_decisions",
        toolset=_TOOLSET,
        schema=_LIST_SCHEMA,
        handler=_list_trusted_decisions_tool,
        check_fn=_reader_check,
        description=_LIST_SCHEMA["description"],
    )
    ctx.register_hook("pre_gateway_dispatch", _on_gateway_dispatch)
