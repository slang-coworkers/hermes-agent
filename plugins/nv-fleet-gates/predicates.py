"""Stateless predicate helpers for the nv-fleet-gates pre_tool_call chain.

Pure functions only: no DB, no config, no registry mutation. The gate closure in
``__init__`` wires these to the captured per-profile config and the fleet-shared
edges store.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Optional

# --- GOV-F22 (c): unclassified shell-capable tool tripwire ------------------
# The load-time guard sees only tool NAMES via registry.get_all_tool_names() —
# an unclassified tool has no entry to inspect — so a shell/exec-capable tool is
# caught by a name token. Underscores/hyphens are separators here (a bare ``\b``
# does NOT split on ``_``), so the lookarounds anchor on non-alnum boundaries.
# None of the stock v2026.8.31 tool names match (browser_exec carries only
# "exec", which is not a token), so the stock tree loads clean.
_DANGER_NAME_RE = re.compile(
    r"(?<![a-z0-9])"
    r"(?:shell|subprocess|bash|sudo|powershell|popen|spawn|command|runner|eval|cmd|script|system)"
    r"(?![a-z0-9])",
    re.IGNORECASE,
)


def looks_dangerous_name(name: str) -> bool:
    return bool(name) and bool(_DANGER_NAME_RE.search(name))


_KANBAN_TERMINATORS = {"kanban_complete", "kanban_request_review", "kanban_request_changes"}


def is_kanban_terminator(canon: str) -> bool:
    return canon in _KANBAN_TERMINATORS


def is_stage_marked(message, markers) -> bool:
    if not isinstance(message, str) or not message:
        return False
    return any(m in message for m in (markers or []))


def command_text(canon: str, args: dict) -> str:
    if not isinstance(args, dict):
        return ""
    if canon == "terminal":
        return str(args.get("command") or "")
    if canon == "execute_code":
        return str(args.get("code") or "")
    return ""


class GhPr:
    __slots__ = ("verb", "repo", "pr")

    def __init__(self, verb, repo, pr):
        self.verb = verb
        self.repo = repo
        self.pr = pr


_GH_PR_RE = re.compile(r"\bgh\s+pr\s+(create|review|comment)\b")


def parse_gh_pr(command: str) -> Optional[GhPr]:
    if not command:
        return None
    m = _GH_PR_RE.search(command)
    if not m:
        return None
    verb = m.group(1)
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    repo = None
    for i, t in enumerate(tokens):
        if t == "--repo" and i + 1 < len(tokens):
            repo = tokens[i + 1]
        elif t.startswith("--repo="):
            repo = t.split("=", 1)[1]
    pr = None
    seen_verb = False
    for t in tokens:
        if not seen_verb:
            if t == verb:
                seen_verb = True
            continue
        if t.isdigit():
            pr = int(t)
            break
    return GhPr(verb, repo, pr)


_MUTATING_CMDS = {
    "tee", "dd", "truncate", "cp", "mv", "rm", "mkdir", "touch",
    "chmod", "chown", "ln", "install", "rsync", "shred",
}
_GIT_MUTATING = {"apply", "checkout", "reset", "clean", "rm", "mv", "restore", "stash"}


def _terminal_mutates(command: str) -> bool:
    if not command:
        return False
    if ">" in command:  # output redirection (> or >>) writes a file
        return True
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return False
    # skip leading VAR=val assignment prefixes
    idx = 0
    while idx < len(tokens):
        t = tokens[idx]
        head = t.split("=", 1)[0]
        if "=" in t and not t.startswith("-") and "/" not in head and head.isidentifier():
            idx += 1
        else:
            break
    if idx >= len(tokens):
        return False
    cmd = Path(tokens[idx]).name
    rest = tokens[idx + 1:]
    if cmd == "sed" and any(t == "-i" or t.startswith("-i") for t in rest):
        return True
    if cmd == "perl" and any(t.startswith("-i") for t in rest):
        return True
    if cmd == "git" and rest and rest[0] in _GIT_MUTATING:
        return True
    if cmd in _MUTATING_CMDS:
        return True
    return False


def is_mutation(canon: str, args: dict) -> bool:
    if canon in ("write_file", "patch"):
        return True
    if canon in ("terminal", "execute_code"):
        return _terminal_mutates(command_text(canon, args))
    return False


def target_path(canon: str, args: dict):
    if not isinstance(args, dict):
        return None
    if canon in ("write_file", "patch"):
        return args.get("path") or args.get("file_path")
    return None


_PLAN_RE = re.compile(r"(^|/)\.hermes/plans/[^/]+\.md$")


def is_plan_path(path) -> bool:
    if not isinstance(path, str) or not path:
        return False
    return bool(_PLAN_RE.search(path.replace("\\", "/")))


def within(child: Path, parent: Path) -> bool:
    """True if *child* is *parent* or lives under it (symlinks resolved)."""
    try:
        child = Path(child).resolve()
        parent = Path(parent).resolve()
    except (OSError, RuntimeError):
        return False
    return child == parent or parent in child.parents


def is_dangerous_command(command: str) -> bool:
    if not command:
        return False
    try:
        from tools.approval import detect_dangerous_command

        res = detect_dangerous_command(command)
        return bool(res[0]) if isinstance(res, tuple) else bool(res)
    except Exception:
        return False
