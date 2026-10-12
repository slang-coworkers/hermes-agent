---
name: implement
description: Execute an approved plan, verify it, then ship.
---

# Implement

Pure execution of an approved plan; diagnosis lives in the plan workflow.

## Invariants

- Plan first; a non-trivial task with no plan goes back to the plan workflow.
- For a bug fix, write a failing test before the fix.
- Keep scope narrow; log unrelated observations rather than acting on them.
- Tests, format, and lint must pass before shipping.

## Steps

1. Setup — load the plan; extract the file list and verification steps; work in
   a fresh worktree, never the main checkout.
2. Recall — scan prior learnings in a sub-agent before changing anything.
3. Reproduce — a failing test for a bug, a skeleton showing the gap for a
   feature; commit it separately so the delta is visible.
4. Change — the minimum edit matching the plan, in one subsystem, existing
   style.
5. Verify — run the tests, format, and lint; delegate a long build to a
   sub-agent.
6. Ship — a descriptive commit linking the issue, then open or update the pull
   request with a summary and test plan.
