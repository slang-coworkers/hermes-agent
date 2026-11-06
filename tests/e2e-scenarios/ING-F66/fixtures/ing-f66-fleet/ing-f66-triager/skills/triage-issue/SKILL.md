---
name: triage-issue
description: Triage a GitHub issue and brief the fixer to act.
---

# Triage Issue

Specialist triage of a GitHub issue: research it, map the solution space, hand
the fixer a briefing they can act on in under a minute, then forward the
resolution upstream. Do not edit others' comments or open, close, or modify pull
requests — that is not triage's surface.

## Steps

1. Read the issue — extract what is broken or requested, the repro, the affected
   component, and any other-user confirmations.
2. Recall — spawn a sub-agent to scan prior triage learnings.
3. Research — fan out via sub-agents over the local checkout, the architecture
   notes, and prior issues; cost is context, not wall-clock.
4. Map the solution space — enumerate two or three candidates, each with a name,
   a file-and-line pointer, a behaviour delta, trade-offs, and a risk.
5. Recommend — a starting point, not a verdict; the fastest correct fix that
   does not regress adjacent surfaces.
6. Classify and persist — write the investigation memo the fixer will read.
7. Report up — send the triage rollup to the parent and attach the memo.
8. Forward to the fixer — always hand off; the fixer decides whether and how to
   fix and may bounce back.
9. Post the outcome on the issue — a short comment so a human landing on the
   issue sees where it stands.
10. Forward the resolution upstream once the fix report lands; the chain is not
    closed until you do.
