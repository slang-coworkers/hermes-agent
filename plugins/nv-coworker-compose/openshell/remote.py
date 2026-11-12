"""Remote-gateway mode for the OpenShell fleet installer.

Thin layer over the pure planner in ``installer.py`` (its OpenShell 0.1 path). The default mode
(``local``) is the existing installer, unchanged: a local Docker OpenShell gateway or the box lane,
reached through whatever ``openshell`` is on PATH. ``remote`` mode targets an in-cluster OpenShell
v0.1.2 gateway and does EVERYTHING through an in-cluster broker: Hermes never holds gateway
credentials or Sandbox RBAC. The ``openshell`` on PATH must be the broker client shim, which forwards
each verb as one JSON line to the broker address (``openshell.broker.addr``, ``tcp://<svc>.<ns>.svc:<port>``)
with the token read from a mounted Secret file. Remote mode requires a spec with
``egress.openshell_api: "0.1"``; the workspace, the inference provider and the listener guard come from
the 0.1 render (``egress.workspace``, ``egress.inference_provider``, ``legacy_seccomp.py``).

What remote mode changes, relative to the base (0.1) plan:

- A ``remote_preflight`` step runs first (apply): the token file exists and is non-empty, the
  token is not in the environment, ``openshell`` is the broker shim (not a real CLI binary),
  and no OpenShell gateway credential (mTLS client key, gateway endpoint env) is visible.
- After ``compose``, a ``remote_render_check`` step refuses a render that cannot work on
  OpenShell 0.1.x: ``inference.local``, the ``sk-OPENSHELL-PROXY-REWRITE`` literal, a
  hard-coded OpenShell credential placeholder, 172.17.0.1 lane hops, a non-native
  inference base URL, or a worker profile that would start a TCP listener (a cluster that runs
  the sandbox seccomp listener in legacy read-only mode fails accept()/getpeername()).
- The base ``provider_check`` becomes a broker ``provider list`` check of every provider any
  worker binds (missing => refuse before any create, with the operator bootstrap steps).
- Each worker ``sandbox create`` uses 0.1 semantics through the broker: a pullable registry
  image, the policy by its broker-side path (ConfigMap ``hermes-policies``) pinned by
  ``--policy-sha256`` of the local render (the broker verifies the bytes before
  binding), explicit providers, and ``--detach``. The broker owns the main process (its
  ``sandbox_command``), so no ``-- CMD`` is sent. Existence and readiness come from
  ``sandbox list -o json`` (never a table).
- An existing sandbox is never re-created silently: a missing digest record or a policy drift
  is applied live with ``policy set --wait`` (keeps the workspace PVC). Only if that is refused
  (filesystem/landlock/process change) AND ``openshell.allow_replace: true`` does the installer
  use the broker's ``sandbox replace`` (delete + create; the PVC is lost). A Stopped sandbox is
  started; a Completed/Error one is refused.
- ``sandbox ssh-config`` output keeps the 0.1 alias ``openshell-<name>.<workspace>`` (which the
  0.1 render's ``terminal.ssh_host`` uses); only allow-listed keywords pass, and the
  ProxyCommand is pinned to the broker (address, token file, proxy env removed).

Model inference: OpenShell 0.1 removed ``inference.local``. Profiles call the provider's native
URL with ``api_key: ${env:<KEY>}`` (the 0.1 render). By default the provider is NOT attached
to workers (``openshell.inference.roles: []``): Hermes model calls run in the
hermes-gateway-agent pod, whose env must carry the key. A role listed in
``roles`` gets ``--provider <openshell_provider>`` at create; inside that sandbox the env var
holds the revision-scoped placeholder the supervisor injects. The installer never creates a
provider or sees a key value.

Stdlib only (plus PyYAML, a Hermes runtime dependency, for spec/config reads).
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import ipaddress
import json
import logging
import os
import re
import shlex
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit



def _load_yaml_compat():
    # Hermes >= 0.22 ships no PyYAML. Same by-path helper as installer.py (the plugin dir is not an
    # importable package); it wraps Hermes' own ruamel-backed ``hermes_yaml``.
    import importlib.util
    path = Path(__file__).resolve().parent.parent / "_yaml_compat.py"
    spec = importlib.util.spec_from_file_location("_nv_yaml_compat_remote", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


yaml = _load_yaml_compat()

logger = logging.getLogger(__name__)

MODE_LOCAL = "local"
MODE_REMOTE = "remote"
MODES = (MODE_LOCAL, MODE_REMOTE)

# Deployment defaults (client token Secret mount, policies ConfigMap). No default broker address:
# remote mode needs openshell.broker.addr (the cluster FQDN, which an OpenShell policy matches exactly).
DEFAULT_BROKER_ADDR = None
DEFAULT_TOKEN_FILE = "/var/run/secrets/osh-broker-client/token"
DEFAULT_POLICY_ROOT = "/etc/osh-broker/policies"
DEFAULT_POLICY_NAME = "policy-{role}.yaml"
DEFAULT_WORKSPACE = "default"
DEFAULT_READY_TIMEOUT_S = 600
# Worker roles that get the inference provider attached. Empty by default: model calls run
# in the gateway-agent pod, so attaching the
# provider to workers would only let worker tool code spend the key (resolution is scoped by
# endpoint, not by binary: docs/how-it-works/providers/profiles.mdx:206).
DEFAULT_INFERENCE_ROLES: list[str] = []
OPENSHELL_API_V01 = "0.1"
# The OpenShell mTLS client key the chart's certgen writes (Secret openshell-client-tls). Only
# the broker pod may mount it; finding it in the installer's pod breaks the no-credentials rule.
CONTRACT_CLIENT_KEY = "/var/run/secrets/openshell-client/tls.key"

_SPEC_KEYS = {
    "mode", "broker", "policy_root", "policy_name", "ready_timeout_s", "image_registry_prefix",
    "inference", "allow_replace", "gpu",
}
# Keys an earlier draft of remote mode accepted. Refused with a pointer instead of "unknown key".
_MOVED_SPEC_KEYS = {
    "workspace": "use egress.workspace; it must equal the broker's workspace",
    "main_command": "the broker owns the sandbox main process (its sandbox_command setting)",
    "allow_custom_main_command": "the broker owns the sandbox main process (its sandbox_command setting)",
}
_BROKER_KEYS = {"addr", "token_file", "allow_plaintext_noncluster"}
_INFERENCE_KEYS = {"roles"}
_GPU_KEYS = {"roles", "count", "sandbox_image"}
GPU_MAX_COUNT = 8

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}

# <service>.<namespace>.svc[.cluster.local] — the in-cluster Service DNS forms. Traffic to these
# stays on the pod network, fenced by the broker's NetworkPolicy; the JSON-line protocol is
# plaintext TCP, so anything else needs an explicit opt-in.
_DNS_LABEL = r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?"
_CLUSTER_HOST_RE = re.compile(rf"^{_DNS_LABEL}\.{_DNS_LABEL}\.svc(?:\.cluster\.local)?$")
_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
# Values that end up inside an ssh ProxyCommand (run by /bin/sh, with ssh % expansion): no
# shell metacharacters, no whitespace, no %.
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:/@+=-]+$")
_WORKSPACE_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_PROVIDER_NAME_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9_]{0,62}[a-z0-9])?$")
_SANDBOX_NAME_RE = re.compile(r"^[a-z0-9-]{1,48}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_PLACEHOLDER_TAG_RE = re.compile(r"^<[A-Z0-9_]+>$")

# Strings that must never survive into a remote render (OpenShell 0.1.x):
#  - inference.local: removed in 0.1.0 (#3195); the host no longer resolves.
#  - sk-OPENSHELL-PROXY-REWRITE: the 0.0.72 rewrite trigger; 0.1 forwards it verbatim (401).
#  - the OpenShell credential-placeholder prefix (built by concatenation below so the literal never
#    appears in this file: the proxy 403s any model request body that carries it): a hard-coded
#    canonical placeholder; endpoint-bound credentials only
#    resolve the revision-scoped value the supervisor injects (openshell-core secrets.rs
#    resolve_placeholder), so the config must reference the env var instead.
#  - 172.17.0.1: the box lane's Docker bridge (lane hops 18255/18777); absent in a cluster.
_FORBIDDEN_RENDER_STRINGS = (
    "inference.local",
    "sk-OPENSHELL-PROXY-REWRITE",
    "openshell:resolve:" + "env:",
    "172.17.0.1",
)
_PLACEHOLDER_PREFIX = _FORBIDDEN_RENDER_STRINGS[2]


def _forbidden_label(bad: str) -> str:
    """How an error names a forbidden string. The placeholder prefix is described, never echoed:
    an error a coworker reads ends up in its next model request body, which the proxy would 403."""
    return "a hard-coded OpenShell credential placeholder" if bad == _PLACEHOLDER_PREFIX else repr(bad)

# ssh_config keywords the broker's ssh-config output may carry. Anything else (LocalCommand,
# PermitLocalCommand, *Forward, SetEnv, Match ...) is refused before it reaches ~/.ssh.
_SSH_KEYWORDS = {
    "host", "user", "hostname", "port", "stricthostkeychecking", "userknownhostsfile",
    "globalknownhostsfile", "loglevel", "serveraliveinterval", "serveralivecountmax",
    "proxycommand", "identityfile", "identitiesonly", "connecttimeout", "requesttty",
}
_SSH_PROXY_FLAGS = ("--gateway-name", "--name", "--workspace", "--server")
_PROXY_ENV = ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
              "no_proxy", "NO_PROXY")
# Env the shim child must not inherit in remote mode: a raw token (only the file is allowed), a
# unix-socket override (would bypass the in-cluster broker), real-CLI gateway selection, and the
# proxies (the broker Service is in-cluster; a CONNECT tunnel through an egress proxy would fail
# or leak the token to the proxy).
_SCRUB_ENV = ("OSH_BROKER_TOKEN", "OSH_BROKER_SOCKET", "OPENSHELL_GATEWAY",
              "OPENSHELL_GATEWAY_ENDPOINT", "OPENSHELL_GATEWAY_INSECURE") + _PROXY_ENV

_PHASE_READY = "Ready"
_PHASE_STOPPED = "Stopped"
# Phases a sandbox never leaves on its own toward Ready (stop/start through the broker only).
_PHASES_FAILED = {"Error", "Completed", "Stopped", "Stopping"}
_LIST_PAGE_LIMIT = 50
_POLL_INTERVAL_S = 5.0
_RENDER_DIGEST_TOKEN = "<render-sha256>"


class RemoteConfigError(ValueError):
    """Remote-mode configuration or precondition refused (fail closed)."""


# --------------------------------------------------------------------------------------
# Mode + config resolution
# --------------------------------------------------------------------------------------

def _spec_block(spec: dict[str, Any]) -> dict[str, Any]:
    block = (spec or {}).get("openshell")
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise RemoteConfigError(f"openshell: must be a mapping, got {type(block).__name__}")
    moved = sorted(k for k in block if k in _MOVED_SPEC_KEYS)
    if moved:
        raise RemoteConfigError("openshell: " + "; ".join(f"{k} is not accepted ({_MOVED_SPEC_KEYS[k]})"
                                                           for k in moved))
    unknown = sorted(set(block) - _SPEC_KEYS)
    if unknown:
        raise RemoteConfigError(f"openshell: unknown key(s) {unknown}; allowed {sorted(_SPEC_KEYS)}")
    return block


def _parse_bool(value: Any, what: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in _TRUE | _FALSE:
        return value.strip().lower() in _TRUE
    raise RemoteConfigError(f"{what} must be a boolean, got {value!r}")


def resolve_mode(spec: dict[str, Any], env: Optional[dict[str, str]] = None,
                 cli_mode: Optional[str] = None) -> tuple[str, str]:
    """``(mode, source)``. Precedence: CLI flag > ``OPENSHELL_MODE`` env > spec
    ``openshell.mode`` > ``local``. An unknown value fails closed."""
    env = os.environ if env is None else env
    for value, source in ((cli_mode, "--openshell-mode"),
                          (env.get("OPENSHELL_MODE"), "env OPENSHELL_MODE"),
                          (_spec_block(spec).get("mode"), "spec openshell.mode")):
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if not isinstance(value, str) or value.strip() not in MODES:
            raise RemoteConfigError(f"{source}: openshell mode must be one of {list(MODES)}, got {value!r}")
        return value.strip(), source
    return MODE_LOCAL, "default"


def validate_broker_addr(addr: Any, allow_plaintext_noncluster: bool = False) -> tuple[str, int]:
    """Parse ``tcp://HOST:PORT``. Remote mode speaks the broker's plaintext JSON-line protocol,
    so only an in-cluster Service name (``<svc>.<ns>.svc[.cluster.local]``) is accepted unless
    the operator explicitly allows a non-cluster address (dev/test only)."""
    if not isinstance(addr, str) or not addr.strip():
        raise RemoteConfigError(f"broker address must be a non-empty string, got {addr!r}")
    addr = addr.strip()
    if not addr.startswith("tcp://"):
        raise RemoteConfigError(
            f"broker address {addr!r} must be tcp://HOST:PORT in remote mode (a unix socket is a "
            "local/box broker; use openshell mode local for that)")
    host, sep, port = addr[len("tcp://"):].rpartition(":")
    if not sep or not host or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise RemoteConfigError(f"broker address {addr!r} must be tcp://HOST:PORT with a port 1-65535")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not (_HOST_RE.match(host) or _is_ip(host)) or not _SAFE_TOKEN_RE.match(host):
        raise RemoteConfigError(f"broker address {addr!r} has an invalid host")
    if not _CLUSTER_HOST_RE.match(host.lower()) and not allow_plaintext_noncluster:
        raise RemoteConfigError(
            f"broker address {addr!r} is not an in-cluster Service name (<svc>.<ns>.svc"
            "[.cluster.local]); the broker protocol is plaintext, so a non-cluster address is "
            "refused unless explicitly allowed (--allow-plaintext-broker / OSH_BROKER_ALLOW_PLAINTEXT=1 "
            "/ openshell.broker.allow_plaintext_noncluster: true)")
    return host, int(port)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def validate_image_ref(ref: Any, *, registry_prefix: Optional[str] = None,
                       allow_placeholder_tag: bool = True) -> str:
    """OpenShell 0.1 ``--from`` must be a pullable registry reference: an explicit registry host,
    a repository path, and a tag or a digest. ``localhost/...`` (the box's local build) and
    implicit ``latest`` are refused. A ``<CI_TAG>``-style placeholder tag is accepted for plans
    and refused for a real run (``allow_placeholder_tag=False``)."""
    if not isinstance(ref, str) or not ref.strip() or any(c.isspace() for c in ref.strip()):
        raise RemoteConfigError(f"sandbox image must be a non-empty registry reference, got {ref!r}")
    ref = ref.strip()
    name, digest = (ref.split("@", 1) + [None])[:2] if "@" in ref else (ref, None)
    tag = None
    last = name.rsplit("/", 1)[-1]
    if ":" in last:
        tag = last.rsplit(":", 1)[1]
        name = name[: -(len(tag) + 1)]
    parts = name.split("/")
    registry = parts[0]
    if len(parts) < 2 or not ("." in registry or ":" in registry) or registry.split(":")[0] == "localhost":
        raise RemoteConfigError(
            f"sandbox image {ref!r} is not pullable from the cluster: it needs an explicit registry "
            "host (e.g. registry.example.com/...), not a local build")
    if digest is not None and not _DIGEST_RE.match(digest):
        raise RemoteConfigError(f"sandbox image {ref!r} has a malformed digest (want sha256:<64 hex>)")
    if tag is None and digest is None:
        raise RemoteConfigError(f"sandbox image {ref!r} needs an explicit tag or digest (no implicit latest)")
    if tag is not None and not _TAG_RE.match(tag):
        if not (allow_placeholder_tag and _PLACEHOLDER_TAG_RE.match(tag)):
            raise RemoteConfigError(
                f"sandbox image {ref!r} has an unresolved or invalid tag {tag!r}"
                + ("" if allow_placeholder_tag else "; substitute the CI tag before a real run"))
    if registry_prefix and not ref.startswith(registry_prefix):
        raise RemoteConfigError(f"sandbox image {ref!r} must come from {registry_prefix!r}")
    return ref


def _env_or(env: dict[str, str], key: str) -> Optional[str]:
    value = env.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def resolve_config(spec: dict[str, Any], env: Optional[dict[str, str]] = None,
                   overrides: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Resolve the remote-mode configuration: CLI ``overrides`` > env > spec ``openshell:`` >
    contract defaults. Pure (no filesystem access). Returns a plain dict including ``mode`` and
    ``sources`` (where each broker setting came from). Raises ``RemoteConfigError``."""
    env = os.environ if env is None else env
    overrides = overrides or {}
    block = _spec_block(spec)
    mode, mode_source = resolve_mode(spec, env, overrides.get("mode"))

    broker = block.get("broker") or {}
    if not isinstance(broker, dict):
        raise RemoteConfigError("openshell.broker must be a mapping")
    unknown = sorted(set(broker) - _BROKER_KEYS)
    if unknown:
        raise RemoteConfigError(f"openshell.broker: unknown key(s) {unknown}; allowed {sorted(_BROKER_KEYS)}")

    sources: dict[str, str] = {"mode": mode_source}

    def _pick(name: str, cli: Any, env_key: str, spec_val: Any, default: Any) -> Any:
        for value, src in ((cli, "cli"), (_env_or(env, env_key), f"env {env_key}"),
                           (spec_val, "spec"), (default, "default")):
            if value is not None:
                sources[name] = src
                return value
        return None

    addr = _pick("broker_addr", overrides.get("broker_addr"), "OSH_BROKER_ADDR",
                 broker.get("addr"), DEFAULT_BROKER_ADDR)
    token_file = _pick("token_file", overrides.get("token_file"), "OSH_BROKER_TOKEN_FILE",
                       broker.get("token_file"), DEFAULT_TOKEN_FILE)
    allow_plain_raw = _pick("allow_plaintext", overrides.get("allow_plaintext") or None,
                            "OSH_BROKER_ALLOW_PLAINTEXT", broker.get("allow_plaintext_noncluster"), False)
    allow_plain = _parse_bool(allow_plain_raw, "allow_plaintext_noncluster")

    egress = (spec or {}).get("egress")
    egress = egress if isinstance(egress, dict) else {}
    workspace = egress.get("workspace", DEFAULT_WORKSPACE)
    if not isinstance(workspace, str) or not _WORKSPACE_RE.match(workspace):
        raise RemoteConfigError(f"egress.workspace must be a DNS label, got {workspace!r}")
    policy_root = block.get("policy_root", DEFAULT_POLICY_ROOT)
    if not isinstance(policy_root, str) or not policy_root.startswith("/") or ".." in policy_root.split("/"):
        raise RemoteConfigError(f"openshell.policy_root must be an absolute path without '..', got {policy_root!r}")
    policy_name = block.get("policy_name", DEFAULT_POLICY_NAME)
    if (not isinstance(policy_name, str) or "{role}" not in policy_name or "/" in policy_name
            or not policy_name.endswith((".yaml", ".yml"))):
        raise RemoteConfigError(
            f"openshell.policy_name must be a flat *.yaml file name containing {{role}}, got {policy_name!r}")
    allow_replace = _parse_bool(block.get("allow_replace", False), "openshell.allow_replace")
    ready_timeout = block.get("ready_timeout_s", DEFAULT_READY_TIMEOUT_S)
    if isinstance(ready_timeout, bool) or not isinstance(ready_timeout, int) or not 10 <= ready_timeout <= 3600:
        raise RemoteConfigError(f"openshell.ready_timeout_s must be an integer 10-3600, got {ready_timeout!r}")
    registry_prefix = block.get("image_registry_prefix")
    if registry_prefix is not None and (not isinstance(registry_prefix, str) or not registry_prefix.strip()):
        raise RemoteConfigError("openshell.image_registry_prefix must be a non-empty string when set")

    # A 0.0.72 spec's egress.inference_provider is the OSH-F64.b endpoint block, not the 0.1 provider wiring.
    provider_block = (egress.get("inference_provider")
                      if mode == MODE_REMOTE or egress.get("openshell_api") == OPENSHELL_API_V01 else None)
    inference = _resolve_inference(block.get("inference"), provider_block)
    gpu = _resolve_gpu(block.get("gpu"))

    cfg = {
        "mode": mode,
        "broker_addr": addr,
        "token_file": token_file,
        "allow_plaintext_noncluster": allow_plain,
        "workspace": workspace,
        "policy_root": policy_root.rstrip("/") or "/",
        "policy_name": policy_name,
        "allow_replace": allow_replace,
        "ready_timeout_s": ready_timeout,
        "image_registry_prefix": registry_prefix,
        "inference": inference,
        "gpu": gpu,
        "sources": sources,
    }
    if mode == MODE_REMOTE:
        if addr is None:
            raise RemoteConfigError("remote mode needs the broker address: set openshell.broker.addr in the spec "
                                    "(or --broker-addr / OSH_BROKER_ADDR)")
        validate_broker_addr(addr, allow_plain)
        if not isinstance(token_file, str) or not token_file.startswith("/") or not _SAFE_TOKEN_RE.match(token_file):
            raise RemoteConfigError(f"broker token file must be an absolute path, got {token_file!r}")
    return cfg


def _resolve_inference(block: Any, provider_block: Any) -> Optional[dict[str, Any]]:
    """The inference provider wiring, derived from the 0.1 render's ``egress.inference_provider`` (provider
    name, credential env, endpoint) so remote mode never restates it. ``openshell.inference``
    only chooses which worker ``roles`` get the provider attached (default none). ``None`` when
    the spec has no inference provider (a 0.0.72 spec, or local mode)."""
    if block is False or block is None:
        block = {}
    if not isinstance(block, dict):
        raise RemoteConfigError("openshell.inference must be a mapping")
    unknown = sorted(set(block) - _INFERENCE_KEYS)
    if unknown:
        raise RemoteConfigError(
            f"openshell.inference: unknown key(s) {unknown}; allowed {sorted(_INFERENCE_KEYS)} (the "
            "provider, credential env and endpoint come from egress.inference_provider)")
    roles = block.get("roles", list(DEFAULT_INFERENCE_ROLES))
    if not (isinstance(roles, list) and all(isinstance(r, str) and r for r in roles)):
        raise RemoteConfigError(f"openshell.inference.roles must be a list of worker roles, got {roles!r}")
    if not isinstance(provider_block, dict):
        return None
    provider = provider_block.get("openshell_provider") or provider_block.get("profile")
    if not isinstance(provider, str) or not _PROVIDER_NAME_RE.match(provider):
        raise RemoteConfigError(f"egress.inference_provider.openshell_provider must be a provider name, got {provider!r}")
    if provider.startswith("_provider_"):
        raise RemoteConfigError("the inference provider may not use the reserved _provider_ prefix")
    credential_env = provider_block.get("credential_env")
    if not isinstance(credential_env, str) or not _ENV_NAME_RE.match(credential_env):
        raise RemoteConfigError(
            f"egress.inference_provider.credential_env must be an UPPER_SNAKE env name, got {credential_env!r}")
    base = provider_block.get("base_url")
    parts = urlsplit(base.strip()) if isinstance(base, str) else None
    host = (parts.hostname or "").lower() if parts else ""
    if not parts or parts.scheme != "https" or not host or not _HOST_RE.match(host) or host == "inference.local":
        raise RemoteConfigError(
            f"egress.inference_provider.base_url must be the native https endpoint, got {base!r}")
    return {"provider": provider, "credential_env": credential_env, "host": host,
            "port": parts.port or 443, "roles": list(roles)}


def _resolve_gpu(block: Any) -> dict[str, Any]:
    """``openshell.gpu``: which worker roles get a GPU sandbox (phase 2: builder, tester), how many
    GPUs each, and the GPU worker image they run. The broker still decides: it only accepts
    ``--gpu`` for its ``gpu_prefixes`` and adds the operator's node placement itself, so this
    block can request a GPU but never choose where it lands. Default: no GPU roles."""
    if block is None or block is False:
        return {"roles": [], "count": 1, "sandbox_image": None}
    if not isinstance(block, dict):
        raise RemoteConfigError("openshell.gpu must be a mapping")
    unknown = sorted(set(block) - _GPU_KEYS)
    if unknown:
        raise RemoteConfigError(f"openshell.gpu: unknown key(s) {unknown}; allowed {sorted(_GPU_KEYS)}")
    roles = block.get("roles", [])
    if not (isinstance(roles, list) and all(isinstance(r, str) and r for r in roles)) or len(set(roles)) != len(roles):
        raise RemoteConfigError(f"openshell.gpu.roles must be a list of distinct worker roles, got {roles!r}")
    count = block.get("count", 1)
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= GPU_MAX_COUNT:
        raise RemoteConfigError(f"openshell.gpu.count must be an integer 1-{GPU_MAX_COUNT}, got {count!r}")
    image = block.get("sandbox_image")
    if roles and image is None:
        raise RemoteConfigError("openshell.gpu.sandbox_image is required with gpu roles (the CPU worker "
                                "image has no CUDA/Vulkan userspace; use hermes-worker-gpu)")
    return {"roles": list(roles), "count": count, "sandbox_image": image}


def role_sandbox(cfg: dict[str, Any], role: str, default_image: str) -> tuple[str, Optional[int]]:
    """(image, gpu count or None) for one worker role."""
    gpu = cfg.get("gpu") or {}
    if role in (gpu.get("roles") or []):
        return gpu["sandbox_image"], gpu["count"]
    return default_image, None


# --------------------------------------------------------------------------------------
# Spec + render checks
# --------------------------------------------------------------------------------------

def check_spec(spec: dict[str, Any], cfg: dict[str, Any], *, for_apply: bool = False) -> None:
    """Spec-level refusals for remote mode (before anything runs). compose (0.1 render) enforces the
    full 0.1 egress contract at render time; these are the remote-specific early refusals."""
    if spec.get("substrate") != "openshell":
        raise RemoteConfigError(f"remote mode requires substrate: openshell (spec has {spec.get('substrate')!r})")
    egress = spec.get("egress")
    if not isinstance(egress, dict):
        raise RemoteConfigError("remote mode requires an egress: mapping")
    api = egress.get("openshell_api")
    if api != OPENSHELL_API_V01:
        raise RemoteConfigError(
            f"remote mode targets an OpenShell 0.1.x gateway and requires egress.openshell_api: "
            f"\"{OPENSHELL_API_V01}\" (spec has {api!r}); the 0.0.72 CLI cannot talk to a 0.1 gateway")
    for key in ("proxy_addr", "broker_addr", "chain_addr"):
        if key in egress:
            raise RemoteConfigError(
                f"egress.{key} is a box-lane hop (172.17.0.1); remote mode reaches the broker "
                "from the gateway-agent pod, never from worker sandboxes. Remove it")
    blob = json.dumps(egress, sort_keys=True)
    for bad in _FORBIDDEN_RENDER_STRINGS + ("host.openshell.internal",):
        if bad in blob:
            raise RemoteConfigError(f"egress carries {_forbidden_label(bad)}, which does not exist on OpenShell 0.1.x")
    if not isinstance(egress.get("inference_provider"), dict) or not cfg.get("inference"):
        raise RemoteConfigError(
            "remote mode requires egress.inference_provider (the 0.1 provider profile; "
            "profiles call the native URL with api_key ${env:<credential_env>})")
    if "rewrite_placeholder" in egress["inference_provider"]:
        raise RemoteConfigError("egress.inference_provider.rewrite_placeholder is an OpenShell 0.0.72 construct")
    validate_image_ref(egress.get("sandbox_image"), registry_prefix=cfg.get("image_registry_prefix"),
                       allow_placeholder_tag=not for_apply)
    roles = _roles(spec)
    unknown = sorted(set(cfg["inference"]["roles"]) - set(roles))
    if unknown:
        raise RemoteConfigError(f"openshell.inference.roles names unknown worker role(s) {unknown}")
    gpu = cfg.get("gpu") or {}
    unknown = sorted(set(gpu.get("roles") or []) - set(roles))
    if unknown:
        raise RemoteConfigError(f"openshell.gpu.roles names unknown worker role(s) {unknown}")
    if gpu.get("roles"):
        validate_image_ref(gpu.get("sandbox_image"), registry_prefix=cfg.get("image_registry_prefix"),
                           allow_placeholder_tag=not for_apply)
    for role in roles:
        tconf = ((spec.get("types") or {}).get(role) or {}).get("config")
        if isinstance(tconf, dict):
            problems = worker_listener_problems(tconf)
            if problems:
                raise RemoteConfigError(f"worker {role!r}: {'; '.join(problems)}")


def _roles(spec: dict[str, Any]) -> list[str]:
    default_profile = spec.get("default_profile", "default")
    return [name for name in (spec.get("types") or {}) if name != default_profile]


_LEGACY_SECCOMP = None


def _legacy_seccomp():
    """The 0.1 render's ``legacy_seccomp.py`` (the shared worker TCP-listener guard), loaded by path."""
    global _LEGACY_SECCOMP
    if _LEGACY_SECCOMP is None:
        path = Path(__file__).resolve().parent / "legacy_seccomp.py"
        mspec = importlib.util.spec_from_file_location("_osh_ast1_legacy_seccomp", path)
        mod = importlib.util.module_from_spec(mspec)
        mspec.loader.exec_module(mod)
        _LEGACY_SECCOMP = mod
    return _LEGACY_SECCOMP


def _core_platform_names() -> set:
    try:
        from gateway.config import Platform  # type: ignore
        return {p.value for p in Platform}
    except ImportError:
        return set()


def _merged_platforms_view(config: dict[str, Any]) -> dict[str, Any]:
    """``config`` with ``platforms`` replaced by the merge Hermes core performs at gateway load
    (gateway/config.py: ``gateway.platforms`` first, then ``gateway.<platform>`` blocks, then the
    top-level ``platforms``, which wins). The 0.1 guard only reads the top level, so a listener
    enabled under ``gateway:`` would otherwise slip through."""
    merged: dict[str, dict[str, Any]] = {}

    def _merge(source: Any) -> None:
        if isinstance(source, dict):
            for name, block in source.items():
                if isinstance(block, dict):
                    merged[name] = {**merged.get(name, {}), **block}

    gateway = config.get("gateway")
    if isinstance(gateway, dict):
        _merge(gateway.get("platforms"))
        known = _core_platform_names() | set(_legacy_seccomp().LISTENER_PLATFORMS)
        _merge({k: v for k, v in gateway.items() if k != "platforms" and k in known})
    _merge(config.get("platforms"))
    view = dict(config)
    view["platforms"] = merged
    return view


def worker_listener_problems(config: dict[str, Any]) -> list[str]:
    """Reasons a WORKER profile config would open a TCP listener, which a worker workload cannot
    serve under a legacy read-only seccomp mode ( accept() with an address and getpeername()
    fail with EOPNOTSUPP). Delegates to ``legacy_seccomp.tcp_listener_findings`` over
    the core-merged platform view. Listeners belong to the gateway-agent pod's DEFAULT profile."""
    return list(_legacy_seccomp().tcp_listener_findings(_merged_platforms_view(config)))


def _expected_api_key(inf: dict[str, Any]) -> str:
    return "${env:" + inf["credential_env"] + "}"


def inference_render_problems(config: dict[str, Any], inf: dict[str, Any]) -> list[str]:
    """Problems with a rendered profile's model-provider wiring under 0.1 provider profiles: any
    provider/model base_url aimed at the inference provider must be the NATIVE https endpoint, and its
    api_key must be the env reference ``${env:<KEY>}`` (the injected revision-scoped
    placeholder), never a literal."""
    problems: list[str] = []
    want_key = _expected_api_key(inf)
    blocks: list[tuple[str, dict[str, Any]]] = []
    providers = config.get("providers")
    if isinstance(providers, dict):
        blocks += [(f"providers.{k}", v) for k, v in providers.items() if isinstance(v, dict)]
    model = config.get("model")
    if isinstance(model, dict) and model.get("base_url"):
        blocks.append(("model", model))
    for where, block in blocks:
        base = block.get("base_url")
        if not isinstance(base, str) or not base.strip():
            continue
        parts = urlsplit(base.strip())
        host = (parts.hostname or "").lower()
        if host == "inference.local":
            problems.append(f"{where}.base_url uses inference.local (removed in OpenShell 0.1.0)")
            continue
        if host != inf["host"]:
            continue
        if parts.scheme != "https" or (parts.port or 443) != inf["port"]:
            problems.append(f"{where}.base_url {base!r} must be https on port {inf['port']}")
        if parts.username or parts.password or parts.query or parts.fragment:
            problems.append(f"{where}.base_url {base!r} must not carry userinfo, query or fragment")
        key = block.get("api_key")
        if key != want_key:
            problems.append(
                f"{where}.api_key must be {want_key!r} (resolved at load to the placeholder the "
                f"OpenShell supervisor injects), got {'<unset>' if key is None else '<a literal value>'}")
    return problems


def check_render(out_dir: Any, spec: dict[str, Any], cfg: dict[str, Any]) -> None:
    """Refuse a compose render that cannot run on OpenShell 0.1.x in remote mode. Reads each
    worker's rendered ``config.yaml`` and ``policy-<role>.yaml`` plus the DEFAULT config."""
    out = Path(out_dir)
    problems: list[str] = []
    roles = _roles(spec)
    default_profile = spec.get("default_profile", "default")
    inf = cfg.get("inference")
    for profile in roles + [default_profile]:
        pdir = out / profile
        cfg_path = pdir / "config.yaml"
        if not cfg_path.is_file():
            problems.append(f"{profile}: rendered config {cfg_path} is missing")
            continue
        files = [cfg_path] + (sorted(pdir.glob("policy-*.yaml")))
        for path in files:
            text = path.read_text(encoding="utf-8-sig")
            for bad in _FORBIDDEN_RENDER_STRINGS:
                if bad in text:
                    problems.append(f"{profile}: {path.name} contains {_forbidden_label(bad)}")
        rendered = yaml.safe_load(cfg_path.read_text(encoding="utf-8-sig")) or {}
        if profile != default_profile:
            if not (pdir / f"policy-{profile}.yaml").is_file():
                problems.append(f"{profile}: rendered policy policy-{profile}.yaml is missing")
            problems += [f"{profile}: {p}" for p in worker_listener_problems(rendered)]
        if inf:
            problems += [f"{profile}: {p}" for p in inference_render_problems(rendered, inf)]
    if problems:
        raise RemoteConfigError("remote render check failed:\n  - " + "\n  - ".join(problems))


# --------------------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------------------

_INSTALLER = None


def load_installer():
    """The sibling ``installer.py`` (loaded by path, like the CLI subaction does)."""
    global _INSTALLER
    if _INSTALLER is None:
        path = Path(__file__).resolve().parent / "installer.py"
        spec = importlib.util.spec_from_file_location("_osh_ast1_installer", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _INSTALLER = mod
    return _INSTALLER


def _type_providers(inst, spec: dict[str, Any], role: str, cfg: dict[str, Any]) -> list[str]:
    names: list[str] = []
    inf = cfg.get("inference")
    if inf and role in inf["roles"]:
        names.append(inf["provider"])
    for name in inst._type_provider_names(spec, role):
        if name.startswith("_provider_"):
            raise RemoteConfigError(f"{role}: provider name {name!r} uses the reserved _provider_ prefix")
        if name not in names:
            names.append(name)
    return names


def broker_policy_path(cfg: dict[str, Any], project: str, role: str) -> str:
    return f"{cfg['policy_root']}/{cfg['policy_name'].format(role=role, project=project)}"


def _create_argv(verb: str, sandbox: str, image: str, policy: str, digest: str,
                 providers: list[str], gpu: Optional[int] = None) -> list[str]:
    """``sandbox create|replace`` for the broker. No ``-- CMD``: the broker owns the sandbox main
    process (config ``sandbox_command``; a main process that exits leaves the sandbox Completed),
    and no ``--workspace`` (the broker injects it and refuses a client one). ``gpu`` adds
    ``--gpu=N``; the broker adds the node placement (never the client)."""
    argv = ["openshell", "sandbox", verb, "--name", sandbox, "--from", image,
            "--policy", policy, "--policy-sha256", digest]
    for p in providers:
        argv += ["--provider", p]
    if gpu is not None:
        argv.append(f"--gpu={int(gpu)}")
    return argv + ["--detach"]


def _policy_set_argv(sandbox: str, policy: str, digest: str) -> list[str]:
    """Live policy update (0.1 ``policy set --wait``): network_policies may change and
    filesystem paths may be added; landlock/process changes are refused by the gateway."""
    return ["openshell", "policy", "set", sandbox, "--policy", policy, "--policy-sha256", digest, "--wait"]


def _note(text: str) -> Any:
    inst = load_installer()
    return inst.Step(f"# {text}", "remote_note")


def _roles_from_base(base: list[Any], project: str) -> list[tuple[str, str]]:
    out = []
    for step in base:
        if step.tag == "provision_create":
            sandbox = step.command[step.command.index("--name") + 1]
            out.append((sandbox, sandbox[len(project) + 1:]))
    return out


def build_plan(home_state: dict[str, Any], spec_path: Any, ref: str, cfg: dict[str, Any],
               *, inst=None) -> list[Any]:
    """The remote-mode plan: the base (0.1) ``installer.build_plan`` (plugins, compose,
    profiles, default edit, managed fragment, rooms, wires, restart are unchanged) with its
    OpenShell steps replaced by broker-driven ones: a preflight first, a render check right after
    compose, one provider check before the first create, then create + ssh-config per worker."""
    inst = inst or load_installer()
    if cfg.get("mode") != MODE_REMOTE:
        raise RemoteConfigError("remote build_plan called with a non-remote config")
    spec = inst.load_spec(spec_path)
    check_spec(spec, cfg)
    base = inst.build_plan(home_state, spec_path, ref)
    project = spec.get("project") or "fleet"
    image = spec["egress"]["sandbox_image"]
    out = inst._render_out_dir()
    inf = cfg["inference"]
    workers = _roles_from_base(base, project)
    providers_by_role = {role: _type_providers(inst, spec, role, cfg) for _, role in workers}
    needed = sorted({p for names in providers_by_role.values() for p in names})

    head = [
        _note(f"openshell mode: remote (source: {cfg['sources']['mode']}); OpenShell 0.1.x gateway "
              "reached ONLY through the in-cluster broker (no gateway credentials, no Sandbox RBAC here)"),
        _note(f"broker {cfg['broker_addr']} (source: {cfg['sources']['broker_addr']}); token file "
              f"{cfg['token_file']} (source: {cfg['sources']['token_file']}); workspace {cfg['workspace']} "
              "(must equal the broker's workspace)"),
        _note(f"policies: broker-side {cfg['policy_root']}/{cfg['policy_name']} (ConfigMap hermes-policies) "
              "must be byte-identical to this render (--policy-sha256); regenerate it from this spec at this ref"),
        _note(f"inference: profiles call https://{inf['host']}:{inf['port']} with api_key "
              f"${{env:{inf['credential_env']}}}; provider {inf['provider']!r} attached to worker roles "
              f"{inf['roles'] or 'none (model calls run in the gateway-agent pod, whose env must hold the key)'}"),
    ]
    gpu_roles = [role for _, role in workers if role in ((cfg.get("gpu") or {}).get("roles") or [])]
    if gpu_roles:
        head.append(_note(f"gpu: worker roles {gpu_roles} get --gpu={cfg['gpu']['count']} on image "
                          f"{cfg['gpu']['sandbox_image']}; the broker adds the node placement (gpu_prefixes, "
                          "gpu_driver_config_file) and the gateway needs patches 0002 + 0003"))
    head.append(inst.Step(
        "# remote preflight: token file present, `openshell` is the broker shim, no OpenShell "
        "gateway credential visible to this process", "remote_preflight"))

    steps: list[Any] = list(head)
    provider_check_done = False
    for step in base:
        if step.tag == "provider_check":
            continue  # replaced by one remote_provider_check covering inference + catalog providers
        if step.tag == "provision_create":
            sandbox = step.command[step.command.index("--name") + 1]
            role = sandbox[len(project) + 1:]
            if not _SANDBOX_NAME_RE.match(sandbox):
                raise RemoteConfigError(f"sandbox name {sandbox!r} must match ^[a-z0-9-]{{1,48}}$")
            if needed and not provider_check_done:
                steps.append(inst.Step(["openshell", "provider", "list", "-o", "json"],
                                       "remote_provider_check", data={"providers": needed}))
                provider_check_done = True
            role_image, gpu_count = role_sandbox(cfg, role, image)
            argv = _create_argv("create", sandbox, role_image, broker_policy_path(cfg, project, role),
                                _RENDER_DIGEST_TOKEN, providers_by_role[role], gpu=gpu_count)
            steps.append(inst.Step(argv, "remote_provision_create", data={
                "sandbox": sandbox, "role": role,
                "rendered_policy": f"{out}/{role}/policy-{role}.yaml"}))
        elif step.tag == "provision_sshconfig":
            sandbox = step.command[-1]
            steps.append(inst.Step(["openshell", "sandbox", "ssh-config", sandbox],
                                   "remote_provision_sshconfig", data={"sandbox": sandbox}))
        else:
            steps.append(step)
            if step.tag == "compose":
                steps.append(inst.Step(
                    f"# remote render check {out}: OpenShell 0.1 provider-profile inference (native "
                    f"base_url, api_key ${{env:{inf['credential_env']}}}), no box-lane hop, no worker "
                    "TCP listener", "remote_render_check"))
    return steps


def plan_text(spec_path: Any, ref: str, cfg: dict[str, Any], *, inst=None) -> str:
    inst = inst or load_installer()
    spec = inst.load_spec(spec_path)
    inst._require_openshell(spec)
    state = inst.collect_home_state(spec, spec_path)
    return inst.render_transcript(build_plan(state, spec_path, ref, cfg, inst=inst))


# --------------------------------------------------------------------------------------
# Apply
# --------------------------------------------------------------------------------------

def child_env(cfg: dict[str, Any], base: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Environment for an ``openshell`` (broker shim) child in remote mode."""
    env = {k: v for k, v in (os.environ if base is None else base).items() if k not in _SCRUB_ENV}
    env["OSH_BROKER_ADDR"] = cfg["broker_addr"]
    env["OSH_BROKER_TOKEN_FILE"] = cfg["token_file"]
    return env


def _credential_paths(env: dict[str, str]) -> list[Path]:
    roots = []
    if env.get("XDG_CONFIG_HOME"):
        roots.append(Path(env["XDG_CONFIG_HOME"]))
    if env.get("HOME"):
        roots.append(Path(env["HOME"]) / ".config")
    hits: list[Path] = []
    for root in roots:
        gw = root / "openshell" / "gateways"
        if gw.is_dir():
            hits += sorted(p for p in gw.glob("*/mtls/tls.key"))
    return hits


TICKET_URL_FILE = "/etc/osh-broker-client/ticket-url"


def ticket_mode_url(env: dict[str, str], url_file: str = TICKET_URL_FILE) -> Optional[str]:
    """The broker ticket-exchange URL when the shim runs in ticket mode (env OSH_BROKER_TICKET_URL, else the
    image's /etc/osh-broker-client/ticket-url), else None (token-file mode)."""
    url = (env.get("OSH_BROKER_TICKET_URL") or "").strip()
    if url:
        return url
    try:
        with open(url_file, encoding="utf-8-sig") as fh:
            return fh.readline().strip() or None
    except OSError:
        return None


def preflight(cfg: dict[str, Any], env: Optional[dict[str, str]] = None,
              *, client_key_path: str = CONTRACT_CLIENT_KEY) -> list[str]:
    """Apply-time G3 checks. Raises ``RemoteConfigError`` on a hard failure; returns warnings."""
    env = dict(os.environ if env is None else env)
    problems: list[str] = []
    warnings: list[str] = []
    if env.get("OSH_BROKER_TOKEN"):
        problems.append("OSH_BROKER_TOKEN is set in the environment; remote mode reads the broker "
                        "token only from the mounted file (unset it)")
    ticket_url = ticket_mode_url(env)
    if ticket_url:
        # Phase 2b (the Hermes gateway inside an OpenShell sandbox): no broker token file at all. The shim buys a
        # single-use ticket per request with the broker client's provider placeholder (never printed).
        bearer_env = env.get("OSH_BROKER_TICKET_BEARER_ENV", "OSH_BROKER_TICKET_BEARER")
        if not (env.get(bearer_env) or "").strip():
            problems.append(f"broker ticket mode ({ticket_url}) but {bearer_env} is not set: attach provider "
                            "the broker client to this sandbox")
    else:
        tf = Path(cfg["token_file"])
        try:
            real = Path(os.path.realpath(tf))
            if not real.is_file():
                problems.append(f"broker token file {tf} is missing (mount the broker token Secret there)")
            elif not os.access(real, os.R_OK):
                problems.append(f"broker token file {tf} is not readable")
            elif not real.read_text(encoding="utf-8-sig", errors="replace").strip():
                problems.append(f"broker token file {tf} is empty")
        except OSError as exc:
            problems.append(f"broker token file {tf}: {exc}")
    exe = shutil.which("openshell", path=env.get("PATH"))
    if not exe:
        problems.append("no `openshell` on PATH; remote mode needs the broker client shim installed as `openshell`")
    else:
        try:
            with open(exe, "rb") as fh:
                head = fh.read(4)
        except OSError as exc:
            head = b""
            problems.append(f"cannot read {exe}: {exc}")
        if head == b"\x7fELF":
            problems.append(f"`openshell` on PATH ({exe}) is a native binary, not the broker shim; remote "
                            "mode must never drive the OpenShell CLI directly (G3)")
    for key in ("OPENSHELL_GATEWAY_ENDPOINT", "OPENSHELL_GATEWAY"):
        if env.get(key):
            problems.append(f"{key} is set; remote mode never selects an OpenShell gateway directly (G3)")
    creds = _credential_paths(env)
    if os.path.exists(client_key_path):
        creds.append(Path(client_key_path))
    if creds:
        problems.append(f"OpenShell gateway client credentials are visible to this process ({', '.join(map(str, creds))}); "
                        "only the broker pod may hold them")
    inf = cfg.get("inference")
    if inf and not (env.get(inf["credential_env"]) or "").strip():
        # Value never read beyond emptiness; never printed.
        warnings.append(f"{inf['credential_env']} is not set in this pod: profiles that run here (the "
                        "gateway-agent) cannot call the inference provider. Inject it with a secretKeyRef to "
                        "the Secret that holds the key")
    if os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/token"):
        warnings.append("a Kubernetes ServiceAccount token is mounted; the gateway-agent needs none "
                        "(set automountServiceAccountToken: false)")
    if problems:
        raise RemoteConfigError("remote preflight failed:\n  - " + "\n  - ".join(problems))
    return warnings


def _run_capture(argv: list[str], cfg: dict[str, Any], timeout: int = 120) -> tuple[int, str, str]:
    import subprocess
    proc = subprocess.run(argv, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          stdin=subprocess.DEVNULL, env=child_env(cfg), timeout=timeout)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _run_checked(argv: list[str], cfg: dict[str, Any], timeout: int = 900) -> str:
    rc, out, err = _run_capture(argv, cfg, timeout=timeout)
    if rc != 0:
        raise RuntimeError(f"{shlex.join(argv)} failed (rc {rc}): {err.strip()[-2000:]}")
    return out


def parse_sandbox_list(text: str) -> tuple[dict[str, str], str]:
    """Parse ``sandbox list -o json``: the 0.1 envelope ``{"sandboxes": [...],
    "next_page_token": ""}`` (or a bare array). Returns ``({name: phase}, next_page_token)``.
    A non-JSON reply (a table) is refused: remote mode never parses tables."""
    try:
        data = json.loads(text)
    except ValueError:
        raise RemoteConfigError("sandbox list did not return JSON (remote mode needs `sandbox list -o json` "
                                "support in the broker; tables are never parsed)")
    if isinstance(data, list):
        items, token = data, ""
    elif isinstance(data, dict) and isinstance(data.get("sandboxes", []), list):
        items, token = data.get("sandboxes", []), data.get("next_page_token") or ""
    else:
        raise RemoteConfigError("sandbox list JSON has an unexpected shape")
    out: dict[str, str] = {}
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            out[item["name"]] = str(item.get("phase") or "")
    if not isinstance(token, str):
        raise RemoteConfigError("sandbox list next_page_token is not a string")
    return out, token


def list_sandboxes(cfg: dict[str, Any], runner: Optional[Callable[[list[str]], str]] = None) -> dict[str, str]:
    """All sandboxes visible through the broker, following page tokens (bounded)."""
    run = runner or (lambda argv: _run_checked(argv, cfg, timeout=120))
    phases: dict[str, str] = {}
    token = ""
    for _ in range(_LIST_PAGE_LIMIT):
        argv = ["openshell", "sandbox", "list", "-o", "json"] + (["--page-token", token] if token else [])
        page, token = parse_sandbox_list(run(argv))
        phases.update(page)
        if not token:
            return phases
    raise RemoteConfigError(f"sandbox list did not finish within {_LIST_PAGE_LIMIT} pages")


def wait_for(name: str, cfg: dict[str, Any], *, want: str, lister=None, sleep=time.sleep,
             clock=time.monotonic) -> str:
    """Poll the broker until ``name`` is Ready (``want='ready'``) or gone (``want='absent'``)."""
    lister = lister or (lambda: list_sandboxes(cfg))
    deadline = clock() + cfg["ready_timeout_s"]
    while True:
        phases = lister()
        phase = phases.get(name)
        if want == "absent" and phase is None:
            return "absent"
        if want == "ready":
            if phase == _PHASE_READY:
                return phase
            if phase in _PHASES_FAILED:
                raise RuntimeError(f"sandbox {name} reached phase {phase} instead of Ready; inspect it "
                                   "(`openshell logs`, `openshell sandbox get`) before re-running")
            if phase is None:
                raise RuntimeError(f"sandbox {name} disappeared while waiting for Ready")
        if clock() >= deadline:
            raise RuntimeError(f"sandbox {name} not {want} after {cfg['ready_timeout_s']}s (last phase {phase!r})")
        sleep(_POLL_INTERVAL_S)


def _digest_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _policy_hint(exc: Exception, cfg: dict[str, Any]) -> str:
    text = str(exc)
    if "mirror stale" in text or "policy root" in text or "does not exist" in text:
        return (f"{text}\n  hint: the broker's {cfg['policy_root']} (ConfigMap hermes-policies) does not hold "
                "this render's bytes; regenerate the deployed policies from this spec at this ref "
                "(sync-policies.py) and roll the ConfigMap before re-running")
    return text


def provision_create(step: Any, ctx: dict[str, Any], *, inst=None, runner=None, lister=None,
                     sleep=time.sleep, clock=time.monotonic) -> str:
    """Create one worker sandbox through the broker, or bring an existing one to this render's
    policy, and wait until it is Ready. Returns the action taken:

    - ``created``: absent (or just finished deleting) -> ``sandbox create``;
    - ``kept``: present with this render's digest on record;
    - ``started``/``adopted``/``policy-updated``: a Stopped sandbox is started (keeps the PVC);
      a sandbox with no digest record (fresh gateway-agent PVC, pre-existing sandbox) or a
      drifted one gets ``policy set --wait`` (live, keeps the PVC) - never a re-create;
    - ``replaced``: only when ``policy set`` is refused (filesystem/landlock/process change) AND
      ``openshell.allow_replace`` is true; replace deletes the workspace PVC.

    A Completed/Error/Stopping sandbox is refused (inspect it first)."""
    inst = inst or load_installer()
    cfg = ctx["remote"]
    data = step.data or {}
    name = data["sandbox"]
    rendered = Path(data["rendered_policy"])
    if not rendered.is_file():
        raise RemoteConfigError(f"{name}: rendered policy {rendered} is missing; run the compose step first")
    digest = _digest_file(rendered)
    argv = [digest if a == _RENDER_DIGEST_TOKEN else a for a in step.command]
    policy = argv[argv.index("--policy") + 1]
    validate_image_ref(argv[argv.index("--from") + 1], registry_prefix=cfg.get("image_registry_prefix"),
                       allow_placeholder_tag=False)
    run = runner or (lambda a: _run_checked(a, cfg))
    lister = lister or (lambda: list_sandboxes(cfg, runner=runner))
    record = inst._record_policy_digest(Path(ctx["home"]), name)
    recorded = record.read_text(encoding="utf-8-sig").strip() if record.is_file() else None

    def _ready() -> None:
        wait_for(name, cfg, want="ready", lister=lister, sleep=sleep, clock=clock)

    def _record() -> None:
        record.parent.mkdir(parents=True, exist_ok=True)
        inst._atomic_write_text(record, digest + "\n")

    phase = lister().get(name)
    if phase == "Deleting":
        wait_for(name, cfg, want="absent", lister=lister, sleep=sleep, clock=clock)
        phase = None
    if phase is None:
        try:
            run(argv)
        except RuntimeError as exc:
            raise RuntimeError(_policy_hint(exc, cfg)) from None
        _ready()
        _record()
        return "created"
    started = False
    if phase == _PHASE_STOPPED:
        run(["openshell", "sandbox", "start", name])  # 0.1 stop/start keeps the workspace PVC
        started = True
    elif phase in _PHASES_FAILED:
        raise RuntimeError(f"sandbox {name} exists in phase {phase}; it is not replaced automatically "
                           f"(its workspace PVC would be lost). Inspect it (`openshell logs {name}`), then "
                           f"`openshell sandbox delete {name}` and re-run")
    if phase != _PHASE_READY or started:
        _ready()
    if recorded == digest:
        return "started" if started else "kept"
    try:
        run(_policy_set_argv(name, policy, digest))
    except RuntimeError as exc:
        if not cfg.get("allow_replace"):
            raise RemoteConfigError(
                f"sandbox {name}: live `policy set` of this render was refused "
                f"({_policy_hint(exc, cfg)}). OpenShell 0.1 only applies network_policies changes and "
                "filesystem additions live; anything else needs a re-create, which DELETES the workspace "
                "PVC. Set openshell.allow_replace: true to let the installer replace it, or delete the "
                "sandbox yourself and re-run") from None
        argv[2] = "replace"  # broker verifies --policy-sha256 BEFORE deleting (D14)
        try:
            run(argv)
        except RuntimeError as exc2:
            raise RuntimeError(_policy_hint(exc2, cfg)) from None
        _ready()
        _record()
        return "replaced"
    _record()
    return "adopted" if recorded is None else "policy-updated"


def check_providers(step: Any, cfg: dict[str, Any], *, runner=None) -> None:
    """Every provider a worker binds must already exist on the gateway (0.1 has no built-in
    profiles, and a non-interactive create never auto-creates one). Read through the broker's
    ``provider list -o json`` (it returns only the providers it allows)."""
    run = runner or (lambda a: _run_checked(a, cfg, timeout=120))
    needed = list((step.data or {}).get("providers") or [])
    present: list[str] = []
    token = ""
    for _ in range(_LIST_PAGE_LIMIT):
        text = run(list(step.command) + (["--page-token", token] if token else []))
        try:
            payload = json.loads(text)
        except ValueError:
            raise RemoteConfigError("provider list did not return JSON (the broker must support "
                                    "`provider list -o json`)") from None
        if not isinstance(payload, dict):
            raise RemoteConfigError("provider list JSON has an unexpected shape")
        for item in payload.get("providers") or []:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                present.append(item["name"])
        token = payload.get("next_page_token") or ""
        if not token:
            break
    else:
        raise RemoteConfigError(f"provider list did not finish within {_LIST_PAGE_LIMIT} pages")
    missing = [n for n in needed if n not in present]
    if missing:
        raise RemoteConfigError(
            f"OpenShell providers {missing} are not visible through the broker. Operator bootstrap "
            "(broker side, never Hermes): `openshell profile import -f <profile>.yaml`, then "
            "`openshell provider create --name <name> --type <profile-id> --credential <ENV_NAME>` "
            "(S4 provider-bootstrap Job), and allow the name in the broker's provider_map")


def normalize_ssh_config(block: str, sandbox: str, cfg: dict[str, Any]) -> str:
    """Normalise one ``sandbox ssh-config`` block for remote mode: the 0.1 alias
    ``openshell-<sandbox>.<workspace>`` is kept as is (the 0.1 render's ``terminal.ssh_host``),
    only allow-listed keywords pass, and the ProxyCommand is pinned to the broker shim with the
    broker address + token file. A different workspace suffix means ``egress.workspace`` does not
    match the broker's ``workspace`` config, which would leave ssh_host unresolvable."""
    alias = f"openshell-{sandbox}.{cfg['workspace']}"
    accepted = {alias}
    for value in (cfg["broker_addr"], cfg["token_file"]):
        if not _SAFE_TOKEN_RE.match(value):
            raise RemoteConfigError(f"{value!r} cannot be embedded in an ssh ProxyCommand")
    out: list[str] = []
    hosts = proxies = 0
    for raw in block.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.replace("=", " ", 1).split(None, 1)
        key = parts[0].lower()
        value = parts[1].strip() if len(parts) > 1 else ""
        if key not in _SSH_KEYWORDS:
            raise RemoteConfigError(f"{sandbox}: ssh-config keyword {parts[0]!r} is not allowed")
        if "%" in value and key != "proxycommand":
            raise RemoteConfigError(f"{sandbox}: ssh-config {parts[0]} value uses % expansion")
        if key == "host":
            hosts += 1
            if value not in accepted:
                raise RemoteConfigError(
                    f"{sandbox}: ssh-config Host {value!r} is not {alias!r}; egress.workspace "
                    f"({cfg['workspace']!r}) must equal the broker's workspace setting")
            out.append(f"Host {alias}")
        elif key == "proxycommand":
            proxies += 1
            out.append(f"    ProxyCommand {_pinned_proxy_command(value, sandbox, cfg)}")
        else:
            out.append(f"    {parts[0]} {value}")
    if hosts != 1 or proxies != 1:
        raise RemoteConfigError(f"{sandbox}: ssh-config must carry exactly one Host and one ProxyCommand")
    return "\n".join(out) + "\n"


def _pinned_proxy_command(value: str, sandbox: str, cfg: dict[str, Any]) -> str:
    toks = shlex.split(value)
    if len(toks) < 2 or toks[0] != "openshell" or toks[1] != "ssh-proxy":
        raise RemoteConfigError(f"{sandbox}: ProxyCommand must invoke `openshell ssh-proxy` (the broker shim), got {value!r}")
    args = toks[2:]
    seen: dict[str, str] = {}
    if len(args) % 2:
        raise RemoteConfigError(f"{sandbox}: malformed ssh-proxy arguments {args!r}")
    for flag, val in zip(args[::2], args[1::2]):
        if flag not in _SSH_PROXY_FLAGS or flag in seen or not _SAFE_TOKEN_RE.match(val):
            raise RemoteConfigError(f"{sandbox}: unexpected ssh-proxy argument {flag} {val!r}")
        seen[flag] = val
    if seen.get("--name") != sandbox:
        raise RemoteConfigError(f"{sandbox}: ssh-proxy --name {seen.get('--name')!r} does not match")
    if "--workspace" in seen and seen["--workspace"] != cfg["workspace"]:
        raise RemoteConfigError(f"{sandbox}: ssh-proxy --workspace {seen['--workspace']!r} is not "
                                f"egress.workspace {cfg['workspace']!r} (it must equal the broker's workspace)")
    unset = " ".join(f"-u {k}" for k in _SCRUB_ENV)
    return (f"env {unset} OSH_BROKER_ADDR={cfg['broker_addr']} OSH_BROKER_TOKEN_FILE={cfg['token_file']} "
            f"openshell ssh-proxy {' '.join(f'{f} {v}' for f, v in zip(args[::2], args[1::2]))}")


def execute_step(step: Any, ctx: dict[str, Any], *, inst=None) -> None:
    inst = inst or load_installer()
    cfg = ctx["remote"]
    tag = step.tag
    if tag == "remote_note":
        return
    if tag == "remote_preflight":
        for warning in preflight(cfg):
            sys.stderr.write(f"[remote] warning: {warning}\n")
        return
    if tag == "remote_render_check":
        check_render(ctx["out"], ctx["spec"], cfg)
        return
    if tag == "remote_provision_create":
        action = provision_create(step, ctx, inst=inst)
        sys.stderr.write(f"[remote] sandbox {step.data['sandbox']}: {action}\n")
        return
    if tag == "remote_provider_check":
        check_providers(step, cfg)
        return
    if tag == "remote_provision_sshconfig":
        sandbox = step.data["sandbox"]
        spec = ctx.get("spec") or {}
        block = normalize_ssh_config(_run_checked(list(step.command), cfg, timeout=120), sandbox, cfg)
        inst._persist_ssh_config(sandbox, spec.get("project") or "fleet", block, spec)
        return
    if tag in ("provision_create", "provision_sshconfig", "provider_check"):
        raise RemoteConfigError(f"local provisioning step {tag} reached the remote executor")
    if isinstance(step.command, (list, tuple)) and step.command and step.command[0] == "openshell":
        raise RemoteConfigError(f"unexpected openshell step in remote mode: {step.command!r}")
    inst._execute_step(step, ctx)


def _ctx(inst, spec, spec_path, cfg, room_creator=None, parker=None) -> dict[str, Any]:
    ctx = inst._apply_ctx(spec, spec_path, room_creator=room_creator, parker=parker)
    ctx["remote"] = cfg
    return ctx


def apply(spec_path: Any, ref: str, cfg: dict[str, Any], *, room_creator=None, room_exists=None,
          parker=None, inst=None) -> None:
    """Remote-mode twin of ``installer.apply``: same state collection, drift probe and room
    handling; the plan is the remote plan and the executor routes remote steps."""
    inst = inst or load_installer()
    spec = inst.load_spec(spec_path)
    inst._require_openshell(spec)
    check_spec(spec, cfg, for_apply=True)
    state = inst.collect_home_state(spec, spec_path)
    if state.get("profiles"):
        try:
            state["profile_digests"] = inst._compute_profile_digests(spec_path, ref, state)
        except Exception as exc:
            logger.debug("drift probe skipped", exc_info=True)
            sys.stderr.write(f"[remote] drift probe skipped: {exc}\n")
    if room_exists is not None:
        spec_rooms = list((spec.get("rooms") or {}).keys())
        present = [rid for rid in spec_rooms if room_exists(rid)]
        state["rooms"] = "all" if spec_rooms and len(present) == len(spec_rooms) else present
    ctx = _ctx(inst, spec, spec_path, cfg, room_creator, parker)
    for step in build_plan(state, spec_path, ref, cfg, inst=inst):
        execute_step(step, ctx, inst=inst)


def phase_a(spec_path: Any, ref: str, cfg: dict[str, Any], *, inst=None) -> None:
    """Phase A is broker-free (plugin bootstrap into the default home), so it is the base phase
    A after the remote spec checks."""
    inst = inst or load_installer()
    spec = inst.load_spec(spec_path)
    inst._require_openshell(spec)
    check_spec(spec, cfg, for_apply=True)
    inst.phase_a(spec_path, ref, _allow_remote=True)


class RemoteInstaller:
    """Adapter with the ``installer`` entry-point surface (``apply``/``plan_text``) bound to a
    resolved remote config, so the CLI subaction can hand it to ``_apply_openshell``."""

    def __init__(self, cfg: dict[str, Any], inst=None) -> None:
        self.cfg = cfg
        self.inst = inst or load_installer()

    def plan_text(self, spec_path: Any, ref: str, policy_root=None, fork_source=None) -> str:
        _refuse_box_lane_options(policy_root, fork_source)
        return plan_text(spec_path, ref, self.cfg, inst=self.inst)

    def apply(self, spec_path: Any, ref: str, *, room_creator=None, room_exists=None, parker=None,
              policy_root=None, fork_source=None) -> None:
        _refuse_box_lane_options(policy_root, fork_source)
        apply(spec_path, ref, self.cfg, room_creator=room_creator, room_exists=room_exists, parker=parker,
              inst=self.inst)


class LocalInstaller:
    """The local installer with an explicit ``--openshell-mode local`` bound, so OPENSHELL_MODE or
    the spec's ``openshell.mode`` cannot flip an operator's explicit local run to remote."""

    def __init__(self, inst) -> None:
        self.inst = inst

    def plan_text(self, spec_path: Any, ref: str, policy_root=None, fork_source=None) -> str:
        return self.inst.plan_text(spec_path, ref, policy_root=policy_root, fork_source=fork_source,
                                   openshell_mode=MODE_LOCAL)

    def apply(self, spec_path: Any, ref: str, *, room_creator=None, room_exists=None, parker=None,
              policy_root=None, fork_source=None) -> None:
        self.inst.apply(spec_path, ref, room_creator=room_creator, room_exists=room_exists, parker=parker,
                        policy_root=policy_root, fork_source=fork_source, openshell_mode=MODE_LOCAL)


def _refuse_box_lane_options(policy_root: Any, fork_source: Any) -> None:
    if policy_root:
        raise RemoteConfigError("--policy-root is a box-lane option; remote mode binds policies by their "
                                "broker-side path (openshell.policy_root) and --policy-sha256")
    if fork_source:
        raise RemoteConfigError("--fork-source is a box-lane option; remote mode installs plugins from the "
                                "spec's plugin_source")


def describe(cfg: dict[str, Any]) -> str:
    """Operator-facing resolved config (no secret values: the token is referenced by path only)."""
    shown = copy.deepcopy(cfg)
    return json.dumps(shown, indent=2, sort_keys=True) + "\n"


def overrides_from_args(ns: Any) -> dict[str, Any]:
    return {
        "mode": getattr(ns, "openshell_mode", None),
        "broker_addr": getattr(ns, "broker_addr", None),
        "token_file": getattr(ns, "broker_token_file", None),
        "allow_plaintext": True if getattr(ns, "allow_plaintext_broker", False) else None,
    }


def add_cli_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--openshell-mode", choices=list(MODES), default=None,
                        help="local (default: the existing local-gateway/box-lane path) or remote "
                             "(in-cluster OpenShell 0.1 gateway via the broker); overrides "
                             "OPENSHELL_MODE and the spec's openshell.mode")
    parser.add_argument("--broker-addr", default=None,
                        help="remote: broker tcp://<svc>.<ns>.svc:<port> (overrides OSH_BROKER_ADDR / spec)")
    parser.add_argument("--broker-token-file", default=None,
                        help="remote: broker token file (overrides OSH_BROKER_TOKEN_FILE / spec)")
    parser.add_argument("--allow-plaintext-broker", action="store_true",
                        help="remote: allow a non-cluster plaintext broker address (dev/test only)")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="osh-remote")
    sub = parser.add_subparsers(dest="mode", required=True)
    for mode in ("plan", "apply", "phase-a", "show-config"):
        p = sub.add_parser(mode)
        p.add_argument("--spec", required=True)
        if mode != "show-config":
            p.add_argument("--ref", required=True)
        add_cli_args(p)
    ns = parser.parse_args(argv)
    inst = load_installer()
    spec = inst.load_spec(ns.spec)
    try:
        cfg = resolve_config(spec, overrides=overrides_from_args(ns))
        if ns.mode == "show-config":
            sys.stdout.write(describe(cfg))
            return 0
        if not inst.SHA40.match(ns.ref):
            parser.error("--ref must be a full 40-character commit SHA")
        if cfg["mode"] != MODE_REMOTE:
            sys.stderr.write("openshell mode resolved to local; use installer.py for the local path\n")
            return 2
        if ns.mode == "plan":
            sys.stdout.write(plan_text(ns.spec, ns.ref, cfg, inst=inst))
        elif ns.mode == "apply":
            sys.stderr.write("[remote] direct apply has no groups.create dispatcher, so a fleet with rooms "
                             "stops at the rooms step; prefer `hermes coworker install-openshell "
                             "--openshell-mode remote --gateway-url ...`\n")
            apply(ns.spec, ns.ref, cfg, inst=inst)
        else:
            phase_a(ns.spec, ns.ref, cfg, inst=inst)
    except RemoteConfigError as exc:
        sys.stderr.write(f"remote mode refused: {exc}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
