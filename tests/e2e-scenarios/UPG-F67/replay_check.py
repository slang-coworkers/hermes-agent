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
core edits in C1, unauthorized core in C2, any core edit after C2 other than the approved WP-D1 patch (exact bytes, with
--wp-d1-approved), and any old-gated desktop line after C2 that is not a recorded import re-point (an E6 path may also
carry, after the E4 head, exactly the approved E6 patch).
Ledger TSV columns: path, stripped fork-added line, tag-site replacement as file:line.
Inventory TSV (--inventory): every delta path's status, surface, class and segment must equal this check's result.
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
WP_D1 = ("gateway/platforms/api_server.py", "gateway/platforms/api_server_runs.py", "gateway/wake.py")
WP_D1_PATCH_SHA256 = "5d7db2e0ea188b991d1e16d2138748230059a4b5d0ae25303929136ec76f7354"
# Pinned options: the diff must not depend on the caller's git config (algorithm, prefixes, inter-hunk merging).
DIFF_ARGS = ("diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames", "--no-relative", "--full-index",
             "-U0", "--inter-hunk-context=0", "--diff-algorithm=myers", "--indent-heuristic", "-O/dev/null",
             "--src-prefix=a/", "--dst-prefix=b/")
WP_D1_DIFF_CONFIG = ("-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false", "-c", "diff.algorithm=myers",
                     "-c", "diff.indentHeuristic=true", "-c", "diff.relative=false", "-c", "diff.interHunkContext=0",
                     "-c", "core.quotePath=true", "-c", "color.diff=false", "-c", "core.abbrev=10")
IMPORT_LINE = re.compile(r"^(import\b|export\s.*\sfrom\s|\}\s*from\s|.*\brequire\()")
# E2: a vitest module mock re-points with its import. A vi.mock row is admitted only when before and after differ
# in the module path alone; the rest of the line (factory included) must be byte-equal, so a factory edit is no row.
VI_MOCK_PATH = re.compile(r"""^vi\.mock\(\s*(['"])([^'"]+)\1(.*)$""")
# A tab-locator row may differ only in a lone `name:` option; the rest of the line (other options included) is byte-equal.
TAB_NAME = re.compile(r"""^(.*\bgetByRole\('tab', \{ name: )([^,]+?)( \}\).*)$""")
TAB_NAME_PATHS = ("apps/desktop/e2e/fleet-f62-ac10.helpers.ts",)
# E6: after the E4 head, the E6 paths may change only by the approved patch, byte for byte (same diff form as WP-D1).
E6_BASE = "11fac72938084cb4013388410a607b4b461ee5cd"
E6_PATHS = ("apps/desktop/e2e/fleet-f62-ac10.helpers.ts",)
E6_PATCH_SHA256 = "2724641592b24323366e68385dc4ee5d7fc33002d5f346c27efbb6367f50f648"
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


def wp_d1_diff(repo, a, b):
    """``git diff --no-ext-diff --no-renames -U0 a b -- <WP-D1 files>``, raw, with the user's diff config neutralised."""
    return git(repo, *WP_D1_DIFF_CONFIG, "diff", "--no-ext-diff", "--no-renames", "-U0", a, b, "--", *WP_D1)


def check_wp_d1(repo, core, head, patch_path):
    import hashlib
    with open(patch_path, "rb") as fh:
        approved = fh.read()
    if hashlib.sha256(approved).hexdigest() != WP_D1_PATCH_SHA256:
        return [f"UNAUTHORIZED-CORE: {patch_path} is not the approved WP-D1 patch (sha256 mismatch)"]
    got = wp_d1_diff(repo, core, head).encode("utf-8")
    if got == approved:
        return []
    first = next((i for i, (x, y) in enumerate(zip(got.splitlines(), approved.splitlines())) if x != y),
                 min(len(got.splitlines()), len(approved.splitlines())))
    return [f"UNAUTHORIZED-CORE after C2: WP-D1 diff differs from the approved patch at line {first + 1}"]


def load_repoints(path):
    """Old-gated desktop re-point record: path, import line before, import line after, reason."""
    rec, bad = {}, []
    if not path:
        return rec, bad
    with open(path, encoding="utf-8", newline="") as fh:
        for n, row in enumerate(csv.reader(fh, delimiter="\t"), 1):
            if not row or not row[0] or row[0].startswith("#"):
                continue
            p, before, after, reason = (row + [""] * 4)[:4]
            mocks = [VI_MOCK_PATH.match(ln.strip()) for ln in (before, after)]
            if any(mocks):
                if not (all(mocks) and mocks[0].group(3) == mocks[1].group(3) and mocks[0].group(2) != mocks[1].group(2)):
                    bad.append(f"REPOINT row {n}: a vi.mock row may change only the module path")
            elif any(tabs := [TAB_NAME.match(ln.strip()) for ln in (before, after)]):
                if not (p in TAB_NAME_PATHS and all(tabs) and tabs[0].group(1) == tabs[1].group(1)
                        and tabs[0].group(3) == tabs[1].group(3) and tabs[0].group(2) != tabs[1].group(2)):
                    bad.append(f"REPOINT row {n}: a tab-caption row may change only the locator's name")
            else:
                for ln in (before.strip(), after.strip()):
                    if ln and not IMPORT_LINE.match(ln):
                        bad.append(f"REPOINT row {n}: {ln!r} is not an import line")
            if not reason.strip():
                bad.append(f"REPOINT row {n}: no reason for {p}")
            r = rec.setdefault(p, (set(), set()))
            r[0].add(before.strip())
            r[1].add(after.strip())
    return rec, bad


def check_old_gated(repo, core, head, paths, rec):
    """Every line an old-gated desktop file changes after C2 is a recorded import re-point."""
    fails = []
    for p in paths:
        before, after = rec.get(p, (set(), set()))
        for ln in git(repo, *DIFF_ARGS, core, head, "--", p).splitlines():
            if ln.startswith(("---", "+++")) or not ln[:1] in "+-":
                continue
            body = ln[1:].strip()
            if body not in (before if ln[0] == "-" else after):
                fails.append(f"UNRECORDED-OLD-GATED: {p}: {ln[:120]!r}")
    return fails


def check_e6(repo, core, head, patch_path, rec):
    """E6 paths: C2..E6_BASE obeys the re-point record; E6_BASE..head is empty or the approved E6 patch, byte for byte."""
    import hashlib
    fails = check_old_gated(repo, core, E6_BASE, E6_PATHS, rec)
    got = git(repo, *WP_D1_DIFF_CONFIG, "diff", "--no-ext-diff", "--no-renames", "-U0", E6_BASE, head, "--",
              *E6_PATHS).encode("utf-8")
    if not got:
        return fails
    try:
        with open(patch_path, "rb") as fh:
            approved = fh.read()
    except OSError:
        return fails + [f"UNRECORDED-OLD-GATED: the E6 paths changed after the E4 head and {patch_path} is missing"]
    if hashlib.sha256(approved).hexdigest() != E6_PATCH_SHA256:
        return fails + [f"UNRECORDED-OLD-GATED: {patch_path} is not the approved E6 patch (sha256 mismatch)"]
    if got != approved:
        fails.append("UNRECORDED-OLD-GATED: the E6 paths' diff after the E4 head differs from the approved E6 patch")
    return fails


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


def check_inventory(path, observed):
    """Every delta row (status, path, surface, class, segment) equals the checker's own result, one row per path;
    ``(new)`` rows name only relocation targets or WP-D1 sites."""
    fails, seen, new_seen = [], {}, set()
    reloc_targets = {t for ts in RELOCATED.values() for t in ts}
    required_new = reloc_targets | {"gateway/platforms/api_server_runs.py", "gateway/platforms/api_server.py#run_internal_session_turn"}
    allowed_new = required_new | set(WP_D1)
    with open(path, encoding="utf-8", newline="") as fh:
        for n, row in enumerate(csv.reader(fh, delimiter="\t"), 1):
            if n == 1 or not row or not row[0]:
                continue
            st, p, surf, cls, seg = (row + [""] * 5)[:5]
            if st == "(new)":
                want = "RELOC-target" if p in reloc_targets else "WP-D1"
                if p not in allowed_new:
                    fails.append(f"INVENTORY row {n}: (new) path {p!r} is neither a relocation target nor a WP-D1 site")
                elif cls != want or surf != "core":
                    fails.append(f"INVENTORY row {n}: (new) {p} must be core/{want}, says {surf}/{cls}")
                new_seen.add(p)
                continue
            if p in seen:
                fails.append(f"INVENTORY row {n}: duplicate path {p}")
            seen[p] = (st, surf, cls, seg)
    for p, want in sorted(observed.items()):
        got = seen.pop(p, None)
        if got is None:
            fails.append(f"INVENTORY: missing row for {p}")
        elif got != want:
            fails.append(f"INVENTORY: {p} says {got}, checker found {want}")
    fails.extend(f"INVENTORY: row for a path outside the delta: {p}" for p in sorted(seen))
    fails.extend(f"INVENTORY: missing (new) row for {p}" for p in sorted(required_new - new_seen))
    return fails


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
    ap.add_argument("--inventory", required=True, help="path inventory TSV: status, path, surface, class, segment, evidence")
    ap.add_argument("--wp-d1-approved", action="store_true",
                    help="WP-D1 is integrated: the WP-D1 files' diff C2..head must equal --wp-d1-patch byte for byte")
    ap.add_argument("--wp-d1-patch", default="tests/e2e-scenarios/UPG-F67/wp_d1_approved.patch")
    ap.add_argument("--repoints", default="tests/e2e-scenarios/UPG-F67/desktop_repoints.tsv",
                    help="old-gated desktop import re-point record: path, import before, import after, reason")
    ap.add_argument("--e6-patch", default="tests/e2e-scenarios/UPG-F67/e6_approved.patch",
                    help="the approved E6 patch for the E6 paths after the E4 head")
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
    counts, observed = {}, {}
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
            cls = "DIVERGED"
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
        observed[path] = (st, surf, cls, seg)
        print(f"{cls}\t{surf}\t{seg}\t{st}\t{path}")

    for _st, path in name_status(repo, a.tag, a.replay):
        if path not in delta_paths:
            fails.append(f"EXTRA in C1 (not in the old delta): {path}")
    for _st, path in name_status(repo, a.replay, a.core):
        if is_core(path, delta_paths) and path not in C2_CORE:
            fails.append(f"UNAUTHORIZED-CORE in C2: {path}")
    post = sorted({p for _s, p in name_status(repo, a.core, a.head)})
    old_gated = []
    for path in post:
        if not is_core(path, delta_paths):
            if path.startswith("apps/desktop/e2e/") and not SURFACE.match(path):
                old_gated.append(path)
            continue
        if not (a.wp_d1_approved and path in WP_D1):
            fails.append(f"UNAUTHORIZED-CORE after C2: {path}" + (" (WP-D1 needs --wp-d1-approved)" if path in WP_D1 else ""))
    if a.wp_d1_approved:
        fails.extend(check_wp_d1(repo, a.core, a.head, a.wp_d1_patch))
    rec, rec_bad = load_repoints(a.repoints if old_gated else "")
    fails.extend(rec_bad)
    e6 = [p for p in old_gated if p in E6_PATHS]
    on_e6 = bool(e6) and not subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", E6_BASE, a.head]).returncode
    fails.extend(check_old_gated(repo, a.core, a.head, [p for p in old_gated if not (on_e6 and p in E6_PATHS)], rec))
    if on_e6:
        fails.extend(check_e6(repo, a.core, a.head, a.e6_patch, rec))

    inv_fails = check_inventory(a.inventory, observed)
    fails.extend(inv_fails)

    print("\n## summary (class, surface, segment) -> count")
    for k in sorted(counts):
        print(f"{k[0]:<10} {k[1]:<11} {k[2]:<6} {counts[k]}")
    print(f"\n## late segment {a.late_from[:10]}..{a.old_head[:10]}: {len(late)} paths, "
          f"{sum(v for k, v in counts.items() if k[2] == 'late' and k[0] in ('R', 'M', 'E', 'RELOC'))} reconciled")
    print(f"\n## unaccounted fork-added lines: {len(flagged)} path(s)")
    for path, n, first in flagged:
        print(f"  {path}: {n}, first: {first!r}")
    print(f"\n## inventory: {len(observed) - sum(1 for f in inv_fails if f.startswith('INVENTORY: ') and 'outside' not in f)}"
          f"/{len(observed)} delta paths matched, {len(inv_fails)} mismatch(es)")
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
