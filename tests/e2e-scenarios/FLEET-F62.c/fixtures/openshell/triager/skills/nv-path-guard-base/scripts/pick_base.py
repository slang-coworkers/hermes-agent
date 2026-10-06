#!/usr/bin/env python3
"""Pick the PR base branch from a checkout's .github/nv-path-guard allowlists.

usage: pick_base.py --repo <checkout> <path> [<path> ...]

Each allowlist ``<branch>.txt`` is loaded the way the repository's path guard loads it
(blank lines and lines starting with ``#`` dropped) and evaluated with git's gitignore
rules, which the guard runs through ``check-ignore --no-index``: the last matching pattern
wins, ``!`` re-includes, and a path under a directory the patterns exclude stays excluded.
The matcher below is a port of git's wildmatch and pattern parsing, so the two agree;
it is stdlib only and reads nothing but the allowlist files.

Exit 0: the single branch whose allowlist owns every path, on stdout.
Exit 3: no branch, or more than one, owns the whole set (nothing on stdout).
Exit 2: the allowlists are missing, empty or unreadable, or a path is not a plain
repo-relative path, so the guard's answer cannot be reproduced.
"""

import argparse
import codecs
import sys
from pathlib import Path

EXIT_OK, EXIT_UNANSWERABLE, EXIT_NO_SINGLE_OWNER = 0, 2, 3
GUARD_DIR = Path(".github") / "nv-path-guard"

_MATCH, _NOMATCH, _ABORT_ALL, _ABORT_TO_STARSTAR = 0, 1, -1, -2
_SLASH, _STAR, _QMARK, _BACKSLASH = ord("/"), ord("*"), ord("?"), ord("\\")
_LBRACKET, _RBRACKET, _BANG, _CARET = ord("["), ord("]"), ord("!"), ord("^")
_DASH, _COLON, _SPACE = ord("-"), ord(":"), ord(" ")
_GLOB_SPECIAL = frozenset(b"*?[\\")


def _ascii(test):
    return frozenset(c for c in range(128) if test(c))


# git's ASCII-only character classes (its own ctype table: space is TAB, LF, CR and SP).
_CLASSES = {
    b"alnum": _ascii(lambda c: chr(c).isalnum()),
    b"alpha": _ascii(lambda c: chr(c).isalpha()),
    b"blank": frozenset(b" \t"),
    b"cntrl": _ascii(lambda c: c < 32 or c == 127),
    b"digit": _ascii(lambda c: chr(c).isdigit()),
    b"graph": _ascii(lambda c: 0x21 <= c <= 0x7E),
    b"lower": _ascii(lambda c: chr(c).islower()),
    b"print": _ascii(lambda c: 0x20 <= c <= 0x7E),
    b"punct": _ascii(lambda c: 0x21 <= c <= 0x7E and not chr(c).isalnum()),
    b"space": frozenset(b" \t\n\r"),
    b"upper": _ascii(lambda c: chr(c).isupper()),
    b"xdigit": frozenset(b"0123456789abcdefABCDEF"),
}


class Unanswerable(ValueError):
    """The guard's answer cannot be reproduced for this input; never read as "not owned"."""


def _at(buf: bytes, i: int) -> int:
    return buf[i] if 0 <= i < len(buf) else 0


def _dowild(p: bytes, pi: int, t: bytes, ti: int, pathname: bool) -> int:
    start = pi
    while pi < len(p):
        p_ch, t_ch = p[pi], _at(t, ti)
        if t_ch == 0 and p_ch != _STAR:
            return _ABORT_ALL
        if p_ch == _BACKSLASH:
            pi += 1
            if t_ch != _at(p, pi):
                return _NOMATCH
        elif p_ch == _QMARK:
            if pathname and t_ch == _SLASH:
                return _NOMATCH
        elif p_ch == _STAR:
            pi += 1
            if _at(p, pi) == _STAR:
                prev = pi - 2
                while _at(p, pi) == _STAR:
                    pi += 1
                nxt = _at(p, pi)
                if not pathname:
                    match_slash = True
                elif ((prev < start or p[prev] == _SLASH)
                        and (nxt in (0, _SLASH) or (nxt == _BACKSLASH and _at(p, pi + 1) == _SLASH))):
                    if nxt == _SLASH and _dowild(p, pi + 1, t, ti, pathname) == _MATCH:
                        return _MATCH
                    match_slash = True
                else:
                    match_slash = False
            else:
                match_slash = not pathname
            if pi >= len(p):
                if not match_slash and _SLASH in t[ti:]:
                    return _NOMATCH
                return _MATCH
            if not match_slash and p[pi] == _SLASH:
                slash = t.find(b"/", ti)
                if slash < 0:
                    return _NOMATCH
                pi, ti = pi + 1, slash + 1
                continue
            while t_ch != 0:
                lit = p[pi]
                if lit not in _GLOB_SPECIAL:
                    while True:
                        t_ch = _at(t, ti)
                        if t_ch == 0 or t_ch == lit or (not match_slash and t_ch == _SLASH):
                            break
                        ti += 1
                    if t_ch != lit:
                        return _NOMATCH
                matched = _dowild(p, pi, t, ti, pathname)
                if matched != _NOMATCH:
                    if not match_slash or matched != _ABORT_TO_STARSTAR:
                        return matched
                elif not match_slash and t_ch == _SLASH:
                    return _ABORT_TO_STARSTAR
                ti += 1
                t_ch = _at(t, ti)
            return _ABORT_ALL
        elif p_ch == _LBRACKET:
            result = _bracket(p, pi, t_ch, pathname)
            if result is None:
                return _ABORT_ALL
            if not result[0]:
                return _NOMATCH
            pi = result[1]
        elif t_ch != p_ch:
            return _NOMATCH
        pi += 1
        ti += 1
    return _NOMATCH if ti < len(t) else _MATCH


def _bracket(p: bytes, pi: int, t_ch: int, pathname: bool):
    """Parse the bracket expression opening at ``p[pi]``. Returns ``(matched, end)`` with
    ``end`` the index of its closing ``]``, or None when it is malformed (git aborts)."""
    pi += 1
    p_ch = _at(p, pi)
    negated = p_ch in (_BANG, _CARET)
    if negated:
        pi += 1
        p_ch = _at(p, pi)
    prev_ch, matched = 0, False
    while True:
        if not p_ch:
            return None
        if p_ch == _BACKSLASH:
            pi += 1
            p_ch = _at(p, pi)
            if not p_ch:
                return None
            if t_ch == p_ch:
                matched = True
        elif p_ch == _DASH and prev_ch and _at(p, pi + 1) and _at(p, pi + 1) != _RBRACKET:
            pi += 1
            p_ch = _at(p, pi)
            if p_ch == _BACKSLASH:
                pi += 1
                p_ch = _at(p, pi)
                if not p_ch:
                    return None
            if prev_ch <= t_ch <= p_ch:
                matched = True
            p_ch = 0
        elif p_ch == _LBRACKET and _at(p, pi + 1) == _COLON:
            start = pi + 2
            pi = start
            while _at(p, pi) and _at(p, pi) != _RBRACKET:
                pi += 1
            p_ch = _at(p, pi)
            if not p_ch:
                return None
            if pi - start - 1 < 0 or p[pi - 1] != _COLON:
                # No ":]": the "[" is an ordinary member of the set.
                pi = start - 2
                p_ch = _LBRACKET
                if t_ch == p_ch:
                    matched = True
            else:
                members = _CLASSES.get(p[start:pi - 1])
                if members is None:
                    return None
                if t_ch in members:
                    matched = True
                p_ch = 0
        elif t_ch == p_ch:
            matched = True
        prev_ch = p_ch
        pi += 1
        p_ch = _at(p, pi)
        if p_ch == _RBRACKET:
            break
    if matched == negated or (pathname and t_ch == _SLASH):
        return False, pi
    return True, pi


def _wildmatch(pattern: bytes, text: bytes, pathname: bool) -> bool:
    return _dowild(pattern, 0, text, 0, pathname) == _MATCH


def _simple_length(b: bytes) -> int:
    for i, c in enumerate(b):
        if c in _GLOB_SPECIAL:
            return i
    return len(b)


def _trim_trailing_spaces(b: bytes) -> bytes:
    last_space, i = None, 0
    while i < len(b):
        if b[i] == _SPACE:
            if last_space is None:
                last_space = i
        else:
            if b[i] == _BACKSLASH:
                i += 1
                if i >= len(b):
                    return b
            last_space = None
        i += 1
    return b if last_space is None else b[:last_space]


def _check_wellformed(text: bytes) -> None:
    """Refuse a pattern git could only ever abort on (a dangling escape, an unclosed or
    unknown bracket class): the guard's answer for it is not an ownership decision."""
    i = 0
    while i < len(text):
        if text[i] == _BACKSLASH:
            if i + 1 >= len(text):
                raise Unanswerable(f"pattern {text!r} ends in an escape")
            i += 2
            continue
        if text[i] == _LBRACKET:
            parsed = _bracket(text, i, 0, False)
            if parsed is None:
                raise Unanswerable(f"pattern {text!r} has a malformed bracket expression")
            i = parsed[1]
        i += 1


class _Pattern:
    __slots__ = ("text", "negative", "mustbedir", "nodir", "endswith", "prefix")

    def __init__(self, entry: bytes) -> None:
        self.negative = entry[:1] == b"!"
        p = entry[1:] if self.negative else entry
        length = len(p)
        self.mustbedir = bool(length) and p[length - 1] == _SLASH
        if self.mustbedir:
            length -= 1
        self.text = p[:length]
        self.nodir = _SLASH not in self.text
        self.prefix = min(_simple_length(p), length)
        self.endswith = p[:1] == b"*" and _simple_length(p[1:]) == len(p) - 1
        _check_wellformed(self.text)

    def matches(self, path: bytes, is_dir: bool) -> bool:
        if self.mustbedir and not is_dir:
            return False
        text, prefix = self.text, self.prefix
        if self.nodir:
            base = path.rsplit(b"/", 1)[-1]
            if prefix == len(text):
                return base == text
            if self.endswith:
                return len(text) - 1 <= len(base) and base.endswith(text[1:])
            return _wildmatch(text, base, pathname=False)
        if text[:1] == b"/":
            text, prefix = text[1:], prefix - 1
        name = path
        if prefix:
            if prefix > len(name) or name[:prefix] != text[:prefix]:
                return False
            text, name = text[prefix:], name[prefix:]
            if not text and not name:
                return True
        return _wildmatch(text, name, pathname=True)


def load_patterns(allowlist: Path) -> list:
    """The allowlist's patterns as git sees them through the guard: the guard keeps every
    line that is not blank and not a ``#`` comment and hands them to git as an excludes
    file, which then drops a BOM, skips comment lines and trims unescaped trailing spaces."""
    try:
        raw = allowlist.read_bytes()
    except OSError as exc:
        raise Unanswerable(f"cannot read {allowlist}: {exc}") from exc
    kept = [line.rstrip("\r") for line in raw.decode("utf-8", "surrogateescape").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]
    if not kept:
        raise Unanswerable(f"{allowlist} carries no patterns")
    buf = b"".join(line.encode("utf-8", "surrogateescape") + b"\n" for line in kept)
    if buf.startswith(codecs.BOM_UTF8):
        buf = buf[len(codecs.BOM_UTF8):]
    patterns = []
    for entry in buf.split(b"\n")[:-1]:
        if not entry or entry[:1] == b"#":
            continue
        if entry.endswith(b"\r"):
            entry = entry[:-1]
        if b"\0" in entry:
            raise Unanswerable(f"{allowlist} has a pattern with a NUL byte")
        patterns.append(_Pattern(_trim_trailing_spaces(entry)))
    return patterns


def _last_match(patterns: list, path: bytes, is_dir: bool):
    for pattern in reversed(patterns):
        if pattern.matches(path, is_dir):
            return pattern
    return None


def match(patterns: list, path: str) -> bool:
    """True when the allowlist owns ``path``: the last pattern matching it is not a negation,
    or a leading directory of it is owned (git cannot re-include a path whose parent
    directory is excluded)."""
    raw = check_path(path)
    parts = raw.split(b"/")
    for depth in range(1, len(parts)):
        hit = _last_match(patterns, b"/".join(parts[:depth]), True)
        if hit is not None and not hit.negative:
            return True
    hit = _last_match(patterns, raw, False)
    return hit is not None and not hit.negative


def check_path(path: str) -> bytes:
    """A plain repo-relative path, as bytes. Anything git would normalise, read as pathspec
    magic, resolve outside the repo, or find inside the guard's scratch repo is refused."""
    raw = path.encode("utf-8", "surrogateescape")
    parts = raw.split(b"/")
    if (not raw or raw[:1] == b":" or b"\0" in raw or parts[0] == b".git"
            or any(part in (b"", b".", b"..") for part in parts)):
        raise Unanswerable(f"{path!r} is not a plain repo-relative path")
    return raw


def owners(repo: Path, paths: list) -> list:
    """Every branch whose allowlist owns all of ``paths``, sorted."""
    guard = repo / GUARD_DIR
    files = sorted(p for p in guard.glob("*.txt") if p.is_file()) if guard.is_dir() else []
    if not files:
        raise Unanswerable(f"no allowlists under {guard}")
    if not paths:
        raise Unanswerable("no paths given")
    for path in paths:
        check_path(path)
    loaded = [(f.stem, load_patterns(f)) for f in files]
    return [branch for branch, patterns in loaded if all(match(patterns, path) for path in paths)]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="pick_base.py", description=__doc__.split("\n")[0])
    parser.add_argument("--repo", required=True, type=Path, help="repository checkout root")
    parser.add_argument("paths", nargs="+", help="changed or planned repo-relative paths")
    args = parser.parse_args(argv)
    try:
        found = owners(args.repo, args.paths)
    except Unanswerable as exc:
        print(f"pick_base: cannot answer: {exc}", file=sys.stderr)
        return EXIT_UNANSWERABLE
    if len(found) != 1:
        held = ", ".join(found) if found else "none"
        print(f"pick_base: no single branch owns every path (owners: {held})", file=sys.stderr)
        return EXIT_NO_SINGLE_OWNER
    print(found[0])
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
