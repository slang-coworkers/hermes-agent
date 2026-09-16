"""Additive unit coverage for the GOV-F27 MCP-scope render.

Hardening beyond the frozen acceptance criteria (test_gov_f27_mcp_scope_acceptance.py):
inherited-server stripping, fail-closed include/connection validation, the native
context-var exemption, and verbatim passthrough of rich connection fields. These
never mint or renumber an AC id — they exercise render behaviour the acceptance
criteria imply but do not each pin. Assertions read the RAW rendered config.yaml
(never load_config, which deep-merges DEFAULT_CONFIG).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from hermes_cli.plugins import PluginManager

COMPOSE_KEY = "nv-coworker-compose"
PLUGINS_ROOT = Path(__file__).resolve().parents[2] / "plugins"
FIXTURE_TREE = Path(__file__).parent / "fixtures" / "gov-f27"

DOCS_RO_URL = "https://onecli.local/mcp/docs"
GATES_SETTINGS = "plugins.entries.nv-fleet-gates.settings"


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
    return loaded.module


def _base_spec() -> dict:
    return {
        "workspace_root": "/data/coworkers",  # ISO-F13: required fleet key
        "project": "demo",
        "default_profile": "default",
        "orchestrator_profile": "orchestrator",
        "worker_profile": "reviewer",
        "spines": {"base": {"source": "spines/base.yaml"}},
        "skills_root": "skills",
        "workflows_root": "workflows",
        "overlays_root": "overlays",
        "default_config": {
            "gateway": {
                "multiplex_profiles": True,
                "multiplex_profile_allowlist": ["orchestrator", "reviewer", "fixer"],
            }
        },
        "types": {
            "orchestrator": {"extends": ["base"], "identity": "O", "ui_meta": {"title": "O", "description": "o", "shape": "hexagon"}},
            "reviewer": {"extends": ["base"], "identity": "R", "skills": ["review-skill"], "ui_meta": {"title": "R", "description": "r", "shape": "diamond"}},
            "fixer": {"extends": ["base"], "identity": "F", "skills": ["fix-skill"], "ui_meta": {"title": "F", "description": "f", "shape": "square"}},
        },
    }


def _write_spec(dest: Path, *, reviewer_mcp=None, fixer_mcp=None, base_mutator=None) -> Path:
    shutil.copytree(FIXTURE_TREE, dest)
    spec = _base_spec()
    if reviewer_mcp is not None:
        spec["types"]["reviewer"]["mcp"] = reviewer_mcp
    if fixer_mcp is not None:
        spec["types"]["fixer"]["mcp"] = fixer_mcp
    (dest / "coworker-types.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")
    if base_mutator is not None:
        base = dest / "spines" / "base.yaml"
        data = yaml.safe_load(base.read_text(encoding="utf-8")) or {}
        base_mutator(data)
        base.write_text(yaml.safe_dump(data), encoding="utf-8")
    return dest / "coworker-types.yaml"


def _render(module, spec: Path, out_root: Path) -> dict:
    out_root.mkdir(parents=True, exist_ok=True)
    return module.compose(str(spec), str(out_root))


def _raw(pdir) -> dict:
    return yaml.safe_load((Path(pdir) / "config.yaml").read_text(encoding="utf-8")) or {}


def _servers(rendered, profile) -> dict:
    return _raw(rendered[profile]).get("mcp_servers") or {}


def _err(module):
    return getattr(module, "CompositionError", Exception)


def test_stale_mcp_servers_stripped(tmp_path, monkeypatch):
    """An inherited/pre-existing mcp_servers is stripped so a profile carries ONLY its role's servers."""

    def _seed(base):
        base.setdefault("config", {})["mcp_servers"] = {
            "leftover": {"url": "https://onecli.local/mcp/leftover", "tools": {"include": ["x"]}}
        }

    module = _compose_module(tmp_path, monkeypatch)
    spec = _write_spec(
        tmp_path / "spec",
        reviewer_mcp={"docs-ro": {"url": DOCS_RO_URL, "include": ["search_docs"]}},
        base_mutator=_seed,
    )
    rendered = _render(module, spec, tmp_path / "out")
    for profile in rendered:
        assert "leftover" not in _servers(rendered, profile), (
            f"{profile}: inherited server must be stripped"
        )
    assert set(_servers(rendered, "reviewer")) == {"docs-ro"}
    assert _servers(rendered, "orchestrator") == {}, "no-MCP role keeps no servers after strip"


def test_missing_include_rejected(tmp_path, monkeypatch):
    """A missing include is refused: core treats an inactive include filter as register-all (fail-open)."""
    module = _compose_module(tmp_path, monkeypatch)
    spec = _write_spec(tmp_path / "spec", reviewer_mcp={"docs-ro": {"url": DOCS_RO_URL}})
    with pytest.raises(_err(module), match=r"include"):
        module.compose(str(spec), str(tmp_path / "out"))


def test_scalar_include_rejected(tmp_path, monkeypatch):
    """A scalar (non-list) include is refused — the exact-set contract requires a list."""
    module = _compose_module(tmp_path, monkeypatch)
    spec = _write_spec(
        tmp_path / "spec", reviewer_mcp={"docs-ro": {"url": DOCS_RO_URL, "include": "search_docs"}}
    )
    with pytest.raises(_err(module), match=r"include"):
        module.compose(str(spec), str(tmp_path / "out"))


def test_non_string_include_entry_rejected(tmp_path, monkeypatch):
    """A non-string include entry is refused (would break the sanitized-name map)."""
    module = _compose_module(tmp_path, monkeypatch)
    spec = _write_spec(
        tmp_path / "spec", reviewer_mcp={"docs-ro": {"url": DOCS_RO_URL, "include": [123]}}
    )
    with pytest.raises(_err(module), match=r"include|string"):
        module.compose(str(spec), str(tmp_path / "out"))


def test_blank_connection_rejected(tmp_path, monkeypatch):
    """A blank url (no command) is refused — no valid transport connection."""
    module = _compose_module(tmp_path, monkeypatch)
    spec = _write_spec(
        tmp_path / "spec", reviewer_mcp={"docs-ro": {"url": "", "include": ["search_docs"]}}
    )
    with pytest.raises(_err(module), match=r"url|command|transport|non-empty"):
        module.compose(str(spec), str(tmp_path / "out"))


def test_empty_include_allowed(tmp_path, monkeypatch):
    """include: [] is a valid explicit empty whitelist (core registers nothing); no scope keys for that server."""
    module = _compose_module(tmp_path, monkeypatch)
    spec = _write_spec(tmp_path / "spec", reviewer_mcp={"docs-ro": {"url": DOCS_RO_URL, "include": []}})
    rendered = _render(module, spec, tmp_path / "out")
    assert _servers(rendered, "reviewer")["docs-ro"]["tools"]["include"] == []
    found = _dig(_raw(rendered["reviewer"]), GATES_SETTINGS + ".mcp_scope")
    assert found[0] and found[1].get("reviewer") == {}, "an empty include yields no allow-list keys"


@pytest.mark.parametrize(
    "ref",
    ["${userHome}", "${workspaceFolder}", "${workspaceFolderBasename}", "${pathSeparator}", "${/}"],
)
def test_native_context_ref_allowed(tmp_path, monkeypatch, ref):
    """Every native ${...} context reference renders verbatim (never rejected as a secret read)."""
    module = _compose_module(tmp_path, monkeypatch, name=f"h-{abs(hash(ref))}")
    spec = _write_spec(
        tmp_path / f"spec-{abs(hash(ref))}",
        reviewer_mcp={"docs-ro": {"url": DOCS_RO_URL, "headers": {"X-Root": ref}, "include": ["search_docs"]}},
    )
    rendered = _render(module, spec, tmp_path / f"out-{abs(hash(ref))}")
    assert _servers(rendered, "reviewer")["docs-ro"]["headers"]["X-Root"] == ref


def test_global_env_ref_allowed(tmp_path, monkeypatch):
    """A genuinely-global env ref (TERMINAL_ prefix, _is_global_env) is allowed in both forms."""
    module = _compose_module(tmp_path, monkeypatch)
    spec = _write_spec(
        tmp_path / "spec",
        reviewer_mcp={
            "docs-ro": {
                "url": DOCS_RO_URL,
                "headers": {"A": "${TERMINAL_FOO}", "B": "${env:TERMINAL_BAR}"},
                "include": ["search_docs"],
            }
        },
    )
    rendered = _render(module, spec, tmp_path / "out")
    hdrs = _servers(rendered, "reviewer")["docs-ro"]["headers"]
    assert hdrs["A"] == "${TERMINAL_FOO}" and hdrs["B"] == "${env:TERMINAL_BAR}"


def test_env_form_context_ref_rejected(tmp_path, monkeypatch):
    """${env:workspaceFolder} is an env reference, not a context var — non-global, so rejected."""
    module = _compose_module(tmp_path, monkeypatch)
    spec = _write_spec(
        tmp_path / "spec",
        reviewer_mcp={
            "docs-ro": {"url": DOCS_RO_URL, "headers": {"X": "${env:workspaceFolder}"}, "include": ["search_docs"]}
        },
    )
    with pytest.raises(_err(module), match=r"env:"):
        module.compose(str(spec), str(tmp_path / "out"))


def test_connection_fields_passthrough(tmp_path, monkeypatch):
    """Every connection field survives verbatim (incl. transport: sse, headers, ssl_verify, args, env); only tools.*/trust are render-managed."""
    module = _compose_module(tmp_path, monkeypatch)
    remote = {
        "url": DOCS_RO_URL,
        "transport": "sse",
        "headers": {"Authorization": "Bearer placeholder"},
        "ssl_verify": False,
        "connect_timeout": 42,
        "include": ["search_docs"],
    }
    stdio = {
        "command": "/usr/bin/local-mcp",
        "args": ["--flag", "value"],
        "env": {"SAFE": "1"},
        "tool_timeout": 30,
        "include": ["read_file"],
    }
    spec = _write_spec(tmp_path / "spec", reviewer_mcp={"remote-srv": remote}, fixer_mcp={"stdio-srv": stdio})
    rendered = _render(module, spec, tmp_path / "out")

    r = _servers(rendered, "reviewer")["remote-srv"]
    assert r["transport"] == "sse", "SSE transport preserved (else runtime silently downgrades to Streamable HTTP)"
    assert r["url"] == DOCS_RO_URL
    assert r["headers"] == {"Authorization": "Bearer placeholder"}
    assert r["ssl_verify"] is False and r["connect_timeout"] == 42
    assert r["tools"] == {"include": ["search_docs"], "resources": False, "prompts": False}
    assert r["trust"] == "untrusted"
    assert "include" not in r, "top-level include is translated into tools.include, not left in the stanza"

    s = _servers(rendered, "fixer")["stdio-srv"]
    assert s["command"] == "/usr/bin/local-mcp"
    assert s["args"] == ["--flag", "value"]
    assert s["env"] == {"SAFE": "1"} and s["tool_timeout"] == 30
    assert s["tools"]["include"] == ["read_file"]


def test_model_name_collision_rejected(tmp_path, monkeypatch):
    """Two distinct (server, tool) declarations that sanitize to the same mcp__server__tool are refused fleet-wide.

    ``(a, b__c)`` and ``(a__b, c)`` both yield ``mcp__a__b__c``; the ``__`` delimiter is
    ambiguous, so one profile's scope key would match another's registered tool.
    """
    module = _compose_module(tmp_path, monkeypatch)
    spec = _write_spec(
        tmp_path / "spec",
        reviewer_mcp={"a": {"url": DOCS_RO_URL, "include": ["b__c"]}},
        fixer_mcp={"a__b": {"url": DOCS_RO_URL, "include": ["c"]}},
    )
    with pytest.raises(_err(module), match=r"collision"):
        module.compose(str(spec), str(tmp_path / "out"))


def test_utility_native_collision_rejected(tmp_path, monkeypatch):
    """A native tool that collides with a generated resource/prompt utility name is refused."""
    module = _compose_module(tmp_path, monkeypatch)
    spec = _write_spec(
        tmp_path / "spec",
        reviewer_mcp={
            "srv": {"url": DOCS_RO_URL, "include": ["list_resources"], "resources": True}
        },
    )
    with pytest.raises(_err(module), match=r"collision"):
        module.compose(str(spec), str(tmp_path / "out"))


def _dig(mapping: dict, dotted: str):
    node = mapping
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node
