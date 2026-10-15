"""on_session_start re-assert for the podman-onecli plugin (CRED-F28).

This hook is a re-assert, NOT the bootstrap: onboarding (identity creation +
secret grants) is the onecli-onboard CLI, run before render. The hook only
re-asserts the LAUNCH profile's identity (idempotent ensure_agent) and acts
only when the active profile is a real named profile — never "default" /
"custom". Its kwargs are session_id / model / platform only, so it cannot mint
per-profile agents for gateway turns. Fail-open: a hook error never breaks a
session.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_SENTINEL_PROFILES = frozenset({"default", "custom"})


def reconcile_identity(**kwargs) -> None:
    try:
        from hermes_cli.profiles import get_active_profile_name

        from . import oneclient

        name = get_active_profile_name()
        if not name or name in _SENTINEL_PROFILES:
            return
        oneclient.ensure_agent(identifier=name)
    except Exception as exc:
        logger.warning("OneCLI identity re-assert skipped: %s", exc, exc_info=True)
