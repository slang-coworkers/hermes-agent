"""Acceptance test for RT-F03 — Session modes (shared | per-thread) + the native
agent-shared Bot Chat lane, rendered GATEWAY-WIDE by nv-coworker-compose.

One ``test_ac_rt_f03_<n>`` per ADR §Acceptance criteria pytest row (AC-1..8); each
docstring's first physical line is ``AC-RT-F03-<n>: `` plus the ADR criterion-column text
VERBATIM (the docstring↔criterion join key). AC-9 is a ``desktop:`` criterion
(apps/desktop/e2e/rt-f03-ac9.spec.ts) and has no function here.

The plugin is loaded from an isolated HERMES_HOME with an EMPTY bundled dir through the
real ``PluginManager().discover_and_load()`` path (shape: tests/hermes_cli/
test_plugin_api_compat.py:14-52). ``compose()`` is driven and the render asserted through
the REAL readers: ``gateway.config.GatewayConfig.from_dict`` for the store-lane flags (the
one config the multiplex SessionStore consumes) and ``gateway.config.load_gateway_config``
for the EFFECTIVE merged adapter-lane extra (the alias merge at gateway/config.py:1599-1637).
AC-5/6/8 additionally build the real ``gateway.session.SessionStore`` from the rendered
DEFAULT config, so the split-brain-free claim is proven end-to-end (gateway/run.py:7474 →
gateway/session.py:2010-2017), not merely at the ``build_session_key`` helper.
"""

from __future__ import annotations

import copy
import shutil
import threading
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_KEY = "nv-coworker-compose"
PLUGIN_SRC = REPO_ROOT / "plugins" / PLUGIN_KEY
DOC_PAGE = REPO_ROOT / "website" / "docs" / "user-guide" / "fleet-session-modes.md"

# mode -> the (group_sessions_per_user, thread_sessions_per_user) the render must emit.
_MODE_FLAGS = {
    "shared": (False, False),
    "per-thread": (True, False),
}
_ALIAS_ENV: tuple = ()  # no platform env gates are consulted by the session-mode render


# ── Fixture spec and loader ──────────────────────────────────────────────────────────
def _write_fixture_spec(spec_dir: Path, spec: dict) -> Path:
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spines").mkdir(exist_ok=True)
    (spec_dir / "spines" / "base.yaml").write_text(
        yaml.safe_dump({"identity": "Base.", "invariants": ["Be helpful."],
                        "workflows": ["wf"], "config": {}}, sort_keys=False),
        encoding="utf-8")
    wf = spec_dir / "workflows" / "wf"
    wf.mkdir(parents=True, exist_ok=True)
    (wf / "SKILL.md").write_text("# wf\n\nDo the work.\n", encoding="utf-8")
    (spec_dir / "skills").mkdir(exist_ok=True)
    p = spec_dir / "coworker-types.yaml"
    p.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    return p


def _base_spec() -> dict:
    return {
        "orchestrator_profile": "orchestrator",
        "default_profile": "default",
        "spines": {"base": {"source": "spines/base.yaml"}},
        "types": {"orchestrator": {"extends": ["base"], "config": {}},
                  "worker": {"extends": ["base"], "config": {}}},
        "default_config": {},
    }


def _spec(fleet_mode=None, worker_mode=None, orch_mode=None,
          worker_config=None, orch_config=None, default_extra=None) -> dict:
    """Build a spec with an optional fleet-level session_mode (default_config.session_mode),
    optional per-coworker restatements (worker/orchestrator config.session_mode), and optional
    extra worker/orchestrator/default config to plant hostile/alias values."""
    spec = _base_spec()
    if fleet_mode is not None:
        spec["default_config"]["session_mode"] = fleet_mode
    if worker_mode is not None:
        spec["types"]["worker"]["config"]["session_mode"] = worker_mode
    if orch_mode is not None:
        spec["types"]["orchestrator"]["config"]["session_mode"] = orch_mode
    if worker_config:
        spec["types"]["worker"]["config"].update(copy.deepcopy(worker_config))
    if orch_config:
        spec["types"]["orchestrator"]["config"].update(copy.deepcopy(orch_config))
    if default_extra:
        spec["default_config"].update(copy.deepcopy(default_extra))
    return spec


@pytest.fixture()
def loaded(tmp_path, monkeypatch):
    from hermes_cli.plugins import PluginManager

    hermes_home = tmp_path / "hermes_home"
    (hermes_home / "plugins").mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, hermes_home / "plugins" / PLUGIN_KEY)
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": [PLUGIN_KEY]}}), encoding="utf-8")
    bundled = tmp_path / "empty_bundled"
    bundled.mkdir()

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")

    manager = PluginManager()
    manager.discover_and_load()
    entry = manager._plugins[PLUGIN_KEY]
    assert entry.enabled is True
    assert entry.error is None
    assert entry.module is not None
    return entry.module, entry


def _render(module, tmp_path, spec: dict, name: str) -> Path:
    out = tmp_path / f"out_{name}"
    module.compose(str(_write_fixture_spec(tmp_path / f"spec_{name}", spec)), str(out))
    return out


def _read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _profile_config(out: Path, profile: str) -> dict:
    return _read_yaml(out / profile / "config.yaml")


def _gwflags(cfg: dict) -> tuple:
    """The (group_sessions_per_user, thread_sessions_per_user) the multiplex store reads,
    through the REAL parser gateway.config.GatewayConfig.from_dict."""
    from gateway.config import GatewayConfig
    gc = GatewayConfig.from_dict(cfg)
    return (gc.group_sessions_per_user, gc.thread_sessions_per_user)


def _top(cfg: dict, key: str) -> bool:
    """True iff the flag is written EXPLICITLY at the top level (where the render serializes
    the store-lane flags onto the DEFAULT profile)."""
    return key in cfg


def _global(cfg: dict, key: str) -> bool:
    """True iff the flag appears at a location the store reads as a GATEWAY-GLOBAL value
    (top-level or under gateway.*) — used to prove non-default profiles carry NO global copy."""
    if key in cfg:
        return True
    gw = cfg.get("gateway")
    return isinstance(gw, dict) and key in gw


def _effective_extra(profile_dir: Path, platform, monkeypatch) -> dict:
    """The EFFECTIVE merged PlatformConfig.extra the real loader consumes for a rendered
    profile — through gateway.config.load_gateway_config()'s alias merge (config.py:1599-1637),
    NOT a single raw location. HERMES_HOME is pointed at the rendered profile dir."""
    from gateway.config import load_gateway_config
    monkeypatch.setenv("HERMES_HOME", str(profile_dir))
    return dict(load_gateway_config().platforms[platform].extra)


def _at(node: dict, *path):
    """Walk a nested-dict path, returning None if any hop is missing/not-a-dict."""
    cur = node
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _make_store(tmp_path: Path, cfg: dict, tag: str):
    """A real gateway SessionStore built from the rendered DEFAULT config — the exact
    construction gateway/run.py:7474 uses (sessions_dir, GatewayConfig)."""
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    sdir = tmp_path / f"sessions_{tag}"
    sdir.mkdir(parents=True, exist_ok=True)
    return SessionStore(sdir, GatewayConfig.from_dict(cfg))


# ── AC-RT-F03-1 ──────────────────────────────────────────────────────────────────────
def test_ac_rt_f03_1(loaded, tmp_path):
    """AC-RT-F03-1: `session_mode: shared` (declared at `default_config.session_mode` or a coworker `config.session_mode`) renders the DEFAULT (multiplexer) profile's top-level `group_sessions_per_user: false` and `thread_sessions_per_user: false`; the `session_mode` pseudo-key is removed from every rendered profile."""
    module, _entry = loaded
    out = _render(module, tmp_path, _spec(fleet_mode="shared"), "shared")
    dflt = _profile_config(out, "default")
    # store lane on DEFAULT: flags false, written EXPLICITLY at the top level
    assert _gwflags(dflt) == (False, False)
    assert _top(dflt, "group_sessions_per_user") and _top(dflt, "thread_sessions_per_user")
    # DEFAULT is the serialization owner only: non-default profiles carry NO gateway-global copy
    for prof in ("orchestrator", "worker"):
        cfg = _profile_config(out, prof)
        assert not _global(cfg, "group_sessions_per_user"), prof
        assert not _global(cfg, "thread_sessions_per_user"), prof
    # the session_mode pseudo-key is removed from every rendered profile
    for prof in ("default", "orchestrator", "worker"):
        cfg = _profile_config(out, prof)
        assert "session_mode" not in cfg, prof
        assert "session_mode" not in cfg.get("default_config", {}), prof

    # a per-coworker restatement is equivalent and equally accepted
    out2 = _render(module, tmp_path, _spec(fleet_mode="shared", worker_mode="shared"), "shared2")
    assert _gwflags(_profile_config(out2, "default")) == (False, False)

    # an omitted default_config is not a conflicting opinion — a lone coworker declaration wins
    out3 = _render(module, tmp_path, _spec(worker_mode="shared"), "shared_coworker_only")
    assert _gwflags(_profile_config(out3, "default")) == (False, False)
    assert "session_mode" not in _profile_config(out3, "worker")


# ── AC-RT-F03-2 ──────────────────────────────────────────────────────────────────────
def test_ac_rt_f03_2(loaded, tmp_path):
    """AC-RT-F03-2: `session_mode: per-thread`, and an OMITTED session_mode, both render the explicit native defaults `group_sessions_per_user: true` and `thread_sessions_per_user: false` on the DEFAULT profile (no silent default)."""
    module, _entry = loaded
    for tag, spec in (("pt", _spec(fleet_mode="per-thread")), ("omit", _spec())):
        dflt = _profile_config(_render(module, tmp_path, spec, tag), "default")
        assert _gwflags(dflt) == (True, False), tag
        # explicit at the top level, not silently defaulted
        assert _top(dflt, "group_sessions_per_user"), tag
        assert _top(dflt, "thread_sessions_per_user"), tag


# ── AC-RT-F03-3 ──────────────────────────────────────────────────────────────────────
def test_ac_rt_f03_3(loaded, tmp_path, monkeypatch):
    """AC-RT-F03-3: After render, every profile that wires a platform has, in the EFFECTIVE merged adapter config the real loader consumes, the SAME `group_sessions_per_user`/`thread_sessions_per_user` as the DEFAULT store flags; a hostile raw declaration of either flag at any location the loader merges (`platforms.<p>.extra`, `gateway.platforms.<p>.extra`, `gateway.<p>.extra`) is stripped/overwritten to the gateway-wide value — the adapter lane cannot diverge from the store lane."""
    module, _entry = loaded
    from gateway.config import Platform

    # Prove BOTH modes. Each hostile block plants a value DIFFERENT from the mode's expected
    # flag at every location the loader merges into PlatformConfig.extra (gateway/config.py:
    # 1599-1637: gateway.<p>.extra wins LAST), so the render must overwrite in BOTH directions.
    # DEFAULT/webhook is covered too — a routeless webhook is valid (_validate_webhook_routes
    # returns early), so it renders.
    def _hostile(pkey, group_val):
        block = {"extra": {"group_sessions_per_user": group_val, "thread_sessions_per_user": True}}
        return {
            "platforms": {pkey: {"enabled": True, **copy.deepcopy(block)}},
            "gateway": {pkey: copy.deepcopy(block),
                        "platforms": {pkey: copy.deepcopy(block)}},
        }

    for mode, hostile_group, expected in (("shared", True, (False, False)),
                                          ("per-thread", False, (True, False))):
        out = _render(module, tmp_path,
                      _spec(fleet_mode=mode,
                            worker_config=_hostile("slack", hostile_group),
                            orch_config=_hostile("discord", hostile_group),
                            default_extra=_hostile("webhook", hostile_group)),
                      f"adapter_{mode}")
        store_flags = _gwflags(_profile_config(out, "default"))
        assert store_flags == expected, mode
        canon = {"group_sessions_per_user": expected[0], "thread_sessions_per_user": expected[1]}

        for prof, plat in (("worker", Platform.SLACK),
                           ("orchestrator", Platform.DISCORD),
                           ("default", Platform.WEBHOOK)):
            # the EFFECTIVE merged adapter extra, through the REAL loader alias-merge, == store flags
            eff = _effective_extra(out / prof, plat, monkeypatch)
            assert (eff["group_sessions_per_user"], eff["thread_sessions_per_user"]) == store_flags, (mode, prof)
            cfg = _profile_config(out, prof)
            # DEFAULT is the serialization owner (it legitimately carries the top-level store flags);
            # every NON-default profile must carry NO gateway-global copy.
            if prof != "default":
                assert not _global(cfg, "group_sessions_per_user"), (mode, prof)
                assert not _global(cfg, "thread_sessions_per_user"), (mode, prof)
            pkey = plat.value
            for flag, want in canon.items():
                assert _at(cfg, "platforms", pkey, "extra", flag) == want, (mode, prof, flag)
                assert _at(cfg, "gateway", "platforms", pkey, "extra", flag) is None, (mode, prof, flag)
                assert _at(cfg, "gateway", pkey, "extra", flag) is None, (mode, prof, flag)


# ── AC-RT-F03-4 ──────────────────────────────────────────────────────────────────────
def test_ac_rt_f03_4(loaded, tmp_path):
    """AC-RT-F03-4: The render compares NORMALIZED effective modes: identical explicit declarations — and an explicit-vs-omitted pair (omission is not a conflicting opinion, so a coworker-only declaration sets the gateway-wide mode) — are ACCEPTED; two genuinely divergent EXPLICIT declarations (any pair — `default_config` vs a coworker, OR two coworkers) raise `CompositionError` pre-startup; an unknown value, and `session_mode: agent-shared` (the native Bot Chat lane, not a flag mode), each raise `CompositionError`."""
    module, _entry = loaded
    # explicit-vs-omitted: fleet per-thread + coworkers omitted is NOT a conflict
    out = _render(module, tmp_path, _spec(fleet_mode="per-thread"), "normeq")
    assert _gwflags(_profile_config(out, "default")) == (True, False)
    # equal-explicit: both explicitly shared
    _render(module, tmp_path, _spec(fleet_mode="shared", worker_mode="shared"), "normeq2")
    # coworker-only (default omits) sets the gateway-wide mode
    out2 = _render(module, tmp_path, _spec(worker_mode="shared"), "coworker_only")
    assert _gwflags(_profile_config(out2, "default")) == (False, False)

    # divergence, any pair, fails closed pre-startup
    with pytest.raises(module.CompositionError):  # default_config vs a coworker
        _render(module, tmp_path, _spec(fleet_mode="shared", worker_mode="per-thread"), "div_default_coworker")
    with pytest.raises(module.CompositionError):  # two coworkers, default omitted
        _render(module, tmp_path, _spec(worker_mode="shared", orch_mode="per-thread"), "div_coworker_coworker")
    # unknown value and agent-shared (a native lane, not a flag mode) each fail closed
    with pytest.raises(module.CompositionError):
        _render(module, tmp_path, _spec(fleet_mode="banter"), "unknown")
    with pytest.raises(module.CompositionError):
        _render(module, tmp_path, _spec(fleet_mode="agent-shared"), "agent_shared_rejected")


# ── AC-RT-F03-5 ──────────────────────────────────────────────────────────────────────
def test_ac_rt_f03_5(loaded, tmp_path):
    """AC-RT-F03-5: A `SessionStore` built from the rendered DEFAULT config through the real gateway path keys two different senders in one non-thread group chat to the SAME session under `shared`, and to DISTINCT sessions under `per-thread`/default — the shared store consumes the rendered flags."""
    module, _entry = loaded
    from gateway.config import Platform
    from gateway.session import SessionSource

    def _grp(user):
        return SessionSource(platform=Platform.DISCORD, chat_id="g1", chat_type="group", user_id=user)

    shared_default = _profile_config(_render(module, tmp_path, _spec(fleet_mode="shared"), "s5"), "default")
    store_shared = _make_store(tmp_path, shared_default, "shared")
    assert store_shared._generate_session_key(_grp("alice")) == store_shared._generate_session_key(_grp("bob"))

    pt_default = _profile_config(_render(module, tmp_path, _spec(fleet_mode="per-thread"), "p5"), "default")
    store_pt = _make_store(tmp_path, pt_default, "perthread")
    assert store_pt._generate_session_key(_grp("alice")) != store_pt._generate_session_key(_grp("bob"))


# ── AC-RT-F03-6 ──────────────────────────────────────────────────────────────────────
def test_ac_rt_f03_6(loaded, tmp_path):
    """AC-RT-F03-6: Across profile × platform × user × group/DM × thread, keys derived through the real `SessionStore._generate_session_key` (built from each rendered mode's DEFAULT config, multiplex on) produce the documented equalities/inequalities: a group chat's senders collapse under shared and split under per-thread; distinct `thread_id`s always key distinctly and one thread is shared across participants; DM keys are computed independently of the mode flags (identified DMs isolated per chat, the no-`chat_id` fallback per participant); two profiles never share a key; two platforms never share a key."""
    module, _entry = loaded
    from gateway.config import Platform
    from gateway.session import SessionSource

    shared_default = _profile_config(_render(module, tmp_path, _spec(fleet_mode="shared"), "s6"), "default")
    pt_default = _profile_config(_render(module, tmp_path, _spec(fleet_mode="per-thread"), "p6"), "default")
    assert _gwflags(shared_default) == (False, False) and _gwflags(pt_default) == (True, False)
    # the rendered DEFAULT config carries gateway.multiplex_profiles: true, so the store's
    # _resolve_profile_for_key honors source.profile (gateway/session.py:1952-1969).
    store_shared = _make_store(tmp_path, shared_default, "s6")
    store_pt = _make_store(tmp_path, pt_default, "p6")

    def k(store, src):
        return store._generate_session_key(src)

    def grp(user, thread=None, profile="pa", platform=Platform.DISCORD):
        return SessionSource(platform=platform, chat_id="g1", chat_type="group",
                             user_id=user, thread_id=thread, profile=profile)

    def dm(user, chat, profile="pa"):
        return SessionSource(platform=Platform.DISCORD, chat_id=chat, chat_type="dm",
                             user_id=user, profile=profile)

    # group non-thread: shared collapses senders; per-thread splits them
    assert k(store_shared, grp("a")) == k(store_shared, grp("b"))
    assert k(store_pt, grp("a")) != k(store_pt, grp("b"))
    # distinct thread_ids always key distinctly; one thread is shared across participants (both modes)
    for store in (store_shared, store_pt):
        assert k(store, grp("a", "t1")) != k(store, grp("a", "t2"))
        assert k(store, grp("a", "t1")) == k(store, grp("b", "t1"))
    # DM keys are mode-independent: identified DM isolated PER CHAT (same participant, distinct
    # chat ids -> distinct keys), and the no-chat_id fallback keys per participant — every DM key
    # identical across the two modes.
    assert k(store_shared, dm("a", "d-1")) == k(store_pt, dm("a", "d-1"))     # mode-independent
    assert k(store_shared, dm("a", "d-1")) != k(store_shared, dm("a", "d-2"))  # isolated per chat id
    assert k(store_shared, dm("a", None)) == k(store_pt, dm("a", None))       # participant fallback, mode-independent
    assert k(store_shared, dm("a", None)) != k(store_shared, dm("b", None))   # distinct participants distinct
    # cross-profile isolation (multiplex on): same source, different profile -> different key
    for store in (store_shared, store_pt):
        assert k(store, grp("a", profile="pa")) != k(store, grp("a", profile="pb"))
    # cross-platform: two platforms never share a key
    for store in (store_shared, store_pt):
        assert k(store, grp("a", platform=Platform.DISCORD)) != k(store, grp("a", platform=Platform.TELEGRAM))


# ── AC-RT-F03-7 ──────────────────────────────────────────────────────────────────────
def test_ac_rt_f03_7(loaded, tmp_path):
    """AC-RT-F03-7: The plugin registers no session-identity seam — `provides_hooks` is empty and neither `pre_gateway_dispatch` nor `on_session_start` is in the loaded plugin's registered hooks — so `build_session_key` remains the sole key authority; the doc assigns `^gh` per-artifact continuity to CH-F52/GOV-F25 (kanban card), not a rewritten key."""
    module, entry = loaded
    hooks = set(getattr(entry, "hooks_registered", []) or [])
    assert "pre_gateway_dispatch" not in hooks
    assert "on_session_start" not in hooks
    manifest_hooks = getattr(entry, "provides_hooks", None)
    if manifest_hooks is None:
        manifest_hooks = getattr(getattr(entry, "manifest", None), "provides_hooks", []) or []
    assert list(manifest_hooks) == []

    assert DOC_PAGE.is_file(), f"missing doc page {DOC_PAGE}"
    text = DOC_PAGE.read_text(encoding="utf-8")
    assert "CH-F52" in text and "GOV-F25" in text, "^gh continuity ownership not documented"
    assert "kanban" in text.lower()
    assert "gh-pr" in text.lower() or "gh-issue" in text.lower() or "^gh" in text
    assert "build_session_key" in text  # named as the sole key authority


# ── AC-RT-F03-8 ──────────────────────────────────────────────────────────────────────
def test_ac_rt_f03_8(loaded, tmp_path, monkeypatch):
    """AC-RT-F03-8: Native `_SessionFlight` creation lock: concurrent `get_or_create_session` for one key returns one shared session (a single created row); different keys create independent sessions; a failing in-flight creation RELEASES the flight so a later call for the same key succeeds."""
    module, _entry = loaded
    import gateway.session as gwsession
    from gateway.config import Platform
    from gateway.session import SessionSource

    default_cfg = _profile_config(_render(module, tmp_path, _spec(fleet_mode="shared"), "s8"), "default")
    store = _make_store(tmp_path, default_cfg, "flight")

    src = SessionSource(platform=Platform.DISCORD, chat_id="g8", chat_type="group", user_id="alice")
    other = SessionSource(platform=Platform.DISCORD, chat_id="g9", chat_type="group", user_id="alice")

    n = 8
    lock = threading.Lock()
    results: list = []
    errors: list = []
    waiters = {"n": 0}
    started = threading.Event()     # the sole owner sets this when it enters creation
    release = threading.Event()     # main releases the owner once all non-owners are queued
    all_queued = threading.Event()  # set deterministically when the (n-1)th non-owner blocks
    create_calls = {"n": 0}

    # Deterministic waiter count (no sleep): a _SessionFlight whose event.wait() is counted.
    # Only NON-owners wait on the flight, so the owner is held until exactly n-1 are blocked.
    real_flight = gwsession._SessionFlight

    class CountingFlight(real_flight):
        def __init__(self):
            super().__init__()
            _wait = self.event.wait

            def counting_wait(timeout=None):
                with lock:
                    waiters["n"] += 1
                    if waiters["n"] == n - 1:
                        all_queued.set()
                return _wait(timeout)

            self.event.wait = counting_wait

    monkeypatch.setattr(gwsession, "_SessionFlight", CountingFlight)

    real_impl = store._get_or_create_session_impl

    def counting_impl(source, **kw):
        # Only the single-flight OWNER reaches the impl; non-owners wait on the flight and
        # return the owner's result. Holding the owner here keeps the flight open so the
        # fan-in is exercised — proving the LOCK, not just idempotency.
        with lock:
            create_calls["n"] += 1
        started.set()
        release.wait(timeout=15)
        return real_impl(source, **kw)

    store._get_or_create_session_impl = counting_impl

    def worker():
        try:
            entry = store.get_or_create_session(src)
            with lock:
                results.append((entry.session_key, entry.session_id))
        except BaseException as exc:  # noqa: BLE001 — surface any failure to the assert
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    try:
        for t in threads:
            t.start()
        assert started.wait(timeout=10), "single-flight owner never entered creation"
        # deterministic: block until exactly the n-1 non-owners are waiting on slot.event
        assert all_queued.wait(timeout=15), "not all non-owners queued on the flight"
        release.set()
        for t in threads:
            t.join(timeout=15)
    finally:
        store._get_or_create_session_impl = real_impl
        release.set()

    assert not errors, errors
    assert len(results) == n
    assert create_calls["n"] == 1, "single-flight must create exactly ONCE for N concurrent callers"
    # one created row: session_id (the unique row identity) is identical for every caller.
    assert len({sid for _k, sid in results}) == 1, "all callers share one session row"
    assert len({k for k, _sid in results}) == 1

    assert store.get_or_create_session(other).session_id != results[0][1]

    boom = SessionSource(platform=Platform.DISCORD, chat_id="g-fail", chat_type="group", user_id="alice")
    real_impl = store._get_or_create_session_impl
    calls = {"n": 0}

    def flaky(source, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real_impl(source, **kw)

    store._get_or_create_session_impl = flaky
    try:
        with pytest.raises(RuntimeError):
            store.get_or_create_session(boom)
        # flight released — no deadlock, key not retained
        assert not store._inflight_sessions.get(store._generate_session_key(boom))
        assert store.get_or_create_session(boom).session_key == store._generate_session_key(boom)
    finally:
        store._get_or_create_session_impl = real_impl
