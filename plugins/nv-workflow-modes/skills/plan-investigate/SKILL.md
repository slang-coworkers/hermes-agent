---
name: plan-investigate
description: "Investigate a question or system READ-ONLY and produce an evidence-backed report. The NanoClaw /plan investigate mode, ported to Hermes as /plan-investigate."
version: 0.1.0
author: NousResearch
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [plan, investigate, read-only, evidence, workflow-modes]
---

# /plan-investigate — read-only investigation

Investigate the subject named in the invocation and hand back an **evidence**-backed
**report**. This mode is strictly **read-only**: you gather and reason, you do not
edit files, run mutating commands, or open PRs. If the task actually requires a
change, say so in the report and stop — switch to `/implement` only on an explicit
follow-up.

Work the phases in order. Keep a short running list of what you have confirmed
versus what is still open, and stop to re-plan rather than pushing through a wrong
assumption.

1. **Frame the question.** Restate what you were asked to investigate in one
   sentence and name the concrete artifacts in scope (files, services, logs, a
   commit range). If the scope is ambiguous, state your interpretation and proceed;
   do not pause for confirmation. List the questions whose answers would settle it.

2. **Gather evidence (read-only).** Search and read the relevant sources — code,
   configuration, logs, docs, prior issues. Cite every load-bearing fact as
   `path:line` or a URL; never assert from memory. Prefer primary sources over
   summaries, and read the actual current state of anything you are about to
   describe. Spawn a subagent for any wide or noisy search so the evidence trail
   stays clean.

3. **Reason and separate facts from hypotheses.** Build the smallest explanation
   consistent with the evidence. Label each claim as a fact (with its citation) or
   a hypothesis (with what would confirm or refute it). Call out contradictions
   between sources explicitly rather than silently picking one.

4. **Write the report.** Deliver a written **report**: the answer or conclusion
   first, then the **evidence** (cited), the facts-vs-hypotheses split, the open
   questions, and a recommended next action. No file changes are part of this mode —
   the report is the whole deliverable.

The user's specific investigation target follows below; treat it as the subject of
this workflow.
