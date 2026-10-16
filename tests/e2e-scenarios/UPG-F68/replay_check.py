#!/usr/bin/env python3
"""UPG-F68 replay-fidelity check (merge gate 2, read-only). Ships as tests/e2e-scenarios/UPG-F68/replay_check.py.

The candidate is the selected tag + C1 (the carry of PR #79 head H5 onto the tag) + the ``-x`` picks + in-surface
commits. Checks, each named after ADR §Merge gate 2:
  (a) C1^ == tag, linear history; every H5 delta path (``git diff --name-status <9.24> <H5>``) is R / M / M-conflict /
      E / U-del at C1, C1 adds no other path, and every H5-added line (whitespace-stripped) of an in-surface or
      old-gated path is present at C1 or ledgered.
  (b) each pick is exactly one ``-x`` commit, in order, whose diff equals its source's diff path by path.
  (c) per-function replay of every H5 core file: in each scope the H5 diff touches, for every stripped line L,
      count@C1(L) == count@tag(L) + count@H5(L) - count@9.24(L), less ledgered translations. The H5 core delta is
      decomposed into the GOV-F25 re-home (9.24..C2_F67), WP-D1 (C2_F67..WP_D1_F67, equal to the approved patch
      bytes) and nothing else; MSG-DELIV is E; F62.g is HELD while tools/bot_mode_dm.py equals the tag, and item-4
      once it is the tag plus exactly the ITEM4 lines.
  (d) the A1 union rule for ``_kanban_sub_op`` (the same equation over the whole function), with its three locators.
  (e) no core path changes after C1 except item 4 (ITEM4, only in ``[UPG-F68 4]`` commits) and
  (e') comment-only ``# health: allow <RULE> -- <why>`` waivers at the E2_ALLOW sites, each file's AST identical
       at C1 and the head (erratum E2.3a); the list is printed.
  (f) an old-gated desktop file changes after C1 only by a recorded import re-point (--repoints): each recorded line is
      a line of an ``import`` statement (multi-line ones included) at C1 (before) or at the head (after).
  (g) fork-ci (user rulings, operator msgs 596/602): a workflow file changes after C1 only in ``[UPG-F68 fork-ci]``
      commits, only on ``runs-on`` / matrix ``runner`` lines, each line being its C1 text with the 32-core labels mapped
      to standard runners, plus the ``timeout-minutes`` raise of the FORK_CI_TIMEOUTS jobs (msg 602: only a slice that
      timed out on a standard runner), the FORK_CI_LINES substitutions and the FORK_CI_INSERTS blocks (only a removable
      block may later be removed), each admitted once under its recorded authority; no workflow line outside a
      comment still names a 32-core label at the head; and the only ``contributors/`` change is adding the FORK_CI_CONTRIBUTORS files with exactly their recorded content (msg 990).
--inventory: one row per path (plus the ``path#scope`` core rows); status, surface, class and origin must equal this
check's result, core rows name their authority, conflict rows carry locators that resolve.
"""
import argparse
import ast
import csv
import hashlib
import os
import re
import io
import subprocess
import sys
import tokenize
from collections import Counter

NINE24 = "f97608f178d1ffeca59860195ab7da295f7c8e5f"
C2_F67 = "f602f80b4a"  # UPG-F67 C2: GOV-F25 + GOV-F25.b re-homed onto 9.24 (an ancestor of H5)
WP_D1_F67 = "0cd8762e2b"  # UPG-F67 WP-D1 (operator-approved), the commit whose core diff is the approved patch
WP_D1_FILES = ("gateway/platforms/api_server.py", "gateway/platforms/api_server_runs.py", "gateway/wake.py")
WP_D1_PATCH = "tests/e2e-scenarios/UPG-F67/wp_d1_approved.patch"
WP_D1_PATCH_SHA256 = "5d7db2e0ea188b991d1e16d2138748230059a4b5d0ae25303929136ec76f7354"
WP_D1_SCOPES = {
    "gateway/platforms/api_server.py": "APIServerAdapter.run_internal_session_turn",
    "gateway/platforms/api_server_runs.py": "run_internal_session_turn",
    "gateway/wake.py": "_self_post_chat_completion",
}
A1 = ("gateway/kanban_watchers.py", "GatewayKanbanWatchersMixin._kanban_sub_op")
A2 = ("gateway/platforms/api_server_openai_routes.py", "OpenAICompatRoutesMixin._run_idempotent")
C1_CONFLICTS = ("gateway/kanban_watchers.py", "gateway/platforms/api_server_openai_routes.py",
                "tests/hermes_cli/test_config_read_guard.py", "tests/hermes_cli/test_kanban_notify.py",
                "website/docs/user-guide/configuration.md")
PICK_CONFLICTS = {("1fb4d74ccb", "plugins/nv-coworker-compose/openshell/installer.py")}
# Authority per H5 core path (ADR §CORE-CHANGE A/B); an inventory row may name only these.
CORE_AUTHORITY = {
    "agent/session_persistence.py": {"GOV-F25"},
    "agent/turn_facade.py": {"GOV-F25"},
    "agent/turn_finalizer.py": {"GOV-F25"},
    "gateway/kanban_watchers.py": {"GOV-F25", "GOV-F25.b"},
    "gateway/kanban_watchers_notifier.py": {"GOV-F25", "GOV-F25.b"},
    "gateway/platforms/api_server.py": {"GOV-F25", "GOV-F25.b", "WP-D1"},
    "gateway/platforms/api_server_openai_routes.py": {"GOV-F25", "GOV-F25.b"},
    "gateway/platforms/api_server_runs.py": {"WP-D1"},
    "gateway/wake.py": {"GOV-F25", "GOV-F25.b", "WP-D1"},
    "hermes_cli/kanban_db.py": {"GOV-F25", "GOV-F25.b"},
    "hermes_cli/kanban_db_connect.py": {"GOV-F25", "GOV-F25.b"},
    "hermes_cli/kanban_db_notify.py": {"GOV-F25", "GOV-F25.b"},
}
# Retained core with no hunk at the candidate (ADR §CORE-CHANGE C/D): the path must equal the tag, or, for ITEM4's path,
# the tag plus exactly ITEM4's lines.
RETAINED = {
    "tools/bot_mode_dm.py#MSG-DELIV": ("tools/bot_mode_dm.py", "E", "MSG-DELIV"),
    "tools/bot_mode_dm.py#F62.g": ("tools/bot_mode_dm.py", "HELD", "F62.g"),
}
# Item 4 (ADR §CORE-CHANGE D, erratum E5.7): the core delta of tools/bot_mode_dm.py from the tag, as (enclosing
# function, raw line) multisets of added and removed lines.
ITEM4 = ("tools/bot_mode_dm.py", "F62.g", {
    "+": (("cleanup_bot_dm_cache", '        locations += [(dm_dir, "*.signal"), (dm_dir, "*.signal.tmp")]'),
          ("_spawn_delivery", "                            _completion_output_chars=REPLY_COMPLETION_CHARS,"),
          ("_spawn_delivery", "                            persist_on_release=True)")),
    "-": (("_spawn_delivery", "                            _completion_output_chars=REPLY_COMPLETION_CHARS)"),),
})
U_DEL = ()
# Erratum E2.3a: the only core lines allowed after C1, as (path, rule, the stripped code line the waiver sits on or
# None for a new first line, the authority of the hunk it waives). Matched by code line, not line number, so the
# table survives a re-carry onto another tag.
E2_ALLOW = (
    ("gateway/kanban_watchers.py", "BLE001", "except Exception as exc:", "GOV-F25"),
    ("gateway/platforms/api_server.py", "BLE001", "except Exception:", "GOV-F25.b"),
    ("gateway/platforms/api_server.py", "FILE_LINES", None, "GOV-F25.b,WP-D1"),
    ("hermes_cli/kanban_db.py", "FILE_LINES", None, "GOV-F25"),
)
HEALTH_ALLOW = re.compile(r"^# health: allow ([A-Z0-9_, ]+) -- .+$")
SURFACE = re.compile(r"^(plugins/|website/docs/|tests/|apps/desktop/e2e/[^/]+-ac\d+\.spec\.ts$)")
ITEM = re.compile(r"\[UPG-F68 (\d+|fork-ci)[a-z]?\]")  # sub-items (5a/5b/5c) record under their item
FORK_CI = re.compile(r"^(\.github/workflows/[^/]+\.ya?ml|contributors/.+)$")
FORK_CI_AUTHORITY = "msg-596"
# (workflow, job) -> raised timeout-minutes. Msg 602: a Python test slice that timed out on a standard runner.
FORK_CI_TIMEOUTS = {(".github/workflows/tests.yml", "test"): 90}
# (workflow, exact line at C1) -> (replacement, authority). Attribution must use the PR base branch on pull_request,
# not origin/main.
FORK_CI_LINES = {
    (".github/workflows/contributor-check.yml", "          MERGE_BASE=$(git merge-base origin/main HEAD)\n"):
        ("          MERGE_BASE=$(git merge-base origin/${{ github.event_name == 'pull_request' && github.base_ref"
         " || 'main' }} HEAD)\n", "msg-990"),
    (".github/workflows/tests.yml", "          HERMES_TEST_WORKERS: 32\n"):
        ("          HERMES_TEST_WORKERS: 4\n", "msg-208"),
    (".github/workflows/tests.yml", "          HERMES_TEST_FILE_TIMEOUT: 600\n"):
        ("          HERMES_TEST_FILE_TIMEOUT: 1800\n", "msg-208"),
    (".github/workflows/tests-os.yml",
     "          HERMES_TEST_WORKERS: ${{ matrix.marker == 'windows' && (runner.arch == 'ARM64' && '16' || '16') || '' }}\n"):
        ("          HERMES_TEST_WORKERS: ${{ matrix.marker == 'windows' && (runner.arch == 'ARM64' && '4' || '4') || '' }}\n",
         "msg-208"),
}
_INSTALL_STEP = r'''
      - name: Install the project into the test environment
        # Route-script children import hermes_cli by module path, and run_tests.sh's env -i drops
        # PYTHONPATH, so the project is installed into the venv itself. --no-deps: the locked set is unchanged.
        run: uv pip install --python "$HERMES_PYTHON" --no-deps -e .
'''
_PTH_STEP = r'''
      - name: Put the checkout on the test environment's import path
        # Route-script children import hermes_cli by module path, and run_tests.sh's env -i drops PYTHONPATH. Minted -I
        # launchers and child trees that are their own whole sys.path must still resolve only their own files, so the
        # finder serves the project's top-level names, after every other finder, and never under -I.
        run: |
          "$HERMES_PYTHON" - "$GITHUB_WORKSPACE" <<'PY'
          import pathlib, sys, sysconfig, tomllib
          root = pathlib.Path(sys.argv[1])
          found = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]["packages"]["find"]
          names = sorted({p.split(".")[0] for p in found["include"]} | {f.stem for f in root.glob("*.py") if f.name != "setup.py"})
          finder = ("import sys, importlib.machinery; sys.flags.isolated or 'HermesCheckoutFinder' in "
                    "[getattr(f, '__name__', '') for f in sys.meta_path] or sys.meta_path.append(type("
                    "'HermesCheckoutFinder', (), {'find_spec': staticmethod(lambda name, path=None, target=None, "
                    "_f=importlib.machinery.PathFinder, _n=frozenset(%r): _f.find_spec(name, [%r]) "
                    "if path is None and name in _n else None)}))\n")
          pathlib.Path(sysconfig.get_paths()["purelib"], "hermes-checkout.pth").write_text(finder % (names, str(root)), encoding="utf-8")
          PY
'''
_GIT_DIAG_STEP = r'''
      - name: Diagnose git-core candidates (read-only)
        # Read-only: for each path _locate_real_git accepts, whether it exists, its direct --version,
        # and the fork-bomb probe; plus PATH git. Removed on the next push.
        if: matrix.marker == 'windows' && runner.arch == 'X64'
        shell: bash
        run: |
          "$HERMES_PYTHON" - <<'PY'
          import shutil, subprocess
          from pathlib import Path
          from hermes_cli import update_cmd, update_cmd_git
          def version(argv):
              try:
                  r = subprocess.run([*argv, "--version"], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
                  return f"rc={r.returncode} out={r.stdout.strip()!r} err={r.stderr.strip()[:200]!r}"
              except Exception as exc:
                  return f"error={exc!r}"
          candidates = [Path(b) / "Git" / a / "libexec" / "git-core" / "git.exe"
                        for b in (r"C:\Program Files", r"C:\Program Files (x86)") for a in ("clangarm64", "mingw64", "mingw32")]
          candidates += update_cmd_git._portable_git_candidates()
          for c in candidates:
              print(f"candidate {c}: exists={c.exists()}")
              if c.exists():
                  print(f"  --version {version([str(c)])}")
                  print(f"  _probe_fork_bomb={update_cmd_git._probe_fork_bomb([str(c)])!r}")
          path_git = shutil.which("git")
          print(f"PATH git: {path_git!r}")
          if path_git:
              print(f"  --version {version([path_git])}")
              print(f"  _probe_fork_bomb={update_cmd_git._probe_fork_bomb([path_git])!r}")
          print(f"_locate_real_git()={update_cmd._locate_real_git()!r}")
          PY
'''
# (workflow, job, exact line at C1 it follows [, discriminator]) -> (inserted block, authority, removable). A
# discriminator tells apart blocks that share an anchor; only a removable block may disappear later.
FORK_CI_INSERTS = {
    (".github/workflows/tests.yml", "test", "          ref: ${{ inputs.commit }}\n"):
        ("          fetch-depth: 0\n", "msg-226", False),
    (".github/workflows/tests-os.yml", "os-tests", "          HERMES_TEST_SLICE: ${{ matrix.slice || '' }}\n"):
        ("          HERMES_TEST_FILE_TIMEOUT: ${{ matrix.marker == 'windows' && '1800' || '' }}\n", "msg-226", False),
    (".github/workflows/tests.yml", "test", "          prune-python-cache: true\n"): (_INSTALL_STEP, "msg-232", True),
    (".github/workflows/tests.yml", "test", "          prune-python-cache: true\n", "msg-252"): (_PTH_STEP, "msg-252", False),
    (".github/workflows/tests-os.yml", "os-tests", "          prune-python-cache: true\n"): (_GIT_DIAG_STEP, "msg-232", True),
}
# Blocks that never coexist at one revision: the install makes hermes_bootstrap importable under -I, which is what
# the .pth exists to avoid (a minted launcher then skips the closure repair).
FORK_CI_EXCLUSIVE = (
    ((".github/workflows/tests.yml", "test", "          prune-python-cache: true\n"),
     (".github/workflows/tests.yml", "test", "          prune-python-cache: true\n", "msg-252")),
)
FORK_CI_CONTRIBUTORS = {
    "contributors/emails/harsh1492@gmail.com": "szihs\n",
    "contributors/emails/hermes-builder@slang-coworkers.local":
        "szihs\n# hermes-builder bot commits, pushed under this account\n",
}
TIMEOUT_LINE = re.compile(r"^(\s+timeout-minutes: )\d+(\s*)$")
JOB_KEY = re.compile(r"^  ([A-Za-z0-9_-]+):\s*$")
FORK_CI_RUNNERS = {
    "ubuntu-latest-32-core": "ubuntu-latest",
    "ubuntu-latest-32-arm-core": "ubuntu-24.04-arm",
    "windows-latest-32-core": "windows-latest",
    "windows-latest-32-arm-core": "windows-11-arm",
}
RUNNER_LABEL = re.compile(r"\b(?:ubuntu|windows)-latest-32-(?:arm-)?core\b")
RUNNER_KEY = re.compile(r"^\s*(?:-\s+)?(?:runs-on|runner):")
CHERRY = re.compile(r"^\(cherry picked from commit ([0-9a-f]{40})\)$", re.MULTILINE)
LOCATOR = re.compile(r"^(tag|H5|C1|HEAD|[0-9a-f]{10,40}):([^:]+):(\d+)-(\d+)$")
_SPEC = r"['\"][^'\"\n]+['\"]"
IMPORT_ONE_LINE = re.compile(rf"^import\s+(?:type\s+)?(?:[\w$]+\s*,\s*)?(?:\{{[^{{}}]*\}}|\*\s+as\s+[\w$]+|[\w$]+)\s+from\s+{_SPEC}\s*;?$"
                             rf"|^import\s+{_SPEC}\s*;?$")
IMPORT_OPEN = re.compile(r"^import\s+(?:type\s+)?(?:[\w$]+\s*,\s*)?\{$")
IMPORT_MEMBER = re.compile(r"^(?:type\s+)?[\w$]+(?:\s+as\s+[\w$]+)?,?$")
IMPORT_CLOSE = re.compile(rf"^\}}\s*from\s+{_SPEC}\s*;?$")
DIFF_ARGS = ("-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false", "-c", "core.quotePath=true",
             "-c", "color.diff=false", "-c", "core.abbrev=10", "diff", "--no-ext-diff", "--no-textconv", "--no-color",
             "--no-renames", "--no-relative", "-U0", "--diff-algorithm=myers")


def git(repo, *args, ok=(0,)):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, encoding="utf-8", check=False,
                       errors="surrogateescape", stdin=subprocess.DEVNULL)
    if r.returncode not in ok:
        sys.exit(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def blob(repo, rev, path):
    return git(repo, "rev-parse", "-q", "--verify", f"{rev}:{path}", ok=(0, 1)).strip() or None


_TEXT = {}


def text_at(repo, rev, path):
    if (rev, path) not in _TEXT:
        _TEXT[(rev, path)] = git(repo, "show", f"{rev}:{path}") if blob(repo, rev, path) else ""
    return _TEXT[(rev, path)]


def name_status(repo, a, b):
    out = {}
    for line in git(repo, "diff", "--no-renames", "--name-status", a, b).splitlines():
        st, *paths = line.split("\t")
        out[paths[-1]] = st[0]
    return out


def diff_lines(repo, a, b, path):
    """(+lines, -lines) of ``git diff -U0 a b -- path``, raw (no strip), with old/new line numbers."""
    plus, minus, old_no, new_no = [], [], 0, 0
    for ln in git(repo, *DIFF_ARGS, a, b, "--", path).splitlines():
        if ln.startswith("@@"):
            m = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", ln)
            old_no, new_no = int(m.group(1)), int(m.group(2))
        elif ln.startswith(("---", "+++", "diff ", "index ", "new file", "deleted file", "\\")):
            continue
        elif ln.startswith("+"):
            plus.append((new_no, ln[1:]))
            new_no += 1
        elif ln.startswith("-"):
            minus.append((old_no, ln[1:]))
            old_no += 1
    return plus, minus


def is_core(path, h5_paths):
    """Outside the allowed diff surface. A fork-modified non-ac desktop file of the H5 delta is old-gated, not core."""
    if SURFACE.match(path):
        return False
    return not (path.startswith("apps/desktop/e2e/") and path in h5_paths)


def surface_of(path, h5_paths):
    if FORK_CI.match(path) and path not in h5_paths:
        return "fork-ci"
    if is_core(path, h5_paths):
        return "core"
    return "old-gated" if path.startswith("apps/desktop/e2e/") and not SURFACE.match(path) else "in-surface"


_SCOPES = {}


def scope_map(text):
    """Line number (1-based) -> innermost enclosing def/class qualname; ``<module>`` outside any."""
    if text in _SCOPES:
        return _SCOPES[text]
    tree = ast.parse(text)
    owner = {}

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                start = min([child.lineno, *(d.lineno for d in child.decorator_list)])
                for n in range(start, child.end_lineno + 1):
                    owner[n] = name
                walk(child, name + ".")
            else:
                walk(child, prefix)

    walk(tree, "")
    _SCOPES[text] = owner
    return owner


def scope_lines(text, scope):
    """Stripped non-empty lines of one scope (``<module>`` = lines outside every def/class)."""
    owner = scope_map(text) if text else {}
    out = Counter()
    for n, ln in enumerate(text.splitlines(), 1):
        if ln.strip() and owner.get(n, "<module>") == scope:
            out[ln.strip()] += 1
    return out


def scope_span(text, scope):
    lines = [n for n, s in scope_map(text).items() if s == scope] if text else []
    return (min(lines), max(lines)) if lines else None


def delta(new, old):
    keys = set(new) | set(old)
    return {k: new[k] - old[k] for k in keys if new[k] != old[k]}


def touched_scopes(repo, a, b, path):
    """Scopes the diff a..b touches: + lines by their scope at b, - lines by their scope at a."""
    plus, minus = diff_lines(repo, a, b, path)
    at_b, at_a = scope_map(text_at(repo, b, path) or "\n"), scope_map(text_at(repo, a, path) or "\n")
    return ({at_b.get(n, "<module>") for n, ln in plus if ln.strip()}
            | {at_a.get(n, "<module>") for n, ln in minus if ln.strip()})


def replay_scope(repo, revs, path, scope):
    """The 3-way-merge invariant per scope: C1 == tag + (H5 - 9.24), split into the GOV-F25 and WP-D1 parts.
    Returns (gov, wpd1, residual) as signed line deltas; the residual must be empty."""
    t = {k: scope_lines(text_at(repo, r, path), scope) for k, r in revs.items()}
    gov = delta(t["c2_f67"], t["nine24"])
    wpd1 = delta(t["wpd1_f67"], t["c2_f67"])
    got = delta(t["c1"], t["tag"])
    want = Counter()
    for d in (gov, wpd1):
        for k, v in d.items():
            want[k] += v
    residual = {k: got.get(k, 0) - want.get(k, 0) for k in set(got) | set(want) if got.get(k, 0) != want.get(k, 0)}
    return gov, wpd1, residual


def check_core(repo, revs, core_paths, allowed, out):
    """(c). Returns fails and the observed ``path#scope`` inventory rows. ``allowed`` maps (path, scope) to the
    residual a ruled hand resolution leaves (A2); every other scope must replay with no residual."""
    fails, rows = [], {}
    approved = open(git(repo, "rev-parse", "--show-toplevel").strip() + "/" + WP_D1_PATCH, "rb").read()
    if hashlib.sha256(approved).hexdigest() != WP_D1_PATCH_SHA256:
        fails.append(f"WP-D1: {WP_D1_PATCH} is not the approved patch (sha256 mismatch)")
    got = git(repo, *DIFF_ARGS, revs["c2_f67"], revs["wpd1_f67"], "--", *WP_D1_FILES).encode("utf-8")
    if got != approved:
        fails.append(f"WP-D1: diff {C2_F67}..{WP_D1_F67} over the WP-D1 files is not the approved patch, byte for byte")
    for path in core_paths:
        tail = name_status(repo, revs["wpd1_f67"], revs["h5"])
        if path in tail:
            fails.append(f"CORE-UNAUTHORIZED: {path} changes between WP-D1 ({WP_D1_F67}) and H5")
        if git(repo, "diff", "--name-only", revs["wpd1_f67"], revs["c2_f67"], "--", path).strip() and \
                path not in WP_D1_FILES:
            fails.append(f"CORE-UNAUTHORIZED: {path} changes in WP-D1 but is not a WP-D1 file")
        scopes = touched_scopes(repo, revs["nine24"], revs["h5"], path)
        for scope in sorted(scopes):
            gov, wpd1, residual = replay_scope(repo, revs, path, scope)
            auth = ",".join(a for a, d in (("GOV-F25", gov), ("WP-D1", wpd1)) if d)
            if wpd1 and WP_D1_SCOPES.get(path) != scope:
                fails.append(f"WP-D1 outside its scope: {path}#{scope}")
            if residual != allowed.get((path, scope), {}):
                first = sorted(residual.items())[0] if residual else ("(missing ruled residual)", 0)
                fails.append(f"CORE-REPLAY: {path}#{scope}: {len(residual)} line(s) differ from tag + H5 delta, "
                             f"first {first[1]:+d} {first[0][:100]!r}")
            rows[f"{path}#{scope}"] = ("M", "core", "replay", "H5", auth or "none")
            print(f"core\t{path}#{scope}\tgov={sum(map(abs, gov.values()))}\twpd1={sum(map(abs, wpd1.values()))}"
                  f"\tresidual={sum(map(abs, residual.values()))}", file=out)
        whole = {k: text_at(repo, r, path) for k, r in revs.items()}
        exp = Counter()
        for k, sign in (("tag", 1), ("h5", 1), ("nine24", -1)):
            for ln in whole[k].splitlines():
                if ln.strip():
                    exp[ln.strip()] += sign
        for (ap, _scope), res in allowed.items():
            if ap == path:
                for k, v in res.items():
                    exp[k] += v
        have = Counter(ln.strip() for ln in whole["c1"].splitlines() if ln.strip())
        if {k: v for k, v in exp.items() if v} != {k: v for k, v in have.items() if v}:
            fails.append(f"CORE-REPLAY: {path}: whole-file line multiset != tag + (H5 - 9.24)")
    return fails, rows


def check_a1(repo, revs, out):
    """(d): ``_kanban_sub_op`` at C1 is exactly the tag body plus GOV-F25's change; prints the three locators."""
    path, scope = A1
    loc = {k: scope_span(text_at(repo, revs[k], path), scope) for k in ("tag", "h5", "c1")}
    for k, label in (("tag", "upstream @tag"), ("h5", "GOV-F25 @H5"), ("c1", "resolved @C1")):
        print(f"A1 locator {label}: {path}:{loc[k][0]}-{loc[k][1]}" if loc[k] else f"A1 locator {label}: MISSING",
              file=out)
    gov, wpd1, residual = replay_scope(repo, revs, path, scope)
    fails = []
    if residual or wpd1 or not loc["c1"]:
        fails.append(f"A1: {path}#{scope} is not exactly the union of the tag body and GOV-F25's change")
    tag_txt, c1_txt = text_at(repo, revs["tag"], path), text_at(repo, revs["c1"], path)
    t_span, c_span = scope_span(tag_txt, scope), scope_span(c1_txt, scope)
    t_body = tag_txt.splitlines()[t_span[0] - 1:t_span[1]] if t_span else []
    c_body = c1_txt.splitlines()[c_span[0] - 1:c_span[1]] if c_span else []
    union = [ln.replace("**extra: Any) -> None:", "**extra: Any) -> Any:", 1).replace(
        "getattr(_kbn, op)(", "return getattr(_kbn, op)(", 1) for ln in t_body]
    if c_body != union:
        fails.append("A1: _kanban_sub_op at C1 is not byte-for-byte the tag body with GOV-F25's `-> Any` and `return`")
    pin = "with _kb.pin_first_board_resolution():"
    body = scope_lines(text_at(repo, revs["c1"], path), scope)
    if not body[pin] or not body["return getattr(_kbn, op)("]:
        fails.append("A1: the env-pin wrap or GOV-F25's return is missing at C1")
    for k, v in sorted(gov.items()):
        print(f"A1 GOV-F25 line {v:+d}: {k}", file=out)
    return fails


def check_a2(repo, revs, out):  # -> (fails, allowed residual)
    """A2: the only hand-resolved line of ``_run_idempotent`` is the tag's PEP 585 signature line plus the kwarg."""
    path, scope = A2
    tag_sig = "log_label: str, fingerprint_keys: list[str], route: str) -> tuple:"
    want = tag_sig.replace("route: str)", "route: str, require_persist: bool = False)")
    gov, wpd1, residual = replay_scope(repo, revs, path, scope)
    h5_sig = next((k for k, v in gov.items() if v > 0 and "require_persist: bool = False)" in k), None)
    old_sig = next((k for k, v in gov.items() if v < 0 and k.startswith("log_label:")), None)
    allowed = {want: 1, tag_sig: -1}
    if h5_sig:
        allowed[h5_sig] = allowed.get(h5_sig, 0) - 1
    if old_sig:
        allowed[old_sig] = allowed.get(old_sig, 0) + 1
    span = scope_span(text_at(repo, revs["c1"], path), scope)
    print(f"A2 locators: upstream @tag {path}:{scope_span(text_at(repo, revs['tag'], path), scope)}, "
          f"GOV-F25.b @H5 {path}:{scope_span(text_at(repo, revs['h5'], path), scope)}, resolved @C1 {path}:{span}",
          file=out)
    allowed = {k: v for k, v in allowed.items() if v}
    if residual == allowed and not wpd1 and h5_sig and old_sig:
        return [], {(path, scope): allowed}
    return [f"A2: {path}#{scope} differs from tag + GOV-F25.b by more than the signature line: "
            f"{sorted(residual.items())[:4]}"], {(path, scope): allowed}


def load_ledger(path, repo, revs):
    """In-surface translations: path, stripped H5-added line, site locator(s) (``tag:`` / ``C1:``) where the line's
    content lives at C1, reason. Every site must resolve."""
    led, bad = {}, []
    if not path:
        return led, bad
    with open(path, encoding="utf-8", newline="") as fh:
        for n, row in enumerate(csv.reader(fh, delimiter="\t"), 1):
            if not row or not row[0] or row[0].startswith("#"):
                continue
            p, h5_line, sites, reason = (row + [""] * 4)[:4]
            if not (h5_line.strip() and sites.strip() and reason.strip()) or locators_resolve(repo, sites, revs):
                bad.append(f"LEDGER row {n}: needs an H5 line, resolving tag:/C1: site locator(s) and a reason")
                continue
            led.setdefault(p, set()).add(h5_line.strip())
    return led, bad


def commits_after(repo, base, head):
    out = []
    for sha in git(repo, "rev-list", "--reverse", f"{base}..{head}").split():
        msg = git(repo, "log", "-1", "--format=%B", sha)
        src = CHERRY.findall(msg)
        item = ITEM.search(msg.splitlines()[0] if msg else "")
        tag = item.group(1) if item else None
        out.append((sha, src[-1] if src else None, int(tag) if tag and tag.isdigit() else tag))
    return out


def import_statement_lines(text):
    """Stripped lines of the well-formed ``import`` statements of a TS/JS file: a whole one-line import, or every line
    of a multi-line ``import {`` block whose lines are all bare specifiers and which closes with ``} from '...'``.
    Fails closed: a block holding any other line (a comment, code) contributes nothing, so a re-point that moves a
    name between imports is import-only and nothing else is."""
    out, block = set(), None
    for ln in text.splitlines():
        stripped = ln.strip()
        if block is None:
            if IMPORT_ONE_LINE.match(stripped):
                out.add(stripped)
            elif IMPORT_OPEN.match(stripped):
                block = [stripped]
        elif IMPORT_MEMBER.match(stripped):
            block.append(stripped)
        elif IMPORT_CLOSE.match(stripped):
            out.update(block + [stripped])
            block = None
        else:
            block = None
    return out


def multiset_diff(repo, a, b, path):
    plus, minus = diff_lines(repo, a, b, path)
    return Counter(ln.strip() for _n, ln in plus if ln.strip()), Counter(ln.strip() for _n, ln in minus if ln.strip())


def locators_resolve(repo, cell, revs):
    """Each locator ``<rev>:<path>:<a>-<b>`` names a line range that exists at that revision."""
    bad = []
    for tok in cell.split():
        m = LOCATOR.match(tok)
        rev = m and {"tag": revs["tag"], "H5": revs["h5"], "C1": revs["c1"], "HEAD": revs["head"]}.get(m.group(1), m.group(1))
        n = len(text_at(repo, rev, m.group(2)).splitlines()) if m else 0
        if not (m and 1 <= int(m.group(3)) <= int(m.group(4)) <= n):
            bad.append(tok)
    return bad


def check_h5_paths(repo, revs, h5_ns, led, out):
    """(a) per H5 delta path at C1. Returns fails, observed {path: (status, surface, class)}, unaccounted list."""
    fails, observed, flagged = [], {}, []
    h5_paths = set(h5_ns)
    for path in sorted(h5_ns):
        st = h5_ns[path]
        cur, new, tag, old = (blob(repo, revs[k], path) for k in ("c1", "h5", "tag", "nine24"))
        surf = surface_of(path, h5_paths)
        if cur == new:
            cls = "R"
        elif cur is None and tag is None and old is not None:
            cls = "U-del" if path in U_DEL else "U-UNRULED"
        elif cur is None:
            cls = "DROPPED"
        elif cur == tag:
            cls = "E"
        elif path in C1_CONFLICTS:
            cls = "M-conflict"
        elif tag == old:
            cls = "DIVERGED"
        else:
            cls = "M"
        if cls in ("DROPPED", "DIVERGED", "U-UNRULED"):
            fails.append(f"{cls}: {path}")
        if surf != "core" and cls in ("M", "M-conflict", "E"):
            have = Counter(ln.strip() for ln in text_at(repo, revs["c1"], path).splitlines() if ln.strip())
            added, _removed = multiset_diff(repo, revs["nine24"], revs["h5"], path)
            missing = [ln for ln in added if have[ln] < 1 and ln not in led.get(path, ())]
            if missing:
                flagged.append((path, len(missing), missing[0][:100]))
                fails.append(f"UNACCOUNTED: {path}: {len(missing)} H5-added line(s) neither at C1 nor ledgered")
        observed[path] = (st, surf, cls, "H5")
        print(f"{cls}\t{surf}\t{st}\t{path}", file=out)
    return fails, observed, flagged


def check_picks(repo, picks, after_c1):
    """(b): picks land in order, each as exactly one ``-x`` commit whose diff equals its source's diff."""
    fails, landed = [], {}
    for sha, src, _item in after_c1:
        if src:
            landed.setdefault(src, []).append(sha)
    order = []
    for p in picks:
        full = git(repo, "rev-parse", f"{p}^{{commit}}").strip()
        got = landed.get(full, [])
        if len(got) != 1:
            fails.append(f"PICK: {p} lands {len(got)} time(s) as a -x commit (want exactly 1)")
            continue
        order.append(got[0])
        mine, theirs = name_status(repo, f"{got[0]}^", got[0]), name_status(repo, f"{full}^", full)
        if mine != theirs:
            fails.append(f"PICK: {p}: path set/status differs from its source")
        for path in sorted(set(mine) & set(theirs)):
            # A recorded conflict (PICK_CONFLICTS) was resolved by keeping both sides byte for byte, so its line
            # multiset must still equal the source's; only the hunk position may differ.
            if multiset_diff(repo, f"{got[0]}^", got[0], path) != multiset_diff(repo, f"{full}^", full, path):
                fails.append(f"PICK: {p}: {path} diff differs from the source's")
    shas = [s for s, _src, _i in after_c1]
    if order != sorted(order, key=shas.index):
        fails.append("PICK: picks are not in the order " + " -> ".join(picks))
    return fails


def check_item4(repo, tag, head, after_c1):
    """Item 4: ITEM4's path differs from the tag by exactly ITEM4's lines, each in its recorded function, and changes
    after C1 only in ``[UPG-F68 4]`` commits. Returns fails."""
    path, auth, want = ITEM4
    fails = [f"{auth}: {sha[:10]} changes {path} outside a [UPG-F68 4] commit"
             for sha, _src, item in after_c1 if item != 4 and path in name_status(repo, f"{sha}^", sha)]
    plus, minus = diff_lines(repo, tag, head, path)
    scopes = {"+": scope_map(text_at(repo, head, path)), "-": scope_map(text_at(repo, tag, path))}
    got = {"+": Counter((scopes["+"].get(n, "<module>"), ln) for n, ln in plus),
           "-": Counter((scopes["-"].get(n, "<module>"), ln) for n, ln in minus)}
    for side in "+-":
        if got[side] != Counter(want[side]):
            extra = sorted((got[side] - Counter(want[side])).elements())
            missing = sorted((Counter(want[side]) - got[side]).elements())
            fails.append(f"{auth}: {path} {side} lines vs the tag differ from ITEM4 (extra {extra[:2]}, missing "
                         f"{missing[:2]})")
    return fails


def check_e2_allow(repo, c1, head, path):
    """2(e'): ``path`` changes from C1 to the head only by added ``# health: allow`` comments at its E2_ALLOW sites, and
    its AST is identical (comments and line positions are not in ``ast.dump``). Returns (fails, allowed sites)."""
    sites = [(rule, code) for f, rule, code, _auth in E2_ALLOW if f == path]
    if not sites:
        return [f"CORE-AFTER-C1: {path} (no later core is authorized in this candidate)"], []
    old, new = text_at(repo, c1, path), text_at(repo, head, path)
    fails, found = [], []
    if ast.dump(ast.parse(old)) != ast.dump(ast.parse(new)):
        fails.append(f"E2-ALLOW: {path}: the AST differs between C1 and the head")
    plus, minus = diff_lines(repo, c1, head, path)
    new_lines = new.splitlines()
    by_new = dict(plus)
    removed = {ln for _n, ln in minus}
    for n, ln in plus:
        code, sep, comment = ln.partition("  # health: allow ")
        if not ln.lstrip().startswith("#") and sep:
            match, rest = HEALTH_ALLOW.match("# health: allow " + comment), code.strip()
            if code not in removed:
                fails.append(f"E2-ALLOW: {path}:{n}: the waiver's code line is not an existing line at C1")
        elif ln.startswith("# health: allow ") and n == 1:
            match, rest = HEALTH_ALLOW.match(ln), None
        else:
            fails.append(f"E2-ALLOW: {path}:{n}: not a comment-only waiver line: {ln.strip()[:100]!r}")
            continue
        if not match:
            fails.append(f"E2-ALLOW: {path}:{n}: waiver does not match `# health: allow <RULE> -- <why>`")
            continue
        rules = {r for r in re.split(r"[,\s]+", match.group(1).strip()) if r}
        hit = [(r, c) for r, c in sites if r in rules and c == rest]
        if len(rules) != 1 or not hit:
            fails.append(f"E2-ALLOW: {path}:{n}: {sorted(rules)} on {rest!r} is not an E2_ALLOW site")
            continue
        sites.remove(hit[0])
        found.append((path, n, hit[0][0], (by_new.get(n) or new_lines[n - 1]).strip()[:60]))
    if len(minus) != sum(1 for _n, ln in plus if not ln.startswith("# health: allow ")):
        fails.append(f"E2-ALLOW: {path}: a removed line is not paired with its waiver-carrying replacement")
    fails.extend(f"E2-ALLOW: {path}: site {r} on {c!r} has no waiver at the head" for r, c in sites)
    return fails, found


def added_allows(repo, c1, head, path):
    """``(line, rules)`` of each ``# health: allow`` comment token at the head that C1 does not already carry."""
    def comments(text):
        try:
            toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
        except (tokenize.TokenError, SyntaxError):
            return []
        return [(t.start[0], t.string) for t in toks if t.type == tokenize.COMMENT and "health: allow" in t.string]
    before = Counter(c for _n, c in comments(text_at(repo, c1, path)))
    out = []
    for n, c in comments(text_at(repo, head, path)):
        if before[c]:
            before[c] -= 1
            continue
        m = HEALTH_ALLOW.match(c)
        out.append((n, ",".join(r for r in re.split(r"[,\s]+", m.group(1).strip()) if r) if m else "?"))
    return out


def fork_ci_authority(path):
    """The attribution files carry msg-990 alone (no runner label in them); a workflow file carries msg-596, plus
    msg-602 for a FORK_CI_TIMEOUTS raise and the authority of each of its FORK_CI_LINES."""
    lines = list(dict.fromkeys([*(auth for (f, _was), (_now, auth) in FORK_CI_LINES.items() if f == path),
                                *(auth for k, (_blk, auth, _rm) in FORK_CI_INSERTS.items() if k[0] == path)]))
    if path in FORK_CI_CONTRIBUTORS or lines == ["msg-990"]:
        return "msg-990"
    timeout = ["msg-602"] if any(f == path for f, _job in FORK_CI_TIMEOUTS) else []
    return ",".join([FORK_CI_AUTHORITY, *timeout, *lines])


def _job_of(lines, n):
    """The ``jobs.<id>`` key whose block holds 1-based line ``n`` (the nearest two-space-indented key above it)."""
    return next((m.group(1) for ln in reversed(lines[:n - 1]) if (m := JOB_KEY.match(ln))), None)


def _fork_ci_line(path, old, n, was, now, used):
    """A changed workflow line is a runner-label mapping on a runs-on/runner key, a FORK_CI_LINES substitution (each
    admitted once per file, so a second copy of a ruled line elsewhere is not) or a FORK_CI_TIMEOUTS raise."""
    if RUNNER_KEY.match(was) and RUNNER_LABEL.sub(lambda m: FORK_CI_RUNNERS[m.group(0)], was) == now:
        return True
    if (path, was) not in used and FORK_CI_LINES.get((path, was), (None,))[0] == now:
        used.add((path, was))
        return True
    m, raised = TIMEOUT_LINE.match(was), FORK_CI_TIMEOUTS.get((path, _job_of(old, n)))
    return bool(m and raised and now == f"{m.group(1)}{raised}{m.group(2)}")


def _strip_fork_ci_inserts(path, lines):
    """``lines`` less each FORK_CI_INSERTS block found right after its anchor inside its job, at most once per
    anchor; returns (remaining lines, the keys of the blocks stripped)."""
    out, found, i = [], set(), 0
    while i < len(lines):
        key = next((k for k, (blk, _auth, _rm) in FORK_CI_INSERTS.items() if k[0] == path and k not in found and out
                    and out[-1] == k[2] and _job_of(out, len(out)) == k[1]
                    and lines[i:i + len(blk.splitlines(keepends=True))] == blk.splitlines(keepends=True)), None)
        if key:
            found.add(key)
            i += len(FORK_CI_INSERTS[key][0].splitlines(keepends=True))
            continue
        out.append(lines[i])
        i += 1
    return out, found


def _drop_fork_ci_inserts(path, old, new):
    """Both sides less their FORK_CI_INSERTS blocks. A block may appear between ``old`` and ``new``, and only a
    removable one may disappear. Returns (old less blocks, new less blocks, blocks added or removed, fails)."""
    old, had = _strip_fork_ci_inserts(path, old)
    new, has = _strip_fork_ci_inserts(path, new)
    fails = [f"FORK-CI: {path}: the inserted block after {k[2].strip()!r} is removed but is not removable"
             for k in sorted(had - has) if not FORK_CI_INSERTS[k][2]]
    fails += [f"FORK-CI: {path}: the {FORK_CI_INSERTS[a][1]} and {FORK_CI_INSERTS[b][1]} blocks after "
              f"{a[2].strip()!r} are both present" for a, b in FORK_CI_EXCLUSIVE if a in has and b in has]
    return old, new, len(has ^ had), fails


def _label_mapping(repo, a, b, path, where):
    """Fails unless ``path`` at ``b`` is ``path`` at ``a`` with runner labels mapped on runs-on/runner lines, the
    FORK_CI_TIMEOUTS raises and the FORK_CI_LINES substitutions, and nothing else; a ``contributors/`` path must be a
    FORK_CI_CONTRIBUTORS file, absent at ``a`` and holding exactly its recorded content at ``b``."""
    if path.startswith("contributors/"):
        if path not in FORK_CI_CONTRIBUTORS:
            return [f"FORK-CI: {path}: not a ruled contributor mapping"], 0
        if blob(repo, a, path) is not None or text_at(repo, b, path) != FORK_CI_CONTRIBUTORS[path]:
            return [f"FORK-CI: {path}: must be added {where} with exactly its ruled content"], 0
        return [], 1
    old, new = text_at(repo, a, path).splitlines(keepends=True), text_at(repo, b, path).splitlines(keepends=True)
    old, new, inserted, fails = _drop_fork_ci_inserts(path, old, new)
    if fails:
        return fails, 0
    if len(old) != len(new):
        return [f"FORK-CI: {path}: line count differs {where} (a label mapping keeps every line)"], 0
    fails, mapped, used = [], 0, set()
    for n, (was, now) in enumerate(zip(old, new), 1):
        if was == now:
            continue
        if _fork_ci_line(path, old, n, was, now, used):
            mapped += 1
        else:
            fails.append(f"FORK-CI: {path}:{n}: not a runner-label mapping {where}: {now.strip()[:100]!r}")
    return fails, mapped + inserted


def check_fork_ci(repo, c1, head, after_c1):
    """(g): workflow and contributors/ files change only in fork-ci commits, only by the ruled fork-ci edits, judged per
    commit (so an edit undone by a later commit is still seen) and from C1 to the head. Returns (fails, lines since C1)."""
    fails = []
    for sha, _src, item in after_c1:
        for p in name_status(repo, f"{sha}^", sha):
            if not FORK_CI.match(p):
                continue
            if item != "fork-ci":
                fails.append(f"FORK-CI: {sha[:10]} changes {p} outside a [UPG-F68 fork-ci] commit")
            fails.extend(_label_mapping(repo, f"{sha}^", sha, p, f"in {sha[:10]}")[0])
    mapped = 0
    for path in sorted(p for p in name_status(repo, c1, head) if FORK_CI.match(p)):
        f_m, n_m = _label_mapping(repo, c1, head, path, "from C1")
        fails.extend(f_m)
        mapped += n_m
    for path in git(repo, "ls-tree", "-r", "--name-only", head, ".github/workflows/").split():
        for n, ln in enumerate(text_at(repo, head, path).splitlines(), 1):
            if RUNNER_LABEL.search(ln) and not ln.lstrip().startswith("#"):
                fails.append(f"FORK-CI: {path}:{n} still names a 32-core runner label")
    return fails, mapped


def origin_of(sha, src, item, picks):
    if src:
        return "#" + {"b41d14df02": "78", "1fb4d74ccb": "77", "35767fe243": "80", "0615c1e7a0": "82"}.get(src[:10], src[:10])
    if item == "fork-ci":
        return item
    return "new" if item == 1 else (f"item-{item}" if item else "untagged")


def check_inventory(path, observed, core_rows, post_origins, needs_locators, repo, revs):
    """One row per observed path and core scope: status, surface, class and origin equal the checker's; ``post_c1``
    lists the origins of the commits after C1 that touch the path; core rows name a matching authority; conflict
    rows carry three resolving locators (upstream side, fork side, resolved hunk)."""
    fails, seen = [], {}
    with open(path, encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh, delimiter="\t"))
    want_head = ["status", "path", "surface", "class", "origin", "authority", "locators", "post_c1"]
    if not rows or rows[0][:8] != want_head:
        return [f"INVENTORY: header must be {'/'.join(want_head)}"]
    for n, row in enumerate(rows[1:], 2):
        if not row or not row[0] or row[0].startswith("#"):
            continue
        st, p, surf, cls, origin, auth, locs, post = (row + [""] * 8)[:8]
        if p in seen:
            fails.append(f"INVENTORY row {n}: duplicate {p}")
        seen[p] = (st, surf, cls, origin, auth, locs, post)
    want = dict(observed)
    want.update(core_rows)
    for p, w in sorted(want.items()):
        got = seen.pop(p, None)
        if got is None:
            fails.append(f"INVENTORY: missing row for {p}")
            continue
        if got[:4] != tuple(w[:4]):
            fails.append(f"INVENTORY: {p} says {got[:4]}, checker found {tuple(w[:4])}")
        if got[6] != ",".join(post_origins.get(p, [])):
            fails.append(f"INVENTORY: {p} post_c1 says {got[6]!r}, checker found {','.join(post_origins.get(p, []))!r}")
        base = p.split("#")[0]
        names = {a for a in got[4].split(",") if a}
        if w[1] == "fork-ci" and got[4] != fork_ci_authority(p):
            fails.append(f"INVENTORY: fork-ci row {p} names authority {got[4]!r}, want {fork_ci_authority(p)}")
        if w[1] == "core":
            if not names:
                fails.append(f"INVENTORY: core row {p} names no authority")
            elif base in CORE_AUTHORITY and not names <= CORE_AUTHORITY[base]:
                fails.append(f"INVENTORY: {p} names authority {got[4]!r}, allowed {sorted(CORE_AUTHORITY[base])}")
            elif len(w) > 4 and w[4] is not None:
                found = set(w[4].split(","))
                mapped = names if w[2] == "E2-allow" else {"GOV-F25" if a.startswith("GOV-F25") else a for a in names}
                if mapped != found:
                    fails.append(f"INVENTORY: {p} authority {got[4]!r} does not match the replayed parts {sorted(found)}")
        if p in needs_locators:
            toks = got[5].split()
            bad = locators_resolve(repo, got[5], revs) if toks else ["(none)"]
            if bad or len(toks) < 3:
                fails.append(f"INVENTORY: {p} needs 3 resolving locators (upstream, fork side, resolved); bad: {bad}")
    fails.extend(f"INVENTORY: row for a path the checker did not observe: {p}" for p in sorted(seen))
    return fails


def write_inventory(path, observed, core_rows, post_origins):
    """Regenerate the checker-derived columns; the hand-authored authority and locators columns are kept."""
    keep = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8", newline="") as fh:
            for row in list(csv.reader(fh, delimiter="\t"))[1:]:
                if row and row[0] and not row[0].startswith("#"):
                    keep[row[1]] = ((row + [""] * 8)[5], (row + [""] * 8)[6])
    rows = []
    for p, w in sorted({**observed, **core_rows}.items()):
        auth, locs = keep.get(p, ("", ""))
        if not auth and w[1] == "core" and len(w) > 4:
            auth = w[4]
        if w[1] == "fork-ci":
            auth = fork_ci_authority(p)
        rows.append([w[0], p, w[1], w[2], w[3], auth, locs, ",".join(post_origins.get(p, []))])
    with open(path, "w", encoding="utf-8", newline="") as fh:
        wr = csv.writer(fh, delimiter="\t", lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
        wr.writerow(["status", "path", "surface", "class", "origin", "authority", "locators", "post_c1"])
        wr.writerows(rows)


# health: allow CC -- each merge-gate check is a step of one linear verdict over the same candidate history, so the steps share its parsed state
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ap.add_argument("--tag", default="d923a5eb487a17c0a68801bba9ef0f56c81040a8")  # v0.22.2
    ap.add_argument("--h5", default="a2afed6dfa21cb0be1fd60930c917f3f3060d0d3")
    ap.add_argument("--c1", required=True, help="the carry commit, parent = tag")
    ap.add_argument("--head", required=True, help="the candidate head")
    ap.add_argument("--picks", default="b41d14df02,1fb4d74ccb,35767fe243", help="the -x picks, in order")
    ap.add_argument("--inventory", required=True, help="path inventory TSV")
    ap.add_argument("--ledger", default="tests/e2e-scenarios/UPG-F68/replay_ledger.tsv",
                    help="in-surface translation ledger TSV: path, H5 line, tag:/C1: site locator(s), reason")
    ap.add_argument("--repoints", default="tests/e2e-scenarios/UPG-F68/desktop_repoints.tsv",
                    help="old-gated desktop import re-point TSV: path, import line before, import line after, reason")
    ap.add_argument("--verbose", action="store_true", help="print one line per path and per core scope")
    ap.add_argument("--write-inventory", action="store_true",
                    help="rewrite --inventory from this check's observations, keeping each row's authority and locators")
    a = ap.parse_args()
    repo = a.repo
    out = sys.stdout if a.verbose else open("/dev/null", "w", encoding="utf-8")
    revs = {k: git(repo, "rev-parse", f"{v}^{{commit}}").strip() for k, v in
            (("tag", a.tag), ("h5", a.h5), ("c1", a.c1), ("head", a.head), ("nine24", NINE24),
             ("c2_f67", C2_F67), ("wpd1_f67", WP_D1_F67))}
    fails = []
    if git(repo, "rev-parse", f"{revs['c1']}^").strip() != revs["tag"]:
        fails.append("BASE: C1^ is not the tag commit")
    if subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", revs["c1"], revs["head"]], check=False,
                      stdin=subprocess.DEVNULL).returncode:
        fails.append("BASE: C1 is not an ancestor of the head")
    if git(repo, "rev-list", "--merges", f"{revs['tag']}..{revs['head']}").strip():
        fails.append("BASE: merge commits between the tag and the head (expected a linear history)")
    for k in ("nine24", "c2_f67", "wpd1_f67"):
        if subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", revs[k], revs["h5"]], check=False,
                          stdin=subprocess.DEVNULL).returncode:
            fails.append(f"BASE: {k} is not an ancestor of H5")

    led, bad = load_ledger(a.ledger if os.path.exists(a.ledger) else "", repo, revs)
    fails.extend(bad)
    h5_ns = name_status(repo, revs["nine24"], revs["h5"])
    h5_paths = set(h5_ns)
    f_a, observed, flagged = check_h5_paths(repo, revs, h5_ns, led, out)
    fails.extend(f_a)
    extra = sorted(set(name_status(repo, revs["tag"], revs["c1"])) - h5_paths)
    fails.extend(f"EXTRA in C1 (not an H5 delta path): {p}" for p in extra)

    core_paths = sorted(p for p in h5_paths if is_core(p, h5_paths))
    f_a2, allowed = check_a2(repo, revs, out)
    f_c, core_rows = check_core(repo, revs, core_paths, allowed, out)
    fails.extend(f_a2 + f_c + check_a1(repo, revs, out))
    after_c1 = commits_after(repo, revs["c1"], revs["head"])
    item4_landed = blob(repo, revs["head"], ITEM4[0]) != blob(repo, revs["tag"], ITEM4[0])
    for key, (path, cls, auth) in RETAINED.items():
        if path == ITEM4[0] and item4_landed:
            cls = "item-4" if auth == ITEM4[1] else cls
        elif blob(repo, revs["head"], path) != blob(repo, revs["tag"], path):
            fails.append(f"{auth}: {path} differs from the tag at the head ({cls} until item 4)")
        core_rows[key] = ("(retained)", "core", cls, "retained", auth)
    if item4_landed:
        fails.extend(check_item4(repo, revs["tag"], revs["head"], after_c1))

    fails.extend(check_picks(repo, [p.strip() for p in a.picks.split(",") if p.strip()], after_c1))
    post = name_status(repo, revs["c1"], revs["head"])
    e2_allowed = []
    for path in sorted(set(post) | {f for f, *_rest in E2_ALLOW}):
        if surface_of(path, h5_paths) == "core" and not (path == ITEM4[0] and item4_landed):
            f_e, found = check_e2_allow(repo, revs["c1"], revs["head"], path)
            fails.extend(f_e)
            e2_allowed.extend(found)
    rec = {}
    if a.repoints and os.path.exists(os.path.join(repo, a.repoints)):
        with open(os.path.join(repo, a.repoints), encoding="utf-8", newline="") as fh:
            for n, row in enumerate(csv.reader(fh, delimiter="\t"), 1):
                if row and row[0] and not row[0].startswith("#"):
                    p, before, after, reason = (row + [""] * 4)[:4]
                    sides = ((before, import_statement_lines(text_at(repo, revs["c1"], p))),
                             (after, import_statement_lines(text_at(repo, revs["head"], p))))
                    if not reason.strip() or not all(x.strip() in imports for x, imports in sides if x.strip()):
                        fails.append(f"REPOINT row {n}: import lines only (at C1 before, at the head after), with a reason")
                    r = rec.setdefault(p, (set(), set()))
                    r[0].add(before.strip())
                    r[1].add(after.strip())
    for path in sorted(post):
        if surface_of(path, h5_paths) == "old-gated":
            before, after = rec.get(path, (set(), set()))
            plus, minus = multiset_diff(repo, revs["c1"], revs["head"], path)
            stray = [ln for ln in plus if ln not in after] + [ln for ln in minus if ln not in before]
            if stray:
                fails.append(f"OLD-GATED after C1: {path}: {len(stray)} unrecorded line(s), first {stray[0][:100]!r}")
    post_origins = {}
    for sha, src, item in after_c1:
        for p in name_status(repo, f"{sha}^", sha):
            o = origin_of(sha, src, item, a.picks)
            if o not in post_origins.setdefault(p, []):
                post_origins[p].append(o)
            if o == "untagged":
                fails.append(f"COMMIT {sha[:10]}: neither a -x pick nor tagged [UPG-F68 <item>]")
    f_g, fork_ci_lines = check_fork_ci(repo, revs["c1"], revs["head"], after_c1)
    fails.extend(f_g)
    observed_all = dict(observed)
    for p in post:
        if p not in h5_paths:
            st = "A" if blob(repo, revs["c1"], p) is None else "M"
            st = st if blob(repo, revs["head"], p) else "D"
            surf = surface_of(p, h5_paths)
            observed_all[p] = (st, surf, "fork-ci" if surf == "fork-ci" else "post-C1", post_origins.get(p, ["?"])[0])
    auth_of = {(f, r): auth for f, r, _c, auth in E2_ALLOW}
    allow_rows = {}
    for path in sorted(p for p in post if p.endswith(".py") and blob(repo, revs["head"], p)):
        core = is_core(path, h5_paths)
        for n, rules in added_allows(repo, revs["c1"], revs["head"], path):
            if rules == "?":
                fails.append(f"HEALTH-ALLOW: {path}:{n}: not of the form `# health: allow <RULE> -- <why>`")
            if core:
                allow_rows[f"{path}#allow:L{n}"] = ("A", "core", "E2-allow", "item-5", auth_of.get((path, rules), ""))
            else:
                allow_rows[f"{path}#allow:L{n}"] = ("A", surface_of(path, h5_paths), "health-allow", "item-5")
    observed_all.update(allow_rows)
    landed = {src[:10] for _sha, src, _i in after_c1 if src}
    needs = set(C1_CONFLICTS) | {f for src, f in PICK_CONFLICTS if src in landed}
    if a.write_inventory:
        write_inventory(a.inventory, observed_all, core_rows, post_origins)
    inv = check_inventory(a.inventory, observed_all, core_rows, post_origins, needs, repo, revs)
    fails.extend(inv)

    counts = Counter((v[2], v[1]) for v in observed.values())
    print("## H5 delta paths at C1 (class, surface) -> count")
    for k in sorted(counts):
        print(f"{k[0]:<11} {k[1]:<11} {counts[k]}")
    print(f"## core scopes replayed: {len(core_rows) - len(RETAINED)} over {len(core_paths)} files")
    print(f"## commits after C1: {len(after_c1)}; paths touched after C1: {len(post)} "
          f"({sum(1 for p in post if p not in h5_paths)} not in the H5 delta)")
    print(f"## unaccounted H5-added lines: {len(flagged)} path(s)")
    for path, n, first in flagged:
        print(f"  {path}: {n}, first: {first!r}")
    print(f"## 2(e') E2 allows after C1 (core, AST identical): {len(e2_allowed)}")
    for path, n, rule, line in e2_allowed:
        print(f"  {path}:{n} {rule}  {line}")
    fork_allows = sorted(k for k, v in allow_rows.items() if v[2] == "health-allow")
    print(f"## health allows added after C1 in fork code: {len(fork_allows)}")
    for k in fork_allows:
        print(f"  {k}")
    print(f"## (g) fork-ci edits after C1 (runner labels, FORK_CI_TIMEOUTS, FORK_CI_LINES, FORK_CI_INSERTS, "
          f"FORK_CI_CONTRIBUTORS): "
          f"{fork_ci_lines}")
    print(f"## inventory mismatches: {len(inv)}")
    if fails:
        print("\nFAIL\n" + "\n".join(fails))
        return 1
    print("\nOK: C1 carries every H5 path; core = tag + GOV-F25 + WP-D1 per scope (A1/A2 as ruled); picks equal their"
          " sources; no core after C1 beyond item 4 and the 2(e') allows; fork-ci is only the ruled runner, timeout, line, insert and contributor edits; inventory"
          " matches")
    return 0


if __name__ == "__main__":
    sys.exit(main())
