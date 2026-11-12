"""nv-fleet-gates product-hook registry: the contract cases the acceptance test does not drive through a gate."""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[2] / "plugins" / "nv-fleet-gates" / "product_hooks.py"
PUBLISHED = "nv_fleet_gates_product_hooks"


class _Ctx:
    def __init__(self, plugin_id: str):
        self.plugin_id = plugin_id
        self.unloads = []

    def on_unload(self, callback):
        self.unloads.append(callback)
        return callback

    def unload(self):
        for callback in reversed(self.unloads):
            callback()


def _copy(name: str):
    spec = importlib.util.spec_from_file_location(name, MODULE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def hooks(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    sys.modules.pop(PUBLISHED, None)
    module = _copy("nv_fleet_gates_product_hooks_test_a")
    yield module
    sys.modules.pop(PUBLISHED, None)
    sys.modules.pop("nv_fleet_gates_product_hooks_test_a", None)
    sys.modules.pop("nv_fleet_gates_product_hooks_test_b", None)


def _home(tmp_path) -> str:
    return str((tmp_path / "home").resolve())


def test_a_second_owner_is_refused_with_both_owners_named(hooks):
    hooks.register_product_hook(_Ctx("first-owner"), "early_veto", lambda **_: None)
    with pytest.raises(ValueError) as err:
        hooks.register_product_hook(_Ctx("second-owner"), "early_veto", lambda **_: None)
    assert "first-owner" in str(err.value) and "second-owner" in str(err.value)


def test_a_later_copy_delegates_to_the_published_registry(hooks, tmp_path):
    assert sys.modules[PUBLISHED] is hooks
    later = _copy("nv_fleet_gates_product_hooks_test_b")
    later.register_product_hook(_Ctx("owner"), "policy_veto", lambda **_: "no")
    assert hooks.registered(_home(tmp_path), "policy_veto") == "owner"
    assert later.call("policy_veto", home=_home(tmp_path), default=None) == "no"


def test_the_callable_gets_keyword_arguments_plus_home(hooks, tmp_path):
    seen = {}

    def fn(**kwargs):
        seen.update(kwargs)

    hooks.register_product_hook(_Ctx("owner"), "delivery_veto", fn)
    assert hooks.call("delivery_veto", home=_home(tmp_path), default=None, tool_name="read_file") is None
    assert seen == {"tool_name": "read_file", "home": _home(tmp_path)}


@pytest.mark.parametrize(
    "point,bad,platform,expected",
    [
        ("tool_result_rewrite", 7, None, None),
        ("room_session_kind", "maybe", "api_server", "unknown"),
        ("room_session_kind", "maybe", "cli", "no"),
        ("room_session_kind", [], "api_server", "unknown"),
        ("room_session_kind", [], "cli", "no"),
        ("forge_cli_parser", "not a parser", None, None),
    ],
)
def test_an_out_of_contract_return_takes_the_points_error_path(hooks, tmp_path, point, bad, platform, expected):
    hooks.register_product_hook(_Ctx("owner"), point, lambda **_: bad)
    assert hooks.call(point, home=_home(tmp_path), default="default", platform=platform) == expected


@pytest.mark.parametrize("point", ["early_veto", "policy_veto", "delivery_veto"])
def test_a_raising_veto_returns_a_refusal(hooks, tmp_path, point):
    def boom(**_):
        raise RuntimeError("probe")

    hooks.register_product_hook(_Ctx("owner"), point, boom)
    refusal = hooks.call(point, home=_home(tmp_path), default=None)
    assert isinstance(refusal, str) and refusal and "probe" not in refusal


def test_own_post_threads_fails_closed_and_normalises_to_a_frozenset(hooks, tmp_path):
    ctx = _Ctx("owner")
    hooks.register_product_hook(ctx, "own_post_threads", lambda **_: {"C1:1.2"})
    assert hooks.call("own_post_threads", home=_home(tmp_path), default=frozenset()) == frozenset({"C1:1.2"})
    hooks.register_product_hook(ctx, "own_post_threads", lambda **_: ["C1:1.2"])
    with pytest.raises(hooks.ProductHookError):
        hooks.call("own_post_threads", home=_home(tmp_path), default=frozenset())


def test_a_parser_return_is_kept(hooks, tmp_path):
    parser = argparse.ArgumentParser(add_help=False)
    hooks.register_product_hook(_Ctx("owner"), "forge_cli_parser", lambda **_: parser)
    assert hooks.call("forge_cli_parser", home=_home(tmp_path), default=None) is parser


def test_an_unloaded_profile_reads_as_no_profile(hooks, tmp_path):
    ctx = _Ctx("nv-fleet-gates")
    hooks._publish_profile(ctx, home=_home(tmp_path), profile="orch", is_orchestrator=True)
    assert hooks.current_profile(home=tmp_path / "home") == "orch" and hooks.is_orchestrator(home=_home(tmp_path))
    ctx.unload()
    assert hooks.current_profile(home=_home(tmp_path)) == "" and hooks.is_orchestrator(home=_home(tmp_path)) is False
