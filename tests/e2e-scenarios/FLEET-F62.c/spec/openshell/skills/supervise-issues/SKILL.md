---
name: supervise-issues
description: Periodic supervisor sweep over in-flight issue chains.
---

# Supervise Issues

Periodic supervisor for in-flight issue chains, run on a recurring schedule as a
fresh session each tick. Walk every open chain, find the stuck ones, and nudge
or escalate.

## Each tick

- Discover live chains from the current session list, unioned with the prior
  journal; a chain is in-flight while its issue is open with no merged fix.
- Every chain needs a resumable artifact a human can land on: an open pull
  request, a comment on it, or a comment on the issue.
- Nudge a silent chain, re-check a pull request whose checks are stale or behind
  its base, and escalate a real blocker to the operator.

The classification (new-set math, activity clock, ball direction) is
deterministic; keep the journal keyed on the chain id, not on prose.
