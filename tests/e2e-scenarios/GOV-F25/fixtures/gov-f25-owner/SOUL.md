# gov-f25-owner

The worker profile for the GOV-F25 live scenario (AC-GOV-F25-7). Its session
opens a PR (`gh pr create`) that nv-artifact's `post_tool_call` observer records
as the first claim of `(repo, pr)`, establishing the owning kanban card and the
durable-retry wake sub. This is the session the fleet notifier must wake — via
the api_server profile-scoped mirror `/p/gov-f25-owner/v1/chat/completions` —
when a later webhook delivery for the same PR arrives at the default profile,
resuming THIS session (not a fresh orphan).
