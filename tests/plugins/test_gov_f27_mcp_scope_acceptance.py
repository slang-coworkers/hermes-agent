"""Acceptance test for GOV-F27 — per-role MCP allow-list / server registry.

One ``test_ac_gov_f27_<n>`` per ``pytest:`` acceptance criterion in the ADR
(/workspace/agent/reports/gov-f27.md §Acceptance criteria). AC-GOV-F27-9 is
``ui:`` and has no function here.

Render ACs read the RAW rendered ``config.yaml`` (never ``load_config()``, which
deep-merges DEFAULT_CONFIG). Veto ACs mirror the merged nv-fleet-gates
acceptance harness: they prime the core registry (``model_tools.discover_builtin_tools``,
required by the gate's load-time restricted-set assertion), supply the full gate
settings block, and load ``nv-fleet-gates`` from a HERMES_HOME whose directory is
the profile name (``_current_profile()`` is ``Path(get_hermes_home()).name``).
Expected values are hardcoded here, never read from the plugin. Compose input
specs are generated in-test into a copy of the shipped composable supporting tree
(spines/skills/workflows/overlays).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

from hermes_cli.config import migrate_config
from hermes_cli.config_migrations import SUPPORT_FLOOR_VERSION
from hermes_cli.plugins import PluginManager

COMPOSE_KEY = "nv-coworker-compose"
GATES_KEY = "nv-fleet-gates"
PLUGINS_ROOT = Path(__file__).resolve().parents[2] / "plugins"
FIXTURE_TREE = (
    Path(__file__).parent / "fixtures" / "gov-f27"
)  # spines/skills/workflows/overlays only

DOCS_RO_URL = "https://onecli.local/mcp/docs"
REPO_RW_URL = "https://onecli.local/mcp/repo"
SHARED_URL = "https://onecli.local/mcp/shared"
SHARED_FULL_URL = "https://onecli.local/mcp/shared-full"
GATES_SETTINGS = "plugins.entries.nv-fleet-gates.settings"


def _stanza(url, include):
    return {
        "url": url,
        "tools": {"include": include, "resources": False, "prompts": False},
        "trust": "untrusted",
    }


REVIEWER_SERVERS = {"docs-ro": _stanza(DOCS_RO_URL, ["search_docs"])}
FIXER_SERVERS = {"repo-rw": _stanza(REPO_RW_URL, ["create_issue", "create_pr"])}
REVIEWER_SCOPE = {"mcp__docs_ro__search_docs": "remote"}
FIXER_SCOPE = {
    "mcp__repo_rw__create_issue": "remote",
    "mcp__repo_rw__create_pr": "remote",
}


# --------------------------------------------------------------------------- #
# Spec generation (in-test; copies the shipped composable supporting tree)
# --------------------------------------------------------------------------- #
def _base_spec() -> dict:
    return {
        "project": "demo",
        "default_profile": "default",
        "orchestrator_profile": "orchestrator",
        "worker_profile": "reviewer",
        "spines": {"base": {"source": "spines/base.yaml"}},
        "skills_root": "skills",
        "workflows_root": "workflows",
        "overlays_root": "overlays",
        "rooms": {
            "fleet-room": {
                "name": "Fleet",
                "members": ["orchestrator", "reviewer", "fixer"],
            }
        },
        "default_config": {
            "gateway": {
                "multiplex_profiles": True,
                "multiplex_profile_allowlist": ["orchestrator", "reviewer", "fixer"],
            }
        },
        "types": {
            "orchestrator": {
                "extends": ["base"],
                "identity": "ORCH-IDENTITY",
                "ui_meta": {
                    "title": "Orchestrator",
                    "description": "Fleet orchestrator",
                    "shape": "hexagon",
                },
            },
            "reviewer": {
                "extends": ["base"],
                "identity": "REVIEWER-IDENTITY",
                "skills": ["review-skill"],
                "overlays": ["critique-gate"],
                "ui_meta": {
                    "title": "Reviewer",
                    "description": "PR reviewer",
                    "shape": "diamond",
                },
            },
            "fixer": {
                "extends": ["base"],
                "identity": "FIXER-IDENTITY",
                "skills": ["fix-skill"],
                "ui_meta": {
                    "title": "Fixer",
                    "description": "Implementer",
                    "shape": "square",
                },
            },
        },
    }


def _write_spec(
    dest: Path, *, reviewer_mcp=None, fixer_mcp=None, stale_scope=None
) -> Path:
    shutil.copytree(FIXTURE_TREE, dest)
    spec = _base_spec()
    if reviewer_mcp is not None:
        spec["types"]["reviewer"]["mcp"] = reviewer_mcp
    if fixer_mcp is not None:
        spec["types"]["fixer"]["mcp"] = fixer_mcp
    (dest / "coworker-types.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")
    if stale_scope is not None:
        base = dest / "spines" / "base.yaml"
        b = yaml.safe_load(base.read_text(encoding="utf-8")) or {}
        (
            b.setdefault("config", {})
            .setdefault("plugins", {})
            .setdefault("entries", {})
            .setdefault("nv-fleet-gates", {})
            .setdefault("settings", {})
        )["mcp_scope"] = stale_scope
        base.write_text(yaml.safe_dump(b), encoding="utf-8")
    return dest / "coworker-types.yaml"


# --------------------------------------------------------------------------- #
# Compose helpers
# --------------------------------------------------------------------------- #
def _compose_module(tmp_path, monkeypatch, name="compose-home"):
    home = tmp_path / name
    (home / "plugins").mkdir(parents=True)
    shutil.copytree(PLUGINS_ROOT / COMPOSE_KEY, home / "plugins" / COMPOSE_KEY)
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": [COMPOSE_KEY]}}), encoding="utf-8"
    )
    bundled = tmp_path / f"bundled-{name}"
    bundled.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    monkeypatch.delenv("HERMES_ENABLE_PROJECT_PLUGINS", raising=False)
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[COMPOSE_KEY]
    assert loaded.enabled is True, getattr(loaded, "error", None)
    assert loaded.error is None and loaded.module is not None
    return loaded.module


def _render(module, spec: Path, out_root: Path) -> dict:
    out_root.mkdir(parents=True, exist_ok=True)
    return module.compose(str(spec), str(out_root))


def _raw(profile_dir) -> dict:
    return (
        yaml.safe_load((Path(profile_dir) / "config.yaml").read_text(encoding="utf-8"))
        or {}
    )


def _dig(mapping: dict, dotted: str):
    node = mapping
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _default_dir(rendered: dict) -> str:
    for pdir in rendered.values():
        found, val = _dig(_raw(pdir), "gateway.multiplex_profiles")
        if found and val is True:
            return pdir
    raise AssertionError("no DEFAULT/multiplexer profile rendered")


def _servers(rendered, profile) -> dict:
    return _raw(rendered[profile]).get("mcp_servers") or {}


def _scope_of(rendered, profile) -> dict:
    found, scope = _dig(_raw(rendered[profile]), GATES_SETTINGS + ".mcp_scope")
    assert found and isinstance(scope, dict), (
        f"{profile}: mcp_scope must be rendered here"
    )
    return scope


def _err(module):
    return getattr(module, "CompositionError", Exception)


def _assert_scope(scope: dict):
    """reviewer/fixer exact; orchestrator empty; any other profile empty (no stale/leak)."""
    assert scope.get("reviewer") == REVIEWER_SCOPE, scope
    assert scope.get("fixer") == FIXER_SCOPE, scope
    assert scope.get("orchestrator") == {}, scope
    for prof, tools in scope.items():
        if prof not in ("reviewer", "fixer"):
            assert tools == {}, (
                f"{prof}: non-role profile must have empty scope, got {tools}"
            )


# --------------------------------------------------------------------------- #
# Veto harness (mirrors tests/plugins/test_nv_fleet_gates_acceptance.py)
# --------------------------------------------------------------------------- #
def _gate_settings(
    mcp_scope, tmp_path, *, enforce_sandbox=False, profile_roles=None
) -> dict:
    return {
        "role": "worker",
        "profile_roles": profile_roles or {},
        "enforce_sandbox": enforce_sandbox,
        "expected_backend": "docker",
        "required_stages": ["OUTPUT_REVIEW"],
        "plan_gate": False,  # off so an mcp_* call reaches the MCP predicate, not the plan gate
        "edges_db_path": str(tmp_path / "fleet" / "edges.db"),
        "stage_markers": ["[Fix Report]"],
        "admin_tools": ["onboard_coworker", "onboard_project", "create_agent"],
        "skills_root": str(tmp_path / "skills"),
        "shared_learnings_path": str(tmp_path / "shared-learnings"),
        "mcp_scope": mcp_scope,
    }


def _load_gate(tmp_path, monkeypatch, profile: str, settings: dict):
    home = tmp_path / "home" / profile  # _current_profile() == profile
    (home / "plugins").mkdir(parents=True)
    shutil.copytree(PLUGINS_ROOT / GATES_KEY, home / "plugins" / GATES_KEY)
    bundled = tmp_path / "empty-bundled"
    bundled.mkdir(exist_ok=True)
    Path(settings["edges_db_path"]).parent.mkdir(parents=True, exist_ok=True)
    settings = dict(settings, profile=profile)
    cfg = {
        "plugins": {
            "enabled": [GATES_KEY],
            "entries": {GATES_KEY: {"settings": settings}},
        }
    }
    (home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.delenv("HERMES_ENABLE_PROJECT_PLUGINS", raising=False)
    import model_tools

    model_tools.discover_builtin_tools()  # the gate's load-time restricted-set assertion reads the registry
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[GATES_KEY]
    assert loaded.enabled is True, getattr(loaded, "error", None)
    return manager, loaded


def _gate(manager, tool_name: str):
    results = manager.invoke_hook(
        "pre_tool_call",
        tool_name=tool_name,
        args={},
        session_id="sess-1",
        task_id="task-1",
        tool_call_id="tc-1",
        turn_id="turn-1",
    )
    return [r for r in (results or []) if isinstance(r, dict) and r.get("action")]


def _blocked(results) -> bool:
    return any(r.get("action") == "block" for r in results)


# --------------------------------------------------------------------------- #
# Render ACs
# --------------------------------------------------------------------------- #
def test_ac_gov_f27_1(tmp_path, monkeypatch):
    """Each coworker's config.yaml carries mcp_servers scoped to exactly its role's complete stanzas; reviewer != fixer; a no-MCP role has none."""
    module = _compose_module(tmp_path, monkeypatch)
    spec = _write_spec(
        tmp_path / "spec",
        reviewer_mcp={"docs-ro": {"url": DOCS_RO_URL, "include": ["search_docs"]}},
        fixer_mcp={
            "repo-rw": {"url": REPO_RW_URL, "include": ["create_issue", "create_pr"]}
        },
    )
    rendered = _render(module, spec, tmp_path / "out")
    assert _servers(rendered, "reviewer") == REVIEWER_SERVERS
    assert _servers(rendered, "fixer") == FIXER_SERVERS
    assert _servers(rendered, "orchestrator") == {}, "no-MCP role gets no servers"
    assert set(_raw(_default_dir(rendered)).get("mcp_servers") or {}) == {
        "docs-ro",
        "repo-rw",
    }


def test_ac_gov_f27_2(tmp_path, monkeypatch):
    """Non-full servers render trust: untrusted (override an unsafe input, insert when absent) + resources/prompts false; explicit trust: full preserved."""
    module = _compose_module(tmp_path, monkeypatch, "h-unsafe")
    spec = _write_spec(
        tmp_path / "spec-unsafe",
        reviewer_mcp={
            "docs-ro": {
                "url": DOCS_RO_URL,
                "include": ["search_docs"],
                "trust": "trusted",
            },
            "trusted-svc": {
                "url": SHARED_FULL_URL,
                "include": ["ping"],
                "trust": "full",
            },
        },
        fixer_mcp={
            "repo-rw": {
                "url": REPO_RW_URL,
                "include": ["create_issue"],
                "trust": "trusted",
            }
        },
    )
    rendered = _render(module, spec, tmp_path / "out-unsafe")
    saw_untrusted = saw_full = False
    for pdir in rendered.values():
        for name, srv in (_raw(pdir).get("mcp_servers") or {}).items():
            if name == "trusted-svc":
                assert srv.get("trust") == "full", "explicit trust: full preserved"
                saw_full = True
            else:
                assert srv.get("trust") == "untrusted", (
                    f"{name}: unsafe input overridden to untrusted"
                )
                saw_untrusted = True
            assert srv.get("tools", {}).get("resources") is False
            assert srv.get("tools", {}).get("prompts") is False
    assert saw_untrusted and saw_full, (
        "the unsafe variant must exercise both the override and the preserve branch"
    )

    module2 = _compose_module(tmp_path, monkeypatch, "h-omitted")
    spec2 = _write_spec(
        tmp_path / "spec-omitted",
        reviewer_mcp={"docs-ro": {"url": DOCS_RO_URL, "include": ["search_docs"]}},
    )
    rendered2 = _render(module2, spec2, tmp_path / "out-omitted")
    saw = False
    for pdir in rendered2.values():
        for name, srv in (_raw(pdir).get("mcp_servers") or {}).items():
            saw = True
            assert srv.get("trust") == "untrusted", (
                f"{name}: absent trust inserted as untrusted"
            )
            assert srv.get("tools", {}).get("resources") is False
            assert srv.get("tools", {}).get("prompts") is False
    assert saw, "omitted variant must still render mcp_servers (INSERT path)"


def test_ac_gov_f27_3(tmp_path, monkeypatch):
    """A shared server renders the DEFAULT union (include union; resources/prompts OR; trust full only if EVERY declaration is full) while each coworker keeps its exact subset; granted utilities appear in that role's scope."""
    module = _compose_module(tmp_path, monkeypatch)
    spec = _write_spec(
        tmp_path / "spec-shared",
        reviewer_mcp={
            "shared-ro": {
                "url": SHARED_URL,
                "include": ["read_a"],
                "resources": True,
                "trust": "full",
            },
            "shared-full": {"url": SHARED_FULL_URL, "include": ["a"], "trust": "full"},
        },
        fixer_mcp={
            "shared-ro": {
                "url": SHARED_URL,
                "include": ["read_b"],
                "prompts": True,
            },  # no trust -> not all full
            "shared-full": {"url": SHARED_FULL_URL, "include": ["b"], "trust": "full"},
        },
    )
    rendered = _render(module, spec, tmp_path / "out")

    default = _raw(_default_dir(rendered))["mcp_servers"]
    assert set(default) == {"shared-ro", "shared-full"}, (
        "DEFAULT holds exactly the union of role servers"
    )
    assert sorted(default["shared-ro"]["tools"]["include"]) == ["read_a", "read_b"]
    assert (
        default["shared-ro"]["tools"]["resources"] is True
        and default["shared-ro"]["tools"]["prompts"] is True
    )
    assert default["shared-ro"]["trust"] == "untrusted", (
        "mixed trust (not every declaration full) -> untrusted"
    )
    assert sorted(default["shared-full"]["tools"]["include"]) == ["a", "b"]
    assert default["shared-full"]["trust"] == "full", "every declaration full -> full"

    assert _servers(rendered, "reviewer")["shared-ro"]["tools"]["include"] == ["read_a"]
    assert _servers(rendered, "fixer")["shared-ro"]["tools"]["include"] == ["read_b"]

    rev_scope = _scope_of(rendered, "reviewer").get("reviewer", {})
    fix_scope = _scope_of(rendered, "fixer").get("fixer", {})
    assert rev_scope.get("mcp__shared_ro__list_resources") == "remote"
    assert rev_scope.get("mcp__shared_ro__read_resource") == "remote"
    assert fix_scope.get("mcp__shared_ro__list_prompts") == "remote"
    assert fix_scope.get("mcp__shared_ro__get_prompt") == "remote"


def test_ac_gov_f27_4(tmp_path, monkeypatch):
    """Render writes the exact profile-keyed mcp_scope into EVERY profile (stale entries replaced, siblings preserved); fails closed on the invalid-spec cases."""
    module = _compose_module(tmp_path, monkeypatch)
    spec = _write_spec(
        tmp_path / "spec",
        reviewer_mcp={"docs-ro": {"url": DOCS_RO_URL, "include": ["search_docs"]}},
        fixer_mcp={
            "repo-rw": {"url": REPO_RW_URL, "include": ["create_issue", "create_pr"]}
        },
        stale_scope={
            "reviewer": {"mcp__stale__evil": "remote"}
        },  # must be atomically replaced, not merged
    )
    rendered = _render(module, spec, tmp_path / "out")

    scopes = []
    for pdir in rendered.values():
        found, settings = _dig(_raw(pdir), GATES_SETTINGS)
        assert found and isinstance(settings, dict) and "mcp_scope" in settings, (
            f"{pdir}: mcp_scope in every profile"
        )
        _assert_scope(settings["mcp_scope"])
        assert settings.get("enabled") is True, (
            "sibling settings (enabled) preserved (deep-merge, not clobbered)"
        )
        scopes.append(settings["mcp_scope"])
    assert all(s == scopes[0] for s in scopes), (
        "the same complete map is copied into every profile"
    )
    assert set(scopes[0]) == set(rendered), (
        "mcp_scope keys cover exactly the rendered profiles (incl. DEFAULT)"
    )

    err = _err(module)
    cases = [
        (
            "collision",
            {
                "docs-ro": {"url": DOCS_RO_URL, "include": ["search_docs"]},
                "docs_ro": {"url": DOCS_RO_URL, "include": ["search_docs"]},
            },
            None,
            r"collision|sanitiz",
        ),
        (
            "badvar",
            {
                "docs-ro": {
                    "url": DOCS_RO_URL,
                    "headers": {"Authorization": "${REPO_TOKEN}"},
                    "include": ["x"],
                }
            },
            None,
            r"\$\{",
        ),
        (
            "badenvvar",
            {
                "docs-ro": {
                    "url": DOCS_RO_URL,
                    "headers": {"Authorization": "${env:REPO_TOKEN}"},
                    "include": ["x"],
                }
            },
            None,
            r"env:",
        ),
        (
            "glob",
            {"docs-ro": {"url": DOCS_RO_URL, "include": ["search_*"]}},
            None,
            r"glob|include|exact",
        ),
        ("neither", {"bad": {"include": ["x"]}}, None, r"transport|url|command"),
        (
            "both",
            {"bad": {"url": DOCS_RO_URL, "command": "srv", "include": ["x"]}},
            None,
            r"transport|url|command",
        ),
        (
            "mismatch",
            {"bad": {"url": DOCS_RO_URL, "transport": "stdio", "include": ["x"]}},
            None,
            r"transport",
        ),
        (
            "conflict",
            {"shared-ro": {"url": SHARED_URL, "include": ["read_a"]}},
            {
                "shared-ro": {
                    "url": "https://onecli.local/mcp/OTHER",
                    "include": ["read_b"],
                }
            },
            r"connection|conflict|identity",
        ),
    ]
    for tag, rev, fix, why in cases:
        spec_bad = _write_spec(
            tmp_path / f"spec-{tag}", reviewer_mcp=rev, fixer_mcp=fix
        )
        with pytest.raises(err, match=why):
            module.compose(str(spec_bad), str(tmp_path / f"out-{tag}"))


def test_ac_gov_f27_8(tmp_path, monkeypatch):
    """Rendered mcp_servers and the mcp_scope map survive `hermes config migrate` value-equivalent (a no-MCP role has no mcp_servers, and that absence is preserved)."""
    module = _compose_module(tmp_path, monkeypatch)
    spec = _write_spec(
        tmp_path / "spec",
        reviewer_mcp={"docs-ro": {"url": DOCS_RO_URL, "include": ["search_docs"]}},
        fixer_mcp={"repo-rw": {"url": REPO_RW_URL, "include": ["create_issue"]}},
    )
    rendered = _render(module, spec, tmp_path / "out")
    for pdir in rendered.values():
        cfg_path = Path(pdir) / "config.yaml"
        before = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        before_servers = before.get("mcp_servers")  # may be absent for a no-MCP role
        _, before_scope = _dig(before, GATES_SETTINGS + ".mcp_scope")
        assert isinstance(before_scope, dict) and before_scope, (
            f"{pdir}: precondition — mcp_scope present+non-empty"
        )
        staged = dict(before)
        staged["_config_version"] = SUPPORT_FLOOR_VERSION
        cfg_path.write_text(yaml.safe_dump(staged), encoding="utf-8")
        monkeypatch.setenv("HERMES_HOME", str(pdir))
        migrate_config(interactive=False, quiet=True)
        after = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        assert after.get("mcp_servers") == before_servers, (
            f"{pdir}: mcp_servers value-equivalent (incl. absence)"
        )
        _, after_scope = _dig(after, GATES_SETTINGS + ".mcp_scope")
        assert after_scope == before_scope, f"{pdir}: mcp_scope survives migrate"


# --------------------------------------------------------------------------- #
# Enforcement ACs (nv-fleet-gates single pre_tool_call gate)
# --------------------------------------------------------------------------- #
def test_ac_gov_f27_5(tmp_path, monkeypatch):
    """The single gate, reading the RENDERED reviewer profile's mcp_scope, blocks a tool outside scope (and an unknown profile), permits (proceeds on) an allow-listed remote one; one pre_tool_call registered."""
    module = _compose_module(tmp_path, monkeypatch, "compose-home")
    spec = _write_spec(
        tmp_path / "spec",
        reviewer_mcp={"docs-ro": {"url": DOCS_RO_URL, "include": ["search_docs"]}},
        fixer_mcp={
            "repo-rw": {"url": REPO_RW_URL, "include": ["create_issue", "create_pr"]}
        },
    )
    rendered = _render(module, spec, tmp_path / "out")
    rendered_scope = _scope_of(rendered, "reviewer")
    roles = {p: "worker" for p in rendered_scope}
    roles["ghost-profile"] = "worker"

    manager, _ = _load_gate(
        tmp_path,
        monkeypatch,
        "reviewer",
        _gate_settings(rendered_scope, tmp_path, profile_roles=roles),
    )
    assert len(manager._hooks.get("pre_tool_call", [])) == 1, (
        "exactly one pre_tool_call (baseline rule 9)"
    )
    assert _gate(manager, "mcp__docs_ro__search_docs") == [], (
        "allow-listed remote tool proceeds (no directive)"
    )
    assert _blocked(_gate(manager, "mcp__repo_rw__create_issue")), (
        "tool outside this profile's scope blocked"
    )

    manager2, _ = _load_gate(
        tmp_path / "g2",
        monkeypatch,
        "ghost-profile",
        _gate_settings(rendered_scope, tmp_path / "g2", profile_roles=roles),
    )
    assert _blocked(_gate(manager2, "mcp__docs_ro__search_docs")), (
        "profile absent from mcp_scope fails closed"
    )


def test_ac_gov_f27_6(tmp_path, monkeypatch):
    """Under enforce_sandbox: true, with the transport map DERIVED from the render, an allow-listed STDIO (and unknown-transport) tool is blocked while an allow-listed REMOTE tool proceeds; unlisted always blocked."""
    module = _compose_module(tmp_path, monkeypatch, "compose-home")
    spec = _write_spec(
        tmp_path / "spec",
        reviewer_mcp={
            "local-fs": {"command": "/bin/true", "include": ["read_file"]},
            "docs-ro": {"url": DOCS_RO_URL, "include": ["search_docs"]},
        },
    )
    rendered = _render(module, spec, tmp_path / "out")
    scope = _scope_of(rendered, "reviewer")
    rev = scope["reviewer"]
    # The render derived transport from command (stdio) vs url (remote), not the test.
    assert rev.get("mcp__local_fs__read_file") == "stdio"
    assert rev.get("mcp__docs_ro__search_docs") == "remote"
    scope = dict(scope, worker=dict(rev, mcp__weird__tool="satellite"))

    import tools.terminal_tool as tt

    monkeypatch.setattr(tt, "ensure_task_env", lambda *a, **k: object(), raising=False)
    monkeypatch.setenv(
        "TERMINAL_ENV", "docker"
    )  # == expected_backend: sandbox predicate on its live (non-mismatch) path
    settings = _gate_settings(
        scope, tmp_path, enforce_sandbox=True, profile_roles={"worker": "worker"}
    )
    manager, _ = _load_gate(tmp_path, monkeypatch, "worker", settings)

    assert _blocked(_gate(manager, "mcp__local_fs__read_file")), (
        "stdio mcp denied under sandbox enforcement"
    )
    assert _blocked(_gate(manager, "mcp__weird__tool")), (
        "unknown-transport mcp denied under enforcement"
    )
    assert _gate(manager, "mcp__docs_ro__search_docs") == [], (
        "allow-listed remote mcp proceeds under enforcement"
    )
    assert _blocked(_gate(manager, "mcp__docs_ro__delete_docs")), (
        "unlisted tool blocked regardless of transport"
    )


# --------------------------------------------------------------------------- #
# Behavioural AC (stock MCP registration consumes the RENDERED stanza)
# --------------------------------------------------------------------------- #
def test_ac_gov_f27_7(tmp_path, monkeypatch):
    """The rendered docs-ro stanza fed to stock registration registers only mcp__docs_ro__search_docs (no other native, no utility) and records the server trust as untrusted."""
    module = _compose_module(tmp_path, monkeypatch)
    spec = _write_spec(
        tmp_path / "spec",
        reviewer_mcp={"docs-ro": {"url": DOCS_RO_URL, "include": ["search_docs"]}},
        fixer_mcp={"repo-rw": {"url": REPO_RW_URL, "include": ["create_issue"]}},
    )
    rendered = _render(module, spec, tmp_path / "out")
    stanza = _servers(rendered, "reviewer")["docs-ro"]

    import tools.mcp_tool as mcp_core  # _server_trust_levels lives on the core module in both trees

    try:
        from tools.mcp_tool_registration import _register_server_tools  # type: ignore
        import tools.mcp_tool_registration as reg_mod  # type: ignore
    except ImportError:
        from tools.mcp_tool import _register_server_tools  # type: ignore

        reg_mod = mcp_core
    from tools.registry import ToolRegistry

    def _tool(name):
        return SimpleNamespace(
            name=name, description=f"{name} desc", inputSchema={"type": "object"}
        )

    server = SimpleNamespace(
        _tools=[_tool("search_docs"), _tool("delete_docs")],
        session=MagicMock(),
        tool_timeout=30.0,
    )
    reg = ToolRegistry()
    with (
        patch("tools.registry.registry", reg),
        patch.object(
            reg_mod, "_track_mcp_tool_server", lambda *a, **k: None, create=True
        ),
        patch.dict(mcp_core._server_trust_levels, {}, clear=True),
    ):
        _register_server_tools(
            "docs-ro", server, stanza
        )  # raw server key; only the tool NAME is sanitized
        trust_seen = dict(mcp_core._server_trust_levels)

    registered = sorted(
        n for n in reg.get_all_tool_names() if n.startswith("mcp__docs_ro__")
    )
    assert registered == ["mcp__docs_ro__search_docs"], (
        "only the allow-listed native tool registers"
    )
    assert trust_seen.get("docs-ro") == "untrusted", (
        "untrusted server annotated -> write-capable tools escalate"
    )
