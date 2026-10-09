"""Lint ``openshell sandbox exec`` call sites against the 0.0.72 CLI rules (ING-F66 E16, E18).

Usage, from the plugin directory: ``python3 -m edge.openshell_lint PATH...``. A path is a file or a
directory (``*.py``, ``*.sh``, ``*.service`` and shebang shell scripts below it), so lane scripts
outside the repository are checked the same way. Each finding prints ``path:line: <rule>: <message>``;
the exit status is 1 when there is any finding.

Rules, both host-CLI behaviour that mocked-subprocess tests cannot see:

- ``positional-name``: ``-n <name>``, ``--name <name>`` or a nonempty ``--name=<name>`` must come before
  the first positional word or ``--``. 0.0.72 runs a positional name as the command in the last-used
  sandbox. Options before it are read with their 0.0.72 arity (``-n``, ``--name`` and ``-g`` take a value,
  ``--no-tty`` none); an unknown option takes none, so the word after it reads as positional. In a
  ``.service`` file the word right after ``exec`` must be ``-n`` or ``--name``.
- ``multiline-arg``: no exec argument contains a newline or carriage return; the CLI refuses it.

Python files are read as an AST: an argv list or tuple literal holding ``"sandbox", "exec"`` is
checked element by element. An element is resolved when it is a string constant, an f-string, a
module-level string constant, a local assigned once from one of those, or, one call deep, a
parameter of the enclosing function, resolved at every call of that function in the same file.
A value the lint cannot read may fill an option's value slot but never counts as the name flag.
Shell and unit files are read as text, line by line with ``#`` comments skipped. In a ``.sh`` file a
``sandbox exec`` is a site only when the word before ``sandbox`` is ``openshell``, a path ending in
``/openshell`` or a parameter expansion, inside quotes too: prose that names the verb is not a site,
a nested ``sh -c 'openshell sandbox exec ...'`` is. The newline rule covers each ``sandbox exec``
command's own argv, from those words to its unquoted end with backslash continuations included; a
quote opened anywhere else (``$(...)``, a function body, a multi-line ``python3 -c '...'``) is
ordinary shell. In a ``.service`` file any quoted argument still
open at the end of its line is a finding. Values held in shell variables cannot be seen. Comments,
docstrings and Markdown are never read.
"""
from __future__ import annotations

import ast
import re
import shlex
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

Finding = Tuple[str, int, str, str]
_NAME_FLAGS = ("-n", "--name")
_OPTION_ARITY = {"-n": 1, "--name": 1, "-g": 1, "--no-tty": 0}
_OPENSHELL_WORD = re.compile(
    r"(?:^|[/'\"(;|&`])openshell['\"]?$"
    r"|(?:^|['\"(;|&`])\$(?:[A-Za-z_]\w*|[0-9@*]|\{[^}]+\})['\"]?$"
)
_SKIP_DIRS = {".venv", "node_modules", ".git", "__pycache__"}
_UNKNOWN = object()


def _crlf(s: str) -> bool:
    return "\n" in s or "\r" in s


def _is_value(tok: object) -> bool:
    return tok is _UNKNOWN or (isinstance(tok, str) and tok != "" and not tok.startswith("-"))


def _head_problem(tokens: List[object]) -> Optional[str]:
    """The positional-name message for the tokens after ``exec``, or None.

    A token is a literal string, or ``_UNKNOWN`` for a value the lint cannot read.
    """
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _NAME_FLAGS:
            if i + 1 < len(tokens) and _is_value(tokens[i + 1]):
                return None
            return f"{tok} has no value before the command"
        if isinstance(tok, str) and tok.startswith("--name="):
            return None if tok[len("--name="):] else "--name= is empty"
        if tok == "--":
            break
        if not isinstance(tok, str) or not tok.startswith("-"):
            return "a positional word comes before -n/--name; 0.0.72 runs a positional name as the command"
        arity = _OPTION_ARITY.get(tok, 0)
        if arity and (i + 1 >= len(tokens) or not _is_value(tokens[i + 1])):
            return f"{tok} has no value before the command"
        i += 1 + arity
    return "no -n/--name names the sandbox before the command"


def _first_word_problem(tokens: List[object]) -> Optional[str]:
    if tokens and tokens[0] in _NAME_FLAGS:
        return None
    return "the word after `sandbox exec` is not -n/--name; 0.0.72 runs a positional name as the command"


class _PyLint:
    def __init__(self, path: Path, tree: ast.Module):
        self.path = str(path)
        self._tree = tree
        self.findings: List[Finding] = []
        self.sites = 0
        self.consts = self._assigned_strings(tree.body, {})
        self.funcs: Dict[str, ast.AST] = {}
        self.calls: List[Tuple[ast.Call, Optional[ast.AST]]] = []
        self._index(tree, None)

    def _assigned_strings(self, body: Iterable[ast.stmt], scope: Dict[str, object]) -> Dict[str, object]:
        out = dict(scope)
        seen: Dict[str, int] = {}
        for node in body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                seen[name] = seen.get(name, 0) + 1
                value = self._value(node.value, out)
                out[name] = value if seen[name] == 1 else _UNKNOWN
        return out

    def _value(self, node: ast.AST, scope: Dict[str, object]) -> object:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            return "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self._value(node.left, scope), self._value(node.right, scope)
            return left + right if isinstance(left, str) and isinstance(right, str) else _UNKNOWN
        if isinstance(node, ast.Name) and node.id in scope:
            return scope[node.id]
        return _UNKNOWN

    def _index(self, node: ast.AST, func: Optional[ast.AST]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.funcs[child.name] = child
                self._index(child, child)
            else:
                if isinstance(child, ast.Call):
                    self.calls.append((child, func))
                self._index(child, func)

    def _scope(self, func: Optional[ast.AST]) -> Dict[str, object]:
        return self.consts if func is None else self._assigned_strings(ast.walk(func), self.consts)

    def _param_values(self, func: ast.AST, param: str) -> List[Tuple[object, int]]:
        args = [a.arg for a in func.args.posonlyargs + func.args.args]
        out = []
        for call, caller in self.calls:
            callee = call.func.id if isinstance(call.func, ast.Name) else getattr(call.func, "attr", None)
            if callee != func.name:
                continue
            arg = next((k.value for k in call.keywords if k.arg == param), None)
            if arg is None and param in args and args.index(param) < len(call.args):
                arg = call.args[args.index(param)]
            if arg is not None:
                out.append((self._value(arg, self._scope(caller)), call.lineno))
        return out

    def check(self, seq: ast.AST, func: Optional[ast.AST]) -> None:
        elts = seq.elts
        at = next((i for i in range(len(elts) - 1) if all(
            isinstance(e, ast.Constant) and e.value == v for e, v in zip(elts[i:i + 2], ("sandbox", "exec")))), None)
        if at is None or at + 2 >= len(elts):
            return
        self.sites += 1
        scope = self._scope(func)
        params = {a.arg for a in func.args.posonlyargs + func.args.args + func.args.kwonlyargs} if func else set()
        problem = _head_problem([self._value(e, scope) for e in elts[at + 2:]])
        if problem:
            self.findings.append((self.path, seq.lineno, "positional-name", problem))
        for i, e in enumerate(elts[at:], at + 1):
            value = self._value(e, scope)
            if isinstance(value, str) and _crlf(value):
                self.findings.append((self.path, seq.lineno, "multiline-arg",
                                      f"argument {i} contains a newline or carriage return"))
            elif isinstance(e, ast.Name) and e.id in params:
                for v, line in self._param_values(func, e.id):
                    if isinstance(v, str) and _crlf(v):
                        self.findings.append((self.path, seq.lineno, "multiline-arg",
                                              f"argument {i} ({e.id}) holds a newline or carriage return "
                                              f"from the call at line {line}"))

    def run(self) -> None:
        def visit(node: ast.AST, func: Optional[ast.AST]) -> None:
            for child in ast.iter_child_nodes(node):
                inner = child if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else func
                if isinstance(child, (ast.List, ast.Tuple)):
                    self.check(child, func)
                visit(child, inner)
        visit(self._tree, None)


_SHELL_EXEC = re.compile(r"\bsandbox(?:[ \t]+(?:\\\n[ \t]*)*|\\\n[ \t]+)exec\b")


def _quote_at(text: str, start: int, end: int) -> Optional[str]:
    """The quote character open at ``end`` when scanning from ``start``, or None."""
    quote, i = None, start
    while i < end:
        c = text[i]
        if quote == "'":
            quote = None if c == "'" else quote
        elif c == "\\":
            i += 1
        elif quote == '"':
            quote = None if c == '"' else quote
        elif c in "'\"":
            quote = c
        i += 1
    return quote


def _shell_command_end(text: str, start: int, inside: Optional[str] = None) -> int:
    if inside:
        # the exec sits inside a quoted argument (`sh -c "openshell sandbox exec …"`): it ends there
        i = start
        while i < len(text) and text[i] != inside:
            i += 2 if text[i] == "\\" and inside == '"' else 1
        return i
    quote, depth, i = None, 0, start
    while i < len(text):
        c = text[i]
        if quote == "'":
            quote = None if c == "'" else quote
        elif quote == '"':
            if c == "\\":
                i += 1
            elif c == '"':
                quote = None
        elif c in "'\"":
            quote = c
        elif c == "\\":
            i += 1
        elif c == "(" and (depth or text[i - 1:i] == "$"):
            depth += 1
        elif c == ")" and depth:
            depth -= 1
        elif not depth and c in "\n;|&)":
            return i
        i += 1
    return i


def _open_quote_at_eol(line: str) -> bool:
    quote, i = None, 0
    while i < len(line):
        c = line[i]
        if quote == "'":
            quote = None if c == "'" else quote
        elif c == "\\":
            i += 1
        elif quote == '"':
            quote = None if c == '"' else quote
        elif c in "'\"":
            quote = c
        elif c == "#" and (i == 0 or line[i - 1].isspace()):
            break
        i += 1
    return quote is not None


def _word_before(text: str, at: int) -> str:
    """The word before the blank run that ends at ``at``, across ``\\`` continuations; "" when there is none."""
    i = at
    while i > 0:
        if text[i - 1] in " \t":
            i -= 1
        elif i >= 2 and text[i - 2:i] == "\\\n":
            i -= 2
        else:
            break
    j = i
    while j > 0 and not text[j - 1].isspace():
        j -= 1
    return text[j:i] if i < at else ""


def _comment_starts(prefix: str, mark: "re.Match[str]") -> bool:
    """Whether the ``#`` that ``mark`` ends on opens a shell comment: unquoted, after an unescaped boundary."""
    before = prefix[:mark.start()]
    escaped = (len(before) - len(before.rstrip("\\"))) % 2 == 1
    return not escaped and _quote_at(prefix, 0, mark.end() - 1) is None


def lint_shell(path: Path, text: str) -> Tuple[List[Finding], int]:
    findings: List[Finding] = []
    sites = 0
    unit = path.suffix == ".service"
    if unit:
        for n, line in enumerate(text.split("\n"), 1):
            if not line.lstrip().startswith(("#", ";")) and _open_quote_at_eol(line):
                findings.append((str(path), n, "multiline-arg", "a quoted argument is still open at the end of the line"))
    for m in _SHELL_EXEC.finditer(text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        prefix = text[line_start:m.start()]
        if any(_comment_starts(prefix, mark) for mark in re.finditer(r"(^|[\s;|&()])#", prefix)):
            continue
        if not unit and not _OPENSHELL_WORD.search(_word_before(text, m.start())):
            continue
        sites += 1
        line = text.count("\n", 0, m.start()) + 1
        inside = _quote_at(text, line_start, m.start())
        segment = text[m.end():_shell_command_end(text, m.end(), inside)].replace("\\\n", " ")
        try:
            tokens = shlex.split(segment, comments=True)
        except ValueError as exc:
            findings.append((str(path), line, "unparsed", f"cannot split the exec command ({exc})"))
            continue
        problem = (_first_word_problem if unit else _head_problem)(tokens)
        if problem:
            findings.append((str(path), line, "positional-name", problem))
        for i, tok in enumerate(tokens, 3):
            if not unit and _crlf(tok):
                findings.append((str(path), line, "multiline-arg", f"argument {i} contains a newline or carriage return"))
    return findings, sites


def lint_python(path: Path, text: str) -> Tuple[List[Finding], int]:
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        return [(str(path), exc.lineno or 0, "unparsed", f"not valid Python ({exc.msg})")], 0
    lint = _PyLint(path, tree)
    lint.run()
    return lint.findings, lint.sites


def _is_shell(path: Path) -> bool:
    if path.suffix in (".sh", ".service"):
        return True
    if path.suffix:
        return False
    try:
        with open(path, "rb") as handle:
            first = handle.readline(128)
    except OSError:
        return False
    return first.startswith(b"#!") and re.search(rb"\b(ba|da|z)?sh\b", first) is not None


def _files(root: Path, exclude: Tuple[Path, ...]) -> Iterable[Path]:
    if root.is_file():
        yield root
        return
    if not root.is_dir():
        raise FileNotFoundError(f"no such file or directory: {root}")
    for p in sorted(root.rglob("*")):
        if (p.is_file() and not _SKIP_DIRS & set(p.relative_to(root).parts)
                and not any(p.resolve().is_relative_to(x) for x in exclude) and (p.suffix == ".py" or _is_shell(p))):
            yield p


def lint_paths(paths: Iterable[Path], exclude: Iterable[Path] = ()) -> Tuple[List[Finding], int]:
    """Every finding under ``paths`` (skipping ``exclude``) plus the number of ``sandbox exec`` sites checked."""
    findings: List[Finding] = []
    sites = 0
    skip = tuple(Path(x).resolve() for x in exclude)
    for root in paths:
        for p in _files(Path(root), skip):
            text = p.read_text(encoding="utf-8", errors="surrogateescape")
            if p.suffix != ".service" and not ("sandbox" in text and "exec" in text):
                continue
            found, n = lint_python(p, text) if p.suffix == ".py" else lint_shell(p, text)
            findings += found
            sites += n
    return findings, sites


def main(argv: List[str]) -> int:
    if not argv:
        print("usage: python3 -m edge.openshell_lint PATH...", file=sys.stderr)
        return 2
    try:
        findings, sites = lint_paths([Path(a) for a in argv])
    except FileNotFoundError as exc:
        print(f"openshell-exec-lint: {exc}", file=sys.stderr)
        return 2
    for path, line, rule, msg in findings:
        print(f"{path}:{line}: {rule}: {msg}")
    print(f"openshell-exec-lint: {sites} sandbox exec site(s) checked, {len(findings)} finding(s)", file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
