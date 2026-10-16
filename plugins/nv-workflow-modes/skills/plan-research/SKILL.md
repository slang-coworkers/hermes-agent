---
name: plan-research
description: "Research a topic across multiple sources, synthesize an answer, and state the uncertainties. The NanoClaw /plan research mode, ported to Hermes as /plan-research."
version: 0.1.0
author: NousResearch
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [plan, research, sources, synthesis, uncertainty, workflow-modes]
---

# /plan-research — research and synthesis workflow

**Research** the topic named in the invocation across several **sources**,
**synthesize** what they agree and disagree on, and be explicit about
**uncertainty**. This is a read-only, evidence-gathering workflow: the deliverable
is a written answer, not a code change.

Work the phases in order.

1. **Scope the research question.** Restate the topic as a precise question and
   list the sub-questions that would answer it. Decide what would count as a good
   answer and what kinds of **sources** are authoritative for this topic (primary
   docs, source code, standards, papers, maintainer decisions) versus merely
   suggestive (blog posts, forum threads, model recall).

2. **Gather from multiple sources.** Collect evidence from several independent
   **sources**, preferring primary and firsthand ones. Record each with a citation
   (URL or `path:line`) and a one-line note of what it supports. Deliberately seek
   sources that could disconfirm the emerging answer, not only ones that confirm
   it. Use a subagent for broad gathering so the working context stays focused.

3. **Synthesize.** Cross-read the sources and **synthesize** a coherent answer:
   where they converge, state the conclusion with its supporting citations; where
   they conflict, name the conflict and which source you weight more and why. Do
   not paper over disagreement — a synthesis that hides it is wrong.

4. **State the answer and its uncertainty.** Deliver the synthesized answer first,
   then the supporting sources, then an explicit **uncertainty** section: what
   remains unknown, which claims rest on weak sources, and what further research
   would reduce the uncertainty. Separate established fact from inference throughout.

The specific research topic follows below; treat it as the subject of this workflow.
