---
name: ci-gate
description: Read the GitHub checks API at the reviewed head and refuse an approval on any non-green check run.
---

# ci-gate

Run this gate at the reviewed head SHA before giving any approve verdict. A
refusal IS the verdict: report the refused check runs and do not approve.

The legacy commit-status endpoint is not a CI signal. It can be empty while a
check run is red, so this gate reads only the check runs.

```
python3 ~/.hermes/skills/ci-gate/scripts/checks_gate.py <owner/repo> <pr> <pinned_head_sha> --read <mode> [--waive <check name> ...]
```

Run the gate through `terminal` in the worker, where skills sync to
`~/.hermes/skills/`; never use the skill directory that skill_view reports or the
skill-directory template variable, which both name the gateway's copy.

- `--read provider`: authenticated GET reads through the GitHub CLI, credentialed
  at the egress proxy by the attached provider (the reviewer).
- `--read anonymous`: credential-free GET reads with curl against the public API
  (the approver; the repository must be public).

Exit 0 with `"pass": true` means every check run at the pinned head completed
with `success`, or was waived by name. Exit 3 is a refusal: a failed, neutral,
skipped, cancelled, timed-out, action-required, stale or still-running run; no
runs at all; a head that moved since review; or an unavailable read (operator
lane L-CHK). Waive a check only when the human reviewer said so explicitly.
