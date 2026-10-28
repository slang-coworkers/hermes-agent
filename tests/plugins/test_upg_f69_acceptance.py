"""UPG-F69 acceptance: the fork replayed onto Hermes v0.22.5 (bf0203a7) from UPG-F68's head.

One test per ``pytest:`` row of ADR ``reports/upg-f69.md`` §Acceptance criteria. The checker CLI and data contract
under test are ADR §D2.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]
SCEN = REPO / "tests" / "e2e-scenarios" / "UPG-F69"
CHECKER = SCEN / "replay_check.py"
V0225 = "bf0203a794a07aac3ff764972ab9087baa4e4f4e"
CHECKOUT_PIN = "actions/checkout@f548e57e544e1ff5a4c46bf1e1b8685f8e4a348a"
RUNNER_LABEL = re.compile(r"blacksmith-\d|-latest-32-(?:arm-)?core")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=upg-f69", "-c", "user.email=upg-f69@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, encoding="utf-8", check=True,
    )


def _run_checker(repo: Path, frm: str, onto: str, head: str, candidate: str, data: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CHECKER), "--repo", str(repo), "--from", frm, "--onto", onto, "--head", head,
         "--candidate", candidate, "--data-dir", str(data)],
        capture_output=True, text=True, encoding="utf-8", timeout=900, check=False,
    )


def _tsv_rows(path: Path) -> list[dict[str, str]]:
    lines = [ln for ln in path.read_text(encoding="utf-8-sig").splitlines() if ln and not ln.startswith("#")]
    header = lines[0].split("\t")
    return [dict(zip(header, ln.split("\t"))) for ln in lines[1:]]


def _yaml_load(path: Path):
    from hermes_yaml import safe_load

    return safe_load(path.read_text(encoding="utf-8-sig"))


def _walk(node, key=None):
    """Yield (key, value) for every scalar value in a loaded YAML document; keys are not values."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, k)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v, key)
    elif isinstance(node, str):
        yield key, node


def test_ac_upg_f69_1():
    """The re-pinned replay checker, run on the candidate with the bump of record (`bump.json`: from `d923a5eb`, onto `bf0203a7`, head = the F68 head it replays), exits 0: every path changed between onto and candidate has exactly one inventory row; every core and fork-ci row has an authority; every core scope and file has an empty residual, a ruled residual or an `absorbed` disposition; every overlap scope carries a matching ruling."""
    bump = json.loads((SCEN / "bump.json").read_text(encoding="utf-8-sig"))
    result = _run_checker(REPO, bump["from"], bump["onto"], bump["head"], "HEAD", SCEN)
    findings = [ln for ln in result.stdout.splitlines() if ln.startswith("FAIL ")]
    assert result.returncode == 0 and not findings, result.stdout[-4000:] + result.stderr[-2000:]
    assert result.stdout.splitlines()[-1].startswith("OK "), result.stdout[-2000:]


# --- AC-UPG-F69-2: a synthetic replay the checker must pass clean and FAIL under each mutation -------------------

FORK_STEP = "      - run: echo fork-ci-step"


def _ci(runner: str, timeout: str, uses: str, test_extra: str = "", lint_extra: str = "") -> str:
    return (
        "name: ci\non: [pull_request]\njobs:\n"
        f"  test:\n    runs-on: {runner}\n    timeout-minutes: {timeout}\n    steps:\n      - uses: {uses}\n{test_extra}"
        f"  lint:\n    runs-on: {runner}\n    steps:\n      - uses: {uses}\n{lint_extra}"
    )


_FROM_FILES = {
    "agent/core_mod.py": "def f():\n    a = 1\n    b = 2\n    return a + b\n\n\ndef g():\n    return 2\n",
    "locales/en.yaml": "greeting: hello\n",
    "plugins/p/__init__.py": "X = 1\n",
    ".github/workflows/ci.yml": _ci("ubuntu-latest", "90", CHECKOUT_PIN),
}
_ONTO_EDITS = {  # upstream: touches f (overlap) and g, moves CI to Blacksmith
    "agent/core_mod.py": "def f():\n    a = 10\n    b = 2\n    return a + b\n\n\ndef g():\n    return 20\n",
    ".github/workflows/ci.yml": _ci("blacksmith-4vcpu-ubuntu-2404", "30",
                                    "useblacksmith/checkout@25227e61ff9dafe400e22fa487b673eac4e4409a"),
}
_HEAD_EDITS = {  # the fork: touches f, adds a locale key, edits its plugin
    "agent/core_mod.py": "def f():\n    a = 1\n    b = 2\n    c = 3\n    return a + b + c\n\n\ndef g():\n    return 2\n",
    "locales/en.yaml": "greeting: hello\nfarewell: bye\n",
    "plugins/p/__init__.py": "X = 2\n",
}
_CANDIDATE = {  # the faithful replay of the fork onto upstream, CI mapped back to a standard runner
    "agent/core_mod.py": "def f():\n    a = 10\n    b = 2\n    c = 3\n    return a + b + c\n\n\ndef g():\n    return 20\n",
    "locales/en.yaml": "greeting: hello\nfarewell: bye\n",
    "plugins/p/__init__.py": "X = 2\n",
    ".github/workflows/ci.yml": _ci("ubuntu-latest", "90", CHECKOUT_PIN, test_extra=FORK_STEP + "\n"),
}


def _write_files(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _scope_sha(source: str, name: str) -> str:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
            return hashlib.sha256(ast.get_source_segment(source, node).encode("utf-8")).hexdigest()
    raise AssertionError(f"scope {name} not found")


def _write_tsv(path: Path, header: str, rows: list[str]) -> None:
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")


@pytest.fixture
def synthetic(tmp_path):
    """A four-revision replay (from / onto / head / candidate-builder) plus a clean data dir, outside the repo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "base")
    _write_files(repo, _FROM_FILES)
    frm = _commit(repo, "from")
    _git(repo, "checkout", "-q", "-b", "upstream")
    _write_files(repo, _ONTO_EDITS)
    onto = _commit(repo, "onto")
    _git(repo, "checkout", "-q", "-b", "fork", frm)
    _write_files(repo, _HEAD_EDITS)
    head = _commit(repo, "head")

    data = tmp_path / "data"
    data.mkdir()
    (data / "bump.json").write_text(json.dumps({"from": frm, "onto": onto, "head": head}), encoding="utf-8")
    _write_tsv(data / "path_inventory.tsv", "status\tpath\tsurface\tclass\torigin\tauthority", [
        "M\tagent/core_mod.py\tcore\treplay\tfork\tGOV-T",
        "M\tlocales/en.yaml\tcore\treplay\tfork\tF62.h-T",
        "M\tplugins/p/__init__.py\tin-surface\tR\tfork\t",
        "M\t.github/workflows/ci.yml\tfork-ci\tfork-ci\tfork\tmsg-596",
    ])
    _write_tsv(data / "core_authority.tsv", "key\tauthority\tdisposition", [
        "agent/core_mod.py\tGOV-T\treplay",
        "locales/en.yaml\tF62.h-T\treplay",
    ])
    _write_tsv(data / "ruled_residuals.tsv", "key\tsha256\tauthority", [])
    _write_tsv(data / "overlap_rulings.tsv", "key\tsha256\truling", [
        f"agent/core_mod.py#f\t{_scope_sha(_CANDIDATE['agent/core_mod.py'], 'f')}\tsynthetic-ruling",
    ])
    _write_tsv(data / "fork_ci_map.tsv", "kind\tmatch\treplacement\tauthority", [
        "label\tblacksmith-4vcpu-ubuntu-2404\tubuntu-latest\tmsg-596",
        f"action\tuseblacksmith/checkout@25227e61ff9dafe400e22fa487b673eac4e4409a\t{CHECKOUT_PIN}\tmsg-596",
    ])
    _write_tsv(data / "fork_ci_sizing.tsv", "workflow\tjob\tkey\tvalue\tauthority", [
        "ci.yml\ttest\ttimeout-minutes\t90\tmsg-602",
    ])
    _write_tsv(data / "fork_ci_lines.tsv", "workflow\tjob\tanchor\tbefore\tafter\tauthority", [
        f"ci.yml\ttest\t- uses: {CHECKOUT_PIN}\t\t{FORK_STEP.strip()}\tmsg-226",
    ])
    _write_tsv(data / "fork_ci_contributors.tsv", "path\tsha256\tauthority", [])

    def build_candidate(mutate=None, base=None) -> str:
        _git(repo, "checkout", "-q", "-B", "candidate", base or onto)
        _write_files(repo, _CANDIDATE)
        if mutate is not None:
            mutate(repo, data)
        return _commit(repo, "candidate")

    return {"repo": repo, "from": frm, "onto": onto, "head": head, "data": data, "build": build_candidate}


def _edit(rel: str, old: str, new: str):
    def mutate(repo: Path, data: Path) -> None:
        path = repo / rel
        text = path.read_text(encoding="utf-8-sig")
        assert old in text
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return mutate


def _add_unknown_core(repo: Path, data: Path) -> None:
    _write_files(repo, {"agent/extra.py": "Y = 1\n"})
    with (data / "path_inventory.tsv").open("a", encoding="utf-8") as fh:
        fh.write("A\tagent/extra.py\tcore\treplay\tfork\tGOV-T\n")


def _drop_overlap_ruling(repo: Path, data: Path) -> None:
    _write_tsv(data / "overlap_rulings.tsv", "key\tsha256\truling", [])


def _delete_data_file(repo: Path, data: Path) -> None:
    (data / "overlap_rulings.tsv").unlink()


def _insert_in_wrong_job(repo: Path, data: Path) -> None:
    _write_files(repo, {".github/workflows/ci.yml": _ci("ubuntu-latest", "90", CHECKOUT_PIN, lint_extra=FORK_STEP + "\n")})


def _drop_fork_change(repo: Path, data: Path) -> None:
    _write_files(repo, {"plugins/p/__init__.py": _FROM_FILES["plugins/p/__init__.py"]})
    rows = (data / "path_inventory.tsv").read_text(encoding="utf-8-sig").splitlines()
    kept = [row for row in rows if "\tplugins/p/__init__.py\t" not in row]
    (data / "path_inventory.tsv").write_text("\n".join(kept) + "\n", encoding="utf-8")


_MUTATIONS = {
    "conflict_marker": (_edit("agent/core_mod.py", "def g():", "<<<<<<< ours\ndef g():"), "CONFLICT_MARKER"),
    "unknown_core_path": (_add_unknown_core, "UNKNOWN_CORE_PATH"),
    "unruled_residual": (_edit("agent/core_mod.py", "return 20", "return 21"), "SCOPE_RESIDUAL"),
    "moved_line_in_overlap": (_edit("agent/core_mod.py", "    a = 10\n    b = 2\n", "    b = 2\n    a = 10\n"),
                              "OVERLAP_BODY_MISMATCH"),
    "unruled_overlap": (_drop_overlap_ruling, "OVERLAP_UNRULED"),
    "blacksmith_label": (_edit(".github/workflows/ci.yml", "runs-on: ubuntu-latest", "runs-on: blacksmith-4vcpu-ubuntu-2404"),
                         "FORK_CI_LABEL"),
    "unruled_action": (_edit(".github/workflows/ci.yml", CHECKOUT_PIN,
                             "useblacksmith/checkout@25227e61ff9dafe400e22fa487b673eac4e4409a"), "FORK_CI_ACTION"),
    "locale_key_changed": (_edit("locales/en.yaml", "greeting: hello", "greeting: hi"), "NONPY_CORE_DELTA"),
    "data_missing": (_delete_data_file, "DATA_MISSING"),
    "fork_delta_missing": (_drop_fork_change, "FORK_DELTA_MISSING"),
    "unruled_ci_edit": (_edit(".github/workflows/ci.yml", CHECKOUT_PIN,
                              "actions/checkout@0123456789abcdef0123456789abcdef01234567"), "FORK_CI_UNRULED"),
    "wrong_job_insert": (_insert_in_wrong_job, "FORK_CI_UNRULED"),
    "stale_bump": (None, "DATA_STALE"),
}


@pytest.mark.parametrize("mutation", [None, *_MUTATIONS])
def test_ac_upg_f69_2(synthetic, mutation):
    """(derived) The checker FAILs, with its named code, on each fail-open it must close. In a synthetic replay, each of these must FAIL: a conflict marker in core; an unknown core path; an un-ruled residual; a fork line moved inside a ruled overlap scope; an un-ruled overlap scope; a Blacksmith runner label in a workflow; an un-ruled `useblacksmith/*` action; a changed carried locale key; a missing data file; a dropped in-surface fork change with its inventory row removed; an un-ruled standard-action pin; a ruled fork-ci insertion moved to another job; ruled data reused for a different `onto` with the same tree. The same synthetic replay without a mutation exits 0."""
    mutate, code = _MUTATIONS[mutation] if mutation else (None, None)
    onto = synthetic["onto"]
    if mutation == "stale_bump":
        repo = synthetic["repo"]
        _git(repo, "checkout", "-q", "-B", "upstream2", onto)
        onto = _commit(repo, "onto, same tree, another commit")
    candidate = synthetic["build"](mutate, base=onto)
    result = _run_checker(synthetic["repo"], synthetic["from"], onto, synthetic["head"], candidate, synthetic["data"])
    findings = [ln for ln in result.stdout.splitlines() if ln.startswith("FAIL ")]
    if mutation is None:
        assert result.returncode == 0 and not findings, result.stdout + result.stderr
    else:
        assert result.returncode == 1, result.stdout + result.stderr
        assert any(ln.split()[1] == code for ln in findings), (code, result.stdout)


def test_ac_upg_f69_3():
    """The candidate is v0.22.5 plus exactly the inventoried fork delta: `onto` in `bump.json` is `bf0203a794a07aac3ff764972ab9087baa4e4f4e`, the merge base of HEAD and onto is onto, and every path that differs between onto and HEAD is an inventory row (all other paths are byte-identical to v0.22.5)."""
    bump = json.loads((SCEN / "bump.json").read_text(encoding="utf-8-sig"))
    assert bump["onto"] == V0225
    assert _git(REPO, "merge-base", "HEAD", V0225).stdout.strip() == V0225
    changed = {p for p in _git(REPO, "diff", "--no-renames", "--name-only", V0225, "HEAD").stdout.splitlines() if p}
    inventoried = {row["path"] for row in _tsv_rows(SCEN / "path_inventory.tsv") if "#" not in row["path"]}
    assert changed, "the candidate carries no fork delta at all"
    assert sorted(changed - inventoried) == []


def _workflow_files() -> list[Path]:
    wf = REPO / ".github" / "workflows"
    return sorted([*wf.glob("*.yml"), *wf.glob("*.yaml"), REPO / ".github" / "actionlint.yaml"])


def test_ac_upg_f69_4():
    """No file under `.github/workflows/` and no entry of `.github/actionlint.yaml` carries a Blacksmith runner label or a GitHub large-runner label (`*-latest-32-core`, `*-latest-32-arm-core`; 8 such non-comment lines in 6 workflows remain at v0.22.5). Every `useblacksmith/*` action left in a workflow has a ruled `KEEP` row. Every sizing row in `fork_ci_sizing.tsv` (job timeout or worker count, each with an authority) holds in the workflow job it names."""
    keep = {row["match"].split("#", 1)[0].strip() for row in _tsv_rows(SCEN / "fork_ci_map.tsv")
            if row["kind"] == "action" and row["replacement"] == "KEEP" and row["authority"]}
    labels, actions = [], []
    for path in _workflow_files():
        for key, value in _walk(_yaml_load(path)):
            rel = path.relative_to(REPO).as_posix()
            if RUNNER_LABEL.search(value):
                labels.append((rel, value))
            if key == "uses" and value.startswith("useblacksmith/") and value.strip() not in keep:
                actions.append((rel, value))
    assert labels == [], labels[:20]
    assert actions == [], actions[:20]

    sizing = _tsv_rows(SCEN / "fork_ci_sizing.tsv")
    assert sizing, "fork_ci_sizing.tsv carries no sizing row"
    for row in sizing:
        assert row["authority"], row
        job = _yaml_load(REPO / ".github" / "workflows" / row["workflow"])["jobs"][row["job"]]
        if row["key"] == "timeout-minutes":
            assert str(job.get("timeout-minutes")) == row["value"], row
        else:
            assert row["key"] == "workers", row
            runs = [str(step.get("run", "")) for step in job.get("steps", [])]
            token = re.compile(rf"(?:-n|--workers|--concurrency)[ =]{re.escape(row['value'])}(?!\d)")
            assert any(token.search(run) for run in runs), row


class _CatalogEntry:
    name = "CommandCode"
    env = ["CMD_API_KEY"]
    api = "https://catalog.example.invalid/v1"
    doc = ""


def test_ac_upg_f69_5(monkeypatch):
    """(derived) The absorbed UA-48 behaviour holds at the candidate. CommandCode resolves its own `COMMANDCODE_API_KEY` and `COMMANDCODE_BASE_URL`, whether or not models.dev lists the provider."""
    from hermes_cli import providers

    monkeypatch.setattr(providers, "_models_dev_info",
                        lambda canonical, allow_network=True: _CatalogEntry() if canonical == "commandcode" else None)
    listed = providers.get_provider("commandcode", allow_network=False)
    assert listed is not None
    assert "COMMANDCODE_API_KEY" in listed.api_key_env_vars and "CMD_API_KEY" in listed.api_key_env_vars
    assert listed.base_url_env_var == "COMMANDCODE_BASE_URL"

    monkeypatch.setattr(providers, "_models_dev_info", lambda canonical, allow_network=True: None)
    unlisted = providers.get_provider("commandcode", allow_network=False)
    assert unlisted is not None
    assert "COMMANDCODE_API_KEY" in unlisted.api_key_env_vars
    assert unlisted.base_url_env_var == "COMMANDCODE_BASE_URL"


# --- AC-UPG-F69-6 (ruling 1a): the persist ack under a started session authority ------------------------------------

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for name in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(name, raising=False)
    kb.init_db()
    return home


_COMPLETE = {"row_ids": [3, 4], "complete": True, "user_row_id": 3, "final_assistant_row_id": 4}
# Every row committed, but a compaction ran during the turn: the receipt no longer vouches for all of it.
_COMPACTED = {"row_ids": [3, 4], "complete": False, "user_row_id": 3, "final_assistant_row_id": 4}
_TRUTHY = {"row_ids": [3, 4], "complete": 1, "user_row_id": 3, "final_assistant_row_id": 4}
_RECEIPTS = {"saved": _COMPLETE, "not_saved": None, "compacted": _COMPACTED, "complete_truthy": _TRUTHY}


@pytest.fixture
def authority_api(kanban_home):
    """An initialized session authority over a fresh state.db and the api_server adapter bound to it."""
    from gateway.config import GatewayConfig, PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    from gateway.session import SessionStore
    from gateway.session_authority import SessionAuthority
    from hermes_state import SessionDB
    from hermes_state_runtime import begin_runtime_epoch
    import uuid

    db = SessionDB(kanban_home / "state.db")
    store = SessionStore(config=GatewayConfig(), sessions_dir=kanban_home / "sessions")
    store._db = db
    runner = SimpleNamespace(_draining=False, session_store=store)
    owner = SessionAuthority(runner, profile_id="default", instance_id="f69-ac6", db=db,
                             epoch=begin_runtime_epoch(db, instance_id="f69-ac6"))
    runner.session_authority = owner
    api = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": uuid.uuid4().hex}))
    api.gateway_runner = runner
    api._session_db = db
    runner._adapter_for_source = lambda source: api
    yield api, owner
    api._response_store.close()
    if getattr(api, "_run_idempotency_store", None) is not None:
        api._run_idempotency_store.close()
    db.close()


def _turns_store(owner, receipt):
    """The authority executes each admitted turn here; the stored result carries `receipt` as v0.22.5's own `persisted_turn`."""
    seen = []

    async def handle(event):
        from gateway.session_results import execution_result
        seen.append(event.text)
        result = {"final_response": "ok", "messages": []}
        if receipt is not None:
            result["persisted_turn"] = dict(receipt)
        execution_result.get()["result"] = result
        return "ok"

    owner.runner._handle_message = handle
    return seen


def _stored_receipts(owner, session_id):
    """Each terminal admission's stored `persisted_turn`, in admission order."""
    from gateway.session_results import admission_result
    from hermes_state_runtime import list_session_admissions

    rows = list_session_admissions(owner.db, session_id=session_id, pending_only=False)
    return [(admission_result(owner.db, row["admission_id"]) or {}).get("result", {}).get("persisted_turn") for row in rows]


@pytest.mark.parametrize("case", ["saved", "not_saved", "compacted", "complete_truthy"])
def test_ac_upg_f69_6(authority_api, case, kanban_home, caplog):
    """(ruling 1a) Through a started session authority, a durable wake is acknowledged only for a fully saved turn, on both paths. A saved turn (`persisted_turn.complete is True`) makes the served-profile `run_internal_session_turn` return `True` and the default-profile HTTP self-post return `X-Hermes-Turn-Persisted: true`. The real durable notifier then delivers the event, and its cursor advances with no alert. A same-key replay joins the settled admission without running the turn again. An unsaved turn (no receipt), a compacted turn (`complete` False, rows present) and a truthy non-bool `complete` are never acknowledged on either path: `False` / `false`, the durable notifier keeps the event (cursor unmoved, sub kept), and after `_DURABLE_ALERT_THRESHOLD` consecutive failures it logs the operator alert. With no session authority, AC-UPG-F67-9's `turn_persisted is True` contract is unchanged."""
    from aiohttp import web

    from gateway.platforms import api_server
    from gateway.platforms import api_server_runs

    api, owner = authority_api
    saved = case == "saved"

    seen = _turns_store(owner, _RECEIPTS[case])
    owner.db.create_session("sess-served", source="api_server")
    served = [asyncio.run(api_server_runs.run_internal_session_turn(
        api, session_id="sess-served", text="wake", profile="reviewer", _api_server=api_server)) for _ in range(2)]
    assert served == [saved, saved]
    assert seen == ["wake", "wake"], "the served wake carries no key: each re-delivery is its own admission"
    assert _stored_receipts(owner, "sess-served") == [_RECEIPTS[case]] * 2

    seen = _turns_store(owner, _RECEIPTS[case])
    owner.db.create_session("sess-default", source="api_server")

    async def self_post():
        from gateway import wake

        acks = []

        @web.middleware
        async def record(request, handler):
            response = await handler(request)
            acks.append((response.status, response.headers.get("X-Hermes-Turn-Persisted")))
            return response

        app = web.Application(middlewares=[record])
        app.router.add_post("/v1/chat/completions", api._handle_chat_completions)
        site_runner = web.AppRunner(app)
        await site_runner.setup()
        site = web.TCPSite(site_runner, "127.0.0.1", 0)
        await site.start()
        api._host, api._port = "127.0.0.1", site._server.sockets[0].getsockname()[1]
        outcomes = []
        try:
            for _attempt in range(2):
                try:
                    await wake._self_post_chat_completion(api, text="wake", session_id="sess-default",
                                                          idempotency_key=f"o/r#f69-6:{case}", require_persist_ack=True)
                    outcomes.append("delivered")
                except RuntimeError as exc:
                    assert "did not confirm persistence" in str(exc), exc
                    outcomes.append("refused")
        finally:
            await site_runner.cleanup()
        return acks, outcomes

    acks, outcomes = asyncio.run(self_post())
    want = "true" if saved else "false"
    assert acks == [(200, want), (200, want)], acks
    assert outcomes == (["delivered"] * 2 if saved else ["refused"] * 2), outcomes
    assert seen == ["wake"], "a same-key replay must join the settled admission, never run the turn again"
    assert _stored_receipts(owner, "sess-default") == [_RECEIPTS[case]]
    for path in ("served", "default"):
        _turns_store(owner, _RECEIPTS[case])
        caplog.clear()
        _assert_durable_notifier(api, owner, path, caplog, kanban_home, saved=saved)


def _assert_durable_notifier(api, owner, path, caplog, home, *, saved):
    """Drive the REAL durable notifier loop with the REAL `deliver_wake` against this authority. A saved turn is
    delivered and its cursor advances with no alert; otherwise every tick fails the persist ack, the event is kept,
    and the threshold-th consecutive failure logs the operator alert."""
    import logging
    from unittest.mock import patch

    from aiohttp import web

    from gateway.config import Platform
    from gateway.kanban_watchers import _DURABLE_ALERT_THRESHOLD
    from gateway.run import GatewayRunner
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_notify as kbn

    chat = f"sess-alert-{path}"
    owner.db.create_session(chat, source="api_server")
    conn = kbc.connect()
    try:
        task_id = kb.create_task(conn, title=f"f69-ac6 {path}", assignee="reviewer")
        kbn.add_notify_sub(conn, task_id=task_id, platform="api_server", chat_id=chat, delivery_mode="wake",
                           retry_policy="durable", notifier_profile="reviewer" if path == "served" else "default")
        kbn.publish_task_notification(conn, task_id, "review submitted", metadata={"idempotency_key": f"o/r#f69-6:{path}"})
        target = kbn.unseen_events_for_sub(conn, task_id=task_id, platform="api_server", chat_id=chat,
                                           thread_id=None, kinds=None)[0]
        before = int(next(s for s in kbn.list_notify_subs(conn, task_id=task_id)
                          if s["chat_id"] == chat).get("last_event_id") or 0)
    finally:
        conn.close()

    runner = object.__new__(GatewayRunner)
    runner._owns_kanban_dispatcher_lock = lambda: True
    runner._running = True
    runner._kanban_sub_fail_counts = {}
    from gateway.config import GatewayConfig

    runner.config = GatewayConfig()
    runner.config.multiplex_profiles = path == "served"
    runner.adapters = {Platform.API_SERVER: api}
    runner._authorization_adapter = lambda platform, owner_profile=None: api
    runner._kanban_notifier_profile = "default"
    runner._profile_adapters = {"reviewer": {Platform.API_SERVER: api}} if path == "served" else {}
    runner._resolve_profile_home_for_source = lambda source: home
    real_sleep, ticks = asyncio.sleep, {"n": 0}

    async def tick(_seconds, *a, **k):
        await real_sleep(0)
        runner._kanban_durable_backoff = {}  # the capped backoff window has elapsed before each tick
        ticks["n"] += 1
        if ticks["n"] > _DURABLE_ALERT_THRESHOLD + 1:
            runner._running = False

    async def main():
        site_runner = None
        if path == "default":
            app = web.Application()
            app.router.add_post("/v1/chat/completions", api._handle_chat_completions)
            site_runner = web.AppRunner(app)
            await site_runner.setup()
            site = web.TCPSite(site_runner, "127.0.0.1", 0)
            await site.start()
            api._host, api._port = "127.0.0.1", site._server.sockets[0].getsockname()[1]
        try:
            with patch("asyncio.sleep", side_effect=tick), \
                    patch("gateway.kanban_watchers_notifier._adapter_for_subscription", lambda *a, **k: api):
                await asyncio.wait_for(runner._kanban_notifier_watcher(interval=1), timeout=120)
        finally:
            if site_runner is not None:
                await site_runner.cleanup()

    with caplog.at_level(logging.WARNING):
        asyncio.run(main())

    alerts = [r for r in caplog.records if r.levelno >= logging.ERROR and "sustained delivery failure" in r.getMessage()]
    conn = kbc.connect()
    try:
        _, pending = kbn.unseen_events_for_sub(conn, task_id=task_id, platform="api_server", chat_id=chat,
                                               thread_id=None, kinds=None)
        subs = kbn.list_notify_subs(conn, task_id=task_id)
    finally:
        conn.close()
    cursors = [int(s.get("last_event_id") or 0) for s in subs]
    if saved:
        assert not alerts, [r.getMessage() for r in alerts]
        assert pending == [] and target > before and cursors == [target], (before, target, cursors, pending)
        return
    assert alerts, f"no operator alert after {_DURABLE_ALERT_THRESHOLD} failed persist acks on the {path} path"
    assert all("did not confirm persistence" in r.getMessage() for r in alerts), [r.getMessage() for r in alerts]
    assert cursors == [before] and [(e.payload or {}).get("idempotency_key") for e in pending] == [f"o/r#f69-6:{path}"]
    assert [s["retry_policy"] for s in subs] == ["durable"]
