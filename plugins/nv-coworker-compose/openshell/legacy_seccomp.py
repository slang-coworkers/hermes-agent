"""5f: no TCP servers inside OpenShell 0.1 worker workloads (legacy seccomp mode).

A host kernel without seccomp ``WAIT_KILLABLE_RECV`` returns EINVAL for that user-notif
``WAIT_KILLABLE_RECV`` flag. OpenShell 0.1.2 then falls back to a listener with write-backs
disabled (refs/OpenShell@v0.1.2
crates/openshell-isolation-interface/src/linux/seccomp_notify.rs:410-426). In that LEGACY
READ-ONLY mode every mediated syscall that must write an address back fails with EOPNOTSUPP:
``getpeername`` (crates/openshell-sandbox/src/network_broker.rs:1556-1580, 1736-1744),
``accept``/``accept4`` with an address buffer, and destination-bearing ``sendmmsg``.

So a TCP server (``accept`` with an address argument) cannot run inside a worker workload.
``hermes serve``, the api_server and webhook platforms, and HTTP/SSE MCP servers belong ONLY in
the hermes-gateway-agent pod, which is a plain Deployment outside OpenShell. Workers use stdio or
unix sockets.

This module is stdlib-only and pure. compose calls ``assert_no_tcp_listeners`` on every worker
profile of an ``openshell_api: "0.1"`` fleet. The installer and compose call
``assert_command_not_a_tcp_server`` on the sandbox main command (the trailing
``sandbox create -- <cmd>``).

Scope, stated honestly: in the shipped ssh topology every profile's Hermes process (and so its
profile config) runs in the hermes-gateway-agent pod; workers are only ssh targets. The
profile-config check is therefore DEFENCE IN DEPTH for a future in-sandbox Hermes, not a guard on
what runs in the workload today. What does run in the workload is (a) the sandbox main command,
which IS guarded here, and (b) whatever the agent launches through the terminal tool, which no
render-time guard can see. (b) is covered by a deployment probe and by
the worker image shipping no server binaries.
"""

from __future__ import annotations

import ipaddress
import os
import shlex
from typing import Any, Iterable, Sequence
from urllib.parse import urlsplit

# Platforms that bind a listener. This mirrors gateway/config.py PORT_BINDING_PLATFORM_VALUES
# without importing it (kept in sync by tests/plugins/test_ast5_legacy_seccomp.py). It is
# deliberately stricter than the core predicate: here feishu counts in any mode, because the
# guard refuses whatever MIGHT bind.
LISTENER_PLATFORMS = frozenset({
    "webhook", "api_server", "msgraph_webhook", "feishu", "wecom_callback", "bluebubbles",
    "sms", "whatsapp_cloud", "line", "teams",  # teams: a port-binder from Hermes 0.22
})
# Platform ``extra`` keys that signal a local listener on any platform.
LISTENER_EXTRA_KEYS = ("port", "listen_port", "bind", "bind_host", "listen", "host")
# MCP server launch flags that select a network transport (vs stdio).
_MCP_NETWORK_TRANSPORTS = ("http", "sse", "streamable-http", "streamable_http", "websocket", "ws")
_MCP_LISTEN_FLAGS = ("--port", "--host", "--listen", "--bind")
_LOOPBACK_NAMES = ("localhost", "localhost.localdomain", "ip6-localhost")

# Main-process commands that start a TCP server. ``hermes`` subcommands are matched on argv[1].
_HERMES_SERVER_SUBCOMMANDS = ("serve", "gateway", "dashboard", "api", "web", "acp-server")
_SERVER_PROGRAMS = ("uvicorn", "gunicorn", "hypercorn", "daphne", "sshd", "nginx", "httpd",
                    "caddy", "node-red", "jupyter", "jupyter-lab", "jupyter-notebook",
                    "code-server", "nc", "ncat", "netcat")
_PYTHON_SERVER_MODULES = ("http.server", "SimpleHTTPServer", "uvicorn", "gunicorn", "aiohttp.web",
                          "flask", "smtpd", "jupyterlab", "notebook")


class LegacySeccompError(ValueError):
    """A worker-workload render or command would open a TCP listener (refused)."""


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _is_local_host(host: str) -> bool:
    host = (host or "").strip().strip("[]").lower()
    if host in _LOOPBACK_NAMES or host in ("0.0.0.0", "::", ""):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_unspecified


def tcp_listener_findings(config: dict[str, Any]) -> list[str]:
    """Every config feature in one rendered profile that would open a TCP listener inside the
    workload. Returns an empty list when the profile is safe."""
    findings: list[str] = []
    platforms = config.get("platforms")
    if isinstance(platforms, dict):
        for name, block in sorted(platforms.items()):
            if not isinstance(block, dict) or not _truthy(block.get("enabled")):
                continue
            extra = block.get("extra") if isinstance(block.get("extra"), dict) else {}
            if name in LISTENER_PLATFORMS:
                findings.append(f"platforms.{name} is enabled (binds a TCP listener)")
                continue
            keys = [k for k in LISTENER_EXTRA_KEYS if k in extra or k in block]
            if keys:
                findings.append(f"platforms.{name} declares listener settings {keys}")
    for key in ("api_server", "webhook"):
        block = config.get(key)
        if isinstance(block, dict) and _truthy(block.get("enabled")):
            findings.append(f"{key}.enabled is true (binds a TCP listener)")
    dashboard = config.get("dashboard")
    if isinstance(dashboard, dict) and (_truthy(dashboard.get("enabled"))
                                        or dashboard.get("port") or dashboard.get("host")):
        findings.append("dashboard is configured (hermes serve binds a TCP listener)")
    servers = config.get("mcp_servers")
    if isinstance(servers, dict):
        for name, stanza in sorted(servers.items()):
            if not isinstance(stanza, dict):
                continue
            url = stanza.get("url")
            if isinstance(url, str) and url.strip():
                parts = urlsplit(url.strip())
                if _is_local_host(parts.hostname or ""):
                    findings.append(f"mcp_servers.{name} targets a local HTTP MCP server at {url!r} "
                                    f"(it would have to listen inside the workload)")
            command = stanza.get("command")
            args = stanza.get("args") if isinstance(stanza.get("args"), list) else []
            if isinstance(command, str) and command.strip():
                argv = [command, *[str(a) for a in args]]
                reason = _mcp_network_transport(argv)
                if reason:
                    findings.append(f"mcp_servers.{name} launches a network MCP server ({reason}); "
                                    f"use the stdio transport in worker workloads")
    return findings


def _mcp_network_transport(argv: Sequence[str]) -> str:
    for i, arg in enumerate(argv):
        low = arg.lower()
        for flag in ("--transport", "-t"):
            if low == flag and i + 1 < len(argv) and argv[i + 1].lower() in _MCP_NETWORK_TRANSPORTS:
                return f"{arg} {argv[i + 1]}"
            if low.startswith(flag + "=") and low.split("=", 1)[1] in _MCP_NETWORK_TRANSPORTS:
                return arg
        if low in _MCP_NETWORK_TRANSPORTS and i > 0 and argv[i - 1].lower() not in ("--transport", "-t"):
            # e.g. `mcp-server sse` / `fastmcp run app.py http`
            return arg
        if any(low == f or low.startswith(f + "=") for f in _MCP_LISTEN_FLAGS):
            return arg
    return ""


def assert_no_tcp_listeners(config: dict[str, Any], profile: str) -> None:
    """Refuse (``LegacySeccompError``) a worker profile render that would open a TCP listener."""
    findings = tcp_listener_findings(config)
    if findings:
        raise LegacySeccompError(
            f"coworker {profile!r} runs as an OpenShell 0.1 worker workload, where TCP servers "
            f"fail (legacy seccomp read-only mode: accept/getpeername return EOPNOTSUPP). "
            f"Move these to the hermes-gateway-agent pod or use stdio/unix sockets: "
            + "; ".join(findings))


def command_tcp_server_reason(argv: Sequence[str]) -> str:
    """Why ``argv`` (a sandbox main command) starts a TCP server, or "" when it does not."""
    words = [str(a) for a in argv]
    if not words:
        return ""
    # `sh -c "<script>"` / `bash -lc "<script>"`: inspect the script's words too.
    prog = os.path.basename(words[0])
    if prog in ("sh", "bash", "dash", "zsh") and len(words) >= 3 and words[1].startswith("-") \
            and "c" in words[1]:
        try:
            inner = shlex.split(words[2])
        except ValueError:
            inner = words[2].split()
        segments: list[list[str]] = [[]]
        for tok in inner:
            if tok in ("&&", "||", ";", "|", "&"):
                segments.append([])
            else:
                segments[-1].append(tok)
        for seg in segments:
            if seg and seg[0] == "exec":
                seg = seg[1:]
            reason = command_tcp_server_reason(seg)
            if reason:
                return reason
        return ""
    if prog in ("env", "nohup", "setsid", "exec"):
        rest = [w for w in words[1:] if "=" not in w or w.startswith("-")]
        rest = [w for w in rest if not w.startswith("-")]
        return command_tcp_server_reason(rest)
    if prog in ("hermes", "hermes-agent") and len(words) > 1:
        sub = [w for w in words[1:] if not w.startswith("-")]
        # skip a `-p <profile>` value
        if "-p" in words or "--profile" in words:
            for flag in ("-p", "--profile"):
                if flag in words:
                    idx = words.index(flag)
                    if idx + 1 < len(words) and words[idx + 1] in sub:
                        sub.remove(words[idx + 1])
        if sub and sub[0] in _HERMES_SERVER_SUBCOMMANDS:
            return f"`hermes {sub[0]}` binds a TCP listener"
    if prog in _SERVER_PROGRAMS:
        if prog in ("nc", "ncat", "netcat") and not any(w in ("-l", "--listen") or
                                                         (w.startswith("-") and "l" in w[1:])
                                                         for w in words[1:]):
            return ""
        return f"`{prog}` is a server"
    if prog.startswith("python") and "-m" in words:
        idx = words.index("-m")
        if idx + 1 < len(words) and words[idx + 1] in _PYTHON_SERVER_MODULES:
            return f"`python -m {words[idx + 1]}` is a server"
    if prog == "socat" and any("LISTEN" in w.upper() for w in words[1:]):
        return "`socat ...-LISTEN` is a server"
    return ""


def assert_command_not_a_tcp_server(argv: Iterable[str], where: str = "sandbox main command") -> None:
    reason = command_tcp_server_reason(list(argv))
    if reason:
        raise LegacySeccompError(
            f"{where} would start a TCP server ({reason}). Under the legacy seccomp mode "
            f"accept() with an address fails with EOPNOTSUPP inside OpenShell 0.1 workloads; use a "
            f"non-listening long-running command (e.g. `sleep infinity`)")
