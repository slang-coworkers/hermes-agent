"""Technical-only text: what a shared note (a learning, a memory entry) may not carry.

A note that other coworkers read later must state technical facts only, never personal data. ``kinds(text, roster)``
names what a text holds that it may not:

  hidden      a control, format, private-use, unassigned or default-ignorable character, or a line or paragraph
              separator (newline and tab are fine): each can hide text from a check or from a reader
  slack_id    a Slack user, workspace or bot id (U..., W..., B...: 9-12 upper-case letters and digits, both present)
  mention     Slack mention syntax: <@U...>, <!subteam^...>, <!channel>, <!here>
  handle      an @handle. In code (a fenced block, or an inline `span`) a line that is only a bare decorator, a
              dotted decorator, a call and a package scope pass (@property alone, @pytest.fixture, @types/node)
  email       an email address (a git@host:group/project remote passes)
  slack_link  a Slack message permalink (https://<team>.slack.com/archives/...)
  name        a person on the roster: the exact username, or the full name (never a first name alone)
  quote       a quoted message outside code: a ``>`` quote line, or a double-quoted span over 120 characters

Every check but ``hidden`` runs on each of ``views(text)``: the text as given, NFKC-folded, with Cyrillic and Greek
look-alike letters folded to Latin, and with accents stripped, so a full-width, look-alike or accented spelling does
not get past it. A fenced block opens and closes on lines of their own; an unclosed fence is not code.

``refusal(kinds, noun)`` is the message a refused write returns: it says what to remove and asks for the technical
fact. The roster file has one person per line, ``<username><TAB><full name>``; blank lines and ``#`` comments are
skipped, a parenthetical suffix of the full name ("(Contractor)") is dropped, and a one-word full name is not matched.
``load_roster`` raises when the file is unreadable or lists nobody: a caller with a configured roster fails closed.

Stdlib only, no import of the plugin package, so other plugins and probes can load it by path. Names that are not on
the roster are not caught; error text belongs in a code span.
"""

from __future__ import annotations

import os
import re
import threading
import unicodedata
from typing import Iterable, Optional

KINDS = ("hidden", "slack_id", "mention", "handle", "email", "slack_link", "name", "quote")
_WHAT = {
    "hidden": "invisible and control characters",
    "slack_id": "Slack ids",
    "mention": "Slack mentions",
    "handle": "@handles",
    "email": "email addresses",
    "slack_link": "Slack message links",
    "name": "people's names and usernames",
    "quote": "quoted messages",
}
REFUSAL = ("Refused: {noun} are technical only. Remove {what}. State the technical fact: what failed, why, the fix, "
           "and where (a source link). Put error output in a code span.")

QUOTE_MAX = 120
_MIN_USERNAME = 3

# Unicode's Default_Ignorable_Code_Point ranges (DerivedCoreProperties), plus the blank braille pattern (So, not
# whitespace): invisible wherever they are rendered.
_IGNORABLE = ((0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160), (0x17B4, 0x17B5),
              (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E), (0x2060, 0x206F), (0x2800, 0x2800),
              (0x3164, 0x3164),
              (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF), (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3),
              (0x1D173, 0x1D17A), (0xE0000, 0xE0FFF))
_IGNORABLE_RE = re.compile("[" + "".join(re.escape(chr(a)) + "-" + re.escape(chr(b)) for a, b in _IGNORABLE) + "]")
_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cs", "Cn", "Zl", "Zp"})
# Cyrillic and Greek letters that look like Latin ones, by code point.
_CONFUSABLES = str.maketrans({
    0x0430: "a", 0x0435: "e", 0x043E: "o", 0x0440: "p", 0x0441: "c", 0x0443: "y", 0x0445: "x", 0x0456: "i",
    0x0458: "j", 0x0455: "s", 0x0501: "d", 0x0261: "g", 0x04BB: "h", 0x0410: "A", 0x0412: "B", 0x0415: "E",
    0x041A: "K", 0x041C: "M", 0x041D: "H", 0x041E: "O", 0x0420: "P", 0x0421: "C", 0x0422: "T", 0x0425: "X",
    0x0406: "I", 0x0408: "J", 0x0405: "S", 0x0391: "A", 0x0392: "B", 0x0395: "E", 0x0396: "Z", 0x0397: "H",
    0x0399: "I", 0x039A: "K", 0x039C: "M", 0x039D: "N", 0x039F: "O", 0x03A1: "P", 0x03A4: "T", 0x03A5: "Y",
    0x03A7: "X", 0x03BF: "o", 0x03B9: "i", 0x03BD: "v", 0x03C1: "p", 0x03C4: "t", 0x03C5: "u", 0x03C7: "x",
})
_LQ, _RQ = chr(0x201C), chr(0x201D)   # curly double quotes

_SLACK_ID = re.compile(r"(?<![A-Za-z0-9_])[UWB](?=[A-Z0-9]{8,11}(?![A-Za-z0-9_]))(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])"
                       r"[A-Z0-9]{8,11}(?![A-Za-z0-9_])")
_MENTION = re.compile(r"<[@!][^>\s]+>")
_HANDLE = re.compile(r"(?<![\w.+@])@\w[\w.-]*")
# Domain labels of any script (an internationalized address is still an address; \w is Unicode-aware here). The
# ASCII form below needs at least one dot; the Unicode form is checked with IDNA before it counts.
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
                    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+")
_IDN_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@(?:[^\W_](?:[\w-]*[^\W_])?\.)+[^\W_](?:[\w-]*[^\W_])?")


def _idn_email(text: str) -> bool:
    """True when ``text`` holds an address whose domain is internationalized (a non-ASCII label that IDNA accepts)."""
    for m in _IDN_EMAIL.finditer(text):
        domain = m.group(0).rsplit("@", 1)[1]
        if domain.isascii():
            continue                       # the ASCII matcher covers it
        try:
            domain.encode("idna")
        except UnicodeError:
            continue
        return True
    return False
_SSH_REMOTE = re.compile(r"(?<![\w.+-])git@[A-Za-z0-9.-]+:[\w.-]+(?:/[\w.-]+)*")
_SLACK_LINK = re.compile(r"(?i)\bhttps?://[\w.-]*slack\.com/(?:archives|app_redirect|messages|team)/\S*")
_QUOTE_LINE = re.compile(r"(?m)^[ \t]*(?:>(?!>>)|&gt;)")
_QUOTED_SPAN = re.compile(f'["{_LQ}]([^"{_LQ}{_RQ}\\n]{{{QUOTE_MAX + 1},}})["{_RQ}]')
# Code: ``` / ~~~ fenced blocks whose opening and closing fences are lines of their own, and `inline` spans. An
# unclosed fence is not code, so it cannot hide what follows.
_FENCED = re.compile(r"(?ms)^[ \t]*(```|~~~)[^\n]*\n.*?^[ \t]*\1[ \t]*$")
_INLINE = re.compile(r"`[^`\n]*`")
_BARE_DECORATOR = re.compile(r"[ \t]*@[A-Za-z_][A-Za-z0-9_]*[ \t]*")
_DOTTED_DECORATOR = re.compile(r"@[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
# A scoped package (npm: lower case, a scope and a name): "@types/node", never "@someone/" alone.
_SCOPED_PACKAGE = re.compile(r"@[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*")


def _fold(text: str) -> str:
    """Accents stripped (NFKD without combining marks) and look-alike letters folded to Latin."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).translate(
        _CONFUSABLES)


def fold(text: str) -> str:
    """The text NFKC-folded, accents stripped and look-alike letters folded to Latin: two texts that read the same
    fold to the same string (a duplicate check compares these)."""
    return _fold(unicodedata.normalize("NFKC", text))


def views(text: str) -> list[str]:
    """The text as given, NFKC-folded, with look-alike letters folded, and with accents stripped too."""
    nfkc = unicodedata.normalize("NFKC", text)
    out: list[str] = []
    for view in (text, nfkc, nfkc.translate(_CONFUSABLES), _fold(nfkc)):
        if view not in out:
            out.append(view)
    return out


def hidden(text: str) -> bool:
    """True when the text holds a character that can hide text: see ``hidden`` in the module docstring."""
    return bool(_IGNORABLE_RE.search(text)) or any(
        c not in "\n\t" and unicodedata.category(c) in _HIDDEN_CATEGORIES for c in text)


class Roster:
    """Compiled matchers for the people on the roster (each name as written and folded, see ``_fold``)."""

    def __init__(self, people: list[tuple[str, str]]):
        users, names = set(), set()
        for username, full in people:
            for form in views(username):
                if len(form) >= _MIN_USERNAME:
                    users.add(form.lower())
            plain = re.sub(r"\([^)]*\)", " ", full)
            for form in (plain, _fold(plain)):
                tokens = form.split()
                if len(tokens) >= 2:
                    names.add(tuple(t.lower() for t in tokens))
                    if len(tokens) > 2:
                        names.add((tokens[0].lower(), tokens[-1].lower()))
        self.size = len(people)
        alts = [r"\s+".join(re.escape(t) for t in toks) for toks in sorted(names, key=len, reverse=True)]
        self._names = re.compile(r"(?<!\w)(?:" + "|".join(alts) + r")(?!\w)", re.IGNORECASE) if alts else None
        ualts = sorted((re.escape(u) for u in users), key=len, reverse=True)
        self._users = (re.compile(r"(?<![\w.-])(?:" + "|".join(ualts) + r")(?![\w-]|\.\w)", re.IGNORECASE)
                       if ualts else None)

    def mentions(self, text: str) -> bool:
        return bool((self._names and self._names.search(text)) or (self._users and self._users.search(text)))


def parse_roster(text: str) -> Roster:
    """People from ``username<TAB>full name`` rows. A data row without a tab, a username or a full name makes the
    whole roster invalid (ValueError): a wrong delimiter must never pass as people with empty names."""
    people = []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        username, tab, full = line.partition("\t")
        username, full = username.strip(), full.strip()
        if not tab or not username or not full:
            raise ValueError(f"roster row {n} is not <username><TAB><full name>")
        people.append((username, full))
    return Roster(people)


_cache: dict = {}
_cache_lock = threading.Lock()


def load_roster(path: str) -> Roster:
    """The roster at ``path``, cached until the file changes. Raises OSError (unreadable) or ValueError (empty)."""
    st = os.stat(path)
    key = (path, st.st_mtime_ns, st.st_size)
    with _cache_lock:
        hit = _cache.get(path)
        if hit and hit[0] == key:
            return hit[1]
    with open(path, encoding="utf-8-sig") as fh:
        roster = parse_roster(fh.read())
    if not roster.size:
        raise ValueError("the roster lists nobody")
    with _cache_lock:
        _cache[path] = (key, roster)
    return roster


def _split_code(text: str) -> tuple[str, str]:
    """(prose, code): the text with its code blanked, and with everything but its code blanked (newlines kept)."""
    mask = [False] * len(text)
    for m in _FENCED.finditer(text):
        mask[m.start():m.end()] = [True] * (m.end() - m.start())
    unfenced = "".join(" " if k and c != "\n" else c for c, k in zip(text, mask))
    for m in _INLINE.finditer(unfenced):
        mask[m.start():m.end()] = [True] * (m.end() - m.start())
    prose = "".join(" " if k and c != "\n" else c for c, k in zip(text, mask))
    code = "".join(c if k or c == "\n" else " " for c, k in zip(text, mask))
    return prose, code


def _code_handle(code: str) -> bool:
    """An @handle in code that is not code: not a line that is only a bare decorator, a dotted decorator, a call or a
    complete scoped package name."""
    for m in _HANDLE.finditer(code):
        line_start = code.rfind("\n", 0, m.start()) + 1
        line_end = code.find("\n", m.end())
        if _BARE_DECORATOR.fullmatch(code[line_start:line_end if line_end >= 0 else len(code)]):
            continue
        if (_DOTTED_DECORATOR.fullmatch(m.group(0)) or code[m.end():m.end() + 1] == "("
                or _SCOPED_PACKAGE.match(code, m.start())):
            continue
        return True
    return False


def kinds(texts: Iterable[str] | str, roster: Optional[Roster] = None) -> list[str]:
    """The kinds of personal data in ``texts`` (one string or several), in KINDS order."""
    if isinstance(texts, str):
        texts = [texts]
    found = set()
    for text in texts:
        if not isinstance(text, str) or not text:
            continue
        if hidden(text):
            found.add("hidden")
        for view in views(text):
            prose, code = _split_code(view)
            if _SLACK_ID.search(view):
                found.add("slack_id")
            if _MENTION.search(view):
                found.add("mention")
            if _HANDLE.search(prose) or _code_handle(code):
                found.add("handle")
            if _EMAIL.search(_SSH_REMOTE.sub(" ", view)) or _idn_email(_SSH_REMOTE.sub(" ", view)):
                found.add("email")
            if _SLACK_LINK.search(view):
                found.add("slack_link")
            if roster is not None and roster.mentions(view):
                found.add("name")
            if _QUOTE_LINE.search(prose) or _QUOTED_SPAN.search(prose):
                found.add("quote")
    return [k for k in KINDS if k in found]


def refusal(found: list[str], noun: str = "notes") -> str:
    return REFUSAL.format(noun=noun, what=", ".join(_WHAT[k] for k in found if k in _WHAT) or "the personal data")
