"""GOV-F22 ADOPT-verify acceptance test — guard catalog (allow/hold/deny) covered
by the merged ``nv-fleet-gates`` fleet veto's restricted-set integrity.

Ships as ``tests/plugins/test_gov_f22_guard_catalog_acceptance.py``. This is the
GOV-F22 row's own self-contained proof; the LOOP-F37 carrier proves the same
behaviours inline as part of its own ADR. Mirrors
``tests/hermes_cli/test_plugin_api_compat.py``: it loads the plugin from an
isolated ``HERMES_HOME`` with an EMPTY bundled-plugin dir through the real
``PluginManager().discover_and_load()`` path, then drives the one
``pre_tool_call`` gate through the production seam.

The covered safety property (dispatch-plan §GOV-F22): the veto's restricted
tool-name set is enumerable and alias-canonicalised. On an essentially complete
registry, a rename/removal causes a loud plugin load failure rather than a
silent missed match; that load failure does not itself stop Hermes. On a
materially partial registry the registered gate-time backstop blocks calls, and
predicate exceptions also return block directives.

One ``test_ac_gov_f22_<n>`` per ``pytest:`` acceptance-criterion id (4), in ADR
order. MUST fail on the stock v2026.8.31 tree (``plugins/nv-fleet-gates/``
absent -> the fixture's ``pytest.fail``) and pass on the fork baseline where the
plugin exists (green-on-baseline is the intended ADOPT proof). Behaviour
contract only: no model lists, no version literals, no reading of source files,
no network, nothing under ``~/.hermes``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from hermes_cli.plugins import PluginManager
from tools.registry import registry

PLUGIN_KEY = "nv-fleet-gates"
_REPO = Path(__file__).resolve().parents[2]
PLUGIN_SRC = _REPO / "plugins" / PLUGIN_KEY


def _settings(hermes_home, edges_db, **overrides):
    settings = {
        "role": "worker",
        "profile_roles": {"orch": "orchestrator", "worker-a": "worker",
                          "w0": "worker", "w1": "worker", "w2": "worker",
                          "w3": "worker", "w4": "worker"},
        "enforce_sandbox": False,
        "expected_backend": "docker",
        "required_stages": ["OUTPUT_REVIEW"],
        "plan_gate": False,
        "edges_db_path": str(edges_db),
        "stage_markers": ["[Fix Report]", "[Review Verdict]"],
        "admin_tools": ["onboard_coworker", "create_agent"],
    }
    settings.update(overrides)
    return settings


def _prime_registry(monkeypatch, hermes_home, bundled):
    monkeypatch.setenv("HOME", str(hermes_home.parent.parent / "fakehome"))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.delenv("HERMES_ENABLE_PROJECT_PLUGINS", raising=False)
    # Importing tools.registry alone does NOT register the built-in tools; the
    # plugin's load-time restricted-set assertion reads get_all_tool_names().
    import model_tools
    model_tools.discover_builtin_tools()


def _load(tmp_path, monkeypatch, *, profile="worker-a", **settings_overrides):
    hermes_home = tmp_path / "home" / profile
    (hermes_home / "plugins").mkdir(parents=True)
    bundled = tmp_path / "empty-bundled"
    bundled.mkdir(exist_ok=True)
    edges_db = tmp_path / "fleet" / f"edges-{profile}.db"
    edges_db.parent.mkdir(parents=True, exist_ok=True)

    if not PLUGIN_SRC.exists():
        pytest.fail(f"plugin source missing: {PLUGIN_SRC}")   # the deliberate stock-tree red
    shutil.copytree(PLUGIN_SRC, hermes_home / "plugins" / PLUGIN_KEY)

    settings = _settings(hermes_home, edges_db, **settings_overrides)
    cfg = {"plugins": {"enabled": [PLUGIN_KEY],
                       "entries": {PLUGIN_KEY: {"settings": settings}}}}
    (hermes_home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")

    _prime_registry(monkeypatch, hermes_home, bundled)
    manager = PluginManager()
    manager.discover_and_load()
    return manager


def _loaded(manager):
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled is True
    assert loaded.error is None
    assert loaded.module is not None
    assert "pre_tool_call" in loaded.hooks_registered
    return loaded


def _gate(manager, tool_name, args, session_id="sess-1"):
    results = manager.invoke_hook(
        "pre_tool_call", tool_name=tool_name, args=args, session_id=session_id,
        task_id="task-1", tool_call_id="tc-1", turn_id="turn-1")
    directives = [r for r in (results or []) if isinstance(r, dict) and r.get("action")]
    return directives[0] if directives else None


def _act(d):
    return d.get("action") if isinstance(d, dict) else None


def _msg(d):
    return d.get("message") if isinstance(d, dict) else ""


class _PredicateCrash(BaseException):
    """A BaseException that is NOT an Exception. AC-GOV-F22-3 names ``except
    BaseException`` specifically; raising this pins that breadth — a refactor to
    ``except Exception`` would let it propagate (core swallows the raising
    pre_tool_call and the tool proceeds = fail-open), turning the node red."""


def test_ac_gov_f22_1(tmp_path, monkeypatch):
    """AC-GOV-F22-1: The veto's restricted set of registry-backed tool names is asserted against
    registry.get_all_tool_names() at load: a missing restricted core name OR an unclassified
    shell-capable registry tool fails the load (loaded.error set, enabled False) as a loud drift
    signal (this load failure does not itself stop Hermes); a stock full-registry load succeeds."""
    import model_tools
    model_tools.discover_builtin_tools()
    real = registry.get_all_tool_names

    # control: an unpatched full registry loads clean, so (a)/(b) below refuse for the RIGHT reason
    # (restricted-set integrity), not an unrelated registration/config failure.
    _loaded(_load(tmp_path, monkeypatch, profile="w0"))

    # (a) a restricted-core tool removed from an essentially-complete registry -> fail LOAD, with
    # the restricted-set refusal message (not any error).
    monkeypatch.setattr(registry, "get_all_tool_names",
                        lambda *a, **k: [n for n in real() if n != "terminal"])
    m1 = _load(tmp_path, monkeypatch, profile="w1")
    assert m1._plugins[PLUGIN_KEY].enabled is False
    assert "restricted core tools absent from the running registry" in (m1._plugins[PLUGIN_KEY].error or "")

    # (b) an un-gated shell-capable/dangerous registry tool -> fail LOAD, with the tripwire message.
    monkeypatch.setattr(registry, "get_all_tool_names",
                        lambda *a, **k: list(real()) + ["exec_shell_unclassified"])
    m2 = _load(tmp_path, monkeypatch, profile="w2")
    assert m2._plugins[PLUGIN_KEY].enabled is False
    assert "shell-capable/dangerous registry tools are not in the gated restricted set" in (m2._plugins[PLUGIN_KEY].error or "")


def test_ac_gov_f22_2(tmp_path, monkeypatch):
    """AC-GOV-F22-2: Incoming tool names are canonicalised through the vendored alias map before
    matching."""
    manager = _load(tmp_path, monkeypatch, profile="worker-a")
    _loaded(manager)
    # the restricted-set backstop is clear on a full registry: a benign call is not blocked, so a
    # block below is the fleet-admin rule, not the fail-closed catch-all.
    assert _act(_gate(manager, "read_file", {})) != "block"
    legacy = _gate(manager, "cronjob", {"action": "add"})
    canonical = _gate(manager, "cronjob_manage", {"action": "add"})
    assert _act(legacy) == _act(canonical) == "block"
    # the LEGACY spelling blocks under the CANONICAL name -> the alias map was applied before match.
    assert "fleet-admin: cronjob_manage is orchestrator-only" in _msg(legacy)
    assert "fleet-admin: cronjob_manage is orchestrator-only" in _msg(canonical)


def test_ac_gov_f22_3(tmp_path, monkeypatch):
    """AC-GOV-F22-3: A predicate that raises yields a block directive (fail-closed), never a
    passthrough."""
    manager = _load(tmp_path, monkeypatch, profile="worker-a")
    loaded = _loaded(manager)
    # positive control: an ungated WIRED edge lets message_agent pass, proving the gate does not
    # block it for an unrelated reason (so the block below is caused by the raising predicate).
    monkeypatch.setattr(loaded.module, "_edges_lookup",
                        lambda *a, **k: {"from": "worker-a", "to": "orch", "gated": False},
                        raising=True)
    assert _gate(manager, "message_agent", {"target": "orch", "message": "hi"}) is None
    # same predicate now raises a BaseException-only type -> the gate's outer `except BaseException`
    # fails CLOSED with block. Using a non-Exception BaseException (not RuntimeError) pins the
    # breadth the criterion names: an `except Exception` refactor would let this escape = fail-open.
    monkeypatch.setattr(loaded.module, "_edges_lookup",
                        lambda *a, **k: (_ for _ in ()).throw(_PredicateCrash("boom")),
                        raising=True)
    d = _gate(manager, "message_agent", {"target": "orch", "message": "hi"})
    assert _act(d) == "block"
    assert "internal error" in _msg(d)


def test_ac_gov_f22_4(tmp_path, monkeypatch):
    """AC-GOV-F22-4 (derived): On a partial/still-initializing registry (MORE THAN ONE restricted-
    core group absent) the plugin stays enabled (removing the veto would be fail-open) and the
    gate-time backstop blocks every gated call fail-closed until restricted-set integrity is
    restored. Exercises both the boundary of "more than one" (missing=2) and the extreme, so a
    one-line bump of _MAX_ABSENT_WHEN_COMPLETE (1->2) — which would fail-LOAD the missing=2 shape
    (disable the veto = fail-open) — turns this node red."""
    import model_tools
    model_tools.discover_builtin_tools()
    real = registry.get_all_tool_names  # captured before patching (returns the true full registry)

    # boundary: EXACTLY two restricted-core groups absent (drop terminal + execute_code, keep the
    # benign read_file for the backstop). missing=2 is just past _MAX_ABSENT_WHEN_COMPLETE=1, so it
    # must NOT fail the load; a threshold bump to >=2 would fail-load here (disable = fail-open).
    monkeypatch.setattr(registry, "get_all_tool_names",
                        lambda *a, **k: [n for n in real() if n not in ("terminal", "execute_code")])
    mb = _load(tmp_path, monkeypatch, profile="w3")
    assert mb._plugins[PLUGIN_KEY].enabled is True
    assert mb._plugins[PLUGIN_KEY].error is None
    assert "pre_tool_call" in mb._plugins[PLUGIN_KEY].hooks_registered
    db = _gate(mb, "read_file", {})
    assert _act(db) == "block"
    assert "restricted core tools absent at gate time" in _msg(db)

    # extreme: a deliberately highly partial registry (one restricted group present — the profile-
    # switch reload shape) must NOT fail the load: disabling the plugin would remove the veto.
    monkeypatch.setattr(registry, "get_all_tool_names", lambda *a, **k: ["skill_manage"])
    m = _load(tmp_path, monkeypatch, profile="w4")
    assert m._plugins[PLUGIN_KEY].enabled is True
    assert m._plugins[PLUGIN_KEY].error is None
    assert "pre_tool_call" in m._plugins[PLUGIN_KEY].hooks_registered
    # a benign, otherwise-allowed tool (read_file) isolates the backstop: the only reason it can
    # block is restricted-set integrity, and the message pins it to the backstop (not _wiring_block).
    d = _gate(m, "read_file", {})
    assert _act(d) == "block"
    assert "restricted core tools absent at gate time" in _msg(d)
