"""Acceptance test for RT-F01 — Entity model with user-level privilege.

Extends nv-coworker-compose (CONFIGURE). The render must:
 (1) enable multiplexing on the DEFAULT profile and name the roster as the
     EFFECTIVE parsed config, winning over core's root>nested precedence;
 (2) keep every webhook route on the DEFAULT profile with its OWN secret + an
     EXPLICIT ``profile:`` binding to a served profile (alias spellings refused);
 (3) refuse a coworker profile that enables any port-binding platform under any
     config spelling, using core's own ``platform_binds_port`` predicate;
 (4) write each coworker's per-platform human allowlist to config.yaml so that,
     loaded through Hermes's real loader, it drives native ``_is_user_authorized``
     through the CONFIG path (no auth env set); and
 (5) ship a doc page whose mapping table covers the six NanoClaw entities + the
     behavior anchors + the two thin spots + the one-time-bring-up note.

The plugin is loaded from an isolated HERMES_HOME with an EMPTY bundled dir through
the real ``PluginManager().discover_and_load()`` path (shape:
tests/hermes_cli/test_plugin_api_compat.py:14-52); the render behaviour is then
driven and the real native readers (``GatewayConfig.from_dict``, ``profiles_to_serve``,
``platform_binds_port``, ``load_gateway_config``, ``_is_user_authorized``) are
exercised so the golden assertions are non-vacuous.

Fails on stock v2026.8.31 (the fixture's copy of the absent plugin dir fails, so
setup cannot proceed). On the pre-RT-F01 fork base, -1/-2/-3/-5 fail (no multiplex
enforcement / route validation / port-binding refusal / doc); -4 is the
security-boundary regression guard (the runtime already bridges top-level
allow_from, so its fail-before is plugin-absent).

Imports used against MAIN-stable module paths (verified stable tag<->main in the
RT-F01 ADR): hermes_cli.profiles, gateway.run.GatewayRunner, gateway.session,
gateway.config. None is the decomposed gateway/run_inbound.py path.
"""

from __future__ import annotations

import shutil
import sys
import types as _types
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_KEY = "nv-coworker-compose"
PLUGIN_SRC = REPO_ROOT / "plugins" / PLUGIN_KEY
DOC_PAGE = REPO_ROOT / "website" / "docs" / "user-guide" / "fleet-entity-model.md"

USER_A = "user-a-allowed"  # placeholders only — no real id/secret is embedded
USER_B = "user-b-denied"
ROSTER = ["orchestrator", "worker"]
_AUTH_ENV = (
    "TELEGRAM_ALLOWED_USERS", "TELEGRAM_ALLOW_ALL_USERS",
    "GATEWAY_ALLOWED_USERS", "GATEWAY_ALLOW_ALL_USERS",
    "GATEWAY_MULTIPLEX_PROFILES",
)


def _write_fixture_spec(spec_dir: Path, spec: dict) -> Path:
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spines").mkdir(exist_ok=True)
    (spec_dir / "spines" / "base.yaml").write_text(
        yaml.safe_dump(
            {"identity": "Base coworker.", "invariants": ["Be helpful."], "config": {}},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    wf_dir = spec_dir / "workflows" / "wf"
    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / "SKILL.md").write_text("# wf\n\nDo the work.\n", encoding="utf-8")
    (spec_dir / "skills").mkdir(exist_ok=True)
    spec_path = spec_dir / "coworker-types.yaml"
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    return spec_path


def _clean_spec() -> dict:
    """A well-formed fleet: DEFAULT carries two valid webhook routes; two canonical
    coworker types carry a top-level telegram allowlist and no port-binding platform.
    The DEFAULT deliberately OMITS the multiplex keys so the clean render proves the
    'never by assumption' enforcement."""
    return {
        "orchestrator_profile": "orchestrator",
        "default_profile": "default",
        "spines": {"base": {"source": "spines/base.yaml"}},
        "default_config": {
            "platforms": {
                "webhook": {
                    "enabled": True,
                    "extra": {
                        "routes": {
                            "issues": {"secret": "ISSUES_PLACEHOLDER", "profile": "orchestrator"},
                            "pr": {"secret": "PR_PLACEHOLDER", "profile": "worker"},
                        },
                    },
                }
            }
        },
        "types": {
            "orchestrator": {
                "extends": ["base"], "identity": "Orchestrator.", "workflows": ["wf"],
                "config": {"platforms": {"telegram": {"enabled": True, "allow_from": [USER_A]}}},
            },
            "worker": {
                "extends": ["base"], "identity": "Worker.", "workflows": ["wf"],
                "config": {"platforms": {"telegram": {"enabled": True, "allow_from": [USER_A]}}},
            },
        },
    }


@pytest.fixture()
def loaded(tmp_path, monkeypatch):
    from hermes_cli.plugins import PluginManager

    hermes_home = tmp_path / "hermes_home"
    (hermes_home / "plugins").mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, hermes_home / "plugins" / PLUGIN_KEY)
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": [PLUGIN_KEY]}}), encoding="utf-8"
    )
    bundled = tmp_path / "empty_bundled"
    bundled.mkdir()

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    for var in _AUTH_ENV:
        monkeypatch.delenv(var, raising=False)

    manager = PluginManager()
    manager.discover_and_load()
    entry = manager._plugins[PLUGIN_KEY]
    assert entry.enabled is True
    assert entry.error is None
    assert entry.module is not None
    return entry.module, hermes_home


def _render(module, tmp_path, spec: dict, name: str) -> Path:
    spec_path = _write_fixture_spec(tmp_path / f"spec_{name}", spec)
    out = tmp_path / f"out_{name}"
    module.compose(str(spec_path), str(out))
    return out


def _read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def test_ac_rt_f01_1(loaded, tmp_path, monkeypatch):
    """The rendered DEFAULT profile enables multiplexing and names the roster as the
    effective parsed config even against a spec that contradicts both keys at both
    spellings; the parsed allowlist drives profiles_to_serve to serve exactly the
    roster + default and exclude a non-roster profile."""
    module, hermes_home = loaded
    from gateway.config import GatewayConfig
    from hermes_cli.profiles import profiles_to_serve

    monkeypatch.delenv("GATEWAY_MULTIPLEX_PROFILES", raising=False)

    out = _render(module, tmp_path, _clean_spec(), "omit")
    cfg = GatewayConfig.from_dict(_read_yaml(out / "default" / "config.yaml"))
    assert cfg.multiplex_profiles is True
    assert set(cfg.multiplex_profile_allowlist) == set(ROSTER)

    contradict = _clean_spec()
    contradict["default_config"]["multiplex_profiles"] = False
    contradict["default_config"]["multiplex_profile_allowlist"] = ["root-stale"]
    contradict["default_config"]["gateway"] = {
        "multiplex_profiles": False,
        "multiplex_profile_allowlist": ["nested-stale"],
    }
    out2 = _render(module, tmp_path, contradict, "contradict")
    cfg2 = GatewayConfig.from_dict(_read_yaml(out2 / "default" / "config.yaml"))
    assert cfg2.multiplex_profiles is True
    assert set(cfg2.multiplex_profile_allowlist) == set(ROSTER)

    profiles_root = hermes_home / "profiles"
    profiles_root.mkdir(exist_ok=True)
    for name in ROSTER:
        shutil.copytree(out2 / name, profiles_root / name)
    (profiles_root / "intruder").mkdir()
    served = {name for name, _ in profiles_to_serve(cfg2.multiplex_profiles, cfg2.multiplex_profile_allowlist)}
    assert served == {"default", "orchestrator", "worker"}
    assert "intruder" not in served

    # canonical-name invariants (so allowlist entries == webhook profile bindings ==
    # served profile dirs): a non-"default" default_profile, a missing orchestrator, a
    # "default" coworker (dir collision), and a non-canonical type name each refuse.
    def _reject_profile(mutator, tag):
        spec = _clean_spec()
        mutator(spec)
        with pytest.raises(module.CompositionError):
            _render(module, tmp_path, spec, tag)

    def _rename_worker(spec, name):
        spec["types"][name] = spec["types"].pop("worker")
        spec["default_config"]["platforms"]["webhook"]["extra"]["routes"]["pr"]["profile"] = name

    _reject_profile(lambda s: s.__setitem__("default_profile", "root"), "nondefault_root")
    _reject_profile(lambda s: s.__setitem__("orchestrator_profile", "ghost"), "missing_orchestrator")
    _reject_profile(lambda s: _rename_worker(s, "default"), "default_type")
    _reject_profile(lambda s: _rename_worker(s, "Worker"), "noncanonical_type")


def test_ac_rt_f01_2(loaded, tmp_path):
    """Every webhook route is rendered only into the DEFAULT profile with its own
    secret and an explicit profile: binding to a served profile; a route missing a
    secret, omitting profile, binding an unknown profile, or hiding under an alias
    platform spelling fails the render; no coworker profile carries a webhook route."""
    module, _home = loaded
    served = {"default", *ROSTER}

    out = _render(module, tmp_path, _clean_spec(), "webhook")
    routes = _read_yaml(out / "default" / "config.yaml")["platforms"]["webhook"]["extra"]["routes"]
    assert set(routes) == {"issues", "pr"}
    for name, route in routes.items():
        assert isinstance(route.get("secret"), str) and route["secret"], name
        assert route.get("profile") in served, name

    for cw in ROSTER:
        webhook = _read_yaml(out / cw / "config.yaml").get("platforms", {}).get("webhook", {})
        assert "routes" not in webhook.get("extra", {})

    def _reject(mutate, tag):
        spec = _clean_spec()
        mutate(spec)
        with pytest.raises(module.CompositionError):
            _render(module, tmp_path, spec, tag)

    def _routes(s):
        return s["default_config"]["platforms"]["webhook"]["extra"]["routes"]

    _reject(lambda s: _routes(s)["pr"].pop("secret"), "nosecret")
    _reject(lambda s: _routes(s)["pr"].__setitem__("secret", ""), "emptysecret")
    _reject(lambda s: _routes(s)["pr"].__setitem__("secret", 7), "nonstringsecret")
    _reject(lambda s: _routes(s)["pr"].pop("profile"), "noprofile")
    _reject(lambda s: _routes(s)["pr"].__setitem__("profile", "ghost"), "ghostprofile")
    # a webhook block under an alias spelling on the default must not evade validation
    _reject(
        lambda s: s["default_config"].setdefault("gateway", {}).setdefault("platforms", {}).__setitem__(
            "webhook", {"enabled": True, "extra": {"routes": {"x": {"secret": "S", "profile": "worker"}}}}),
        "aliasroute",
    )
    # a coworker must carry no webhook routes even when disabled (routes belong on DEFAULT)
    _reject(
        lambda s: s["types"]["worker"]["config"].setdefault("platforms", {}).__setitem__(
            "webhook", {"enabled": False, "extra": {"routes": {"shadow": {"secret": "S", "profile": "worker"}}}}),
        "coworker_disabled_route",
    )


def test_ac_rt_f01_3(loaded, tmp_path, monkeypatch):
    """No coworker profile enables a port-binding platform under any config spelling:
    the render refuses webhook/api_server/line and webhook-mode feishu (canonical +
    alias spellings), accepts websocket-mode feishu, and actually calls core's
    platform_binds_port rather than a hard-coded set."""
    module, _home = loaded
    from gateway.config import platform_binds_port

    def _reject(mutate, tag):
        spec = _clean_spec()
        mutate(spec["types"]["worker"]["config"])
        with pytest.raises(module.CompositionError):
            _render(module, tmp_path, spec, tag)

    for platform in ("webhook", "api_server", "line"):
        _reject(lambda c, p=platform: c.setdefault("platforms", {}).__setitem__(p, {"enabled": True}),
                f"canon_{platform}")
    _reject(lambda c: c.__setitem__("webhook", {"enabled": True}), "toplevel")
    _reject(lambda c: c.setdefault("gateway", {}).__setitem__("webhook", {"enabled": True}), "gw")
    _reject(lambda c: c.setdefault("gateway", {}).setdefault("platforms", {}).__setitem__(
        "webhook", {"enabled": True}), "gwplatforms")
    _reject(lambda c: c.setdefault("platforms", {}).__setitem__(
        "feishu", {"enabled": True, "extra": {"connection_mode": "webhook"}}), "feishu_webhook")

    ok = _clean_spec()
    ok["types"]["worker"]["config"]["platforms"]["feishu"] = {
        "enabled": True, "extra": {"connection_mode": "websocket"},
    }
    _render(module, tmp_path, ok, "feishu_ws")

    assert platform_binds_port("webhook", None) is True
    assert platform_binds_port("feishu", {"connection_mode": "websocket"}) is False

    # prove the render routes through the imported predicate (not a literal set):
    # forcing platform_binds_port true makes an otherwise-accepted platform refuse.
    compose_mod = sys.modules[module.compose.__module__]
    monkeypatch.setattr(compose_mod, "platform_binds_port", lambda p, e=None: True)
    spec = _clean_spec()  # telegram is not port-binding and is normally accepted
    with pytest.raises(module.CompositionError):
        _render(module, tmp_path, spec, "forced")


def test_ac_rt_f01_4(loaded, tmp_path, monkeypatch):
    """A coworker's per-platform allowlist is written to config.yaml (never .env), and
    when the rendered profile is loaded through Hermes's real loader that allowlist
    drives native _is_user_authorized through the config path to admit the allowlisted
    human and default-deny another."""
    module, _home = loaded
    from gateway.config import Platform, load_gateway_config
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource

    out = _render(module, tmp_path, _clean_spec(), "authz")
    for cw in ROSTER:
        assert _read_yaml(out / cw / "config.yaml")["platforms"]["telegram"]["allow_from"] == [USER_A]
    for env_file in list(out.rglob(".env")) + list(out.rglob(".env.template")):
        assert USER_A not in env_file.read_text(encoding="utf-8")

    # load the rendered worker config through the real loader so its generic bridge
    # (config.py:1739-1740) populates PlatformConfig.extra["allow_from"].
    load_home = tmp_path / "load_home"
    load_home.mkdir()
    shutil.copyfile(out / "worker" / "config.yaml", load_home / "config.yaml")
    monkeypatch.setenv("HERMES_HOME", str(load_home))
    for var in _AUTH_ENV:
        monkeypatch.delenv(var, raising=False)
    cfg = load_gateway_config()
    pc = cfg.platforms[Platform.TELEGRAM]
    assert pc.extra.get("allow_from") == [USER_A]

    runner = object.__new__(GatewayRunner)
    runner.pairing_store = None
    runner._profile_adapters = {"worker": {Platform.TELEGRAM: _types.SimpleNamespace(config=pc)}}

    def _src(user_id: str) -> SessionSource:
        return SessionSource(
            platform=Platform.TELEGRAM, chat_id=user_id, chat_type="dm",
            user_id=user_id, profile="worker",
        )

    assert runner._is_user_authorized(_src(USER_A)) is True
    assert runner._is_user_authorized(_src(USER_B)) is False


def test_ac_rt_f01_5(loaded):
    """The doc page's mapping table covers the six NanoClaw entities (each -> a
    non-placeholder Hermes object), names the load-bearing anchors, records the two
    thin spots, and states rooms are a one-time operator bring-up whose membership is
    verified from hosted_rooms (distinct from the ui_meta projection)."""
    assert DOC_PAGE.is_file(), f"missing doc page {DOC_PAGE}"
    raw = DOC_PAGE.read_text(encoding="utf-8")
    low = raw.lower()

    placeholders = {"", "tbd", "todo", "-", "—", "n/a", "tk"}
    rows = {}
    for line in raw.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and set(cells[0]) != {"-"}:
            rows[cells[0].strip("`").strip().lower()] = cells[1].strip("`").strip()
    for entity in ("users", "messaging_groups", "wirings", "agent_groups",
                   "user_roles", "agent_group_members"):
        assert entity in rows, f"entity mapping row missing: {entity}"
        assert rows[entity].lower() not in placeholders, f"{entity} maps to a placeholder"

    for anchor in ("sessionsource", "pairingstore", "slash_access", "hosted_rooms", "ui_meta"):
        assert anchor in low, f"missing Hermes anchor: {anchor}"
    assert "allow_from" in low or "per-platform allowlist" in low, "missing allowlist anchor"
    assert "profile_routes" in low or "profile routing" in low, "missing profile-routing anchor"
    assert "gov-f26" in low or "elevated-orchestrator" in low or "elevated orchestrator" in low
    assert "per profile" in low and "per (human, project)" in low
    assert "room administration" in low
    assert "one-time operator bring-up" in low
    # hosted_rooms (authoritative) vs ui_meta (projection) must be stated together, not scattered
    paras = [p.lower() for p in raw.split("\n\n")]
    assert any(all(t in p for t in ("hosted_rooms", "ui_meta", "authoritative", "projection")) for p in paras), \
        "doc must state, in one place, that hosted_rooms is authoritative and ui_meta a projection"
