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

  // One-line operator feedback for a resolution outcome that returns 2xx with granted:false
  // (unauthorized / deferred / already-resolved / …) — otherwise the click looks like a no-op.
  const OUTCOME_NOTE = {
    unauthorized: "Not authorized for this profile.",
    "already-resolved": "Already resolved.",
    "no-episode": "No pending escalation.",
    "immortal-continue-only": "Continue-only session.",
    "invalid-amount": "Enter a valid exact USD amount.",
    managed: "Ceiling is administrator-managed.",
    "stale-generation": "Superseded by a newer cost event.",
    "session-closed": "Session already closed.",
    deferred: "Retrying — check again shortly."
  };
  // Verbatim manual-resume notice surfaced when a granted resolution LEAVES an engaged ESTOP belt
  // (the plugin never unlinks it), so the card never collapses to a bare "Applied." that hides the
  // retained pause. Matches the backend _cost_outcome_text notice exactly.
  const MANUAL_RESUME_NOTICE = "Continue applied — the per-session cost block is cleared, but the profile ESTOP belt remains engaged (gates cron/kanban/new inbounds); resume via `hermes resume` or the UA-28 upstream lock";
  function outcomeNote(res) {
    if (!res || typeof res !== "object") return "Request failed.";
    if (!res.granted) return OUTCOME_NOTE[res.reason] || `Not applied (${res.reason}).`;
    // manual_resume_required means the profile ESTOP belt (which the plugin never lifts) is still
    // engaged. It is decision-specific: a Stop is NOT a Continue, so it must never render the
    // Continue notice — Stop leaves the session blocked outright.
    const belt = !!res.manual_resume_required;
    if (res.decision === "stop")
      return belt
        ? "Session stopped; the profile ESTOP belt remains engaged (resume via `hermes resume`)."
        : "Session stopped.";
    if (res.decision === "ceiling") {
      const amt = res.amount_usd != null ? ` to $${Number(res.amount_usd).toFixed(2)}` : "";
      if (!res.money_block_cleared) {
        // granted (the ceiling IS written) but at/below current spend, so the money block stays set
        return belt
          ? `Ceiling set${amt}; the per-session cost block remains active (ceiling not above current spend). The profile ESTOP belt also remains engaged; resume via \`hermes resume\` or the UA-28 upstream lock.`
          : `Ceiling set${amt}; the per-session cost block remains active (ceiling not above current spend).`;
      }
      return belt
        ? `Ceiling set${amt}. The per-session cost block is cleared, but the profile ESTOP belt remains engaged; resume via \`hermes resume\` or the UA-28 upstream lock.`
        : `Ceiling set${amt}.`;
    }
    // continue
    if (belt) return MANUAL_RESUME_NOTICE;
    return "Applied.";
  }

  function httpStatus(e) {
    // SDK.fetchJSON rejects with a plain Error("<code>: <body>") carrying no status field, so
    // recover the HTTP status from an explicit property when present, else the "<code>" prefix.
    if (!e) return null;
    if (typeof e.status === "number") return e.status;
    if (typeof e.statusCode === "number") return e.statusCode;
    const m = typeof e.message === "string" ? /^(\d{3})\b/.exec(e.message) : null;
    return m ? Number(m[1]) : null;
  }

  function EscalationCard() {
    const [items, setItems] = useState([]);
    const [busy, setBusy] = useState({});
    const [amounts, setAmounts] = useState({});
    // Notes are partitioned by profile so a resolution recorded under one profile never renders after a
    // switch to another (a resolved episode is absent from the new profile's pending set).
    const [notesByProfile, setNotesByProfile] = useState({});
    const [activeProfile, setActiveProfile] = useState(null);
    // Monotonic request token: a slow response for a PREVIOUS profile must not overwrite the
    // current profile's card state after a profile switch (only the latest refresh applies).
    const reqSeq = React.useRef(0);

    const refresh = useCallback(async () => {
      const seq = ++reqSeq.current;
      try {
        const requestedProfile = await currentProfile();
        const rows = await SDK.fetchJSON(`${API}/escalations?profile=${encodeURIComponent(requestedProfile)}`);
        // Drop a stale result: a newer refresh started, OR the active profile changed while this
        // request was in flight (the header slot is not remounted on a profile switch, so a
        // delayed prior-profile response must not overwrite the current profile's card).
        if (seq !== reqSeq.current || requestedProfile !== (await currentProfile())) return;
        setItems(Array.isArray(rows) ? rows : []);
        setActiveProfile(requestedProfile);
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
      // Hoisted so the catch records the note under the same profile the request targeted.
      let profile = activeProfile;
      try {
        profile = await currentProfile();
        const body = { decision };
        if (amountUsd !== undefined && amountUsd !== null) body.amount_usd = amountUsd;
        const res = await SDK.fetchJSON(
          `${API}/escalations/${encodeURIComponent(episodeId)}/resolve?profile=${encodeURIComponent(profile)}`,
          { method: "POST", body: JSON.stringify(body) }
        );
        // Record under the profile this resolve targeted (captured above), not whatever is active later.
        setNotesByProfile((m) => ({ ...m, [profile]: { ...(m[profile] || {}), [episodeId]: outcomeNote(res) } }));
      } catch (e) {
        // Reserve the auth message for an explicit 401/403; a 500 / network error is a generic,
        // retryable failure. (An in-band unauthorized RESULT is a 2xx {granted:false,
        // reason:"unauthorized"} handled by outcomeNote above.)
        const status = httpStatus(e);
        const note = (status === 401 || status === 403)
          ? "Not authorized for this profile."
          : "Could not resolve — please retry.";
        setNotesByProfile((m) => ({ ...m, [profile]: { ...(m[profile] || {}), [episodeId]: note } }));
      } finally {
        setBusy((b) => ({ ...b, [episodeId]: false }));
        refresh();
      }
    }, [refresh, activeProfile]);

    // A granted resolution removes the episode from the pending list on the next refresh; keep its
    // outcome note visible afterward (esp. the manual-resume notice) as a compact row, so the operator
    // still sees why the session is paused and how to resume it. Notes are scoped to the active profile.
    const notes = notesByProfile[activeProfile] || {};
    const pending = new Set(items.map((it) => it.episode_id));
    const resolvedNotes = Object.keys(notes).filter((id) => notes[id] && !pending.has(id));
    if (!items.length && !resolvedNotes.length) return null;

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
          // An immortal (daily) session is Continue-only: no Stop, no set-ceiling control.
          immortal ? null : h("button",
            { type: "button", disabled: !!busy[eid], onClick: () => resolve(eid, "stop"),
              className: "rounded border border-(--ui-stroke-secondary) px-2 py-0.5 disabled:opacity-50" },
            "Stop"),
          immortal ? null : h("input",
            { type: "number", step: "0.01", min: "0", placeholder: "exact $",
              value: amounts[eid] || "", "aria-label": "exact ceiling in USD",
              onChange: (e) => setAmounts((a) => ({ ...a, [eid]: e.target.value })),
              className: "w-20 rounded border border-(--ui-stroke-secondary) px-1 py-0.5" }),
          immortal ? null : h("button",
            { type: "button", disabled: !!busy[eid] || !amounts[eid],
              onClick: () => resolve(eid, "ceiling", parseFloat(amounts[eid])),
              className: "rounded border border-(--ui-stroke-secondary) px-2 py-0.5 disabled:opacity-50" },
            "Set ceiling"),
          notes[eid] ? h("span", { role: "status", className: "text-(--ui-text-tertiary)" }, notes[eid]) : null
        );
      }),
      resolvedNotes.map((id) =>
        h("div",
          { key: `note-${id}`, className: "flex flex-wrap items-center gap-2 py-1" },
          h("span", { className: "font-medium" }, "Cost escalation"),
          h("span", { role: "status", className: "text-(--ui-text-tertiary)" }, notes[id])))
    );
  }

  // The card is a shell-wide slot; register a hidden-tab placeholder too so a direct
  // hit on the tab path still resolves to a component (per the dashboard SDK guidance).
  PLUGINS.register("nv-cost-cap", EscalationCard);
  PLUGINS.registerSlot("nv-cost-cap", "header-banner", EscalationCard);
})();
