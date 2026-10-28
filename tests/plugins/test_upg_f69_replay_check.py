"""Edge cases of the UPG-F69 replay checker beyond AC-UPG-F69-2's mutation set."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CHECKER = REPO / "tests" / "e2e-scenarios" / "UPG-F69" / "replay_check.py"


def _checker_module():
    spec = importlib.util.spec_from_file_location("upg_f69_replay_check", CHECKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
         *args], capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout.strip()


def _commit(repo: Path, files: dict[str, str], message: str, parent: str | None = None) -> str:
    if parent:
        _git(repo, "checkout", "-q", "--detach", parent)
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _run(repo: Path, frm: str, onto: str, head: str, candidate: str, data: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CHECKER), "--repo", str(repo), "--from", frm, "--onto", onto, "--head", head,
         "--candidate", candidate, "--data-dir", str(data)],
        capture_output=True, text=True, encoding="utf-8", timeout=300, check=False,
    )


def _replay(tmp_path: Path, onto_f: str) -> tuple[Path, list[str]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    a = _commit(repo, {"agent/m.py": "def f():\n    return 1\n"}, "from")
    b = _commit(repo, {"agent/m.py": onto_f}, "onto", parent=a)
    h = _commit(repo, {"agent/m.py": "def f():\n    return 2\n"}, "head", parent=a)
    c = _commit(repo, {"agent/m.py": onto_f.replace("return 1", "return 2")}, "candidate", parent=b)
    return repo, [a, b, h, c]


def test_a_reindent_upstream_makes_an_overlap_scope(tmp_path):
    """A scope upstream only re-indented and the fork edited is an overlap scope that needs a ruling."""
    repo, revs = _replay(tmp_path, "def f():\n        return 1\n")
    result = _run(repo, *revs, tmp_path / "no-data")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "FAIL OVERLAP_UNRULED agent/m.py#f:" in result.stdout, result.stdout


def test_a_blank_line_upstream_makes_an_overlap_scope(tmp_path):
    """A blank line upstream added inside a scope the fork edited is a text change of that scope."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    a = _commit(repo, {"agent/m.py": "def f():\n    x = 1\n    return 1\n"}, "from")
    b = _commit(repo, {"agent/m.py": "def f():\n    x = 1\n\n    return 1\n"}, "onto", parent=a)
    h = _commit(repo, {"agent/m.py": "def f():\n    x = 1\n    return 2\n"}, "head", parent=a)
    c = _commit(repo, {"agent/m.py": "def f():\n    x = 1\n\n    return 2\n"}, "candidate", parent=b)
    result = _run(repo, a, b, h, c, tmp_path / "no-data")
    assert "FAIL OVERLAP_UNRULED agent/m.py#f:" in result.stdout, result.stdout


def test_a_scope_upstream_left_alone_is_no_overlap(tmp_path):
    """Only the fork changed the scope, so no overlap ruling is asked for."""
    repo, revs = _replay(tmp_path, "def f():\n    return 1\n\n\ndef g():\n    return 3\n")
    result = _run(repo, *revs, tmp_path / "no-data")
    assert "OVERLAP_UNRULED" not in result.stdout, result.stdout


def test_generic_mode_lists_what_needs_a_ruling_and_never_attests(tmp_path):
    """With no data for the bump the run exits 1, names each overlap scope, and prints no OK line."""
    repo, revs = _replay(tmp_path, "def f():\n    x = 0\n    return 1\n")
    result = _run(repo, *revs, tmp_path / "no-data")
    lines = result.stdout.splitlines()
    assert result.returncode == 1
    assert "FAIL DATA_MISSING bump.json: absent" in lines
    assert any(ln.startswith("FAIL OVERLAP_UNRULED agent/m.py#f:") for ln in lines)
    assert not any(ln.startswith("OK ") for ln in lines)


def test_an_unresolvable_revision_exits_2(tmp_path):
    """A revision git cannot resolve is a usage error, not a finding."""
    repo, revs = _replay(tmp_path, "def f():\n    return 1\n")
    result = _run(repo, revs[0], "no-such-revision", revs[2], revs[3], tmp_path / "no-data")
    assert result.returncode == 2
    assert "FAIL" not in result.stdout


def test_a_workers_row_is_not_met_by_a_comment():
    """A worker count only counts in a step line of the named job, never in a YAML comment."""
    rc = _checker_module()
    onto = ["jobs:", "  check:", "    # --concurrency 16", "    steps:", "      - run: echo hello"]
    candidate = ["jobs:", "  check:", "    # --concurrency 2", "    steps:", "      - run: echo hello"]
    rep = rc.Report()
    row = {"workflow": "ci.yml", "job": "check", "key": "workers", "value": "2", "authority": "t", "_line": "s:1"}
    rc.expected_fork_ci(onto, candidate, rc.ForkCiMap([]), [row], [], rep)
    assert any(f.startswith("FAIL DATA_STALE s:1:") for f in rep.findings), rep.findings


def test_a_workers_row_explains_its_step_edit():
    """The worker token added to the job's step line is explained by its sizing row."""
    rc = _checker_module()
    onto = ["jobs:", "  check:", "    steps:", "      - run: node checks.mjs"]
    candidate = ["jobs:", "  check:", "    steps:", "      - run: node checks.mjs --concurrency 2"]
    rep = rc.Report()
    row = {"workflow": "ci.yml", "job": "check", "key": "workers", "value": "2", "authority": "t", "_line": "s:1"}
    assert rc.expected_fork_ci(onto, candidate, rc.ForkCiMap([]), [row], [], rep) == candidate
    assert rep.findings == []


CHECKOUT_OLD = "useblacksmith/checkout@25227e61ff9dafe400e22fa487b673eac4e4409a"
CHECKOUT_PIN = "actions/checkout@f548e57e544e1ff5a4c46bf1e1b8685f8e4a348a"
WORKFLOW = "name: ci\non: [pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"


def _fork_ci_replay(tmp_path: Path, onto_uses: list[str], candidate_uses: list[str], map_rows: list[str]):
    """A one-workflow repo where from = head = onto, plus a data dir holding exactly the given map rows."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    wf = ".github/workflows/ci.yml"
    b = _commit(repo, {wf: WORKFLOW + "".join(f"      - uses: {u}\n" for u in onto_uses)}, "onto")
    c = _commit(repo, {wf: WORKFLOW + "".join(f"      - uses: {u}\n" for u in candidate_uses)}, "candidate")
    data = tmp_path / "data"
    data.mkdir()
    (data / "bump.json").write_text(f'{{"from": "{b}", "onto": "{b}", "head": "{b}"}}', encoding="utf-8")
    rc = _checker_module()
    rows = {"fork_ci_map.tsv": map_rows}
    if onto_uses != candidate_uses:
        rows["path_inventory.tsv"] = [f"M\t{wf}\tfork-ci\tfork-ci\tt\tt"]
    for name, header in rc.TABLES.items():
        (data / name).write_text("\n".join(["\t".join(header), *rows.get(name, [])]) + "\n", encoding="utf-8")
    return _run(repo, b, b, b, c, data)


def test_an_action_row_without_a_ref_is_malformed(tmp_path):
    """An action map row must name the whole owner/name@<ref> it rules; ``owner/name`` alone is malformed data."""
    result = _fork_ci_replay(tmp_path, [CHECKOUT_OLD], [CHECKOUT_PIN],
                             [f"action\tuseblacksmith/checkout\t{CHECKOUT_PIN}\tt"])
    assert result.returncode == 1, result.stdout + result.stderr
    assert any(ln.startswith("FAIL DATA_MISSING fork_ci_map.tsv:2: malformed: an action match is the whole")
               for ln in result.stdout.splitlines()), result.stdout


def test_a_keep_row_authorizes_only_its_own_ref(tmp_path):
    """A KEEP row keeps exactly its owner/name@<ref>; the same action at another ref is still an unruled action."""
    other = "useblacksmith/checkout@" + "0" * 40
    result = _fork_ci_replay(tmp_path, [CHECKOUT_OLD, other], [CHECKOUT_OLD, other],
                             [f"action\t{CHECKOUT_OLD}  # v1\tKEEP\thosted-runner proof"])
    actions = [ln for ln in result.stdout.splitlines() if ln.startswith("FAIL FORK_CI_ACTION ")]
    assert result.returncode == 1, result.stdout + result.stderr
    assert actions == [f"FAIL FORK_CI_ACTION .github/workflows/ci.yml: jobs.test.steps[1].uses = {other} has no KEEP row"]
