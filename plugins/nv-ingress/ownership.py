"""PR ownership: first claim wins, one lineage rule, replacement, remap (D6)."""

from __future__ import annotations

import re
import time
from typing import Any, Dict, Optional

from . import ledger, sessions

# The GitHub CLI's pr-create verb, tolerant of the argv-list form execute_code uses.
_CREATE_INTENT_RE = re.compile(r"\bgh\W+pr\W+create\b", re.IGNORECASE)
_PR_URL_RE = re.compile(r"https?://github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)", re.IGNORECASE)
_SUPERSEDES_RE = re.compile(r"\b(?:supersedes|replaces)\s+#(\d+)\b", re.IGNORECASE)


def parse_create(invocation: str, result: str) -> Optional[Dict[str, Any]]:
    """``{repo, pr, replaces}`` when a PR-create command produced a PR URL, else None."""
    if not _CREATE_INTENT_RE.search(invocation or ""):
        return None
    m = _PR_URL_RE.search(result or "")
    if not m:
        return None
    rep = _SUPERSEDES_RE.search(invocation or "")
    return {"repo": m.group(1), "pr": int(m.group(2)), "replaces": int(rep.group(1)) if rep else None}


def _row(conn, repo: str, pr: int):
    return conn.execute(
        "SELECT owner_profile, session_id, thread_id, superseded_by FROM pr_owner WHERE repo = ? AND pr = ?",
        (repo, int(pr))).fetchone()


def lineage_ok(profile: str, claimant: str, held_profile: str, held_session: str, held_thread: str) -> bool:
    """The one rule for re-claim and replacement: same profile, the claimant IS the
    held session or a verified compression continuation of it, on the held thread."""
    if profile != held_profile or not claimant:
        return False
    return (sessions.continues(profile, claimant, held_session)
            and sessions.thread_of(profile, claimant) == held_thread)


def _refuse(conn, repo: str, pr: int, holder: Optional[str], claimant: str, session: str, reason: str) -> Dict[str, Any]:
    conn.execute("INSERT INTO claim_refusals (repo, pr, holder, claimant, claimant_session, reason, at)"
                 " VALUES (?, ?, ?, ?, ?, ?, ?)", (repo, int(pr), holder, claimant, session, reason, time.time()))
    return {"status": "refused", "repo": repo, "pr": int(pr), "holder": holder, "reason": reason}


def claim(repo: str, pr: int, profile: str, session_id: str) -> Dict[str, Any]:
    """Record ownership of ``(repo, pr)`` for ``profile``/``session_id`` (first claim wins)."""
    conn = ledger.connect()
    try:
        if not session_id:
            conn.execute("INSERT INTO unclaimable (repo, pr, claimant, reason, at) VALUES (?, ?, ?, ?, ?)",
                         (repo, int(pr), profile, "no session id on the claiming call", time.time()))
            conn.commit()
            return {"status": "unclaimable", "repo": repo, "pr": int(pr)}
        conn.execute("BEGIN IMMEDIATE")
        row = _row(conn, repo, pr)
        if row is None:
            conn.execute("INSERT INTO pr_owner (repo, pr, owner_profile, session_id, thread_id, claimed_at)"
                         " VALUES (?, ?, ?, ?, ?, ?)",
                         (repo, int(pr), profile, session_id, sessions.thread_of(profile, session_id), time.time()))
            conn.commit()
            return {"status": "claimed", "repo": repo, "pr": int(pr), "owner": profile}
        holder, held_session, held_thread, _sup = row
        if lineage_ok(profile, session_id, holder, held_session, held_thread):
            tip = sessions.tip(profile, session_id)
            conn.execute("UPDATE pr_owner SET session_id = ? WHERE repo = ? AND pr = ?", (tip, repo, int(pr)))
            conn.commit()
            return {"status": "refreshed", "repo": repo, "pr": int(pr), "owner": holder}
        reason = "held by another profile" if holder != profile else "not the claimed session or its continuation"
        out = _refuse(conn, repo, pr, holder, profile, session_id, reason)
        conn.commit()
        return out
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def has_owner(repo: str, pr: int) -> bool:
    conn = ledger.connect()
    try:
        return _row(conn, repo, pr) is not None
    finally:
        conn.close()


def replace(repo: str, old_pr: int, new_pr: int, profile: str, session_id: str) -> Dict[str, Any]:
    """Claim ``new_pr`` as the replacement of ``old_pr``, inheriting its owner and thread."""
    conn = ledger.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        old = _row(conn, repo, old_pr)
        if old is None:
            out = _refuse(conn, repo, new_pr, None, profile, session_id, f"#{old_pr} has no owner to inherit")
        elif old[3] is not None:
            out = _refuse(conn, repo, new_pr, old[0], profile, session_id, f"#{old_pr} is already superseded")
        elif not lineage_ok(profile, session_id, old[0], old[1], old[2]):
            out = _refuse(conn, repo, new_pr, old[0], profile, session_id,
                          f"not the session that owns #{old_pr} or its continuation")
        elif _row(conn, repo, new_pr) is not None:
            out = _refuse(conn, repo, new_pr, _row(conn, repo, new_pr)[0], profile, session_id,
                          f"#{new_pr} is already claimed")
        else:
            conn.execute("INSERT INTO pr_owner (repo, pr, owner_profile, session_id, thread_id, claimed_at)"
                         " VALUES (?, ?, ?, ?, ?, ?)",
                         (repo, int(new_pr), old[0], sessions.tip(profile, session_id), old[2], time.time()))
            conn.execute("UPDATE pr_owner SET superseded_by = ? WHERE repo = ? AND pr = ?",
                         (int(new_pr), repo, int(old_pr)))
            out = {"status": "replaced", "repo": repo, "old_pr": int(old_pr), "pr": int(new_pr), "owner": old[0]}
        conn.commit()
        return out
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def remap(repo: str, pr: int, to: str, session_id: Optional[str]) -> Dict[str, Any]:
    """Point ``(repo, pr)`` at ``to``'s session. Callers have passed the role and approval gates."""
    from hermes_cli.profiles import normalize_profile_name, validate_profile_name

    try:
        to = normalize_profile_name(to)
        validate_profile_name(to)
    except (TypeError, ValueError):
        return {"status": "refused", "reason": "invalid target profile"}
    target = session_id or sessions.bot_chat(to)
    if not target or sessions.get(to, target) is None:
        return {"status": "refused", "reason": f"target session does not exist in profile {to!r}"}
    conn = ledger.connect()
    try:
        conn.execute(
            "INSERT INTO pr_owner (repo, pr, owner_profile, session_id, thread_id, claimed_at) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(repo, pr) DO UPDATE SET owner_profile = excluded.owner_profile,"
            " session_id = excluded.session_id, thread_id = excluded.thread_id",
            (repo, int(pr), to, target, sessions.thread_of(to, target), time.time()))
        conn.commit()
        return {"status": "remapped", "repo": repo, "pr": int(pr), "owner": to, "session_id": target}
    finally:
        conn.close()
