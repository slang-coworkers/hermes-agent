"""Host edge receiver: verify, normalize, spool, forward (D2).

Runs on the host as ``python3 -m edge --config <edge.yaml>`` with
``PYTHONPATH=<checkout>/plugins/nv-ingress``. It is stdlib only and imports no
Hermes code. It holds the platform secrets (read from host files, never from
the render) and is the only signer the in-sandbox route trusts. Anything that
fails verification is answered 401 and never reaches the spool or the forward.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("nv_ingress.edge")

# Header names as the release adapter reads them (gateway/platforms/webhook.py:753, :847, :1121).
GITHUB_SIGNATURE_HEADER = "X-Hub-Signature-256"
# GitLab signing-token headers (GitLab webhook docs, §Delivery headers / §Signing tokens).
GITLAB_TIMESTAMP_HEADER = "webhook-timestamp"
GITLAB_SIGNATURE_HEADER = "webhook-signature"
REQUEST_ID_HEADER = "X-Request-ID"  # the release adapter's dedupe fallback (webhook.py:845-851)

MAX_BODY_BYTES = 1_048_576  # the release adapter's default cap
REPLAY_WINDOW_SECONDS = 300
BACKOFF_CAP_SECONDS = 60.0
DEAD_LETTER_AFTER_SECONDS = 24 * 3600
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _load_envelope():
    path = Path(__file__).resolve().parent.parent / "envelope.py"
    spec = importlib.util.spec_from_file_location("_nv_ingress_edge_envelope", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


envelope = _load_envelope()
# The forward target is host loopback; an inherited proxy env must never reroute it.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def load_secrets(platforms: Dict[str, Dict[str, str]]) -> Dict[str, bytes]:
    """Signing keys per platform, read from the host files the config names."""
    keys: Dict[str, bytes] = {}
    for name, entry in (platforms or {}).items():
        raw = Path(os.path.expanduser(entry["secret_file"])).read_bytes()
        if name == "gitlab":
            # The signing token is ``whsec_`` + base64 key material (Standard Webhooks).
            token = raw.strip().decode("ascii")
            keys[name] = base64.b64decode(token[len("whsec_"):] if token.startswith("whsec_") else token)
        else:
            keys[name] = raw
    return keys


def verify_github(key: bytes, headers: Any, body: bytes) -> bool:
    provided = envelope.header(headers, GITHUB_SIGNATURE_HEADER)
    if not provided:
        return False
    expected = "sha256=" + hmac.new(key, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided.encode(), expected.encode())


def verify_gitlab(key: bytes, headers: Any, body: bytes, now: Optional[float] = None) -> bool:
    msg_id = envelope.header(headers, envelope.GITLAB_ID_HEADER)
    stamp = envelope.header(headers, GITLAB_TIMESTAMP_HEADER)
    provided = envelope.header(headers, GITLAB_SIGNATURE_HEADER)
    if not (msg_id and stamp and provided):
        return False
    try:
        ts = int(stamp)
    except ValueError:
        return False
    if abs((time.time() if now is None else now) - ts) > REPLAY_WINDOW_SECONDS:
        return False
    signed = msg_id.encode() + b"." + stamp.encode() + b"." + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    # The header is a space-separated list of ``<version>,<signature>`` entries.
    for entry in provided.split():
        version, _, sig = entry.partition(",")
        if version == "v1" and hmac.compare_digest(sig.encode(), expected.encode()):
            return True
    return False


def spool_name(source: str, delivery_id: str) -> str:
    ident = delivery_id if _SAFE_ID.match(delivery_id) else hashlib.sha256(delivery_id.encode()).hexdigest()
    return f"{source}-{ident}.json"


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class Edge:
    """The receiver state shared by the HTTP handler and the forwarder thread."""

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.events = frozenset(cfg.get("events") or envelope.ROUTED_EVENTS)
        self.forward_url = cfg["forward_url"]
        self.spool = Path(os.path.expanduser(cfg["spool_dir"]))
        self.spool.mkdir(parents=True, exist_ok=True)
        self.dead_letter = self.spool / "dead-letter"
        self.keys: Dict[str, bytes] = {}
        self.reload()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._next_attempt: Dict[str, Tuple[float, int]] = {}
        self._forwarder = threading.Thread(target=self._forward_loop, name="nv-ingress-forward",
                                           daemon=True)

    def reload(self) -> None:
        self.keys = load_secrets(self.cfg.get("platforms") or {})

    # -- receive -----------------------------------------------------------

    def receive(self, platform: str, headers: Any, body: bytes) -> Tuple[int, Dict[str, Any]]:
        key = self.keys.get(platform)
        if key is None:
            return 404, {"error": "unknown platform"}
        ok = verify_gitlab(key, headers, body) if platform == "gitlab" else verify_github(key, headers, body)
        if not ok:
            logger.warning("edge: rejected unsigned or badly signed %s request", platform)
            return 401, {"error": "invalid signature"}
        event_header = envelope.GITLAB_EVENT_HEADER if platform == "gitlab" else envelope.GITHUB_EVENT_HEADER
        event = envelope.header(headers, event_header)
        try:
            payload = json.loads(body)
        except ValueError:
            return 400, {"error": "body is not JSON"}
        env = envelope.normalize(platform, event, headers, payload)
        if not env.get("delivery_id") or not event:
            return 400, {"error": "missing delivery id or event"}
        if env["event"] not in self.events:
            return 202, {"status": "ignored", "event": env["event"]}
        entry = {"spooled_at": time.time(), "envelope": env}
        _atomic_write(self.spool / spool_name(platform, env["delivery_id"]),
                      json.dumps(entry, sort_keys=True).encode())
        logger.info("edge: spooled %s %s delivery %s", platform, env["event"], env["delivery_id"])
        self._wake.set()
        return 202, {"status": "accepted", "delivery_id": env["delivery_id"]}

    # -- forward -----------------------------------------------------------

    def _post(self, env: Dict[str, Any]) -> bool:
        body = json.dumps({**env, "event_type": envelope.TRANSPORT_EVENT}).encode()
        req = urllib.request.Request(self.forward_url, data=body, method="POST", headers={
            "Content-Type": "application/json", REQUEST_ID_HEADER: str(env["delivery_id"])})
        try:
            with _DIRECT.open(req, timeout=30) as resp:
                status, raw = resp.status, resp.read()
        except (urllib.error.URLError, OSError):
            return False
        try:
            outcome = json.loads(raw or b"{}").get("status")
        except ValueError:
            outcome = None
        acked = (status, outcome) in ((202, "accepted"), (200, "duplicate"))
        if acked:
            logger.info("edge: forward of delivery %s acknowledged as %s", env["delivery_id"], outcome)
        return acked

    def _forward_one(self, path: Path, now: float) -> None:
        due, attempts = self._next_attempt.get(path.name, (0.0, 0))
        if due > now:
            return
        try:
            entry = json.loads(path.read_bytes())
        except (OSError, ValueError):
            return
        if self._post(entry["envelope"]):
            path.unlink(missing_ok=True)
            self._next_attempt.pop(path.name, None)
            return
        if now - float(entry.get("spooled_at") or now) > DEAD_LETTER_AFTER_SECONDS:
            self.dead_letter.mkdir(exist_ok=True)
            os.replace(path, self.dead_letter / path.name)
            self._next_attempt.pop(path.name, None)
            logger.error("edge: delivery %s moved to dead-letter after 24 h of failed forwards",
                         entry["envelope"].get("delivery_id"))
            return
        self._next_attempt[path.name] = (now + min(BACKOFF_CAP_SECONDS, 2.0 ** attempts), attempts + 1)

    def _pending(self):
        stamped = []
        for path in self.spool.glob("*.json"):
            try:
                stamped.append((path.stat().st_mtime, path))
            except FileNotFoundError:
                continue
        return [path for _mtime, path in sorted(stamped)]

    def _forward_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            for path in self._pending():
                if self._stop.is_set():
                    return
                try:
                    self._forward_one(path, time.time())
                except Exception:
                    logger.warning("edge: forward attempt failed", exc_info=True)
            self._wake.wait(1.0)

    def start(self) -> None:
        self._forwarder.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()


class _Handler(BaseHTTPRequestHandler):
    server_version = "nv-ingress-edge"

    def log_message(self, fmt, *args):
        # http.server writes access lines to stderr; keep them in the edge logger instead.
        logger.debug("edge: " + fmt, *args)

    def _reply(self, code: int, payload: Dict[str, Any]) -> None:
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        platform = {"/github": "github", "/gitlab": "gitlab"}.get(self.path.split("?", 1)[0])
        if platform is None:
            return self._reply(404, {"error": "unknown path"})
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            return self._reply(411, {"error": "length required"})
        if length < 0 or length > MAX_BODY_BYTES:
            return self._reply(413, {"error": "payload too large"})
        body = self.rfile.read(length)
        code, payload = self.server.edge.receive(platform, self.headers, body)
        self._reply(code, payload)

    def do_GET(self):
        if self.path == "/health":
            return self._reply(200, {"status": "ok"})
        self._reply(404, {"error": "unknown path"})


class EdgeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, edge: Edge):
        super().__init__(address, _Handler)
        self.edge = edge

    def shutdown(self) -> None:
        self.edge.stop()
        super().shutdown()


def make_server(cfg: Dict[str, Any]) -> Tuple[EdgeServer, threading.Thread]:
    """Bind the receiver, start the forwarder, and serve on a daemon thread."""
    host, _, port = str(cfg["listen"]).rpartition(":")
    edge = Edge(cfg)
    server = EdgeServer((host or "127.0.0.1", int(port)), edge)
    edge.start()
    thread = threading.Thread(target=server.serve_forever, name="nv-ingress-edge", daemon=True)
    thread.start()
    return server, thread
