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
  * AC-2 proves the surface-NAMESPACED-principal authz on ALL THREE principal classes that funnel
    through the one chokepoint — `dashboard:<provider>:<org_id>:<user_id>` from the server session
    (`principal_from_request`), `gateway:<platform.value>:<scope>:<user_id>` from the event
    (`principal_from_event`), and `cli:<orchestrator-profile>:<fleet-admin-id>` from the CLI —
    never a client-supplied one; a BARE un-namespaced user_id, a same-user DIFFERENT-workspace
    principal, AND a `cli:`-namespaced non-operator are all refused (cross-surface + workspace
    isolation + no CLI operator-check bypass).
    Operators are read context-free via load_config_readonly() so the ctx-free dashboard-import
    path authorizes identically.
  * AC-8 (the live gateway path via a real `pre_gateway_dispatch` `/cost` event) is the
    scenario artifact in the ADR, not a function here.
  * AC-10 (the never-unlink safety invariant) proves the plugin NEVER unlinks or disengages the
    profile ESTOP sentinel on ANY resolution path (Continue / runnable set-ceiling / Stop / reconcile):
    the sentinel REMAINS byte-for-byte + inode across each, a fails-if-unlink-invoked probe FAILS if any
    deletion primitive touches it, and a granted Continue surfaces estop_disposition="left" +
    manual_resume_required=true (resumes=false). The belt is lifted only by UA-28 or a manual `hermes
    resume`. Two engage-side regressions ride beside it: test_estop_acquisition_barrier (a concurrent
    operator pause racing the plugin's os.link publish, driven through the real resolve_escalation("stop")
    engage) and test_estop_engage_fault_injection (os.link unsupported -> O_EXCL fallback no receipt;
    linked-but-unrecorded sentinel treated foreign, never cleanup-unlinked).

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

Drop-in artifact: the builder copies it to tests/plugins/test_nv_cost_cap_escalation_acceptance.py
before running; repo-root discovery for the plugin dir assumes that location.
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
# CLI_OP is the orchestrator's fleet-admin principal for `hermes cost-cap resolve`; it funnels through
# the SAME operator-list authz as the panel/gateway surfaces (no bypass) and must be a configured
# operator. CLI_STRANGER is a cli:-namespaced non-operator (denied like any other non-operator).
CLI_OP = "cli:orch-1:fleet-admin"
CLI_STRANGER = "cli:orch-1:not-admin"
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
                        "operators": [OP_DASH, OPERATOR, CLI_OP],
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
    assert {"pre_gateway_dispatch", "on_session_end", "on_session_finalize"} <= set(loaded.hooks_registered)
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


def _engage_owned(mod, session_id):
    """Engage the profile-local ESTOP as an nv-cost-cap-owned sentinel through the plugin's real engage
    seam (store.engage_owned, atomic no-replace os.link publish), so a per-engagement receipt is recorded
    in plugin_db. Under MANUAL-RESUME the plugin never consults the receipt to clear (it never clears);
    the receipt exists solely so UA-28's upstream disengage_owned can later identify the plugin's own
    sentinel. A direct estop.engage() records no receipt and is therefore foreign to the plugin."""
    mod.store.engage_owned(session_id)


@contextlib.contextmanager
def _sentinel_untouched(estop, why):
    """Fail if the profile-local sentinel is deleted, replaced, or its bytes change across the block.
    A canary hardlink pins the original inode, so a delete-and-identical-rewrite (which byte-equality
    alone would miss) changes st_ino and is caught; where hardlinks are unsupported it degrades to byte
    verification (the design itself has a hardlink-unavailable fallback)."""
    sp = estop.sentinel_path()
    before = sp.read_bytes()
    canary = sp.with_name(sp.name + ".nvcc-canary")
    try:
        canary.hardlink_to(sp)
    except (OSError, NotImplementedError):
        canary = None
    try:
        orig_ino = canary.stat().st_ino if canary is not None else None
        yield
        assert sp.exists() and sp.read_bytes() == before, f"{why} (bytes changed)"
        if orig_ino is not None:
            assert sp.stat().st_ino == orig_ino, f"{why} (deleted-and-rewritten)"
    finally:
        if canary is not None:
            canary.unlink()


@contextlib.contextmanager
def _fail_if_sentinel_unlinked(estop):
    """MANUAL-RESUME probe: FAIL the test if the plugin invokes any deletion/rename primitive on the
    ESTOP sentinel, or calls estop.disengage() at all, inside the block. Non-sentinel paths (e.g. an
    atomic config.yaml write) pass through to the real primitive, so a set-ceiling's config write is
    unaffected — only a touch of the sentinel path trips it. This is the never-unlink invariant asserted
    directly (beside the byte+inode canary in _sentinel_untouched)."""
    import os as _os
    import pathlib

    sp = str(estop.sentinel_path())
    real_path_unlink = pathlib.Path.unlink
    real_os_unlink = _os.unlink
    real_os_remove = _os.remove
    real_os_rename = _os.rename
    real_os_replace = _os.replace
    real_disengage = estop.disengage

    def _guard_path_unlink(self, *a, **k):
        assert str(self) != sp, "plugin invoked Path.unlink on the ESTOP sentinel — never-unlink violated"
        return real_path_unlink(self, *a, **k)

    def _guard_os_unlink(path, *a, **k):
        assert str(path) != sp, "plugin invoked os.unlink on the ESTOP sentinel — never-unlink violated"
        return real_os_unlink(path, *a, **k)

    def _guard_os_remove(path, *a, **k):
        assert str(path) != sp, "plugin invoked os.remove on the ESTOP sentinel — never-unlink violated"
        return real_os_remove(path, *a, **k)

    def _guard_os_rename(src, dst, *a, **k):
        assert str(src) != sp and str(dst) != sp, "plugin renamed the ESTOP sentinel — never-unlink violated"
        return real_os_rename(src, dst, *a, **k)

    def _guard_os_replace(src, dst, *a, **k):
        # os.replace also backs pathlib.Path.replace, so this covers atomic-rename deletions too.
        assert str(src) != sp and str(dst) != sp, "plugin os.replace'd the ESTOP sentinel — never-unlink violated"
        return real_os_replace(src, dst, *a, **k)

    def _guard_disengage(*a, **k):
        raise AssertionError("plugin invoked estop.disengage — never-unlink violated")

    pathlib.Path.unlink = _guard_path_unlink
    _os.unlink = _guard_os_unlink
    _os.remove = _guard_os_remove
    _os.rename = _guard_os_rename
    _os.replace = _guard_os_replace
    estop.disengage = _guard_disengage
    try:
        yield
    finally:
        pathlib.Path.unlink = real_path_unlink
        _os.unlink = real_os_unlink
        _os.remove = real_os_remove
        _os.rename = real_os_rename
        _os.replace = real_os_replace
        estop.disengage = real_disengage


# Verbatim operator notice a granted Continue must surface while the belt is left.
MANUAL_RESUME_NOTICE = (
    "Continue applied — the per-session cost block is cleared, but the profile ESTOP belt remains "
    "engaged (gates cron/kanban/new inbounds); resume via `hermes resume` or the UA-28 upstream lock"
)


def _assert_left_belt(res, mod, *, why, expect_notice=False):
    """A granted resolution whose profile ESTOP belt remains engaged must surface it honestly: the money
    block cleared, but the belt stands so the session is not runnable and the operator must resume it.
    A Continue additionally renders the verbatim manual-resume notice through _cost_outcome_text; the
    set-ceiling verb differs, so its notice text is left to the plugin and only the fields are asserted."""
    assert res.get("resumes") is False, f"{why}: resumes must be False while a sentinel is engaged"
    assert res.get("estop_disposition") == "left", f"{why}: estop_disposition must be 'left' ({res!r})"
    assert res.get("manual_resume_required") is True, f"{why}: manual_resume_required must be True ({res!r})"
    if expect_notice:
        rendered = mod._cost_outcome_text(res)
        assert MANUAL_RESUME_NOTICE in rendered, f"{why}: verbatim manual-resume notice absent ({rendered!r})"


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
    """AC-COST-F30-2: A resolution is authorized against structured operators using a surface-namespaced principal on all three resolution surfaces (dashboard, gateway, CLI) through one chokepoint with no bypass; a non-operator, a bare un-namespaced user_id, the same user_id in a different workspace scope, or a cli:-namespaced non-operator applies no effect while a matching namespaced operator is granted."""
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

    # CLI seam: `hermes cost-cap resolve` funnels through the SAME chokepoint with a
    # cli:<orchestrator-profile>:<fleet-admin-id> principal that must be a configured operator — no
    # bypass. A cli:-namespaced NON-operator is denied exactly like any other non-operator; the
    # configured CLI operator grants. (The cli_scope operational gate is the fleet's separate plugin.)
    csid = "sess-f30-2-cli"
    cep = _seed_episode(mod, csid, kind="breach")
    cli_before = dict(mod.store.get_state(csid))
    denied_cli = mod.resolve_escalation(csid, cep, 1, "continue", CLI_STRANGER)
    assert denied_cli["granted"] is False and denied_cli.get("reason") == "unauthorized", denied_cli
    assert mod.store.resolutions(csid) == []
    assert dict(mod.store.get_state(csid)) == cli_before
    granted_cli = mod.resolve_escalation(csid, cep, 1, "continue", CLI_OP)
    assert granted_cli["granted"] is True, granted_cli
    assert len(_applied(mod, csid)) == 1

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

    # set-ceiling is a RESOLUTION, not just a config write: on a Tier-2 blocked breach (spend 10.0) an
    # exact ceiling of 12.50 (> spend) applies the resolution row AND clears the block so the session runs.
    ok_session = "sess-f30-3-ok"
    with _pinned_home("profa"):
        ok_ep = _seed_episode(mod, ok_session, kind="breach", budget_gen=1)
        assert dict(mod.store.get_state(ok_session))["blocked"] == 1, "a breach starts blocked"
    exact = mod.set_episode_ceiling("profa", ok_session, ok_ep, 1, "12.50", OPERATOR)
    assert exact["granted"] is True, exact
    with _pinned_home("profa"):
        rows = mod.store.resolutions(ok_session)
        assert dict(mod.store.get_state(ok_session))["blocked"] == 0, "a ceiling above spend clears the Tier-2 block"
    assert len(rows) == 1 and rows[0]["decision"] == "ceiling"
    assert rows[0]["status"] == "applied", rows[0]
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
            # seed as a blocked breach so a stray unblock/baseline change before the invalid-amount return
            # is caught by the FULL-state compare, not only the resolution-row/ceiling checks.
            bad_ep = _seed_episode(mod, bad_session, kind="breach", budget_gen=1)
            bad_before = dict(mod.store.get_state(bad_session))
        rejected = mod.set_episode_ceiling("profa", bad_session, bad_ep, 1, bad, OPERATOR)
        assert rejected["granted"] is False, (bad, rejected)
        assert rejected.get("reason") == "invalid-amount", (bad, rejected)
        with _pinned_home("profa"):
            assert mod.store.resolutions(bad_session) == [], bad
            assert dict(mod.store.get_state(bad_session)) == bad_before, (bad, "invalid amount must not mutate cap_state")
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
    """AC-COST-F30-4: The immortal decision uses the real F29 immortal kind — an immortal 'daily' episode is Continue-ONLY (BOTH Stop and set-ceiling refused with no mutation, Continue advances day_start_total) while an ordinary mortal Stop is honored and blocks the session."""
    mod = loaded_plugin.module

    # Immortal daily (the real F29 immortal kind): Stop refused, Continue advances the day baseline.
    imm = "sess-f30-4-daily"
    dep = _seed_episode(mod, imm, kind="daily")
    stopped = mod.resolve_escalation(imm, dep, 1, "stop", OPERATOR)
    assert stopped["granted"] is False and stopped.get("reason") == "immortal-continue-only", stopped
    assert mod.store.resolutions(imm) == []

    # Continue-ONLY means set-ceiling is ALSO refused on an immortal episode (not just Stop), with no
    # mutation — else an impl that allowed immortal set-ceiling would slip through. Seeded in a pinned
    # profile so set_episode_ceiling (which pins the profile to find the episode) locates it, then refuses.
    _write_profile_ceiling("immceil", 100.0)
    with _pinned_home("immceil"):
        ceil_ep = _seed_episode(mod, "sess-f30-4-immceil", kind="daily")
        imm_before = dict(mod.store.get_state("sess-f30-4-immceil"))
    ceil_refused = mod.set_episode_ceiling("immceil", "sess-f30-4-immceil", ceil_ep, 1, "12.50", OPERATOR)
    assert ceil_refused["granted"] is False and ceil_refused.get("reason") == "immortal-continue-only", ceil_refused
    with _pinned_home("immceil"):
        assert dict(mod.store.get_state("sess-f30-4-immceil")) == imm_before, "immortal set-ceiling must not mutate"
        assert mod.store.resolutions("sess-f30-4-immceil") == [], "immortal set-ceiling must record no resolution"

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

    # The gateway `/cost stop` PARSER routes to a mortal Stop through the same chokepoint (proves the
    # decision set is Stop-capable on the gateway surface, not just Continue).
    gw_stop = "sess-f30-4-gwstop"
    _seed_episode(mod, gw_stop, kind="escalation", blocked=False)
    gw = mod.handle_cost_command(gw_stop, "/cost stop", OPERATOR)
    assert gw["granted"] is True and gw.get("decision") == "stop", gw
    assert dict(mod.store.get_state(gw_stop))["blocked"] == 1, "gateway /cost stop must block the session"


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
    """AC-COST-F30-9: (derived) profile isolation through the REAL ctx-free dashboard router —
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
    # a decoy body carrying forged identity fields must be IGNORED — the route derives its principal
    # from request.state.session (op-1, not a profa operator), never the body, so it still denies.
    r_deny = client.post(f"{base}/escalations/{episode}/resolve?profile=profa",
                         json={"decision": "continue", "principal": PROFA_OP, "actor": PROFA_OP,
                               "user_id": "profa-op"}, headers={"x-test-user": "op-1"})
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

    # (c) an encoded traversal profile is rejected with a CLEAN 4xx (not a 500) and creates NO escaped
    #     profile dir/db — validate_profile_name must reject BEFORE get_profile_dir/set_hermes_home_override.
    escaped = Path(os.environ["HERMES_HOME"]).parent / "nvcc-escape-probe"
    assert not escaped.exists(), "precondition: the escape target must not exist"
    r_trav = client.get(f"{base}/escalations?profile=..%2f..%2fnvcc-escape-probe", headers={"x-test-user": "op-1"})
    assert 400 <= r_trav.status_code < 500, ("traversal must be a clean 4xx rejection, not a 500", r_trav.status_code, r_trav.text)
    assert not escaped.exists(), "a traversal profile must not create an escaped profile dir"
    assert not (escaped / "plugin-data" / PLUGIN_KEY / "data.db").exists(), "no escaped plugin db"

    # (c2) the MUTATION-bearing resolve POST must ALSO reject a traversal profile with a clean 4xx,
    #      no escaped dir, no store effect (validation precedes every read/authz/CAS/config op).
    r_trav_post = client.post(f"{base}/escalations/{episode}/resolve?profile=..%2f..%2fnvcc-escape-probe",
                              json={"decision": "continue"}, headers={"x-test-user": "profa-op"})
    assert 400 <= r_trav_post.status_code < 500, ("POST traversal must be a clean 4xx", r_trav_post.status_code, r_trav_post.text)
    assert not escaped.exists(), "the resolve POST must not create an escaped profile dir"
    with _pinned_home("profa"):
        assert dict(mod.store.get_state("sess-f30-9")) == before, "traversal POST must not mutate profa"
        assert _applied(mod, "sess-f30-9") == [], "traversal POST must apply nothing"

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
    """AC-COST-F30-10: (derived, safety) the plugin NEVER unlinks or disengages the profile ESTOP
    sentinel on ANY resolution path. A granted Continue, a runnable set-ceiling, a granted Stop, and a
    reconcile pass each apply the per-session cap_state resolution but LEAVE the sentinel byte-for-byte
    with its inode unchanged — owned, foreign, or fleet-root alike — the result reports
    estop_disposition='left' + manual_resume_required=true (resumes=false) while a sentinel is engaged,
    and a probe FAILS the test if the plugin touches the sentinel via Path.unlink/os.unlink/os.remove/
    os.rename/estop.disengage on any path. The belt is lifted only upstream or by a manual `hermes
    resume`, never by the plugin, so a cost-cap resolution can never silently lift an operator pause."""
    from agent import estop

    mod = loaded_plugin.module

    # Each sub-case runs in its OWN pinned profile so cap_state and the profile-local ESTOP sentinel
    # (estop.sentinel_path() = get_hermes_home()/ESTOP, honouring the override) are isolated. Ownership
    # is an unguessable per-engagement receipt recorded in plugin_db when the plugin engages (atomic
    # link-publish) — never the reason text; _engage_owned records one, estop.engage() (foreign) does not.
    # Because the plugin never inspects ownership to decide a clear (it never clears), the never-unlink
    # probe holds identically for owned, foreign, and fleet-root sentinels — the receipt exists only so
    # the upstream owner-scoped disengage can later identify the plugin's own sentinel.
    for name in ("estopi", "estopif", "estopceil", "estopstop", "estoprec", "estopacq"):
        _write_profile_ceiling(name, 100.0)

    # (i) granted CONTINUE on the blocked session, OWNED sentinel: the money block clears (blocked=0)
    # but the sentinel REMAINS byte-for-byte and inode-identical, no deletion primitive is invoked, the
    # result surfaces the left belt with the verbatim notice, and the recorded receipt is still present.
    with _pinned_home("estopi"):
        ep_i = _seed_episode(mod, "sess-f30-10-cont", kind="breach")
        _engage_owned(mod, "sess-f30-10-cont")  # OWNED via the plugin (receipt recorded)
        with _sentinel_untouched(estop, "Continue must leave the OWNED sentinel"), \
                _fail_if_sentinel_unlinked(estop):
            res = mod.resolve_escalation("sess-f30-10-cont", ep_i, 1, "continue", OPERATOR)
        assert res["granted"] is True, res
        assert dict(mod.store.get_state("sess-f30-10-cont"))["blocked"] == 0, "the money block clears"
        assert estop.sentinel_path().exists(), "the belt is LEFT after a granted Continue"
        assert mod.store.estop_receipt() is not None, "the plugin's own receipt is preserved (never cleared)"
        _assert_left_belt(res, mod, why="Continue on an owned engaged belt", expect_notice=True)

    # (i-foreign) same, but a FOREIGN operator pause (no receipt): equally never touched, and the plugin
    # never adopts it — estop_receipt() stays None. A cost-cap resolution must never lift an operator pause.
    with _pinned_home("estopif"):
        ep_if = _seed_episode(mod, "sess-f30-10-cont-f", kind="breach")
        estop.engage(reason="operator maintenance window")  # foreign: no plugin receipt
        with _sentinel_untouched(estop, "Continue must leave a FOREIGN pause byte-for-byte"), \
                _fail_if_sentinel_unlinked(estop):
            res = mod.resolve_escalation("sess-f30-10-cont-f", ep_if, 1, "continue", OPERATOR)
        assert res["granted"] is True, res
        assert dict(mod.store.get_state("sess-f30-10-cont-f"))["blocked"] == 0, "resolution IS applied"
        assert mod.store.estop_receipt() is None, "a foreign pause is never adopted as owned"
        _assert_left_belt(res, mod, why="Continue while a foreign operator pause is engaged", expect_notice=True)

    # (ii) runnable SET-CEILING (an exact ceiling above spend clears the block) with an OWNED sentinel:
    # the resolution still applies and clears the money block, but the sentinel is LEFT. The config.yaml
    # write is a non-sentinel path, so the unlink probe passes it through.
    with _pinned_home("estopceil"):
        ep_ceil = _seed_episode(mod, "sess-f30-10-ceil", kind="breach")
        _engage_owned(mod, "sess-f30-10-ceil")  # OWNED via the plugin (receipt recorded)
        # set_episode_ceiling pins "estopceil" internally; that pin nests under this one (same profile),
        # so estop.sentinel_path() here resolves to estopceil's sentinel and the guards watch it.
        with _sentinel_untouched(estop, "runnable set-ceiling must leave the sentinel"), \
                _fail_if_sentinel_unlinked(estop):
            res = mod.set_episode_ceiling("estopceil", "sess-f30-10-ceil", ep_ceil, 1, "12.50", OPERATOR)
        assert res["granted"] is True, res
        assert dict(mod.store.get_state("sess-f30-10-ceil"))["blocked"] == 0, "ceiling above spend clears the block"
        assert estop.sentinel_path().exists(), "the belt is LEFT after a runnable set-ceiling"
        # the set-ceiling verb differs from Continue, so only the structured left-belt fields are asserted.
        _assert_left_belt(res, mod, why="runnable set-ceiling on an owned engaged belt")

    # (iii) granted STOP, OWNED sentinel: Stop keeps the session blocked and (re-)engages via the same
    # atomic link-publish — it NEVER unlinks. The sentinel remains and no deletion primitive fires.
    with _pinned_home("estopstop"):
        ep_stop = _seed_episode(mod, "sess-f30-10-stop", kind="escalation", blocked=False)
        _engage_owned(mod, "sess-f30-10-stop")  # OWNED via the plugin (receipt recorded)
        with _sentinel_untouched(estop, "Stop must never unlink the sentinel"), \
                _fail_if_sentinel_unlinked(estop):
            res = mod.resolve_escalation("sess-f30-10-stop", ep_stop, 1, "stop", OPERATOR)
        assert res["granted"] is True, res
        assert dict(mod.store.get_state("sess-f30-10-stop"))["blocked"] == 1, "a mortal Stop blocks the session"
        assert estop.sentinel_path().exists(), "Stop leaves the sentinel engaged"
        assert res.get("resumes") is False, "a Stop never resumes the session"

    # (iv) RECONCILE over a pinned profile whose sole blocker closed unresolved, OWNED sentinel: the
    # reconciler finalises the closed-unresolved episode but LEAVES the sentinel byte-for-byte AND
    # inode-identical — it performs no ESTOP clear of any kind. reconcile_once() runs while pinned to
    # estoprec (it enumerates + pins profiles itself; list_profiles ignores the override), so the guard
    # watches estoprec's sentinel.
    with _pinned_home("estoprec"):
        _seed_episode(mod, "sess-f30-10-rec", kind="breach")
        _engage_owned(mod, "sess-f30-10-rec")  # OWNED via the plugin (receipt recorded)
        mod._on_session_finalize(session_id="sess-f30-10-rec")  # sole blocker closed unresolved
        with _sentinel_untouched(estop, "reconcile must leave the sentinel byte-for-byte and inode-identical"), \
                _fail_if_sentinel_unlinked(estop):
            acted = mod.reconcile_once()
        # reconcile must ACT (finalize the closed-unresolved episode), not no-op: a no-op reconciler
        # leaves the sentinel untouched too, so the retained-sentinel assertion alone would false-green.
        assert isinstance(acted, int) and acted >= 1, acted
        rec_rows = mod.store.resolutions("sess-f30-10-rec")
        assert len(rec_rows) == 1 and rec_rows[0]["actor"] == "reconciler", rec_rows
        assert rec_rows[0]["status"] == "cancelled" and rec_rows[0]["reason"] == "session-closed", rec_rows
        assert estop.sentinel_path().exists(), "reconcile must LEAVE the sentinel"

    # (vi) ACQUISITION SAFETY (deterministic): engage-on-breach must never overwrite an operator pause
    # that already exists. With a foreign pause in place, _engage_owned publishes via atomic no-replace
    # link, which EEXIST-leaves the existing sentinel and records NO receipt (estop_receipt() stays None)
    # — so the operator bytes are untouched and a subsequent resolve reads FOREIGN and leaves it. The
    # concurrent publication race is test_estop_acquisition_barrier.
    with _pinned_home("estopacq"):
        ep_q = _seed_episode(mod, "sess-f30-10-acq", kind="breach")
        estop.engage(reason="operator pause before the plugin engages")  # pre-existing foreign pause
        with _sentinel_untouched(estop, "engage-on-breach must not clobber an existing operator pause"):
            _engage_owned(mod, "sess-f30-10-acq")  # link-publish EEXIST-leaves; records no receipt
        assert mod.store.estop_receipt() is None, "an EEXIST-leave must record no ownership"
        with _sentinel_untouched(estop, "the operator pause must survive the resolve"), \
                _fail_if_sentinel_unlinked(estop):
            res = mod.resolve_escalation("sess-f30-10-acq", ep_q, 1, "continue", OPERATOR)
        assert res["granted"] is True, res
        assert estop.sentinel_path().exists(), "the pre-existing operator pause must survive"
        _assert_left_belt(res, mod, why="Continue while a foreign pre-existing operator pause is engaged", expect_notice=True)

    # (vii) FLEET-ROOT pause: an unqualified operator `hermes pause` writes the fleet-root sentinel (the
    # BASE home = fleet root, no override). is_engaged() is fleet-root-inclusive, so a Continue resolved
    # in the base home leaves resumes=false, the fleet-root sentinel untouched, no receipt adopted, and no
    # deletion primitive invoked — the plugin never lifts a fleet-root pause either.
    ep_fr = _seed_episode(mod, "sess-f30-10-fleet", kind="breach")
    estop.engage(reason="unqualified operator fleet-root pause")  # base home => fleet-root sentinel
    with _sentinel_untouched(estop, "Continue must leave a FLEET-ROOT pause byte-for-byte"), \
            _fail_if_sentinel_unlinked(estop):
        res = mod.resolve_escalation("sess-f30-10-fleet", ep_fr, 1, "continue", OPERATOR)
    assert res["granted"] is True, res
    assert estop.sentinel_path().exists(), "the fleet-root pause must survive a Continue"
    assert mod.store.estop_receipt() is None, "a fleet-root pause is never adopted as owned"
    _assert_left_belt(res, mod, why="Continue while a fleet-root operator pause is engaged", expect_notice=True)


def test_estop_acquisition_barrier(loaded_plugin, monkeypatch):
    """Concurrent-publication race on a REAL production engage path (discriminator for the atomic-publish
    engage): a mortal resolve_escalation("stop") engages the profile-local sentinel through
    store.engage_owned's atomic no-replace os.link — the SAME seam the breach path uses, not a synthetic
    engage. With os.link intercepted to block immediately before the real link, an operator estop.engage()
    lands FIRST, so the plugin's link raises EEXIST, EEXIST-leaves the operator bytes/inode, and records
    NO matching receipt. The unsafe O_EXCL-create-then-write would clobber the operator pause here, so this
    distinguishes the fix from that bug. Runs on the base (unpinned) home so both threads observe the same
    sentinel without copying the ContextVar override."""
    import os

    from agent import estop

    mod = loaded_plugin.module
    ep = _seed_episode(mod, "sess-f30-acq-bar", kind="escalation", blocked=False)
    real_link = os.link
    at_link = threading.Event()
    release = threading.Event()

    def _blocking_link(src, dst, *args, **kwargs):
        # block the plugin's publish immediately before the real link so an operator pause can race in
        if str(dst) == str(estop.sentinel_path()):
            at_link.set()
            release.wait(5)
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "link", _blocking_link)
    box = {}

    def _drive_stop():
        try:
            # A mortal Stop engages the profile-local sentinel through store.engage_owned's link-publish
            # — the SAME production engage seam the breach path uses — so driving it drives real logic.
            box["res"] = mod.resolve_escalation("sess-f30-acq-bar", ep, 1, "stop", OPERATOR)
        except Exception as exc:          # noqa: BLE001 — EEXIST must be handled INTERNALLY; any leak is a defect
            box["err"] = exc

    t = threading.Thread(target=_drive_stop)
    t.start()
    assert at_link.wait(5), "the Stop engage never reached os.link — it must publish via os.link(temp, sentinel)"
    estop.engage(reason="operator pause during the plugin publish")  # lands while the plugin is blocked
    op_bytes = estop.sentinel_path().read_bytes()
    op_ino = estop.sentinel_path().stat().st_ino
    release.set()
    t.join(5)
    assert not t.is_alive(), "Stop thread hung"
    assert box.get("err") is None, ("resolve_escalation must handle EEXIST internally, not raise", box)
    # SAFETY: the operator pause survives byte-for-byte and is not deleted-and-rewritten by the plugin.
    assert estop.sentinel_path().read_bytes() == op_bytes, "operator pause clobbered by the plugin's link"
    assert estop.sentinel_path().stat().st_ino == op_ino, "operator pause deleted-and-rewritten"
    assert mod.store.estop_receipt() is None, "the EEXIST-leave must record no ownership"
    # the plugin owns nothing: a FRESH resolve reads FOREIGN and does not resume.
    ep2 = _seed_episode(mod, "sess-f30-acq-bar-2", kind="breach")
    res2 = mod.resolve_escalation("sess-f30-acq-bar-2", ep2, 1, "continue", OPERATOR)
    assert res2.get("resumes") is False, "a foreign operator pause must not be owned or resumed"


def test_estop_engage_fault_injection(loaded_plugin, monkeypatch):
    """Fault-injection on the engage path — the plugin NEVER cleanup-unlinks (a cleanup unlink would race
    the lockless core primitive), and never adopts ownership it does not hold:
    (A) os.link unsupported (OSError) -> the plugin falls back to an O_EXCL empty-payload engage that still
        fail-safe engages (the sentinel EXISTS) but records NO receipt, so a later resolve reads it FOREIGN
        (resumes=false) and never unlinks it.
    (B) the receipt write fails AFTER a successful os.link -> the plugin LEAVES the linked sentinel, records
        NO ownership (estop_receipt() is None), and issues NO cleanup unlink.
    Runs on the base (unpinned) home. os.link and the receipt-write seam are patched inside scoped
    monkeypatch contexts so the fixture's HERMES_HOME/HOME env is never disturbed."""
    import os

    from agent import estop

    mod = loaded_plugin.module

    # (A) os.link raises -> O_EXCL fail-safe fallback engages, no receipt; no unlink primitive is invoked.
    # The os.link patch is scoped so it never reaches the later resolve, and the fixture env is untouched.
    ep_a = _seed_episode(mod, "sess-f30-fault-a", kind="breach")

    def _no_link(*a, **k):
        raise OSError("hardlinks unsupported on this filesystem")

    with monkeypatch.context() as link_patch:
        link_patch.setattr(os, "link", _no_link)
        with _fail_if_sentinel_unlinked(estop):
            _engage_owned(mod, "sess-f30-fault-a")  # link raises -> O_EXCL empty fallback engages fail-safe
    assert estop.sentinel_path().exists(), "engage must fail-safe (fallback engages) even without os.link"
    assert mod.store.estop_receipt() is None, "the O_EXCL fallback records no ownership"
    with _fail_if_sentinel_unlinked(estop):
        res = mod.resolve_escalation("sess-f30-fault-a", ep_a, 1, "continue", OPERATOR)
    assert res["granted"] is True, res
    assert estop.sentinel_path().exists(), "the fallback sentinel (no receipt) is FOREIGN -> left"
    _assert_left_belt(res, mod, why="resolve over an unowned O_EXCL fallback sentinel", expect_notice=True)

    # Isolate case B: the plugin never lifts the belt, so the test clears case A's fallback sentinel via the
    # operator core API. Otherwise case B's engage would find an existing sentinel, take the no-replace EEXIST
    # path, and never reach the patched receipt-write seam — leaving the fault untested.
    estop.disengage()
    assert not estop.sentinel_path().exists()

    # (B) inject a receipt-write failure AFTER os.link succeeds by making the discrete record_estop_receipt
    # seam raise, then engage through the real Stop path (resolve_escalation("stop") -> store.engage_owned):
    # the sentinel must be LEFT (linked), estop_receipt() None (no ownership adopted), and NO deletion
    # primitive invoked. A cleanup unlink after a receipt failure would race the primitive and is a defect.
    ep_b = _seed_episode(mod, "sess-f30-fault-b", kind="escalation", blocked=False)
    receipt_writes = 0

    def _raise_receipt(*a, **k):
        nonlocal receipt_writes
        receipt_writes += 1
        raise RuntimeError("plugin_db receipt write failed after os.link")

    with monkeypatch.context() as receipt_patch:
        receipt_patch.setattr(mod.store, "record_estop_receipt", _raise_receipt)
        with _fail_if_sentinel_unlinked(estop):
            res_b = mod.resolve_escalation("sess-f30-fault-b", ep_b, 1, "stop", OPERATOR)
    assert res_b["granted"] is True, res_b
    assert receipt_writes == 1, "case B did not reach the post-link receipt-write seam exactly once"
    assert dict(mod.store.get_state("sess-f30-fault-b"))["blocked"] == 1, "the Stop money-side resolution still applies"
    assert estop.sentinel_path().exists(), "a receipt-write failure must LEAVE the linked sentinel"
    assert mod.store.estop_receipt() is None, "a receipt-write failure must adopt NO ownership"


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


def test_cli_resolve_orchestrator_gate_pin_and_principal(loaded_plugin, monkeypatch):
    """The `hermes cost-cap resolve` CLI fallback gates callers to the orchestrator profile, pins the
    target --profile around the resolution, authorizes it against a constructed cli: principal, and
    requires --amount-usd for set-ceiling — the CLI seam _resolve_core (AC-1..5) does not exercise."""
    import argparse

    from hermes_cli import profiles as _profiles

    mod = loaded_plugin.module

    # The target profile owns the episode and must list the actor the handler constructs, because
    # resolve_via_cli authorizes under the target's pin (fleet_admin_id unset -> admin id = active).
    cli_actor = "cli:orchestrator:orchestrator"
    _write_profile_ceiling("profa", 10.0, operators=[OP_DASH, OPERATOR, cli_actor])
    with _pinned_home("profa"):
        episode = _seed_episode(mod, "sess-cli-resolve", kind="breach")
        before = dict(mod.store.get_state("sess-cli-resolve"))

    def _ns(**over):
        base = dict(cost_cap_command="resolve", profile="profa", session_id="sess-cli-resolve",
                    episode_id=episode, budget_gen=1, decision="continue", amount_usd=None)
        base.update(over)
        return argparse.Namespace(**base)

    # Drive the active-vs-orchestrator relation explicitly: the sandbox HERMES_HOME resolves the
    # active profile to the default (== the default orchestrator), which would mask the operational
    # gate. That gate precedes the authz seam, so a non-orchestrator caller is refused with a
    # top-level reason and no resolution row (of any status) is written.
    monkeypatch.setattr(mod, "_orchestrator_profile", lambda: "orchestrator")
    monkeypatch.setattr(_profiles, "get_active_profile_name", lambda: "worker")
    refused = mod._cli_cost_cap(_ns())
    assert refused["ok"] is False and "orchestrator-only" in refused["reason"], refused
    with _pinned_home("profa"):
        assert mod.store.resolutions("sess-cli-resolve") == []
        assert dict(mod.store.get_state("sess-cli-resolve")) == before

    monkeypatch.setattr(_profiles, "get_active_profile_name", lambda: "orchestrator")

    missing_amt = mod._cli_cost_cap(_ns(decision="set-ceiling", amount_usd=None))
    assert missing_amt["ok"] is False and "amount-usd" in missing_amt["reason"], missing_amt
    with _pinned_home("profa"):
        assert mod.store.resolutions("sess-cli-resolve") == []
        assert dict(mod.store.get_state("sess-cli-resolve")) == before

    granted = mod._cli_cost_cap(_ns(decision="continue"))
    assert granted["ok"] is True and granted["actor"] == cli_actor, granted
    assert granted["result"].get("granted") is True, granted
    with _pinned_home("profa"):
        after = dict(mod.store.get_state("sess-cli-resolve"))
        applied = _applied(mod, "sess-cli-resolve")
    assert after["blocked"] == 0, after
    assert after["window_start_total"] == pytest.approx(before["window_start_total"] + INCREMENT_USD), after
    assert len(applied) == 1 and applied[0]["actor"] == cli_actor, applied
