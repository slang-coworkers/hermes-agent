---
name: plan-review
description: "Review work READ-ONLY and return findings by severity plus a clear verdict. The NanoClaw /plan review mode, ported to Hermes as /plan-review (distinct from the stock /review independent-subagent command)."
version: 0.1.0
author: NousResearch
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [plan, review, read-only, findings, verdict, workflow-modes]
---

# /plan-review — read-only review workflow

Review the target named in the invocation (a diff, a design, a document, a
release) and return **findings** ordered by severity plus a single **verdict**.
This is a **read-only** workflow body that runs inline in the current turn — it is
deliberately *not* the stock `/review` command, which spawns an independent
subagent over a conversation snapshot. You inspect and judge; you do not edit,
fix, or run mutating commands.

Work the phases in order.

1. **Establish the review baseline.** Restate what is being reviewed and against
   what standard (the acceptance criteria, the spec, the coding conventions, the
   invariants). Read the actual target at its current state — the real diff, the
   real file:line — never a remembered version. Note anything out of scope so the
   review stays narrow.

2. **Inspect for findings.** Go through the target systematically: correctness and
   edge cases first, then safety/security, then contract/API fit, then clarity and
   maintainability. For each **finding** capture `path:line`, what is wrong, why it
   matters, and a concrete suggested fix. Keep read-only: reproduce reasoning from
   the evidence, do not apply the fix.

3. **Rank and decide.** Sort findings into must-fix (blocks) versus advisory
   (author's call). A must-fix is a correctness, safety, security, or data-loss
   defect, or a scope reduction below spec without an evidenced blocker.

4. **Deliver the verdict.** State one **verdict** — approve or request-changes —
   with the must-fix list that justifies it and the advisories below it. If you
   approve, say what you checked; if you request changes, every must-fix names its
   fix and how to re-verify it.

The specific review target follows below; treat it as the subject of this workflow.
