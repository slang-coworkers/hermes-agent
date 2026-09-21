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
                         [--decision continue|stop|ceiling] [--amount <usd>]
It auto-discovers the session with a pending escalation episode (the one the live turn crossed),
forges `gateway:<platform>:<scope>:<user>`, drives the hook, and prints one JSON line:
    {"session": ..., "principal": ..., "notice": ..., "before": {...}, "after": {...}}
The scenario asserts on `after` (window_start_total advance / blocked / applied rows) and `notice`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys


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


async def _run(args) -> int:
    os.environ["HERMES_HOME"] = args.home
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    os.environ["HERMES_BUNDLED_PLUGINS"] = os.path.join(repo_root, "plugins")
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    from gateway.config import Platform
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.session import SessionSource
    from hermes_cli.plugins import PluginManager

    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins["nv-cost-cap"]
    assert loaded.enabled and loaded.module is not None, getattr(loaded, "error", None)
    mod = loaded.module

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
    event = MessageEvent(text=text, message_type=MessageType.TEXT, source=source,
                         message_id="ac8", internal=True)

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
    mod._pre_gateway_dispatch(event=event, gateway=gw, session_store=ss)
    await asyncio.sleep(0.05)  # let the scheduled _deliver_platform_notice run
    after = _state(mod, session_id)

    print(json.dumps({
        "session": session_id, "principal": principal,
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
    return asyncio.run(_run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
