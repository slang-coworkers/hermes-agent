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
import re
from pathlib import Path

from . import oneclient

logger = logging.getLogger(__name__)

# The identifier becomes a OneCLI agent name and a URL path segment, so it is
# constrained to a safe grammar rather than URL-quoted after the fact.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


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
    if not _IDENTIFIER_RE.match(identifier):
        raise ValueError(f"unsafe OneCLI profile identifier {identifier!r}")
    return identifier


def make_onboard_handler(profile_secret_sets):
    """Build the argparse handler, capturing the plugin's configured grants.

    ``handler_fn`` is called with only the argparse namespace, so the config
    read at register() time is captured here rather than re-read at call time.
    """
    sets = dict(profile_secret_sets or {})

    def onboard_handler(args) -> None:
        identifier = _identifier_from(args.profile)
        if identifier not in sets:
            raise ValueError(
                f"no plugins.entries.podman-onecli.settings.profile_secret_sets entry "
                f"for {identifier!r}; add it (use [] to leave the agent ungranted)"
            )
        raw = sets[identifier]
        if not isinstance(raw, list) or not all(
            isinstance(s, str) and s for s in raw
        ):
            raise ValueError(
                f"profile_secret_sets[{identifier!r}] must be a list of non-empty "
                f"provider names ([] to leave ungranted; got {raw!r})"
            )
        secrets = list(raw)
        oneclient.ensure_agent(identifier=identifier)
        oneclient.set_secrets(identifier=identifier, secrets=secrets)
        config = oneclient.get_container_config(agent=identifier)
        if config is None:
            raise oneclient.OneCLIError(
                f"onboarding {identifier!r}: container-config did not resolve after "
                f"ensure_agent/set_secrets"
            )
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
