"""hello plugin.

Registers a ``/hello`` slash command whose handler returns a greeting naming
the active profile, and retains the P1-HELLO ``post_tool_call`` observer hook
that appends one line per tool call to the plugin's own durable storage under
``<HERMES_HOME>/plugin-data/hello/``.
"""

import json
from datetime import datetime, timezone

from hermes_cli.profiles import get_active_profile_name
from plugins.plugin_storage import plugin_data_dir

_LOG_FILENAME = "hello.log"


def _record_tool_call(
    tool_name: str = "",
    task_id: str = "",
    session_id: str = "",
    duration_ms: int = 0,
    **_: object,
) -> None:
    # **kwargs is load-bearing twice over: invoke_hook withholds additive fields
    # from narrow callbacks (plugins.py:5546-5562), and doctor makes a missing
    # **kwargs an ERROR (plugin_dev.py:391-400), which would fail --ci.
    line = json.dumps(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool_name": tool_name,
            "task_id": task_id,
            "session_id": session_id,
            "duration_ms": duration_ms,
        },
        sort_keys=True,
    )
    # Resolved per call, and the whole write is self-contained: post_tool_call is
    # timeout-bounded (plugins.py:427) so this may execute on a daemon thread.
    log = plugin_data_dir("hello") / _LOG_FILENAME
    with log.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _hello_command(raw_args: str = "") -> str:
    # get_active_profile_name() resolves HERMES_HOME per call, so the greeting
    # follows the profile the command is invoked under.
    return f"Hello from {get_active_profile_name()}!"


def register(ctx) -> None:
    ctx.register_hook("post_tool_call", _record_tool_call)
    ctx.register_command(
        "hello",
        _hello_command,
        description="Greet, naming the active profile.",
    )
