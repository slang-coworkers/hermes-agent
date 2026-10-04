"""Delivery transport: the release CLI, no secret, no HTTP.

``run_cli`` is the single subprocess seam. It runs one CLI turn under the
target profile's cross-process turn lock (release ``tools/bot_relay.py``), the
same lock ``message_agent`` deliveries take, so an ingress delivery never races
a Bot Chat delivery into the same profile.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DELIVERY_TIMEOUT_SECONDS = 1800


def _hermes_cli() -> str:
    from tools.bot_relay import _hermes_cli as release_cli

    return release_cli()


def bot_chat_argv(profile: str, query_file: str) -> List[str]:
    """The release local Bot Chat delivery argv (created on first use, as ``message_agent``)."""
    from tools.bot_relay import local_delivery_command

    return local_delivery_command(profile, query_file)


def resume_argv(profile: str, session_id: str, query_file: str) -> List[str]:
    """Resume the owner's EXISTING session; an unknown id exits non-zero and creates nothing."""
    return [_hermes_cli(), "-p", profile, "chat", "-Q", "--resume", session_id,
            "--query-file", query_file]


def run_cli(argv: List[str], prompt_path: str) -> int:
    """Run one delivery turn; returns the CLI exit code (non-zero = not delivered)."""
    from hermes_cli.profiles import get_profile_dir
    from tools.bot_relay import TurnBusyError, acquire_turn_lock

    profile = argv[argv.index("-p") + 1]
    root = get_profile_dir("default")
    try:
        with acquire_turn_lock(root, profile):
            proc = subprocess.run(argv, check=False, stdin=subprocess.DEVNULL, capture_output=True,
                                  text=True, timeout=DELIVERY_TIMEOUT_SECONDS)
    except TurnBusyError:
        logger.info("nv-ingress delivery to %s deferred: target busy", profile)
        return 75
    except subprocess.TimeoutExpired:
        logger.warning("nv-ingress delivery to %s timed out", profile)
        return 124
    if proc.returncode != 0:
        logger.warning("nv-ingress delivery to %s exited %s: %s", profile, proc.returncode,
                       (proc.stderr or proc.stdout or "").strip()[-300:])
    return proc.returncode


def write_prompt(text: str) -> str:
    # mkstemp creates the file 0600, so the prompt is never world-readable.
    fd, path = tempfile.mkstemp(prefix="nv-ingress-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    except BaseException:
        Path(path).unlink(missing_ok=True)
        raise
    return path


def marker(env: Dict[str, Any], pr: Optional[int]) -> str:
    tail = f"/{pr}" if pr is not None else ""
    return f"ingress-delivery: {env.get('source')}/{env.get('delivery_id')}{tail}"


def prompt(env: Dict[str, Any], pr: Optional[int], *, unowned: bool) -> str:
    """One short functional paragraph naming the event, then the delivery marker (D5)."""
    repo = env.get("repo") or "?"
    kind = env.get("kind")
    subject = f"{repo}#{pr}" if pr is not None else f"{repo} issue #{env.get('number')}"
    parts = [f"Inbound {env.get('source')} {env.get('event')} event"
             + (f" ({env.get('action')})" if env.get("action") else "") + f" for {subject}."]
    if env.get("title"):
        parts.append(f"Title: {env['title']}.")
    if kind == "ci":
        parts.append(f"Check {env.get('name') or '?'} concluded {env.get('conclusion') or 'pending'}"
                     f" at head {env.get('head_sha') or '?'}.")
        parts.append("Read the checks API through the ci-gate skill before acting on this CI result.")
    if env.get("body"):
        parts.append(f"Text: {env['body']}")
    if env.get("url"):
        parts.append(f"Link: {env['url']}.")
    if env.get("self_authored"):
        parts.append("This event is self-authored: it records the fleet's own action, so do not"
                     " re-act to it.")
    if unowned and kind != "issue":
        parts.append("This is an unowned PR event: no fleet coworker has claimed this PR; decide"
                     " whether anyone should act on it.")
    return "\n".join(parts) + "\n\n" + marker(env, pr) + "\n"
