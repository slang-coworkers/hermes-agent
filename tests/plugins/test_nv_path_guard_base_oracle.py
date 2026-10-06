"""Guard-agreement oracle for the nv-path-guard-base skill's ``pick_base.match``.

The repository path guard decides ownership with ``git check-ignore --no-index`` in a fresh, empty,
isolated repo (``core.excludesFile=<allowlist>``, ``core.ignoreCase=false``, no inherited ``GIT_*``,
no system or global config). ``pick_base`` reimplements that matching in pure Python, so this test
runs git's recipe and the helper over an adversarial corpus and requires identical owned sets.
"""

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PICK_BASE = (REPO_ROOT / "tests" / "e2e-scenarios" / "FLEET-F62.c" / "spec" / "openshell"
             / "skills" / "nv-path-guard-base" / "scripts" / "pick_base.py")


def _pick_base():
    spec = importlib.util.spec_from_file_location("nv_path_guard_pick_base", PICK_BASE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git_owned(tmp_path, patterns, paths):
    """The guard's recipe, verbatim in effect: an empty isolated repo, the allowlist as the
    excludes file, case-sensitive, NUL-delimited batch check-ignore."""
    root = tmp_path / f"oracle-{len(list(tmp_path.iterdir()))}"
    repo = root / "repo"
    repo.mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, HOME=str(root), XDG_CONFIG_HOME=str(root))
    subprocess.run(["git", "init", "-q", "--template=", str(repo)], cwd=root, env=env, check=True)
    (repo / ".git" / "info").mkdir(parents=True, exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_bytes(b"")
    allowlist = root / "allowlist"
    allowlist.write_bytes(b"".join(p.encode("utf-8") + b"\n" for p in patterns))
    proc = subprocess.run(
        ["git", "-C", str(repo), "-c", "core.ignoreCase=false", "-c", f"core.excludesFile={allowlist}",
         "check-ignore", "-z", "--no-index", "--stdin"],
        input=b"".join(p.encode("utf-8") + b"\0" for p in paths), capture_output=True, env=env, timeout=60)
    assert proc.returncode in (0, 1), proc.stderr
    return {raw.decode("utf-8") for raw in proc.stdout.split(b"\0") if raw}, allowlist


# (allowlist patterns, candidate paths) — negation and both parent-rule forms, ** in every
# position, anchored vs unanchored, directory-only, escaped metacharacters, classes, case.
CORPUS = [
    (["docs/**", "!docs/private/**"], ["docs/a.md", "docs/private/s.md", "docs/private", "x/docs/a.md"]),
    (["d/**", "!d/sub/**"], ["d/a", "d/sub/f", "d/sub/x/y"]),
    (["d/*", "!d/sub/", "d/sub/*", "!d/sub/f"], ["d/a", "d/sub/f", "d/sub/g", "d/sub/x/y"]),
    (["src/*", "!src/overlay/", "src/overlay/*", "!src/overlay/**", "/setup/"],
     ["src/core.ts", "src/overlay/x.ts", "setup/install.sh", "nested/setup/install.sh", "src/a/b.ts"]),
    (["lib/**", "!lib/keep/**"], ["lib/keep/a.c", "lib/x.c", "lib"]),
    (["**/hermes-*.md", "a/**/b", "**/foo", "foo/**", "x**y", "a/**b"],
     ["hermes-x.md", "d/e/hermes-x.md", "a/b", "a/x/y/b", "foo", "q/foo", "foo/z", "xay", "x/a/y", "a/xb", "a/q/xb"]),
    (["*.md", "!README.md", "doc/*.txt", "/top.txt", "build/"],
     ["a.md", "README.md", "x/README.md", "doc/a.txt", "doc/x/a.txt", "top.txt", "x/top.txt", "build/o.o",
      "x/build/o.o", "build"]),
    (["\\!bang", "\\#hash", "sp\\ ", "trail  ", "[abc].c", "[!a].c", "[a-c]x", "[[:digit:]]n", "f?o", "Case/**"],
     ["!bang", "#hash", "sp ", "trail", "trail  ", "a.c", "d.c", "bx", "dx", "1n", "an", "foo", "f/o", "Case/x",
      "case/x"]),
    (["*", "!*/"], ["a", "a/b", "a/b/c"]),
    (["a/", "!a/b"], ["a/b", "a/c", "a/b/c"]),
    (["**/", "!x"], ["x", "y/x", "y/z"]),
    (["*.py[cod]", "[]]x", "[!]]y", "a[^b]c", "*~", "[[:upper:]][[:lower:]]*.rs"],
     ["m.pyc", "m.pyx", "]x", "ay", "]y", "axc", "abc", "f~", "Ab.rs", "ab.rs", "AB.rs"]),
    (["docs", "!docs/keep"], ["docs/keep", "docs/x", "docs"]),
    (["/a/b/", "a/**/c/", "**/z/**"], ["a/b/f", "x/a/b/f", "a/c/f", "a/x/y/c/f", "z/q", "p/z/q", "pz/q"]),
    (["*/x/*.c", "!*/x/keep.c", "**"], ["a/x/m.c", "a/x/keep.c", "a/b/x/m.c", "top.c"]),
]


@pytest.mark.parametrize("patterns,paths", CORPUS, ids=[f"case{i}" for i in range(len(CORPUS))])
def test_pick_base_match_agrees_with_git_check_ignore(tmp_path, patterns, paths):
    pick_base = _pick_base()
    git_owned, allowlist = _git_owned(tmp_path, patterns, paths)
    loaded = pick_base.load_patterns(allowlist)
    assert {p for p in paths if pick_base.match(loaded, p)} == git_owned


def test_pick_base_refuses_what_it_cannot_evaluate(tmp_path):
    """A pattern git can only abort on, or a path git would normalise, is "cannot answer" (exit 2),
    never read as owned or unowned."""
    pick_base = _pick_base()
    guard = tmp_path / ".github" / "nv-path-guard"
    guard.mkdir(parents=True)
    (guard / "nv-main.txt").write_text("src/**\n", encoding="utf-8")
    for bad_path in ("../x", "src/./a", "/abs", "src//a", ":(glob)src"):
        assert pick_base.main(["--repo", str(tmp_path), bad_path]) == 2, bad_path
    (guard / "nv-bad.txt").write_text("[[:nope:]]x\n", encoding="utf-8")
    assert pick_base.main(["--repo", str(tmp_path), "src/a"]) == 2
    (guard / "nv-bad.txt").write_text("# only a comment\n\n", encoding="utf-8")
    assert pick_base.main(["--repo", str(tmp_path), "src/a"]) == 2
    shutil.rmtree(guard)
    assert pick_base.main(["--repo", str(tmp_path), "src/a"]) == 2
