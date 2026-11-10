"""nv-artifact claims a PR/MR onto the card that opened it and never mints a card a dispatcher would run.

A kanban worker's turn runs under a per-turn task id (``run_conversation`` gets none, so
``agent/turn_context.py`` mints a uuid4): the ``post_tool_call`` hook's ``task_id`` never names the card. The
worker's card is the dispatcher-owned ``HERMES_KANBAN_TASK``; a claim from a worker binds there and creates no
card. Outside a worker (a chat, cron or API turn) the claim mints an ownership anchor: parked, never dispatched,
titled with the forge's own sigil (``!`` for a GitLab MR), and not minted at all for a configured chain's branch or
for an MR the ledger already owns. The forge CLI comes from the ``forge_tools`` setting and the chain's work branch
from ``chain_branch_re`` (a complete regex, applied with fullmatch); both are empty by default. FAKE data only.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

import hermes_yaml as yaml
from hermes_cli.plugins import PluginManager
from tests.plugins import test_nv_artifact_acceptance as acceptance
from tests.plugins.test_nv_artifact_acceptance import OWNER_PROFILE, _bare_card, _card_session_id, _ownership

artifact = acceptance.artifact

GL = "https://gitlab.example.test"
PROJECT = "group/project"
FORGE = "example-forge"
CHAIN_SETTINGS = {
    "forge_tools": [FORGE],
    "chain_projects": ["project"],
    "chain_branch_re": r"chain-bot/chain-(?:project)-(?:issue-[1-9][0-9]{0,8}|slack-[0-9]{10}\.[0-9]{6})"
                       r"(?:-r[1-9][0-9]?)?",
}
CHAIN = "chain-bot/chain-project-issue-422"
CARD_KEY = "chain-project-issue-422:builder:r1"


@pytest.fixture
def configured(artifact):
    """``artifact`` reloaded with the forge and chain settings a downstream fleet renders."""
    cfg_path = artifact.home / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    cfg["plugins"]["entries"][acceptance.PLUGIN_KEY]["settings"].update(CHAIN_SETTINGS)
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[acceptance.PLUGIN_KEY]
    assert loaded.enabled is True and loaded.error is None and loaded.module is not None
    return SimpleNamespace(manager=manager, loaded=loaded, root=artifact.root, home=artifact.home)


def _tasks() -> list[dict]:
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect() as conn:
        conn.row_factory = None
        cols = ("id", "title", "status", "assignee", "created_by", "body", "idempotency_key")
        return [dict(zip(cols, row)) for row in conn.execute(f"SELECT {', '.join(cols)} FROM tasks ORDER BY rowid")]


def _mr_json(iid: int, branch: str = "", project: str = PROJECT) -> str:
    out = {"iid": iid, "state": "opened", "web_url": f"{GL}/{project}/-/merge_requests/{iid}"}
    if branch:
        out["source_branch"] = branch
    return json.dumps(out, indent=2, sort_keys=True)


def _forge_create(branch: str) -> str:
    return f"{FORGE} --project {PROJECT} mr create --source-branch {branch} --title 'Fix' --draft"


def _claim(artifact, command, result, *, task_id, session_id, tool="terminal"):
    args = {"command": command} if tool == "terminal" else {"code": command}
    artifact.manager.invoke_hook("post_tool_call", tool_name=tool, args=args, result=result,
                                 status="ok", task_id=task_id, session_id=session_id)


def _worker(monkeypatch, card: str) -> str:
    """Enter a dispatcher-owned worker's context for ``card``; returns the turn's own (non-card) task id."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", card)
    return str(uuid.uuid4())


@pytest.mark.parametrize("command,result,repo,pr", [
    (_forge_create(CHAIN), _mr_json(333, CHAIN), PROJECT, 333),
    ("gh pr create --title x --body y", "Creating pull request\nhttps://github.com/o/r/pull/7\n", "o/r", 7),
], ids=["gitlab", "github"])
def test_a_workers_create_claims_its_own_card_and_mints_none(configured, monkeypatch, command, result, repo, pr):
    card = _bare_card(OWNER_PROFILE, CARD_KEY)
    before = _tasks()
    _claim(configured, command, result, task_id=_worker(monkeypatch, card), session_id="S-worker")

    assert _ownership(configured.root, repo=repo, pr=pr) == [(repo, pr, card, OWNER_PROFILE)]
    assert _tasks() == before
    assert _card_session_id(card) == "S-worker"


def test_a_worker_whose_card_the_board_cannot_resolve_claims_and_mints_nothing(configured, monkeypatch):
    _claim(configured, _forge_create("chain-bot/repro-x-20261104"), _mr_json(5, "chain-bot/repro-x-20261104"),
           task_id=_worker(monkeypatch, "t_00000000"), session_id="S-worker")
    assert _ownership(configured.root) == []
    assert _tasks() == []


@pytest.mark.parametrize("command,result,repo,pr,title", [
    (_forge_create("chain-bot/repro-x-20261104"), _mr_json(7, "chain-bot/repro-x-20261104"),
     PROJECT, 7, f"{PROJECT}!7"),
    ("gh pr create --title x", "https://github.com/o/r/pull/12\n", "o/r", 12, "o/r#12"),
], ids=["gitlab", "github"])
def test_an_anchor_outside_a_worker_is_parked_and_carries_the_forge_sigil(configured, command, result, repo, pr,
                                                                          title):
    _claim(configured, command, result, task_id="", session_id="S-chat")

    (anchor,) = _tasks()
    assert anchor["title"] == title
    assert anchor["status"] == "blocked"  # parked: the dispatcher runs ready cards only
    assert anchor["assignee"] == OWNER_PROFILE and anchor["created_by"] == "nv-artifact"
    assert title in anchor["body"] and "not a work item" in anchor["body"].lower()
    assert _ownership(configured.root, repo=repo, pr=pr) == [(repo, pr, anchor["id"], OWNER_PROFILE)]
    assert _card_session_id(anchor["id"]) == "S-chat"

    # The dispatcher's promotion pass leaves it parked (its block is sticky), so it never reaches 'ready'.
    import hermes_cli.kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect() as conn:
        kb.recompute_ready(conn)
    assert [t["status"] for t in _tasks()] == ["blocked"]


@pytest.mark.parametrize("branch", [CHAIN, f"{CHAIN}-r2", "chain-bot/chain-project-slack-1793195911.774739"])
@pytest.mark.parametrize("branch_in", ["result", "command"])
def test_a_chain_branch_outside_a_worker_mints_and_claims_nothing(configured, branch, branch_in):
    command = _forge_create(branch) if branch_in == "command" else f"{FORGE} mr create --title t"
    result = _mr_json(333, branch if branch_in == "result" else "")
    _claim(configured, command, result, task_id="", session_id="S-chat")
    assert _tasks() == []
    assert _ownership(configured.root) == []


@pytest.mark.parametrize("branch", [f"{CHAIN}-x", f"{CHAIN}-r0", f"x{CHAIN}", "chain-bot/chain-other-issue-422",
                                    "chain-bot/chain-project-issue-0422"])
def test_a_near_miss_of_the_chain_branch_outside_a_worker_mints_an_anchor(configured, branch):
    _claim(configured, _forge_create(branch), _mr_json(333, branch), task_id="", session_id="S-chat")
    (anchor,) = _tasks()
    assert anchor["title"] == f"{PROJECT}!333" and anchor["status"] == "blocked"


def test_without_chain_branch_re_a_chain_branch_outside_a_worker_mints_an_anchor(artifact):
    _claim(artifact, f"glab mr create -s {CHAIN} --title t", _mr_json(333, CHAIN), task_id="", session_id="S-chat")
    (anchor,) = _tasks()
    assert anchor["title"] == f"{PROJECT}!333" and anchor["status"] == "blocked"


def test_an_mr_the_ledger_already_owns_mints_nothing_outside_a_worker(configured, monkeypatch):
    card = _bare_card(OWNER_PROFILE, "chain-project-issue-9:builder:r1")
    _claim(configured, _forge_create("fix/9"), _mr_json(9, "fix/9"),
           task_id=_worker(monkeypatch, card), session_id="S-worker")
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    before = _tasks()

    _claim(configured, _forge_create("fix/9"), _mr_json(9, "fix/9"), task_id="", session_id="S-chat")
    assert _tasks() == before
    assert _ownership(configured.root, repo=PROJECT, pr=9) == [(PROJECT, 9, card, OWNER_PROFILE)]
    assert _card_session_id(card) == "S-worker"


def test_a_cron_job_inside_a_workers_environment_claims_and_mints_nothing(configured, monkeypatch):
    """A job that inherits the worker's HERMES_KANBAN_TASK does not own that card (owned_kanban_task() is "")."""
    from agent.delegation_context import non_dispatcher_owned_context

    card = _bare_card(OWNER_PROFILE, CARD_KEY)
    before = _tasks()
    monkeypatch.setenv("HERMES_KANBAN_TASK", card)
    with non_dispatcher_owned_context():
        _claim(configured, _forge_create("fix/5"), _mr_json(5, "fix/5"), task_id=str(uuid.uuid4()),
               session_id="S-cron")
    assert _tasks() == before
    assert _ownership(configured.root) == []


def test_a_delegated_child_of_a_worker_claims_and_mints_nothing(configured, monkeypatch):
    from agent.delegation_context import delegated_child_context

    card = _bare_card(OWNER_PROFILE, CARD_KEY)
    before = _tasks()
    monkeypatch.setenv("HERMES_KANBAN_TASK", card)
    with delegated_child_context("S-child"):
        _claim(configured, _forge_create("fix/6"), _mr_json(6, "fix/6"), task_id=str(uuid.uuid4()),
               session_id="S-child")
    assert _tasks() == before
    assert _ownership(configured.root) == []


URL_ONLY = f"{GL}/{PROJECT}/-/merge_requests/333\n"


@pytest.mark.parametrize("tool,invocation", [
    ("terminal", f'glab mr create --title "Fix parser; preserve behaviour" -s {CHAIN}'),
    ("terminal", f"cd /w && {FORGE} --project {PROJECT} mr create --source-branch={CHAIN} --title t"),
    ("execute_code", f'import subprocess\nsubprocess.run([\n    "glab", "mr", "create",\n'
                     f'    "--source-branch", "{CHAIN}",\n])\n'),
    ("execute_code", f"import subprocess\nsubprocess.run('{FORGE} mr create --source-branch {CHAIN} "
                     "--title \"a; b\"', shell=True)\n"),
], ids=["quoted-semicolon", "flag-equals", "multiline-argv", "shell-string"])
def test_the_chain_branch_is_read_from_the_create_commands_own_tokens(configured, tool, invocation):
    _claim(configured, invocation, URL_ONLY, task_id="", session_id="S-chat", tool=tool)
    assert _tasks() == []
    assert _ownership(configured.root) == []


def test_flag_text_inside_a_title_is_not_the_source_branch(configured):
    command = f'{FORGE} mr create --title "see --source-branch {CHAIN}" --source-branch fix/7'
    _claim(configured, command, _mr_json(7), task_id="", session_id="S-chat")
    (anchor,) = _tasks()
    assert anchor["title"] == f"{PROJECT}!7"


def test_only_the_created_mrs_own_json_names_its_branch(configured):
    listing = json.dumps([{"iid": 9, "source_branch": CHAIN, "web_url": f"{GL}/{PROJECT}/-/merge_requests/9"}])
    result = listing + "\n" + _mr_json(10, "fix/10")
    _claim(configured, f"{FORGE} mr list --state opened; {FORGE} mr create --source-branch fix/10 --title t",
           result, task_id="", session_id="S-chat")
    (anchor,) = _tasks()
    assert anchor["title"] == f"{PROJECT}!10"


@pytest.mark.parametrize("title", ["';'", "'|'", "'&'", "'a\nb'", '"x && y"'])
def test_quoted_punctuation_in_a_title_is_an_argument_not_a_boundary(configured, title):
    _claim(configured, f"glab mr create --title {title} --source-branch {CHAIN}", URL_ONLY,
           task_id="", session_id="S-chat")
    assert _tasks() == []
    assert _ownership(configured.root) == []


@pytest.mark.parametrize("command,result,title", [
    (f"gh pr create --title '--head={CHAIN}' --head fix/7", "https://github.com/o/r/pull/7\n", "o/r#7"),
    (f'{FORGE} mr create --title "--source-branch={CHAIN}" --source-branch fix/7', _mr_json(7), f"{PROJECT}!7"),
], ids=["gh", "forge-cli"])
def test_an_option_value_that_looks_like_a_branch_flag_is_not_one(configured, command, result, title):
    _claim(configured, command, result, task_id="", session_id="S-chat")
    (anchor,) = _tasks()
    assert anchor["title"] == title


def test_argv_elements_are_data_even_when_they_carry_shell_quotes(configured):
    code = (f'import subprocess\nsubprocess.run([\n    "glab", "mr", "create",\n    "--source-branch", "{CHAIN}",\n'
            '    "--title", "Fix user\'s parser",\n])\n')
    _claim(configured, code, URL_ONLY, task_id="", session_id="S-chat", tool="execute_code")
    assert _tasks() == []
    assert _ownership(configured.root) == []


DEEP = '{"a": ' * 20000 + "1" + "}" * 20000  # past the JSON decoder's recursion limit


def test_deeply_nested_json_in_the_output_never_drops_a_workers_claim(configured, monkeypatch):
    card = _bare_card(OWNER_PROFILE, CARD_KEY)
    _claim(configured, _forge_create(CHAIN), DEEP + "\n" + _mr_json(333, CHAIN),
           task_id=_worker(monkeypatch, card), session_id="S-worker")
    assert _ownership(configured.root, repo=PROJECT, pr=333) == [(PROJECT, 333, card, OWNER_PROFILE)]


def test_deeply_nested_json_before_the_created_mr_still_yields_its_branch(configured):
    _claim(configured, f"{FORGE} mr create --title t", DEEP + "\n" + _mr_json(333, CHAIN),
           task_id="", session_id="S-chat")
    assert _tasks() == []
    assert _ownership(configured.root) == []


@pytest.mark.parametrize("command", [
    f'glab mr create --title="Fix; parser" --source-branch {CHAIN}',
    f"glab mr create --title t '--source-branch={CHAIN}'",
    f"{FORGE} --project {PROJECT} mr create \\\n  --source-branch {CHAIN} \\\n  --title t",
], ids=["quote-inside-word", "quoted-flag", "line-continuation"])
def test_shell_words_are_cooked_as_the_shell_would(configured, command):
    _claim(configured, command, URL_ONLY, task_id="", session_id="S-chat")
    assert _tasks() == []
    assert _ownership(configured.root) == []


def test_an_argv_value_is_used_exactly_never_unquoted(configured):
    code = (f'import subprocess\nsubprocess.run(["glab", "mr", "create", "--source-branch", "\'{CHAIN}\'", '
            '"--title", "t"])\n')
    _claim(configured, code, URL_ONLY, task_id="", session_id="S-chat", tool="execute_code")
    (anchor,) = _tasks()
    assert anchor["title"] == f"{PROJECT}!333"


def test_an_over_long_number_in_an_earlier_url_never_drops_a_workers_claim(configured, monkeypatch):
    card = _bare_card(OWNER_PROFILE, CARD_KEY)
    noise = f"{GL}/{PROJECT}/-/merge_requests/{'9' * 5000}\n"
    _claim(configured, _forge_create(CHAIN), noise + URL_ONLY, task_id=_worker(monkeypatch, card), session_id="S-w")
    assert _ownership(configured.root, repo=PROJECT, pr=333) == [(PROJECT, 333, card, OWNER_PROFILE)]


def test_without_forge_tools_only_glab_claims_an_mr(artifact):
    _claim(artifact, _forge_create("fix/3"), _mr_json(3, "fix/3"), task_id="", session_id="S-chat")
    assert _tasks() == [] and _ownership(artifact.root) == []
    _claim(artifact, "glab mr create --source-branch fix/4 --title t", _mr_json(4, "fix/4"),
           task_id="", session_id="S-chat")
    (anchor,) = _tasks()
    assert anchor["title"] == f"{PROJECT}!4"


def test_without_a_chain_setting_a_chain_shaped_branch_still_gets_its_anchor(artifact):
    _claim(artifact, f"glab mr create --source-branch {CHAIN} --title t", URL_ONLY, task_id="", session_id="S-chat")
    (anchor,) = _tasks()
    assert anchor["title"] == f"{PROJECT}!333" and anchor["status"] == "blocked"


@pytest.mark.parametrize("forge_tools", [[FORGE, 7], FORGE])
def test_forge_tools_that_is_not_a_list_of_strings_falls_back_to_glab_only(artifact, forge_tools):
    cfg_path = artifact.home / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    cfg["plugins"]["entries"][acceptance.PLUGIN_KEY]["settings"]["forge_tools"] = forge_tools
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[acceptance.PLUGIN_KEY]
    assert loaded.enabled is True and loaded.error is None
    reloaded = SimpleNamespace(manager=manager, loaded=loaded, root=artifact.root, home=artifact.home)
    _claim(reloaded, _forge_create("fix/5"), _mr_json(5, "fix/5"), task_id="", session_id="S-chat")
    assert _tasks() == [] and _ownership(artifact.root) == []
