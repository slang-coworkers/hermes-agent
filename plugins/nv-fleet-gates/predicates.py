"""Stateless predicate helpers for the nv-fleet-gates pre_tool_call chain.

Pure functions only: no DB, no config, no registry mutation. The gate closure in
``__init__`` wires these to the captured per-profile config and the fleet-shared
edges store.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

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


# Split a shell command on statement/pipe separators so a mutation or a gh-pr
# verb hiding after `&&`/`;`/`|` in a compound command is not missed.
_SEGMENT_RE = re.compile(r"\s*(?:&&|\|\||[;\n|])\s*")


def _segments(command: str):
    return [s for s in _SEGMENT_RE.split(command or "") if s.strip()]


# --- gh pr egress + PR-comment invitation (LOOP-F37-2 / -6) -----------------
_GH_PR_RE = re.compile(r"\bgh\s+pr\s+(create|review|comment)\b")


def gh_pr_egress(command: str) -> bool:
    """True if the command runs any `gh pr create|review|comment` (any segment)."""
    return bool(command) and bool(_GH_PR_RE.search(command))


def _parse_repo_pr(segment: str):
    try:
        tokens = shlex.split(segment)
    except ValueError:
        tokens = segment.split()
    repo = None
    for i, t in enumerate(tokens):
        if t == "--repo" and i + 1 < len(tokens):
            repo = tokens[i + 1]
        elif t.startswith("--repo="):
            repo = t.split("=", 1)[1]
    pr = None
    seen_comment = False
    for t in tokens:
        if not seen_comment:
            if t == "comment":
                seen_comment = True
            continue
        if t.isdigit():
            pr = int(t)
            break
    return repo, pr


def gh_pr_comment_targets(command: str):
    """One (repo, pr) per `gh pr comment` occurrence anywhere in the command.

    Uses finditer over the whole string (not one search per segment) so a
    comment nested in a command substitution — `gh pr create --body "$(gh pr
    comment 12 --repo o/r ...)"` — is still caught. Each target is invitation-
    checked; an unparseable one surfaces as (None, …), which the invitation
    predicate treats as unauthorized (fail-closed).
    """
    if not command:
        return []
    matches = list(_GH_PR_RE.finditer(command))
    out = []
    for i, m in enumerate(matches):
        if m.group(1) != "comment":
            continue
        end = matches[i + 1].start() if i + 1 < len(matches) else len(command)
        out.append(_parse_repo_pr(command[m.start():end]))
    return out


# --- mutation classification (plan gate + critique freshness) ---------------
_MUTATING_CMDS = {
    "tee", "dd", "truncate", "cp", "mv", "rm", "mkdir", "touch",
    "chmod", "chown", "ln", "install", "rsync", "shred",
}
_GIT_MUTATING = {"apply", "checkout", "reset", "clean", "rm", "mv", "restore", "stash"}

def _is_valid_python(code: str) -> bool:
    if not code or not code.strip():
        return False
    try:
        compile(code, "<execute_code>", "exec")
        return True
    except (SyntaxError, ValueError):
        return False


def _segment_mutates(seg: str) -> bool:
    if ">" in seg:  # output redirection (> or >>) writes a file
        return True
    try:
        tokens = shlex.split(seg)
    except ValueError:
        tokens = seg.split()
    if not tokens:
        return False
    idx = 0
    while idx < len(tokens):  # skip leading VAR=val assignment prefixes
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
    return cmd in _MUTATING_CMDS


def _terminal_mutates(command: str) -> bool:
    return any(_segment_mutates(seg) for seg in _segments(command))


def is_mutation(canon: str, args: dict) -> bool:
    if canon in ("write_file", "patch"):
        return True
    if canon == "terminal":
        return _terminal_mutates(command_text(canon, args))
    if canon == "execute_code":
        # execute_code runs Python: a valid script can write files, so treat any
        # syntactically-valid Python as a potential mutation. A syntax-invalid
        # payload (e.g. a bare `gh pr …` shell string, as AC-LOOP-F37-2 sends)
        # is classified by the shell rules instead.
        code = command_text(canon, args)
        if _is_valid_python(code):
            return True
        return _terminal_mutates(code)
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
    # No inner except: a detector fault must reach the gate's try/except
    # BaseException and fail closed, not be swallowed into "not dangerous".
    if not command:
        return False
    from tools.approval import detect_dangerous_command

    res = detect_dangerous_command(command)
    return bool(res[0]) if isinstance(res, tuple) else bool(res)
