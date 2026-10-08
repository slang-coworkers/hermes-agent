(function () {
  "use strict";
  // nv-artifact dashboard plugin — fleet outcome analytics (funnel, win-rate,
  // cost-per-merge) read from the single fleet ledger via the orchestrator-only
  // /api/plugins/nv-artifact/outcomes endpoint.
  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const React = SDK.React;
  const h = React.createElement;
  const { useState, useEffect } = SDK.hooks;
  const C = SDK.components || {};

  function api(path) {
    // Host SDK fetchJSON handles auth (loopback token vs gated cookie).
    return SDK.fetchJSON("/api/plugins/nv-artifact" + path);
  }

  function Panel(props) {
    // Prefer the host Card primitives; fall back to plain divs so the page
    // still renders against an older host SDK.
    if (C.Card && C.CardHeader && C.CardTitle && C.CardContent) {
      return h(C.Card, { className: "nv-panel" },
        h(C.CardHeader, null, h(C.CardTitle, null, props.title)),
        h(C.CardContent, null, props.children));
    }
    return h("div", { className: "nv-panel" },
      h("h3", { className: "nv-panel-title" }, props.title),
      h("div", { className: "nv-panel-body" }, props.children));
  }

  function Funnel(funnel) {
    const order = ["merged", "closed", "abandoned", "blocked", "completed", "unknown"];
    const keys = Object.keys(funnel || {}).sort(function (a, b) {
      const ia = order.indexOf(a), ib = order.indexOf(b);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
    });
    if (keys.length === 0) return h("p", { className: "nv-empty" }, "No artifacts recorded yet.");
    return h("ul", { className: "nv-funnel" }, keys.map(function (k) {
      return h("li", { key: k, className: "nv-funnel-row", "data-outcome": k },
        h("span", { className: "nv-funnel-label" }, k),
        h("span", { className: "nv-funnel-count" }, String(funnel[k])));
    }));
  }

  function NvArtifactPage() {
    const [data, setData] = useState(null);
    const [error, setError] = useState(null);

    useEffect(function () {
      let alive = true;
      api("/outcomes").then(function (d) {
        if (alive) setData(d);
      }).catch(function (e) {
        if (alive) setError(String(e && e.message ? e.message : e));
      });
      return function () { alive = false; };
    }, []);

    if (error) return h("div", { className: "nv-artifact" }, h("p", { className: "nv-error" }, "Failed to load outcomes: " + error));
    if (!data) return h("div", { className: "nv-artifact" }, h("p", { className: "nv-loading" }, "Loading outcomes…"));
    if (data.authorized === false) {
      return h("div", { className: "nv-artifact" },
        h("p", { className: "nv-restricted" }, "Outcome analytics are available to the orchestrator profile only."));
    }

    const winPct = Math.round((data.winrate || 0) * 100);
    const cpm = (data.cost_per_merge || 0).toFixed(2);

    return h("div", { className: "nv-artifact" },
      h("h2", { className: "nv-heading" }, "Fleet artifact outcomes"),
      h("div", { className: "nv-grid" },
        h(Panel, { title: "Funnel" }, Funnel(data.funnel)),
        h(Panel, { title: "Win rate" },
          h("div", { className: "nv-metric", "data-metric": "winrate" }, winPct + "%"),
          h("div", { className: "nv-sub" }, String(data.merged || 0) + " merged / " + String(data.total || 0) + " total")),
        h(Panel, { title: "Cost per merge" },
          h("div", { className: "nv-metric", "data-metric": "cost-per-merge" }, cpm),
          h("div", { className: "nv-sub" }, "USD per merged artifact"))));
  }

  window.__HERMES_PLUGINS__.register("nv-artifact", NvArtifactPage);
})();
