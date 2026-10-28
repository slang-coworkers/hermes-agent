"""UPG-F69 replay checker (ADR ``reports/upg-f69.md`` §D2).

    python replay_check.py --repo R --from A --onto B --head H --candidate C --data-dir D

Proves that candidate C replays the fork delta A..H onto B. It reads git objects only: no checkout, no write to the
repo, no network. Output is one ``FAIL <CODE> <key>: <detail>`` line per finding, then ``OK <n> paths`` or
``FAILED <k>``. Exit 0 iff there is no finding, 1 on any finding (missing ruled data included), 2 only for a bad
command line or an unresolvable revision.

Data files in D are TSV: ``#`` comment lines, one header row, cells in Linear-TSV escapes (``\\\\`` ``\\t`` ``\\n``),
so a ``fork_ci_lines.tsv`` before/after cell may hold a multi-line block. ``bump.json`` names the bump the data was
written for; when it is missing or names other revisions, every ruled row is ignored and the run lists everything that
needs a ruling (the generic mode a nightly upstream sync runs with its own ``--onto``). It never prints ``OK`` then.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
TABLES = {
    "path_inventory.tsv": ("status", "path", "surface", "class", "origin", "authority"),
    "core_authority.tsv": ("key", "authority", "disposition"),
    "ruled_residuals.tsv": ("key", "sha256", "authority"),
    "overlap_rulings.tsv": ("key", "sha256", "ruling"),
    "fork_ci_map.tsv": ("kind", "match", "replacement", "authority"),
    "fork_ci_lines.tsv": ("workflow", "job", "anchor", "before", "after", "authority"),
    "fork_ci_contributors.tsv": ("path", "sha256", "authority"),
    "fork_ci_sizing.tsv": ("workflow", "job", "key", "value", "authority"),
}
IN_SURFACE = ("plugins/", "website/docs/", "tests/", "apps/desktop/e2e/")
FORK_CI = (".github/", "contributors/")
NONPY_FORMATS = (".yaml", ".yml", ".json", ".toml", ".md", ".txt")
MARKER = re.compile(r"^(<{7}|>{7}) ")
LABEL_SHAPE = re.compile(r"blacksmith-\d|-latest-32-(?:arm-)?core")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SHA40 = re.compile(r"^[0-9a-f]{40}$")
ACTION_REF = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}( +# \S.*)?$")
JOB_KEY = re.compile(r"^(\s+)([A-Za-z0-9_-]+):\s*(?:#.*)?$")
WORKERS = r"(?:-n|--workers|--concurrency)[ =]{v}(?!\d)"
WORKERS_ANY = re.compile(r"((?:-n|--workers|--concurrency)[ =])\d+(?!\d)")


class BadRevision(Exception):
    pass


class Git:
    """Read-only git access: rev-parse, diff --name-status and one ``cat-file --batch`` for blobs."""

    def __init__(self, repo: str):
        self.repo = repo
        self._batch = None
        self._cache: dict[tuple[str, str], bytes | None] = {}

    def run(self, *args: str) -> str:
        proc = subprocess.run(["git", "-C", self.repo, "-c", "core.quotepath=false", *args],
                              capture_output=True, text=True, encoding="utf-8", errors="surrogateescape", check=False)
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, args, proc.stdout, proc.stderr)
        return proc.stdout

    def resolve(self, rev: str) -> str:
        try:
            return self.run("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}").strip()
        except subprocess.CalledProcessError:
            raise BadRevision(rev) from None

    def name_status(self, a: str, b: str) -> dict[str, str]:
        out = self.run("diff", "--no-renames", "--no-ext-diff", "--name-status", "-z", a, b).split("\0")
        return {out[i + 1]: out[i][0] for i in range(0, len(out) - 1, 2)}

    def ls(self, rev: str, prefix: str) -> list[str]:
        out = self.run("ls-tree", "-r", "-z", "--name-only", rev, "--", prefix).split("\0")
        return [p for p in out if p]

    def blob(self, rev: str, path: str) -> bytes | None:
        key = (rev, path)
        if key not in self._cache:
            if self._batch is None:
                self._batch = subprocess.Popen(["git", "-C", self.repo, "cat-file", "--batch"],
                                               stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            self._batch.stdin.write(f"{rev}:{path}\n".encode("utf-8", "surrogateescape"))
            self._batch.stdin.flush()
            header = self._batch.stdout.readline().split()
            if len(header) == 3 and header[1] == b"blob":
                data = self._batch.stdout.read(int(header[2]))
                self._batch.stdout.read(1)
                self._cache[key] = data
            else:
                self._cache[key] = None
        return self._cache[key]

    def text(self, rev: str, path: str) -> str | None:
        data = self.blob(rev, path)
        return None if data is None else data.decode("utf-8", "surrogateescape")


def lines_of(text: str | None) -> list[str]:
    return [] if text is None else text.split("\n")[:-1] if text.endswith("\n") else text.split("\n")


def surface_of(path: str) -> str:
    if path.startswith(IN_SURFACE):
        return "in-surface"
    return "fork-ci" if path.startswith(FORK_CI) else "core"


def unescape(cell: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t", "\\": "\\"}.get(m.group(1), m.group(0)), cell)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()


def signed(after: Counter, before: Counter) -> Counter:
    out = Counter(after)
    out.subtract(before)
    return Counter({k: v for k, v in out.items() if v})


def residual(c: Counter, b: Counter, h: Counter, a: Counter) -> Counter:
    out = signed(c, b)
    out.subtract(signed(h, a))
    return Counter({k: v for k, v in out.items() if v})


def residual_sha(res: Counter) -> str:
    entries = [f"+{k}" if v > 0 else f"-{k}" for k, v in res.items() for _ in range(abs(v))]
    return sha256("\n".join(sorted(entries)))


class Report:
    def __init__(self):
        self.findings: list[str] = []

    def fail(self, code: str, key: str, detail: str) -> None:
        self.findings.append(f"FAIL {code} {key}: {detail}")


def load_data(data_dir: Path, rep: Report) -> tuple[dict | None, dict[str, list[dict]]]:
    """Every table as a list of row dicts (``_line`` = its line in the file). Missing or malformed → DATA_MISSING."""
    bump = None
    bump_path = data_dir / "bump.json"
    if not bump_path.is_file():
        rep.fail("DATA_MISSING", "bump.json", "absent")
    else:
        try:
            bump = json.loads(bump_path.read_text(encoding="utf-8-sig"))
            if not all(isinstance(bump.get(k), str) and SHA40.match(bump[k]) for k in ("from", "onto", "head")):
                raise ValueError("from/onto/head must be full 40-char SHAs")
        except (ValueError, AttributeError) as exc:
            rep.fail("DATA_MISSING", "bump.json", f"malformed: {exc}")
            bump = None
    tables: dict[str, list[dict]] = {}
    for name, header in TABLES.items():
        tables[name] = []
        path = data_dir / name
        if not path.is_file():
            rep.fail("DATA_MISSING", name, "absent")
            continue
        rows = [(n, ln) for n, ln in enumerate(path.read_text(encoding="utf-8-sig").split("\n"), 1)
                if ln and not ln.startswith("#")]
        if not rows or tuple(rows[0][1].split("\t")) != header:
            rep.fail("DATA_MISSING", name, f"header must be {' '.join(header)}")
            continue
        for n, ln in rows[1:]:
            cells = ln.split("\t")
            if len(cells) != len(header):
                rep.fail("DATA_MISSING", f"{name}:{n}", f"{len(cells)} columns, expected {len(header)}")
                continue
            row = dict(zip(header, (unescape(c) for c in cells)))
            row["_line"] = f"{name}:{n}"
            problem = row_problem(name, row)
            if problem:
                rep.fail("DATA_MISSING", row["_line"], f"malformed: {problem}")
            else:
                tables[name].append(row)
    return bump, tables


def row_problem(name: str, row: dict) -> str | None:
    if name == "path_inventory.tsv":
        if row["status"] not in ("A", "M", "D"):
            return f"status {row['status']!r}"
        if row["surface"] not in ("in-surface", "fork-ci", "core"):
            return f"surface {row['surface']!r}"
    elif name == "core_authority.tsv" and row["disposition"] not in ("replay", "absorbed", "ruled"):
        return f"disposition {row['disposition']!r}"
    elif name in ("ruled_residuals.tsv", "overlap_rulings.tsv") and not SHA256.match(row["sha256"]):
        return "sha256 must be 64 hex"
    elif name == "fork_ci_contributors.tsv" and not (SHA256.match(row["sha256"]) or row["sha256"] == "deleted"):
        return "sha256 must be 64 hex or `deleted`"
    elif name == "fork_ci_map.tsv":
        if row["kind"] not in ("label", "action"):
            return f"kind {row['kind']!r}"
        if row["kind"] == "label" and not re.fullmatch(r"[\w.-]+", row["replacement"]):
            return "a label replacement is one runner label"
        if row["kind"] == "action" and not ACTION_REF.match(row["match"]):
            return "an action match is the whole owner/name@<40-sha> [# comment]"
        if row["kind"] == "action" and row["replacement"] != "KEEP" and not ACTION_REF.match(row["replacement"]):
            return "an action replacement is owner/name@<40-sha> [# comment] or KEEP"
    elif name == "fork_ci_sizing.tsv":
        if row["key"] not in ("timeout-minutes", "workers") or not row["value"].isdigit():
            return "key must be timeout-minutes|workers with a numeric value"
    elif name == "fork_ci_lines.tsv" and not (row["before"] or row["after"]):
        return "before and after are both empty"
    return None


class PyScopes:
    """Lines of a Python source attributed to their innermost def/class (dotted qualname), else ``<module>``."""

    def __init__(self, text: str | None):
        self.text = text or ""
        self.lines = lines_of(text)
        self.tree = None
        self.nodes: dict[str, ast.AST] = {}
        self.owner = ["<module>"] * len(self.lines)
        if text is None:
            return
        try:
            self.tree = ast.parse(self.text)
        except (SyntaxError, ValueError):
            return
        self._walk(self.tree.body, "")

    def _walk(self, body, prefix: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{node.name}"
                self.nodes.setdefault(name, node)
                first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
                for i in range(first - 1, node.end_lineno):
                    self.owner[i] = name
                self._walk(node.body, f"{name}.")
            else:
                for field in ("body", "orelse", "finalbody"):
                    inner = getattr(node, field, None)
                    if isinstance(inner, list):
                        self._walk(inner, prefix)
                for sub in [*getattr(node, "handlers", []), *getattr(node, "cases", [])]:
                    self._walk(sub.body, prefix)

    @property
    def parses(self) -> bool:
        return self.tree is not None or not self.text

    def multiset(self, scope: str | None = None) -> Counter:
        return Counter(ln.strip() for ln, o in zip(self.lines, self.owner)
                       if ln.strip() and (scope is None or o == scope))

    def own_text(self, scope: str) -> list[str]:
        """The scope's own raw lines (nested defs are their own scopes): any textual edit, a re-indent or a blank
        line included, changes it."""
        return [ln for ln, o in zip(self.lines, self.owner) if o == scope]

    def scopes(self) -> set[str]:
        return set(self.owner) | {"<module>"}

    def segment(self, scope: str) -> str:
        """The scope's source text: ``ast.get_source_segment`` of its node; ``<module>`` joins the module-level
        statements that are not a def/class."""
        if self.tree is None:
            return ""
        if scope == "<module>":
            return "\n".join(ast.get_source_segment(self.text, n) or "" for n in self.tree.body
                             if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
        node = self.nodes.get(scope)
        return "" if node is None else ast.get_source_segment(self.text, node) or ""


def code_comment(line: str) -> tuple[str, str]:
    """Split a YAML line into its code and its trailing ``#`` comment (a ``#`` after whitespace, outside quotes)."""
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i], line[i:]
    return line, ""


def jobs_of(lines: list[str]) -> list[str]:
    """The job id each line of a workflow belongs to ("" outside ``jobs:``)."""
    out, job, in_jobs, indent = [], "", False, None
    for ln in lines:
        if ln[:1] not in ("", " ", "\t", "#"):
            in_jobs, job, indent = ln.split("#")[0].strip() == "jobs:", "", None
        elif in_jobs and (m := JOB_KEY.match(ln)) and (indent is None or len(m.group(1)) == indent):
            indent, job = len(m.group(1)), m.group(2)
        out.append(job)
    return out


class ForkCiMap:
    """The ``fork_ci_map.tsv`` substitutions; ``used`` tracks the rows that changed a line."""

    def __init__(self, rows: list[dict]):
        self.labels = [r for r in rows if r["kind"] == "label"]
        self.actions = [r for r in rows if r["kind"] == "action"]
        self.keep = {r["match"].split("#", 1)[0].strip() for r in self.actions if r["replacement"] == "KEEP"
                     and r["authority"]}
        self.label_set = {r["match"] for r in self.labels}
        self.used: set[str] = set()

    def apply(self, line: str) -> str:
        for row in self.actions:
            if row["replacement"] == "KEEP":
                continue
            new = line.replace(row["match"], row["replacement"])
            if new != line:
                self.used.add(row["_line"])
                line = new
        code, comment = code_comment(line)
        for row in self.labels:
            new = re.sub(rf"(?<![\w-]){re.escape(row['match'])}(?![\w-])", lambda _m: row["replacement"], code)
            if new != code:
                self.used.add(row["_line"])
                code = new
        return code + comment


def workflow_path(name: str) -> str:
    return name if "/" in name else f".github/workflows/{name}"


def unique_in_job(lines: list[str], jobs: list[str], job: str, stripped: str) -> int | None:
    hits = [i for i, ln in enumerate(lines) if jobs[i] == job and ln.strip() == stripped]
    return hits[0] if len(hits) == 1 else None


def expected_fork_ci(b_lines: list[str], c_lines: list[str], fmap: ForkCiMap, sizing: list[dict], rows: list[dict],
                     rep: Report) -> list[str]:
    """E(B): the onto file with every ruled fork-ci edit applied — the map on every line, then each sizing row, then
    each ``fork_ci_lines`` row in file order (its lines are C's raw lines after the same unique anchor). A row that
    cannot apply is DATA_STALE; whatever E(B) and C still differ by is unruled."""
    exp = [fmap.apply(ln) for ln in b_lines]
    c_jobs = jobs_of(c_lines)
    for row in sizing:
        jobs = jobs_of(exp)
        if row["key"] == "timeout-minutes":
            hits = [i for i, ln in enumerate(exp) if jobs[i] == row["job"] and is_job_key(exp, jobs, i, "timeout-minutes")]
            if len(hits) != 1:
                rep.fail("DATA_STALE", row["_line"], f"job {row['job']} has {len(hits)} job-level timeout-minutes lines")
                continue
            exp[hits[0]] = re.sub(r"(timeout-minutes:\s*)[^\s#]+", lambda m: m.group(1) + row["value"], exp[hits[0]],
                                count=1)
        else:
            token = re.compile(WORKERS.format(v=re.escape(row["value"])))
            done = False
            c_steps, e_steps = step_lines(c_lines, c_jobs, row["job"]), step_lines(exp, jobs, row["job"])
            for ci in (i for i in c_steps if token.search(code_comment(c_lines[i])[0])):
                m = token.search(c_lines[ci])
                base = c_lines[ci][:m.start()].rstrip(" ") + c_lines[ci][m.end():]
                hits = [i for i in e_steps if exp[i] == base
                        or WORKERS_ANY.sub(lambda mm: mm.group(1) + row["value"], exp[i]) == c_lines[ci]]
                if len(hits) == 1:
                    exp[hits[0]], done = c_lines[ci], True
                    break
            if not done:
                rep.fail("DATA_STALE", row["_line"], f"no line of job {row['job']} takes {row['value']} workers")
    for row in rows:
        jobs = jobs_of(exp)
        before = row["before"].split("\n") if row["before"] else []
        after = row["after"].split("\n") if row["after"] else []
        a = unique_in_job(exp, jobs, row["job"], row["anchor"])
        ca = unique_in_job(c_lines, c_jobs, row["job"], row["anchor"])
        if a is None or ca is None:
            counts = [sum(j == row["job"] and ln.strip() == row["anchor"] for ln, j in zip(x, js))
                      for x, js in ((exp, jobs), (c_lines, c_jobs))]
            rep.fail("DATA_STALE", row["_line"], f"anchor occurs {counts[0]}x in job {row['job']} of E(B) and "
                     f"{counts[1]}x in the candidate; it must occur once in each")
            continue
        if [ln.strip() for ln in exp[a + 1:a + 1 + len(before)]] != before:
            rep.fail("DATA_STALE", row["_line"], "the onto lines after the anchor are not `before`")
            continue
        if [ln.strip() for ln in c_lines[ca + 1:ca + 1 + len(after)]] != after:
            rep.fail("DATA_STALE", row["_line"], "the candidate lines after the anchor are not `after`")
            continue
        exp[a + 1:a + 1 + len(before)] = c_lines[ca + 1:ca + 1 + len(after)]
    return exp


def step_lines(lines: list[str], jobs: list[str], job: str) -> list[int]:
    """Indices of the non-comment lines under the job's own ``steps:`` key."""
    start = next((i for i, ln in enumerate(lines) if jobs[i] == job and is_job_key(lines, jobs, i, "steps")), None)
    if start is None:
        return []
    out = []
    for i in range(start + 1, len(lines)):
        if jobs[i] != job:
            break
        if not lines[i].strip() or lines[i].lstrip().startswith("#"):
            continue
        same = indent(lines[i]) == indent(lines[start])
        if indent(lines[i]) < indent(lines[start]) or same and not lines[i].lstrip().startswith("- "):
            break
        out.append(i)
    return out


def indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def is_job_key(lines: list[str], jobs: list[str], i: int, key: str) -> bool:
    """Line i is ``key:`` at its job's own level (the indent of the job's first key)."""
    if not jobs[i] or not lines[i].lstrip().startswith(f"{key}:"):
        return False
    job_line = jobs.index(jobs[i])
    level = next(indent(ln) for ln in lines[job_line + 1:] if ln.strip() and not ln.lstrip().startswith("#"))
    return indent(lines[i]) == level


def yaml_load(text: str):
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from hermes_yaml import safe_load

    return safe_load(text)


def scalars(node, where: str = "", key=None):
    """(location, key, value) for every string scalar value of a loaded YAML document; keys are not values."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from scalars(v, f"{where}.{k}" if where else str(k), k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from scalars(v, f"{where}[{i}]", key)
    elif isinstance(node, str):
        yield where, key, node


class Checker:
    def __init__(self, git: Git, revs: dict[str, str], data_dir: Path):
        self.git, self.revs, self.rep = git, revs, Report()
        self.A, self.B, self.H, self.C = revs["from"], revs["onto"], revs["head"], revs["candidate"]
        bump, tables = load_data(data_dir, self.rep)
        self.ruled = bump is not None
        if bump is not None:
            for k in ("from", "onto", "head"):
                if bump[k] != revs[k]:
                    self.rep.fail("DATA_STALE", "bump.json", f"--{k} resolves to {revs[k]}, bump.json says {bump[k]}")
                    self.ruled = False
        self.t = tables if self.ruled else {name: [] for name in TABLES}
        self.fork = git.name_status(self.A, self.H)
        self.cand = git.name_status(self.B, self.C)
        self.changed = sorted(set(self.fork) | set(self.cand))

    def run(self) -> int:
        self.inventory()
        core = [p for p in self.changed if surface_of(p) == "core"]
        self.core(core)
        self.markers()
        self.fork_ci_labels()
        self.fork_delta()
        self.fork_ci_edits()
        for line in self.rep.findings:
            print(line)
        if self.rep.findings:
            print(f"FAILED {len(self.rep.findings)}")
            return 1
        print(f"OK {len(self.changed)} paths")
        return 0

    def authority_rows(self, name: str, column: str = "authority") -> None:
        for row in self.t[name]:
            if not row[column].strip():
                self.rep.fail("NO_AUTHORITY", row["_line"], f"empty {column}")

    def ruled_residual(self, key: str, res: Counter) -> bool:
        rows = [r for r in self.t["ruled_residuals.tsv"] if r["key"] == key]
        hit = [r for r in rows if r["sha256"] == residual_sha(res)]
        for r in hit:
            self.used_residuals.add(r["_line"])
        return bool(hit)

    def inventory(self) -> None:
        rows: dict[str, list[dict]] = {}
        for row in self.t["path_inventory.tsv"]:
            rows.setdefault(row["path"], []).append(row)
        for path, status in sorted(self.cand.items()):
            got = rows.get(path, [])
            if not got:
                self.rep.fail("INVENTORY_MISSING", path, f"changed onto→candidate ({status}), no inventory row")
                continue
            for extra in got[1:]:
                self.rep.fail("INVENTORY_EXTRA", extra["_line"], f"second row for {path}")
            row, surface = got[0], surface_of(path)
            if row["status"] != status or row["surface"] != surface:
                self.rep.fail("INVENTORY_MISSING", path, f"row says {row['status']}/{row['surface']}, path is "
                              f"{status}/{surface}")
            if surface != "in-surface" and not row["authority"].strip():
                self.rep.fail("NO_AUTHORITY", path, f"{surface} row with an empty authority")
        for path, got in sorted(rows.items()):
            if path not in self.cand:
                for row in got:
                    self.rep.fail("INVENTORY_EXTRA", row["_line"], f"{path} does not differ onto→candidate")

    def core(self, core: list[str]) -> None:
        """Rules 1-4 over every core path changed from→head or onto→candidate."""
        self.used_residuals: set[str] = set()
        self.used_overlaps: set[str] = set()
        auth = {r["key"]: r for r in self.t["core_authority.tsv"] if "#" not in r["key"]}
        self.authority_rows("core_authority.tsv")
        for row in self.t["core_authority.tsv"]:
            path, _, scope = row["key"].partition("#")
            if path not in core:
                self.rep.fail("DATA_STALE", row["_line"], f"{path} is not a changed core path")
            elif scope and not any(scope in PyScopes(self.git.text(r, path)).scopes() for r in (self.H, self.C)):
                self.rep.fail("DATA_STALE", row["_line"], f"no scope {scope} in {path} at head or candidate")
        for path in core:
            row = auth.get(path)
            if row is None:
                self.rep.fail("UNKNOWN_CORE_PATH", path, "changed core path with no core_authority.tsv row")
            texts = [self.git.text(r, path) for r in (self.A, self.B, self.H, self.C)]
            if row is not None and row["disposition"] == "absorbed":
                self.absorbed(path, *texts)
            elif path.endswith(".py"):
                self.python_core(path, *texts)
            elif path.endswith(NONPY_FORMATS):
                self.nonpy_core(path, *texts)
            else:
                self.rep.fail("UNKNOWN_FORMAT", path, "a core file outside .py .yaml .yml .json .toml .md .txt")
        self.authority_rows("ruled_residuals.tsv")
        self.authority_rows("overlap_rulings.tsv", "ruling")
        for row in self.t["overlap_rulings.tsv"]:
            if row["_line"] not in self.used_overlaps:
                self.rep.fail("DATA_STALE", row["_line"], f"{row['key']} is not an overlap scope at this bump")

    def absorbed(self, path: str, a: str | None, b: str | None, h: str | None, c: str | None) -> None:
        fork = signed(Counter(ln.strip() for ln in lines_of(h) if ln.strip()),
                      Counter(ln.strip() for ln in lines_of(a) if ln.strip()))
        up = signed(Counter(ln.strip() for ln in lines_of(b) if ln.strip()),
                    Counter(ln.strip() for ln in lines_of(a) if ln.strip()))
        if self.git.blob(self.C, path) != self.git.blob(self.B, path):
            self.rep.fail("ABSORBED_MISMATCH", path, "candidate blob differs from onto")
        missing = [k for k, v in fork.items() if not (up.get(k, 0) * v > 0 and abs(up[k]) >= abs(v))]
        if missing:
            self.rep.fail("ABSORBED_MISMATCH", path, f"{len(missing)} fork line(s) not in onto's change, e.g. "
                          f"{missing[0][:120]!r}")

    def python_core(self, path: str, *texts: str | None) -> None:
        a, b, h, c = (PyScopes(t) for t in texts)
        if not c.parses:
            self.rep.fail("SYNTAX", path, "does not parse at the candidate")
            self.used_overlaps |= {r["_line"] for r in self.t["overlap_rulings.tsv"] if r["key"].startswith(f"{path}#")}
        else:
            for scope in sorted(a.scopes() | b.scopes() | h.scopes() | c.scopes()):
                res = residual(c.multiset(scope), b.multiset(scope), h.multiset(scope), a.multiset(scope))
                if res and not self.ruled_residual(f"{path}#{scope}", res):
                    self.rep.fail("SCOPE_RESIDUAL", f"{path}#{scope}", describe(res))
            for scope in sorted(a.scopes() | h.scopes() | b.scopes()):
                if a.own_text(scope) != h.own_text(scope) and a.own_text(scope) != b.own_text(scope):
                    self.overlap(path, scope, c.segment(scope))
        res = residual(c.multiset(), b.multiset(), h.multiset(), a.multiset())
        if res and not self.ruled_residual(path, res):
            self.rep.fail("FILE_RESIDUAL", path, describe(res))

    def overlap(self, path: str, scope: str, text_at_c: str) -> None:
        key = f"{path}#{scope}"
        rows = [r for r in self.t["overlap_rulings.tsv"] if r["key"] == key]
        for r in rows:
            self.used_overlaps.add(r["_line"])
        if not rows:
            self.rep.fail("OVERLAP_UNRULED", key, "fork and onto both changed this scope; it needs a ruling")
        elif not any(r["sha256"] == sha256(text_at_c) for r in rows):
            self.rep.fail("OVERLAP_BODY_MISMATCH", key, f"candidate scope sha256 is {sha256(text_at_c)}")

    def nonpy_core(self, path: str, *texts: str | None) -> None:
        a, b, h, c = (Counter(lines_of(t)) for t in texts)
        res = residual(c, b, h, a)
        if res and not self.ruled_residual(path, res):
            self.rep.fail("NONPY_CORE_DELTA", path, describe(res))
        if path.endswith((".yaml", ".yml")) and texts[3] is not None:
            try:
                yaml_load(texts[3])
            except Exception as exc:  # any loader error is a parse failure of the candidate file
                self.rep.fail("NONPY_CORE_PARSE", path, f"hermes_yaml.safe_load: {type(exc).__name__}")

    def markers(self) -> None:
        """Rule 5: no changed path carries a conflict marker at the candidate."""
        for path in self.changed:
            for n, ln in enumerate(lines_of(self.git.text(self.C, path)), 1):
                if MARKER.match(ln):
                    self.rep.fail("CONFLICT_MARKER", f"{path}:{n}", ln[:80])

    def fork_ci_labels(self) -> None:
        """Rule 6: every workflow and actionlint.yaml at the candidate, scalar by scalar."""
        fmap = ForkCiMap(self.t["fork_ci_map.tsv"])
        paths = [p for p in self.git.ls(self.C, ".github/workflows")
                 if p.count("/") == 2 and p.endswith((".yml", ".yaml"))]
        paths += [p for p in self.git.ls(self.C, ".github/actionlint.yaml") if p == ".github/actionlint.yaml"]
        for path in paths:
            try:
                doc = yaml_load(self.git.text(self.C, path))
            except Exception as exc:  # any loader error is a parse failure of the candidate file
                self.rep.fail("SYNTAX", path, f"hermes_yaml.safe_load: {type(exc).__name__}")
                continue
            for where, key, value in scalars(doc):
                if LABEL_SHAPE.search(value) or value in fmap.label_set:
                    self.rep.fail("FORK_CI_LABEL", path, f"{where} = {value[:120]}")
                if key == "uses" and value.startswith("useblacksmith/") and value.strip() not in fmap.keep:
                    self.rep.fail("FORK_CI_ACTION", path, f"{where} = {value} has no KEEP row")

    def fork_delta(self) -> None:
        """Rule 7: every in-surface path the fork changed keeps its change at the candidate."""
        for path in sorted(p for p in self.fork if surface_of(p) == "in-surface"):
            a, b, h, c = (Counter(lines_of(self.git.text(r, path))) for r in (self.A, self.B, self.H, self.C))
            res = residual(c, b, h, a)
            if res and not self.ruled_residual(path, res):
                self.rep.fail("FORK_DELTA_MISSING", path, describe(res))
        for row in self.t["ruled_residuals.tsv"]:
            if row["_line"] not in self.used_residuals:
                self.rep.fail("DATA_STALE", row["_line"], f"{row['key']} has no residual with this sha256")

    def fork_ci_edits(self) -> None:
        """Rule 8: every onto→candidate edit of a fork-ci path is a map substitution, a sizing row or a lines row."""
        fmap = ForkCiMap(self.t["fork_ci_map.tsv"])
        for name in ("fork_ci_map.tsv", "fork_ci_lines.tsv", "fork_ci_contributors.tsv", "fork_ci_sizing.tsv"):
            self.authority_rows(name)
        paths = sorted(p for p in self.cand if surface_of(p) == "fork-ci")
        contrib = {r["path"]: r for r in self.t["fork_ci_contributors.tsv"]}
        for row in self.t["fork_ci_contributors.tsv"]:
            if row["path"] not in self.cand:
                self.rep.fail("DATA_STALE", row["_line"], f"{row['path']} does not differ onto→candidate")
        for table in ("fork_ci_lines.tsv", "fork_ci_sizing.tsv"):
            for row in self.t[table]:
                if workflow_path(row["workflow"]) not in paths:
                    self.rep.fail("DATA_STALE", row["_line"], f"{row['workflow']} has no fork-ci edit")
        for path in paths:
            if path.startswith("contributors/"):
                blob = self.git.blob(self.C, path)
                want = "deleted" if blob is None else hashlib.sha256(blob).hexdigest()
                row = contrib.get(path)
                if row is None or row["sha256"] != want:
                    self.rep.fail("FORK_CI_UNRULED", path, f"no fork_ci_contributors.tsv row with sha256 {want}")
                continue
            b_lines = lines_of(self.git.text(self.B, path))
            c_lines = lines_of(self.git.text(self.C, path))
            sizing = [r for r in self.t["fork_ci_sizing.tsv"] if workflow_path(r["workflow"]) == path]
            rows = [r for r in self.t["fork_ci_lines.tsv"] if workflow_path(r["workflow"]) == path]
            exp = expected_fork_ci(b_lines, c_lines, fmap, sizing, rows, self.rep)
            c_jobs, e_jobs = jobs_of(c_lines), jobs_of(exp)
            matcher = difflib.SequenceMatcher(None, exp, c_lines, autojunk=False)
            for op, i1, i2, j1, j2 in matcher.get_opcodes():
                for i in range(i1, i2) if op != "equal" else ():
                    self.rep.fail("FORK_CI_UNRULED", f"{path}:{e_jobs[i] or '-'}", f"- {exp[i].strip()[:160]}")
                for j in range(j1, j2) if op != "equal" else ():
                    self.rep.fail("FORK_CI_UNRULED", f"{path}:{c_jobs[j] or '-'}", f"+ {c_lines[j].strip()[:160]}")
        for row in self.t["fork_ci_map.tsv"]:
            if row["_line"] not in fmap.used and row["replacement"] != "KEEP":
                self.rep.fail("DATA_STALE", row["_line"], f"{row['match']} changes no fork-ci line")


def describe(res: Counter) -> str:
    plus, minus = sum(v for v in res.values() if v > 0), -sum(v for v in res.values() if v < 0)
    first = sorted(res)[0]
    return f"residual +{plus}/-{minus} (sha256 {residual_sha(res)}), e.g. {'+' if res[first] > 0 else '-'}{first[:100]!r}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    for name in ("repo", "from", "onto", "head", "candidate", "data-dir"):
        ap.add_argument(f"--{name}", required=True)
    args = ap.parse_args(argv)
    git = Git(args.repo)
    try:
        revs = {k: git.resolve(getattr(args, k)) for k in ("from", "onto", "head", "candidate")}
    except BadRevision as exc:
        print(f"ERROR: cannot resolve revision {exc} in {args.repo}", file=sys.stderr)
        return 2
    return Checker(git, revs, Path(args.data_dir)).run()


if __name__ == "__main__":
    sys.exit(main())
