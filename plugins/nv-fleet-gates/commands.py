"""Which commands a tool call's text runs: shell and Python text read into argv lists, with data kept apart.

A write tripwire and the OUTBOUND policy both ask "does this call run a git push / a forge CLI write (a tool
named in the ``forge_tools`` setting) / an HTTP client, and with which arguments". Searching the raw text for words answers wrongly both ways (``git log
--grep='git push'`` is not a push; a comment body that quotes a CLI example is not a second call), so this module
reads the text the way a shell (or Python) would and returns the argv of every command in EXECUTABLE position:

  * shell: quote-aware segments (``;``, ``&&``, ``||``, ``|``, ``&``, newlines), wrappers skipped (VAR=val, env,
    command, exec, sudo, timeout, nice, xargs, ...), ``bash -c '<code>'`` / ``python3 -c '<code>'`` / ``eval`` and
    the bodies of ``$(...)``, backticks and ``<(...)`` read recursively, a heredoc read as code only when the
    command it feeds is a shell or Python reading stdin (``bash <<EOF``, ``python3 - <<'PY'``); any other heredoc,
    and every argument (``--body``, ``-m``), is data;
  * Python: string-constant token lists (``["git", "push", ...]``), ``subprocess``/``os.system`` calls (a string
    run with a shell is read as shell), and a string constant that starts like a command (``"git push ..."``,
    fail closed: it may be run later);
  * raw (text that does not parse): every piece between separators, split naively (fail closed).

It reads text; it cannot follow what the text computes: a tripwire, not a boundary.
Stdlib only.
"""

from __future__ import annotations

import ast
import functools
import re
import shlex
from pathlib import PurePosixPath
from typing import Optional

SHELL_PARSE_MAX = 200_000  # longer text is read raw (no exemptions): the fail-closed direction
_MAX_DEPTH = 4
PIECE_SPLIT_RE = re.compile(r"[\n;&|]")
# The parser's own limits (a too-deep or too-large source) are "not parseable", not a scanner fault.
UNPARSEABLE = (SyntaxError, ValueError, MemoryError, RecursionError)

_SHELL_KEYWORDS = frozenset({"do", "then", "else", "elif", "if", "while", "until", "!", "{", "(", "time"})
_EXEMPT_ENV_RE = re.compile(r"(?:NV_GITLAB_\w*|GITLAB_\w*|PYTHONPATH|LANG|LC_\w+|TZ|NO_COLOR|TERM|COLUMNS)")
_ASSIGN_RE = re.compile(r"([A-Za-z_]\w*)=")
PYTHON_RE = re.compile(r"python(?:\d+(?:\.\d+)*)?")
_PY_ARG_FLAGS = frozenset({"-W", "-X", "-Q"})
_GIT_ARG_OPTS = frozenset({"-C", "-c", "--exec-path", "--git-dir", "--work-tree", "--namespace", "--super-prefix",
                           "--config-env", "--list-cmds", "--attr-source"})
_GIT_MESSAGE_CMDS = frozenset({"commit", "tag"})
_SUBST_RE = re.compile(r"\$\(|`|<\(|>\(")
_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "ash", "mksh"})
_WRAPPER_VALUE_OPTS = {
    "env": {"-u", "--unset", "-C", "--chdir"},
    "sudo": {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "--user", "--group"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p"},
    "stdbuf": {"-i", "-o", "-e"},
    "xargs": {"-I", "-n", "-P", "-d", "-a", "-E", "-L", "-s", "--arg-file", "--delimiter", "--max-args",
              "--max-procs", "--replace"},
}
_WRAPPERS_PLAIN = frozenset({"command", "exec", "builtin", "nohup", "time", "doas", "setsid", "unbuffer", "chronic"})
# A Python string constant that starts like a command we classify (it may be run later: fail closed).
_COMMANDISH_WORDS = (r"git(?:-push|-send-pack)?", "glab", "hermes", "curl", "wget", "xh", r"python[\d.]*", "bash", "sh")
# The forge CLIs (``forge_tools``) and their ``python3 -m`` module names; none unless configure() names one.
FORGE_TOOLS: frozenset = frozenset()
_FORGE_MODULES: frozenset = frozenset()


def _commandish_re(tools) -> re.Pattern:
    words = (*map(re.escape, sorted(tools, key=len, reverse=True)), *_COMMANDISH_WORDS)
    return re.compile(r"\s*(?:[A-Za-z_]\w*=\S*\s+)*(?:\S*/)?(?:" + "|".join(words) + r")(?=\s|$)")


_COMMANDISH_RE = _commandish_re(())


def configure(forge_tools=()) -> None:
    """Name the forge CLIs whose invocations are read as forge calls (nv-fleet-gates' ``forge_tools`` setting)."""
    global FORGE_TOOLS, _FORGE_MODULES, _COMMANDISH_RE
    tools = frozenset(str(t) for t in forge_tools if str(t).strip())
    FORGE_TOOLS = tools
    _FORGE_MODULES = frozenset(t.replace("-", "_") for t in tools)
    _COMMANDISH_RE = _commandish_re(tools)
_SHELL_CALLS = frozenset({"system", "popen", "getoutput", "getstatusoutput", "create_subprocess_shell"})
_EXEC_CALLS = frozenset({"run", "call", "check_call", "check_output", "Popen", "create_subprocess_exec"})


# --------------------------------------------------------------------------- shell segmentation
class Seg:
    """One simple command of shell text."""

    __slots__ = ("bodies", "head", "plain", "subst", "substs", "text")

    def __init__(self) -> None:
        self.text: list[str] = []    # the segment's command text (line continuations removed)
        self.head = ""               # the same, as a string (without heredoc bodies)
        self.subst = False           # a command / process substitution that runs code
        self.substs: list[str] = []  # the code inside each top-level $(...), `...`, <(...), >(...)
        self.bodies: list[str] = []  # heredoc bodies the segment feeds
        self.plain = True            # runs unconditionally in this shell: not after && / ||, not in a pipeline or &


@functools.lru_cache(maxsize=8)
def shell_segments(text: str) -> Optional[list[Seg]]:
    """Split shell text into simple commands, quote-aware, with heredoc bodies attached to their command.

    Returns None when the text does not parse (unbalanced quotes or substitutions): the caller then reads it
    whole, with no exemption. Cached (the tripwire and the argv reader split the same text); callers must not
    change the result.
    """
    return _Splitter(text).run()


@functools.lru_cache(maxsize=4096)
def basename(path: str) -> str:
    """The last component of a path (``PurePosixPath(path).name``), cached: it runs once per word."""
    return PurePosixPath(path).name


class _Splitter:
    """One pass over shell text: quotes, substitutions, comments, heredocs and command separators."""

    def __init__(self, text: str) -> None:
        self.text, self.n, self.i = text, len(text), 0
        self.segs: list[Seg] = []
        self.cur = Seg()
        self.pending: list[tuple[Seg, str, bool, bool]] = []  # heredocs of this line: (owner, delim, -, quoted)
        self.single = self.double = self.backtick = False
        self.depth = 0          # $( / <( / >( nesting
        self.sub_start = -1     # where the outermost substitution's code starts
        self.tick_start = -1    # where the open backtick's code starts

    def run(self) -> Optional[list[Seg]]:
        while self.i < self.n:
            c, nxt = self.text[self.i], self.text[self.i + 1:self.i + 2]
            if self.single:
                self.single = c != "'"
                self._emit(c, 1)
            elif c == "\\":
                self._escape(nxt)
            elif self.double:
                self._in_double(c, nxt)
            elif not (self._quote_or_subst(c, nxt) or self._comment(c) or self._top_level(c, nxt)):
                self._emit(c, 1)
        if self.single or self.double or self.depth or self.backtick:
            return None
        self._close()
        if self.pending:
            _consume_heredocs(self.text, self.n, self.pending)
        return self.segs

    def _emit(self, s: str, step: int) -> None:
        self.cur.text.append(s)
        self.i += step

    def _prev(self) -> str:
        return self.cur.text[-1][-1:] if self.cur.text else ""

    def _close(self) -> None:
        cur = self.cur
        cur.head = "".join(cur.text)
        if cur.head.strip() or cur.bodies:
            self.segs.append(cur)
        self.cur = Seg()

    def _subst(self, s: str) -> None:
        self.cur.subst = True
        if self.depth == 0 and not self.backtick:
            self.sub_start = self.i + 2
        self.depth += 1
        self._emit(s, 2)

    def _close_paren(self) -> None:
        self.depth -= 1
        if self.depth == 0 and self.sub_start >= 0:
            self.cur.substs.append(self.text[self.sub_start:self.i])
            self.sub_start = -1

    def _backtick(self) -> None:
        self.cur.subst = True
        if self.depth:
            self.backtick = not self.backtick
            return
        if not self.backtick:
            self.tick_start = self.i + 1
        else:
            self.cur.substs.append(self.text[self.tick_start:self.i])
        self.backtick = not self.backtick

    def _escape(self, nxt: str) -> None:
        if nxt == "\n":  # line continuation
            self.i += 2
        else:
            self._emit("\\" + nxt, 2)

    def _in_double(self, c: str, nxt: str) -> None:
        if c == "$" and nxt == "(":
            self._subst("$(")
            return
        if c == '"':
            self.double = False
        elif c == "`":
            self._backtick()
        elif c == ")" and self.depth:
            self._close_paren()
        self._emit(c, 1)

    def _quote_or_subst(self, c: str, nxt: str) -> bool:
        if c in "$<>" and nxt == "(":
            self._subst(c + "(")
            return True
        if c == "'":
            self.single = True
        elif c == '"':
            self.double = True
        elif c == "`":
            self._backtick()
        elif c == "(" and self.depth:
            self.depth += 1
        elif c == ")" and self.depth:
            self._close_paren()
        else:
            return False
        self._emit(c, 1)
        return True

    def _comment(self, c: str) -> bool:
        if c != "#" or self._prev() not in ("", " ", "\t", "\n", ";", "&", "|", "("):
            return False
        j = self.text.find("\n", self.i)
        j = self.n if j < 0 else j
        self._emit(self.text[self.i:j], j - self.i)
        return True

    def _top_level(self, c: str, nxt: str) -> bool:
        """Heredocs and command separators, outside quotes and substitutions."""
        if self.depth or self.backtick:
            return False
        if c == "<":
            return self._heredoc_op(nxt)
        if c == "\n":
            self._close()
            self.i += 1
            if self.pending:
                self.i = _consume_heredocs(self.text, self.i, self.pending)
                self.pending = []
            return True
        if c == ";" or (c == "&" and nxt == "&"):
            self._close()
            self.cur.plain = c == ";"  # the right side of && runs only sometimes
            self.i += 1 if c == ";" else 2
            return True
        if c == "&" and not (self._prev() in (">", "<") or nxt == ">"):  # not a redirect (2>&1, &>file)
            self.cur.plain = False  # a background job runs in a subshell
            self._close()
            self.i += 1
            return True
        if c == "|" and self._prev() != ">":  # not a clobber redirect (>|)
            if nxt != "|":
                self.cur.plain = False  # a pipeline element runs in a subshell
            self._close()
            self.cur.plain = False      # the right side of || runs only sometimes; of |, in a subshell
            self.i += 2 if nxt in ("|", "&") else 1
            return True
        return False

    def _heredoc_op(self, nxt: str) -> bool:
        if self.text.startswith("<<<", self.i):  # here-string, not a heredoc
            self._emit("<<<", 3)
            return True
        hd = _heredoc(self.text, self.i) if nxt == "<" else None
        if not hd:
            return False
        delim, strip_tabs, quoted, end = hd
        self.pending.append((self.cur, delim, strip_tabs, quoted))
        self._emit(self.text[self.i:end], end - self.i)
        return True


_HEREDOC_WORD_RE = re.compile(r"<<(-)?[ \t]*((?:'[^'\n]*'|\"[^\"\n]*\"|\\.|[^\s;&|<>()])+)")


def _heredoc(text: str, i: int):
    m = _HEREDOC_WORD_RE.match(text, i)
    if not m:
        return None
    word = m.group(2)
    quoted = any(q in word for q in ("'", '"', "\\"))
    delim = re.sub(r"\\(.)", r"\1", word).replace("'", "").replace('"', "")
    return delim, bool(m.group(1)), quoted, m.end()


def _consume_heredocs(text: str, i: int, pending) -> int:
    for owner, delim, strip_tabs, quoted in pending:
        lines = []
        while i < len(text):
            j = text.find("\n", i)
            j = len(text) if j < 0 else j
            line = text[i:j]
            i = j + 1
            if (line.lstrip("\t") if strip_tabs else line) == delim:
                break
            lines.append(line)
        body = "\n".join(lines)
        if not quoted and _SUBST_RE.search(body):
            owner.subst = True
        owner.bodies.append(body)
    return i


def seg_tokens(head: str) -> list[str]:
    try:
        return shlex.split(head, comments=True)
    except ValueError:
        return []


# --------------------------------------------------------------------------- exemptions (the tripwire's)
def _command_index(toks: list[str]) -> Optional[int]:
    """Index of the command word after shell keywords, VAR=val prefixes and env/command/exec/timeout wrappers.

    None when a prefix is one the exemption does not allow (e.g. GIT_*=, PATH=), or there is no command.
    """
    i = 0
    while i < len(toks) and toks[i] in _SHELL_KEYWORDS:
        i += 1
    while i < len(toks):
        t = toks[i]
        m = _ASSIGN_RE.match(t)
        if m:
            if not _EXEMPT_ENV_RE.fullmatch(m.group(1)):
                return None
            i += 1
        elif t in ("env", "command", "exec"):
            i += 1
        elif t == "timeout" and i + 1 < len(toks) and re.fullmatch(r"\d+(?:\.\d+)?[smhd]?", toks[i + 1]):
            i += 2
        else:
            return i
    return None


def _python_forge_rest(args: list[str]) -> Optional[list[str]]:
    """The forge arguments of ``python3 [flags] -m <forge module> ...`` / ``python3 [flags] <path>/<forge tool>
    ...``, else None."""
    j = 0
    while j < len(args):
        t = args[j]
        if t == "-m":
            if j + 1 < len(args) and args[j + 1].removesuffix(".__main__") in _FORGE_MODULES:
                return args[j + 2:]
            return None
        if t in ("-c", "-"):
            return None
        if t in _PY_ARG_FLAGS:
            j += 2
        elif t.startswith("-"):
            j += 1
        else:
            return args[j + 1:] if _is_forge_script(t) else None
    return None


def _is_forge_script(path: str) -> bool:
    """``.../bin/<forge tool>``, ``.../<forge module>/__main__.py`` or the package directory ``.../<forge module>``."""
    pp = PurePosixPath(path.rstrip("/"))
    return pp.name in FORGE_TOOLS | _FORGE_MODULES or (pp.name == "__main__.py" and pp.parent.name in _FORGE_MODULES)


def _git_message(args: list[str]) -> bool:
    """git [-C dir ...] commit|tag ...: the message is text. Any -c / --config-env loses the exemption."""
    j = 0
    while j < len(args) and args[j].startswith("-"):
        if args[j] == "-c" or args[j].startswith("--config-env"):
            return False
        j += 2 if args[j] in _GIT_ARG_OPTS else 1
    return j < len(args) and args[j] in _GIT_MESSAGE_CMDS


def exemption(seg: Seg) -> Optional[str]:
    """``"forge"`` for a forge CLI invocation, ``"git-message"`` for a git commit/tag (message text), ``"read"``
    for a read-only command (grep, rg, cat, head, tail, less, wc, ``sed -n``, ``git grep|log|show|diff|blame``)
    that writes no file, else None.

    Only when nothing inside the segment runs code (a command / process substitution, an unquoted heredoc that
    holds one).
    """
    if seg.subst:
        return None
    toks = seg_tokens(seg.head)
    i = _command_index(toks)
    if i is None:
        return None
    cmd, rest = basename(toks[i]), toks[i + 1:]
    if cmd in FORGE_TOOLS or (PYTHON_RE.fullmatch(cmd) and _python_forge_rest(rest) is not None):
        return "forge"
    if cmd == "git" and _git_message(rest):
        return "git-message"
    return "read" if _read_only(cmd, rest, seg.head) else None


_READ_CMDS = frozenset({"grep", "egrep", "fgrep", "rg", "ag", "cat", "head", "tail", "less", "more", "wc", "nl"})
_GIT_READ_CMDS = frozenset({"grep", "log", "show", "diff", "blame", "status"})


def _read_only(cmd: str, rest: list[str], head: str) -> bool:
    if any(unq and _REDIRECT_RE.fullmatch(w) and ">" in w for w, unq in (words(head) or [])):
        return False  # writes a file
    if cmd in _READ_CMDS:
        return True
    if cmd == "sed":
        return "-n" in rest and not any(t.startswith("-i") or t.startswith("--in-place") for t in rest)
    if cmd == "git":
        call = git_call(["git"] + rest)
        return bool(call) and call[0] in _GIT_READ_CMDS and not any(g == "-c" for g in call[2])
    return False


# --------------------------------------------------------------------------- Python
@functools.lru_cache(maxsize=4)
def _tree(code: str) -> Optional[ast.AST]:
    """The parsed module, or None when the text is not Python (or too deep to parse). Cached: the same text is
    asked about several times per call; callers must not change the tree."""
    try:
        return ast.parse(code)
    except UNPARSEABLE:
        return None


def is_python(code: str) -> bool:
    # not Python, or too deep to parse: read it as shell / raw text instead
    return bool(code.strip()) and _tree(code) is not None


def python_lists(code: str) -> tuple[list[str], list[str]]:
    """(each list/tuple of string constants joined by blanks, every string constant) of Python code."""
    tree = _tree(code)
    if tree is None:
        return [], []
    lists: list[str] = []
    strings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            toks = [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if toks:
                lists.append(" ".join(toks))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            strings.append(node.value)
    return lists, strings


# --------------------------------------------------------------------------- argv extraction
def runs(text: str, mode: str) -> list[list[str]]:
    """The argv of every command the text runs (``mode``: shell, python or raw), recursively."""
    text = text.replace("\r\n", "\n")
    if mode == "python":
        return _python_runs(text, 0)
    if mode == "shell":
        return _shell_runs(text, 0)
    return _raw_runs(text)


def _split(piece: str) -> list[str]:
    try:
        return shlex.split(piece, comments=True)
    except ValueError:
        return piece.split()


_REDIRECT_RE = re.compile(r"(?:\d*|&)(?:>>?|>&|>\||<<<|<<-?|<&|<>|<)(.*)", re.DOTALL)


def words(head: str) -> Optional[list[tuple[str, bool]]]:
    """Shell words of one command, each with whether it starts unquoted (only such a word can be a redirection:
    ``--body '<b>x</b>'`` is an argument). Quotes and backslashes are removed as the shell does; an unquoted ``#``
    at a word start ends the line. None when the quotes do not balance."""
    out: list[tuple[str, bool]] = []
    word: list[str] = []
    started = unquoted_start = False
    i, n = 0, len(head)
    while i < n:
        c = head[i]
        if c in " \t\n":
            if started:
                out.append(("".join(word), unquoted_start))
                word, started = [], False
            i += 1
            continue
        if not started:
            if c == "#":
                break
            started, unquoted_start = True, c not in "'\""
        if c == "'":
            j = head.find("'", i + 1)
            if j < 0:
                return None
            word.append(head[i + 1:j])
            i = j + 1
        elif c == '"':
            i += 1
            while i < n and head[i] != '"':
                if head[i] == "\\" and i + 1 < n and head[i + 1] in '"\\$`\n':
                    i += 1
                word.append(head[i])
                i += 1
            if i >= n:
                return None
            i += 1
        elif c == "\\" and i + 1 < n:
            word.append(head[i + 1])
            i += 2
        else:
            word.append(c)
            i += 1
    if started:
        out.append(("".join(word), unquoted_start))
    return out


def argv_of(head: str) -> list[str]:
    """The arguments of one command without its redirections (``> f``, ``2>&1``, ``<<EOF``, ``<<< 'x'``): they are
    not arguments (an argparse schema would read a heredoc marker as one)."""
    ws = words(head)
    if ws is None:
        return head.split()
    out, i = [], 0
    while i < len(ws):
        word, unquoted = ws[i]
        m = _REDIRECT_RE.fullmatch(word) if unquoted else None
        if m:
            i += 1 if m.group(1) else 2  # the target is attached, or it is the next word
            continue
        out.append(word)
        i += 1
    return out


def _raw_runs(text: str) -> list[list[str]]:
    out = []
    for piece in PIECE_SPLIT_RE.split(text.replace("\\\n", " ")):
        argv = unwrap(argv_of(piece))
        if argv:
            out.append(argv)
    return out


_DECLARE = frozenset({"export", "local", "declare", "readonly", "typeset"})
_VAR_WORD_RE = re.compile(r"\$\{?([A-Za-z_]\w*)\}?")


def _shell_runs(text: str, depth: int, scope: Optional[dict] = None) -> list[list[str]]:
    """``scope`` carries the variables and aliases this call assigns (``G="forge --project p"; $G ...``,
    ``alias gf=forge; gf ...``), so a command word that names one is read as what it expands to."""
    if depth > _MAX_DEPTH or len(text) > SHELL_PARSE_MAX:
        return _raw_runs(text)
    segs = shell_segments(text)
    if segs is None:
        return _raw_runs(text)
    scope = scope if scope is not None else {"vars": {}, "aliases": {}, "text": text}
    out: list[list[str]] = []
    block = 0  # if / while / until / for / case / { ... } nesting: an assignment inside runs only sometimes
    for seg in segs:
        argv = argv_of(seg.head)
        first = argv[0] if argv else ""
        if first in _BLOCK_OPEN or (first.endswith("()") and "{" in argv) or (first == "function" and "{" in argv):
            block += 1
        _remember(argv, scope, straight=seg.plain and block == 0)
        if first in _BLOCK_CLOSE:
            block = max(0, block - 1)
        out += _argv_runs(_expand_args(_expand(unwrap(argv), scope), scope), seg.bodies, depth, scope)
        for inner in seg.substs:  # a subshell: it sees this shell's variables, its own assignments stay inside
            out += _shell_runs(inner, depth + 1, _child(scope))
    return out


_BLOCK_OPEN = frozenset({"if", "while", "until", "for", "case", "select", "{", "(", "[[", "(("})
_BLOCK_CLOSE = frozenset({"fi", "done", "esac", "}", ")"})


def _child(scope: Optional[dict]) -> Optional[dict]:
    if scope is None:
        return None
    return {"vars": dict(scope["vars"]), "aliases": dict(scope["aliases"]), "text": scope["text"],
            "names": scope.get("names")}


def _bound_once(name: str, scope: dict) -> bool:
    """The call names ``name`` exactly once other than as ``$name`` / ``${name}``: that one is the assignment we
    read. Any other binding (a second assignment, ``for name in``, ``read name``, ``printf -v name``, ``let``,
    ``unset``, ``name+=``, an ``eval`` string) makes the value unknowable from the text."""
    names = scope.get("names")
    if names is None:  # one pass over the call's text, however many assignments it holds
        text, names = scope["text"], {}
        for m in _NAME_RE.finditer(text):
            if text[max(0, m.start() - 2):m.start()] != "${":
                names[m.group(0)] = names.get(m.group(0), 0) + 1
        scope["names"] = names
    return names.get(name, 0) == 1


_NAME_RE = re.compile(r"(?<![\w$])[A-Za-z_]\w*(?!\w)")


def _remember(argv: list[str], scope: dict, straight: bool = True) -> None:
    """Record ``NAME=value`` assignments (bare or after export/local/declare) and ``alias name=value``.

    A value is known only for a STRAIGHT-LINE assignment: the segment runs unconditionally in this shell (not after
    ``&&`` / ``||``, not in a pipeline, a background job or a block), and nothing else in the call binds the name.
    Otherwise the name is recorded as unknown (None): its uses stay unresolved, and an unresolved forge
    argument fails closed. A variable from an earlier call (a persistent shell) is never known.
    """
    words_ = argv[1:] if argv and argv[0] in _DECLARE else argv
    if words_ and all(_ASSIGN_RE.match(w) for w in words_):
        for w in words_:
            name, _, value = w.partition("=")
            known = straight and name not in scope["vars"] and _bound_once(name, scope)
            scope["vars"][name] = value if known else None
    elif argv and argv[0] == "alias":
        for w in argv[1:]:
            name, eq, value = w.partition("=")
            if eq:
                scope["aliases"][name] = value if name not in scope["aliases"] else None


def _expand(argv: list[str], scope: dict) -> list[str]:
    """A command word that is a variable this call assigned, or an alias it defined, replaced by its words."""
    for _ in range(4):
        if not argv:
            return argv
        m = _VAR_WORD_RE.fullmatch(argv[0])
        if m and scope["vars"].get(m.group(1)) is not None:
            argv = unwrap(scope["vars"][m.group(1)].split()) + argv[1:]
        elif scope["aliases"].get(argv[0]) is not None:
            argv = unwrap(scope["aliases"][argv[0]].split()) + argv[1:]
        else:
            return argv
    return argv


_SIMPLE_VALUE_RE = re.compile(r"[^\s$`\\'\"]*")


def _expand_args(argv: list[str], scope: dict) -> list[str]:
    """``$NAME`` / ``${NAME}`` inside an argument replaced by a KNOWN one-word value (``P=group/project; N=7;
    forge --project "$P" mr get "$N"``). Anything else (an unknown or computed value, one that would split)
    stays literal: the forge parser then refuses it and the call counts as an opaque write (fail closed)."""
    if len(argv) < 2 or not any("$" in a for a in argv[1:]):
        return argv

    def sub(m: re.Match) -> str:
        value = scope["vars"].get(m.group(1) or m.group(2))
        return value if value is not None and _SIMPLE_VALUE_RE.fullmatch(value) else m.group(0)

    return argv[:1] + [_VAR_REF_RE.sub(sub, a) if "$" in a else a for a in argv[1:]]


_VAR_REF_RE = re.compile(r"\$(?:\{([A-Za-z_]\w*)\}|([A-Za-z_]\w*))")


def unwrap(toks: list[str]) -> list[str]:
    """The command and its arguments, past shell keywords, VAR=val prefixes and wrappers (env, sudo, timeout...)."""
    i = 0
    while i < len(toks):
        t = toks[i]
        base = basename(t)
        if t in _SHELL_KEYWORDS or _ASSIGN_RE.match(t):
            i += 1
        elif base in _WRAPPER_VALUE_OPTS or base in _WRAPPERS_PLAIN:
            i += 1
            takes_value = _WRAPPER_VALUE_OPTS.get(base, set())
            while i < len(toks) and (toks[i].startswith("-") or (base == "env" and _ASSIGN_RE.match(toks[i]))):
                i += 2 if toks[i] in takes_value else 1
            if base == "timeout" and i < len(toks) and re.fullmatch(r"\d+(?:\.\d+)?[smhd]?", toks[i]):
                i += 1
        else:
            break
    return toks[i:]


def _argv_runs(argv: list[str], bodies: list[str], depth: int, scope: Optional[dict] = None) -> list[list[str]]:
    if not argv:
        return []
    base = basename(argv[0])
    out = [argv]
    if base == "function" or argv[0].endswith("()"):  # a function definition: its body runs when it is called
        rest = argv[2:] if base == "function" else argv[1:]
        rest = [w for w in rest if w not in ("{", "}", "()")]
        return out + _argv_runs(unwrap(rest), [], depth, scope) if rest else out
    if base == "alias":
        return out + [unwrap(w.partition("=")[2].split()) for w in argv[1:] if "=" in w]
    if base in _SHELLS:
        for j, t in enumerate(argv[1:], 1):
            if t == "-c" or (t.startswith("-") and not t.startswith("--") and "c" in t[1:]):
                if j + 1 < len(argv):
                    out += _shell_runs(argv[j + 1], depth + 1, _child(scope))
                return out
            if not t.startswith("-") or t in ("-", "-s"):
                break
        if len(argv) == 1 or argv[1] in ("-s", "-"):  # commands from stdin: the heredoc is code
            for body in bodies:
                out += _shell_runs(body, depth + 1)
    elif PYTHON_RE.fullmatch(base):
        out += _python_stdin_or_c(argv, bodies, depth)
    elif base == "eval":
        out += _shell_runs(" ".join(argv[1:]), depth + 1, scope)
    return out


def _python_stdin_or_c(argv: list[str], bodies: list[str], depth: int) -> list[list[str]]:
    j = 1
    while j < len(argv):
        t = argv[j]
        if t == "-c":
            return _python_runs(argv[j + 1], depth + 1) if j + 1 < len(argv) else []
        if t == "-m":
            return []
        if t in _PY_ARG_FLAGS:
            j += 2
            continue
        if t.startswith("-") and t != "-":
            j += 1
            continue
        if t != "-":
            return []  # a script file: not visible here
        break
    out: list[list[str]] = []
    for body in bodies:  # `python3 -` or bare `python3` reads the heredoc as its program
        out += _python_runs(body, depth + 1)
    return out


def _call_name(func) -> str:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _python_runs(code: str, depth: int) -> list[list[str]]:
    if depth > _MAX_DEPTH:
        return _raw_runs(code)
    tree = _tree(code)
    if tree is None:
        return _raw_runs(code)
    out: list[list[str]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            toks = [e.value if isinstance(e, ast.Constant) and isinstance(e.value, str) else "?" for e in node.elts]
            if toks and toks[0] != "?":
                out.append(unwrap(toks))
        elif isinstance(node, ast.Call):
            out += _python_call_runs(node, depth)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and _COMMANDISH_RE.match(node.value):
            out += _shell_runs(node.value, depth + 1)
    return out


def _python_call_runs(node: ast.Call, depth: int) -> list[list[str]]:
    name = _call_name(node.func)
    first = node.args[0] if node.args else None
    if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
        return []
    shell_kw = [k for k in node.keywords if k.arg == "shell"]
    shell = bool(shell_kw) and not (isinstance(shell_kw[0].value, ast.Constant) and not shell_kw[0].value.value)
    if shell or name in _SHELL_CALLS:
        return _shell_runs(first.value, depth + 1)
    if name in _EXEC_CALLS:
        return [unwrap(_split(first.value))]
    return []


# --------------------------------------------------------------------------- what a command is
def git_call(argv: list[str]) -> Optional[tuple[str, list[str], list[str]]]:
    """``(subcommand, its arguments, the global option words)`` of a git command, else None."""
    if not argv:
        return None
    base = basename(argv[0])
    if base.startswith("git-") and len(base) > 4:  # /usr/lib/git-core/git-push, git-send-pack, ...
        return base[4:], argv[1:], []
    if base != "git":
        return None
    i, glob = 1, []
    while i < len(argv) and argv[i].startswith("-"):
        t = argv[i]
        glob.append(t)
        if t in _GIT_ARG_OPTS and i + 1 < len(argv):
            glob.append(argv[i + 1])
            i += 2
        else:
            i += 1
    if i >= len(argv):
        return None
    return argv[i], argv[i + 1:], glob


def forge_args(argv: list[str]) -> Optional[list[str]]:
    """The arguments of a forge CLI invocation (``<tool>``, ``python3 -m <module>``, ``python3
    <path>/<tool>``), else None."""
    if not argv:
        return None
    base = basename(argv[0])
    if base in FORGE_TOOLS:
        return argv[1:]
    if PYTHON_RE.fullmatch(base):
        return _python_forge_rest(argv[1:])
    return None


_SYSTEM_DIRS = ("/usr/", "/bin/", "/sbin/", "/opt/", "/etc/", "/lib/", "/lib64/")
_GIT_RESTALE = frozenset({"cherry-pick", "merge", "pull", "rebase", "am", "revert", "apply", "checkout", "switch",
                          "reset", "restore", "stash", "clean", "rm", "mv"})
_BUILD_CMDS = frozenset({"make", "gmake", "cmake", "ninja"})


def script_run(argv: list[str]) -> bool:
    """Runs a script file the gate cannot read: ``bash|sh <file>``, ``python3 <file>``, ``source``/``. <file>``, or a
    path to an executable outside the system directories (``./push.sh``, ``/tmp/x``)."""
    if not argv:
        return False
    base = basename(argv[0])
    if forge_args(argv) is not None:
        return False  # the forge CLI itself, read by its own schema
    if base in ("source", "."):
        return len(argv) > 1
    if base in _SHELLS:
        for t in argv[1:]:
            if t == "-c" or (t.startswith("-") and not t.startswith("--") and "c" in t[1:]) or t in ("-s", "-"):
                return False
            if not t.startswith("-"):
                return True
        return False
    if PYTHON_RE.fullmatch(base):
        j = 1
        while j < len(argv):
            t = argv[j]
            if t in ("-c", "-m", "-"):
                return False
            if t in _PY_ARG_FLAGS:
                j += 2
                continue
            if t.startswith("-"):
                j += 1
                continue
            return True
        return False
    first = argv[0]
    if first.startswith(("./", "../", "~/")):
        return True
    return first.startswith("/") and not first.startswith(_SYSTEM_DIRS)


def restales(argv: list[str]) -> bool:
    """A command that changes the working tree (or may, by running code): git cherry-pick / merge / pull / rebase / am /
    revert / ..., make / cmake / ninja, any shell or Python program. It restales a critique (critique freshness), even
    where the plan gate does not count it as a file mutation."""
    if not argv:
        return False
    base = basename(argv[0])
    call = git_call(argv)
    if call and call[0] in _GIT_RESTALE:
        return True
    return base in _BUILD_CMDS or base in _SHELLS or bool(PYTHON_RE.fullmatch(base)) or script_run(argv)


def prefix_assignments(head: str) -> list[str]:
    """The NAMEs a command's leading ``NAME=value`` words (also after ``env``) set for it."""
    ws = [w for w, _ in (words(head) or [])]
    names, i = [], 0
    while i < len(ws):
        m = _ASSIGN_RE.match(ws[i])
        if m:
            names.append(m.group(1))
        elif ws[i] not in ("env", "command", "exec") and not (ws[i].startswith("-") and i and ws[i - 1] == "env"):
            break
        i += 1
    return names
