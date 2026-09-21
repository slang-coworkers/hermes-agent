/**
 * nv-cost-cap — Dashboard UI plugin (COST-F30).
 *
 * A shell-wide (`header-banner`) escalation card that renders whenever the ACTIVE
 * profile has a pending cost-escalation episode, regardless of which page/session the
 * operator is on. Continue / Stop / set-exact-ceiling POST to the plugin's own
 * auth-gated backend at /api/plugins/nv-cost-cap/; the backend derives the operator
 * principal from the server session and applies the resolution exactly once.
 *
 * Plain IIFE, no build step. `fetchJSON` does NOT auto-scope /api/plugins/*, so the
 * current profile is derived here and appended explicitly to every call.
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const PLUGINS = window.__HERMES_PLUGINS__;
  if (!SDK || !PLUGINS) return;

  const { React } = SDK;
  const h = React.createElement;
  const { useState, useEffect, useCallback } = SDK.hooks;
  const API = "/api/plugins/nv-cost-cap";

  async function currentProfile() {
    const q = new URLSearchParams(window.location.search).get("profile");
    if (q) return q;
    try {
      const info = await SDK.api.getActiveProfile();
      return (info && (info.active || info.current)) || "default";
    } catch (e) {
      return "default";
    }
  }

  function EscalationCard() {
    const [items, setItems] = useState([]);
    const [busy, setBusy] = useState({});
    const [amounts, setAmounts] = useState({});

    const refresh = useCallback(async () => {
      try {
        const profile = await currentProfile();
        const rows = await SDK.fetchJSON(`${API}/escalations?profile=${encodeURIComponent(profile)}`);
        setItems(Array.isArray(rows) ? rows : []);
      } catch (e) {
        // transient (auth/profile switch) — keep the last view, retry on the next tick
      }
    }, []);

    useEffect(() => {
      refresh();
      const t = setInterval(refresh, 5000);
      return () => clearInterval(t);
    }, [refresh]);

    const resolve = useCallback(async (episodeId, decision, amountUsd) => {
      setBusy((b) => ({ ...b, [episodeId]: true }));
      try {
        const profile = await currentProfile();
        const body = { decision };
        if (amountUsd !== undefined && amountUsd !== null) body.amount_usd = amountUsd;
        await SDK.fetchJSON(
          `${API}/escalations/${encodeURIComponent(episodeId)}/resolve?profile=${encodeURIComponent(profile)}`,
          { method: "POST", body: JSON.stringify(body) }
        );
      } catch (e) {
        // a repeat resolve returns already-resolved; the refresh below reconciles the view
      } finally {
        setBusy((b) => ({ ...b, [episodeId]: false }));
        refresh();
      }
    }, [refresh]);

    if (!items.length) return null;

    return h(
      "div",
      { className: "w-full border-b border-(--ui-stroke-secondary) bg-(--ui-surface) px-4 py-2 text-sm", role: "region", "aria-label": "cost escalations" },
      items.map((it) => {
        const eid = it.episode_id;
        const immortal = !!it.immortal;
        return h(
          "div",
          { key: eid, className: "flex flex-wrap items-center gap-2 py-1" },
          h("span", { className: "font-medium" }, "Cost escalation"),
          h("span", { className: "text-(--ui-text-secondary)" },
            `session ${it.session} · spend $${Number(it.spend || 0).toFixed(2)}` +
            (it.ceiling != null ? ` · ceiling $${Number(it.ceiling).toFixed(2)}` : "") +
            (immortal ? " · immortal (continue-only)" : "")),
          h("button",
            { type: "button", disabled: !!busy[eid], onClick: () => resolve(eid, "continue"),
              className: "rounded bg-(--ui-accent) px-2 py-0.5 text-white disabled:opacity-50" },
            "Continue"),
          immortal ? null : h("button",
            { type: "button", disabled: !!busy[eid], onClick: () => resolve(eid, "stop"),
              className: "rounded border border-(--ui-stroke-secondary) px-2 py-0.5 disabled:opacity-50" },
            "Stop"),
          h("input",
            { type: "number", step: "0.01", min: "0", placeholder: "exact $",
              value: amounts[eid] || "", "aria-label": "exact ceiling in USD",
              onChange: (e) => setAmounts((a) => ({ ...a, [eid]: e.target.value })),
              className: "w-20 rounded border border-(--ui-stroke-secondary) px-1 py-0.5" }),
          h("button",
            { type: "button", disabled: !!busy[eid] || !amounts[eid],
              onClick: () => resolve(eid, "ceiling", parseFloat(amounts[eid])),
              className: "rounded border border-(--ui-stroke-secondary) px-2 py-0.5 disabled:opacity-50" },
            "Set ceiling")
        );
      })
    );
  }

  // The card is a shell-wide slot; register a hidden-tab placeholder too so a direct
  // hit on the tab path still resolves to a component (per the dashboard SDK guidance).
  PLUGINS.register("nv-cost-cap", EscalationCard);
  PLUGINS.registerSlot("nv-cost-cap", "header-banner", EscalationCard);
})();
