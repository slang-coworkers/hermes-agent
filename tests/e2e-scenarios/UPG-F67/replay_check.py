#!/usr/bin/env python3
"""UPG-F67 replay-fidelity check (merge gate, read-only). Ships as tests/e2e-scenarios/UPG-F67/replay_check.py.

Reconciles EVERY path of the old fork delta (``git diff --name-status <old-base> <old-head>``) against the candidate's
history: C1 (replay, parent = tag commit), C2 (authorized core re-home, parent = C1), HEAD. Per path:
  R      byte-identical to the old head (non-core at C1; core at C2)
  M      merged into a file the tag also changed; every fork-added line present (whitespace-stripped) or ledgered
  E      equal to the tag because the tag already carries every fork-added line, or ledger-declared equivalent
  RELOC  core code the tag moved: fork-added lines found in the path or its relocation targets at C2, or ledgered
  U-del  the tag deleted it and it is on the ruled list
Fails (exit 1) on DROPPED, DIVERGED, U-UNRULED, any fork-added line neither present nor ledgered, EXTRA paths in C1,
core edits in C1, unauthorized core in C2, and any core edit after C2 except WP-D1's two files with --wp-d1-approved.
Ledger TSV columns: path, stripped fork-added line, tag-site replacement as file:line.
"""
import argparse
import csv
import re
import subprocess
import sys

U_DEL = ("apps/desktop/e2e/bot-mode-closed-chat-stays-closed.spec.ts", "apps/desktop/e2e/glyph-spinner.spec.ts",
         "apps/desktop/e2e/group-to-local-bot-handoff.spec.ts", "apps/desktop/e2e/tile-unread-bug.spec.ts")
EQUIVALENT = ("tools/bot_mode_dm.py", "tests/tools/test_bot_mode_dm.py")
RELOCATED = {
    "run_agent.py": ("agent/session_persistence.py", "agent/turn_facade.py"),
    "hermes_cli/kanban_db.py": ("hermes_cli/kanban_db_connect.py", "hermes_cli/kanban_db_notify.py"),
    "gateway/kanban_watchers.py": ("gateway/kanban_watchers_notifier.py",),
    "gateway/platforms/api_server.py": ("gateway/platforms/api_server_openai_routes.py",),
}
C2_CORE = set(RELOCATED) | {t for ts in RELOCATED.values() for t in ts} | {"gateway/wake.py", "agent/turn_finalizer.py"}
WP_D1 = {"gateway/wake.py", "gateway/platforms/api_server_runs.py"}
SURFACE = re.compile(r"^(plugins/|website/docs/|tests/|apps/desktop/e2e/[^/]+-ac\d+\.spec\.ts$)")


def git(repo, *args, ok=(0,)):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if r.returncode not in ok:
        sys.exit(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def blob(repo, rev, path):
    return git(repo, "rev-parse", "-q", "--verify", f"{rev}:{path}", ok=(0, 1)).strip() or None


def text_at(repo, rev, path):
    return git(repo, "show", f"{rev}:{path}") if blob(repo, rev, path) else ""


def name_status(repo, a, b):
    out = []
    for line in git(repo, "diff", "--no-renames", "--name-status", a, b).splitlines():
        st, *paths = line.split("\t")
        out.append((st[0], paths[-1]))
    return out


def fork_added(repo, a, b, path):
    diff = git(repo, "diff", "--no-renames", a, b, "--", path)
    return [ln[1:].strip() for ln in diff.splitlines()
            if ln.startswith("+") and not ln.startswith("+++") and ln[1:].strip()]


def is_core(path, old_delta):
    """Outside the allowed diff surface. A fork-modified non-ac desktop spec in the old delta is old-gated, not core."""
    if SURFACE.match(path):
        return False
    return not (path.startswith("apps/desktop/e2e/") and path in old_delta)


def load_ledger(path, repo, tag):
    """Each row's third column must name a real tag-tree site (file present at the tag, line within it)."""
    if not path:
        return {}, []
    led, bad = {}, []
    with open(path, encoding="utf-8", newline="") as fh:
        for n, row in enumerate(csv.reader(fh, delimiter="\t"), 1):
            if not row or not row[0] or row[0].startswith("#"):
                continue
            site = row[2].strip() if len(row) >= 3 else ""
            file_, _, line = site.rpartition(":")
            lines = len(text_at(repo, tag, file_).splitlines()) if file_ else 0
            if not (line.isdigit() and 1 <= int(line) <= lines):
                bad.append(f"LEDGER row {n}: invalid tag site {site!r} for {row[0]}")
                continue
            led.setdefault(row[0], set()).add(row[1].strip())
    return led, bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ap.add_argument("--old-base", default="29112bef099274229cadff79cdff7bf7b99c4b77")
    ap.add_argument("--old-head", default="d0402936fdc15ec08bad72b5667655b8e509089d")
    ap.add_argument("--late-from", default="e4db7bca9d72681c9e358f8c4a195cd7ac0e8d39")
    ap.add_argument("--tag", default="f97608f178d1ffeca59860195ab7da295f7c8e5f")
    ap.add_argument("--replay", required=True, help="C1, the replay commit")
    ap.add_argument("--core", required=True, help="C2, the authorized core re-home commit")
    ap.add_argument("--head", required=True, help="the PR head")
    ap.add_argument("--ledger", default="", help="translation ledger TSV (path, stripped line, tag file:line)")
    ap.add_argument("--wp-d1-approved", action="store_true", help="operator approved WP-D1 (decision-msg170)")
    a = ap.parse_args()
    repo, fails, flagged = a.repo, [], []
    led, bad = load_ledger(a.ledger, repo, a.tag)
    fails.extend(bad)

    tag_c = git(repo, "rev-parse", f"{a.tag}^{{commit}}").strip()
    for child, parent, label in ((a.replay, tag_c, "C1^ == tag"), (a.core, a.replay, "C2^ == C1")):
        if git(repo, "rev-parse", f"{child}^").strip() != git(repo, "rev-parse", f"{parent}^{{commit}}").strip():
            fails.append(f"BASE: {label} does not hold")
    if subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", a.core, a.head]).returncode:
        fails.append("BASE: C2 is not an ancestor of the head")
    if git(repo, "rev-list", "--merges", f"{tag_c}..{a.head}").strip():
        fails.append("BASE: merge commits between the tag and the head (expected a linear history)")

    delta = name_status(repo, a.old_base, a.old_head)
    delta_paths = {p for _s, p in delta}
    late = {p for _s, p in name_status(repo, a.late_from, a.old_head)}
    counts = {}
    for st, path in delta:
        old, new, tag = (blob(repo, r, path) for r in (a.old_base, a.old_head, a.tag))
        core = is_core(path, delta_paths)
        if core and blob(repo, a.replay, path) != tag:
            fails.append(f"CORE-IN-C1: {path} differs from the tag at C1")
        at = a.core if core else a.replay
        cur = blob(repo, at, path)
        added = fork_added(repo, a.old_base, a.old_head, path)
        sites = [path, *RELOCATED.get(path, ())]
        text = {ln.strip() for s in sites for ln in text_at(repo, at, s).splitlines()}
        missing = [ln for ln in added if ln not in text and ln not in led.get(path, ())]
        if cur == new:
            cls, missing = "R", []
        elif cur is None and tag is None and old is not None:
            cls, missing = ("U-del" if path in U_DEL else "U-UNRULED"), []
        elif cur is None:
            cls = "DROPPED"
        elif path in RELOCATED:
            cls = "RELOC"
        elif cur == tag:
            cls = "E" if (not missing or path in EQUIVALENT) else "DROPPED"
            if path in EQUIVALENT:
                missing = []
        elif tag == old:
            cls = "DIVERGED"  # upstream left it alone, yet the candidate differs from the old head
        else:
            cls = "M"
        if missing:
            flagged.append((path, len(missing), missing[0][:100]))
            fails.append(f"UNACCOUNTED: {path}: {len(missing)} fork-added line(s) neither present nor ledgered")
        if cls in ("DROPPED", "DIVERGED", "U-UNRULED"):
            fails.append(f"{cls}: {path}")
        seg = "late" if path in late else "early"
        surf = "core" if core else ("old-gated" if path.startswith("apps/desktop/e2e/") and not SURFACE.match(path)
                                    else "in-surface")
        counts[(cls, surf, seg)] = counts.get((cls, surf, seg), 0) + 1
        print(f"{cls}\t{surf}\t{seg}\t{st}\t{path}")

    for _st, path in name_status(repo, a.tag, a.replay):
        if path not in delta_paths:
            fails.append(f"EXTRA in C1 (not in the old delta): {path}")
    for _st, path in name_status(repo, a.replay, a.core):
        if is_core(path, delta_paths) and path not in C2_CORE:
            fails.append(f"UNAUTHORIZED-CORE in C2: {path}")
    post = sorted({p for _s, p in name_status(repo, a.core, a.head)})
    for path in post:
        if is_core(path, delta_paths) and not (a.wp_d1_approved and path in WP_D1):
            fails.append(f"UNAUTHORIZED-CORE after C2: {path}" + (" (WP-D1 needs --wp-d1-approved)" if path in WP_D1 else ""))

    print("\n## summary (class, surface, segment) -> count")
    for k in sorted(counts):
        print(f"{k[0]:<10} {k[1]:<11} {k[2]:<6} {counts[k]}")
    print(f"\n## late segment {a.late_from[:10]}..{a.old_head[:10]}: {len(late)} paths, "
          f"{sum(v for k, v in counts.items() if k[2] == 'late' and k[0] in ('R', 'M', 'E', 'RELOC'))} reconciled")
    print(f"\n## unaccounted fork-added lines: {len(flagged)} path(s)")
    for path, n, first in flagged:
        print(f"  {path}: {n}, first: {first!r}")
    print(f"\n## commits after C2 touch {len(post)} paths (P4 judges each)")
    for p in post:
        print(f"  {'core' if is_core(p, delta_paths) else 'in-surface/old-gated'}\t{p}")
    if fails:
        print("\nFAIL\n" + "\n".join(fails))
        return 1
    print("\nOK: every delta path reconciled; no unaccounted fork line; nothing extra; core only where authorized")
    return 0


if __name__ == "__main__":
    sys.exit(main())
