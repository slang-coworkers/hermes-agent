"""Acceptance test for SELF-F55 — Agent templates (Agent Plugins 1.0.0) and plugin MCP servers.

Proves stock Hermes v2026.8.31 satisfies both lanes through the real install/load/update
paths: portable Agent Plugins v1 (install disabled, enable per profile, namespaced skill,
portable mcp.json in runtime MCP discovery with ${PLUGIN_ROOT}/${PLUGIN_DATA} expansion) and
profile distributions (layout install, .env.EXAMPLE credentials, paused cron, update
ownership). Hermetic: temp HERMES_HOME, empty HERMES_BUNDLED_PLUGINS, no network.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from hermes_cli.plugins import PluginManager
from hermes_cli.plugins_cmd import cmd_install
from hermes_cli.profile_distribution import install_distribution, update_distribution
from tools.mcp_tool import _load_mcp_config
from cron.jobs import is_job_runnable

# The six paths the requirement fixes as distribution-owned (asserted as the contract, not
# derived from the code's DEFAULT_DIST_OWNED so the expectation cannot drift with the source).
REQUIRED_OWNED = ("distribution.yaml", "SOUL.md", "config.yaml", "mcp.json", "skills", "cron")

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
DIST_FIXTURE = FIXTURES_DIR / "self_f55_coworker_dist"
PLUGIN_FIXTURE = FIXTURES_DIR / "self_f55_agent_plugin"

NAMESPACE_RE = re.compile(r"^agent-plugin-[a-z0-9][a-z0-9-]*-[0-9a-f]{8}$")
ENV_REQUIRED_VAR = "SELF_F55_REQUIRED_TOKEN"
ENV_SECRET_SENTINEL = "should-never-be-copied"


# --------------------------------------------------------------------------- helpers


def _hermes_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "hermes-home"
    home.mkdir(parents=True, exist_ok=True)
    empty_bundled = tmp_path / "bundled-empty"
    empty_bundled.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(empty_bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    return home


def _write_config_enabled(home: Path, enabled: list[str]) -> None:
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": enabled}}), encoding="utf-8"
    )


def _read_enabled(home: Path) -> list:
    cfg_path = home / "config.yaml"
    if not cfg_path.exists():
        return []
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    return list((data.get("plugins") or {}).get("enabled") or [])


def _local_git_repo(src: Path, dest: Path) -> str:
    shutil.copytree(src, dest)
    git = ["git", "-C", str(dest)]
    subprocess.run(git + ["init", "-q"], check=True)
    subprocess.run(git + ["config", "user.email", "t@t.invalid"], check=True)
    subprocess.run(git + ["config", "user.name", "t"], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "fixture"], check=True)
    return dest.resolve().as_uri()


def _plugin_manifest_name(pkg_dir: Path) -> str:
    return json.loads((pkg_dir / "plugin.json").read_text(encoding="utf-8"))["name"]


def _first_mcp_server_name(pkg_dir: Path) -> str:
    return next(iter(json.loads((pkg_dir / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]))


def _find_entry(manager: PluginManager, name: str):
    return next(
        (lp for lp in manager._plugins.values() if getattr(lp.manifest, "name", None) == name),
        None,
    )


def _installed_dir(plan, home: Path, name: str) -> Path:
    for attr in ("target_dir", "target", "profile_dir", "path"):
        val = getattr(plan, attr, None)
        if isinstance(val, (str, Path)) and Path(val).exists():
            return Path(val)
    return home / "profiles" / name


def _install_dist(tmp_path, monkeypatch, name: str) -> tuple[Path, Path, Path]:
    home = _hermes_home(tmp_path, monkeypatch)
    staged = tmp_path / f"dist-src-{name}"
    shutil.copytree(DIST_FIXTURE, staged)
    plan = install_distribution(str(staged), name=name)
    return home, staged, _installed_dir(plan, home, name)


# ----------------------------------------------------------------- Lane A: Agent Plugins v1


@pytest.fixture()
def installed_disabled(tmp_path, monkeypatch):
    home = _hermes_home(tmp_path, monkeypatch)
    repo_uri = _local_git_repo(PLUGIN_FIXTURE, tmp_path / "plugin-repo")
    name = _plugin_manifest_name(PLUGIN_FIXTURE)
    # No force= override: a caution/dangerous scan verdict must fail the install, not be bypassed.
    cmd_install(repo_uri, enable=False)
    return home, name


def test_ac_self_f55_1(installed_disabled):
    """A portable Agent Plugins v1 package installed with --no-enable lands on disk and is disabled."""
    home, name = installed_disabled
    landed = home / "plugins" / name
    assert (landed / "plugin.json").is_file(), (
        "the portable package must land at <HERMES_HOME>/plugins/<manifest name>/"
    )
    assert _plugin_manifest_name(landed) == name, "the landed plugin.json must be the installed package"
    assert name not in _read_enabled(home), "--no-enable must not add the plugin to plugins.enabled"

    manager = PluginManager()
    manager.discover_and_load()
    entry = _find_entry(manager, name)
    assert entry is not None, "the installed package must be discovered"
    assert entry.enabled is False, "an un-enabled portable package must load disabled"


def test_ac_self_f55_2(installed_disabled):
    """Once enabled per profile, the package loads and its skill registers under agent-plugin-<slug>-<hash>."""
    home, name = installed_disabled
    _write_config_enabled(home, [name])

    manager = PluginManager()
    manager.discover_and_load()
    entry = _find_entry(manager, name)
    assert entry is not None and entry.enabled is True and entry.error is None

    namespace = entry.manifest.skill_namespace
    expected_digest = hashlib.sha256(entry.manifest.key.encode("utf-8")).hexdigest()[:8]
    assert NAMESPACE_RE.fullmatch(namespace), f"namespace {namespace!r} must be agent-plugin-<slug>-<8hex>"
    assert namespace.endswith(f"-{expected_digest}"), "namespace hash must be sha256(key)[:8] (deterministic)"
    assert manager.list_plugin_skills(namespace), "the portable skill must be registered under the namespace"


def test_ac_self_f55_3(installed_disabled):
    """The enabled package's portable mcp.json server is merged into runtime MCP discovery with PLUGIN_ROOT/PLUGIN_DATA expanded."""
    home, name = installed_disabled
    _write_config_enabled(home, [name])

    manager = PluginManager()
    manager.discover_and_load()
    entry = _find_entry(manager, name)
    namespace = entry.manifest.skill_namespace
    pkg_root = Path(entry.manifest.path).resolve()
    server_name = _first_mcp_server_name(PLUGIN_FIXTURE)
    expected_key = f"{namespace}__{server_name}"
    data_dir = str(home / "plugin-data" / namespace)

    servers = _load_mcp_config()  # reads config.yaml:mcp_servers + merges enabled portable plugins
    assert expected_key in servers, f"portable server must appear in runtime MCP discovery as {expected_key!r}"
    cfg = servers[expected_key]
    env = cfg.get("env", {})
    assert env.get("PLUGIN_ROOT") == str(pkg_root), "PLUGIN_ROOT must expand to the resolved package root"
    assert env.get("PLUGIN_DATA") == data_dir, "PLUGIN_DATA must be the profile-scoped data dir"
    joined = " ".join(str(a) for a in cfg.get("args", []))
    assert "${PLUGIN_ROOT}" not in joined and "${PLUGIN_DATA}" not in joined, "placeholders in args must be expanded"
    assert str(pkg_root) in joined and data_dir in joined, "expanded PLUGIN_ROOT and PLUGIN_DATA must appear in args"


# ------------------------------------------------------------- Lane B: profile distributions


def test_ac_self_f55_4(tmp_path, monkeypatch):
    """A profile-distribution layout installs from a local directory with distribution-owned content present."""
    _, _, installed = _install_dist(tmp_path, monkeypatch, "f55dist")
    for owned in REQUIRED_OWNED:
        assert (installed / owned).exists(), f"distribution-owned {owned!r} must land in the installed profile"
    # empty skills/ and cron/ are bootstrapped on install, so assert the real payload landed:
    assert list(installed.glob("skills/*/SKILL.md")), "the distribution's skill payload must be copied"
    assert (installed / "cron" / "jobs.json").is_file(), "the distribution's cron/jobs.json must be copied"
    # config.yaml is where a coworker type's LIVE runtime MCP servers are authored (the inert
    # profile-root mcp.json is not read at runtime), so assert the config carries a real payload
    # rather than a bootstrapped-empty stub.
    cfg = yaml.safe_load((installed / "config.yaml").read_text(encoding="utf-8")) or {}
    assert cfg.get("model"), "the distribution config.yaml must carry a model setting"
    assert isinstance(cfg.get("mcp_servers"), dict) and cfg["mcp_servers"], (
        "the distribution config.yaml must author a runtime mcp_servers entry (the live MCP lane)"
    )


def test_ac_self_f55_5(tmp_path, monkeypatch):
    """Credentials are represented by .env.EXAMPLE and a .env is never copied from the distribution."""
    home = _hermes_home(tmp_path, monkeypatch)
    staged = tmp_path / "dist-src-env"
    shutil.copytree(DIST_FIXTURE, staged)
    # The .env.EXAMPLE guarantee only holds if the source actually carries both credential
    # inputs; assert that at the source so the test cannot false-green on a drifted fixture
    # (either a .env.template alone or env_requires alone would still produce a .env.EXAMPLE).
    manifest = yaml.safe_load((staged / "distribution.yaml").read_text(encoding="utf-8")) or {}
    requires = manifest.get("env_requires")
    assert isinstance(requires, list), "the distribution must declare env_requires as a list"
    assert any(r.get("name") == ENV_REQUIRED_VAR and r.get("required", True) is True for r in requires), (
        "env_requires must mark SELF_F55_REQUIRED_TOKEN required"
    )
    assert any(r.get("required") is False for r in requires), "env_requires must include an optional entry"
    template = staged / ".env.template"
    assert template.is_file(), "the distribution must ship a .env.template"
    template_body = template.read_text(encoding="utf-8")
    assert ENV_REQUIRED_VAR in template_body, ".env.template must name the required credential"
    (staged / ".env").write_text(f"SELF_F55_REQUIRED_TOKEN={ENV_SECRET_SENTINEL}\n", encoding="utf-8")
    plan = install_distribution(str(staged), name="f55env")
    installed = _installed_dir(plan, home, "f55env")

    example = installed / ".env.EXAMPLE"
    assert example.is_file(), "install must produce a .env.EXAMPLE"
    body = example.read_text(encoding="utf-8")
    assert ENV_REQUIRED_VAR in body, ".env.EXAMPLE must name the required credential"
    assert ENV_SECRET_SENTINEL not in body, ".env.EXAMPLE must not carry a real secret value"
    # .env.EXAMPLE must be the shipped .env.template, copied verbatim — not a fallback synthesized
    # from env_requires, which would also contain the required var and mask a broken copy.
    assert body == template_body, ".env.EXAMPLE must be the distribution's .env.template, copied verbatim"
    assert not (installed / ".env").exists(), "a .env must never be copied from the distribution"


def test_ac_self_f55_6(tmp_path, monkeypatch):
    """A distribution cron job authored enabled:false installs paused (not runnable)."""
    _, _, installed = _install_dist(tmp_path, monkeypatch, "f55cron")
    jobs_file = installed / "cron" / "jobs.json"
    assert jobs_file.is_file(), "the distribution's cron/jobs.json must land on disk"
    jobs = json.loads(jobs_file.read_text(encoding="utf-8"))
    records = jobs if isinstance(jobs, list) else jobs.get("jobs", [])
    job = next((j for j in records if j.get("enabled") is False), None)
    assert job is not None, "the distribution must ship a job authored enabled:false"
    assert job.get("state") == "paused", "the authored job must install in the paused state"
    assert not is_job_runnable(job), "a job authored enabled:false must not be runnable"
    # Isolate the enabled=false gate: with the pause marker removed, enabled=false alone must
    # still make the job not runnable, so a state:paused marker cannot mask an enabled regression.
    assert not is_job_runnable({**job, "state": "scheduled", "paused_at": None}), (
        "enabled:false alone must gate runnability, independent of the pause marker"
    )


def test_ac_self_f55_7(tmp_path, monkeypatch):
    """profile update re-applies a bumped distribution-owned SOUL.md while preserving user-owned .env/memories."""
    _, staged, installed = _install_dist(tmp_path, monkeypatch, "f55upd")
    (installed / ".env").write_text("USER_SECRET=keep-me\n", encoding="utf-8")
    (installed / "memories").mkdir(exist_ok=True)
    (installed / "memories" / "user.md").write_text("remember this\n", encoding="utf-8")

    new_soul = "# Updated SOUL\nbumped by the distribution\n"
    (staged / "SOUL.md").write_text(new_soul, encoding="utf-8")
    update_distribution("f55upd", force_config=False)

    assert (installed / "SOUL.md").read_text(encoding="utf-8") == new_soul, "distribution-owned SOUL.md must update"
    assert (installed / ".env").read_text(encoding="utf-8") == "USER_SECRET=keep-me\n", "user .env must be preserved"
    assert (installed / "memories" / "user.md").read_text(encoding="utf-8") == "remember this\n", "user memories must be preserved"


def test_ac_self_f55_8(tmp_path, monkeypatch):
    """profile update preserves a user-edited config.yaml unless --force-config, which overwrites it."""
    _, staged, installed = _install_dist(tmp_path, monkeypatch, "f55cfg")
    user_cfg = "model: user-pinned-model\n"
    (installed / "config.yaml").write_text(user_cfg, encoding="utf-8")
    (staged / "config.yaml").write_text("model: distribution-model\n", encoding="utf-8")

    update_distribution("f55cfg", force_config=False)
    assert (installed / "config.yaml").read_text(encoding="utf-8") == user_cfg, "unforced update must preserve user config.yaml"

    update_distribution("f55cfg", force_config=True)
    assert (installed / "config.yaml").read_text(encoding="utf-8") == "model: distribution-model\n", "--force-config must overwrite config.yaml"
