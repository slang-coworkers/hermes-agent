"""ci-gate: refuse an approval unless every check run at the pinned head is green (D7).

Usage: checks_gate.py <owner/repo> <pr> <pinned_sha> --read provider|anonymous [--waive NAME ...]

Both read modes fetch the commit's check runs (paged) and the PR's current head
SHA. Neither reads the legacy combined-status endpoint. The provider mode uses
the GitHub CLI's REST passthrough (GET only), credentialed at the egress proxy.
The anonymous mode uses curl with a fixed, credential-free argv. Output is one
JSON line; exit 0 = pass, 3 = refuse.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional

API = "https://api.github.com"
PASS, REFUSE = 0, 3
PER_PAGE = 100
MAX_PAGES = 10
_TIMEOUT_SECONDS = 60


class ReadUnavailable(Exception):
    """The checks API could not be read; the gate refuses and names the operator lane."""


def evaluate(runs: List[Dict[str, Any]], pinned_sha: str, current_head: Optional[str],
             waivers: List[str]) -> Dict[str, Any]:
    """Pure verdict over the check runs read at ``pinned_sha``."""
    waived = set(waivers or [])
    refused: List[Dict[str, Any]] = []
    if current_head != pinned_sha:
        refused.append({"reason": "head moved", "pinned": pinned_sha, "current": current_head})
    if not runs:
        refused.append({"reason": "no check runs at the pinned head"})
    for run in runs:
        name = run.get("name")
        if run.get("status") != "completed":
            refused.append({"name": name, "reason": f"not completed ({run.get('status')})"})
        elif run.get("conclusion") != "success" and name not in waived:
            refused.append({"name": name, "reason": f"conclusion {run.get('conclusion')}"})
    return {"pass": not refused, "pinned_sha": pinned_sha, "refused": refused,
            "checked": sorted({str(r.get("name")) for r in runs})}


def _json_or_unavailable(proc: subprocess.CompletedProcess, what: str) -> Any:
    if proc.returncode != 0:
        raise ReadUnavailable(f"{what}: read exited {proc.returncode}")
    try:
        return json.loads(proc.stdout)
    except ValueError as exc:
        raise ReadUnavailable(f"{what}: unparsable response") from exc


def _provider_get(path: str) -> Any:
    gh = shutil.which("gh")
    if gh is None:
        raise ReadUnavailable("GitHub CLI not found")
    proc = subprocess.run([gh, "api", "-X", "GET", path], stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                          timeout=_TIMEOUT_SECONDS, check=False)
    return _json_or_unavailable(proc, path)


def _anonymous_get(path: str) -> Any:
    curl = shutil.which("curl")
    if curl is None:
        raise ReadUnavailable("curl not found")
    # -q first so no curl config file is read; no auth, netrc, cookie or write option.
    argv = [curl, "-q", "--silent", "--show-error", "--fail", "--max-time", str(_TIMEOUT_SECONDS),
            "-X", "GET", "-H", "Accept: application/vnd.github+json",
            "-H", "X-GitHub-Api-Version: 2022-11-28", f"{API}{path}"]
    proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                          timeout=_TIMEOUT_SECONDS + 5, check=False)
    return _json_or_unavailable(proc, path)


def read(repo: str, pr: int, sha: str, mode: str) -> tuple:
    get = _provider_get if mode == "provider" else _anonymous_get
    runs: List[Dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        data = get(f"/repos/{repo}/commits/{sha}/check-runs?per_page={PER_PAGE}&page={page}")
        batch = (data or {}).get("check_runs") if isinstance(data, dict) else None
        if not isinstance(batch, list):
            raise ReadUnavailable("check-runs: unexpected response shape")
        runs.extend(batch)
        if len(batch) < PER_PAGE or len(runs) >= int(data.get("total_count") or 0):
            break
    pull = get(f"/repos/{repo}/pulls/{pr}")
    head = ((pull or {}).get("head") or {}).get("sha") if isinstance(pull, dict) else None
    return runs, head


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="checks_gate.py")
    parser.add_argument("repo")
    parser.add_argument("pr", type=int)
    parser.add_argument("pinned_sha")
    parser.add_argument("--read", required=True, choices=("provider", "anonymous"))
    parser.add_argument("--waive", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        runs, head = read(args.repo, args.pr, args.pinned_sha, args.read)
    except (ReadUnavailable, OSError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"pass": False, "pinned_sha": args.pinned_sha, "read": args.read,
                          "refused": [{"reason": f"checks read unavailable (operator lane L-CHK): {exc}"}]}))
        return REFUSE
    verdict = evaluate(runs, args.pinned_sha, head, args.waive)
    verdict["read"] = args.read
    print(json.dumps(verdict))
    return PASS if verdict["pass"] else REFUSE


if __name__ == "__main__":
    sys.exit(main())
