// nv-cost-cap — Desktop (Electron) plugin half (COST-F30).
//
// Renders the cost-escalation card as a contributed pane over the SAME backend as the
// web dashboard half (/api/plugins/nv-cost-cap, served by dashboard/plugin_api.py on the
// gateway host). Loaded from $HERMES_HOME/plugins/nv-cost-cap/desktop/plugin.js once the
// package is installed and the desktop half is enabled in Settings → Plugins (default off).
//
// Disk plugin: loaded uncompiled, so UI is written with jsx()/jsxs(), never JSX syntax.
// `ctx.rest` carries the profile only as IPC metadata, so — like the web client — we append
// ?profile=<active profile> to every backend call ourselves.

import { host, useValue } from '@hermes/plugin-sdk'
import { useQuery, useMutation, useQueryClient } from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const API = '/escalations'

// One-line operator feedback for a 2xx resolution that returns granted:false (unauthorized /
// deferred / already-resolved / …) — otherwise the click looks like a silent no-op.
const OUTCOME_NOTE = {
  unauthorized: 'Not authorized for this profile.',
  'already-resolved': 'Already resolved.',
  'no-episode': 'No pending escalation.',
  'immortal-continue-only': 'Continue-only session.',
  'invalid-amount': 'Enter a valid exact USD amount.',
  managed: 'Ceiling is administrator-managed.',
  'stale-generation': 'Superseded by a newer cost event.',
  'session-closed': 'Session already closed.',
  deferred: 'Retrying — check again shortly.',
}
// Verbatim manual-resume notice surfaced when a granted resolution LEAVES an engaged ESTOP belt
// (the plugin never unlinks it); matches the backend _cost_outcome_text notice exactly.
const MANUAL_RESUME_NOTICE = 'Continue applied — the per-session cost block is cleared, but the profile ESTOP belt remains engaged (gates cron/kanban/new inbounds); resume via `hermes resume` or the UA-28 upstream lock'
function outcomeNote(res) {
  if (!res || typeof res !== 'object') return 'Request failed.'
  if (!res.granted) return OUTCOME_NOTE[res.reason] || `Not applied (${res.reason}).`
  // manual_resume_required means the profile ESTOP belt (which the plugin never lifts) is still
  // engaged; it is decision-specific — a Stop is not a Continue and must never render the Continue
  // notice (Stop leaves the session blocked outright).
  const belt = !!res.manual_resume_required
  if (res.decision === 'stop')
    return belt
      ? 'Session stopped; the profile ESTOP belt remains engaged (resume via `hermes resume`).'
      : 'Session stopped.'
  if (res.decision === 'ceiling') {
    const amt = res.amount_usd != null ? ` to $${Number(res.amount_usd).toFixed(2)}` : ''
    if (!res.money_block_cleared) {
      // granted (the ceiling IS written) but at/below current spend, so the money block stays set
      return belt
        ? `Ceiling set${amt}; the per-session cost block remains active (ceiling not above current spend). The profile ESTOP belt also remains engaged; resume via \`hermes resume\` or the UA-28 upstream lock.`
        : `Ceiling set${amt}; the per-session cost block remains active (ceiling not above current spend).`
    }
    return belt
      ? `Ceiling set${amt}. The per-session cost block is cleared, but the profile ESTOP belt remains engaged; resume via \`hermes resume\` or the UA-28 upstream lock.`
      : `Ceiling set${amt}.`
  }
  // continue
  if (belt) return MANUAL_RESUME_NOTICE
  return 'Applied.'
}

// Recover the HTTP status from a rejected ctx.rest call so a network/5xx failure is not mislabelled
// as an authorization failure (only a real 401/403 tells the operator to change permissions).
function httpStatus(e) {
  if (!e) return null
  if (typeof e.status === 'number') return e.status
  if (typeof e.statusCode === 'number') return e.statusCode
  const m = typeof e.message === 'string' ? /^(\d{3})\b/.exec(e.message) : null
  return m ? Number(m[1]) : null
}
function errorNote(e) {
  const s = httpStatus(e)
  return (s === 401 || s === 403) ? 'Not authorized for this profile.' : 'Could not resolve — please retry.'
}

// One card per pending episode. A CHILD component (not an inline map body) so each card can hold its
// own ceiling-input state via a hook. The OUTCOME note is held at the PANE level (keyed by episode) and
// passed in, so it survives this card unmounting when the resolved episode leaves the pending list.
function EscalationCard({ it, note, resolve }) {
  const immortal = !!it.immortal
  const [amount, setAmount] = useState('')
  const amt = Number.parseFloat(amount)
  const canSet = Number.isFinite(amt) && amt > 0

  const controls = [
    jsx('button', {
      type: 'button',
      className: 'rounded bg-(--ui-accent) px-2 py-0.5 text-white',
      onClick: () => resolve(it.episode_id, 'continue'),
      children: 'Continue',
    }, 'continue'),
  ]
  // An immortal (daily) session is Continue-only: no Stop, no set-ceiling control.
  if (!immortal) {
    controls.push(jsx('button', {
      type: 'button',
      className: 'rounded border border-(--ui-stroke-secondary) px-2 py-0.5',
      onClick: () => resolve(it.episode_id, 'stop'),
      children: 'Stop',
    }, 'stop'))
    controls.push(jsx('input', {
      type: 'number',
      step: '0.01',
      min: '0',
      inputMode: 'decimal',
      'aria-label': 'exact ceiling USD',
      className: 'w-20 rounded border border-(--ui-stroke-secondary) bg-transparent px-1 py-0.5',
      placeholder: 'USD',
      value: amount,
      onChange: (e) => setAmount(e.target.value),
    }, 'ceiling-input'))
    controls.push(jsx('button', {
      type: 'button',
      disabled: !canSet,
      className: 'rounded border border-(--ui-stroke-secondary) px-2 py-0.5 disabled:opacity-50',
      // decision 'ceiling' is the dashboard backend's set-ceiling verb (plugin_api.py).
      onClick: () => canSet && resolve(it.episode_id, 'ceiling', amt),
      children: 'Set ceiling',
    }, 'set-ceiling'))
  }

  return jsxs('div', {
    className: 'flex flex-col gap-1 rounded border border-(--ui-stroke-secondary) p-2',
    children: [
      jsx('div', {
        className: 'font-medium',
        children: `Cost escalation — session ${it.session}`,
      }, 'title'),
      jsx('div', {
        className: 'text-(--ui-text-secondary)',
        children:
          `spend $${Number(it.spend || 0).toFixed(2)}` +
          (it.ceiling != null ? ` · ceiling $${Number(it.ceiling).toFixed(2)}` : '') +
          (immortal ? ' · immortal (continue-only)' : ''),
      }, 'meta'),
      jsxs('div', { className: 'flex flex-wrap items-center gap-2', children: controls }, 'controls'),
      note ? jsx('div', { role: 'status', className: 'text-(--ui-text-tertiary)', children: note }, 'note') : null,
    ],
  })
}

function EscalationPane(ctx) {
  return function Pane() {
    const profile = useValue(host.state.profile) || 'default'
    const qc = useQueryClient()
    const q = `?profile=${encodeURIComponent(profile)}`
    // Notes are partitioned by profile so a resolution recorded under one profile never renders after a
    // switch to another (a resolved episode is absent from the new profile's pending set).
    const [notesByProfile, setNotesByProfile] = useState({})

    const { data } = useQuery({
      queryKey: ['nv-cost-cap', 'escalations', profile],
      queryFn: () => ctx.rest(`${API}${q}`),
      refetchInterval: 5000,
    })

    const resolveMut = useMutation({
      mutationFn: ({ episodeId, decision, amountUsd }) =>
        ctx.rest(`${API}/${encodeURIComponent(episodeId)}/resolve${q}`, {
          method: 'POST',
          body: amountUsd == null ? { decision } : { decision, amount_usd: amountUsd },
        }),
      onSettled: () => qc.invalidateQueries({ queryKey: ['nv-cost-cap', 'escalations', profile] }),
    })

    // Set the outcome note at PANE level (keyed by episode, under the profile it was resolved against)
    // so it survives the card unmounting when the resolved episode leaves the pending list on the next
    // refetch (esp. the manual-resume notice). The request profile is captured for the async outcome.
    const resolve = (episodeId, decision, amountUsd) => {
      const p = profile
      return resolveMut.mutateAsync({ episodeId, decision, amountUsd }).then(
        (res) => setNotesByProfile((m) => ({ ...m, [p]: { ...(m[p] || {}), [episodeId]: outcomeNote(res) } })),
        (e) => setNotesByProfile((m) => ({ ...m, [p]: { ...(m[p] || {}), [episodeId]: errorNote(e) } })),
      )
    }

    const items = Array.isArray(data) ? data : []
    const notes = notesByProfile[profile] || {}
    const pending = new Set(items.map((it) => it.episode_id))
    const resolvedNotes = Object.keys(notes).filter((id) => notes[id] && !pending.has(id))
    if (!items.length && !resolvedNotes.length) {
      return jsx('div', {
        className: 'flex h-full items-center justify-center p-3 text-sm text-(--ui-text-tertiary)',
        children: 'No cost escalations',
      })
    }

    return jsx('div', {
      className: 'flex h-full flex-col gap-2 p-3 text-sm',
      children: [
        ...items.map((it) => jsx(EscalationCard, { it, note: notes[it.episode_id], resolve }, it.episode_id)),
        ...resolvedNotes.map((id) => jsx('div', {
          role: 'status',
          className: 'rounded border border-(--ui-stroke-secondary) p-2 text-(--ui-text-tertiary)',
          children: notes[id],
        }, `note-${id}`)),
      ],
    })
  }
}

export default {
  id: 'nv-cost-cap', // must match the plugin folder name
  name: 'Cost cap',
  register(ctx) {
    ctx.register({
      id: 'escalations-pane',
      area: 'panes',
      title: 'Cost escalations',
      data: { placement: 'right', width: '320px' },
      render: () => jsx(EscalationPane(ctx), {}),
    })
  },
}
