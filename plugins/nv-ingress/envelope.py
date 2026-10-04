"""Envelope v1: the one normalized shape every inbound platform event takes.

Pure stdlib with no Hermes import, because the host edge loads this file by path
with no Hermes venv. GitHub and GitLab map to the same key set (``FIELDS``).
GitLab events are renamed into GitHub's vocabulary in ``event``, and the raw
header value is kept in ``platform_event``.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Mapping, Optional

SCHEMA = "ingress.v1"
# The forward's transport event type. It is the body ``event_type`` the release
# adapter falls back to when no platform event header is present, and the only
# event the rendered ingress route admits.
TRANSPORT_EVENT = SCHEMA

FIELDS = (
    "schema", "source", "delivery_id", "event", "platform_event", "action", "repo",
    "kind", "number", "pr_numbers", "head_branch", "head_sha", "conclusion", "name",
    "labels", "sender", "sender_type", "url", "received_at", "title", "body",
)

CI_EVENTS = ("check_run", "check_suite", "workflow_run")
ROUTED_EVENTS = tuple(sorted({
    "issues", "issue_comment", "pull_request", "pull_request_review",
    "pull_request_review_comment", *CI_EVENTS,
}))

# Header names as the release adapter reads them (gateway/platforms/webhook.py:753-754, :847).
GITHUB_EVENT_HEADER = "X-GitHub-Event"
GITHUB_DELIVERY_HEADER = "X-GitHub-Delivery"
GITLAB_EVENT_HEADER = "X-Gitlab-Event"
# GitLab's retry-stable delivery id (GitLab webhook docs, §Delivery headers).
GITLAB_ID_HEADER = "webhook-id"

_GITLAB_EVENTS = {
    "Issue Hook": "issues",
    "Merge Request Hook": "pull_request",
    "Note Hook": "issue_comment",
    "Pipeline Hook": "workflow_run",
    "Job Hook": "workflow_run",
}
_GITLAB_MR_ACTIONS = {"open": "opened", "close": "closed", "reopen": "reopened",
                      "update": "synchronize", "merge": "closed"}
_GITLAB_ISSUE_ACTIONS = {"open": "opened", "close": "closed", "reopen": "reopened",
                         "update": "edited"}
_GITLAB_CONCLUSION = {"failed": "failure", "success": "success", "canceled": "cancelled"}

_KIND = {
    "issues": "issue",
    "pull_request": "pr",
    "pull_request_review": "review",
    "pull_request_review_comment": "comment",
    "issue_comment": "comment",
    "check_run": "ci",
    "check_suite": "ci",
    "workflow_run": "ci",
}

BODY_LIMIT = 2000


def header(headers: Mapping[str, Any], name: str) -> str:
    """Case-insensitive header read; HTTP header names are case-insensitive."""
    want = name.lower()
    for key, value in (headers or {}).items():
        if str(key).lower() == want:
            return str(value)
    return ""


def _d(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _nums(items: Any) -> List[int]:
    out: List[int] = []
    for item in items if isinstance(items, list) else []:
        n = _int(_d(item).get("number"))
        if n is not None and n not in out:
            out.append(n)
    return out


def _clip(text: Any) -> Optional[str]:
    return str(text)[:BODY_LIMIT] if text else None


def _base(source: str, delivery_id: str, event: str, platform_event: str) -> Dict[str, Any]:
    env = dict.fromkeys(FIELDS)
    env.update(schema=SCHEMA, source=source, delivery_id=delivery_id, event=event,
               platform_event=platform_event, kind=_KIND.get(event, "other"),
               pr_numbers=[], labels=[], received_at=int(time.time()))
    return env


def _github(event: str, headers: Mapping[str, Any], p: Dict[str, Any]) -> Dict[str, Any]:
    env = _base("github", header(headers, GITHUB_DELIVERY_HEADER), event, event)
    sender = _d(p.get("sender"))
    env.update(action=p.get("action"), repo=_d(p.get("repository")).get("full_name"),
               sender=sender.get("login"), sender_type=sender.get("type") or "User")
    pull, issue = _d(p.get("pull_request")), _d(p.get("issue"))
    if event == "issues":
        env.update(number=_int(issue.get("number")), title=issue.get("title"),
                   url=issue.get("html_url"),
                   labels=[str(lbl.get("name")) for lbl in issue.get("labels") or [] if _d(lbl).get("name")])
        label = _d(p.get("label")).get("name")
        if label and label not in env["labels"]:
            env["labels"].append(str(label))
    elif event == "issue_comment":
        env.update(number=_int(issue.get("number")), title=issue.get("title"),
                   body=_clip(_d(p.get("comment")).get("body")),
                   url=_d(p.get("comment")).get("html_url"))
        if issue.get("pull_request") and env["number"] is not None:
            env["pr_numbers"] = [env["number"]]
        else:
            env["kind"] = "other"
    elif event in ("pull_request", "pull_request_review", "pull_request_review_comment"):
        head = _d(pull.get("head"))
        env.update(number=_int(pull.get("number")), title=pull.get("title"),
                   head_sha=head.get("sha"), head_branch=head.get("ref"),
                   url=pull.get("html_url"))
        if env["number"] is not None:
            env["pr_numbers"] = [env["number"]]
        if event == "pull_request_review":
            review = _d(p.get("review"))
            env.update(body=_clip(review.get("body")), conclusion=review.get("state"))
        elif event == "pull_request_review_comment":
            env["body"] = _clip(_d(p.get("comment")).get("body"))
    elif event in CI_EVENTS:
        run = _d(p.get(event))
        branch = run.get("head_branch") or _d(run.get("check_suite")).get("head_branch")
        env.update(head_sha=run.get("head_sha"), head_branch=branch,
                   conclusion=run.get("conclusion"), name=run.get("name"),
                   url=run.get("html_url"), pr_numbers=_nums(run.get("pull_requests")))
    return env


def _gitlab(platform_event: str, headers: Mapping[str, Any], p: Dict[str, Any]) -> Dict[str, Any]:
    event = _GITLAB_EVENTS.get(platform_event, platform_event)
    env = _base("gitlab", header(headers, GITLAB_ID_HEADER), event, platform_event)
    attrs, mr = _d(p.get("object_attributes")), _d(p.get("merge_request"))
    user = _d(p.get("user"))
    env.update(repo=_d(p.get("project")).get("path_with_namespace"),
               sender=user.get("username"),
               sender_type="Bot" if user.get("bot") else "User",
               labels=[str(lbl.get("title")) for lbl in p.get("labels") or [] if _d(lbl).get("title")])
    if platform_event == "Issue Hook":
        env.update(number=_int(attrs.get("iid")), title=attrs.get("title"), url=attrs.get("url"),
                   action=_GITLAB_ISSUE_ACTIONS.get(attrs.get("action"), attrs.get("action")))
    elif platform_event == "Merge Request Hook":
        env.update(number=_int(attrs.get("iid")), title=attrs.get("title"), url=attrs.get("url"),
                   head_branch=attrs.get("source_branch"),
                   head_sha=_d(attrs.get("last_commit")).get("id"),
                   action=_GITLAB_MR_ACTIONS.get(attrs.get("action"), attrs.get("action")))
        if env["number"] is not None:
            env["pr_numbers"] = [env["number"]]
    elif platform_event == "Note Hook":
        env.update(action="created", body=_clip(attrs.get("note")), url=attrs.get("url"))
        if mr:
            env["number"] = _int(mr.get("iid"))
            env["pr_numbers"] = [env["number"]] if env["number"] is not None else []
            env["head_branch"] = mr.get("source_branch")
        else:
            env["kind"] = "other"
    elif platform_event in ("Pipeline Hook", "Job Hook"):
        status = attrs.get("status") or p.get("build_status")
        env.update(action="completed",
                   conclusion=_GITLAB_CONCLUSION.get(status, "pending"),
                   head_sha=attrs.get("sha") or p.get("sha"),
                   head_branch=attrs.get("ref") or p.get("ref"),
                   name=attrs.get("name") or p.get("build_name"), url=attrs.get("url"))
        iid = _int(mr.get("iid"))
        env["pr_numbers"] = [iid] if iid is not None else []
    return env


def normalize(source: str, event: str, headers: Mapping[str, Any], payload: Any) -> Dict[str, Any]:
    """Map one verified platform event to envelope v1 (every key of ``FIELDS``)."""
    p = payload if isinstance(payload, dict) else {}
    if source == "gitlab":
        return _gitlab(event, headers, p)
    return _github(event, headers, p)
