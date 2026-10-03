"""nv-bot-chat — surfaces each served profile's hidden canonical "Bot Chat" (OSH-F64.c).

``hermes bot-chats`` prints the served set's canonical Bot Chats as JSON; the
``dashboard/`` extension lists and opens them in the web dashboard through the
stock dashboard-plugin mechanism (stock web cannot list hidden sessions).
"""
from __future__ import annotations

import json

from . import resolver


def _setup_bot_chats(parser) -> None:
    """No arguments: the served set comes from the gateway config."""


def _cli_bot_chats(args) -> int:
    # `hermes` uses a handler's return value only as the exit code, so the
    # listing has to be printed.
    print(json.dumps({"bot_chats": resolver.list_canonical_bot_chats()}, indent=2))
    return 0


def register(ctx) -> None:
    ctx.register_cli_command(
        name="bot-chats",
        help="List the served profiles' hidden canonical Bot Chats (JSON)",
        setup_fn=_setup_bot_chats,
        handler_fn=_cli_bot_chats,
        description=(
            "Print, as JSON, each profile the gateway serves whose session store "
            "holds a hidden canonical \"Bot Chat\": its session id, live "
            "compression tip and message count."
        ),
    )
