// AC-OSH-F64-4 browser-open log: agent-browser `--init-script`, read back with `agent-browser console`.
// Only /api/pty sockets are counted: the dashboard also opens /api/ws and /api/events, and only /api/pty
// logs `pty accepted` (hermes_cli/web_server.py:17460). Query values that carry a credential are redacted
// before anything is logged. Lines (epoch seconds):
//   OSHWS ready <ts> <page path>                    once per document load, so a zero-open window is observable
//   OSHWS created <ts> <redacted url>               a /api/pty socket was constructed
//   OSHWS open <ts> <redacted url>                  its handshake completed
//   OSHWS url <ts> <page path> resume=<value|->     the page location at that open (the SPA rewrite evidence)
(() => {
  const Orig = window.WebSocket;
  const SECRET = ["token", "attach", "ticket", "internal"];
  const now = () => (Date.now() / 1000).toFixed(3);
  const redact = (u) => {
    try {
      const x = new URL(u, location.href);
      for (const k of SECRET) if (x.searchParams.has(k)) x.searchParams.set(k, "REDACTED");
      return x;
    } catch (e) {
      return null;
    }
  };
  const page = () => {
    const r = new URLSearchParams(location.search).get("resume");
    return location.pathname + " resume=" + (r || "-");
  };
  console.info("OSHWS ready " + now() + " " + location.pathname);
  function W(url, protocols) {
    const ws = protocols === undefined ? new Orig(url) : new Orig(url, protocols);
    const x = redact(String(url));
    if (x && x.pathname.endsWith("/api/pty")) {
      console.info("OSHWS created " + now() + " " + x.toString());
      ws.addEventListener("open", () => {
        console.info("OSHWS open " + now() + " " + x.toString());
        console.info("OSHWS url " + now() + " " + page());
      });
    }
    return ws;
  }
  W.prototype = Orig.prototype;
  W.CONNECTING = 0;
  W.OPEN = 1;
  W.CLOSING = 2;
  W.CLOSED = 3;
  window.WebSocket = W;
})();
