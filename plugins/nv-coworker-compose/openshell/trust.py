"""OpenShell sandbox trust and docker-bridge NO_PROXY helpers (FLEET-F62.e).

OpenShell re-mints the sandbox CA on every sandbox restart, so a bundle copied once goes
stale. The ``openshell_trust`` secret source points the CA env names at the live bundle (or
a bundle derived from it) at every Hermes start. Every declared hop on the docker bridge is
reachable only through the sandbox proxy, so no NO_PROXY the fleet renders, installs or
inherits may list it.

No package imports: ``compose.py`` and ``installer.py`` are also executed as standalone
modules (the installer as a script inside the sandbox), so both load this file by path. Its
only non-stdlib import is Hermes' own pinned dotenv parser, used to read a ``.env`` exactly as
the runtime does.
"""

from __future__ import annotations

import ipaddress
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Tuple

SOURCE_NAME = "openshell_trust"
SPEC_KEY = "egress.openshell_trust"
PLUGIN_KEY = "nv-coworker-compose"
FLEET_MARKER = "openshell_fleet"
CA_ENV_NAMES = (
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "HERMES_CA_BUNDLE",
    "DENO_CERT",
)
NO_PROXY_NAMES = ("NO_PROXY", "no_proxy")
LOOPBACK_NO_PROXY = "127.0.0.1,localhost,::1"
DERIVED_BUNDLE = ("plugin-data", PLUGIN_KEY, "openshell-trust", "ca-bundle.pem")

_BRIDGE_HOSTS = ("172.17.0.1", "host.docker.internal")
# The broker and OneCLI hops plus the bare hosts: a token that bypasses the proxy for any of
# these under the release matchers is a bridge entry.
_BRIDGE_PROBES: Tuple[Tuple[str, Optional[int]], ...] = (
    ("172.17.0.1", 18777),
    ("172.17.0.1", 18255),
    ("172.17.0.1", None),
    ("host.docker.internal", None),
)
_TRUST_KEYS = frozenset({"bundle", "merge"})
_TOKEN_SPLIT = re.compile(r"[\s,]+")


class TrustError(ValueError):
    """A malformed ``egress.openshell_trust`` or an unusable bundle input."""


def _split_host_port(value: str) -> Tuple[str, Optional[int]]:
    # Mirrors gateway.platforms.base._split_host_port at the pinned release.
    raw = str(value or "").strip()
    if not raw:
        return "", None
    if "://" in raw:
        from urllib.parse import urlsplit

        parsed = urlsplit(raw)
        try:
            port = parsed.port
        except ValueError:
            port = None
        return (parsed.hostname or "").lower().rstrip("."), port
    if raw.startswith("[") and "]" in raw:
        host, _, rest = raw[1:].partition("]")
        port = int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else None
        return host.lower().rstrip("."), port
    if raw.count(":") == 1:
        host, _, maybe_port = raw.rpartition(":")
        if maybe_port.isdigit():
            return host.lower().rstrip("."), int(maybe_port)
    return raw.lower().strip("[]").rstrip("."), None


def _entry_matches(entry: str, host: str, port: Optional[int]) -> bool:
    # Mirrors gateway.platforms.base._no_proxy_entry_matches at the pinned release.
    token = str(entry or "").strip().lower()
    if not token:
        return False
    if token == "*":
        return True
    token_host, token_port = _split_host_port(token)
    if token_port is not None and (port is None or token_port != port):
        return False
    if not token_host:
        return False
    try:
        network = ipaddress.ip_network(token_host, strict=False)
        try:
            return ipaddress.ip_address(host) in network
        except ValueError:
            return False
    except ValueError:
        pass
    try:
        token_ip = ipaddress.ip_address(token_host)
        try:
            return ipaddress.ip_address(host) == token_ip
        except ValueError:
            return False
    except ValueError:
        pass
    if token_host.startswith("*."):
        return host.endswith(token_host[1:])
    if token_host.startswith("."):
        return host == token_host[1:] or host.endswith(token_host)
    return host == token_host or host.endswith(f".{token_host}")


def _suffix_matches(entry: str, host: str) -> bool:
    # Mirrors one entry of gateway.platforms.base.is_host_excluded_by_no_proxy.
    normalized = entry.strip().lower()
    if not normalized:
        return False
    if normalized == "*":
        return True
    if normalized.startswith("*."):
        normalized = normalized[2:]
    elif normalized.startswith("."):
        normalized = normalized[1:]
    return host == normalized or host.endswith(f".{normalized}")


def is_bridge_entry(entry: str) -> bool:
    """True when a NO_PROXY token names the docker bridge (any port) or would make either
    release matcher bypass the proxy for a bridge hop (``*``, a suffix, a containing CIDR)."""
    token = str(entry or "").strip()
    if not token:
        return False
    host, _ = _split_host_port(token)
    if host in _BRIDGE_HOSTS:
        return True
    return any(
        _entry_matches(token, probe_host, probe_port) or _suffix_matches(token, probe_host)
        for probe_host, probe_port in _BRIDGE_PROBES
    )


def _tokens(value: str) -> List[str]:
    return [t for t in _TOKEN_SPLIT.split(value or "") if t]


def bridge_entries(value: Any) -> List[str]:
    """The bridge tokens of a NO_PROXY value, in order."""
    if value is None:
        return []
    return [t for t in _tokens(str(value)) if is_bridge_entry(t)]


def strip_bridge_no_proxy(value: str) -> str:
    """Drop every bridge token, keeping the rest in order and spelling. Unchanged when
    nothing is dropped; the loopback default when nothing survives."""
    tokens = _tokens(value)
    kept = [t for t in tokens if not is_bridge_entry(t)]
    if len(kept) == len(tokens):
        return value
    return ",".join(kept) or LOOPBACK_NO_PROXY


def claim_no_proxy(environ: Mapping[str, str]) -> Dict[str, str]:
    """Both NO_PROXY spellings as the trust source claims them: each spelling's own value
    stripped, else the other spelling's, else the loopback default."""
    upper, lower = environ.get("NO_PROXY") or "", environ.get("no_proxy") or ""
    out: Dict[str, str] = {}
    for name, own, other in (("NO_PROXY", upper, lower), ("no_proxy", lower, upper)):
        raw = own or other
        out[name] = strip_bridge_no_proxy(raw) if raw.strip() else LOOPBACK_NO_PROXY
    return out


def derive_bundle(live: str, merge: Iterable[str], dest: Path) -> None:
    """Write ``live`` followed by every ``merge`` PEM to ``dest`` with an atomic replace.
    Every input must be a regular, non-empty file; nothing is written otherwise."""
    texts = []
    for path in (live, *merge):
        p = Path(path)
        if not p.is_file():
            raise TrustError(f"{path} is missing or not a regular file")
        text = p.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            raise TrustError(f"{path} is empty")
        texts.append(text)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text("\n".join(texts) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o644)
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            tmp.unlink()


def _abs_path(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TrustError(f"{SPEC_KEY}.{field} must be a non-empty absolute path, got {value!r}")
    if any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in value):
        raise TrustError(f"{SPEC_KEY}.{field} must not contain control characters, got {value!r}")
    path = value.strip()
    if not path.startswith("/") or any(c in path for c in "*?[") or ".." in path.split("/"):
        raise TrustError(
            f"{SPEC_KEY}.{field} must be an absolute path without a glob or '..', got {path!r}")
    return path


def parse_trust(value: Any) -> Dict[str, Any]:
    """Validate a PRESENT ``egress.openshell_trust`` into ``{bundle[, merge]}``."""
    if not isinstance(value, dict):
        raise TrustError(f"{SPEC_KEY} must be a mapping with a bundle path, got {type(value).__name__}")
    unknown = sorted(str(k) for k in value if k not in _TRUST_KEYS)
    if unknown:
        raise TrustError(f"{SPEC_KEY} has unknown key(s) {unknown}; allowed: bundle, merge")
    if "bundle" not in value:
        raise TrustError(f"{SPEC_KEY}.bundle is required (the live OpenShell bundle path)")
    out: Dict[str, Any] = {"bundle": _abs_path(value["bundle"], "bundle")}
    if "merge" in value:
        merge = value["merge"]
        if not isinstance(merge, list) or not merge:
            raise TrustError(f"{SPEC_KEY}.merge must be a non-empty list of absolute paths, got {merge!r}")
        out["merge"] = [_abs_path(m, "merge") for m in merge]
    return out


def trust_from_spec(spec: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The validated trust mapping of an openshell spec, or None when the key is absent."""
    egress = spec.get("egress")
    if not isinstance(egress, dict) or SOURCE_NAME not in egress:
        return None
    return parse_trust(egress[SOURCE_NAME])


def trust_block(trust: Mapping[str, Any]) -> Dict[str, Any]:
    """The ``secrets.openshell_trust`` section the source reads as its config."""
    block: Dict[str, Any] = {"enabled": True, "override_existing": True, "bundle": trust["bundle"]}
    if trust.get("merge"):
        block["merge"] = list(trust["merge"])
    return block


def with_trust_source(sources: Any) -> List[Any]:
    """``secrets.sources`` with ``openshell_trust`` first and the rest kept, de-duplicated."""
    rest: List[Any] = []
    for name in sources if isinstance(sources, list) else []:
        if name != SOURCE_NAME and name not in rest:
            rest.append(name)
    return [SOURCE_NAME, *rest]


def preserved_trust_names(secrets_cfg: Any) -> List[str]:
    """CA names and NO_PROXY spellings a ``secrets.preserve_existing`` carves out (they would
    outrank the trust source's override)."""
    preserved = secrets_cfg.get("preserve_existing") if isinstance(secrets_cfg, dict) else None
    if not isinstance(preserved, (list, tuple)):
        return []
    guarded = set(CA_ENV_NAMES) | set(NO_PROXY_NAMES)
    return sorted({v.strip() for v in preserved if isinstance(v, str) and v.strip() in guarded})


def config_bridge_no_proxy(config: Any) -> List[Tuple[str, List[str]]]:
    """``[(spelling, bridge entries)]`` for each top-level NO_PROXY scalar that lists the bridge."""
    if not isinstance(config, dict):
        return []
    out = []
    for name in NO_PROXY_NAMES:
        value = config.get(name)
        if isinstance(value, (str, int, float)) and bridge_entries(str(value)):
            out.append((name, bridge_entries(str(value))))
    return out


def _env_file_assignments(path: Path) -> Dict[str, Optional[str]]:
    """A dotenv file parsed the way Hermes loads it (python-dotenv, ``utf-8-sig``), so quoting,
    inline comments and ``export`` read exactly as at runtime."""
    if not path.is_file():
        return {}
    from dotenv import dotenv_values

    return dict(dotenv_values(path, encoding="utf-8-sig"))


def env_file_clashes(path: Path, *, keyed: bool) -> List[str]:
    """Variables an env file sets that outrank the start-time guards: a bridge-listing NO_PROXY
    spelling always, any assignment of a CA name when the fleet declares the trust key."""
    assigned = _env_file_assignments(path)
    names = [n for n in NO_PROXY_NAMES if bridge_entries(assigned.get(n))]
    if keyed:
        names += [n for n in CA_ENV_NAMES if n in assigned and assigned[n] is not None]
    return names


def preflight_install(config: Any, home: Optional[Path], managed_dir: Optional[Path], *,
                      keyed: bool) -> None:
    """Refuse, before any edit, an input that outranks the start-time guards in the home being
    installed into: the managed-scope ``.env`` (applied after every source), the home ``.env``
    (reloaded with override after the once-per-home source apply), a top-level NO_PROXY scalar
    (bridged into the env) and, for a keyed install, a preserved CA or NO_PROXY name."""
    problems: List[str] = []
    if managed_dir is not None:
        managed_env = Path(managed_dir) / ".env"
        for name in env_file_clashes(managed_env, keyed=keyed):
            problems.append(f"managed .env {managed_env} sets {name}")
    if home is not None:
        home_env = Path(home) / ".env"
        for name in env_file_clashes(home_env, keyed=keyed):
            problems.append(f"home .env {home_env} sets {name}")
        for name, _entries in config_bridge_no_proxy(config):
            problems.append(f"home config.yaml top-level {name} lists a docker-bridge entry")
    if keyed:
        secrets_cfg = config.get("secrets") if isinstance(config, dict) else None
        for name in preserved_trust_names(secrets_cfg):
            problems.append(f"home config.yaml secrets.preserve_existing lists {name}")
    if problems:
        raise TrustError(
            "refusing an input that outranks the OpenShell trust/NO_PROXY guards "
            f"({SPEC_KEY}): " + "; ".join(problems)
            + ". Remove it (the trust env is re-derived at every start; NO_PROXY never lists the docker bridge).")


def is_openshell_fleet_config(cfg: Any) -> bool:
    """An openshell-rendered home (ssh backend plus the veto's matching expected backend) or a
    default home the installer marked."""
    if not isinstance(cfg, dict):
        return False
    entries = ((cfg.get("plugins") or {}).get("entries") or {}) if isinstance(cfg.get("plugins"), dict) else {}
    if not isinstance(entries, dict):
        return False

    def _settings(key: str) -> Dict[str, Any]:
        entry = entries.get(key)
        settings = entry.get("settings") if isinstance(entry, dict) else None
        return settings if isinstance(settings, dict) else {}

    if _settings(PLUGIN_KEY).get(FLEET_MARKER) is True:
        return True
    terminal = cfg.get("terminal")
    backend = terminal.get("backend") if isinstance(terminal, dict) else None
    return backend == "ssh" and _settings("nv-fleet-gates").get("expected_backend") == "ssh"


def strip_inherited_no_proxy(environ: MutableMapping[str, str], cfg: Any) -> List[str]:
    """On an openshell fleet home, drop bridge entries from the inherited NO_PROXY spellings.
    Returns the spellings rewritten; a non-fleet home or a clean value is left untouched."""
    if not is_openshell_fleet_config(cfg):
        return []
    changed = []
    for name in NO_PROXY_NAMES:
        value = environ.get(name)
        if value and bridge_entries(value):
            environ[name] = strip_bridge_no_proxy(value)
            changed.append(name)
    return changed
