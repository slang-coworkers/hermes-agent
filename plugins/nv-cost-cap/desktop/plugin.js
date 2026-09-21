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
import { jsx, jsxs } from 'react/jsx-runtime'

const API = '/escalations'

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
      children: items.map((it) => {
        const immortal = !!it.immortal
        const controls = [
          jsx('button', {
            type: 'button',
            className: 'rounded bg-(--ui-accent) px-2 py-0.5 text-white',
            onClick: () => resolve.mutate({ episodeId: it.episode_id, decision: 'continue' }),
            children: 'Continue',
          }, 'continue'),
        ]
        if (!immortal) {
          controls.push(jsx('button', {
            type: 'button',
            className: 'rounded border border-(--ui-stroke-secondary) px-2 py-0.5',
            onClick: () => resolve.mutate({ episodeId: it.episode_id, decision: 'stop' }),
            children: 'Stop',
          }, 'stop'))
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
            jsxs('div', { className: 'flex items-center gap-2', children: controls }, 'controls'),
          ],
        }, it.episode_id)
      }),
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
