"""Acceptance test for SELF-F54 — self-modification tier 1 and agent-spawned agents.

Proves the covering surface for SELF-F54 against a tree that has the fleet
carrier merged: the orchestrator-gated create_agent tool over create_profile
(check_fn schema omission and a single profiles.create dispatch), the runtime
nv-fleet-gates veto that refuses a worker's create_agent/onboard_coworker, and
the stock tier-1 self-modification safety gates (protected-instruction writes
held by default, opt-in memory write-approval staging). Each criterion carries an
in-test differential control so a plausible regression flips the assertion.
"""

from pathlib import Path
import shutil

import pytest
import yaml

from hermes_cli.plugins import PluginManager

# --- Agent-spawned agents (nv-coworker-compose): AC-SELF-F54-1 / -2 -----------

COMPOSE_KEY = "nv-coworker-compose"
COMPOSE_SRC = Path(__file__).resolve().parents[2] / "plugins" / COMPOSE_KEY
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "loop-f35"
FIXTURE_SPEC = FIXTURE_DIR / "coworker-types.yaml"
EXPECTED_FILE = FIXTURE_DIR / "expected.yaml"


def _expected():
    return yaml.safe_load(EXPECTED_FILE.read_text(encoding="utf-8"))


def _compose_write_home(tmp_path, monkeypatch, profile_name=None, config=None):
    root = tmp_path / "hermes-root"
    home = root / "profiles" / profile_name if profile_name else root / "hermes-home"
    plugins_dir = home / "plugins"
    plugins_dir.mkdir(parents=True)
    shutil.copytree(COMPOSE_SRC, plugins_dir / COMPOSE_KEY)
    (home / "config.yaml").write_text(
        yaml.safe_dump(config if config is not None else {"plugins": {"enabled": [COMPOSE_KEY]}}),
        encoding="utf-8",
    )
    empty_bundled = tmp_path / "bundled-plugins"
    empty_bundled.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(empty_bundled))
    return home


def _compose_load(tmp_path, monkeypatch, profile_name=None, config=None):
    _compose_write_home(tmp_path, monkeypatch, profile_name=profile_name, config=config)
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[COMPOSE_KEY]
    assert loaded.enabled is True
    assert loaded.error is None
    assert loaded.module is not None
    return manager, loaded


def _compose_render(loaded, out_root):
    return loaded.module.compose(str(FIXTURE_SPEC), str(out_root))


def _compose_config(rendered_dir):
    return yaml.safe_load((Path(rendered_dir) / "config.yaml").read_text(encoding="utf-8"))


def test_ac_self_f54_1(tmp_path, monkeypatch):
    """AC-SELF-F54-1: Each orchestrator-gated gateway tool's check_fn returns True only for the orchestrator profile, present in the orchestrator's tool schema, absent from a worker profile's schema"""
    from tools.registry import registry

    exp = _expected()
    tools = exp["tools_orchestrator_only"]

    _, seed = _compose_load(tmp_path / "seed", monkeypatch)
    rendered = _compose_render(seed, tmp_path / "rendered")

    def _load_role(role):
        cfg = _compose_config(rendered[role])
        assert COMPOSE_KEY in (cfg.get("plugins", {}).get("enabled") or []), (
            f"{role}: rendered config does not enable {COMPOSE_KEY} — its onboard tools would be absent"
        )
        mgr, _ = _compose_load(tmp_path / f"run-{role}", monkeypatch, profile_name=role, config=cfg)
        return mgr

    orch_mgr = _load_role(exp["orchestrator_profile"])
    orch_names = {d["function"]["name"] for d in registry.get_definitions(set(tools))}
    for tool in tools:
        entry = registry.get_entry(tool, scope=orch_mgr.scope_key)
        assert entry is not None and entry.check_fn is not None, f"{tool} missing check_fn"
        assert entry.check_fn() is True, f"{tool} check_fn False for orchestrator"
        assert tool in orch_names, f"{tool} absent from orchestrator schema"

    worker_mgr = _load_role(exp["worker_profile"])
    worker_names = {d["function"]["name"] for d in registry.get_definitions(set(tools))}
    for tool in tools:
        entry = registry.get_entry(tool, scope=worker_mgr.scope_key)
        assert entry is not None and entry.check_fn is not None
        assert entry.check_fn() is False, f"{tool} check_fn True for a worker profile"
        assert tool not in worker_names, f"{tool} present in a worker's schema"


def test_ac_self_f54_2(tmp_path, monkeypatch):
    """AC-SELF-F54-2: The create_agent tool handler dispatches a single profiles.create call carrying the requested profile fields"""
    from tools.registry import registry

    manager, loaded = _compose_load(tmp_path, monkeypatch)
    exp = _expected()
    calls = []
    monkeypatch.setattr(
        loaded.module, "_dispatch_rpc",
        lambda method, params: calls.append((method, dict(params))) or {"ok": True},
        raising=True,
    )
    entry = registry.get_entry("create_agent", scope=manager.scope_key)
    entry.handler(dict(exp["create_agent_fields"]))
    creates = [c for c in calls if c[0] == "profiles.create"]
    assert len(creates) == 1, f"expected exactly one profiles.create dispatch, got {len(creates)}"
    for key, val in exp["create_agent_fields"].items():
        assert creates[0][1].get(key) == val, f"profiles.create missing {key}={val!r}"


# --- Runtime veto (nv-fleet-gates): AC-SELF-F54-3 -----------------------------

FLEET_KEY = "nv-fleet-gates"
FLEET_SRC = Path(__file__).resolve().parents[2] / "plugins" / FLEET_KEY


def _fleet_base_settings(edges_db, **overrides):
    settings = {
        "profile_roles": {"orch": "orchestrator", "worker-a": "worker"},
        "enforce_sandbox": False,
        "expected_backend": "docker",
        "required_stages": ["OUTPUT_REVIEW"],
        "plan_gate": True,
        "edges_db_path": str(edges_db),
        "stage_markers": ["[Fix Report]", "[Review Verdict]"],
        "admin_tools": ["onboard_coworker", "onboard_project", "create_agent",
                        "record_decision", "record_human_verdict"],
    }
    settings.update(overrides)
    return settings


def _fleet_prime_registry(monkeypatch, hermes_home, bundled):
    monkeypatch.setenv("HOME", str(hermes_home.parent.parent / "fakehome"))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.delenv("HERMES_ENABLE_PROJECT_PLUGINS", raising=False)
    # importing tools.registry alone does NOT register the built-ins; the gate's
    # restricted-set backstop reads registry.get_all_tool_names(), so prime it.
    import model_tools

    model_tools.discover_builtin_tools()


def _fleet_load(tmp_path, monkeypatch, *, profile="worker-a", edges_db=None, **settings_overrides):
    hermes_home = tmp_path / "home" / profile
    (hermes_home / "plugins").mkdir(parents=True)
    bundled = tmp_path / "empty-bundled"
    bundled.mkdir(exist_ok=True)
    edges_db = edges_db or (tmp_path / "fleet" / "edges.db")
    edges_db.parent.mkdir(parents=True, exist_ok=True)

    if not FLEET_SRC.exists():
        pytest.fail(f"plugin source missing: {FLEET_SRC}")

    shutil.copytree(FLEET_SRC, hermes_home / "plugins" / FLEET_KEY)

    settings = _fleet_base_settings(edges_db, **settings_overrides)
    cfg = {"plugins": {"enabled": [FLEET_KEY],
                       "entries": {FLEET_KEY: {"settings": settings}}}}
    (hermes_home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")

    _fleet_prime_registry(monkeypatch, hermes_home, bundled)
    manager = PluginManager()
    manager.discover_and_load()
    return manager, hermes_home, edges_db


def _fleet_loaded(manager):
    loaded = manager._plugins[FLEET_KEY]
    assert loaded.enabled is True
    assert loaded.error is None
    assert loaded.module is not None
    assert "pre_tool_call" in loaded.hooks_registered
    return loaded


def _fleet_gate(manager, tool_name, args=None, session_id="sess-1"):
    results = manager.invoke_hook(
        "pre_tool_call", tool_name=tool_name, args=args or {}, session_id=session_id,
        task_id="task-1", tool_call_id="tc-1", turn_id="turn-1")
    directives = [r for r in (results or []) if isinstance(r, dict) and r.get("action")]
    return directives[0] if directives else None


def _fleet_act(directive):
    return directive.get("action") if isinstance(directive, dict) else None


def test_ac_self_f54_3(tmp_path, monkeypatch):
    """AC-SELF-F54-3: A worker profile's create_agent or onboard_coworker call is refused at runtime by the nv-fleet-gates pre_tool_call veto with action block, while the orchestrator profile's identical call is not blocked"""
    # The spawn/onboard names must be in settings.admin_tools (they are, per
    # _fleet_base_settings) for the fleet-admin predicate to gate them.
    spawn_tools = ("create_agent", "onboard_coworker")

    worker, _, edges = _fleet_load(tmp_path, monkeypatch, profile="worker-a")
    _fleet_loaded(worker)
    # A benign tool is NOT blocked, so a block below is the fleet-admin rule, not
    # a blanket "block every worker tool" regression.
    assert _fleet_gate(worker, "read_file", {"path": "x"}) is None
    for tool in spawn_tools:
        directive = _fleet_gate(worker, tool)
        assert _fleet_act(directive) == "block", f"{tool} not blocked for a worker"
        assert f"fleet-admin: {tool} is orchestrator-only" in directive.get("message", ""), (
            f"{tool} blocked for the wrong reason: {directive!r}"
        )

    orch, _, _ = _fleet_load(tmp_path, monkeypatch, profile="orch", edges_db=edges)
    _fleet_loaded(orch)
    for tool in spawn_tools:
        assert _fleet_act(_fleet_gate(orch, tool)) != "block", f"{tool} blocked for the orchestrator"


# --- Stock self-mod tier-1 safety gates: AC-SELF-F54-4 / -5 -------------------
# HERMES_HOME is sandboxed to tmp_path/hermes_test by the autouse conftest
# fixture, so tmp_path itself is OUTSIDE HERMES_HOME (the ~/.hermes tree is
# exempt from the protected-instruction gate — tools/file_tools.py:817).


@pytest.mark.parametrize("protected_name", ["AGENTS.md", "CLAUDE.md", "SOUL.md", ".cursorrules"])
def test_ac_self_f54_4(tmp_path, protected_name):
    """AC-SELF-F54-4: A write via the file-write tool to a protected instruction file basename (SOUL.md, AGENTS.md, CLAUDE.md, .cursorrules) in a project-local directory is held closed by default with the file never written when no approver is present, while a write to a non-protected file in the same directory succeeds"""
    import json

    import tools.file_tools as ft
    from tools.file_tools import write_file_tool

    enabled, _ = ft._protected_instruction_config()
    assert enabled is True, "protected-instruction gate not default-on"

    protected = tmp_path / protected_name
    res = json.loads(write_file_tool(str(protected), "injected persona"))
    # No approval callback registered in a fresh process, so the held write cannot
    # be resolved and fails closed rather than proceeding.
    assert res.get("error") and "BLOCKED" in res["error"], res
    assert not protected.exists(), f"{protected_name} was written despite the hold"

    # A non-protected file in the same directory writes, so the block above is the
    # protected-instruction rule and not a catch-all refusal.
    ok = tmp_path / "notes.txt"
    res_ok = json.loads(write_file_tool(str(ok), "ordinary note"))
    assert not res_ok.get("error"), res_ok
    assert ok.exists() and ok.read_text(encoding="utf-8") == "ordinary note"


def _set_write_approval(subsystem, enabled):
    import hermes_cli.config as cfg

    c = cfg.load_config()
    c.setdefault(subsystem, {})["write_approval"] = enabled
    cfg.save_config(c)  # save pops the config cache; the next load re-reads


def test_ac_self_f54_5(tmp_path):
    """AC-SELF-F54-5: With the memory write-approval setting enabled a memory self-edit is staged as a pending approval record rather than applied, while with the setting at its default-off value the same edit is applied directly"""
    import json

    from tools.memory_tool import memory_tool, MemoryStore
    from tools import write_approval as wa

    applied_text = "default-off memory edit applied to the store"
    staged_text = "enabled memory edit held for approval"

    def _has(store, text):
        return any(text in entry for entry in store.memory_entries)

    assert wa.write_approval_enabled("memory") is False, "memory write-approval not off by default"
    store = MemoryStore()
    store.load_from_disk()
    applied = json.loads(memory_tool("add", "memory", applied_text, store=store))
    assert applied.get("success") is True and applied.get("pending_id") is None, applied
    assert _has(store, applied_text), "an off-gate edit was not applied to the store"
    assert wa.pending_count("memory") == 0, "an edit was staged while the gate was off"
    reloaded = MemoryStore()
    reloaded.load_from_disk()
    assert _has(reloaded, applied_text), "the applied edit did not persist to disk"

    _set_write_approval("memory", True)
    assert wa.write_approval_enabled("memory") is True
    staging = MemoryStore()
    staging.load_from_disk()
    staged = json.loads(memory_tool("add", "memory", staged_text, store=staging))
    assert staged.get("staged") is True and staged.get("pending_id"), staged
    assert wa.pending_count("memory") == 1, "the edit was not staged while the gate was on"
    assert not _has(staging, staged_text), "a staged edit mutated the live store"
    after = MemoryStore()
    after.load_from_disk()
    assert not _has(after, staged_text), "a staged edit reached disk instead of being held"
