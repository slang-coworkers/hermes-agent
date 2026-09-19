"""podman-onecli plugin (CRED-F28): per-profile OneCLI egress identity.

register(ctx) wires the OneCLI SecretSource (proxy coordinates only), the
on_session_start identity re-assert, and the onecli-onboard CLI command. It
registers NO pre_tool_call veto and NO TerminalEnvironmentProvider — the
fail-closed guarantee is provided by the shipped bin/podman-onecli-wrap set as
HERMES_DOCKER_BINARY, not by a core hook. The submodule is bound via a relative
import so callers can reach ``<plugin>.oneclient`` on the loaded module.
"""
from __future__ import annotations

from . import oneclient
from .cli import make_onboard_handler, onboard_setup
from .hooks import reconcile_identity
from .secret_source import OneCLISecretSource


def register(ctx) -> None:
    api_key_env = ctx.get_config("api_key_env", "ONECLI_API_KEY") or "ONECLI_API_KEY"
    oneclient.configure(
        gateway_api_base_url=ctx.get_config("gateway_api_base_url"),
        api_key_env=api_key_env,
        insecure_no_auth_origins=ctx.get_config("insecure_no_auth_origins", []),
    )
    ctx.register_secret_source(OneCLISecretSource(api_key_env=api_key_env))
    ctx.register_hook("on_session_start", reconcile_identity)
    profile_secret_sets = ctx.get_config("profile_secret_sets", {}) or {}
    ctx.register_cli_command(
        name="onecli-onboard",
        help="Ensure this profile's OneCLI identity and secret grants",
        setup_fn=onboard_setup,
        handler_fn=make_onboard_handler(profile_secret_sets),
    )
