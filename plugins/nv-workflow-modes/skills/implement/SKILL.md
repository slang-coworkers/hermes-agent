---
name: implement
description: "Execute an approved plan: make the change, edit the code, write and run tests, and validate the result. The NanoClaw /implement command, ported to Hermes as /implement (no stock builtin)."
version: 0.1.0
author: NousResearch
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [implement, edit, test, validate, execute, workflow-modes]
---

# /implement — execution workflow

**Implement** the change described in the invocation. Unlike the plan-modes, this
is an *execution* workflow: you may **edit** files and run commands. It assumes a
plan already exists (from `/plan` or `/plan-investigate`); if the change is
non-trivial and no plan exists, produce one first, then implement it.

Work the phases in order and keep the scope narrow — do only what was asked, and
log unrelated observations instead of acting on them.

1. **Confirm the plan and reproduce.** Restate the change and its acceptance
   criteria. For a bug fix, first write a failing **test** that reproduces it; for a
   feature, identify the smallest change that satisfies the criteria. Do not start
   editing until you can state what "done" looks like as something you can check.

2. **Make the change.** **Edit** the code as the minimum diff that meets the plan,
   in the existing style of the surrounding code, in one subsystem at a time. Add a
   comment only for non-obvious *why*, never to restate what the code does. Prefer a
   one-line fix over a clever rewrite when both work.

3. **Test.** Extend or add tests that assert the new behaviour as a contract, and
   run the project's real **test** runner — not a bare invocation that skips its
   harness. A change is not done because it was written; it is done when a test
   proves it works.

4. **Validate and format.** **Validate** the whole change: run the tests, the
   linter, and the formatter, and confirm they pass. Re-read the diff for scope
   creep and leftover debug code. Report the outcome honestly with the passing
   evidence (test output, diff); if **validation** fails after two attempts, stop
   and surface the failure rather than looping.

The specific implementation task follows below; treat it as the subject of this
workflow.
