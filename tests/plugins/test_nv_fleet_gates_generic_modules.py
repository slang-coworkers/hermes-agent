"""nv-fleet-gates' generic helper modules with their fork defaults: no forge CLI, no review bot, redaction on."""

from __future__ import annotations

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
