---
name: plan
description: Plan, investigate, review, or research into a report.
---

# Plan

Any task ending in a written artifact — a plan, investigation, review, or
research memo. Output is text; no file changes. Hand off to the implement
workflow when code or docs must change.

## Steps

1. Understand — restate the ask and identify the mode; state your reading and
   proceed if scope is ambiguous.
2. Recall — spawn a sub-agent to scan prior shared learnings before
   investigating, so exploration stays out of your context.
3. Research — read code, issues, and docs read-only; fan out via sub-agents for
   wide scope.
4. Synthesize — organize by mode: approaches with trade-offs for a plan; facts
   versus hypotheses for an investigation; findings by severity for a review.
5. Deliver — write the report with mode-appropriate sections and cite concrete
   files and lines.
6. Handoff — post a short summary linking the report; invoke the implement
   workflow when the mode is plan and code changes follow.

## Checks gate (before any approve verdict)

Before you recommend approving a pull request, run the ci-gate skill at the
exact head SHA you reviewed, in provider-read mode:

    python3 <ci-gate skill dir>/scripts/checks_gate.py <owner/repo> <pr> <reviewed head sha> --read provider

Exit 0 means every check run at that head completed green. Exit 3 is a refusal:
your verdict is "do not approve", naming each refused check run from its output.
Never fall back to the legacy commit-status endpoint; it can be empty while a
check run is red.
