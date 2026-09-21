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
function outcomeNote(res) {
  if (!res || typeof res !== 'object') return 'Request failed.'
  if (res.granted) {
    if (res.decision === 'ceiling' && res.amount_usd != null) return `Ceiling set to $${Number(res.amount_usd).toFixed(2)}.`
    return 'Applied.'
  }
  return OUTCOME_NOTE[res.reason] || `Not applied (${res.reason}).`
}

// One card per pending episode. A CHILD component (not an inline map body) so each card
// can hold its own ceiling-input / outcome-note state via hooks — hooks cannot live in a map callback.
function EscalationCard({ it, resolve }) {
  const immortal = !!it.immortal
  const [amount, setAmount] = useState('')
  const [note, setNote] = useState('')
  const amt = Number.parseFloat(amount)
  const canSet = Number.isFinite(amt) && amt > 0

  const act = (payload) =>
    resolve.mutateAsync(payload).then(
      (res) => setNote(outcomeNote(res)),
      () => setNote('Not authorized for this profile.'),
    )

  const controls = [
    jsx('button', {
      type: 'button',
      className: 'rounded bg-(--ui-accent) px-2 py-0.5 text-white',
      onClick: () => act({ episodeId: it.episode_id, decision: 'continue' }),
      children: 'Continue',
    }, 'continue'),
  ]
  // An immortal (daily) session is Continue-only: no Stop, no set-ceiling control.
  if (!immortal) {
    controls.push(jsx('button', {
      type: 'button',
      className: 'rounded border border-(--ui-stroke-secondary) px-2 py-0.5',
      onClick: () => act({ episodeId: it.episode_id, decision: 'stop' }),
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
      onClick: () => canSet && act({ episodeId: it.episode_id, decision: 'ceiling', amountUsd: amt }),
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

    const { data } = useQuery({
      queryKey: ['nv-cost-cap', 'escalations', profile],
      queryFn: () => ctx.rest(`${API}${q}`),
      refetchInterval: 5000,
    })

    const resolve = useMutation({
      mutationFn: ({ episodeId, decision, amountUsd }) =>
        ctx.rest(`${API}/${encodeURIComponent(episodeId)}/resolve${q}`, {
          method: 'POST',
          body: amountUsd == null ? { decision } : { decision, amount_usd: amountUsd },
        }),
      onSettled: () => qc.invalidateQueries({ queryKey: ['nv-cost-cap', 'escalations', profile] }),
    })

    const items = Array.isArray(data) ? data : []
    if (!items.length) {
      return jsx('div', {
        className: 'flex h-full items-center justify-center p-3 text-sm text-(--ui-text-tertiary)',
        children: 'No cost escalations',
      })
    }

    return jsx('div', {
      className: 'flex h-full flex-col gap-2 p-3 text-sm',
      children: items.map((it) => jsx(EscalationCard, { it, resolve }, it.episode_id)),
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
