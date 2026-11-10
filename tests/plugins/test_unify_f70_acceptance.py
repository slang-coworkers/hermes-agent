"""UNIFY-F70 (UNIFY-A) acceptance: the fork is the single source of the common nv-* plugins.

One test per ``pytest:`` row of ADR ``reports/unify-f70.md`` §Acceptance criteria that is not a carried node set
(AC-UNIFY-F70-2, -3 and -4 are carried sets, verified by the tester's ``scripts/run_tests.sh`` runs; ADR §D4).
The registration-API contract under test is ADR §Product-hook registration API; the checker rule is ADR §D5.
``POINTS`` holds one drive per hook point: it exercises the point's real call site in a loaded nv-fleet-gates and
returns an observation that equals ``seen_default`` with nothing registered and ``seen_valid`` with the probe plugin
registered. ``NEW_OUT_OF_SURFACE`` holds the ruled new paths outside the plugin surface (ADR §D3, DN-1).
This file lands in a public repository: it names no downstream product, host or person.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import inspect
import json
import shutil
import subprocess
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import pytest


REPO = Path(__file__).resolve().parents[2]
SCEN = REPO / "tests" / "e2e-scenarios" / "UNIFY-F70"
CHECKER = REPO / "tests" / "e2e-scenarios" / "UPG-F69" / "replay_check.py"
PLUGINS = REPO / "plugins"
COMMON = ("nv-fleet-gates", "nv-artifact", "nv-coworker-compose")
PUBLISHED = "nv_fleet_gates_product_hooks"
# ADR §D3/§D5: the only new paths outside the plugin surface DN-21 (msg 36) admits; DN-1 option (a), no COMMON_PIN.
NEW_OUT_OF_SURFACE = ("common/COMMON_PATHS", "scripts/common_drift.py", ".github/workflows/common-drift.yml")


PLUGIN_KEY = "nv-fleet-gates"
PROBE_KEY = "unify-f70-probe"
STAGE_MARKERS = ["[Fix Report]", "[Review Verdict]", "[Resolution]", "[handoff]"]

PROBE_REFUSAL = "UNIFY-F70 probe refusal"
PROBE_REWRITE = "UNIFY-F70 probe rewrite"
# No "%" and no marker word, so the placeholder redaction leaves both texts unchanged.
PROBE_RESULT = '{"content": "probe file body"}'
PROBE_CHANNEL = "CPROBE0001"
PROBE_THREAD = "1700000000.000100"
PROBE_OWN_THREAD = f"{PROBE_CHANNEL}:{PROBE_THREAD}"
# "--iid 7": the parser reads 7 as the option's int value; the no-parser path drops the option and keeps no iid.
FORGE_ARGV = ("issue", "comment", "--iid", "7")

POST_ALLOWLIST_KEY = "slack_post"


def _settings(home, edges_db, **overrides) -> dict[str, Any]:
    """nv-fleet-gates settings the acceptance test's ``_home`` writes: the fork harness ``_base_settings``
    plus the neutral fork defaults of the three generic checks the reconcile brings in (all OFF)."""
    home = Path(home)
    settings: dict[str, Any] = {
        "role": "worker",
        "profile_roles": {"orch": "orchestrator", "worker-a": "worker",
                          "worker-b": "worker", "worker-c": "worker"},
        "enforce_sandbox": False,
        "expected_backend": "docker",
        "required_stages": ["OUTPUT_REVIEW"],
        "plan_gate": True,
        "edges_db_path": str(edges_db),
        "stage_markers": list(STAGE_MARKERS),
        "admin_tools": ["onboard_coworker", "onboard_project", "create_agent",
                        "record_decision", "record_human_verdict"],
        "skills_root": str(home / "skills"),
        "shared_learnings_path": str(home / "shared-learnings"),
        "profile": home.name,
        "chain": {"outbound_policy": False},
        "cron_delivery_guard": False,
        "critique_push_remotes": [],
        POST_ALLOWLIST_KEY: {},
    }
    settings.update(overrides)
    return settings


def config_for(home, edges_db, point: Optional[Point] = None, *, with_probe: bool = False) -> dict[str, Any]:
    """The whole config.yaml dict for one home: nv-fleet-gates enabled with ``_settings`` plus the point's
    overrides, and (``with_probe``) the probe plugin enabled and pointed at ``point.name``."""
    overrides = dict(point.overrides) if point is not None else {}
    entries: dict[str, Any] = {PLUGIN_KEY: {"settings": _settings(home, edges_db, **overrides)}}
    enabled = [PLUGIN_KEY]
    if with_probe:
        if point is None:
            raise ValueError("with_probe needs a point")
        enabled.append(PROBE_KEY)
        entries[PROBE_KEY] = {"settings": {"point": point.name}}
    return {"plugins": {"enabled": enabled, "entries": entries}}


# Written by the test into <home>/plugins/unify-f70-probe/ (write_text(..., encoding="utf-8")). manifest v2
# because requires_plugins is a v2 field; it makes the loader load nv-fleet-gates first in the same scope.
PROBE_PLUGIN_YAML = """\
manifest_version: 2
name: unify-f70-probe
version: 0.0.1
description: Test probe that registers one nv-fleet-gates product-hook point.
requires_plugins: [nv-fleet-gates]
provides_tools: []
provides_hooks: []
"""

# The import is deliberately unguarded: a missing registry module must surface as the probe's load error,
# never as a silent "nothing registered" that would read as the empty default.
PROBE_INIT_PY = '''\
CALLS = []
RETURN = None


def register(ctx):
    point = ctx.get_config("point")
    if not point or not ctx.has_plugin("nv-fleet-gates"):
        return
    import nv_fleet_gates_product_hooks as hooks

    if getattr(hooks, "API_VERSION", None) != 1:
        raise RuntimeError("nv-fleet-gates product-hook API_VERSION is not 1")

    def fn(**kwargs):
        CALLS.append(sorted(kwargs))
        return RETURN

    hooks.register_product_hook(ctx, point, fn)
'''


def probe_forge_parser(spec: dict[str, Any]) -> argparse.ArgumentParser:
    """A parser that knows one ``<noun> <verb>`` pair and its int options. FORGE_ARGV never makes it error,
    so it needs no silencing (the call site's own _ParseExit handling is not reached)."""
    parser = argparse.ArgumentParser(prog="forge", add_help=False)
    parser.add_argument("--project")
    nouns = parser.add_subparsers(dest="noun")
    verbs = nouns.add_parser(spec["noun"], add_help=False).add_subparsers(dest="verb")
    verb = verbs.add_parser(spec["verb"], add_help=False)
    for option in spec.get("int_options", ()):
        verb.add_argument(option, type=int)
    return parser


def materialise(point: str, value: Any) -> Any:
    """The JSON form of a point's return turned into the value the hook returns."""
    if point == "own_post_threads":
        return frozenset(value or ())
    if point == "forge_cli_parser":
        return probe_forge_parser(value) if value else None
    return value


def _plugin_module(manager) -> types.ModuleType:
    loaded = manager._plugins[PLUGIN_KEY]
    if not (loaded.enabled and loaded.error is None and loaded.module is not None):
        raise AssertionError(f"{PLUGIN_KEY} did not load: enabled={loaded.enabled} error={loaded.error}")
    return loaded.module


def _submodule(manager, name: str) -> types.ModuleType:
    """The submodule the loaded plugin itself uses (its ``from . import <name>`` attribute), else the import
    under the scope's own package name (bare or ``__home_<digest>``)."""
    module = _plugin_module(manager)
    sub = getattr(module, name, None)
    if isinstance(sub, types.ModuleType):
        return sub
    return importlib.import_module(f"{module.__name__}.{name}")


def _with_home_if_accepted(manager, fn: Callable[..., Any], kwargs: dict[str, Any]) -> dict[str, Any]:
    # A post-reconcile signature may take the registry home explicitly; a signature check, not a source read.
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return kwargs
    if "home" in params:
        # The manager's own home, never the ambient one: AC-7 drives home A after loading home B.
        kwargs = dict(kwargs, home=str(Path(manager.home_path).resolve()))
    return kwargs


def _gate(manager, tool_name: str, args: dict[str, Any], session_id: str = "sess-1") -> Optional[dict]:
    """The fork harness ``_gate``: the first directive any pre_tool_call callback returned, else None."""
    results = manager.invoke_hook(
        "pre_tool_call", tool_name=tool_name, args=args, session_id=session_id,
        task_id="task-1", tool_call_id="tc-1", turn_id="turn-1")
    directives = [r for r in (results or []) if isinstance(r, dict) and r.get("action")]
    return directives[0] if directives else None


def _directive_obs(directive: Optional[dict], needle: str, label: str) -> dict[str, Any]:
    message = str((directive or {}).get("message") or "")
    return {"action": (directive or {}).get("action"), label: bool(needle) and needle in message}


def _drive_worker_read(manager) -> dict[str, Any]:
    return _directive_obs(_gate(manager, "read_file", {"path": "README.md"}), PROBE_REFUSAL, "probe_text")


def drive_early_veto(manager) -> dict[str, Any]:
    return _drive_worker_read(manager)


def drive_policy_veto(manager) -> dict[str, Any]:
    return _drive_worker_read(manager)


def drive_delivery_veto(manager) -> dict[str, Any]:
    return _drive_worker_read(manager)


def drive_tool_result_rewrite(manager) -> Optional[str]:
    results = manager.invoke_hook(
        "transform_tool_result", tool_name="read_file", args={"path": "README.md"}, result=PROBE_RESULT,
        session_id="sess-1", task_id="task-1", tool_call_id="tc-1", turn_id="turn-1",
        duration_ms=1, status="ok", error_type=None, error_message=None)
    return next((r for r in (results or []) if isinstance(r, str)), None)


def drive_room_session_kind(manager) -> str:
    room_session = _plugin_module(manager)._room_session
    return room_session("sess-1", "api_server", **_with_home_if_accepted(manager, room_session, {}))


def drive_room_session_kind_via_gate(manager) -> dict[str, Any]:
    return _directive_obs(_gate(manager, "kanban_create", {"title": "probe card"}), "group room",
                          "room_refusal")


def drive_own_post_threads(manager) -> dict[str, Any]:
    args = {"action": "create", "schedule": "every 1h", "prompt": "probe status report",
            "deliver": f"slack:{PROBE_OWN_THREAD}"}
    return _directive_obs(_gate(manager, "cronjob_manage", args), "cron delivery:", "cron_refusal")


def drive_forge_cli_parser(manager) -> Optional[dict[str, Any]]:
    forge_op = _submodule(manager, "outbound").forge_op
    write = forge_op(list(FORGE_ARGV), **_with_home_if_accepted(manager, forge_op, {}))
    if write is None:
        return None
    return {"noun": write.noun, "verb": write.verb, "iid": write.iid}


_VETO_KWARGS = ("tool_name", "args", "session_id", "task_id", "profile", "is_orchestrator", "kanban_task", "home")
_NOT_BLOCKED = {"action": None, "probe_text": False}
_PROBE_BLOCKED = {"action": "block", "probe_text": True}


@dataclass(frozen=True)
class Point:
    """One registry point: what its call site passes, its empty and probe returns (JSON form, see
    ``materialise``), and what ``drive`` observes for each. ``kwargs`` are names only, ``home`` included
    (ADR: every point is called with its keyword args plus ``home``); AC-7 checks them as a subset of what
    the probe's callback received."""

    name: str
    call_site: str
    kwargs: tuple[str, ...]
    default: Any
    valid: Any
    seen_default: Any
    seen_valid: Any
    drive: Callable[[Any], Any]
    profile: str = "worker-a"
    overrides: dict = field(default_factory=dict, hash=False)
    id: str = ""

    @property
    def test_id(self) -> str:
        return self.id or self.name


POINTS: tuple[Point, ...] = (
    Point("early_veto", "downstream __init__.py:1227-1232 (_gate, before SANDBOX)", _VETO_KWARGS,
          default=None, valid=PROBE_REFUSAL, seen_default=_NOT_BLOCKED, seen_valid=_PROBE_BLOCKED,
          drive=drive_early_veto),
    Point("policy_veto", "downstream __init__.py:1233-1238 (_gate, after CRON-DELIVERY, before OUTBOUND)",
          _VETO_KWARGS + ("origin", "owned_card", "kanban_tenants", "writes"),
          default=None, valid=PROBE_REFUSAL, seen_default=_NOT_BLOCKED, seen_valid=_PROBE_BLOCKED,
          drive=drive_policy_veto),
    Point("delivery_veto", "downstream __init__.py:1242-1244 (_gate, after OUTBOUND, before GATES)",
          ("tool_name", "args", "session_id", "task_id", "profile", "home"),
          default=None, valid=PROBE_REFUSAL, seen_default=_NOT_BLOCKED, seen_valid=_PROBE_BLOCKED,
          drive=drive_delivery_veto),
    Point("tool_result_rewrite", "downstream __init__.py:1341-1353 (_transform_tool_result, before redaction)",
          ("tool_name", "args", "result", "session_id", "task_id", "status", "error_type", "error_message",
           "duration_ms", "home"),
          default=None, valid=PROBE_REWRITE, seen_default=None, seen_valid=PROBE_REWRITE,
          drive=drive_tool_result_rewrite),
    Point("room_session_kind", "downstream __init__.py:279-291 (_room_session)",
          ("session_id", "platform", "home"),
          default="no", valid="room", seen_default="no", seen_valid="room",
          drive=drive_room_session_kind),
    Point("own_post_threads", "downstream __init__.py:726-746 (_cron_delivery_block -> cron_delivery.reason)",
          ("profile", "env", "home"),
          default=[], valid=[PROBE_OWN_THREAD],
          seen_default={"action": "block", "cron_refusal": True},
          seen_valid={"action": None, "cron_refusal": False},
          drive=drive_own_post_threads, profile="orch",
          overrides={"cron_delivery_guard": True,
                     POST_ALLOWLIST_KEY: {"orch": {"channels": [PROBE_CHANNEL], "users": [],
                                                   "own_threads_only": True}}}),
    Point("forge_cli_parser", "downstream outbound.py:241-260, 278 (forge_parser / forge_op)",
          ("home",),
          default=None, valid={"noun": "issue", "verb": "comment", "int_options": ["--iid"]},
          seen_default={"noun": "issue", "verb": "comment", "iid": None},
          seen_valid={"noun": "issue", "verb": "comment", "iid": 7},
          drive=drive_forge_cli_parser),
)

# Optional second drive for room_session_kind through the public pre_tool_call hook (outbound policy on).
# Kept out of POINTS so AC-6 stays one parametrized case per point; usable as an extra AC-7 case.
GATE_VARIANTS: tuple[Point, ...] = (
    Point("room_session_kind", "downstream __init__.py:961 via _outbound_block :998",
          ("session_id", "platform", "home"),
          default="no", valid="room",
          seen_default={"action": None, "room_refusal": False},
          seen_valid={"action": "block", "room_refusal": True},
          drive=drive_room_session_kind_via_gate,
          overrides={"chain": {"outbound_policy": True}}, id="room_session_kind-gate"),
)

POINTS_BY_NAME: dict[str, Point] = {p.name: p for p in POINTS}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _home(root: Path, point: Point, *, with_probe: bool = False, also: tuple[str, ...] = ()) -> Path:
    """``<root>/home/<point.profile>`` with nv-fleet-gates (plus ``also``, plus the probe) copied in and enabled."""
    home = root / "home" / point.profile
    (home / "plugins").mkdir(parents=True)
    edges_db = root / "fleet" / "edges.db"
    edges_db.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(PLUGINS / PLUGIN_KEY, home / "plugins" / PLUGIN_KEY)
    cfg = config_for(home, edges_db, point, with_probe=with_probe)
    for key in also:
        shutil.copytree(PLUGINS / key, home / "plugins" / key)
        cfg["plugins"]["enabled"].append(key)
    if "nv-artifact" in also:
        cfg["plugins"]["entries"]["nv-artifact"] = {"settings": {"ledger_profile": "default"}}
    if with_probe:
        _write(home / "plugins" / PROBE_KEY / "plugin.yaml", PROBE_PLUGIN_YAML)
        _write(home / "plugins" / PROBE_KEY / "__init__.py", PROBE_INIT_PY)
    # JSON is YAML; this keeps the config writer independent of a yaml dependency.
    _write(home / "config.yaml", json.dumps(cfg))
    return home


def _load(home: Path, bundled: Path, monkeypatch):
    """Discover and load ``home``'s plugins through the real path (the fork's nv-fleet-gates harness shape)."""
    monkeypatch.setenv("HOME", str(home.parent.parent / "fakehome"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    for name in ("HERMES_ENABLE_PROJECT_PLUGINS", "HERMES_KANBAN_TASK", "HERMES_MANAGED_DIR"):
        monkeypatch.delenv(name, raising=False)
    import model_tools

    model_tools.discover_builtin_tools()
    from hermes_cli.plugins import PluginManager

    manager = PluginManager()
    manager.discover_and_load()
    return manager


def _assert_loaded(manager, keys) -> None:
    for key in keys:
        loaded = manager._plugins[key]
        assert loaded.enabled is True and loaded.error is None and loaded.module is not None, (key, loaded.error)


@pytest.fixture
def bundled(tmp_path) -> Path:
    path = tmp_path / "empty-bundled"
    path.mkdir()
    return path


@pytest.fixture(autouse=True)
def _fresh_registry():
    """The registry is process-wide by design (ADR §Product-hook registration API); each test starts without it."""
    sys.modules.pop(PUBLISHED, None)
    yield
    sys.modules.pop(PUBLISHED, None)


def _manifest_sha(manifest: Path, suffix: str) -> str:
    """The sha256 of the ``sha256sum``-format line whose fork path ends with ``suffix``."""
    hits = [ln.split()[0] for ln in manifest.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#") and ln.split()[-1].endswith(suffix)]
    assert len(hits) == 1, f"{manifest} must name exactly one {suffix}: {hits}"
    return hits[0]


def test_ac_unify_f70_1():
    """`common/COMMON_PATHS` at the head is byte-identical to the agreed file: the downstream MR's file plus the line `plugins/nv-fleet-gates/product_hooks.py` after `predicates.py` (the request's "drift-script unit tests pass" half is the carried node set `tests/plugins/test_common_drift.py`)"""
    want = _manifest_sha(SCEN / "downstream-mr.sha256", "common/COMMON_PATHS")
    assert hashlib.sha256((REPO / "common" / "COMMON_PATHS").read_bytes()).hexdigest() == want


@pytest.mark.parametrize("point", POINTS, ids=[p.test_id for p in POINTS])
def test_ac_unify_f70_6(point, tmp_path, bundled, monkeypatch):
    """(derived) With nv-fleet-gates, nv-artifact and nv-coworker-compose loaded and no product plugin, the registry holds no registration for any hook point and every call site takes that point's stated empty-registry default"""
    home = _home(tmp_path, point, also=("nv-artifact", "nv-coworker-compose"))
    manager = _load(home, bundled, monkeypatch)
    _assert_loaded(manager, COMMON)
    hooks = sys.modules[PUBLISHED]
    assert point.name in hooks.POINTS
    assert hooks.registered(home, point.name) is None
    assert point.drive(manager) == point.seen_default


@pytest.mark.parametrize("point", POINTS + GATE_VARIANTS, ids=[p.test_id for p in POINTS + GATE_VARIANTS])
def test_ac_unify_f70_7(point, tmp_path, bundled, monkeypatch):
    """(derived) A plugin discovered from an isolated `HERMES_HOME` that declares `requires_plugins: [nv-fleet-gates]` and registers a callback for a point from `register(ctx)` has that callback invoked at the point, with the point's keyword arguments, in its own profile only"""
    home_a = _home(tmp_path / "a", point, with_probe=True)
    manager_a = _load(home_a, bundled, monkeypatch)
    _assert_loaded(manager_a, (PLUGIN_KEY, PROBE_KEY))
    probe = manager_a._plugins[PROBE_KEY].module
    probe.RETURN = materialise(point.name, point.valid)

    home_b = _home(tmp_path / "b", point)
    manager_b = _load(home_b, bundled, monkeypatch)
    _assert_loaded(manager_b, (PLUGIN_KEY,))

    hooks = sys.modules[PUBLISHED]
    assert hooks.registered(home_a, point.name) == PROBE_KEY
    assert hooks.registered(home_b, point.name) is None
    assert point.drive(manager_a) == point.seen_valid
    assert probe.CALLS and set(point.kwargs) <= set(probe.CALLS[-1]), probe.CALLS
    calls_before = len(probe.CALLS)
    assert point.drive(manager_b) == point.seen_default
    assert len(probe.CALLS) == calls_before, "another profile's call site reached this profile's callback"

    assert manager_a.unload(PROBE_KEY)
    assert hooks.registered(home_a, point.name) is None
    assert point.drive(manager_a) == point.seen_default


class _FakeCtx:
    """The two PluginContext members the register call uses (ADR: `ctx.plugin_id`, `ctx.on_unload`)."""

    def __init__(self, plugin_id: str):
        self.plugin_id = plugin_id
        self._unloads: list[Callable[[], None]] = []

    def on_unload(self, callback):
        self._unloads.append(callback)
        return callback

    def unload(self) -> None:
        for callback in reversed(self._unloads):
            callback()


def test_ac_unify_f70_8(tmp_path, bundled, monkeypatch):
    """(derived) `register_product_hook` enforces the frozen rules: same owner replaces, a second owner for the same (home, point) raises `ValueError`, an unknown point raises `ValueError`, a non-callable raises `TypeError`, a late registration applies from the next call, unload removes the owner's callbacks, a raising or out-of-contract veto blocks, and the two read-only accessors report the loaded profile and its role (`False` / `""` for a home without nv-fleet-gates)"""
    point = POINTS_BY_NAME["early_veto"]
    home = _home(tmp_path, point)
    manager = _load(home, bundled, monkeypatch)
    _assert_loaded(manager, (PLUGIN_KEY,))
    hooks = sys.modules[PUBLISHED]
    key = str(home.resolve())
    kwargs = {name: None for name in point.kwargs if name != "home"}
    calls: list[str] = []

    def ask() -> Any:
        return hooks.call(point.name, home=key, default=point.default, **kwargs)

    def fn(tag: str):
        def _hook(**_kw):
            calls.append(tag)
            return point.valid
        return _hook

    owner, other = _FakeCtx("nv-owner"), _FakeCtx("nv-other")
    assert ask() == point.default and calls == []

    hooks.register_product_hook(owner, point.name, fn("first"))
    assert ask() == point.valid and calls == ["first"], "a late registration applies from the next call"

    hooks.register_product_hook(owner, point.name, fn("second"))
    assert ask() == point.valid and calls == ["first", "second"], "the same owner replaces its callable"

    with pytest.raises(ValueError):
        hooks.register_product_hook(other, point.name, fn("other"))
    assert hooks.registered(home, point.name) == "nv-owner"
    with pytest.raises(ValueError):
        hooks.register_product_hook(owner, "nv-unify-f70-no-such-point", fn("unknown"))
    with pytest.raises(TypeError):
        hooks.register_product_hook(owner, point.name, "not callable")

    owner.unload()
    assert hooks.registered(home, point.name) is None and ask() == point.default
    assert calls == ["first", "second"]

    hooks.register_product_hook(other, point.name, fn("third"))
    assert hooks.unregister_product_hooks(other) == 1
    assert hooks.registered(home, point.name) is None

    parser_point = POINTS_BY_NAME["forge_cli_parser"]
    parser_owner = _FakeCtx("parser-owner")
    assert parser_point.drive(manager) == parser_point.seen_default
    hooks.register_product_hook(parser_owner, parser_point.name, lambda **_: probe_forge_parser(parser_point.valid))
    assert parser_point.drive(manager) == parser_point.seen_valid
    hooks.register_product_hook(parser_owner, parser_point.name, lambda **_: None)
    assert parser_point.drive(manager) == parser_point.seen_default
    parser_owner.unload()

    def boom(**_kw):
        raise RuntimeError("probe failure")

    for bad in (boom, lambda **_kw: 42):
        hooks.register_product_hook(owner, point.name, bad)
        assert point.drive(manager)["action"] == "block"
        hooks.unregister_product_hooks(owner)
    assert point.drive(manager) == point.seen_default
    _check_accessors(tmp_path, bundled, monkeypatch)


def _check_accessors(tmp_path, bundled, monkeypatch) -> None:
    """The read-only accessors report the loaded profile and its role; a home without nv-fleet-gates reads as
    ``False`` / ``""``."""
    worker, orch = POINTS_BY_NAME["early_veto"], POINTS_BY_NAME["own_post_threads"]
    home_w = _home(tmp_path / "w", worker)
    _assert_loaded(_load(home_w, bundled, monkeypatch), (PLUGIN_KEY,))
    home_o = _home(tmp_path / "o", orch)
    _assert_loaded(_load(home_o, bundled, monkeypatch), (PLUGIN_KEY,))
    hooks = sys.modules[PUBLISHED]
    assert hooks.current_profile(home=str(home_w.resolve())) == worker.profile
    assert hooks.is_orchestrator(home=str(home_w.resolve())) is False
    assert hooks.current_profile(home=str(home_o.resolve())) == orch.profile
    assert hooks.is_orchestrator(home=str(home_o.resolve())) is True
    nowhere = str((tmp_path / "no-plugins").resolve())
    assert hooks.is_orchestrator(home=nowhere) is False and hooks.current_profile(home=nowhere) == ""


TSV_HEADERS = {
    "path_inventory.tsv": "status\tpath\tsurface\tclass\torigin\tauthority",
    "core_authority.tsv": "key\tauthority\tdisposition",
    "ruled_residuals.tsv": "key\tsha256\tauthority",
    "overlap_rulings.tsv": "key\tsha256\truling",
    "fork_ci_map.tsv": "kind\tmatch\treplacement\tauthority",
    "fork_ci_lines.tsv": "workflow\tjob\tanchor\tbefore\tafter\tauthority",
    "fork_ci_contributors.tsv": "path\tsha256\tauthority",
    "fork_ci_sizing.tsv": "workflow\tjob\tkey\tvalue\tauthority",
}
NEW_FILES_HEADER = "path\tsha256\tauthority"
WORKFLOW = ("name: common-drift\non: [pull_request, workflow_dispatch]\njobs:\n  common-drift:\n"
            "    runs-on: ubuntu-latest\n    continue-on-error: true\n    steps:\n"
            "      - uses: actions/checkout@f548e57e544e1ff5a4c46bf1e1b8685f8e4a348a # v7.0.1\n"
            "      - run: python3 scripts/common_drift.py\n")
NEW = {NEW_OUT_OF_SURFACE[0]: "plugins/nv-fleet-gates/\n", NEW_OUT_OF_SURFACE[1]: "def main():\n    return 0\n",
       NEW_OUT_OF_SURFACE[2]: WORKFLOW}
AUTH = "DN-21 msg 36"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=unify-f70", "-c", "user.email=unify-f70@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


def _surface(path: str) -> str:
    if path.startswith(("plugins/", "website/docs/", "tests/", "apps/desktop/e2e/")):
        return "in-surface"
    return "fork-ci" if path.startswith(".github/") else "core"


def _synthetic(tmp_path: Path, *, files: dict[str, str], rows: list[tuple[str, str]]) -> subprocess.CompletedProcess:
    """from = onto = head = a base commit; the candidate adds ``files`` and edits one in-surface file. Every changed
    path gets its inventory row; ``rows`` are the ruled_new_files.tsv rows (path, sha256)."""
    repo, data = tmp_path / "repo", tmp_path / "data"
    repo.mkdir(parents=True)
    data.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "base")
    _write(repo / "plugins" / "p" / "__init__.py", "X = 1\n")
    base = _commit(repo, "base")
    for path, text in files.items():
        _write(repo / path, text)
    _write(repo / "plugins" / "p" / "__init__.py", "X = 2\n")
    candidate = _commit(repo, "candidate")

    _write(data / "bump.json", json.dumps({"from": base, "onto": base, "head": base}))
    inventory = [f"A\t{p}\t{_surface(p)}\tnew\tUNIFY-F70\t{AUTH}" for p in sorted(files)]
    inventory.append("M\tplugins/p/__init__.py\tin-surface\tnew\tUNIFY-F70\t")
    for name, header in TSV_HEADERS.items():
        _write(data / name, "\n".join([header, *(inventory if name == "path_inventory.tsv" else [])]) + "\n")
    _write(data / "ruled_new_files.tsv", "\n".join([NEW_FILES_HEADER, *(f"{p}\t{s}\t{AUTH}" for p, s in rows)]) + "\n")
    return subprocess.run(
        [sys.executable, str(CHECKER), "--repo", str(repo), "--from", base, "--onto", base, "--head", base,
         "--candidate", candidate, "--data-dir", str(data)],
        capture_output=True, text=True, encoding="utf-8", timeout=300, check=False,
    )


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fails(result: subprocess.CompletedProcess, code: str, path: str) -> bool:
    return result.returncode == 1 and any(
        ln.startswith(f"FAIL {code} ") and path in ln for ln in result.stdout.splitlines())


def test_ac_unify_f70_9(tmp_path):
    """(derived, DN-21 P4) The F69 replay checker accepts a new path outside `plugins/**`, `tests/**`, `website/docs/**`, `apps/desktop/e2e/**` only when it is one of the three DN-21 drift paths with a `ruled_new_files.tsv` row whose sha256 equals the candidate blob"""
    good_rows = [(p, _sha(t)) for p, t in NEW.items()]

    ok = _synthetic(tmp_path / "ok", files=NEW, rows=good_rows)
    assert ok.returncode == 0 and ok.stdout.splitlines()[-1].startswith("OK "), ok.stdout + ok.stderr

    fourth = dict(NEW, **{"scripts/other_tool.py": "X = 1\n"})
    extra = _synthetic(tmp_path / "fourth", files=fourth, rows=[*good_rows, ("scripts/other_tool.py", _sha("X = 1\n"))])
    assert _fails(extra, "NEW_FILE_UNRULED", "scripts/other_tool.py"), extra.stdout

    bad_hash = [(p, "0" * 64 if p == NEW_OUT_OF_SURFACE[0] else s) for p, s in good_rows]
    mismatch = _synthetic(tmp_path / "mismatch", files=NEW, rows=bad_hash)
    assert _fails(mismatch, "NEW_FILE_MISMATCH", NEW_OUT_OF_SURFACE[0]), mismatch.stdout

    without_workflow = {p: t for p, t in NEW.items() if p != NEW_OUT_OF_SURFACE[2]}
    stale = _synthetic(tmp_path / "stale", files=without_workflow, rows=good_rows)
    assert _fails(stale, "DATA_STALE", NEW_OUT_OF_SURFACE[2]), stale.stdout

    duplicate = _synthetic(tmp_path / "duplicate", files=NEW, rows=[*good_rows, good_rows[0]])
    assert _fails(duplicate, "DATA_DUPLICATE", NEW_OUT_OF_SURFACE[0]), duplicate.stdout

    broken = dict(NEW, **{NEW_OUT_OF_SURFACE[1]: "def main(:\n"})
    syntax = _synthetic(tmp_path / "syntax", files=broken,
                        rows=[(p, _sha(t)) for p, t in broken.items()])
    assert _fails(syntax, "SYNTAX", NEW_OUT_OF_SURFACE[1]), syntax.stdout
