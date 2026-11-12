---
name: approve
description: Check a fleet pull request's check runs at the pinned head and hand the verdict to the human approver.
---

# Approve

Approvals are human-only. The approver checks that a pull request's check runs
are green at the exact head the reviewers saw and hands that checks verdict to
the human approver; it never approves a pull request itself.

## Steps

1. Read the pull request and pin its head SHA (the commit the reviewer reviewed).
2. Run the ci-gate skill in credential-free mode at that pinned head:

       python3 ~/.hermes/skills/ci-gate/scripts/checks_gate.py <owner/repo> <pr> <pinned head sha> --read anonymous

   Run it through `terminal` in your worker, where skills sync to
   `~/.hermes/skills/`; never use the skill directory that skill_view reports or
   the skill-directory template variable, which both name the gateway's copy.

3. Checks green at the pinned head (exit 0): hand to the human approver,
   quoting the gate's JSON line.
4. Refused checks (exit 3): name every refused check run from the gate's
   output in the verdict you hand to the human approver. An unavailable read is
   a refusal too. Never take a verdict from the legacy commit-status endpoint.
