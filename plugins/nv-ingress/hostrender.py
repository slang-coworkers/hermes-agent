"""The ``ingress:`` fleet-spec block: validation and the host-side artefacts.

This module is the single owner of both. ``nv-coworker-compose`` loads it by
sibling path during a render, and ``hermes ingress render-host`` calls the same
functions, so the units and the edge config cannot drift between the two. Every
secret is named by its FILE path on the host and is never read here.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List

NO_AUTH = "INSECURE_NO_AUTH"  # release gateway/platforms/webhook.py:131, the loopback-gated mode
LOOPBACK = "127.0.0.1"
PLATFORMS = ("github", "gitlab")
ROUTE_SCRIPT = "ingress_stage.py"
UNITS = ("nv-ingress-edge.service", "nv-ingress-forward.service", "nv-ingress-gateway-boot.service")

_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_KEYS = {
    "route", "port", "platforms", "replace_routes", "issue_labels", "self_logins",
    "per_pr_hourly_budget", "gateway_sandbox", "gateway_start",
    "host_checkout", "host_python", "host_dir", "openshell_bin", "edge_listen", "edge_state_dir",
    "scope",
}


def _sibling(rel: str, name: str):
    path = Path(__file__).resolve().parent / rel
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _envelope():
    return _sibling("envelope.py", "_nv_ingress_envelope_render")


def routed_events() -> List[str]:
    return list(_envelope().ROUTED_EVENTS)


def _str_list(value: Any, key: str, *, nonempty: bool) -> List[str]:
    if value is None and not nonempty:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ValueError(f"ingress.{key} must be a list of non-empty strings")
    if nonempty and not value:
        raise ValueError(f"ingress.{key} must not be empty")
    return [v.strip() for v in value]


def _no_newline(value: str, key: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError(f"ingress.{key} must be one line")
    return value


def validate(block: Any) -> Dict[str, Any]:
    """Validate the spec's ``ingress:`` block and return the resolved parameters."""
    if not isinstance(block, dict):
        raise ValueError("ingress must be a mapping")
    unknown = set(block) - _KEYS
    if unknown:
        raise ValueError(f"ingress: unknown key(s) {sorted(unknown)}")
    if block.get("replace_routes", True) is not True:
        raise ValueError("ingress.replace_routes: false is refused — under ingress the static webhook"
                         " routes are always replaced, so no route can carry a platform secret")
    route = str(block.get("route", "ingress"))
    if not _SAFE.match(route):
        raise ValueError(f"ingress.route {route!r} is not a safe route name")
    port = block.get("port", 8644)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("ingress.port must be an integer in 1..65535")
    platforms = block.get("platforms")
    if not isinstance(platforms, dict) or not platforms:
        raise ValueError("ingress.platforms must name at least one platform")
    resolved_platforms: Dict[str, Dict[str, str]] = {}
    for name, entry in platforms.items():
        if name not in PLATFORMS:
            raise ValueError(f"ingress.platforms: unsupported platform {name!r}")
        if not isinstance(entry, dict) or set(entry) != {"secret_file"}:
            raise ValueError(f"ingress.platforms.{name} must carry exactly one key, secret_file")
        path = entry["secret_file"]
        if not isinstance(path, str) or not PurePosixPath(path).is_absolute():
            raise ValueError(f"ingress.platforms.{name}.secret_file must be an absolute host path")
        resolved_platforms[name] = {"secret_file": _no_newline(path, f"platforms.{name}.secret_file")}
    gateway_sandbox = str(block.get("gateway_sandbox") or "")
    if not _SAFE.match(gateway_sandbox):
        raise ValueError("ingress.gateway_sandbox must name the gateway sandbox")
    gateway_start = _str_list(block.get("gateway_start"), "gateway_start", nonempty=True)
    budget = block.get("per_pr_hourly_budget", 30)
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
        raise ValueError("ingress.per_pr_hourly_budget must be a positive integer")
    try:
        # The edge parses the same block at start-up; one parser keeps the two from drifting.
        scope = _sibling("edge/scope.py", "_nv_ingress_scope_render").parse(block.get("scope"))
    except ValueError as exc:
        raise ValueError(f"ingress.{exc}") from exc
    params = {
        "route": route,
        "port": port,
        "platforms": resolved_platforms,
        "issue_labels": _str_list(block.get("issue_labels"), "issue_labels", nonempty=True),
        "self_logins": _str_list(block.get("self_logins"), "self_logins", nonempty=False),
        "per_pr_hourly_budget": budget,
        "gateway_sandbox": gateway_sandbox,
        "gateway_start": gateway_start,
        "scope": scope,
    }
    for key, default in (("host_checkout", "%h/hermes-agent"), ("host_python", "/usr/bin/python3"),
                         ("host_dir", "%h/.config/nv-ingress"), ("openshell_bin", "openshell"),
                         ("edge_listen", "127.0.0.1:8650"),
                         ("edge_state_dir", "~/.local/state/nv-ingress")):
        value = block.get(key, default)
        if not isinstance(value, str) or not value.strip() or " " in value:
            raise ValueError(f"ingress.{key} must be a non-empty string without spaces")
        params[key] = _no_newline(value.strip(), key)
    return params


def route_block(params: Dict[str, Any]) -> Dict[str, Any]:
    """The one in-sandbox ingress route: loopback-gated no-auth, the transport event only."""
    return {"secret": NO_AUTH, "script": ROUTE_SCRIPT, "events": [_envelope().TRANSPORT_EVENT],
            "profile": "default"}


def plugin_settings(params: Dict[str, Any], orchestrator_profile: str) -> Dict[str, Any]:
    return {
        "route": params["route"],
        "orchestrator_profile": orchestrator_profile,
        "issue_labels": list(params["issue_labels"]),
        "self_logins": list(params["self_logins"]),
        "per_pr_hourly_budget": params["per_pr_hourly_budget"],
        "webhook_guard": True,
    }


def edge_config(params: Dict[str, Any]) -> Dict[str, Any]:
    cfg = {
        "listen": params["edge_listen"],
        "forward_url": f"http://{LOOPBACK}:{params['port']}/webhooks/{params['route']}",
        "spool_dir": params["edge_state_dir"].rstrip("/") + "/spool",
        "events": routed_events(),
        "platforms": {name: dict(entry) for name, entry in sorted(params["platforms"].items())},
        "gateway": {
            "sandbox": params["gateway_sandbox"],
            "port": params["port"],
            "openshell_bin": params["openshell_bin"],
            "start": list(params["gateway_start"]),
            "lock_file": params["edge_state_dir"].rstrip("/") + "/gateway-boot.lock",
        },
    }
    if params.get("scope"):
        cfg["scope"] = dict(params["scope"])
    return cfg


def _unit(description: str, exec_start: str, *, env: str = "", after: str = "") -> str:
    lines = ["[Unit]", f"Description={description}"]
    if after:
        lines.append(f"After={after}")
    lines += ["", "[Service]", "Type=simple"]
    if env:
        lines.append(f"Environment={env}")
    lines += [f"ExecStart={exec_start}", "Restart=always", "RestartSec=5", "",
              "[Install]", "WantedBy=default.target", ""]
    return "\n".join(lines)


def unit_files(params: Dict[str, Any]) -> Dict[str, str]:
    pythonpath = f"PYTHONPATH={params['host_checkout'].rstrip('/')}/plugins/nv-ingress"
    config = f"{params['host_dir'].rstrip('/')}/edge.yaml"
    port = params["port"]
    return {
        "nv-ingress-edge.service": _unit(
            "nv-ingress host edge receiver", f"{params['host_python']} -m edge --config {config}",
            env=pythonpath, after="network-online.target"),
        "nv-ingress-forward.service": _unit(
            "nv-ingress loopback forward into the gateway sandbox",
            f"{params['openshell_bin']} forward service {params['gateway_sandbox']}"
            f" --target-host {LOOPBACK} --target-port {port} --local {LOOPBACK}:{port}"),
        "nv-ingress-gateway-boot.service": _unit(
            "nv-ingress gateway-boot supervisor",
            f"{params['host_python']} -m edge.gateway_boot --config {config}", env=pythonpath),
    }


def render_host(params: Dict[str, Any], out_root: Path) -> None:
    """Write ``host/`` (three user units + ``edge.yaml``) and ``hooks/github-events.json``."""
    host = Path(out_root) / "host"
    hooks = Path(out_root) / "hooks"
    host.mkdir(parents=True, exist_ok=True)
    hooks.mkdir(parents=True, exist_ok=True)
    for name, body in unit_files(params).items():
        (host / name).write_text(body, encoding="utf-8")
    # JSON is valid YAML, so the stdlib-only edge reads this file without PyYAML.
    (host / "edge.yaml").write_text(json.dumps(edge_config(params), indent=2, sort_keys=True) + "\n",
                                    encoding="utf-8")
    (hooks / "github-events.json").write_text(json.dumps(routed_events(), indent=2) + "\n",
                                              encoding="utf-8")
