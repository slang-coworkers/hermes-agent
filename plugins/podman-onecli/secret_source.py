"""OneCLI SecretSource (CRED-F28).

Resolves a profile's OneCLI proxy coordinates from the control plane. It never
resolves a real provider credential: OneCLI returns proxy URLs (carrying the
``aoc_`` agent token as userinfo), CA-trust vars, a CA path and a placeholder
provider key, and this source returns that ``env`` mapping verbatim. The
lowercase / extra CA aliases are synthesized by the render into
``terminal.docker_env``, not here.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import FrozenSet

from agent.secret_sources.base import ErrorKind, FetchResult, SecretSource

from . import oneclient

logger = logging.getLogger(__name__)


class OneCLISecretSource(SecretSource):
    name = "onecli"
    label = "OneCLI credential gateway"
    shape = "mapped"

    def __init__(self, api_key_env: str = "ONECLI_API_KEY") -> None:
        self._api_key_env = api_key_env or "ONECLI_API_KEY"

    def protected_env_vars(self, cfg: dict) -> FrozenSet[str]:
        # The OneCLI control-plane bootstrap key must never be overwritten by any
        # source (agent/secret_sources/base.py:186).
        return frozenset({self._api_key_env})

    def fetch(self, cfg: dict, home_path: Path) -> FetchResult:
        # The ABC contract requires fetch() to never raise; every failure path
        # returns an error result instead.
        result = FetchResult()
        identifier = Path(home_path).name
        if not identifier:
            result.error = "OneCLI identity: could not derive a profile identifier from HERMES_HOME."
            result.error_kind = ErrorKind.NOT_CONFIGURED
            return result
        try:
            config = oneclient.get_container_config(agent=identifier)
        except Exception as exc:
            logger.warning(
                "OneCLI container-config fetch failed for %s: %s", identifier, exc, exc_info=True
            )
            result.error = f"OneCLI container-config request failed for {identifier!r}: {exc}"
            result.error_kind = ErrorKind.NETWORK
            return result
        if config is None:
            result.error = f"OneCLI agent {identifier!r} is not configured (no container-config)."
            result.error_kind = ErrorKind.NOT_CONFIGURED
            return result
        env = config.get("env") if isinstance(config, dict) else None
        if not isinstance(env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in env.items()
        ):
            result.error = (
                f"OneCLI container-config for {identifier!r} has no string env mapping."
            )
            result.error_kind = ErrorKind.INTERNAL
            return result
        result.secrets = dict(env)
        return result
