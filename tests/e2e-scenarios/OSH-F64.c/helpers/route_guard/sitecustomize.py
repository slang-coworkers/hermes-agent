"""AC-OSH-F64-4 G3 route guard: a fail-closed check on every httpx request a fleet process sends.

Loaded as `sitecustomize` from PYTHONPATH at every fleet start. It does nothing unless
OSH_F64C_ROUTE_GUARD_DIR is set. When it is set:

- at import, writes <dir>/<pid>.armed;
- wraps httpx.Client.send and httpx.AsyncClient.send. Before a process's first POST it sends one
  `GET https://inference.local/v1/models` through the same client, carrying only the served
  config's placeholder bearer (recorded as kind "probe");
- rejects, before dispatch, any POST whose URL is not exactly ROUTE, whose bearer does not equal
  providers.compatible-endpoint.api_key in $HERMES_HOME/config.yaml, or that comes before a probe
  that returned 200. Non-POST requests pass;
- appends one JSON line per decision to <dir>/<pid>.jsonl; a dispatched request carries "sent", its
  dispatch time, which is what the router logs against. Header values are never written.
"""
import json
import os
import threading
import time

ROUTE = "https://inference.local/v1/chat/completions"
PROBE = "https://inference.local/v1/models"
_DIR = os.environ.get("OSH_F64C_ROUTE_GUARD_DIR")
_lock = threading.Lock()
_probed = {}  # pid -> probe status; keyed by pid so a forked child probes again


def _netns():
    try:
        return os.readlink("/proc/self/ns/net")
    except OSError:
        return "unreadable"


def _home():
    return os.environ.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")


def _record(entry):
    entry.update(ts=time.time(), pid=os.getpid(), ppid=os.getppid(), netns=_netns(),
                 home=os.path.basename(_home().rstrip("/")))
    try:
        with open(os.path.join(_DIR, "%d.jsonl" % os.getpid()), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
        return True
    except OSError:
        return False


def _placeholder():
    try:
        import yaml
        with open(os.path.join(_home(), "config.yaml"), encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        key = ((cfg.get("providers") or {}).get("compatible-endpoint") or {}).get("api_key")
        return key if isinstance(key, str) and key else None
    except Exception:
        return None


def _target(request):
    url = request.url
    port = "" if url.port in (None, 443) else ":%d" % url.port
    return "%s://%s%s%s%s" % (url.scheme, url.host, port, url.path, "?q" if url.query else "")


def _arm():
    import httpx

    class RouteGuardRejected(httpx.TransportError):
        pass

    orig_send = httpx.Client.send
    orig_async_send = httpx.AsyncClient.send

    def _check(request, key):
        if request.url.scheme != "https" or _target(request) != ROUTE:
            return "url"
        if key is None:
            return "config"
        if request.headers.get("authorization") != "Bearer " + key:
            return "bearer"
        return None

    def _reject(request, kind, reason, target):
        _record({"kind": kind, "method": request.method, "target": target,
                 "decision": "rejected", "reason": reason, "status": None})
        raise RouteGuardRejected("route guard rejected %s (%s)" % (request.method, reason), request=request)

    def _done(request, kind, target, sent, response=None, error=None):
        status = response.status_code if response is not None else "error:" + type(error).__name__
        return _record({"kind": kind, "method": request.method, "target": target, "sent": sent,
                        "decision": "allowed", "status": status})

    def _probe_request(client, key):
        return client.build_request("GET", PROBE, headers={"Authorization": "Bearer " + key})

    def send(self, request, **kwargs):
        # Taken before dispatch: a transport may rewrite request.url in place.
        target = request.url.host + request.url.path
        if request.method != "POST":
            sent = time.time()
            response = orig_send(self, request, **kwargs)
            _done(request, "other", target, sent, response)
            return response
        key = _placeholder()
        reason = _check(request, key)
        if reason:
            _reject(request, "post", reason, target)
        with _lock:
            if os.getpid() not in _probed:
                probe = _probe_request(self, key)
                sent = time.time()
                try:
                    resp = orig_send(self, probe)
                    resp.close()
                    _probed[os.getpid()] = resp.status_code if _done(probe, "probe", "inference.local/v1/models", sent, resp) else "unrecorded"
                except httpx.HTTPError as exc:
                    _done(probe, "probe", "inference.local/v1/models", sent, error=exc)
                    _probed[os.getpid()] = "error"
        if _probed[os.getpid()] != 200:
            _reject(request, "post", "no_probe_200", target)
        sent = time.time()
        try:
            response = orig_send(self, request, **kwargs)
        except httpx.HTTPError as exc:
            _done(request, "post", target, sent, error=exc)
            raise
        if not _done(request, "post", target, sent, response):
            response.close()
            _reject(request, "post", "unrecorded", target)
        return response

    async def async_send(self, request, **kwargs):
        # Taken before dispatch: a transport may rewrite request.url in place.
        target = request.url.host + request.url.path
        if request.method != "POST":
            sent = time.time()
            response = await orig_async_send(self, request, **kwargs)
            _done(request, "other", target, sent, response)
            return response
        key = _placeholder()
        reason = _check(request, key)
        if reason:
            _reject(request, "post", reason, target)
        if os.getpid() not in _probed:
            probe = _probe_request(self, key)
            sent = time.time()
            try:
                resp = await orig_async_send(self, probe)
                await resp.aclose()
                _probed[os.getpid()] = resp.status_code if _done(probe, "probe", "inference.local/v1/models", sent, resp) else "unrecorded"
            except httpx.HTTPError as exc:
                _done(probe, "probe", "inference.local/v1/models", sent, error=exc)
                _probed[os.getpid()] = "error"
        if _probed[os.getpid()] != 200:
            _reject(request, "post", "no_probe_200", target)
        sent = time.time()
        try:
            response = await orig_async_send(self, request, **kwargs)
        except httpx.HTTPError as exc:
            _done(request, "post", target, sent, error=exc)
            raise
        if not _done(request, "post", target, sent, response):
            await response.aclose()
            _reject(request, "post", "unrecorded", target)
        return response

    httpx.Client.send = send
    httpx.AsyncClient.send = async_send


if _DIR:
    try:
        os.makedirs(_DIR, exist_ok=True)
        with open(os.path.join(_DIR, "%d.armed" % os.getpid()), "w", encoding="utf-8") as fh:
            json.dump({"pid": os.getpid(), "ppid": os.getppid(), "netns": _netns(),
                       "home": os.path.basename(_home().rstrip("/"))}, fh)
        _arm()
    except ImportError:
        pass  # no httpx in this interpreter: it can send no httpx request to guard
