# gov-f25-default

The DEFAULT (multiplexer) profile for the GOV-F25 live scenario (AC-GOV-F25-7).
It carries the signed GitHub webhook route `gh-pr`. When a later PR delivery
arrives for an artifact already CLAIMED by a worker, this profile's
`pre_gateway_dispatch` observer durably enqueues the notification onto the
owning kanban card and returns `{"action":"skip"}` — so this profile mints no
disposable session for the delivery; the owner is woken by the notifier instead.
