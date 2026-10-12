---
name: sdlc-review
description: Review a pull request's diff and report concrete defects.
---

# sdlc-review

Review the pull request you were invited to.

1. Read the change: `gh pr diff <pr> --repo <repo>`.
2. Assess correctness, security, and clarity of the diff only (not the whole repo).
3. Report **concrete** findings. For each defect, name it and quote the exact
   offending token or line so a human can locate it. Hard-coded credentials or
   secret literals (e.g. an `AKIA…` string) are always must-fix — call them out
   explicitly.
4. If the diff is clean, say so in one line. Keep the review to a few sentences.

Do not post the review yourself; the invoking webhook route delivers your reply
to the PR.
