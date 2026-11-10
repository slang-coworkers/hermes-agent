"""5f: getpeername() shim for OpenShell 0.1 workloads in legacy seccomp mode.

Reference/fallback implementation. A production worker image may ship its own shim (loaded by a
``.pth`` in every site-packages, marker ``_osh_legacy_getpeername_shim``). This module
is used only by the plugin-side ``Dockerfile.worker-0.1`` reference image; do not install both.
If that shim is already active this one detects its marker and does nothing, so the two never
double-wrap ``getpeername``. It is a no-op unless ``OSH_LEGACY_GETPEERNAME_SHIM=1``.

Why: on a host kernel without WAIT_KILLABLE_RECV, OpenShell 0.1.2 runs its seccomp listener in legacy read-only mode.
``getpeername()`` on every mediated outbound TCP socket then fails with EOPNOTSUPP (refs/OpenShell@v0.1.2
crates/openshell-sandbox/src/network_broker.rs:1556-1580, 1736-1744). CPython's synchronous
``ssl.SSLSocket._create`` calls ``getpeername()`` and re-raises any errno except ENOTCONN. So
every sync HTTPS client (urllib, requests, httpx sync, pip) fails with ``OSError(95)`` before the
TLS handshake. asyncio already tolerates it (selector_events wraps getpeername).

The shim catches EOPNOTSUPP only, for AF_INET/AF_INET6 sockets only. It returns the address the
socket was connect()ed to (when known), else the unspecified address. The socket IS connected,
so SSLSocket proceeds. Any other errno (including ENOTCONN) propagates unchanged.
This is inferred, not yet probed (5f). Upstream fix to
request: in legacy mode get_peer_name should respond_continue instead of failing.
"""

import errno as _errno
import os as _os
import socket as _socket
import weakref as _weakref

_ENV = "OSH_LEGACY_GETPEERNAME_SHIM"


def install(sock_cls=_socket.socket):
    """Wrap ``sock_cls.getpeername``/``connect``. Idempotent. Returns True when installed."""
    if getattr(sock_cls, "_osh_legacy_shim", False):
        return False
    # A production .pth shim already wraps getpeername: never stack.
    if getattr(sock_cls.getpeername, "_osh_legacy_getpeername_shim", False):
        return False
    peers = _weakref.WeakKeyDictionary()
    orig_getpeername = sock_cls.getpeername
    orig_connect = sock_cls.connect

    def connect(self, address):
        try:
            peers[self] = address
        except TypeError:
            pass
        return orig_connect(self, address)

    def getpeername(self):
        try:
            return orig_getpeername(self)
        except OSError as exc:
            if exc.errno != _errno.EOPNOTSUPP or self.family not in (_socket.AF_INET, _socket.AF_INET6):
                raise
            known = peers.get(self)
            if known is not None:
                return known
            return ("0.0.0.0", 0) if self.family == _socket.AF_INET else ("::", 0, 0, 0)

    sock_cls.connect = connect
    sock_cls.getpeername = getpeername
    sock_cls._osh_legacy_shim = True
    return True


if _os.environ.get(_ENV) == "1":
    install()
