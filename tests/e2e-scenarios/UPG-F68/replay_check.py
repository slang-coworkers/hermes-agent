#!/usr/bin/env python3
"""UPG-F68 replay-fidelity check (merge gate 2, read-only). Ships as tests/e2e-scenarios/UPG-F68/replay_check.py.

The candidate is the selected tag + C1 (the carry of PR #79 head H5 onto the tag) + the ``-x`` picks + in-surface
commits. Checks, each named after ADR §Merge gate 2:
  (a) C1^ == tag, linear history; every H5 delta path (``git diff --name-status <9.24> <H5>``) is R / M / M-conflict /
      E / U-del at C1, C1 adds no other path, and every H5-added line (whitespace-stripped) of an in-surface or
      old-gated path is present at C1 or ledgered.
  (b) each pick is exactly one ``-x`` commit, in order, whose diff equals its source's diff path by path.
  (c) per-function replay of every H5 core file: in each scope the H5 diff touches, for every stripped line L,
      count@C1(L) == count@tag(L) + count@H5(L) - count@9.24(L), less ledgered translations. The H5 core delta is
      decomposed into the GOV-F25 re-home (9.24..C2_F67), WP-D1 (C2_F67..WP_D1_F67, equal to the approved patch
      bytes) and nothing else; MSG-DELIV is E and F62.g HELD (tools/bot_mode_dm.py equals the tag).
  (d) the A1 union rule for ``_kanban_sub_op`` (the same equation over the whole function), with its three locators.
  (e) no core path changes after C1 (item 4, the only later core, is not in this candidate).
  (f) an old-gated desktop file changes after C1 only by a recorded import re-point.
--inventory: one row per path (plus the ``path#scope`` core rows); status, surface, class and origin must equal this
check's result, core rows name their authority, conflict rows carry locators that resolve.
"""
import argparse
import ast
import csv
import hashlib
import os
import re
import subprocess
import sys
from collections import Counter

NINE24 = "f97608f178d1ffeca59860195ab7da295f7c8e5f"
C2_F67 = "f602f80b4a"  # UPG-F67 C2: GOV-F25 + GOV-F25.b re-homed onto 9.24 (an ancestor of H5)
WP_D1_F67 = "0cd8762e2b"  # UPG-F67 WP-D1 (operator-approved), the commit whose core diff is the approved patch
WP_D1_FILES = ("gateway/platforms/api_server.py", "gateway/platforms/api_server_runs.py", "gateway/wake.py")
WP_D1_PATCH = "tests/e2e-scenarios/UPG-F67/wp_d1_approved.patch"
WP_D1_PATCH_SHA256 = "5d7db2e0ea188b991d1e16d2138748230059a4b5d0ae25303929136ec76f7354"
WP_D1_SCOPES = {
    "gateway/platforms/api_server.py": "APIServerAdapter.run_internal_session_turn",
    "gateway/platforms/api_server_runs.py": "run_internal_session_turn",
    "gateway/wake.py": "_self_post_chat_completion",
}
A1 = ("gateway/kanban_watchers.py", "GatewayKanbanWatchersMixin._kanban_sub_op")
A2 = ("gateway/platforms/api_server_openai_routes.py", "OpenAICompatRoutesMixin._run_idempotent")
C1_CONFLICTS = ("gateway/kanban_watchers.py", "gateway/platforms/api_server_openai_routes.py",
                "tests/hermes_cli/test_config_read_guard.py", "tests/hermes_cli/test_kanban_notify.py",
                "website/docs/user-guide/configuration.md")
PICK_CONFLICTS = {("1fb4d74ccb", "plugins/nv-coworker-compose/openshell/installer.py")}
# Authority per H5 core path (ADR §CORE-CHANGE A/B); an inventory row may name only these.
CORE_AUTHORITY = {
    "agent/session_persistence.py": {"GOV-F25"},
    "agent/turn_facade.py": {"GOV-F25"},
    "agent/turn_finalizer.py": {"GOV-F25"},
    "gateway/kanban_watchers.py": {"GOV-F25", "GOV-F25.b"},
    "gateway/kanban_watchers_notifier.py": {"GOV-F25", "GOV-F25.b"},
    "gateway/platforms/api_server.py": {"GOV-F25", "GOV-F25.b", "WP-D1"},
    "gateway/platforms/api_server_openai_routes.py": {"GOV-F25", "GOV-F25.b"},
    "gateway/platforms/api_server_runs.py": {"WP-D1"},
    "gateway/wake.py": {"GOV-F25", "GOV-F25.b", "WP-D1"},
    "hermes_cli/kanban_db.py": {"GOV-F25", "GOV-F25.b"},
    "hermes_cli/kanban_db_connect.py": {"GOV-F25", "GOV-F25.b"},
    "hermes_cli/kanban_db_notify.py": {"GOV-F25", "GOV-F25.b"},
}
# Retained core with no hunk at the candidate (ADR §CORE-CHANGE C/D): the path must equal the tag.
RETAINED = {
    "tools/bot_mode_dm.py#MSG-DELIV": ("tools/bot_mode_dm.py", "E", "MSG-DELIV"),
    "tools/bot_mode_dm.py#F62.g": ("tools/bot_mode_dm.py", "HELD", "F62.g"),
}
U_DEL = ()
SURFACE = re.compile(r"^(plugins/|website/docs/|tests/|apps/desktop/e2e/[^/]+-ac\d+\.spec\.ts$)")
ITEM = re.compile(r"\[UPG-F68 (\d+)\]")
CHERRY = re.compile(r"^\(cherry picked from commit ([0-9a-f]{40})\)$", re.MULTILINE)
LOCATOR = re.compile(r"^(tag|H5|C1|HEAD|[0-9a-f]{10,40}):([^:]+):(\d+)-(\d+)$")
IMPORT_LINE = re.compile(r"^(import\b|export\s.*\sfrom\s|\}\s*from\s|.*\brequire\()")
DIFF_ARGS = ("-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false", "-c", "core.quotePath=true",
             "-c", "color.diff=false", "-c", "core.abbrev=10", "diff", "--no-ext-diff", "--no-textconv", "--no-color",
             "--no-renames", "--no-relative", "-U0", "--diff-algorithm=myers")


def git(repo, *args, ok=(0,)):
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, encoding="utf-8", check=False,
                       errors="surrogateescape", stdin=subprocess.DEVNULL)
    if r.returncode not in ok:
        sys.exit(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def blob(repo, rev, path):
    return git(repo, "rev-parse", "-q", "--verify", f"{rev}:{path}", ok=(0, 1)).strip() or None


_TEXT = {}


def text_at(repo, rev, path):
    if (rev, path) not in _TEXT:
        _TEXT[(rev, path)] = git(repo, "show", f"{rev}:{path}") if blob(repo, rev, path) else ""
    return _TEXT[(rev, path)]


def name_status(repo, a, b):
    out = {}
    for line in git(repo, "diff", "--no-renames", "--name-status", a, b).splitlines():
        st, *paths = line.split("\t")
        out[paths[-1]] = st[0]
    return out


def diff_lines(repo, a, b, path):
    """(+lines, -lines) of ``git diff -U0 a b -- path``, raw (no strip), with old/new line numbers."""
    plus, minus, old_no, new_no = [], [], 0, 0
    for ln in git(repo, *DIFF_ARGS, a, b, "--", path).splitlines():
        if ln.startswith("@@"):
            m = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", ln)
            old_no, new_no = int(m.group(1)), int(m.group(2))
        elif ln.startswith(("---", "+++", "diff ", "index ", "new file", "deleted file", "\\")):
            continue
        elif ln.startswith("+"):
            plus.append((new_no, ln[1:]))
            new_no += 1
        elif ln.startswith("-"):
            minus.append((old_no, ln[1:]))
            old_no += 1
    return plus, minus


def is_core(path, h5_paths):
    """Outside the allowed diff surface. A fork-modified non-ac desktop file of the H5 delta is old-gated, not core."""
    if SURFACE.match(path):
        return False
    return not (path.startswith("apps/desktop/e2e/") and path in h5_paths)


def surface_of(path, h5_paths):
    if is_core(path, h5_paths):
        return "core"
    return "old-gated" if path.startswith("apps/desktop/e2e/") and not SURFACE.match(path) else "in-surface"


_SCOPES = {}


def scope_map(text):
    """Line number (1-based) -> innermost enclosing def/class qualname; ``<module>`` outside any."""
    if text in _SCOPES:
        return _SCOPES[text]
    tree = ast.parse(text)
    owner = {}

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                start = min([child.lineno, *(d.lineno for d in child.decorator_list)])
                for n in range(start, child.end_lineno + 1):
                    owner[n] = name
                walk(child, name + ".")
            else:
                walk(child, prefix)

    walk(tree, "")
    _SCOPES[text] = owner
    return owner


def scope_lines(text, scope):
    """Stripped non-empty lines of one scope (``<module>`` = lines outside every def/class)."""
    owner = scope_map(text) if text else {}
    out = Counter()
    for n, ln in enumerate(text.splitlines(), 1):
        if ln.strip() and owner.get(n, "<module>") == scope:
            out[ln.strip()] += 1
    return out


def scope_span(text, scope):
    lines = [n for n, s in scope_map(text).items() if s == scope] if text else []
    return (min(lines), max(lines)) if lines else None


def delta(new, old):
    keys = set(new) | set(old)
    return {k: new[k] - old[k] for k in keys if new[k] != old[k]}


def touched_scopes(repo, a, b, path):
    """Scopes the diff a..b touches: + lines by their scope at b, - lines by their scope at a."""
    plus, minus = diff_lines(repo, a, b, path)
    at_b, at_a = scope_map(text_at(repo, b, path) or "\n"), scope_map(text_at(repo, a, path) or "\n")
    return ({at_b.get(n, "<module>") for n, ln in plus if ln.strip()}
            | {at_a.get(n, "<module>") for n, ln in minus if ln.strip()})


def replay_scope(repo, revs, path, scope):
    """The 3-way-merge invariant per scope: C1 == tag + (H5 - 9.24), split into the GOV-F25 and WP-D1 parts.
    Returns (gov, wpd1, residual) as signed line deltas; the residual must be empty."""
    t = {k: scope_lines(text_at(repo, r, path), scope) for k, r in revs.items()}
    gov = delta(t["c2_f67"], t["nine24"])
    wpd1 = delta(t["wpd1_f67"], t["c2_f67"])
    got = delta(t["c1"], t["tag"])
    want = Counter()
    for d in (gov, wpd1):
        for k, v in d.items():
            want[k] += v
    residual = {k: got.get(k, 0) - want.get(k, 0) for k in set(got) | set(want) if got.get(k, 0) != want.get(k, 0)}
    return gov, wpd1, residual


def check_core(repo, revs, core_paths, allowed, out):
    """(c). Returns fails and the observed ``path#scope`` inventory rows. ``allowed`` maps (path, scope) to the
    residual a ruled hand resolution leaves (A2); every other scope must replay with no residual."""
    fails, rows = [], {}
    approved = open(git(repo, "rev-parse", "--show-toplevel").strip() + "/" + WP_D1_PATCH, "rb").read()
    if hashlib.sha256(approved).hexdigest() != WP_D1_PATCH_SHA256:
        fails.append(f"WP-D1: {WP_D1_PATCH} is not the approved patch (sha256 mismatch)")
    got = git(repo, *DIFF_ARGS, revs["c2_f67"], revs["wpd1_f67"], "--", *WP_D1_FILES).encode("utf-8")
    if got != approved:
        fails.append(f"WP-D1: diff {C2_F67}..{WP_D1_F67} over the WP-D1 files is not the approved patch, byte for byte")
    for path in core_paths:
        tail = name_status(repo, revs["wpd1_f67"], revs["h5"])
        if path in tail:
            fails.append(f"CORE-UNAUTHORIZED: {path} changes between WP-D1 ({WP_D1_F67}) and H5")
        if git(repo, "diff", "--name-only", revs["wpd1_f67"], revs["c2_f67"], "--", path).strip() and \
                path not in WP_D1_FILES:
            fails.append(f"CORE-UNAUTHORIZED: {path} changes in WP-D1 but is not a WP-D1 file")
        scopes = touched_scopes(repo, revs["nine24"], revs["h5"], path)
        for scope in sorted(scopes):
            gov, wpd1, residual = replay_scope(repo, revs, path, scope)
            auth = ",".join(a for a, d in (("GOV-F25", gov), ("WP-D1", wpd1)) if d)
            if wpd1 and WP_D1_SCOPES.get(path) != scope:
                fails.append(f"WP-D1 outside its scope: {path}#{scope}")
            if residual != allowed.get((path, scope), {}):
                first = sorted(residual.items())[0] if residual else ("(missing ruled residual)", 0)
                fails.append(f"CORE-REPLAY: {path}#{scope}: {len(residual)} line(s) differ from tag + H5 delta, "
                             f"first {first[1]:+d} {first[0][:100]!r}")
            rows[f"{path}#{scope}"] = ("M", "core", "replay", "H5", auth or "none")
            print(f"core\t{path}#{scope}\tgov={sum(map(abs, gov.values()))}\twpd1={sum(map(abs, wpd1.values()))}"
                  f"\tresidual={sum(map(abs, residual.values()))}", file=out)
        whole = {k: text_at(repo, r, path) for k, r in revs.items()}
        exp = Counter()
        for k, sign in (("tag", 1), ("h5", 1), ("nine24", -1)):
            for ln in whole[k].splitlines():
                if ln.strip():
                    exp[ln.strip()] += sign
        for (ap, _scope), res in allowed.items():
            if ap == path:
                for k, v in res.items():
                    exp[k] += v
        have = Counter(ln.strip() for ln in whole["c1"].splitlines() if ln.strip())
        if {k: v for k, v in exp.items() if v} != {k: v for k, v in have.items() if v}:
            fails.append(f"CORE-REPLAY: {path}: whole-file line multiset != tag + (H5 - 9.24)")
    return fails, rows


def check_a1(repo, revs, out):
    """(d): ``_kanban_sub_op`` at C1 is exactly the tag body plus GOV-F25's change; prints the three locators."""
    path, scope = A1
    loc = {k: scope_span(text_at(repo, revs[k], path), scope) for k in ("tag", "h5", "c1")}
    for k, label in (("tag", "upstream @tag"), ("h5", "GOV-F25 @H5"), ("c1", "resolved @C1")):
        print(f"A1 locator {label}: {path}:{loc[k][0]}-{loc[k][1]}" if loc[k] else f"A1 locator {label}: MISSING",
              file=out)
    gov, wpd1, residual = replay_scope(repo, revs, path, scope)
    fails = []
    if residual or wpd1 or not loc["c1"]:
        fails.append(f"A1: {path}#{scope} is not exactly the union of the tag body and GOV-F25's change")
    tag_txt, c1_txt = text_at(repo, revs["tag"], path), text_at(repo, revs["c1"], path)
    t_span, c_span = scope_span(tag_txt, scope), scope_span(c1_txt, scope)
    t_body = tag_txt.splitlines()[t_span[0] - 1:t_span[1]] if t_span else []
    c_body = c1_txt.splitlines()[c_span[0] - 1:c_span[1]] if c_span else []
    union = [ln.replace("**extra: Any) -> None:", "**extra: Any) -> Any:", 1).replace(
        "getattr(_kbn, op)(", "return getattr(_kbn, op)(", 1) for ln in t_body]
    if c_body != union:
        fails.append("A1: _kanban_sub_op at C1 is not byte-for-byte the tag body with GOV-F25's `-> Any` and `return`")
    pin = "with _kb.pin_first_board_resolution():"
    body = scope_lines(text_at(repo, revs["c1"], path), scope)
    if not body[pin] or not body["return getattr(_kbn, op)("]:
        fails.append("A1: the env-pin wrap or GOV-F25's return is missing at C1")
    for k, v in sorted(gov.items()):
        print(f"A1 GOV-F25 line {v:+d}: {k}", file=out)
    return fails


def check_a2(repo, revs, out):  # -> (fails, allowed residual)
    """A2: the only hand-resolved line of ``_run_idempotent`` is the tag's PEP 585 signature line plus the kwarg."""
    path, scope = A2
    tag_sig = "log_label: str, fingerprint_keys: list[str], route: str) -> tuple:"
    want = tag_sig.replace("route: str)", "route: str, require_persist: bool = False)")
    gov, wpd1, residual = replay_scope(repo, revs, path, scope)
    h5_sig = next((k for k, v in gov.items() if v > 0 and "require_persist: bool = False)" in k), None)
    old_sig = next((k for k, v in gov.items() if v < 0 and k.startswith("log_label:")), None)
    allowed = {want: 1, tag_sig: -1}
    if h5_sig:
        allowed[h5_sig] = allowed.get(h5_sig, 0) - 1
    if old_sig:
        allowed[old_sig] = allowed.get(old_sig, 0) + 1
    span = scope_span(text_at(repo, revs["c1"], path), scope)
    print(f"A2 locators: upstream @tag {path}:{scope_span(text_at(repo, revs['tag'], path), scope)}, "
          f"GOV-F25.b @H5 {path}:{scope_span(text_at(repo, revs['h5'], path), scope)}, resolved @C1 {path}:{span}",
          file=out)
    allowed = {k: v for k, v in allowed.items() if v}
    if residual == allowed and not wpd1 and h5_sig and old_sig:
        return [], {(path, scope): allowed}
    return [f"A2: {path}#{scope} differs from tag + GOV-F25.b by more than the signature line: "
            f"{sorted(residual.items())[:4]}"], {(path, scope): allowed}


def load_ledger(path, repo, revs):
    """In-surface translations: path, stripped H5-added line, site locator(s) (``tag:`` / ``C1:``) where the line's
    content lives at C1, reason. Every site must resolve."""
    led, bad = {}, []
    if not path:
        return led, bad
    with open(path, encoding="utf-8", newline="") as fh:
        for n, row in enumerate(csv.reader(fh, delimiter="\t"), 1):
            if not row or not row[0] or row[0].startswith("#"):
                continue
            p, h5_line, sites, reason = (row + [""] * 4)[:4]
            if not (h5_line.strip() and sites.strip() and reason.strip()) or locators_resolve(repo, sites, revs):
                bad.append(f"LEDGER row {n}: needs an H5 line, resolving tag:/C1: site locator(s) and a reason")
                continue
            led.setdefault(p, set()).add(h5_line.strip())
    return led, bad


def commits_after(repo, base, head):
    out = []
    for sha in git(repo, "rev-list", "--reverse", f"{base}..{head}").split():
        msg = git(repo, "log", "-1", "--format=%B", sha)
        src = CHERRY.findall(msg)
        item = ITEM.search(msg.splitlines()[0] if msg else "")
        out.append((sha, src[-1] if src else None, int(item.group(1)) if item else None))
    return out


def multiset_diff(repo, a, b, path):
    plus, minus = diff_lines(repo, a, b, path)
    return Counter(ln.strip() for _n, ln in plus if ln.strip()), Counter(ln.strip() for _n, ln in minus if ln.strip())


def locators_resolve(repo, cell, revs):
    """Each locator ``<rev>:<path>:<a>-<b>`` names a line range that exists at that revision."""
    bad = []
    for tok in cell.split():
        m = LOCATOR.match(tok)
        rev = m and {"tag": revs["tag"], "H5": revs["h5"], "C1": revs["c1"], "HEAD": revs["head"]}.get(m.group(1), m.group(1))
        n = len(text_at(repo, rev, m.group(2)).splitlines()) if m else 0
        if not (m and 1 <= int(m.group(3)) <= int(m.group(4)) <= n):
            bad.append(tok)
    return bad


def check_h5_paths(repo, revs, h5_ns, led, out):
    """(a) per H5 delta path at C1. Returns fails, observed {path: (status, surface, class)}, unaccounted list."""
    fails, observed, flagged = [], {}, []
    h5_paths = set(h5_ns)
    for path in sorted(h5_ns):
        st = h5_ns[path]
        cur, new, tag, old = (blob(repo, revs[k], path) for k in ("c1", "h5", "tag", "nine24"))
        surf = surface_of(path, h5_paths)
        if cur == new:
            cls = "R"
        elif cur is None and tag is None and old is not None:
            cls = "U-del" if path in U_DEL else "U-UNRULED"
        elif cur is None:
            cls = "DROPPED"
        elif cur == tag:
            cls = "E"
        elif path in C1_CONFLICTS:
            cls = "M-conflict"
        elif tag == old:
            cls = "DIVERGED"
        else:
            cls = "M"
        if cls in ("DROPPED", "DIVERGED", "U-UNRULED"):
            fails.append(f"{cls}: {path}")
        if surf != "core" and cls in ("M", "M-conflict", "E"):
            have = Counter(ln.strip() for ln in text_at(repo, revs["c1"], path).splitlines() if ln.strip())
            added, _removed = multiset_diff(repo, revs["nine24"], revs["h5"], path)
            missing = [ln for ln in added if have[ln] < 1 and ln not in led.get(path, ())]
            if missing:
                flagged.append((path, len(missing), missing[0][:100]))
                fails.append(f"UNACCOUNTED: {path}: {len(missing)} H5-added line(s) neither at C1 nor ledgered")
        observed[path] = (st, surf, cls, "H5")
        print(f"{cls}\t{surf}\t{st}\t{path}", file=out)
    return fails, observed, flagged


def check_picks(repo, picks, after_c1):
    """(b): picks land in order, each as exactly one ``-x`` commit whose diff equals its source's diff."""
    fails, landed = [], {}
    for sha, src, _item in after_c1:
        if src:
            landed.setdefault(src, []).append(sha)
    order = []
    for p in picks:
        full = git(repo, "rev-parse", f"{p}^{{commit}}").strip()
        got = landed.get(full, [])
        if len(got) != 1:
            fails.append(f"PICK: {p} lands {len(got)} time(s) as a -x commit (want exactly 1)")
            continue
        order.append(got[0])
        mine, theirs = name_status(repo, f"{got[0]}^", got[0]), name_status(repo, f"{full}^", full)
        if mine != theirs:
            fails.append(f"PICK: {p}: path set/status differs from its source")
        for path in sorted(set(mine) & set(theirs)):
            # A recorded conflict (PICK_CONFLICTS) was resolved by keeping both sides byte for byte, so its line
            # multiset must still equal the source's; only the hunk position may differ.
            if multiset_diff(repo, f"{got[0]}^", got[0], path) != multiset_diff(repo, f"{full}^", full, path):
                fails.append(f"PICK: {p}: {path} diff differs from the source's")
    shas = [s for s, _src, _i in after_c1]
    if order != sorted(order, key=shas.index):
        fails.append("PICK: picks are not in the order " + " -> ".join(picks))
    return fails


def origin_of(sha, src, item, picks):
    if src:
        return "#" + {"b41d14df02": "78", "1fb4d74ccb": "77", "35767fe243": "80", "0615c1e7a0": "82"}.get(src[:10], src[:10])
    return "new" if item == 1 else (f"item-{item}" if item else "untagged")


def check_inventory(path, observed, core_rows, post_origins, needs_locators, repo, revs):
    """One row per observed path and core scope: status, surface, class and origin equal the checker's; ``post_c1``
    lists the origins of the commits after C1 that touch the path; core rows name a matching authority; conflict
    rows carry three resolving locators (upstream side, fork side, resolved hunk)."""
    fails, seen = [], {}
    with open(path, encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh, delimiter="\t"))
    want_head = ["status", "path", "surface", "class", "origin", "authority", "locators", "post_c1"]
    if not rows or rows[0][:8] != want_head:
        return [f"INVENTORY: header must be {'/'.join(want_head)}"]
    for n, row in enumerate(rows[1:], 2):
        if not row or not row[0] or row[0].startswith("#"):
            continue
        st, p, surf, cls, origin, auth, locs, post = (row + [""] * 8)[:8]
        if p in seen:
            fails.append(f"INVENTORY row {n}: duplicate {p}")
        seen[p] = (st, surf, cls, origin, auth, locs, post)
    want = dict(observed)
    want.update(core_rows)
    for p, w in sorted(want.items()):
        got = seen.pop(p, None)
        if got is None:
            fails.append(f"INVENTORY: missing row for {p}")
            continue
        if got[:4] != tuple(w[:4]):
            fails.append(f"INVENTORY: {p} says {got[:4]}, checker found {tuple(w[:4])}")
        if got[6] != ",".join(post_origins.get(p, [])):
            fails.append(f"INVENTORY: {p} post_c1 says {got[6]!r}, checker found {','.join(post_origins.get(p, []))!r}")
        base = p.split("#")[0]
        names = {a for a in got[4].split(",") if a}
        if w[1] == "core":
            if not names:
                fails.append(f"INVENTORY: core row {p} names no authority")
            elif base in CORE_AUTHORITY and not names <= CORE_AUTHORITY[base]:
                fails.append(f"INVENTORY: {p} names authority {got[4]!r}, allowed {sorted(CORE_AUTHORITY[base])}")
            elif len(w) > 4 and w[4] is not None:
                found = set(w[4].split(","))
                mapped = {"GOV-F25" if a.startswith("GOV-F25") else a for a in names}
                if mapped != found:
                    fails.append(f"INVENTORY: {p} authority {got[4]!r} does not match the replayed parts {sorted(found)}")
        if p in needs_locators:
            toks = got[5].split()
            bad = locators_resolve(repo, got[5], revs) if toks else ["(none)"]
            if bad or len(toks) < 3:
                fails.append(f"INVENTORY: {p} needs 3 resolving locators (upstream, fork side, resolved); bad: {bad}")
    fails.extend(f"INVENTORY: row for a path the checker did not observe: {p}" for p in sorted(seen))
    return fails


def write_inventory(path, observed, core_rows, post_origins):
    """Regenerate the checker-derived columns; the hand-authored authority and locators columns are kept."""
    keep = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8", newline="") as fh:
            for row in list(csv.reader(fh, delimiter="\t"))[1:]:
                if row and row[0] and not row[0].startswith("#"):
                    keep[row[1]] = ((row + [""] * 8)[5], (row + [""] * 8)[6])
    rows = []
    for p, w in sorted({**observed, **core_rows}.items()):
        auth, locs = keep.get(p, ("", ""))
        if not auth and w[1] == "core" and len(w) > 4:
            auth = w[4]
        rows.append([w[0], p, w[1], w[2], w[3], auth, locs, ",".join(post_origins.get(p, []))])
    with open(path, "w", encoding="utf-8", newline="") as fh:
        wr = csv.writer(fh, delimiter="\t", lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
        wr.writerow(["status", "path", "surface", "class", "origin", "authority", "locators", "post_c1"])
        wr.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    ap.add_argument("--tag", default="e06df9c2eae6901f4c88f464e0ae6e753c0428b8")
    ap.add_argument("--h5", default="a2afed6dfa21cb0be1fd60930c917f3f3060d0d3")
    ap.add_argument("--c1", required=True, help="the carry commit, parent = tag")
    ap.add_argument("--head", required=True, help="the candidate head")
    ap.add_argument("--picks", default="b41d14df02,1fb4d74ccb,35767fe243", help="the -x picks, in order")
    ap.add_argument("--inventory", required=True, help="path inventory TSV")
    ap.add_argument("--ledger", default="tests/e2e-scenarios/UPG-F68/replay_ledger.tsv",
                    help="in-surface translation ledger TSV: path, H5 line, tag:/C1: site locator(s), reason")
    ap.add_argument("--repoints", default="", help="old-gated desktop import re-point TSV (optional)")
    ap.add_argument("--verbose", action="store_true", help="print one line per path and per core scope")
    ap.add_argument("--write-inventory", action="store_true",
                    help="rewrite --inventory from this check's observations, keeping each row's authority and locators")
    a = ap.parse_args()
    repo = a.repo
    out = sys.stdout if a.verbose else open("/dev/null", "w", encoding="utf-8")
    revs = {k: git(repo, "rev-parse", f"{v}^{{commit}}").strip() for k, v in
            (("tag", a.tag), ("h5", a.h5), ("c1", a.c1), ("head", a.head), ("nine24", NINE24),
             ("c2_f67", C2_F67), ("wpd1_f67", WP_D1_F67))}
    fails = []
    if git(repo, "rev-parse", f"{revs['c1']}^").strip() != revs["tag"]:
        fails.append("BASE: C1^ is not the tag commit")
    if subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", revs["c1"], revs["head"]], check=False,
                      stdin=subprocess.DEVNULL).returncode:
        fails.append("BASE: C1 is not an ancestor of the head")
    if git(repo, "rev-list", "--merges", f"{revs['tag']}..{revs['head']}").strip():
        fails.append("BASE: merge commits between the tag and the head (expected a linear history)")
    for k in ("nine24", "c2_f67", "wpd1_f67"):
        if subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", revs[k], revs["h5"]], check=False,
                          stdin=subprocess.DEVNULL).returncode:
            fails.append(f"BASE: {k} is not an ancestor of H5")

    led, bad = load_ledger(a.ledger if os.path.exists(a.ledger) else "", repo, revs)
    fails.extend(bad)
    h5_ns = name_status(repo, revs["nine24"], revs["h5"])
    h5_paths = set(h5_ns)
    f_a, observed, flagged = check_h5_paths(repo, revs, h5_ns, led, out)
    fails.extend(f_a)
    extra = sorted(set(name_status(repo, revs["tag"], revs["c1"])) - h5_paths)
    fails.extend(f"EXTRA in C1 (not an H5 delta path): {p}" for p in extra)

    core_paths = sorted(p for p in h5_paths if is_core(p, h5_paths))
    f_a2, allowed = check_a2(repo, revs, out)
    f_c, core_rows = check_core(repo, revs, core_paths, allowed, out)
    fails.extend(f_a2 + f_c + check_a1(repo, revs, out))
    for key, (path, cls, auth) in RETAINED.items():
        if blob(repo, revs["head"], path) != blob(repo, revs["tag"], path):
            fails.append(f"{auth}: {path} differs from the tag at the head ({cls} until item 4)")
        core_rows[key] = ("(retained)", "core", cls, "retained", auth)

    after_c1 = commits_after(repo, revs["c1"], revs["head"])
    fails.extend(check_picks(repo, [p.strip() for p in a.picks.split(",") if p.strip()], after_c1))
    post = name_status(repo, revs["c1"], revs["head"])
    for path in sorted(post):
        if is_core(path, h5_paths):
            fails.append(f"CORE-AFTER-C1: {path} (no later core is authorized in this candidate)")
    rec = {}
    if a.repoints:
        with open(a.repoints, encoding="utf-8", newline="") as fh:
            for n, row in enumerate(csv.reader(fh, delimiter="\t"), 1):
                if row and row[0] and not row[0].startswith("#"):
                    p, before, after, reason = (row + [""] * 4)[:4]
                    if not reason.strip() or not all(IMPORT_LINE.match(x.strip()) for x in (before, after) if x.strip()):
                        fails.append(f"REPOINT row {n}: import lines only, with a reason")
                    r = rec.setdefault(p, (set(), set()))
                    r[0].add(before.strip())
                    r[1].add(after.strip())
    for path in sorted(post):
        if surface_of(path, h5_paths) == "old-gated":
            before, after = rec.get(path, (set(), set()))
            plus, minus = multiset_diff(repo, revs["c1"], revs["head"], path)
            stray = [ln for ln in plus if ln not in after] + [ln for ln in minus if ln not in before]
            if stray:
                fails.append(f"OLD-GATED after C1: {path}: {len(stray)} unrecorded line(s), first {stray[0][:100]!r}")
    post_origins = {}
    for sha, src, item in after_c1:
        for p in name_status(repo, f"{sha}^", sha):
            o = origin_of(sha, src, item, a.picks)
            if o not in post_origins.setdefault(p, []):
                post_origins[p].append(o)
            if o == "untagged":
                fails.append(f"COMMIT {sha[:10]}: neither a -x pick nor tagged [UPG-F68 <item>]")
    observed_all = dict(observed)
    for p in post:
        if p not in h5_paths:
            st = "A" if blob(repo, revs["c1"], p) is None else "M"
            st = st if blob(repo, revs["head"], p) else "D"
            observed_all[p] = (st, surface_of(p, h5_paths), "post-C1", post_origins.get(p, ["?"])[0])
    landed = {src[:10] for _sha, src, _i in after_c1 if src}
    needs = set(C1_CONFLICTS) | {f for src, f in PICK_CONFLICTS if src in landed}
    if a.write_inventory:
        write_inventory(a.inventory, observed_all, core_rows, post_origins)
    inv = check_inventory(a.inventory, observed_all, core_rows, post_origins, needs, repo, revs)
    fails.extend(inv)

    counts = Counter((v[2], v[1]) for v in observed.values())
    print("## H5 delta paths at C1 (class, surface) -> count")
    for k in sorted(counts):
        print(f"{k[0]:<11} {k[1]:<11} {counts[k]}")
    print(f"## core scopes replayed: {len(core_rows) - len(RETAINED)} over {len(core_paths)} files")
    print(f"## commits after C1: {len(after_c1)}; paths touched after C1: {len(post)} "
          f"({sum(1 for p in post if p not in h5_paths)} not in the H5 delta)")
    print(f"## unaccounted H5-added lines: {len(flagged)} path(s)")
    for path, n, first in flagged:
        print(f"  {path}: {n}, first: {first!r}")
    print(f"## inventory mismatches: {len(inv)}")
    if fails:
        print("\nFAIL\n" + "\n".join(fails))
        return 1
    print("\nOK: C1 carries every H5 path; core = tag + GOV-F25 + WP-D1 per scope (A1/A2 as ruled); picks equal their"
          " sources; no core after C1; inventory matches")
    return 0


if __name__ == "__main__":
    sys.exit(main())
