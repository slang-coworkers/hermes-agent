"""Acceptance test for COST-F30 (Option C) — plugin-owned cost-escalation decision.

Extends the merged ``nv-cost-cap`` plugin (COST-F29). Option C is PORT-as-plugin with
ZERO core change: the decision is a plugin-owned native card in the dashboard/desktop
panel (dashboard SDK) + a `/cost` path on messaging gateways, all resolved through ONE
plugin authz+CAS+apply chokepoint (`resolve_escalation`) backed by the plugin's own
`plugin_db`. No built-in approval queue, no `ctx.request_approval`.

Loaded through the REAL discovery path (shape: tests/hermes_cli/test_plugin_api_compat.py)
from an isolated HERMES_HOME with an empty bundled dir; drives the plugin's own public
functions and asserts each acceptance criterion's OBSERVABLE effect (the kind's window
baseline advanced by an EXACT amount, or blocked flipped), not merely a ledger row.

The matrix proves C's FULL scope, not just the escalation kind:
  * AC-1 is parameterized across all four F29 episode kinds — escalation (Tier-1, unblocked),
    breach + unknown_pricing (Tier-2 mortal, blocked), daily (immortal, unblocked) — and, per
    kind, additionally fires TWO synchronized concurrent resolves to prove competing-click CAS
    (the pill-double-grant P0 under a real race, not just sequentially).
  * AC-4 uses the REAL immortal kind ``daily`` (Continue-only, Stop refused) AND an ordinary
    mortal escalation Stop (honored: blocks the session; a later Continue loses the CAS).
  * AC-2 proves the surface-NAMESPACED-principal authz on BOTH surfaces that funnel through the
    chokepoint — `dashboard:<provider>:<org_id>:<user_id>` from the server session
    (`principal_from_request`) and `gateway:<platform.value>:<scope>:<user_id>` from the event
    (`principal_from_event`) — never a client-supplied one; a BARE un-namespaced user_id AND a
    same-user DIFFERENT-workspace principal are both refused (cross-surface + workspace isolation).
    Operators are read context-free via load_config_readonly() so the ctx-free dashboard-import
    path authorizes identically.
  * AC-8 (the live gateway path via a real `pre_gateway_dispatch` `/cost` event) is the
    scenario artifact in the ADR, not a function here.

COST-F30 (Option C) API the builder re-exports at the ``nv-cost-cap`` package level:
  * store.claim_resolution(episode_id, session_id, budget_gen, decision, actor, amount_usd=None) -> bool
        one BEGIN IMMEDIATE tx (connection sets PRAGMA busy_timeout so a competing writer
        serializes, then sees the row and loses): rejects a closed session / mismatched pair /
        duplicate; stores the ABSOLUTE effect target; True iff this caller claimed it (the CAS).
  * store.resolutions(session_id) -> list[dict]  (episode_id/decision/actor/amount_usd/status/reason)
  * resolve_escalation(session_id, episode_id, budget_gen, decision, principal, *, amount_usd=None) -> dict
        the ONE authz+CAS+apply chokepoint. Authorizes the NAMESPACED `principal` string against
        the operators read via load_config_readonly() (context-free; honours the current/pinned
        HERMES_HOME), so both the ctx-free dashboard route and the ambient-scoped gateway path
        authorize identically; no bare-id fallback. Continue advances the kind's baseline
        (daily -> day_start_total, else window_start_total) by escalation_increment_usd and clears
        blocked; a mortal Stop blocks the session; an immortal Stop is refused.
  * handle_cost_command(session_id, text, principal, *, profile=None) -> dict
        the gateway `/cost continue|stop|ceiling <exact-usd>` seam; the pre_gateway_dispatch caller
        builds the principal via principal_from_event(event); resolves the session's latest pending
        episode — continue/stop through resolve_escalation, ceiling through set_episode_ceiling.
  * principal_from_event(event) -> str | None
        the gateway principal seam: f"gateway:{platform.value}:{scope}:{user_id}" where
        platform = getattr(source.platform, "value", source.platform) (a Platform enum → "slack",
        never "Platform.SLACK") and scope = source.scope_id or source.guild_id or "-" (workspace
        isolation, so the same user_id in a different workspace is a DIFFERENT principal).
  * principal_from_request(request) -> str | None
        the dashboard route's principal seam: returns f"dashboard:{s.provider}:{s.org_id}:{s.user_id}"
        from request.state.session (server-set) or None (loopback/no session). NEVER reads the body.
  * set_episode_ceiling(profile, session_id, episode_id, budget_gen, amount, principal) -> dict
        CONTEXT-FREE (must not need _CTX — the dashboard route has none): writes the exact ceiling
        into the pinned profile's config via the context-free config.save_config path.
        exact-float, profile-scoped; rejects a multiplier/invalid amount.
  * reconcile_once() -> int   — finalise closed-unresolved, re-apply claimed-but-unapplied.
  * _on_session_finalize(session_id=...)   — marks a session truly closed.

Fails on the stock (COST-F29) tree — those symbols do not exist. Behavior contract, no
network, nothing under ~/.hermes.
"""

import contextlib
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from hermes_cli.plugins import PluginManager

PLUGIN_KEY = "nv-cost-cap"
REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_SRC = REPO_ROOT / "plugins" / PLUGIN_KEY

# Principals are SURFACE-NAMESPACED (a bare user_id is never an operator): dashboard
# "dashboard:<provider>:<org_id>:<user_id>", gateway "gateway:<platform.value>:<scope>:<user_id>".
# OTHER_SCOPE is the same user in a different workspace, BARE_OP the un-namespaced id — both refused.
OP_DASH = "dashboard:oauth:org-1:op-1"
OPERATOR = "gateway:slack:ws-1:op-1"
STRANGER = "gateway:slack:ws-1:stranger"
OTHER_SCOPE = "gateway:slack:ws-2:op-1"
BARE_OP = "op-1"
# PROFA_OP is the target profile's operator, DISJOINT from the default config's operators:
# its user_id "profa-op" is absent from the default set (op-1), so a default-only principal can be
# proved DENIED for a profa request — the router MUST authorize against the pinned ?profile, not default.
PROFA_OP = "dashboard:oauth:org-1:profa-op"
TODAY = "2026-09-19"
INCREMENT_USD = 5.0

# One tuple per F29 episode kind: (kind, immortal, blocked_at_raise, baseline_field).
# escalation is Tier-1 (never blocks); breach/unknown_pricing are Tier-2 mortal (block);
# daily is the immortal per-day kind (never blocks, advances the day baseline).
KIND_CASES = [
    ("escalation", False, False, "window_start_total"),
    ("breach", False, True, "window_start_total"),
    ("unknown_pricing", False, True, "window_start_total"),
    ("daily", True, False, "day_start_total"),
]
_TIER2_MORTAL = {"breach", "unknown_pricing"}


@pytest.fixture
def loaded_plugin(tmp_path, monkeypatch):
    # FUNCTION-scoped and using the injected monkeypatch so it runs AFTER conftest's
    # function-scoped autouse HERMES_HOME sandbox and its setenv wins — a module-scoped
    # fixture would have its HERMES_HOME replaced per-function, leaving operators unreadable.
    if not PLUGIN_SRC.is_dir():
        pytest.fail(f"plugin source not found at {PLUGIN_SRC}")

    hermes_home = tmp_path / "home"
    (hermes_home / "plugins").mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, hermes_home / "plugins" / PLUGIN_KEY)

    cfg = {
        "plugins": {
            "enabled": [PLUGIN_KEY],
            "entries": {
                PLUGIN_KEY: {
                    "settings": {
                        "operators": [OP_DASH, OPERATOR],
                        "escalation_increment_usd": INCREMENT_USD,
                        "escalation_reconcile_seconds": 3600,
                    }
                }
            },
        }
    }
    (hermes_home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")

    bundled = tmp_path / "empty_bundled"
    bundled.mkdir()

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")

    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled is True, getattr(loaded, "error", None)
    assert loaded.error is None
    assert loaded.module is not None
    assert {"pre_gateway_dispatch", "on_session_finalize"} <= set(loaded.hooks_registered)
    return loaded


def _seed_episode(mod, session_id, *, kind="escalation", budget_gen=1, immortal=None, blocked=None):
    """Seed one pending episode of ``kind`` with the state F29 would leave at its raise.

    ``immortal``/``blocked`` default from the kind so each case is realistic: daily is
    immortal + unblocked; breach/unknown_pricing are mortal + blocked; escalation is
    mortal + unblocked. Callers override to build the AC-4 mortal-Stop case.
    """
    if immortal is None:
        immortal = kind == "daily"
    if blocked is None:
        blocked = kind in _TIER2_MORTAL
    store = mod.store
    store.set_state(
        session_id,
        effective_usd=10.0,
        window_start_total=0.0,
        day_start_total=0.0,
        budget_gen=budget_gen,
        blocked=1 if blocked else 0,
        immortal=1 if immortal else 0,
    )
    episode_id = store.record_episode(
        session_id, kind, budget_gen, TODAY,
        dedup_key=f"{session_id}:{kind}:{budget_gen}",
    )
    assert episode_id, f"seed: {kind} episode should be newly recorded"
    return episode_id


@contextlib.contextmanager
def _pinned_home(profile):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_cli.profiles import get_profile_dir

    token = set_hermes_home_override(str(get_profile_dir(profile)))
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def _profile_ceiling(profile):
    from hermes_cli.profiles import get_profile_dir

    cfg_path = Path(get_profile_dir(profile)) / "config.yaml"
    if not cfg_path.exists():
        return None
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    return (
        data.get("plugins", {}).get("entries", {}).get(PLUGIN_KEY, {})
        .get("settings", {}).get("profile_ceiling_usd")
    )


def _write_profile_ceiling(profile, value, operators=None):
    """Seed a pre-existing ceiling in ``profile``'s config so a set-ceiling can be shown scoped.

    ``operators`` defaults to the same namespaced set as the default config; pass a DISJOINT list
    to prove profile-scoped authz isolation (AC-9). ``escalation_increment_usd`` is seeded so a
    Continue resolved under the pinned profile advances the baseline by the same INCREMENT_USD.
    """
    from hermes_cli.profiles import get_profile_dir

    d = Path(get_profile_dir(profile))
    d.mkdir(parents=True, exist_ok=True)
    cfg_path = d / "config.yaml"
    data = (yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}) or {}
    settings = data.setdefault("plugins", {}).setdefault("entries", {}).setdefault(PLUGIN_KEY, {}).setdefault("settings", {})
    settings["profile_ceiling_usd"] = value
    # the target profile is itself a configured plugin profile, so authz under its pinned home
    # sees the configured namespaced operator (set_episode_ceiling may check operators before or after pinning).
    settings["operators"] = list(operators) if operators is not None else [OP_DASH, OPERATOR]
    settings.setdefault("escalation_increment_usd", INCREMENT_USD)
    cfg_path.write_text(yaml.safe_dump(data), encoding="utf-8")


def _base_ceiling():
    import os

    cfg_path = Path(os.environ["HERMES_HOME"]) / "config.yaml"
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    return (
        data.get("plugins", {}).get("entries", {}).get(PLUGIN_KEY, {})
        .get("settings", {}).get("profile_ceiling_usd")
    )


def _applied(mod, session_id):
    return [r for r in mod.store.resolutions(session_id) if r["status"] == "applied"]


def _baseline(mod, session_id, field):
    return dict(mod.store.get_state(session_id))[field]


def _fake_event(platform_value, scope_id, user_id):
    """A minimal pre_gateway_dispatch event; source.platform is a Platform-like enum (.value)."""
    source = SimpleNamespace(
        platform=SimpleNamespace(value=platform_value), scope_id=scope_id, guild_id=None, user_id=user_id
    )
    return SimpleNamespace(source=source)


# --- acceptance tests ------------------------------------------------------


@pytest.mark.parametrize("kind,immortal,blocked,baseline", KIND_CASES)
def test_ac_cost_f30_1(loaded_plugin, kind, immortal, blocked, baseline):
    """AC-COST-F30-1: Resolving one crossing episode twice on the same (episode_id, budget_gen) — sequentially or via two concurrent clicks — applies the Continue effect exactly once, for every F29 episode kind."""
    mod = loaded_plugin.module

    # (a) sequential double-resolve: the Continue effect applies exactly once, on the
    # baseline the kind gates (daily -> day_start_total, else window_start_total).
    seq = f"sess-f30-1-{kind}-seq"
    ep = _seed_episode(mod, seq, kind=kind)
    seeded = dict(mod.store.get_state(seq))
    assert seeded["immortal"] == (1 if immortal else 0), (kind, seeded)
    assert seeded["blocked"] == (1 if blocked else 0), (kind, seeded)
    start = _baseline(mod, seq, baseline)
    first = mod.resolve_escalation(seq, ep, 1, "continue", OPERATOR)
    assert first["granted"] is True, first
    after = dict(mod.store.get_state(seq))
    assert after[baseline] == pytest.approx(start + INCREMENT_USD), (kind, after)
    assert after["blocked"] == 0
    assert len(_applied(mod, seq)) == 1

    second = mod.resolve_escalation(seq, ep, 1, "continue", OPERATOR)
    assert second["granted"] is False and second.get("reason") == "already-resolved", second
    assert dict(mod.store.get_state(seq)) == after, "no second application"
    assert len(_applied(mod, seq)) == 1, "exactly one increment (pill-double-grant defect)"

    # (b) two synchronized concurrent resolves of the SAME episode: the CAS + the store's
    # busy_timeout serialize them so exactly one grants and exactly one increment lands.
    con = f"sess-f30-1-{kind}-con"
    ep2 = _seed_episode(mod, con, kind=kind)
    base2 = _baseline(mod, con, baseline)
    barrier = threading.Barrier(2)
    results = []

    def _race():
        barrier.wait()
        results.append(mod.resolve_escalation(con, ep2, 1, "continue", OPERATOR))

    threads = [threading.Thread(target=_race) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in threads), "a resolve thread hung (busy_timeout too short?)"
    granted = [r for r in results if r.get("granted") is True]
    lost = [r for r in results if r.get("granted") is False]
    assert len(granted) == 1 and len(lost) == 1, results
    assert lost[0].get("reason") == "already-resolved", lost
    assert _baseline(mod, con, baseline) == pytest.approx(base2 + INCREMENT_USD), con
    assert len(_applied(mod, con)) == 1, "exactly one increment under the competing-click race"


def test_ac_cost_f30_2(loaded_plugin):
    """AC-COST-F30-2: A resolution is authorized against structured operators using a surface-namespaced principal on both surfaces; a non-operator, a bare un-namespaced user_id, or the same user_id in a different workspace scope applies no effect while a matching namespaced operator is granted."""
    mod = loaded_plugin.module

    # Dashboard seam: the namespaced principal is built from the server session, never the body.
    session_ok = SimpleNamespace(state=SimpleNamespace(
        session=SimpleNamespace(provider="oauth", org_id="org-1", user_id="op-1")))
    session_ok.body = {"user": "attacker", "provider": "evil", "org_id": "evil"}  # decoy: must be ignored
    assert mod.principal_from_request(session_ok) == OP_DASH, "principal must be the namespaced session id, not the body"
    loopback = SimpleNamespace(state=SimpleNamespace(session=None))
    assert mod.principal_from_request(loopback) is None, "no session -> None (fail-closed)"

    # A BARE user_id must be refused even though "op-1" is the user_id inside every operator entry
    # (the cross-surface-collision defense), and so must a namespaced non-operator.
    sid = "sess-f30-2"
    ep = _seed_episode(mod, sid, kind="breach")
    before = dict(mod.store.get_state(sid))
    for bad in (BARE_OP, STRANGER, OTHER_SCOPE):
        denied = mod.resolve_escalation(sid, ep, 1, "continue", bad)
        assert denied["granted"] is False and denied.get("reason") == "unauthorized", (bad, denied)
    assert mod.store.resolutions(sid) == []
    assert dict(mod.store.get_state(sid)) == before

    # Gateway `/cost` seam: principal_from_event builds gateway:<platform.value>:<scope_id>:<user_id>
    # (a Platform enum must stringify to its .value, not "Platform.SLACK"); a same-user but
    # DIFFERENT-workspace principal is refused (scope_id isolation).
    ok_ev = _fake_event("slack", "ws-1", "op-1")
    assert mod.principal_from_event(ok_ev) == OPERATOR, mod.principal_from_event(ok_ev)
    assert mod.principal_from_event(_fake_event("slack", "ws-2", "op-1")) == OTHER_SCOPE
    gsid = "sess-f30-2-gw"
    _seed_episode(mod, gsid, kind="breach")
    gw_before = dict(mod.store.get_state(gsid))
    for bad in (BARE_OP, STRANGER, OTHER_SCOPE):
        gdenied = mod.handle_cost_command(gsid, "/cost continue", bad)
        assert gdenied["granted"] is False and gdenied.get("reason") == "unauthorized", (bad, gdenied)
    assert mod.store.resolutions(gsid) == []
    assert dict(mod.store.get_state(gsid)) == gw_before
    ggranted = mod.handle_cost_command(gsid, "/cost continue", mod.principal_from_event(ok_ev))
    assert ggranted["granted"] is True, ggranted
    assert len(_applied(mod, gsid)) == 1

    # The dashboard-namespaced operator grants through the same chokepoint.
    granted = mod.resolve_escalation(sid, ep, 1, "continue", OP_DASH)
    assert granted["granted"] is True, granted
    assert len(_applied(mod, sid)) == 1


def test_ac_cost_f30_3(loaded_plugin):
    """AC-COST-F30-3: An operator set-ceiling takes the exact USD value scoped to the target profile only, resolving the episode; a multiplier/invalid arg is rejected with no mutation."""
    mod = loaded_plugin.module
    from hermes_cli.profiles import get_profile_dir

    for name in ("profa", "profb"):
        Path(get_profile_dir(name)).mkdir(parents=True, exist_ok=True)
    # Distinct pre-existing ceilings so we can prove ONLY profa moves; base stays unset.
    _write_profile_ceiling("profa", 7.0)
    _write_profile_ceiling("profb", 9.0)

    str_session = "sess-f30-3-str"
    with _pinned_home("profa"):
        str_ep = _seed_episode(mod, str_session, budget_gen=1)
    unauth = mod.set_episode_ceiling("profa", str_session, str_ep, 1, "12.50", STRANGER)
    assert unauth["granted"] is False and unauth.get("reason") == "unauthorized", unauth
    with _pinned_home("profa"):
        assert mod.store.resolutions(str_session) == []
    assert _profile_ceiling("profa") == pytest.approx(7.0), "unauthorized must not change profa"
    assert _profile_ceiling("profb") == pytest.approx(9.0), "unauthorized must not touch profb"
    assert _base_ceiling() is None, "unauthorized must not touch base"

    ok_session = "sess-f30-3-ok"
    with _pinned_home("profa"):
        ok_ep = _seed_episode(mod, ok_session, budget_gen=1)
    exact = mod.set_episode_ceiling("profa", ok_session, ok_ep, 1, "12.50", OPERATOR)
    assert exact["granted"] is True, exact
    with _pinned_home("profa"):
        rows = mod.store.resolutions(ok_session)
    assert len(rows) == 1 and rows[0]["decision"] == "ceiling"
    assert rows[0]["amount_usd"] == pytest.approx(12.50), rows[0]
    assert _profile_ceiling("profa") == pytest.approx(12.50), "target profile ceiling written exactly"
    assert _profile_ceiling("profb") == pytest.approx(9.0), "other profile must be unchanged"
    assert _base_ceiling() is None, "base home ceiling must stay unset"
    assert mod.store.resolutions(ok_session) == [], "base home must hold no resolution row"

    # The gateway `/cost ceiling <usd>` seam routes through set_episode_ceiling: same operator
    # gate, same exact-float parse (profile = the running profile, pinned to profa here).
    gw_session = "sess-f30-3-gw"
    with _pinned_home("profa"):
        _seed_episode(mod, gw_session, budget_gen=1)
        gw_unauth = mod.handle_cost_command(gw_session, "/cost ceiling 3.25", STRANGER, profile="profa")
        assert gw_unauth["granted"] is False and gw_unauth.get("reason") == "unauthorized", gw_unauth
        gw = mod.handle_cost_command(gw_session, "/cost ceiling 3.25", OPERATOR, profile="profa")
        assert gw["granted"] is True and gw.get("decision") == "ceiling", gw
        gw_rows = mod.store.resolutions(gw_session)
    assert len(gw_rows) == 1 and gw_rows[0]["amount_usd"] == pytest.approx(3.25), gw_rows
    assert _profile_ceiling("profa") == pytest.approx(3.25), "gateway /cost ceiling sets the exact value"

    for bad in ("2x", "x2", "+50%", "1e999", "-3", "0"):
        bad_session = f"sess-f30-3-{bad}"
        with _pinned_home("profa"):
            bad_ep = _seed_episode(mod, bad_session, budget_gen=1)
        rejected = mod.set_episode_ceiling("profa", bad_session, bad_ep, 1, bad, OPERATOR)
        assert rejected["granted"] is False, (bad, rejected)
        assert rejected.get("reason") == "invalid-amount", (bad, rejected)
        with _pinned_home("profa"):
            assert mod.store.resolutions(bad_session) == [], bad
    assert _profile_ceiling("profa") == pytest.approx(3.25), "rejected args must not change the last-set ceiling"

    # The dashboard imports plugin_api.py WITHOUT register(ctx), so the ceiling write must be
    # context-free: prove set_episode_ceiling still applies with _CTX forced to None.
    Path(get_profile_dir("profc")).mkdir(parents=True, exist_ok=True)
    _write_profile_ceiling("profc", 8.0)
    noctx_session = "sess-f30-3-noctx"
    with _pinned_home("profc"):
        noctx_ep = _seed_episode(mod, noctx_session, budget_gen=1)
    saved_ctx = getattr(mod, "_CTX", None)
    mod._CTX = None
    try:
        noctx = mod.set_episode_ceiling("profc", noctx_session, noctx_ep, 1, "4.00", OPERATOR)
        assert noctx["granted"] is True, noctx
        assert _profile_ceiling("profc") == pytest.approx(4.00), "ceiling write must not depend on _CTX"
    finally:
        mod._CTX = saved_ctx


def test_ac_cost_f30_4(loaded_plugin):
    """AC-COST-F30-4: The immortal decision uses the real F29 immortal kind — an immortal 'daily' episode is Continue-only (Stop refused, Continue advances day_start_total) while an ordinary mortal Stop is honored and blocks the session."""
    mod = loaded_plugin.module

    # Immortal daily (the real F29 immortal kind): Stop refused, Continue advances the day baseline.
    imm = "sess-f30-4-daily"
    dep = _seed_episode(mod, imm, kind="daily")
    stopped = mod.resolve_escalation(imm, dep, 1, "stop", OPERATOR)
    assert stopped["granted"] is False and stopped.get("reason") == "immortal-continue-only", stopped
    assert mod.store.resolutions(imm) == []
    cont = mod.resolve_escalation(imm, dep, 1, "continue", OPERATOR)
    assert cont["granted"] is True, cont
    assert _baseline(mod, imm, "day_start_total") == pytest.approx(INCREMENT_USD), imm

    # Ordinary mortal Stop: honored — it blocks the session; a later Continue loses the CAS.
    mortal = "sess-f30-4-mortal"
    mep = _seed_episode(mod, mortal, kind="escalation", blocked=False)
    halted = mod.resolve_escalation(mortal, mep, 1, "stop", OPERATOR)
    assert halted["granted"] is True, halted
    assert dict(mod.store.get_state(mortal))["blocked"] == 1, "mortal Stop must block the session"
    applied = _applied(mod, mortal)
    assert len(applied) == 1 and applied[0]["decision"] == "stop", applied
    late = mod.resolve_escalation(mortal, mep, 1, "continue", OPERATOR)
    assert late["granted"] is False and late.get("reason") == "already-resolved", late
    assert dict(mod.store.get_state(mortal))["blocked"] == 1, "Stop stays winning under the CAS"


def test_ac_cost_f30_5(loaded_plugin):
    """AC-COST-F30-5: The reconciler makes mid-decision episodes consistent and idempotent — a
    closed-while-unresolved episode is finalised so a later resolution no-ops, and a claimed-but-unapplied
    grant is applied exactly once across repeated reconcile passes (proven across two profiles)."""
    mod = loaded_plugin.module

    # Two DISTINCT CONFIGURED profiles each hold one mid-decision episode. reconcile_once() must
    # process BOTH regardless of the caller's ambient home; a reconciler that only touched the
    # caller's home would leave one profile stuck. Each is a configured plugin profile (operators
    # seeded via _write_profile_ceiling) so the later authorized resolve reaches the CAS rather than
    # failing auth-first (resolve_escalation authorizes before it touches the store).
    closed_session = "sess-f30-5a"
    crash_session = "sess-f30-5b"
    for name in ("reconx", "recony"):
        _write_profile_ceiling(name, 10.0)
    with _pinned_home("reconx"):
        closed_ep = _seed_episode(mod, closed_session, budget_gen=1)
        mod._on_session_finalize(session_id=closed_session)
    with _pinned_home("recony"):
        crash_ep = _seed_episode(mod, crash_session, budget_gen=1)
        claimed = mod.store.claim_resolution(crash_ep, crash_session, 1, "continue", OPERATOR, 0.0 + INCREMENT_USD)
        assert claimed is True

    # ONE reconcile pass from the UNPINNED base home must act on BOTH profiles (>=2 actions):
    # list_profiles()/get_profile_dir enumerate from the DEFAULT root _get_profiles_root()
    # (release tree profiles.py:293), NOT the pinned override, so the profiles are discoverable
    # from the base home — which itself holds no episodes, so a reconciler that ignored profiles
    # would act zero times and fail this assertion.
    acted = mod.reconcile_once()
    assert isinstance(acted, int) and acted >= 2, acted

    with _pinned_home("reconx"):
        rows = mod.store.resolutions(closed_session)
    assert len(rows) == 1 and rows[0]["actor"] == "reconciler", rows
    assert rows[0]["status"] == "cancelled" and rows[0]["reason"] == "session-closed", rows
    with _pinned_home("reconx"):
        late = mod.resolve_escalation(closed_session, closed_ep, 1, "continue", OPERATOR)
    assert late["granted"] is False and late.get("reason") == "already-resolved", late

    with _pinned_home("recony"):
        after = dict(mod.store.get_state(crash_session))
    assert after["window_start_total"] == pytest.approx(INCREMENT_USD), after
    assert after["blocked"] == 0

    # A second pass (again unpinned) is idempotent for both profiles.
    mod.reconcile_once()
    with _pinned_home("recony"):
        assert dict(mod.store.get_state(crash_session)) == after, "reconcile is idempotent"
        assert len(_applied(mod, crash_session)) == 1, "applied exactly once"
    with _pinned_home("reconx"):
        assert len(mod.store.resolutions(closed_session)) == 1, "finalise recorded exactly once"


def test_ac_cost_f30_9(loaded_plugin):
    """AC-COST-F30-9 (derived): profile isolation through the REAL ctx-free dashboard router —
    running from the DEFAULT home, GET/authz/CAS act on the TARGET ?profile only. A DEFAULT-profile
    operator is DENIED for a profa request (no mutation) while profa's DISJOINT operator SUCCEEDS
    (exactly one increment); this DETECTS an impl that authorizes against the default before pinning."""
    import importlib.util
    import os
    import sys

    from fastapi import FastAPI, Request
    from starlette.testclient import TestClient

    from hermes_cli.dashboard_auth.base import Session

    mod = loaded_plugin.module

    # profa carries DISJOINT operators (only PROFA_OP); the default config's op-1 is NOT one of them.
    _write_profile_ceiling("profa", 10.0, operators=[PROFA_OP])
    with _pinned_home("profa"):
        episode = _seed_episode(mod, "sess-f30-9", budget_gen=1)
        before = dict(mod.store.get_state("sess-f30-9"))

    # Import the ctx-free router EXACTLY as web_server does (spec_from_file_location + sys.modules
    # registered BEFORE exec for forward-ref resolution — hermes_cli/web_server.py:19158-19179).
    api_path = Path(os.environ["HERMES_HOME"]) / "plugins" / PLUGIN_KEY / "dashboard" / "plugin_api.py"
    module_name = f"hermes_dashboard_plugin_{PLUGIN_KEY}"
    spec = importlib.util.spec_from_file_location(module_name, api_path)
    assert spec is not None and spec.loader is not None, api_path
    api_mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = api_mod
    spec.loader.exec_module(api_mod)

    def _session(uid):
        return Session(user_id=uid, email=f"{uid}@example.test", display_name=uid, org_id="org-1",
                       provider="oauth", expires_at=9999999999, access_token="t", refresh_token="r")

    app = FastAPI()

    @app.middleware("http")
    async def _inject_session(request: Request, call_next):
        # simulate the dashboard auth gate (hermes_cli/dashboard_auth/middleware.py:519) setting the
        # server-verified session; the router must derive its principal from THIS, never the body.
        uid = request.headers.get("x-test-user")
        if uid:
            request.state.session = _session(uid)
        return await call_next(request)

    app.include_router(api_mod.router, prefix=f"/api/plugins/{PLUGIN_KEY}")
    client = TestClient(app)
    base = f"/api/plugins/{PLUGIN_KEY}"

    # GET is target-scoped: profa (has the episode) lists it; the default profile (no episode) does not.
    r_profa = client.get(f"{base}/escalations?profile=profa", headers={"x-test-user": "op-1"})
    assert r_profa.status_code < 400, r_profa.text
    assert "sess-f30-9" in [e.get("session") for e in r_profa.json()], r_profa.json()
    r_default = client.get(f"{base}/escalations?profile=default", headers={"x-test-user": "op-1"})
    assert r_default.status_code < 400, r_default.text
    assert "sess-f30-9" not in [e.get("session") for e in r_default.json()], r_default.json()

    # (a) a DEFAULT-profile operator (op-1, absent from profa's operators) is DENIED for a profa
    #     request: the router authorizes against the pinned-profa operators, not the default's. The
    #     denial must be an explicit unauthorized RESPONSE (not merely a no-op / an unrelated 5xx),
    #     AND no store effect.
    r_deny = client.post(f"{base}/escalations/{episode}/resolve?profile=profa",
                         json={"decision": "continue"}, headers={"x-test-user": "op-1"})
    denied = r_deny.json()
    assert (
        r_deny.status_code == 403 and denied.get("detail") == "unauthorized"
    ) or (
        r_deny.status_code < 400
        and denied.get("granted") is False
        and denied.get("reason") == "unauthorized"
    ), (r_deny.status_code, denied)
    with _pinned_home("profa"):
        after_deny = dict(mod.store.get_state("sess-f30-9"))
        assert _applied(mod, "sess-f30-9") == [], "default operator applied no effect in profa"
    assert after_deny == before, (r_deny.status_code, r_deny.text)

    # (b) profa's DISJOINT operator SUCCEEDS — exactly one increment, blocked cleared.
    r_ok = client.post(f"{base}/escalations/{episode}/resolve?profile=profa",
                       json={"decision": "continue"}, headers={"x-test-user": "profa-op"})
    assert r_ok.status_code < 400, r_ok.text
    with _pinned_home("profa"):
        after_ok = dict(mod.store.get_state("sess-f30-9"))
        applied = _applied(mod, "sess-f30-9")
    assert after_ok["window_start_total"] == pytest.approx(before["window_start_total"] + INCREMENT_USD), after_ok
    assert after_ok["blocked"] == 0
    assert len(applied) == 1, applied


def test_ac_cost_f30_10(loaded_plugin):
    """AC-COST-F30-10 (derived, safety): on the default estop_on_breach=true a granted Continue lifts the shared profile-local ESTOP sentinel ONLY when it is nv-cost-cap-owned (reason begins 'nv-cost-cap:') AND no non-immortal OPEN session in the profile — including the just-resolved one — remains blocked/unpriced/over-ceiling; a FOREIGN (hermes pause) sentinel and a still-needed multi-session belt (a second session blocked, over-ceiling, or recon-unpriced) are both LEFT while the resolution is still applied, and the sentinel is unlinked only when owned and the belt is clear."""
    from agent import estop

    mod = loaded_plugin.module

    # Each sub-case runs in its OWN pinned profile so cap_state and the profile-local ESTOP
    # sentinel (estop.sentinel_path() = get_hermes_home()/ESTOP, honouring the override) are
    # isolated; each profile carries the namespaced operators and a high ceiling so a resolved
    # session is provably runnable (spent well under the ceiling, not over-ceiling).
    for name in ("estopf", "estopb", "estopc", "estopd"):
        _write_profile_ceiling(name, 100.0)

    # (i) FOREIGN sentinel preserved: a `hermes pause`-style sentinel (reason without the
    # 'nv-cost-cap:' prefix) must survive an authorized Continue even when the belt is otherwise
    # clear — a cost-cap resolution must never lift an operator pause.
    with _pinned_home("estopf"):
        ep = _seed_episode(mod, "sess-f30-10-foreign", kind="breach")
        estop.engage(reason="operator maintenance window")  # foreign: no 'nv-cost-cap:' prefix
        assert estop.sentinel_path().exists()
        res = mod.resolve_escalation("sess-f30-10-foreign", ep, 1, "continue", OPERATOR)
        assert res["granted"] is True, res
        assert dict(mod.store.get_state("sess-f30-10-foreign"))["blocked"] == 0, "resolution IS applied"
        assert estop.sentinel_path().exists(), "a FOREIGN (hermes pause) sentinel must be left intact"

    # (ii) Multi-session belt preserved: with an nv-cost-cap-owned sentinel and TWO non-immortal
    # blocked sessions, resolving ONE must LEAVE the sentinel because the other session is still
    # blocked — clearing it would resume cron/kanban for the profile during an active breach.
    with _pinned_home("estopb"):
        ep_a = _seed_episode(mod, "sess-f30-10-belt-a", kind="breach")
        _seed_episode(mod, "sess-f30-10-belt-b", kind="breach")  # stays blocked
        estop.engage(reason="nv-cost-cap: session sess-f30-10-belt-a exceeded the Tier-2 cost ceiling")
        res = mod.resolve_escalation("sess-f30-10-belt-a", ep_a, 1, "continue", OPERATOR)
        assert res["granted"] is True, res
        assert dict(mod.store.get_state("sess-f30-10-belt-a"))["blocked"] == 0
        assert dict(mod.store.get_state("sess-f30-10-belt-b"))["blocked"] == 1, "the other session stays blocked"
        assert estop.sentinel_path().exists(), "the shared belt must be left while another session is still blocked"

    # (iii) Cleared when owned AND belt-clear: the sole owned breach, once resolved and runnable,
    # DOES lift the sentinel — the guard is not over-conservative, which is what lets an authorized
    # Continue on the sole priced breach resume (cf. AC-8 Part B).
    with _pinned_home("estopc"):
        ep_c = _seed_episode(mod, "sess-f30-10-clear", kind="breach")
        estop.engage(reason="nv-cost-cap: session sess-f30-10-clear exceeded the Tier-2 cost ceiling")
        res = mod.resolve_escalation("sess-f30-10-clear", ep_c, 1, "continue", OPERATOR)
        assert res["granted"] is True, res
        assert dict(mod.store.get_state("sess-f30-10-clear"))["blocked"] == 0
        assert not estop.sentinel_path().exists(), "an owned sentinel with a clear belt must be lifted"

    # (iv) BODY-LESS sentinel preserved: engage()'s touch() fail-safe can leave a sentinel with no
    # JSON body; an unreadable/empty body is not provably nv-cost-cap-owned, so it must be treated
    # as foreign and survive an authorized Continue even with a clear belt.
    with _pinned_home("estopd"):
        ep_d = _seed_episode(mod, "sess-f30-10-empty", kind="breach")
        estop.sentinel_path().write_text("", encoding="utf-8")  # body-less: json.loads fails -> foreign
        assert estop.sentinel_path().exists()
        res = mod.resolve_escalation("sess-f30-10-empty", ep_d, 1, "continue", OPERATOR)
        assert res["granted"] is True, res
        assert dict(mod.store.get_state("sess-f30-10-empty"))["blocked"] == 0
        assert estop.sentinel_path().exists(), "a body-less/unreadable sentinel must be treated as foreign and left"

    # (v) OVER-CEILING belt-holder: with an owned sentinel, a SECOND non-immortal session that is
    # not blocked but is over its ceiling still holds the belt, so resolving a runnable session
    # LEAVES the sentinel — the belt honours over-ceiling, not only blocked.
    _write_profile_ceiling("estope", 8.0)
    with _pinned_home("estope"):
        ep_e = _seed_episode(mod, "sess-f30-10-ceil", kind="breach")  # after Continue: spent 5 < 8 -> runnable
        mod.store.set_state("sess-f30-10-ceil-holder", effective_usd=50.0, window_start_total=0.0,
                            day_start_total=0.0, budget_gen=1, blocked=0, immortal=0)  # spent 50 >= 8 -> over-ceiling
        estop.engage(reason="nv-cost-cap: session sess-f30-10-ceil exceeded the Tier-2 cost ceiling")
        res = mod.resolve_escalation("sess-f30-10-ceil", ep_e, 1, "continue", OPERATOR)
        assert res["granted"] is True, res
        assert dict(mod.store.get_state("sess-f30-10-ceil"))["blocked"] == 0
        assert estop.sentinel_path().exists(), "a non-blocked over-ceiling session must hold the belt"

    # (vi) RECON-UNPRICED belt-holder: a reconcile-only unknown-pricing session (recon_unpriced=1,
    # no unpriced call rows) still holds the belt — the belt must honour the recon marker the
    # evaluator uses, not only the calls-table unpriced_count, or AC-8 Part A regresses.
    _write_profile_ceiling("estopg", 100.0)
    with _pinned_home("estopg"):
        ep_g = _seed_episode(mod, "sess-f30-10-recon", kind="breach")  # runnable after Continue
        mod.store.set_state("sess-f30-10-recon-holder", effective_usd=0.0, window_start_total=0.0,
                            day_start_total=0.0, budget_gen=1, blocked=0, immortal=0, recon_unpriced=1)
        estop.engage(reason="nv-cost-cap: session sess-f30-10-recon exceeded the Tier-2 cost ceiling")
        res = mod.resolve_escalation("sess-f30-10-recon", ep_g, 1, "continue", OPERATOR)
        assert res["granted"] is True, res
        assert dict(mod.store.get_state("sess-f30-10-recon"))["blocked"] == 0
        assert estop.sentinel_path().exists(), "a recon-unpriced (recon_unpriced=1) session must hold the belt"


def test_daily_rollover_supersedes_stale_daily_generation(loaded_plugin):
    """Regression (COST-F30): an immortal daily window advances budget_gen on UTC-day rollover,
    so a PRIOR day's unresolved daily episode is stale-generation — a late Continue on it is
    refused and cannot grant the new day's baseline fresh headroom. Not an AC id; guards the
    generation invariant the daily path depends on (the escalation CAS gen-check assumes the
    generation moves forward, which for the immortal per-day window only the rollover does)."""
    mod = loaded_plugin.module
    sid = "sess-f30-daily-rollover"
    # Day N: an immortal daily session at generation 1 with a day-N crossing episode.
    day_n_ep = _seed_episode(mod, sid, kind="daily", budget_gen=1)
    mod.store.set_state(
        sid, effective_usd=20.0, day_key="2026-09-19", day_start_total=10.0, last_evaluated_total=20.0
    )
    # Roll into day N+1 through the immortal boundary evaluator: the generation must advance.
    mod._evaluate_daily(sid, today="2026-09-20")
    rolled = dict(mod.store.get_state(sid))
    assert rolled["budget_gen"] == 2, rolled
    before = _baseline(mod, sid, "day_start_total")
    # The prior-day episode (claimed at generation 1) is now stale: refused, baseline unchanged.
    stale = mod.resolve_escalation(sid, day_n_ep, 1, "continue", OPERATOR)
    assert stale["granted"] is False and stale.get("reason") == "stale-generation", stale
    assert _baseline(mod, sid, "day_start_total") == pytest.approx(before), (
        "a stale daily Continue must not advance the new day's baseline"
    )
    rows = mod.store.resolutions(sid)
    assert rows and all(r["status"] == "cancelled" for r in rows), rows


# --- Guarded ESTOP-clear behavior contracts (beside AC-COST-F30-10) ---------------------------
# Finer guard conditions AC-10 does not itself assert: the ceiling call-site guards the same way,
# the clear leaves a fleet-root pause and keeps resumes false, the notice claims resume only on a
# real blocked→runnable transition, a failed unlink defers and the reconciler recovers, a
# concurrent breach that commits before the scan keeps the belt, and the reconciler clears an
# owned sentinel once its sole blocker closes.


def test_estop_clear_ceiling_call_site_guards_foreign(loaded_plugin):
    """Regression (F30-REV-1): the CEILING resolve call-site guards the ESTOP clear too — a FOREIGN
    (non-nv-cost-cap) sentinel is LEFT by a granted set-ceiling, exactly as on the Continue path."""
    from agent import estop

    mod = loaded_plugin.module
    _write_profile_ceiling("estopceil", 100.0)
    with _pinned_home("estopceil"):
        ep = _seed_episode(mod, "sess-ceil-foreign", kind="breach")
        estop.engage(reason="operator maintenance window")  # foreign: no 'nv-cost-cap:' prefix
        assert estop.sentinel_path().exists()
        res = mod.set_episode_ceiling("estopceil", "sess-ceil-foreign", ep, 1, "80.0", OPERATOR)
        assert res["granted"] is True, res
        assert dict(mod.store.get_state("sess-ceil-foreign"))["blocked"] == 0
        assert estop.sentinel_path().exists(), "a foreign sentinel must survive a set-ceiling resolve"


def test_estop_leaves_fleet_root_pause_and_resumes_false(loaded_plugin):
    """Regression (F30-REV-1): clearing our OWN profile-local sentinel does NOT lift a FLEET-ROOT
    operator pause, and the just-resolved session reports resumes=false while any pause is engaged
    (resumes is decoupled from the profile clear, via the fleet-root-inclusive is_engaged())."""
    from agent import estop

    mod = loaded_plugin.module
    _write_profile_ceiling("estopfleet", 100.0)
    with _pinned_home("estopfleet"):
        ep = _seed_episode(mod, "sess-fleet", kind="breach")
        fleet = estop._canonical_root() / "ESTOP"  # a DIFFERENT path from the profile-local sentinel
        fleet.parent.mkdir(parents=True, exist_ok=True)
        fleet.write_text('{"reason": "operator fleet pause"}', encoding="utf-8")
        assert fleet.resolve() != estop.sentinel_path().resolve(), "fleet-root must differ from profile-local"
        estop.engage(reason="nv-cost-cap: session sess-fleet exceeded the Tier-2 cost ceiling")
        res = mod.resolve_escalation("sess-fleet", ep, 1, "continue", OPERATOR)
        assert res["granted"] is True, res
        assert not estop.sentinel_path().exists(), "our owned profile-local sentinel is cleared"
        assert fleet.exists(), "the fleet-root operator pause must be left intact"
        assert res.get("resumes") is False, "a still-engaged fleet-root pause keeps resumes false"
        fleet.unlink(missing_ok=True)


def test_cost_outcome_text_claims_resume_only_when_stopped_then_resumed(loaded_plugin):
    """Regression (F30-REV-2): the notice claims "session resumed" ONLY for a real blocked→runnable
    transition (was_blocked AND resumes); a never-blocked but runnable Continue reads "runnable"
    (not "resumed"); a still-paused Continue reads a plain "Continue applied." with no false claim."""
    mod = loaded_plugin.module
    resumed = mod._cost_outcome_text(
        {"granted": True, "decision": "continue", "resumes": True, "was_blocked": True})
    runnable = mod._cost_outcome_text(
        {"granted": True, "decision": "continue", "resumes": True, "was_blocked": False})
    still_paused = mod._cost_outcome_text(
        {"granted": True, "decision": "continue", "resumes": False, "was_blocked": True})
    assert "resumed" in resumed.lower(), resumed
    assert "resumed" not in runnable.lower() and "runnable" in runnable.lower(), runnable
    assert "resumed" not in still_paused.lower(), still_paused
    assert "continue applied" in still_paused.lower(), still_paused


def test_estop_unlink_failure_defers_then_reconciler_clears(loaded_plugin, monkeypatch):
    """Regression (F30-REV-1): a failing guarded unlink DEFERS (row stays pending, tx1 unblock
    durable), and a later reconcile_once() re-runs the guarded clear and lifts the owned sentinel."""
    from agent import estop

    mod = loaded_plugin.module
    _write_profile_ceiling("estopdefer", 100.0)
    orig_unlink = mod.store._unlink_local_sentinel
    with _pinned_home("estopdefer"):
        ep = _seed_episode(mod, "sess-defer", kind="breach")
        estop.engage(reason="nv-cost-cap: session sess-defer exceeded the Tier-2 cost ceiling")
        monkeypatch.setattr(mod.store, "_unlink_local_sentinel", lambda: False)
        res = mod.resolve_escalation("sess-defer", ep, 1, "continue", OPERATOR)
        assert res["granted"] is False and res["reason"] == "deferred", res
        assert dict(mod.store.get_state("sess-defer"))["blocked"] == 0, "tx1 unblock is durable"
        assert estop.sentinel_path().exists(), "sentinel still present after the deferred unlink"
        assert len(_applied(mod, "sess-defer")) == 0, "the resolution row stays pending, not applied"
        # Restore ONLY the unlink patch — monkeypatch.undo() would also revert the loaded_plugin
        # fixture's HERMES_HOME setenv (same monkeypatch instance), sending reconcile_once() to the
        # wrong home where this profile does not exist.
        monkeypatch.setattr(mod.store, "_unlink_local_sentinel", orig_unlink)
    acted = mod.reconcile_once()
    assert acted >= 1, acted
    with _pinned_home("estopdefer"):
        assert not estop.sentinel_path().exists(), "reconcile_once re-runs the guarded clear and lifts it"
        assert len(_applied(mod, "sess-defer")) == 1, "the reconciler finalises the pending resolution"


def test_estop_belt_held_by_second_session_keeps_resumes_false(loaded_plugin):
    """Regression (F30-REV-1, multi-session belt): while another non-immortal session is still
    blocked, resolving one LEAVES the shared sentinel AND the resolved session reports
    resumes=false (the shared belt pauses the whole profile)."""
    from agent import estop

    mod = loaded_plugin.module
    _write_profile_ceiling("estopbelt2", 100.0)
    with _pinned_home("estopbelt2"):
        ep_a = _seed_episode(mod, "sess-belt2-a", kind="breach")
        _seed_episode(mod, "sess-belt2-b", kind="breach")  # stays blocked
        estop.engage(reason="nv-cost-cap: session sess-belt2-a exceeded the Tier-2 cost ceiling")
        res = mod.resolve_escalation("sess-belt2-a", ep_a, 1, "continue", OPERATOR)
        assert res["granted"] is True, res
        assert estop.sentinel_path().exists(), "the belt is left while another session is blocked"
        assert dict(mod.store.get_state("sess-belt2-b"))["blocked"] == 1
        assert res.get("resumes") is False, "the shared belt keeps the resolved session non-resumed"


def test_estop_reconciler_clears_after_sole_blocker_closes(loaded_plugin):
    """Regression (F30-REV-1, step d): an owned sentinel whose SOLE blocker CLOSED without a
    resolution is lifted by reconcile_once()'s guarded clear (closed rows are excluded from the
    belt) — no stuck belt."""
    from agent import estop

    mod = loaded_plugin.module
    _write_profile_ceiling("estopclosed", 100.0)
    with _pinned_home("estopclosed"):
        _seed_episode(mod, "sess-closed", kind="breach")
        estop.engage(reason="nv-cost-cap: session sess-closed exceeded the Tier-2 cost ceiling")
        assert estop.sentinel_path().exists()
        mod.store.mark_session_closed("sess-closed")  # closed without a resolution
    acted = mod.reconcile_once()
    assert acted >= 1, acted
    with _pinned_home("estopclosed"):
        assert not estop.sentinel_path().exists(), "reconcile lifts an owned sentinel whose blocker closed"


def test_estop_concurrent_breach_serializes_leaves_belt(loaded_plugin):
    """Regression (F30-REV-1): tx2's BEGIN IMMEDIATE serialises against a concurrent breach — a real
    second-session breach that commits blocked=1 before the resolver's belt scan leaves the shared
    sentinel. Barrier-coordinated so the breach lands first, deterministically (no lock deadlock:
    the breach commits before the resolve enters tx2)."""
    import threading

    from agent import estop

    mod = loaded_plugin.module
    _write_profile_ceiling("estopconc", 100.0)
    with _pinned_home("estopconc"):
        ep_a = _seed_episode(mod, "sess-conc-a", kind="breach")
        mod.store.set_state("sess-conc-b", effective_usd=0.0, window_start_total=0.0,
                            day_start_total=0.0, budget_gen=1, blocked=0, immortal=0)
        estop.engage(reason="nv-cost-cap: session sess-conc-a exceeded the Tier-2 cost ceiling")
        start = threading.Barrier(2)
        breached = threading.Event()

        def _breach():
            start.wait(timeout=5)
            with _pinned_home("estopconc"):
                mod.store.set_state("sess-conc-b", blocked=1)  # a real concurrent breach writer
            breached.set()

        t = threading.Thread(target=_breach)
        t.start()
        start.wait(timeout=5)
        assert breached.wait(timeout=5), "the concurrent breach did not commit"
        res = mod.resolve_escalation("sess-conc-a", ep_a, 1, "continue", OPERATOR)
        t.join(timeout=5)
        assert res["granted"] is True, res
        assert dict(mod.store.get_state("sess-conc-b"))["blocked"] == 1
        assert estop.sentinel_path().exists(), "a concurrent breach committed before the scan keeps the belt"
        assert res.get("resumes") is False, "the shared belt keeps the resolved session non-resumed"
