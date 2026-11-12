"""nv-fleet-gates' generic helper modules with their fork defaults: no forge CLI, no review bot, no push critique."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "nv-fleet-gates"
PACKAGE = "nv_fleet_gates_generic_modules_test"


def _load(name: str):
    if PACKAGE not in sys.modules:
        spec = importlib.util.spec_from_file_location(PACKAGE, PLUGIN / "__init__.py",
                                                      submodule_search_locations=[str(PLUGIN)])
        package = importlib.util.module_from_spec(spec)
        package.__path__ = [str(PLUGIN)]
        sys.modules[PACKAGE] = package
    full = f"{PACKAGE}.{name}"
    spec = importlib.util.spec_from_file_location(full, PLUGIN / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def commands():
    return _load("commands")


@pytest.fixture
def origin():
    return _load("conversation_origin")


def test_no_forge_cli_is_known_by_default(commands):
    assert commands.forge_args(["example-forge", "mr", "create"]) is None
    assert commands.forge_args(["python3", "-m", "example_forge", "mr", "create"]) is None


def test_a_configured_forge_cli_is_read_in_every_spelling(commands):
    commands.configure(["example-forge"])
    assert commands.forge_args(["example-forge", "mr", "create"]) == ["mr", "create"]
    assert commands.forge_args(["/usr/local/bin/example-forge", "issue", "get"]) == ["issue", "get"]
    assert commands.forge_args(["python3", "-m", "example_forge", "mr", "get"]) == ["mr", "get"]
    assert commands.forge_args(["python3", "-m", "example_forge.__main__", "mr", "get"]) == ["mr", "get"]
    assert commands.forge_args(["python3", "/opt/x/example_forge/__main__.py", "mr"]) == ["mr"]
    assert commands.forge_args(["glab", "mr", "create"]) is None
    commands.configure([])
    assert commands.forge_args(["example-forge", "mr", "create"]) is None


def test_a_configured_forge_cli_segment_is_a_forge_call(commands):
    text = "example-forge --project group/project mr get 7"
    (seg,) = commands.shell_segments(text)
    assert commands.exemption(seg) != "forge"
    commands.configure(["example-forge"])
    assert commands.exemption(seg) == "forge"
    (other,) = commands.shell_segments("example-forgery mr get 7")
    assert commands.exemption(other) != "forge"


def test_no_delivery_asks_for_a_review_without_a_review_bot(origin):
    payload = {"gitlab": {"reviewers_added": ["reviewer-bot"]}, "comment": {"body": "@reviewer-bot please review"}}
    assert origin.review_asked(payload) is False
    origin.configure("reviewer-bot")
    assert origin.review_asked(payload) is True
    assert origin.review_asked({"comment": {"body": "@reviewer-bot thanks"}}) is False
    assert origin.review_asked({"comment": {"body": "@reviewer-botty please review"}}) is False
    origin.configure("")
    assert origin.review_asked(payload) is False


class _Ctx:
    def __init__(self, plugin_id: str):
        self.plugin_id = plugin_id

    def on_unload(self, callback):
        return callback


def _probe_parser(iid_option: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="example-forge")
    parser.add_argument("--project")
    nouns = parser.add_subparsers(dest="noun")
    verbs = nouns.add_parser("issue").add_subparsers(dest="verb")
    comment = verbs.add_parser("comment")
    comment.add_argument(iid_option, dest="iid", type=int)
    comment.add_argument("--body-file")
    verbs.add_parser("get").add_argument("iid", type=int)
    return parser


@pytest.fixture
def outbound(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    module = _load("outbound")
    yield module
    module.commands.configure([])
    module.configure([])


@pytest.fixture
def cron():
    return _load("cron_delivery")


def _push(outbound, remote: str):
    (write,) = outbound.classify("terminal", {"command": f"git push {remote} HEAD"})
    return write


def test_a_push_needs_no_critique_without_critique_push_remotes(outbound):
    assert outbound.critique_egress(_push(outbound, "origin")) is False
    assert outbound.critique_egress(_push(outbound, "https://forge.example/group/project.git")) is False


def test_critique_push_remotes_hold_a_matching_or_unreadable_remote(outbound):
    outbound.configure([r"^/group/"])
    assert outbound.critique_egress(_push(outbound, "https://forge.example/group/project.git")) is True
    assert outbound.critique_egress(_push(outbound, "git@forge.example:group/project.git")) is True
    assert outbound.critique_egress(_push(outbound, "origin")) is True
    assert outbound.critique_egress(_push(outbound, "https://forge.example/other/project.git")) is False


def test_a_critique_push_remote_that_does_not_compile_holds_every_push(outbound):
    outbound.configure(["("])
    assert outbound.critique_egress(_push(outbound, "https://forge.example/other/project.git")) is True


def test_a_forge_cli_call_is_a_forge_write_only_once_configured(outbound):
    call = {"command": "example-forge issue comment --iid 7 --body-file note.md"}
    assert outbound.classify("terminal", call) == []
    outbound.commands.configure(["example-forge"])
    (write,) = outbound.classify("terminal", call)
    assert (write.kind, write.noun, write.verb) == ("gitlab", "issue", "comment")
    assert outbound.classify("terminal", {"command": "example-forge issue get 7"}) == []
    assert outbound.critique_egress(write) is True


def test_without_a_parser_a_forge_write_has_no_target(outbound):
    outbound.commands.configure(["example-forge"])
    write = outbound.forge_op(["--project", "group/project", "issue", "comment", "--iid", "7"])
    assert (write.noun, write.verb, write.project, write.iid) == ("issue", "comment", "", None)


def test_the_registered_forge_parser_binds_the_target_and_is_asked_on_every_call(outbound, tmp_path):
    home = str((tmp_path / "home").resolve())
    hooks = outbound.product_hooks
    argv = ["--project", "group/project", "issue", "comment", "--iid", "7"]
    hooks.register_product_hook(_Ctx("example-forge"), "forge_cli_parser", lambda **_: _probe_parser("--iid"))
    write = outbound.forge_op(argv, home=home)
    assert (write.project, write.iid) == ("group/project", 7)
    hooks.register_product_hook(_Ctx("example-forge"), "forge_cli_parser", lambda **_: _probe_parser("--number"))
    write = outbound.forge_op(argv, home=home)
    assert (write.project, write.iid) == ("group/project", None)  # the replacement binds no --iid
    assert outbound.forge_op(["issue", "get", "7"], home=home) is None


def test_the_imported_forge_client_and_unresolved_command_words_are_opaque_writes(outbound):
    outbound.commands.configure(["example-forge"])
    code = {"code": "import example_forge\nexample_forge.Client().comment(7)"}
    assert outbound.classify("execute_code", code) == [outbound.Write("gitlab", "client", "python")]
    text = {"command": '"$FORGE" issue comment --iid 7  # FORGE=example-forge, set by the caller'}
    assert outbound.classify("terminal", text) == [outbound.Write("gitlab", "?", "?")]


def test_slack_writes_from_text_are_outbound(outbound):
    assert outbound.classify("terminal", {"command": "hermes -p bot send slack:C0123456 hi"}) == [
        outbound.Write("slack", "hermes-send")]
    assert outbound.classify("terminal", {"command": "curl -X POST https://slack.com/api/chat.postMessage"}) == [
        outbound.Write("slack", "raw")]
    assert outbound.classify("terminal", {"command": "curl https://slack.com/api/conversations.history"}) == []


SETTINGS = {"bot": {"channels": ["C0ALLOWED"], "users": ["U0ALLOWED"]}}
SLACK_SESSION = {"platform": "slack", "chat_id": "C0LIVE01", "thread_id": "171.1", "user_id": "U0ASKER1"}


def _create(cron, deliver, prompt="daily summary", session=SLACK_SESSION):
    return cron.reason({"action": "create", "deliver": deliver, "prompt": prompt}, profile="bot",
                       settings=SETTINGS, session=session)


def test_cron_delivery_admits_the_allow_list_the_origin_and_local(cron):
    assert _create(cron, "slack:C0ALLOWED") is None
    assert _create(cron, "slack:U0ALLOWED") is None
    assert _create(cron, "origin") is None
    assert _create(cron, None) is None
    assert _create(cron, "local") is None
    assert _create(cron, "slack:C0LIVE01:171.1") is None


def test_cron_delivery_refuses_other_targets(cron):
    assert "not allowed" in _create(cron, "slack:C0OTHER01")
    assert "not allowed" in _create(cron, "slack:C0LIVE01")  # a channel only inside its thread
    assert "not a delivery this fleet allows" in _create(cron, "telegram:12345")


def test_cron_delivery_refuses_a_broadcast_to_slack(cron):
    assert "broadcast" in _create(cron, "slack:C0ALLOWED", prompt="status for <!here>")
    assert _create(cron, "local", prompt="status for @channel") is None
    assert cron.has_mass_mention("ops@here.example") is False
