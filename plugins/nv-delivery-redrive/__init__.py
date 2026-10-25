"""nv-delivery-redrive: a busy target's local ``message_agent`` delivery is re-driven automatically.

Send side (``tool_execution`` middleware, observe-only, + ``post_tool_call``) records a ticket per tracked local
delivery; the gateway's ``on_kanban_dispatch_tick`` drainer owns it until a typed final outcome, re-driving core's
own delivery runner under the same ``delivery_id``; ``transform_terminal_output`` keeps a tracked busy completion
truthful ("queued, do NOT resend"). Tracking needs ``orchestrator_profile`` and a live drainer heartbeat.
"""

from __future__ import annotations

import logging
import sys

from . import drain, notify, probe, send, settings, store

logger = logging.getLogger(__name__)

__all__ = ["drain", "notify", "probe", "register", "send", "settings", "store"]

# Core-private names this plugin calls instead of copying (ADR A6); a rename disables the plugin loudly.
_CORE_NAMES = {
    "tools.bot_mode_dm": ("_delivery_command", "_runner_argv", "_dm_delivery_id", "_dm_dir", "_live_intent_file",
                          "_resolve_local_name", "_LOCAL_TARGET_RE"),
    "tools.bot_relay": ("acquire_turn_lock", "BOT_CHAT_TURN_ARGS", "_hermes_cli", "turn_wait_seconds",
                        "turn_lock_path"),
    "tools.bot_live_delivery": ("read_delivery_result", "cancel_queued_delivery"),
    "tools.bot_mode_probe": ("_hermes_root", "_profile_name", "_roster", "_display_name", "_handle"),
    "tools.bot_failure_reasons": ("QUEUED_EXPIRED", "classify_agent_error"),
    "tools.process_registry": ("process_registry", "ProcessRegistry"),
}
_SESSION_FIELDS = ("_completion_event", "_reader_thread", "output_buffer", "detached", "exit_code", "owner_task_id",
                   "command")
_REGISTRY_METHODS = ("get", "list_sessions")


def _missing_core_names() -> list[str]:
    import dataclasses
    import importlib

    missing = []
    for module_name, names in _CORE_NAMES.items():
        try:
            module = importlib.import_module(module_name)
        except Exception:
            logger.debug("nv-delivery-redrive: core module %s did not import", module_name, exc_info=True)
            missing.append(module_name)
            continue
        missing.extend(f"{module_name}.{name}" for name in names if not hasattr(module, name))
    try:
        from tools.process_registry import ProcessRegistry, ProcessSession

        missing.extend(f"ProcessRegistry.{name}" for name in _REGISTRY_METHODS if not hasattr(ProcessRegistry, name))

        fields = {f.name for f in dataclasses.fields(ProcessSession)}
        missing.extend(f"ProcessSession.{name}" for name in _SESSION_FIELDS if name not in fields)
    except Exception:
        logger.debug("nv-delivery-redrive: process registry shape unreadable", exc_info=True)
        missing.append("tools.process_registry.ProcessSession")
    return missing


def register(ctx) -> None:
    if sys.platform == "win32":
        logger.info("nv-delivery-redrive: core's turn lock is a no-op without fcntl; re-drive is POSIX only")
        return
    missing = _missing_core_names()
    if missing:
        logger.error("nv-delivery-redrive: disabled; core names it depends on are missing: %s", ", ".join(missing))
        return
    settings.bind(ctx)
    settings.orchestrator(store.root())
    ctx.register_middleware("tool_execution", send.middleware)
    ctx.register_hook("post_tool_call", send.post_tool_call)
    ctx.register_hook("transform_terminal_output", send.transform_terminal_output)
    ctx.register_hook("on_kanban_dispatch_tick", drain.tick)
