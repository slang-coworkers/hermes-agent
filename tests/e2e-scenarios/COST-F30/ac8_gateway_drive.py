#!/usr/bin/env python3
"""AC-COST-F30-8 gateway-drive harness — hermetic, in-process, faithful to the runtime path.

The container has no real messaging platform, so this drives the plugin's OWN
``pre_gateway_dispatch`` callback exactly as ``GatewayRunner._handle_message`` would
(gateway/run.py:18141 `invoke_hook("pre_gateway_dispatch", event=, gateway=, session_store=)`),
with a forged ``SessionSource`` and a minimal gateway object exposing only the two methods the
plugin uses (`_session_key_for_source`, `_deliver_platform_notice`) plus a `session_store`
whose `peek_session_id` returns the crossed session. So `principal_from_event`, the durable
CAS, and the effect on the profile's plugin_db all run for real; only the transport is stubbed.

Usage (one invocation per gateway send):
    ac8_gateway_drive.py --home <profile-home> --user <uid> [--platform slack] [--scope ws-1]
                         [--session <id>] [--decision continue|stop|ceiling] [--amount <usd>]
Without --session it auto-discovers the session with a pending escalation episode (the one the
live turn crossed); with --session it targets that session directly (needed for a repeat drive of
an ALREADY-resolved episode, which has nothing pending). It forges `gateway:<platform>:<scope>:<user>`,
drives the hook, and prints one JSON line:
    {"session": ..., "principal": ..., "hook_result": {...}, "resumes": <bool>,
     "estop_disposition": "left"|"absent"|null, "manual_resume_required": <bool>,
     "estop_engaged": <bool|null>, "sentinel_exists": <bool|null>,
     "notice": ..., "before": {...}, "after": {...}}
`hook_result` is the pre_gateway_dispatch return ({"action":"skip",...} when /cost is consumed).
`resumes` is the plugin's OWN honest predicate `store.session_resumes(session_id)` = own-runnability
AND NOT `estop.is_engaged()`, read against the REAL on-disk state — NOT a next_call/middleware probe
(which bypasses the ordinary-inbound ESTOP gate and would falsely report a resume for a belt-left
session, Orchestrator msg 82 R2). `estop_disposition`/`manual_resume_required` mirror the plugin's
result surface; `estop_engaged`/`sentinel_exists` are the on-disk belt proof. The scenario asserts on
`after` (window_start_total advance / blocked / applied rows), `notice`, `hook_result.action`,
`resumes`, and the belt fields.

AC-8 Part B (seeded priced-path): `--seed-priced-breach --session <id>` sets `estop_on_breach=false`
(config + the boundary-eval ctx), clears any sentinel Part A left (a test-side operator resume, never
the plugin), then seeds a PRICED `breach` through the REAL COST-F29 accrual path (`record_api_request`
with the priced model `claude-opus-4-8` + `_evaluate_boundary`) so it blocks IN-BAND with NO sentinel,
prints `{"seeded","priced_total","crossed","episode_kinds","unpriced_signal","state","estop_engaged",
"sentinel_exists"}`, and exits WITHOUT a gateway drive (no live model call). The subsequent drive
invocations then run on the same session as in Part A — and because no belt gates it, an authorized
Continue on the priced breach yields `resumes=true` / `estop_disposition="absent"` (unlike Part A's
belt-left `unknown_pricing` crossing, which correctly stays `resumes=false` / `estop_disposition="left"`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone


def _state(mod, session_id):
    st = dict(mod.store.get_state(session_id))
    applied = [r for r in mod.store.resolutions(session_id) if r["status"] == "applied"]
    return {
        "window_start_total": st.get("window_start_total"),
        "day_start_total": st.get("day_start_total"),
        "blocked": st.get("blocked"),
        "applied": len(applied),
        "resolutions": len(mod.store.resolutions(session_id)),
    }


def _is_engaged(estop):
    try:
        return bool(estop.is_engaged())
    except Exception:
        return None


def _sentinel_exists(estop):
    try:
        return estop.sentinel_path().exists()
    except Exception:
        return None


def _set_config_estop_on_breach(home, value):
    """Persist plugins.entries.nv-cost-cap.settings.estop_on_breach into the profile config.yaml
    (AC-8 Part B: estop_on_breach=false so the priced breach blocks IN-BAND without a sentinel)."""
    import yaml

    path = os.path.join(home, "config.yaml")
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        cfg = {}
    settings = cfg.setdefault("plugins", {}).setdefault("entries", {}).setdefault(
        "nv-cost-cap", {}).setdefault("settings", {})
    settings["estop_on_breach"] = value
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh)


async def _run(args) -> int:
    os.environ["HERMES_HOME"] = args.home
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    os.environ["HERMES_BUNDLED_PLUGINS"] = os.path.join(repo_root, "plugins")
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    import hermes_cli.plugins as plugins_mod
    from agent import estop
    from gateway.config import Platform
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.session import SessionSource
    from hermes_cli.plugins import PluginManager

    manager = PluginManager()
    manager.discover_and_load()
    # Pin the process-global manager to THIS instance (the sanctioned embedder hook,
    # hermes_cli/plugins.py:6224-6233) so run_llm_execution_middleware — which resolves
    # callbacks via get_plugin_manager()._middleware — runs OUR registered _llm_execution.
    plugins_mod._plugin_manager = manager
    loaded = manager._plugins["nv-cost-cap"]
    assert loaded.enabled and loaded.module is not None, getattr(loaded, "error", None)
    mod = loaded.module

    # provider="anthropic" routes claude-opus-4-8 to the PRICED table entry
    # (usage_pricing.py:211-222); the default/unknown route fail-closes as unknown_pricing
    # and never resumes, so only a priced breach can prove Part B's resume.
    if args.seed_priced_breach:
        if not args.session:
            print(json.dumps({"error": "--seed-priced-breach requires --session"}))
            return 2
        session_id = args.session
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        platform_value = Platform(args.platform).value
        # Part B runs estop_on_breach=false so the priced breach blocks IN-BAND without engaging a
        # sentinel; persist it to config.yaml AND stub _CTX (which _cfg_bool reads) to False for the
        # boundary evaluation. Clear any sentinel Part A's crossing left (the operator `hermes resume`
        # equivalent — a test action, never the plugin) so the profile home is verifiably belt-free
        # for the resume proof.
        _set_config_estop_on_breach(args.home, False)

        class _SeedCtx:
            def get_config(self, key, default=None):
                return False if key == "estop_on_breach" else default

            def spawn_task(self, coro, name=None):
                return None

        mod._CTX = _SeedCtx()
        try:
            estop.disengage()
        except Exception:
            pass
        priced_total = mod.record_api_request(
            session_id, "claude-opus-4-8",
            {"input_tokens": 20000, "output_tokens": 5000},
            "ac8-priced-seed", provider="anthropic",
        )
        crossed = mod._evaluate_boundary(session_id, today=today, platform=platform_value)
        try:
            estop.disengage()  # defensive: guarantee no sentinel regardless of the config read path
        except Exception:
            pass
        kinds = sorted({e["kind"] for e in mod.episodes(session_id)})
        print(json.dumps({
            "seeded": session_id, "priced_total": priced_total, "crossed": crossed,
            "episode_kinds": kinds, "unpriced_signal": mod._unpriced_signal(session_id),
            "state": _state(mod, session_id),
            "estop_engaged": _is_engaged(estop), "sentinel_exists": _sentinel_exists(estop),
        }))
        return 0

    # Target the given session (a repeat drive of an ALREADY-resolved episode has nothing
    # pending, so it must be named explicitly), else auto-discover the session the live turn
    # crossed (the one left with a pending episode).
    session_id = args.session
    if not session_id:
        pending = mod.store.pending_escalations()
        if not pending:
            print(json.dumps({"error": "no pending escalation — the live turn did not cross"}))
            return 2
        session_id = pending[0]["session"]

    # A real MessageEvent with a forged source; principal = gateway:<platform>:<scope>:<user>.
    source = SessionSource(
        platform=Platform(args.platform),
        chat_id="c-ac8",
        chat_type="dm",
        user_id=args.user,
        scope_id=args.scope,
    )
    text = f"/cost {args.decision}" + (f" {args.amount}" if args.amount else "")
    # internal=False: a real operator /cost is a user message — the same non-internal event
    # GatewayRunner._handle_message forwards to pre_gateway_dispatch (the hook fires only for
    # non-internal messages, gateway/run.py:18138).
    event = MessageEvent(text=text, message_type=MessageType.TEXT, source=source,
                         message_id="ac8", internal=False)

    notices: list[str] = []
    loop = asyncio.get_running_loop()

    class _Gateway:
        def __init__(self, store_):
            self.session_store = store_

        def _session_key_for_source(self, src):
            return "ac8-key"

        async def _deliver_platform_notice(self, src, content):
            notices.append(content)

    class _SessionStore:
        def peek_session_id(self, key):
            return session_id

    class _Ctx:
        # Only spawn_task/get_config are touched on the gateway path; run the scheduled
        # notice coroutine on THIS loop so it is captured before we read it back.
        def spawn_task(self, coro, name=None):
            return loop.create_task(coro)

        def get_config(self, key, default=None):
            return default

    ss = _SessionStore()
    gw = _Gateway(ss)
    mod._CTX = _Ctx()
    mod._reconcile_loop_started = True  # do not start the periodic reconcile loop in the harness

    before = _state(mod, session_id)
    principal = mod.principal_from_event(event)
    hook_result = mod._pre_gateway_dispatch(event=event, gateway=gw, session_store=ss)
    await asyncio.sleep(0.05)  # let the scheduled _deliver_platform_notice run
    after = _state(mod, session_id)
    # "Does the session's NEXT turn run now?" — the plugin's OWN honest predicate against the REAL
    # on-disk state: session_resumes() = own-runnability AND NOT estop.is_engaged(). It is FALSE while
    # a profile ESTOP belt stands (the plugin never unlinks it), because the belt gates an ordinary new
    # inbound at gateway/run.py:18245-18314. NOT a next_call/middleware probe — a next_call bypasses
    # that gate and would falsely report a resume for a belt-left session (Orchestrator msg 82 R2).
    resumes = mod.store.session_resumes(session_id)
    engaged = _is_engaged(estop)
    estop_disposition = "left" if engaged else ("absent" if engaged is not None else None)

    print(json.dumps({
        "session": session_id, "principal": principal,
        "hook_result": hook_result, "resumes": resumes,
        "estop_disposition": estop_disposition,
        "manual_resume_required": bool(engaged),
        "estop_engaged": engaged, "sentinel_exists": _sentinel_exists(estop),
        "notice": notices[-1] if notices else None,
        "before": before, "after": after,
    }))
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--home", required=True)
    p.add_argument("--user", required=True)
    p.add_argument("--session", default="",
                   help="target this session directly (for a repeat drive of an already-resolved "
                        "episode, which has nothing pending to auto-discover)")
    p.add_argument("--platform", default="slack")
    p.add_argument("--scope", default="ws-1")
    p.add_argument("--decision", default="continue")
    p.add_argument("--amount", default="")
    p.add_argument("--seed-priced-breach", action="store_true", dest="seed_priced_breach",
                   help="AC-8 Part B: seed a PRICED breach (model claude-opus-4-8) on --session "
                        "via the real record_api_request + _evaluate_boundary accrual path, print "
                        "the seeded state, and exit (no gateway drive); no live model call")
    return asyncio.run(_run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
