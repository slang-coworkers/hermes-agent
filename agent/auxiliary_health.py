"""Route identity for auxiliary calls: custom-endpoint health checks and the provider allow-list."""
import contextlib
from typing import Any, Optional

from hermes_cli.route_identity import normalize_route_base_url

def _unhealthy_cache_key(provider: str, base_url: Optional[str] = None, model: Optional[str] = None) -> Any:
    """Provider-wide key, or endpoint-specific key for an explicit custom endpoint — prefixed with the
    active profile home: a 402 on profile A's account must not hide the provider from profile B's
    (differently funded) account in the same multiplexed process."""
    from agent.auxiliary_client import _normalize_chain_label, _normalize_resolved_model
    from hermes_constants import hermes_home_key
    label = _normalize_chain_label(provider)
    endpoint = normalize_route_base_url(_custom_health_base_url(provider, base_url))
    key = (hermes_home_key(), "custom-endpoint", endpoint) if endpoint else (hermes_home_key(), label)
    model = _normalize_resolved_model(model, provider)  # configured ``gpt-x`` and wire ``openai/gpt-x`` share a key
    return (*key, model.strip().lower()) if model and model.strip() else key


def _custom_health_base_url(provider: str, explicit_base_url: Optional[str] = None) -> str:
    """Return the concrete custom endpoint used to scope health and failed-route checks."""
    from agent.auxiliary_client import _current_custom_base_url
    explicit = str(explicit_base_url or "").strip()
    from agent.auxiliary_client import _normalize_chain_label
    label = _normalize_chain_label(provider)
    if label == "local/custom":
        return explicit or _current_custom_base_url()
    if label.startswith("custom:") and explicit:
        return explicit
    with contextlib.suppress(ImportError):
        from hermes_cli.runtime_provider import _get_named_custom_provider, _resolves_to_custom
        if _resolves_to_custom(label):
            return explicit or _current_custom_base_url()
        entry = _get_named_custom_provider(provider)
        if entry:
            return explicit or str(entry.get("base_url") or "").strip()
    return ""




# Quarantine hold per unavailable-reason label. Payment/quota depletion and a dead credential
# last hours, so those keep the long default TTL (None); a per-minute 429, a dropped connection or
# a garbled body clears in seconds — holding the lane for 10 minutes process-wide would hide a
# healthy fallback from every aux task over one transient blip.
_TRANSIENT_CANDIDATE_QUARANTINE_SECONDS = 60.0
_CANDIDATE_QUARANTINE_TTL: dict[str, Optional[float]] = {
    "rate limit": _TRANSIENT_CANDIDATE_QUARANTINE_SECONDS,
    "connection error": _TRANSIENT_CANDIDATE_QUARANTINE_SECONDS,
    "invalid provider response": _TRANSIENT_CANDIDATE_QUARANTINE_SECONDS,
    "provider overloaded": _TRANSIENT_CANDIDATE_QUARANTINE_SECONDS,
    "server error": _TRANSIENT_CANDIDATE_QUARANTINE_SECONDS,
}


def fallback_candidate_quarantine_ttl(reason: Optional[str]) -> Optional[float]:
    """Seconds to hide a fallback candidate for ``reason`` (a ``fallback_label`` label, or None
    for a stale credential); None means the long default TTL."""
    return _CANDIDATE_QUARANTINE_TTL.get(reason or "")


# Provider allow-list (``auxiliary.allowed_providers``). Opt-in: unset or [] keeps today's
# behaviour. Set, a provider that is not listed is never resolved, probed or called by any
# auxiliary route (text or vision, primary or fallback); ``auxiliary.free_only`` cannot do
# this, it only filters OpenRouter SKUs. Identity is the provider name (``custom:<name>`` ==
# ``<name>``). A route with its own ``base_url`` override is ``custom``: its name does not say
# where it goes. A ``custom`` route without one (the live runtime of a ``providers:`` entry)
# counts as that entry when it calls the entry's own endpoint. Set but naming nothing allows
# nothing.


class AuxiliaryProviderNotAllowed(RuntimeError):
    """An auxiliary route names a provider outside ``auxiliary.allowed_providers``."""


def _allowlist_label(provider: Optional[str], base_url: Optional[str] = None) -> str:
    from agent.auxiliary_client import _fallback_provider_from_label, _normalize_aux_provider
    if base_url and str(base_url).strip():
        return "custom"
    label = _normalize_aux_provider(_fallback_provider_from_label(str(provider or "")))
    return "custom" if label == "local/custom" else label


def _allowed_labels(cfg: dict[str, Any]) -> Optional[frozenset]:
    """``auxiliary.allowed_providers`` as route identities; None means no restriction."""
    from agent.skill_utils import parse_config_string_list
    from hermes_cli.config import cfg_get
    raw = cfg_get(cfg, "auxiliary", "allowed_providers")
    if raw is None or raw == "" or raw == []:
        return None
    names = [str(name) for name in parse_config_string_list(raw) if str(name).strip()]
    return frozenset(_allowlist_label(name) for name in names)


def _entry_endpoint(name: str, cfg: dict[str, Any]) -> str:
    """The normalized base_url of the ``providers:`` entry ``name``, or "" when it is not one."""
    from hermes_cli.providers import resolve_user_provider
    providers = cfg.get("providers") if isinstance(cfg.get("providers"), dict) else {}
    pdef = resolve_user_provider(name, providers)
    return normalize_route_base_url(pdef.base_url) if pdef is not None and pdef.base_url else ""


def _named_endpoint_label(endpoint: str, allowed: frozenset, cfg: dict[str, Any]) -> str:
    """The listed ``providers:`` entry whose configured base_url is ``endpoint``, or ""."""
    want = normalize_route_base_url(endpoint)
    providers = cfg.get("providers") if isinstance(cfg.get("providers"), dict) else {}
    for name in providers:
        label = _allowlist_label(str(name))
        if label in allowed and _entry_endpoint(str(name), cfg) == want:
            return label
    return ""


def aux_route_allowed(
    provider: Optional[str], base_url: Optional[str] = None, *, endpoint: Optional[str] = None,
) -> bool:
    """Whether an auxiliary route may be used. ``base_url`` is the route's own override (always
    ``custom``); ``endpoint`` is where a resolved route actually calls, which identifies a
    ``custom`` route that has no override."""
    from hermes_cli.config import load_config_readonly
    cfg = load_config_readonly() or {}
    allowed = _allowed_labels(cfg)
    if allowed is None:
        return True
    label = _allowlist_label(provider, base_url)
    if label == "custom" and endpoint and not (base_url and str(base_url).strip()):
        label = _named_endpoint_label(endpoint, allowed, cfg) or label
    elif endpoint and label in allowed:
        # A runtime can carry another base_url for a named entry: a listed ``providers:`` entry is
        # allowed only at its own endpoint (built-in providers are identified by name alone).
        own = _entry_endpoint(label, cfg)
        if own and own != normalize_route_base_url(endpoint):
            return False
    return label in allowed


def require_aux_route_allowed(
    provider: Optional[str], task: Optional[str], base_url: Optional[str] = None,
    *, endpoint: Optional[str] = None,
) -> None:
    """Raise :class:`AuxiliaryProviderNotAllowed` for a route outside the allow-list; ``auto`` is
    resolved later and checked then."""
    if str(provider or "").strip().lower() == "auto" or aux_route_allowed(provider, base_url, endpoint=endpoint):
        return
    raise AuxiliaryProviderNotAllowed(
        f"auxiliary {task or 'call'}: provider '{_allowlist_label(provider, base_url)}' is not in "
        "auxiliary.allowed_providers — add it there or route this task to a listed provider")
