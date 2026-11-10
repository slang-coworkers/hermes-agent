"""The conversation a session was asked in, named durably so every later turn of that session can tell.

A GitLab request reaches the orchestrator on the webhook route ``gitlab``: one session per delivery, keyed
``webhook:v2:<b64(profile, route, delivery)>`` (gateway/platforms/webhook.py ``_WebhookDeliveryIdentity``).
That key does not say WHICH issue or MR the delivery was about; the payload does (the GitLab ingress shapes
it: ``repository.full_name`` and ``gitlab.target`` {kind, iid}). A later turn in the same session, such as a kanban
wake when a chain card completes, carries no payload. So the plugin records the delivery's item once, at
ingress (``pre_gateway_dispatch``, which sees each webhook delivery once, after its authorization, and never
an internal wake), keyed by the session's chat id.

``current()`` answers, for the running turn, "which conversation was this session asked in":
  * Slack (and other chat platforms): the chat and thread from the session context;
  * the GitLab webhook route: the recorded item {project, kind, iid}, plus a merge request's ``source_branch``
    and ``head_sha`` (None when the payload did not name them) and ``review_asked`` (the ``review_bot`` setting's bot
    added as a reviewer, or a mention of it that says review), or None when nothing was recorded.
``is_own_gitlab_item(project, kind, iid)`` is the check a reply rule uses: True only for the item the session
was asked about. A chain built on it uses both: an issue session's chain key names that issue, and
progress goes back to that item.

Only the authenticated GitLab route records: the session's chat id must name the ``gitlab`` webhook route (the
loopback route the GitLab ingress edge forwards to after it verified GitLab's secret) and the payload's own
``gitlab.delivery_id`` must be that delivery (the edge stamps it; the chat id carries it), so another webhook route's
caller-controlled JSON can never choose a conversation. A second, different record for the same chat id is a
conflict: the session's origin is dropped (a tombstone), so it has no own GitLab item at all.

Rows older than ``MAX_AGE_DAYS`` (the fleet's 30-day retention) are pruned on write.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import sqlite3
import time
from typing import Any, Callable, Optional

from plugins.plugin_storage import plugin_db

logger = logging.getLogger(__name__)

_PLUGIN = "nv-fleet-gates"
MAX_AGE_DAYS = 30
GITLAB_KINDS = ("issue", "merge_request")
GITLAB_ROUTE = "gitlab"     # the webhook route the ingress edge forwards authenticated GitLab deliveries to
_MR_FIELDS = ("source_branch", "head_sha")  # what a merge request's delivery also names (gitlab.target)
_MR_COLUMNS = (("source_branch", "TEXT"), ("head_sha", "TEXT"), ("review_asked", "INTEGER"))
# The bot a review request names (nv-fleet-gates' ``review_bot`` setting, a login); none by default, so no delivery
# asks for a review.
REVIEW_BOT = ""
_MENTION_RE = None
_REVIEW_WORD_RE = re.compile(r"(?i)\breview(?:s|ing)?\b")  # review, reviews, reviewing, re-review; not reviewer
_CONFLICT = "conflict"      # the kind of a tombstone row: two different items claimed one session


def _conn():
    conn = plugin_db(_PLUGIN)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS conversation_origins ("
        "chat_id TEXT PRIMARY KEY, project TEXT NOT NULL, kind TEXT NOT NULL, iid INTEGER NOT NULL, "
        "recorded_at INTEGER NOT NULL, source_branch TEXT, head_sha TEXT, review_asked INTEGER)"
    )
    cols = {row[1] for row in conn.execute("PRAGMA table_info(conversation_origins)").fetchall()}
    for col, kind in _MR_COLUMNS:  # a table from before the merge request columns
        if col not in cols:
            try:
                conn.execute(f"ALTER TABLE conversation_origins ADD COLUMN {col} {kind}")
            except sqlite3.OperationalError as exc:  # another connection added it first
                if "duplicate column" not in str(exc).lower():
                    raise
    conn.commit()
    return conn


def configure(review_bot: str = "") -> None:
    """Set the review bot: the webhook route's mention rule for it, and the word that makes a mention a request."""
    global REVIEW_BOT, _MENTION_RE
    REVIEW_BOT = str(review_bot or "").strip().lower()
    _MENTION_RE = (re.compile(r"(?i)(^|[^a-z0-9_.-])@" + re.escape(REVIEW_BOT) + r"([^a-z0-9_-]|$)")
                   if REVIEW_BOT else None)


def review_asked(payload: Any) -> bool:
    """True when a shaped GitLab delivery asks the review bot for a review: the bot newly added as a reviewer, or the
    text that mentions it (a comment, or the description of an MR just opened or reopened) says review."""
    if not isinstance(payload, dict) or not REVIEW_BOT:
        return False
    gitlab = payload.get("gitlab") if isinstance(payload.get("gitlab"), dict) else {}
    added = gitlab.get("reviewers_added") if isinstance(gitlab.get("reviewers_added"), list) else []
    if any(isinstance(u, str) and u.strip().lower() == REVIEW_BOT for u in added):
        return True
    comment = payload.get("comment") if isinstance(payload.get("comment"), dict) else None
    pull = payload.get("pull_request") if isinstance(payload.get("pull_request"), dict) else None
    if comment is not None:
        text = comment.get("body")
    elif pull is not None and payload.get("action") in ("opened", "reopened"):
        text = pull.get("body")
    else:
        return False
    return isinstance(text, str) and bool(_MENTION_RE.search(text)) and bool(_REVIEW_WORD_RE.search(text))


def gitlab_item(payload: Any) -> Optional[dict[str, Any]]:
    """``{project, kind, iid}`` of the one item a shaped GitLab webhook payload is about (a merge request adds its
    ``source_branch`` and ``head_sha``, each None when the payload has none, and ``review_asked``), else None."""
    if not isinstance(payload, dict):
        return None
    gitlab = payload.get("gitlab") if isinstance(payload.get("gitlab"), dict) else {}
    target = gitlab.get("target") if isinstance(gitlab.get("target"), dict) else {}
    repo = payload.get("repository") if isinstance(payload.get("repository"), dict) else {}
    project, kind, iid = repo.get("full_name"), target.get("kind"), target.get("iid")
    if not isinstance(project, str) or "/" not in project or kind not in GITLAB_KINDS:
        return None
    try:
        iid = int(iid)
    except (TypeError, ValueError):
        return None
    if isinstance(target.get("iid"), bool) or iid <= 0:
        return None
    item: dict[str, Any] = {"project": project, "kind": kind, "iid": iid}
    if kind == "merge_request":
        for field in _MR_FIELDS:
            value = target.get(field)
            item[field] = value if isinstance(value, str) and value else None
        item["review_asked"] = review_asked(payload)
    return item


def record(chat_id: Any, item: Optional[dict[str, Any]], *, now: Optional[int] = None) -> bool:
    """Remember ``item`` as the conversation of session ``chat_id``. A session is one delivery, so it is asked about
    exactly one item: the same item again is a no-op, a DIFFERENT one is a conflict that drops the origin."""
    if not isinstance(chat_id, str) or not chat_id or not item:
        return False
    now = int(time.time() if now is None else now)
    conn = _conn()
    try:
        conn.execute("DELETE FROM conversation_origins WHERE recorded_at < ?", (now - MAX_AGE_DAYS * 86400,))
        row = conn.execute("SELECT project, kind, iid FROM conversation_origins WHERE chat_id = ?",
                           (chat_id,)).fetchone()
        if row is None:
            asked = item.get("review_asked")
            conn.execute("INSERT INTO conversation_origins (chat_id, project, kind, iid, recorded_at, source_branch, "
                         "head_sha, review_asked) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (chat_id, item["project"], item["kind"], int(item["iid"]), now, item.get("source_branch"),
                          item.get("head_sha"), None if asked is None else int(asked is True)))
            conn.commit()
            return True
        if (row[0], row[1], int(row[2])) != (item["project"], item["kind"], int(item["iid"])):
            logger.warning("nv-fleet-gates conversation origin: conflicting records for one session; dropping its "
                           "origin (no own GitLab item)")
            conn.execute("UPDATE conversation_origins SET project = '', kind = ?, iid = 0 WHERE chat_id = ?",
                         (_CONFLICT, chat_id))
            conn.commit()
        return False
    finally:
        conn.close()


def delivery_identity(chat_id: Any) -> Optional[tuple[str, str]]:
    """``(route, delivery id)`` of a webhook session's chat id (``webhook:v2:<b64 json [profile, route, delivery]>``,
    gateway/platforms/webhook.py ``_WebhookDeliveryIdentity``), else None."""
    if not isinstance(chat_id, str) or not chat_id.startswith("webhook:v2:"):
        return None
    token = chat_id[len("webhook:v2:"):]
    try:
        parts = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not (isinstance(parts, list) and len(parts) == 3 and all(isinstance(x, str) for x in parts)):
        return None
    return parts[1], parts[2]


def record_event(event: Any) -> bool:
    """``pre_gateway_dispatch``: record an authenticated GitLab delivery's item under its session's chat id."""
    source = getattr(event, "source", None)
    platform = getattr(getattr(source, "platform", None), "value", getattr(source, "platform", None))
    if str(platform or "") != "webhook":
        return False
    chat_id = getattr(source, "chat_id", None)
    identity = delivery_identity(chat_id)
    payload = getattr(event, "raw_message", None)
    gitlab = payload.get("gitlab") if isinstance(payload, dict) and isinstance(payload.get("gitlab"), dict) else {}
    if identity is None or identity[0] != GITLAB_ROUTE or not identity[1] or gitlab.get("delivery_id") != identity[1]:
        return False  # not the authenticated gitlab route, or the payload is not that delivery's
    return record(chat_id, gitlab_item(payload))


def lookup(chat_id: Any) -> Optional[dict[str, Any]]:
    if not isinstance(chat_id, str) or not chat_id:
        return None
    conn = _conn()
    try:
        row = conn.execute("SELECT project, kind, iid, source_branch, head_sha, review_asked FROM conversation_origins "
                           "WHERE chat_id = ?", (chat_id,)).fetchone()
    finally:
        conn.close()
    if not row or row[1] == _CONFLICT:
        return None
    item: dict[str, Any] = {"project": row[0], "kind": row[1], "iid": int(row[2])}
    if row[1] == "merge_request":
        item["source_branch"], item["head_sha"], item["review_asked"] = row[3] or None, row[4] or None, row[5] == 1
    return item


def current(env: Optional[Callable[[str, str], str]] = None) -> Optional[dict[str, Any]]:
    """The running turn's originating conversation, from the session context (None: no chat, e.g. CLI/cron)."""
    if env is None:
        from gateway.session_context import get_session_env as env
    platform, chat_id = env("HERMES_SESSION_PLATFORM", ""), env("HERMES_SESSION_CHAT_ID", "")
    if not platform or not chat_id:
        return None
    if platform == "webhook":
        return {"platform": platform, "chat_id": chat_id, "gitlab": lookup(chat_id)}
    return {"platform": platform, "chat_id": chat_id, "thread_id": env("HERMES_SESSION_THREAD_ID", "") or None}


def is_own_gitlab_item(project: str, kind: str, iid: Any,
                       env: Optional[Callable[[str, str], str]] = None) -> bool:
    """True when this session was asked about exactly this GitLab item (its own originating conversation)."""
    origin = current(env)
    item = (origin or {}).get("gitlab")
    try:
        return bool(item) and (item["project"], item["kind"], item["iid"]) == (project, kind, int(iid))
    except (TypeError, ValueError):
        return False
