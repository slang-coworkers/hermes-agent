"""OpenShell sandbox-policy porter/validator: 0.0.72 -> 0.1.2 (5d).

The policy FILE stays ``version: 1`` in OpenShell 0.1.x. The "v2" in 0.1 is gateway.toml
(``[openshell] version = 2``), not the sandbox policy. So there is no schema conversion to do.
This module PORTS our 0.0.72-era policies to what a 0.1.2 gateway accepts and what the target
cluster can reach, then VALIDATES the result offline against a Python mirror of the 0.1.2 closed schema.

Port rules (each one is recorded in ``PortResult.changes``; nothing is dropped silently):

* ``binaries[].harness`` is removed (0.1.0 removed the field; refs/OpenShell@v0.1.2
  crates/openshell-policy-schema/src/lib.rs:428-431, proto sandbox.proto:351 ``reserved``).
* ``tls: terminate`` / ``tls: passthrough`` are removed (automatic TLS; 0.1.2 rejects both,
  crates/openshell-policy/src/l7_validate.rs:71-75). We never substitute ``tls: skip``.
* Lane hops are dropped on the Kubernetes target: the Docker bridge ``172.17.0.1`` (the
  18255 OneCLI and 18777 broker hops) and ``host.openshell.internal``. Neither exists in a cluster.
* Inference endpoints are dropped from USER policy. That covers ``inference.local``, removed in
  0.1.0 (docs/upgrade/0-1-0.mdx:95), and every ``--inference-host``. Inference egress comes
  only from the ``_provider_<name>`` layer that ``sandbox create --provider <name>``
  composes from the imported profile. That keeps a single authority, and it avoids the
  composed-policy ambiguity check (crates/openshell-policy/src/ambiguity.rs).
* Hosts outside the target allow-list are dropped. The k8s target has no built-in list: the operator
  names each reachable host with ``--allow-host`` (a provider's egress comes from its provider profile).
* Lane-only filesystem paths (``/opt/osh-lane``, ``/tmp/osh-lane``) and lane binaries are dropped.
* A rule left with no endpoints is dropped. If no rule is left, ``network_policies`` is omitted.
* An L7 endpoint (a ``protocol`` other than ``tcp``) with no ``enforcement`` gets
  ``enforcement: enforce``. 0.1.2 treats an unspecified enforcement as AUDIT
  (proto sandbox.proto:120): rules would be logged, not enforced.

Refused loudly (``PolicyPortError``): an unknown field anywhere, a ``null`` optional section, a
duplicate YAML key, a ``<<`` merge key, more than one document, a version other than 1, a
``_provider_``-prefixed rule key, an unknown tls/enforcement/access/protocol value, and any
other 0.1.2 semantic violation left after porting.

Output is deterministic (fixed key order, sorted rule keys) and carries a sha256 file digest:
the D14 ``--policy-sha256`` value and the ``SHA256SUMS`` line. ``port`` is all-or-nothing: if any
source is refused, nothing is written and ``SHA256SUMS`` is left untouched.

CLI::

    policy_port.py port SRC... --out DIR [--name SRC=NAME]... [--allow-host H]... [--target k8s|docker]
    policy_port.py validate [--strict] FILE...   (--strict: an L7 endpoint without enforcement fails)
    policy_port.py digest FILE...

Stdlib + PyYAML only, or ruamel.yaml where PyYAML is absent (Hermes >= 0.22 ships no PyYAML; its
venv has ruamel.yaml). Both backends load with the same strict limits and dump the same bytes. It
imports nothing from Hermes core, so it runs in CI and offline.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

try:
    import yaml
except ImportError:  # Hermes >= 0.22: no PyYAML; the ruamel.yaml backend below takes over
    yaml = None

# ---------------------------------------------------------------------------------------------
# 0.1.2 closed schema (mirror of crates/openshell-policy-schema/src/lib.rs:137-431)
# ---------------------------------------------------------------------------------------------
TOP_FIELDS = ("version", "filesystem_policy", "landlock", "process", "network_policies",
              "network_middlewares")
FS_FIELDS = ("include_workdir", "read_only", "read_write")
LANDLOCK_FIELDS = ("compatibility",)
PROCESS_FIELDS = ("run_as_user", "run_as_group")
RULE_FIELDS = ("name", "endpoints", "binaries")
# Field order here is the serialization order of NetworkEndpoint in the schema crate.
ENDPOINT_FIELDS = (
    "host", "path", "port", "ports", "protocol", "tls", "enforcement", "access", "rules",
    "allowed_ips", "deny_rules", "allow_encoded_slash", "websocket_credential_rewrite",
    "request_body_credential_rewrite", "allow_uninspected_credentials", "persisted_queries",
    "graphql_persisted_queries", "graphql_max_body_bytes", "credential_signing",
    "signing_service", "signing_region", "credential_binding", "json_rpc", "mcp",
)
L7_MATCH_FIELDS = ("method", "path", "command", "query", "operation_type", "operation_name",
                   "fields", "tool", "params")
BINARY_FIELDS = ("path",)
MCP_FIELDS = ("versions", "max_body_bytes", "strict_tool_names", "allow_all_known_mcp_methods")
NON_NULL_OPTIONAL_TOP = ("filesystem_policy", "landlock", "process")

LANDLOCK_VALUES = ("best_effort", "hard_requirement")
TLS_VALUES = ("", "skip")
ENFORCEMENT_VALUES = ("", "enforce", "audit")
ACCESS_VALUES = ("", "read-only", "read-write", "full")
L7_PROTOCOLS = ("rest", "websocket", "graphql", "sql", "json-rpc", "mcp")
SIGNING_VALUES = ("", "sigv4", "sigv4:body", "sigv4:no_body")
RESERVED_RULE_PREFIX = "_provider_"  # crates/openshell-server/src/grpc/validation.rs:1006-1019
MAX_FILESYSTEM_PATHS = 256          # crates/openshell-policy/src/lib.rs:1030
MAX_PATH_LENGTH = 4096              # crates/openshell-policy/src/lib.rs:1033
MAX_BYTES = 4 * 1024 * 1024         # openshell-policy-schema ParseLimits.max_bytes
MAX_ALIAS_EXPANSIONS = 100          # openshell-policy-schema ParseLimits.max_alias_expansions
MAX_PROTO_POLICY_BYTES = 262_144    # crates/openshell-server/src/grpc/mod.rs:160

# ---------------------------------------------------------------------------------------------
# Port targets
# ---------------------------------------------------------------------------------------------
# No built-in k8s allow-list: the hosts a cluster's worker sandboxes reach are the operator's
# --allow-host values. Inference hosts are dropped from user policy (the provider layer owns
# them): inference.local always, plus every --inference-host.
DEFAULT_K8S_ALLOWED_HOSTS: tuple = ()
INFERENCE_HOSTS = ("inference.local",)
LANE_HOSTS = ("172.17.0.1", "host.openshell.internal", "host.docker.internal")
LANE_PORTS = (18255, 18777)
LANE_PATH_PREFIXES = ("/opt/osh-lane", "/tmp/osh-lane")  # NOSONAR sandbox-policy path constant, no file is created; no-tmp: ok — a lane path inside the OpenShell sandbox policy, not Hermes scratch space
LANE_BINARIES = ("/usr/bin/socat", "/usr/bin/ssh")


class PolicyPortError(ValueError):
    """The policy cannot be ported or does not validate against the 0.1.2 schema."""


class PortResult:
    __slots__ = ("changes", "document", "sha256", "text", "warnings")

    def __init__(self, document: dict[str, Any], changes: list[str], warnings: list[str]) -> None:
        self.document = document
        self.changes = changes
        self.warnings = warnings
        self.text = dump_policy(document)
        self.sha256 = hashlib.sha256(self.text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------------
# Strict YAML loading (duplicate keys, merge keys, multi-doc, alias budget)
# ---------------------------------------------------------------------------------------------
if yaml is not None:
    class _StrictLoader(yaml.SafeLoader):
        """SafeLoader that rejects what the 0.1.2 parser rejects (schema lib.rs:512-544)."""

        def __init__(self, stream: Any) -> None:
            super().__init__(stream)
            self._alias_count = 0

        def compose_node(self, parent: Any, index: Any) -> Any:  # type: ignore[override]
            if self.check_event(yaml.events.AliasEvent):
                self._alias_count += 1
                if self._alias_count > MAX_ALIAS_EXPANSIONS:
                    raise PolicyPortError(
                        f"policy uses more than {MAX_ALIAS_EXPANSIONS} YAML alias expansions")
            return super().compose_node(parent, index)

        def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:  # type: ignore[override]
            _check_mapping_keys(node, lambda key_node: self.construct_object(key_node, deep=True))
            return super().construct_mapping(node, deep=deep)


def _check_mapping_keys(node: Any, construct_key: Any) -> None:
    """Refuse a ``<<`` merge key or a duplicate key in one mapping node (both backends)."""
    seen = set()
    for key_node, _ in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge" or key_node.value == "<<":
            raise PolicyPortError(
                f"YAML merge keys ('<<') are rejected by OpenShell 0.1.2 "
                f"(line {key_node.start_mark.line + 1})")
        key = construct_key(key_node)
        if key in seen:
            raise PolicyPortError(
                f"duplicate YAML key {key!r} (line {key_node.start_mark.line + 1}); "
                f"OpenShell 0.1.2 rejects duplicate keys")
        seen.add(key)


def _ruamel_yaml11(typ: str = "safe") -> Any:
    """A pure ruamel.yaml instance that resolves (and quotes) plain scalars as YAML 1.1, like PyYAML."""
    import re

    from ruamel.yaml import YAML
    from ruamel.yaml.resolver import VersionedResolver, implicit_resolvers

    class _Yaml11Resolver(VersionedResolver):
        @property
        def processing_version(self) -> tuple[int, int]:
            return (1, 1)

        @property
        def versioned_resolver(self) -> Any:
            # ruamel's YAML 1.1 table also reads bare y/Y/n/N as booleans and 1.5e3 (unsigned
            # exponent) as a float; PyYAML does neither. Use PyYAML's two patterns.
            pyyaml = {
                "tag:yaml.org,2002:bool": re.compile(
                    r"^(?:yes|Yes|YES|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF)$"),
                "tag:yaml.org,2002:float": re.compile(
                    r"^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?|\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?"
                    r"|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$"),
            }
            if (1, 1) not in self._version_implicit_resolver:
                for versions, tag, regexp, first in implicit_resolvers:
                    if (1, 1) in versions:
                        self.add_version_implicit_resolver((1, 1), tag, pyyaml.get(tag, regexp), first)
            return self._version_implicit_resolver[(1, 1)]

    y = YAML(typ=typ, pure=True)
    y.Resolver = _Yaml11Resolver
    return y


def _ruamel_load_all_strict(text: str) -> list[Any]:
    """ruamel.yaml twin of ``yaml.load_all(text, Loader=_StrictLoader)``."""
    from ruamel.yaml.events import AliasEvent
    from ruamel.yaml.nodes import MappingNode, SequenceNode

    y = _ruamel_yaml11()
    aliases = sum(1 for event in y.parse(text) if isinstance(event, AliasEvent))
    if aliases > MAX_ALIAS_EXPANSIONS:
        raise PolicyPortError(f"policy uses more than {MAX_ALIAS_EXPANSIONS} YAML alias expansions")
    key_loader = _ruamel_yaml11()

    def walk(node: Any, seen: set) -> None:
        if id(node) in seen:
            return
        seen.add(id(node))
        if isinstance(node, MappingNode):
            _check_mapping_keys(node, lambda key_node: key_loader.constructor.construct_object(key_node, deep=True))
            for key_node, value_node in node.value:
                walk(key_node, seen)
                walk(value_node, seen)
        elif isinstance(node, SequenceNode):
            for item in node.value:
                walk(item, seen)

    for root in _ruamel_yaml11().compose_all(text):
        walk(root, set())
    return list(_ruamel_yaml11().load_all(text))


def load_policy_text(text: str, source: str = "<policy>") -> dict[str, Any]:
    """Parse one authored policy with the 0.1.2 parser's structural limits."""
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise PolicyPortError(f"{source}: policy exceeds {MAX_BYTES} bytes")
    if yaml is not None:
        yaml_error: Any = yaml.YAMLError
    else:
        from ruamel.yaml.error import YAMLError as yaml_error
    try:
        if yaml is not None:
            docs = list(yaml.load_all(text, Loader=_StrictLoader))
        else:
            docs = _ruamel_load_all_strict(text)
    except PolicyPortError as exc:
        raise PolicyPortError(f"{source}: {exc}") from None
    except yaml_error as exc:
        raise PolicyPortError(f"{source}: invalid YAML: {exc}") from None
    docs = [d for d in docs if d is not None]
    if len(docs) != 1:
        raise PolicyPortError(f"{source}: expected exactly one YAML document, found {len(docs)}")
    doc = docs[0]
    if not isinstance(doc, dict):
        raise PolicyPortError(f"{source}: policy must be a mapping, got {type(doc).__name__}")
    return doc


def load_policy_file(path: Any) -> dict[str, Any]:
    p = Path(path)
    return load_policy_text(p.read_text(encoding="utf-8-sig"), str(p))


# ---------------------------------------------------------------------------------------------
# Validation (offline mirror of 0.1.2 schema + semantic checks)
# ---------------------------------------------------------------------------------------------
def _unknown(mapping: dict[str, Any], allowed: Sequence[str], where: str, errors: list[str]) -> None:
    for key in mapping:
        if key not in allowed:
            hint = ""
            if key == "harness":
                hint = " (the deprecated 'harness' field was removed in 0.1.0)"
            errors.append(f"unknown field '{where}.{key}'{hint}")


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _host_wildcard_shape_invalid(host: str) -> bool:
    # crates/openshell-policy/src/lib.rs:1685-1702
    if host in ("*", "**"):
        return True
    if "*" not in host:
        return False
    labels = host.split(".")
    first = labels[0]
    if "**" in first and first != "**":
        return True
    return any("**" in lab or ("*" in lab and lab != "*") for lab in labels[1:])


def _validate_matcher_block(rule: Any, key: str, where: str, errors: list[str]) -> bool:
    """Validate one allow/deny matcher. Returns True when it allows nothing."""
    if not isinstance(rule, dict):
        errors.append(f"{where}: {key} must be a mapping")
        return True
    _unknown(rule, L7_MATCH_FIELDS, where, errors)
    for field in ("method", "path", "command", "operation_type", "operation_name"):
        if field in rule and not isinstance(rule[field], str):
            errors.append(f"{where}.{field} must be a string")
    return not any(rule.get(f) for f in ("method", "path", "command", "operation_type",
                                          "operation_name", "fields", "params"))


# health: allow CC -- _validate_endpoint is an ordered fail-closed check of every endpoint field the 0.1.2 schema accepts; not split until tests cover each branch, so the split cannot change a verdict
def _validate_endpoint(ep: Any, where: str, errors: list[str]) -> None:
    if not isinstance(ep, dict):
        errors.append(f"{where} must be a mapping")
        return
    _unknown(ep, ENDPOINT_FIELDS, where, errors)
    host = ep.get("host", "")
    if not isinstance(host, str):
        errors.append(f"{where}.host must be a string")
        host = ""
    protocol = ep.get("protocol", "")
    if not isinstance(protocol, str):
        errors.append(f"{where}.protocol must be a string")
        protocol = ""
    explicit_tcp = protocol.lower() == "tcp"
    l7 = protocol.lower() if protocol.lower() in L7_PROTOCOLS else None
    if protocol and not explicit_tcp and l7 is None:
        errors.append(f"{where}: unknown protocol '{protocol}' (expected tcp, rest, websocket, "
                      f"graphql, sql, json-rpc, or mcp)")
    allowed_ips = ep.get("allowed_ips") or []
    if not host.strip():
        if explicit_tcp or not allowed_ips:
            errors.append(f"{where}: endpoint host must not be empty")
    elif explicit_tcp and _is_ip_literal(host):
        errors.append(f"{where}: protocol tcp needs a DNS host, not the IP literal {host!r}")
    if host and "*" in host:
        if (host.startswith("*.") or host.startswith("**.")) and len(host.split(".")) <= 2:
            errors.append(f"{where}: TLD wildcard host {host!r} is rejected")
        if _host_wildcard_shape_invalid(host):
            errors.append(f"{where}: invalid wildcard host shape {host!r}")
    port = ep.get("port", 0)
    ports = ep.get("ports") or []
    if not isinstance(port, int) or isinstance(port, bool):
        errors.append(f"{where}.port must be an integer")
        port = 0
    if not isinstance(ports, list):
        errors.append(f"{where}.ports must be a list")
        ports = []
    effective = list(ports) if ports else ([port] if port else [])
    if not effective:
        errors.append(f"{where}: endpoint {host!r} has no port (0.1.2 MissingEndpointPort)")
    for p in effective:
        if not isinstance(p, int) or isinstance(p, bool) or not 1 <= p <= 65535:
            errors.append(f"{where}: port {p!r} outside 1..65535")
    tls = ep.get("tls", "")
    if tls not in TLS_VALUES:
        errors.append(f"{where}: unknown tls value {tls!r}; omit the field for automatic TLS "
                      f"(terminate/passthrough were removed in 0.1.0)")
    enforcement = ep.get("enforcement", "")
    if enforcement not in ENFORCEMENT_VALUES:
        errors.append(f"{where}: unknown enforcement value {enforcement!r}")
    access = ep.get("access", "")
    if access not in ACCESS_VALUES:
        errors.append(f"{where}: unknown access value {access!r}")
    signing = ep.get("credential_signing", "")
    if signing not in SIGNING_VALUES:
        errors.append(f"{where}: unknown credential_signing value {signing!r}")
    if signing and not ep.get("signing_service"):
        errors.append(f"{where}: credential_signing requires signing_service")
    if signing and ep.get("request_body_credential_rewrite"):
        errors.append(f"{where}: credential_signing and request_body_credential_rewrite conflict")
    rules = ep.get("rules") or []
    deny_rules = ep.get("deny_rules") or []
    if not isinstance(rules, list) or not isinstance(deny_rules, list):
        errors.append(f"{where}: rules/deny_rules must be lists")
        return
    empties = []
    for i, rule in enumerate(rules):
        rwhere = f"{where}.rules[{i}]"
        if not isinstance(rule, dict):
            errors.append(f"{rwhere} must be a mapping")
            continue
        _unknown(rule, ("allow",), rwhere, errors)
        if "allow" not in rule:
            errors.append(f"{rwhere}: missing 'allow'")
            empties.append(True)
            continue
        empties.append(_validate_matcher_block(rule["allow"], "allow", f"{rwhere}.allow", errors))
    for i, rule in enumerate(deny_rules):
        _validate_matcher_block(rule, "deny", f"{where}.deny_rules[{i}]", errors)
    mcp = ep.get("mcp")
    if "mcp" in ep:
        if not isinstance(mcp, dict):
            errors.append(f"{where}.mcp must be a mapping (null is rejected)")
            mcp = {}
        else:
            _unknown(mcp, MCP_FIELDS, f"{where}.mcp", errors)
            versions = mcp.get("versions")
            if "versions" in mcp:
                if not isinstance(versions, list) or not versions:
                    errors.append(f"{where}.mcp.versions must be a non-empty list of exact revisions")
                elif len(set(versions)) != len(versions) or any(
                        v in ("draft", "latest") for v in versions):
                    errors.append(f"{where}.mcp.versions must be unique exact revisions "
                                  f"(no draft/latest)")
    mcp_all = bool((mcp or {}).get("allow_all_known_mcp_methods"))
    for opt in ("credential_binding", "json_rpc"):
        if opt in ep and not isinstance(ep[opt], dict):
            errors.append(f"{where}.{opt} must be a mapping (null is rejected)")
    if isinstance(ep.get("credential_binding"), dict):
        _unknown(ep["credential_binding"], ("provider",), f"{where}.credential_binding", errors)
    # L7 semantics: crates/openshell-policy/src/l7_validate.rs:243-330
    if explicit_tcp:
        l7_only = [f for f in ("access", "rules", "deny_rules", "enforcement", "path",
                               "allow_encoded_slash", "websocket_credential_rewrite",
                               "request_body_credential_rewrite", "persisted_queries",
                               "graphql_persisted_queries") if ep.get(f)]
        if l7_only:
            errors.append(f"{where}: protocol tcp does not support L7 fields {l7_only}")
    if rules and access:
        errors.append(f"{where}: rules and access are mutually exclusive")
    if l7 in ("json-rpc", "mcp") and access:
        errors.append(f"{where}: protocol {l7} does not support access presets")
    if l7 == "json-rpc" and not rules:
        errors.append(f"{where}: protocol json-rpc requires explicit rules")
    if l7 and l7 not in ("json-rpc", "mcp") and not rules and not access:
        errors.append(f"{where}: protocol {l7} requires rules or access")
    if l7 == "mcp" and not rules and not access and not mcp_all:
        errors.append(f"{where}: protocol mcp requires rules unless allow_all_known_mcp_methods")
    if rules and all(empties) and not (l7 == "mcp" and mcp_all):
        errors.append(f"{where}: rules would deny all traffic (no allow clause)")
    if deny_rules and (not protocol or (not rules and not access)):
        errors.append(f"{where}: deny_rules need a protocol and a base allow set")
    if not protocol and (rules or access or enforcement):
        errors.append(f"{where}: rules/access/enforcement need an L7 protocol")


# health: allow CC -- validate_policy is an ordered fail-closed walk of the whole policy document; not split until tests cover each branch, so the split cannot change a verdict
def validate_policy(doc: Any, source: str = "<policy>") -> list[str]:
    """Return every 0.1.2 violation in ``doc`` (empty list == valid). Offline mirror of the
    schema walk + ``validate_policy`` + L7 semantics; the composed-policy ambiguity check and
    provider layers can only be checked by a live gateway (see NEEDS-NETWORK)."""
    errors: list[str] = []
    if not isinstance(doc, dict):
        return [f"{source}: policy must be a mapping"]
    _unknown(doc, TOP_FIELDS, "policy", errors)
    if "version" not in doc:
        errors.append("missing required field 'version'")
    elif doc["version"] != 1 or isinstance(doc["version"], bool):
        errors.append(f"unsupported policy version {doc['version']!r}; expected version 1")
    for key in NON_NULL_OPTIONAL_TOP:
        if key in doc and doc[key] is None:
            errors.append(f"'{key}: null' is rejected; omit the key instead")
    fs = doc.get("filesystem_policy")
    if isinstance(fs, dict):
        _unknown(fs, FS_FIELDS, "filesystem_policy", errors)
        ro = fs.get("read_only") or []
        rw = fs.get("read_write") or []
        if not isinstance(ro, list) or not isinstance(rw, list):
            errors.append("filesystem_policy.read_only/read_write must be lists")
        else:
            if len(ro) + len(rw) > MAX_FILESYSTEM_PATHS:
                errors.append(f"too many filesystem paths ({len(ro) + len(rw)} > {MAX_FILESYSTEM_PATHS})")
            for path in [*ro, *rw]:
                if not isinstance(path, str):
                    errors.append(f"filesystem path {path!r} must be a string")
                    continue
                if len(path) > MAX_PATH_LENGTH:
                    errors.append(f"filesystem path too long ({len(path)})")
                if not path.startswith("/"):
                    errors.append(f"filesystem path {path!r} must be absolute")
                if ".." in path.split("/"):
                    errors.append(f"filesystem path {path!r} contains '..'")
            for path in rw:
                if isinstance(path, str) and not path.rstrip("/"):
                    errors.append("read_write '/' is overly broad")
        if "include_workdir" in fs and not isinstance(fs["include_workdir"], bool):
            errors.append("filesystem_policy.include_workdir must be a boolean")
    elif fs is not None:
        errors.append("filesystem_policy must be a mapping")
    landlock = doc.get("landlock")
    if isinstance(landlock, dict):
        _unknown(landlock, LANDLOCK_FIELDS, "landlock", errors)
        comp = landlock.get("compatibility", "best_effort")
        if comp not in LANDLOCK_VALUES:
            errors.append(f"landlock.compatibility {comp!r} must be one of {LANDLOCK_VALUES}")
    elif landlock is not None:
        errors.append("landlock must be a mapping")
    process = doc.get("process")
    if isinstance(process, dict):
        _unknown(process, PROCESS_FIELDS, "process", errors)
        for field in PROCESS_FIELDS:
            val = process.get(field, "")
            if val in ("", "sandbox"):
                continue
            if isinstance(val, int) and not isinstance(val, bool):
                val = str(val)
            if not (isinstance(val, str) and val.isdigit() and 1 <= int(val) <= 2**32 - 2):
                errors.append(f"process.{field} {val!r} must be 'sandbox' or a numeric id")
    elif process is not None:
        errors.append("process must be a mapping")
    policies = doc.get("network_policies") or {}
    if not isinstance(policies, dict):
        errors.append("network_policies must be a mapping")
        policies = {}
    for key in sorted(policies):
        rule = policies[key]
        where = f"network_policies.{key}"
        if str(key).startswith(RESERVED_RULE_PREFIX):
            errors.append(f"{where}: the '{RESERVED_RULE_PREFIX}' prefix is reserved for "
                          f"provider-composed rules")
        if not isinstance(rule, dict):
            errors.append(f"{where} must be a mapping")
            continue
        _unknown(rule, RULE_FIELDS, where, errors)
        for i, ep in enumerate(rule.get("endpoints") or []):
            _validate_endpoint(ep, f"{where}.endpoints[{i}]", errors)
        for i, b in enumerate(rule.get("binaries") or []):
            bwhere = f"{where}.binaries[{i}]"
            if not isinstance(b, dict):
                errors.append(f"{bwhere} must be a mapping with 'path'")
                continue
            _unknown(b, BINARY_FIELDS, bwhere, errors)
            if not isinstance(b.get("path"), str) or not b.get("path"):
                errors.append(f"{bwhere}.path must be a non-empty string")
    if doc.get("network_middlewares"):
        errors.append("network_middlewares are not ported by this tool; author them by hand "
                      "and validate against a live 0.1.2 gateway")
    return [f"{source}: {e}" for e in errors]


# ---------------------------------------------------------------------------------------------
# Porting
# ---------------------------------------------------------------------------------------------
def _norm_host(host: str) -> str:
    return host.strip().rstrip(".").lower()


def _is_lane_path(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in LANE_PATH_PREFIXES)


def _is_l7_endpoint(ep: Any) -> bool:
    protocol = ep.get("protocol") if isinstance(ep, dict) else None
    return isinstance(protocol, str) and protocol.lower() in L7_PROTOCOLS


def lint_policy(doc: Any) -> list[str]:
    """Safety findings that the 0.1.2 schema ACCEPTS but that we do not want: an L7 endpoint
    without ``enforcement`` (AUDIT, i.e. rules logged but not enforced)."""
    findings: list[str] = []
    policies = doc.get("network_policies") if isinstance(doc, dict) else None
    for key, rule in sorted((policies or {}).items()) if isinstance(policies, dict) else []:
        for i, ep in enumerate((rule or {}).get("endpoints") or [] if isinstance(rule, dict) else []):
            if _is_l7_endpoint(ep) and not ep.get("enforcement"):
                findings.append(f"network_policies.{key}.endpoints[{i}] ({ep.get('host')}:"
                                f"{ep.get('port')}): L7 endpoint without enforcement runs in AUDIT "
                                f"mode (sandbox.proto:120); set enforcement: enforce")
    return findings


def _ordered_endpoint(ep: dict[str, Any]) -> dict[str, Any]:
    return {k: ep[k] for k in ENDPOINT_FIELDS if k in ep}


# health: allow CC -- port_policy is an ordered fail-closed port (strip, refuse, rebuild, re-validate); not split until tests cover each branch, so the split cannot change a verdict
def port_policy(
    doc: dict[str, Any],
    *,
    target: str = "k8s",
    allowed_hosts: Optional[Sequence[str]] = None,
    inference_hosts: Sequence[str] = (),
    source: str = "<policy>",
) -> PortResult:
    """Port one 0.0.72 policy document to 0.1.2. ``target`` is ``k8s`` (a cluster: lane hops and
    non-allow-listed hosts are dropped) or ``docker`` (a 0.1 Docker-driver box: only the
    schema-level fixes, inference.local and the 172.17.0.1 hops). Raises ``PolicyPortError``
    when anything unknown, or anything still invalid after porting, is found."""
    if target not in ("k8s", "docker"):
        raise PolicyPortError(f"unknown target {target!r} (k8s|docker)")
    allow = None
    if target == "k8s":
        allow = {_norm_host(h) for h in (allowed_hosts if allowed_hosts is not None
                                         else DEFAULT_K8S_ALLOWED_HOSTS)}
        if not allow:
            raise PolicyPortError("the k8s target needs its allow-list: name each reachable host with --allow-host")
    inference = {_norm_host(h) for h in (*INFERENCE_HOSTS, *inference_hosts)}
    changes: list[str] = []
    warnings: list[str] = []
    if not isinstance(doc, dict):
        raise PolicyPortError(f"{source}: policy must be a mapping")

    # 1. Strip the 0.0.72-only constructs we know how to remove, then refuse anything else
    # unknown. Unknown fields are checked AFTER these specific removals so the error points at a
    # real problem.
    work = json.loads(json.dumps(doc))  # deep copy, plain types
    for key, rule in (work.get("network_policies") or {}).items():
        if not isinstance(rule, dict):
            continue
        for b in rule.get("binaries") or []:
            if isinstance(b, dict) and "harness" in b:
                b.pop("harness")
                changes.append(f"network_policies.{key}: removed binaries[].harness "
                               f"(field removed in 0.1.0)")
        for ep in rule.get("endpoints") or []:
            if isinstance(ep, dict) and ep.get("tls") in ("terminate", "passthrough"):
                changes.append(f"network_policies.{key}: removed tls: {ep['tls']} on "
                               f"{ep.get('host')}:{ep.get('port')} (automatic TLS in 0.1.x)")
                ep.pop("tls")
    pre = [e for e in validate_policy(work, source)
           if "unknown field" in e or "null" in e or "version" in e or RESERVED_RULE_PREFIX in e]
    if pre:
        raise PolicyPortError("refusing to port:\n  " + "\n  ".join(pre))

    out: dict[str, Any] = {"version": 1}
    fs = work.get("filesystem_policy")
    if isinstance(fs, dict):
        new_fs: dict[str, Any] = {}
        if "include_workdir" in fs:
            new_fs["include_workdir"] = fs["include_workdir"]
        for field in ("read_only", "read_write"):
            kept = []
            for path in fs.get(field) or []:
                if target == "k8s" and _is_lane_path(path):
                    changes.append(f"filesystem_policy.{field}: dropped lane path {path}")
                    continue
                if path not in kept:
                    kept.append(path)
            if kept:
                new_fs[field] = kept
        out["filesystem_policy"] = new_fs
        if target == "k8s":
            ro_rw = [*(new_fs.get("read_only") or []), *(new_fs.get("read_write") or [])]
            if any(p in ("/home/sandbox", "/workspace") for p in ro_rw):
                warnings.append("filesystem_policy lists /home/sandbox or /workspace; on K8s the "
                                "workdir is /sandbox (driver.rs:670). Kept; best_effort skips "
                                "missing paths")
    landlock = work.get("landlock")
    if isinstance(landlock, dict):
        out["landlock"] = {"compatibility": landlock.get("compatibility", "best_effort")}
        if out["landlock"]["compatibility"] == "hard_requirement":
            warnings.append("landlock hard_requirement: OpenShell 0.1.2 needs Landlock ABI >= 3 "
                            "(check the cluster's kernel)")
    process = work.get("process")
    if isinstance(process, dict):
        out["process"] = {k: process[k] for k in PROCESS_FIELDS if k in process}
        if target == "k8s" and out["process"]:
            warnings.append("process.run_as_* is ignored by the 0.1.2 Kubernetes driver; the UID "
                            "comes from the namespace SCC range (driver.rs:1452-1506)")

    new_policies: dict[str, Any] = {}
    for key in sorted(work.get("network_policies") or {}):
        rule = work["network_policies"][key]
        endpoints = []
        for ep in rule.get("endpoints") or []:
            host = _norm_host(str(ep.get("host", "")))
            port = ep.get("port")
            label = f"{ep.get('host')}:{port}"
            if host in inference:
                why = ("inference.local was removed in 0.1.0" if host == "inference.local" else
                       "inference egress comes only from the provider-profile layer "
                       "(_provider_<name>) attached at sandbox create")
                changes.append(f"network_policies.{key}: dropped inference endpoint {label} ({why})")
                continue
            if host in LANE_HOSTS or (port in LANE_PORTS and _is_ip_literal(host)):
                changes.append(f"network_policies.{key}: dropped lane hop {label} "
                               f"(box-only Docker bridge; no equivalent on {target})")
                continue
            if allow is not None and host not in allow:
                changes.append(f"network_policies.{key}: dropped {label} (not on the k8s "
                               f"allow-list: {', '.join(sorted(allow))})")
                continue
            if ep.get("tls") == "skip":
                warnings.append(f"network_policies.{key}: {label} keeps tls: skip (admin-authored "
                                f"only; disables inspection and credential rewrite)")
            if _is_l7_endpoint(ep) and not ep.get("enforcement"):
                ep = dict(ep, enforcement="enforce")
                changes.append(f"network_policies.{key}: set enforcement: enforce on {label} "
                               f"(an unspecified enforcement is AUDIT in 0.1.2, sandbox.proto:120)")
            endpoints.append(_ordered_endpoint(ep))
        if not endpoints:
            if rule.get("endpoints"):
                changes.append(f"network_policies.{key}: dropped the rule (no endpoints left)")
            continue
        binaries = []
        for b in rule.get("binaries") or []:
            path = b.get("path")
            if target == "k8s" and (_is_lane_path(path) or path in LANE_BINARIES):
                changes.append(f"network_policies.{key}: dropped lane binary {path}")
                continue
            if {"path": path} not in binaries:
                binaries.append({"path": path})
        new_rule: dict[str, Any] = {"name": rule.get("name") or key, "endpoints": endpoints}
        if binaries:
            new_rule["binaries"] = binaries
        new_policies[key] = new_rule
    if new_policies:
        out["network_policies"] = new_policies
    elif work.get("network_policies"):
        changes.append("network_policies: omitted (every rule was dropped; egress comes only "
                       "from attached provider profiles)")

    errors = validate_policy(out, source)
    if errors:
        raise PolicyPortError("ported policy is still invalid:\n  " + "\n  ".join(errors))
    result = PortResult(out, changes, warnings)
    if len(result.text.encode("utf-8")) > MAX_PROTO_POLICY_BYTES:
        raise PolicyPortError(f"{source}: ported policy exceeds the gateway's "
                              f"{MAX_PROTO_POLICY_BYTES}-byte cap")
    return result


def dump_policy(doc: dict[str, Any]) -> str:
    """Deterministic YAML: fixed top-level/endpoint order (built by ``port_policy``), no
    flow style, no aliases."""

    if yaml is None:
        return _ruamel_dump_policy(doc)

    class _NoAlias(yaml.SafeDumper):
        def ignore_aliases(self, data: Any) -> bool:
            return True

    return yaml.dump(doc, Dumper=_NoAlias, sort_keys=False, default_flow_style=False,
                     allow_unicode=False, width=4096)


def _ruamel_dump_policy(doc: dict[str, Any]) -> str:
    """ruamel.yaml twin of the PyYAML ``dump_policy`` emitter: PyYAML's block layout (sequences not
    indented under their key), insertion order, ASCII escapes, width 4096, no aliases."""
    import io

    from ruamel.yaml.representer import SafeRepresenter

    class _NoAliasRepresenter(SafeRepresenter):
        def ignore_aliases(self, data: Any) -> bool:
            return True

    y = _ruamel_yaml11()
    y.Representer = _NoAliasRepresenter
    y.default_flow_style = False
    y.allow_unicode = False
    y.width = 4096
    y.sort_base_mapping_type_on_output = False
    y.indent(mapping=2, sequence=2, offset=0)
    out = io.StringIO()
    y.dump(doc, out)
    return out.getvalue()


def sha256_file(path: Any) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_sha256sums(out_dir: Any) -> Path:
    """(Re)write ``SHA256SUMS`` over every ``*.yaml`` in ``out_dir`` (sha256sum format, sorted)."""
    out = Path(out_dir)
    lines = [f"{sha256_file(p)}  {p.name}" for p in sorted(out.glob("*.yaml"))]
    target = out / "SHA256SUMS"
    _atomic_write_text(target, "".join(line + "\n" for line in lines))
    return target


HEADER = ("# OpenShell 0.1.2 sandbox policy (file schema version 1), ported from {source}\n"
          "# by plugins/nv-coworker-compose/openshell/policy_port.py (target={target}).\n"
          "# Generated: do not edit; regenerate and refresh SHA256SUMS.\n")


def render_ported(result: PortResult, source: str, target: str) -> str:
    return HEADER.format(source=source, target=target) + result.text


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------
def _cmd_port(args: argparse.Namespace) -> int:
    names: dict[str, str] = {}
    for item in args.name or []:
        src, _, name = item.partition("=")
        if not name:
            print(f"--name expects SRC=NAME, got {item!r}", file=sys.stderr)
            return 2
        names[src] = name
    labels: dict[str, str] = {}
    for item in args.source_label or []:
        src, _, label = item.partition("=")
        if not label:
            print(f"--source-label expects SRC=LABEL, got {item!r}", file=sys.stderr)
            return 2
        labels[src] = label
    out_dir = Path(args.out)
    allowed = args.allow_host or None
    report: list[dict[str, Any]] = []
    pending: dict[str, str] = {}
    failed = False
    # Phase 1: port + validate EVERY source in memory. Nothing is written unless all succeed, so
    # a refused source can never leave a partial set or a truncated SHA256SUMS behind.
    for src in args.sources:
        name = names.get(src) or Path(src).stem
        label = labels.get(src, src)
        try:
            result = port_policy(load_policy_file(src), target=args.target,
                                 allowed_hosts=allowed, inference_hosts=args.inference_host or (),
                                 source=label)
        except (PolicyPortError, OSError) as exc:
            print(f"ERROR {src}: {exc}", file=sys.stderr)
            failed = True
            continue
        fname = f"{name}.yaml"
        if fname in pending:
            print(f"ERROR {src}: output name {fname} is produced by more than one source",
                  file=sys.stderr)
            failed = True
            continue
        text = render_ported(result, label, args.target)
        pending[fname] = text
        report.append({"source": label, "output": fname,
                       "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                       "changes": result.changes, "warnings": result.warnings})
    if failed:
        print(f"refused: nothing written to {out_dir} (port is all-or-nothing)", file=sys.stderr)
        return 1
    # Phase 2: atomic per-file replace, then SHA256SUMS last.
    out_dir.mkdir(parents=True, exist_ok=True)
    for fname, text in pending.items():
        _atomic_write_text(out_dir / fname, text)
    write_sha256sums(out_dir)
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    else:
        print(json.dumps(report, indent=2))
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    rc = 0
    for path in args.files:
        try:
            doc = load_policy_file(path)
            errors = validate_policy(doc, path)
            findings = lint_policy(doc)
        except PolicyPortError as exc:
            errors, findings = [str(exc)], []
        if args.strict:
            errors = errors + findings
        else:
            for f in findings:
                print(f"WARNING {path}: {f}", file=sys.stderr)
        for e in errors:
            print(e)
        if errors:
            rc = 1
        else:
            print(f"{path}: OK (0.1.2 offline schema)")
    return rc


def _cmd_digest(args: argparse.Namespace) -> int:
    for path in args.files:
        print(f"{sha256_file(path)}  {path}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="policy_port.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("port", help="port 0.0.72 policies to 0.1.2 and write SHA256SUMS")
    p.add_argument("sources", nargs="+")
    p.add_argument("--out", required=True)
    p.add_argument("--name", action="append", help="SRC=NAME output basename override")
    p.add_argument("--allow-host", action="append",
                   help="k8s allow-list host (repeatable; required for --target k8s)")
    p.add_argument("--inference-host", action="append",
                   help="an inference endpoint host to drop from user policy (repeatable; inference.local always is)")
    p.add_argument("--target", choices=("k8s", "docker"), default="k8s")
    p.add_argument("--report", help="write the JSON change report here instead of stdout")
    p.add_argument("--source-label", action="append",
                   help="SRC=LABEL provenance written into the output header and report")
    p.set_defaults(func=_cmd_port)
    v = sub.add_parser("validate", help="validate policies against the 0.1.2 offline schema")
    v.add_argument("--strict", action="store_true",
                   help="also fail on accepted-but-unsafe constructs (L7 endpoint in AUDIT mode)")
    v.add_argument("files", nargs="+")
    v.set_defaults(func=_cmd_validate)
    d = sub.add_parser("digest", help="print sha256sum-format digests (D14 --policy-sha256)")
    d.add_argument("files", nargs="+")
    d.set_defaults(func=_cmd_digest)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
