"""onecli-onboard CLI command for the podman-onecli plugin (CRED-F28).

Ensures a profile's OneCLI identity and its selective secret grants BEFORE any
render, in the order OneCLI requires: ensure_agent -> set_secrets ->
get_container_config. Invoked by the compose/onboard step in a process before
the gateway's own SecretSource.fetch(). Stays selective (never mode "all"); an
empty grant leaves the agent ungranted so an unassigned sibling still gets 401.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from . import oneclient

logger = logging.getLogger(__name__)


def onboard_setup(parser) -> None:
    parser.add_argument(
        "--profile",
        required=True,
        help="Profile name or path; the OneCLI identifier is its basename.",
    )


def _identifier_from(profile: str) -> str:
    identifier = Path(profile).expanduser().name
    if not identifier:
        raise ValueError(f"could not derive a profile identifier from {profile!r}")
    return identifier


def make_onboard_handler(profile_secret_sets):
    """Build the argparse handler, capturing the plugin's configured grants.

    ``handler_fn`` is called with only the argparse namespace, so the config
    read at register() time is captured here rather than re-read at call time.
    """
    sets = dict(profile_secret_sets or {})

    def onboard_handler(args) -> None:
        identifier = _identifier_from(args.profile)
        oneclient.ensure_agent(identifier=identifier)
        secrets = list(sets.get(identifier, []) or [])
        oneclient.set_secrets(identifier=identifier, secrets=secrets)
        oneclient.get_container_config(agent=identifier)
        _persist_identity(identifier, secrets_granted=len(secrets))

    return onboard_handler


def _persist_identity(identifier: str, *, secrets_granted: int) -> None:
    # Best-effort audit reference only — never a token or the bootstrap key.
    try:
        from plugins.plugin_storage import plugin_data_dir

        ref = Path(plugin_data_dir("podman-onecli")) / "identities" / f"{identifier}.json"
        ref.parent.mkdir(parents=True, exist_ok=True)
        ref.write_text(
            json.dumps({"identifier": identifier, "secrets_granted": secrets_granted}),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning(
            "OneCLI identity reference not persisted for %s: %s", identifier, exc, exc_info=True
        )
