"""``openshell_trust`` SecretSource (FLEET-F62.e): re-derive sandbox trust at every start.

Each Hermes process start runs this source inside ``load_hermes_dotenv()``, before
credentials are read. It points every CA env name at the live OpenShell bundle (or at a
bundle derived from it plus the ``merge`` PEMs, under the hydrated home's plugin-data), so a
CA re-minted by a sandbox restart is trusted with no edit. It always claims both NO_PROXY
spellings, stripped of the docker bridge; listed first in ``secrets.sources``, that claim
cannot be replaced by a later source.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import FrozenSet

from agent.secret_sources.base import ErrorKind, FetchResult, SecretSource, get_source_environment

from . import trust

logger = logging.getLogger(__name__)


class OpenShellTrustSource(SecretSource):
    name = trust.SOURCE_NAME
    label = "OpenShell sandbox trust"
    shape = "mapped"

    def protected_env_vars(self, cfg: dict) -> FrozenSet[str]:
        return frozenset()

    def fetch(self, cfg: dict, home_path: Path) -> FetchResult:
        result = FetchResult()
        try:
            result.secrets.update(trust.claim_no_proxy(get_source_environment()))
            ca_path = self._ca_path(cfg, Path(home_path), result)
            if ca_path:
                for name in trust.CA_ENV_NAMES:
                    result.secrets[name] = ca_path
        except Exception as exc:  # fetch() must never raise (SecretSource contract)
            logger.warning("openshell_trust fetch failed: %s", exc, exc_info=True)
            result.secrets = {}
            result.error = f"openshell_trust fetch failed: {exc}"
            result.error_kind = ErrorKind.INTERNAL
        return result

    @staticmethod
    def _ca_path(cfg: dict, home: Path, result: FetchResult) -> str:
        """The path every CA name points at, or "" (trust fails closed, with a warning)."""
        bundle = cfg.get("bundle") if isinstance(cfg, dict) else None
        merge = cfg.get("merge") if isinstance(cfg, dict) else None
        if not isinstance(bundle, str) or not bundle.strip():
            result.warnings.append("openshell_trust: no bundle configured; CA env names left unchanged")
            return ""
        bundle = bundle.strip()
        live = Path(bundle)
        if not live.is_file() or live.stat().st_size == 0:
            result.warnings.append(
                f"openshell_trust: live bundle {bundle} is missing or empty; CA env names left unchanged")
            return ""
        if not merge:
            return bundle
        if not isinstance(merge, list) or not all(isinstance(m, str) and m.strip() for m in merge):
            result.warnings.append("openshell_trust: merge must be a list of paths; CA env names left unchanged")
            return ""
        dest = home.joinpath(*trust.DERIVED_BUNDLE)
        try:
            trust.derive_bundle(bundle, [m.strip() for m in merge], dest)
        except (trust.TrustError, OSError) as exc:
            result.warnings.append(
                f"openshell_trust: cannot derive from live bundle {bundle}: {exc}; CA env names left unchanged")
            return ""
        return str(dest)
