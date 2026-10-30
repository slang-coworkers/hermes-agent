---
name: approve
description: Gate the final approval of a fleet pull request on the checks API at the pinned head.
---

# Approve

The approver grants the final approval only for a pull request whose check runs
are green at the exact head the reviewers saw.

## Steps

1. Read the pull request and pin its head SHA (the commit the reviewer reviewed).
2. Run the ci-gate skill in credential-free mode at that pinned head:

       python3 ~/.hermes/skills/ci-gate/scripts/checks_gate.py <owner/repo> <pr> <pinned head sha> --read anonymous

   Run it through `terminal` in your worker, where skills sync to
   `~/.hermes/skills/`; never use the skill directory that skill_view reports or
   the skill-directory template variable, which both name the gateway's copy.

3. Exit 0: approve, quoting the gate's JSON line.
4. Exit 3: refuse to approve and name every refused check run from the gate's
   output. An unavailable read is a refusal too. Never approve on the legacy
   commit-status endpoint.
