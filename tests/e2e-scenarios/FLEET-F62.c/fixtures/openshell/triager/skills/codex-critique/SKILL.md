---
name: codex-critique
description: Independent read-only second-opinion review run by codex.
---

# Codex Critique

Run an independent second-opinion review by codex at each natural workflow
transition. Codex reads the artifacts itself in a separate, read-only session;
hand it file paths, not contents, and keep the returned thread id for
follow-up rounds.

## Stages

- Diagnosis review once the root cause or request is identified.
- Plan review once an approach is chosen, before editing.
- Code review once edits and tests pass, before reporting.
- Output review once a deliverable is drafted, before sending.

## Rounds

Three rounds is the whole budget. Address each must-fix item with a concrete
fix and a re-check, then reply on the saved thread. A must-fix that survives
the final round is escalated to the parent with the evidence.
