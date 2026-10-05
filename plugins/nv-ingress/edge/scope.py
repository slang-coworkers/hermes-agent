"""Edge scope (ADR D2 Scope): which verified events of a shared repo the edge admits.

Off unless the edge config carries ``scope``. The caller answers an out-of-scope
event 202 and logs it; this module counts it per event type. PR registrations
and the counts live in ``scope.json`` beside the spool dir, so later events of a
registered PR still match after an edge restart. Stdlib only, like the edge.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

KEYS = ("repo", "ref_prefix", "label_prefix")
PR_EVENTS = ("pull_request", "pull_request_review", "pull_request_review_comment")
CI_EVENTS = ("check_run", "check_suite", "workflow_run")
_API_REPO_PATH = "/repos/"


def parse(raw: Any) -> Optional[Dict[str, str]]:
    """The validated ``scope`` rules, or None when the config has no scope."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != set(KEYS) or not all(
            isinstance(raw[k], str) and raw[k].strip() and "\n" not in raw[k] for k in KEYS):
        raise ValueError("scope must carry exactly repo, ref_prefix and label_prefix as one-line strings")
    return {k: raw[k].strip() for k in KEYS}


def _d(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _repo_of(api_url: Any) -> str:
    # A CI event's pull_requests[] entry names its head repo only by API url, not full name.
    path = urllib.parse.urlsplit(str(api_url or "")).path
    return path[len(_API_REPO_PATH):].strip("/") if path.startswith(_API_REPO_PATH) else ""


class Scope:
    """Scope rules plus the durable PR registrations and out-of-scope counts."""

    def __init__(self, rules: Dict[str, str], path: Path, write: Callable[[Path, bytes], None]):
        self.rules = rules
        self.path = path
        self._write = write
        self._lock = threading.Lock()
        try:
            state = json.loads(path.read_bytes())
        except FileNotFoundError:
            state = {}
        # A corrupt file raises here: starting empty would quietly unregister every PR.
        if state.get("rules") not in (None, rules):
            # Registrations made under other rules could admit traffic this scope excludes.
            raise ValueError(f"{path} was written for a different scope; move it aside to start fresh")
        self.counts: Dict[str, int] = {str(k): int(v) for k, v in _d(state.get("counts")).items()}
        self.prs: Dict[str, Dict[str, Any]] = {str(k): _d(v) for k, v in _d(state.get("prs")).items()}

    def counts_snapshot(self) -> Dict[str, int]:
        with self._lock:
            return dict(self.counts)

    def admit(self, env: Dict[str, Any], payload: Any) -> bool:
        """True when ``env`` is in scope. May reduce ``env["pr_numbers"]`` to the kept PRs.

        A registration is written before this returns, so before the caller spools.
        """
        with self._lock:
            ok, register = self._test(env, _d(payload))
            counts, prs = dict(self.counts), dict(self.prs)
            if ok:
                prs.update(register)
                if prs == self.prs:
                    return True
            else:
                counts[env["event"]] = counts.get(env["event"], 0) + 1
            # Persist first: memory never runs ahead of what a restart would read back.
            self._write(self.path, json.dumps({"counts": counts, "prs": prs, "rules": self.rules},
                                              sort_keys=True).encode())
            self.counts, self.prs = counts, prs
            return ok

    def _test(self, env: Dict[str, Any], payload: Dict[str, Any]) -> Tuple[bool, Dict[str, Dict[str, Any]]]:
        rules = self.rules
        if env.get("repo") != rules["repo"]:
            return False, {}
        event = env.get("event")
        branch = str(env.get("head_branch") or "")
        prefixed = branch.startswith(rules["ref_prefix"])
        if event in PR_EVENTS:
            if not prefixed or env.get("number") is None:
                return False, {}
            return True, {str(env["number"]): {"head_sha": env.get("head_sha"), "head_branch": branch}}
        labelled = any(str(name).startswith(rules["label_prefix"]) for name in env.get("labels") or [])
        if event == "issues":
            return labelled, {}
        if event == "issue_comment":
            return labelled or str(env.get("number")) in self.prs, {}
        if event not in CI_EVENTS:
            return False, {}
        nums: List[int] = list(env.get("pr_numbers") or [])
        if not nums:
            head = env.get("head_sha")
            known = any((head and pr.get("head_sha") == head) or (branch and pr.get("head_branch") == branch)
                        for pr in self.prs.values())
            return known or prefixed, {}
        admissible = self._own_prefix_prs(event, env, payload) if prefixed else {}
        kept, register = [], {}
        for n in nums:
            if str(n) in self.prs:
                kept.append(n)
            elif n in admissible:
                kept.append(n)
                register[str(n)] = admissible[n]
        if not kept:
            return False, {}
        env["pr_numbers"] = kept
        return True, register

    def _own_prefix_prs(self, event: str, env: Dict[str, Any], payload: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
        """PRs whose OWN head ref has the prefix in the scoped repo (E5); a base ref never counts."""
        out: Dict[int, Dict[str, Any]] = {}
        for item in _d(payload.get(event)).get("pull_requests") or []:
            head = _d(_d(item).get("head"))
            ref = str(head.get("ref") or "")
            try:
                number = int(_d(item).get("number"))
            except (TypeError, ValueError):
                continue
            if ref.startswith(self.rules["ref_prefix"]) and _repo_of(_d(head.get("repo")).get("url")) == self.rules["repo"]:
                out[number] = {"head_sha": head.get("sha") or env.get("head_sha"), "head_branch": ref}
        return out
