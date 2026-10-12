"""OneCLI SecretSource (CRED-F28) + OSH-F64 §D7 chain-dial.

Resolves a profile's OneCLI proxy coordinates from the control plane. It never
resolves a real provider credential: OneCLI returns proxy URLs (carrying the
``aoc_`` agent token as userinfo), CA-trust vars, a CA path and a placeholder
provider key, and this source returns that ``env`` mapping. It additionally
exposes the token-bearing proxy URL under all four common proxy spellings
(FLEET-F62 C1): the render forwards those four NAMES via
``terminal.docker_forward_env`` (name-only), and the docker client resolves each
to THIS live value at exec, winning over the tokenless ``docker_env``
placeholder — so a spelling missing or tokenless here would let curl (which
gives the lowercase precedence) fall back to a placeholder and fail to reach
OneCLI.

Two optional settings (OSH-F64 §D7, openshell chain-dial). When ``proxy_rewrite``
is set, the returned proxy's host:port is rewritten to the loopback onecli-chain
(the credential userinfo preserved) and the source returns ONLY the forced
proxy+CA keys — so a served coworker's ``override_existing`` apply can replace the
inherited denied proxy without clobbering the bootstrap ANTHROPIC_API_KEY /
NO_PROXY / the protected ONECLI_API_KEY. When ``ca_bundle`` is set the four CA env
names are forced to that path (otherwise the extra CA aliases are synthesized by
the render into ``terminal.docker_env``, not here). Neither set → unchanged.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import FrozenSet, Optional
from urllib.parse import urlsplit, urlunsplit

from agent.secret_sources.base import ErrorKind, FetchResult, SecretSource

from . import oneclient

logger = logging.getLogger(__name__)

# The four common proxy-env spellings. curl gives the lowercase spelling
# precedence, so the token-bearing value must be present under all four.
_PROXY_ALIASES = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
# The four CA-bundle env names an in-child TLS client honours. SSL_CERT_FILE is the
# load-bearing one for the served-profile model turn (default httpx trust_env);
# the other three cover Hermes' own resolver (agent/ssl_verify.py).
_CA_ENV_NAMES = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "HERMES_CA_BUNDLE")


def _rewrite_proxy_hostport(url: str, hostport: str) -> str:
    # Replace a proxy URL's host:port with the chain address, preserving the scheme
    # and any user:pass@ userinfo (the OneCLI aoc_ token) so the rewritten proxy still
    # authenticates to the chain (OSH-F64 §D7).
    parts = urlsplit(url)
    userinfo = parts.netloc.rsplit("@", 1)[0] + "@" if "@" in parts.netloc else ""
    return urlunsplit((parts.scheme, f"{userinfo}{hostport}", parts.path, parts.query, parts.fragment))


class OneCLISecretSource(SecretSource):
    name = "onecli"
    label = "OneCLI credential gateway"
    shape = "mapped"

    def __init__(
        self,
        api_key_env: str = "ONECLI_API_KEY",
        *,
        proxy_rewrite: Optional[str] = None,
        ca_bundle: Optional[str] = None,
    ) -> None:
        self._api_key_env = api_key_env or "ONECLI_API_KEY"
        # Empty string is treated as unset (opt-in): both default to the current behaviour.
        self._proxy_rewrite = proxy_rewrite or None
        self._ca_bundle = ca_bundle or None

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
        proxy_value = next((env[k] for k in _PROXY_ALIASES if env.get(k)), "")
        if self._proxy_rewrite:
            # Chain-dial mode (OSH-F64 §D7): return ONLY the values that must WIN over the
            # child's inherited ambient — the four rewritten proxy spellings and, when a bundle
            # is configured, the four CA names — so override_existing replaces the denied proxy
            # without touching ANTHROPIC_API_KEY / NO_PROXY / the protected ONECLI_API_KEY.
            result.secrets = {}
            if proxy_value:
                try:
                    rewritten = _rewrite_proxy_hostport(proxy_value, self._proxy_rewrite)
                except Exception as exc:  # fetch() must never raise (ABC contract)
                    logger.warning(
                        "OneCLI proxy rewrite failed for %s: %s", identifier, exc, exc_info=True
                    )
                    result.secrets = {}
                    result.error = f"OneCLI proxy rewrite failed for {identifier!r}: {exc}"
                    result.error_kind = ErrorKind.INTERNAL
                    return result
                for name in _PROXY_ALIASES:
                    result.secrets[name] = rewritten
            if self._ca_bundle:
                for name in _CA_ENV_NAMES:
                    result.secrets[name] = self._ca_bundle
            return result
        # Mirror the token-bearing proxy onto all four spellings: curl gives the lowercase
        # spelling precedence, so any left missing/tokenless would select a tokenless placeholder.
        result.secrets = dict(env)
        if proxy_value:
            for name in _PROXY_ALIASES:
                result.secrets[name] = proxy_value
        if self._ca_bundle:
            for name in _CA_ENV_NAMES:
                result.secrets[name] = self._ca_bundle
        return result
