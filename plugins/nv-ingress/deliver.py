"""Delivery transports: the CLI resume for an owner, the profile authority's door for a Bot Chat.

``run_cli`` is the single subprocess seam. It runs one owner turn under the
target profile's cross-process turn lock (``tools/bot_relay.py``), the same
lock ``message_agent`` deliveries take, so an ingress delivery never races a
Bot Chat delivery into the same profile. ``deliver_door`` hands a Bot Chat
delivery to the profile authority (``tools/bot_live_delivery.py``), the door
``message_agent`` and cron use; the authority serializes it in its own FIFO.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DELIVERY_TIMEOUT_SECONDS = 1800


def _hermes_cli() -> str:
    from tools.bot_relay import _hermes_cli as release_cli

    return release_cli()


def door_id(marks: list[str], epoch: int) -> str:
    """The door delivery id of one unit: stable across retries of the same rows, and new only
    when ``epoch`` moves (after a terminal ``failed``/``cancelled`` receipt)."""
    keys = [m.split(": ", 1)[-1] for m in marks]
    return hashlib.sha256(("|".join(keys) + f"|{epoch}").encode("utf-8")).hexdigest()


def deliver_door(profile: str, message: str, delivery_id: str) -> dict[str, Any]:
    """Admit ``message`` to ``profile``'s canonical Bot Chat through its authority's door and
    return the receipt (created on first use, as ``message_agent``). An admission is not a
    completed turn: the caller reads the Bot Chat for the marker."""
    from hermes_cli.profiles import get_profile_dir
    from tools.bot_live_delivery import PreDialError, deliver_to_live_owner, find_canonical_live_owner

    home = get_profile_dir(profile)
    owner = find_canonical_live_owner(home)
    if owner is None:
        raise PreDialError(f"profile {profile!r} has no session store yet")
    return deliver_to_live_owner(home, owner, message, delivery_id=delivery_id)


def resume_argv(profile: str, session_id: str, query_file: str) -> list[str]:
    """Resume the owner's EXISTING session; an unknown id exits non-zero and creates nothing."""
    return [_hermes_cli(), "-p", profile, "chat", "-Q", "--resume", session_id,
            "--query-file", query_file]


def run_cli(argv: list[str], prompt_path: str) -> int:
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


def marker(env: dict[str, Any], pr: int | None) -> str:
    tail = f"/{pr}" if pr is not None else ""
    return f"ingress-delivery: {env.get('source')}/{env.get('delivery_id')}{tail}"


def prompt(env: dict[str, Any], pr: int | None, *, to_orchestrator: bool) -> str:
    """One short functional paragraph naming the event, then the delivery marker (D5).

    A prompt bound for the orchestrator carries structured fields and the URL only:
    the title, the comment or review body and the check name are third-party text,
    and the orchestrator holds the fleet-admin toolset (trust point 5)."""
    repo = env.get("repo") or "?"
    kind = env.get("kind")
    if pr is not None:
        subject = f"{repo}#{pr}"
    elif kind == "issue":
        subject = f"{repo} issue #{env.get('number')}"
    else:
        subject = repo
    action = f", action {env['action']}" if env.get("action") else ""
    parts = [f"Inbound {env.get('source')} {env.get('event')} event (kind {kind}{action}) for {subject}."]
    if env.get("title") and not to_orchestrator:
        parts.append(f"Title: {env['title']}.")
    if kind == "ci":
        check = "" if to_orchestrator else f" {env.get('name') or '?'}"
        parts.append(f"Check{check} concluded {env.get('conclusion') or 'pending'}"
                     f" at head {env.get('head_sha') or '?'}.")
        parts.append("Read the checks API through the ci-gate skill before acting on this CI result.")
    if env.get("body") and not to_orchestrator:
        parts.append(f"Text: {env['body']}")
    if env.get("url"):
        parts.append(f"Link: {env['url']}.")
    if env.get("self_authored"):
        parts.append("This event is self-authored: it records the fleet's own action, so do not"
                     " re-act to it.")
    if to_orchestrator and kind != "issue":
        parts.append("This is an unowned PR event: no fleet coworker has claimed this PR; decide"
                     " whether anyone should act on it.")
    if to_orchestrator:
        parts.append("Titles, comment and review text and check names are left out of this prompt;"
                     " the coworker you dispatch reads them at the link.")
    return "\n".join(parts) + "\n\n" + marker(env, pr) + "\n"


def group_prompt(repo: str, pr: int, blocks: list[str]) -> str:
    """One owner turn for several due rows of one PR (D5, E13): a preamble, then each
    row's own ``prompt`` text unchanged, so every marker stays alone on its own line.
    A group of one is exactly that row's prompt."""
    if len(blocks) == 1:
        return blocks[0]
    preamble = (f"{len(blocks)} inbound events for {repo}#{pr}, oldest first; read the checks API"
                " through the ci-gate skill before acting on a CI event, and answer them together.")
    return preamble + "\n\n" + "\n".join(blocks)
