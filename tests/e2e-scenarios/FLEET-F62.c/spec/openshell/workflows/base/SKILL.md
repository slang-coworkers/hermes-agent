---
name: base
description: Universal workflow stub and overlay splice point.
---

# Base Workflow

A universal stub and splice point for cross-cutting overlays; coworkers never
invoke it directly. In NanoClaw a workflow that omitted an extends field
implicitly extended this one. Hermes compose does not carry that over: it renders
only the workflows a role binds, so this file is never bound or rendered, and an
overlay targeting the base workflow is not applied to other workflows.

## Steps

0. Track — seed a checklist with the concrete workflow's steps and mark each
   done as you go.
1. Understand — read the inbound request; identify what is asked and the success
   criterion; ask once if it is ambiguous.
2. Setup — ready the workspace and files you will touch; recall useful memory.
3. Change — do the work as the smallest correct change.
4. Deliver — produce the artifact the request asked for and verify it.
5. Report — send a concise status to the caller and end the turn.
