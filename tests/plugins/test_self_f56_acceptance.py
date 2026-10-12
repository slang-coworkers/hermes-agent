"""Acceptance tests for SELF-F56 project and coworker onboarding.

The nv-coworker-compose carrier verifies criteria 2-4 through its own nodes in
tests/plugins/test_nv_coworker_compose_acceptance.py::test_ac_self_f56_{2,3,4}
(not redefined here — the tester joins criteria to tests by function name). This
file verifies the new AC-SELF-F56-5, AC-SELF-F56-6 and AC-SELF-F56-7: 5 and 6
drive stock hermes_cli surfaces directly (create_profile role-description
round-trip; install_distribution from a local directory); 7 loads the carrier
and drives onboard_coworker through the real install_distribution path, so it
needs the baseline branch where the carrier is present. The own-diff
documentation control is tests/plugins/test_self_f56_doc_contract.py.
"""

from pathlib import Path
import shutil

import pytest
import yaml

from hermes_cli.plugins import PluginManager

# --- carrier harness ---

PLUGIN_KEY = "nv-coworker-compose"
PLUGIN_SRC = Path(__file__).resolve().parents[2] / "plugins" / PLUGIN_KEY
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "loop-f35"
FIXTURE_SPEC = FIXTURE_DIR / "coworker-types.yaml"


def _write_home(tmp_path, monkeypatch, profile_name=None, config=None):
    root = tmp_path / "hermes-root"
    home = root / "profiles" / profile_name if profile_name else root / "hermes-home"
    plugins_dir = home / "plugins"
    plugins_dir.mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, plugins_dir / PLUGIN_KEY)
    (home / "config.yaml").write_text(
        yaml.safe_dump(config if config is not None else {"plugins": {"enabled": [PLUGIN_KEY]}}),
        encoding="utf-8",
    )
    empty_bundled = tmp_path / "bundled-plugins"
    empty_bundled.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(empty_bundled))
    return home


def _load(tmp_path, monkeypatch, profile_name=None, config=None):
    # Fail explicitly when the required carrier is unavailable.
    if not PLUGIN_SRC.exists():
        pytest.fail(f"carrier plugin source missing (expected at {PLUGIN_SRC}) — baseline branch required")
    _write_home(tmp_path, monkeypatch, profile_name=profile_name, config=config)
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled is True
    assert loaded.error is None
    assert loaded.module is not None
    return manager, loaded


# --- stock profile sandbox ---


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    default_home = tmp_path / ".hermes"
    default_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    return tmp_path


def test_ac_self_f56_5(profile_env):
    """AC-SELF-F56-5: Stock create_profile creates an enumerable coworker profile whose routing description round-trips through list_profiles, providing the manual single-coworker path alongside distribution-backed fleet onboarding"""
    from hermes_cli.profiles import _get_profiles_root, create_profile, list_profiles

    reviewer_desc = "reviews pull requests and files findings for the fleet"
    notes_desc = "keeps the team's durable notes and daily digests"

    reviewer_dir = create_profile("reviewer-bot", no_alias=True, no_skills=True, description=reviewer_desc)
    notes_dir = create_profile("notes-bot", no_alias=True, no_skills=True, description=notes_desc)

    root = _get_profiles_root()
    assert reviewer_dir.is_dir() and reviewer_dir.parent == root
    assert notes_dir.is_dir() and notes_dir != reviewer_dir

    by_name = {p.name: p for p in list_profiles()}
    assert {"reviewer-bot", "notes-bot"} <= set(by_name), f"onboarded profiles not enumerated: {set(by_name)}"
    # The routing description the kanban decomposer reads must round-trip per profile — a
    # regression that dropped or cross-wired descriptions flips this differential.
    assert by_name["reviewer-bot"].description == reviewer_desc
    assert by_name["notes-bot"].description == notes_desc
    assert by_name["reviewer-bot"].description != by_name["notes-bot"].description


def test_ac_self_f56_6(profile_env, monkeypatch):
    """AC-SELF-F56-6: Stock install_distribution installs a whole-agent coworker profile from a local distribution directory with no network, the stock primitive onboard coworker rides to install each rendered coworker type"""
    from hermes_cli.profiles import _get_profiles_root, list_profiles
    import hermes_cli.profile_distribution as profile_distribution
    from hermes_cli.profile_distribution import (
        DistributionManifest,
        install_distribution,
        write_manifest,
    )

    # A local-directory source must never touch the network — fail if the git clone path runs.
    monkeypatch.setattr(
        profile_distribution, "_git_clone",
        lambda *a, **k: pytest.fail("local distribution install attempted a network clone"),
        raising=True,
    )

    def _staging(root, name):
        staged = root / f"staging_{name}"
        staged.mkdir(parents=True, exist_ok=True)
        (staged / "SOUL.md").write_text(f"I am {name}.\n", encoding="utf-8")
        (staged / "config.yaml").write_text("model:\n  model: gpt-4\n", encoding="utf-8")
        (staged / "mcp.json").write_text('{"servers": {}}\n', encoding="utf-8")
        write_manifest(staged, DistributionManifest(name=name, version="0.1.0"))
        return staged

    src = profile_env / "dists"  # a LOCAL directory source — install_distribution does not clone
    plan_a = install_distribution(str(_staging(src, "coworker-alpha")), name="coworker-alpha")
    plan_b = install_distribution(str(_staging(src, "coworker-beta")), name="coworker-beta")

    root = _get_profiles_root()
    for plan, name in ((plan_a, "coworker-alpha"), (plan_b, "coworker-beta")):
        target = Path(plan.target_dir)
        assert target.is_dir() and target.parent == root, f"{name} not installed under the profiles root"
        assert plan.manifest.name == name
        assert (target / "SOUL.md").is_file(), f"{name}: distribution payload not copied"
    assert Path(plan_a.target_dir) != Path(plan_b.target_dir)

    installed = {p.name for p in list_profiles()}
    assert {"coworker-alpha", "coworker-beta"} <= installed, f"installed coworkers not enumerated: {installed}"


def test_ac_self_f56_7(tmp_path, monkeypatch):
    """AC-SELF-F56-7: onboard coworker installs one distribution-backed profile for every coworker type declared by the input before returning success"""
    from hermes_cli.profiles import list_profiles

    _, loaded = _load(tmp_path, monkeypatch)
    spec_data = yaml.safe_load(FIXTURE_SPEC.read_text(encoding="utf-8")) or {}
    coworkers = list((spec_data.get("types") or {}).keys())
    created_rooms = set()

    def _rpc(method, params):
        if method == "profiles.list":
            return {"profiles": [{"name": n, "ui_meta": {}, "ui_meta_revisions": {}} for n in coworkers]}
        if method == "session.list":
            # a canonical Bot Chat already present -> _ensure_canonical_bot_chat confirms
            return {"sessions": [{"title": loaded.module.BOT_CHAT_TITLE}]}
        if method == "profiles.configure":
            return {"applied": {"ui_meta": True}, "ui_meta_revisions": {"hermes-bots": 1}}
        if method == "groups.state":
            rid = params.get("room_id") or params.get("name")
            return {"exists": rid in created_rooms}
        if method == "groups.create":
            created_rooms.add(params.get("room_id") or params.get("name"))
            return {"ok": True}
        # No catch-all success — an unexpected dispatch must surface, not be masked.
        raise AssertionError(f"unexpected RPC: {method}")

    monkeypatch.setattr(loaded.module, "_gateway_preflight", lambda url: (True, "ok"), raising=True)
    monkeypatch.setattr(loaded.module, "_dispatch_rpc", _rpc, raising=True)

    # No _install/install_distribution stub: the REAL install path renders and installs each
    # declared coworker into the isolated HERMES_HOME, so a broken install flips result["ok"].
    result = loaded.module.onboard_coworker(
        str(FIXTURE_SPEC), settings={"gateway_url": "ws://127.0.0.1:9/api/ws?token=t"}
    )
    assert result.get("ok") is True, f"onboard_coworker did not report success: {result}"

    installed = {p.name: p for p in list_profiles()}
    for name in coworkers:
        assert name in installed, f"declared coworker {name} was not installed as a profile"
        # Distribution-backed, not a bare create_profile: swapping _install for create_profile
        # would leave distribution_name None and no distribution.yaml, flipping these.
        assert installed[name].distribution_name == name, f"{name} not installed from its distribution"
        assert (installed[name].path / "distribution.yaml").is_file(), f"{name} missing distribution.yaml"
