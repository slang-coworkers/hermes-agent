---
name: nv-path-guard-base
description: Pick the PR base branch from the path-guard allowlists.
---

# Path-guard base branch

The repository's `.github/nv-path-guard/<branch>.txt` allowlists say which branch owns
which paths, and its CI path guard fails a pull request whose changed paths are not
owned by the base it targets. Pick the base from those allowlists, never from the
default branch and never from the issue text.

## Use

Run the helper that ships in this skill's `scripts/` directory (in a worker sandbox:
`~/.hermes/skills/nv-path-guard-base/scripts/pick_base.py`), passing the checkout and every
changed or planned path relative to the repository root:

    python3 ~/.hermes/skills/nv-path-guard-base/scripts/pick_base.py --repo <checkout> <path> [<path> ...]

- Exit 0: stdout is the one branch whose allowlist owns every path. Use it as the base.
- Exit 3, no output: no branch, or more than one, owns the whole set. Do not guess.
  Report the conflict up with the paths and stop.
- Exit 2: the allowlists are missing, empty or unreadable, or a pattern or path cannot
  be evaluated. Report it up and stop.

The helper matches with the guard's own rules (gitignore syntax, including negation and
git's rule that a path under an excluded directory stays excluded). It reads only the
allowlist files and needs no network.
