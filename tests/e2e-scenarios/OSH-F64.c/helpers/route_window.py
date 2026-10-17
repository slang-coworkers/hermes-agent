"""AC-OSH-F64-4 G4 windows under erratum E9: baseline parity at win_open, the direction-aware re-read at win_close.

Usage:
  python3 route_window.py manifest <guard-dir> <t>
  python3 route_window.py parity <scenario-dir> <w> <k> [final]
  python3 route_window.py close <scenario-dir> <w> <W0> <W1> <k> [final] [--reported-store <store>]

AC-OSH-F64-4.md's win_open / win_close take the usage snapshots and guard exports and call this after each one.
Every call re-derives the window from the files alone: usage-<w>-before.<j>.txt / usage-<w>-after.<j>.txt,
guard-marker-<w>.json (the W0 marker), guard/ with guard-frozen-<w>.json (the frozen export), and
guard-live/<w>/<label>/{guard/,manifest.json} (live exports p<j>, pfinal, c<j>, cfinal). Exit status: 0 ok,
1 FAIL, 3 FAIL(env), 10 take snapshot k+1 (~5 s later) and its live export, 11 take the final live export.
`parity` writes parity-<w>.txt and, once parity holds, usage-<w>-before.txt. `close` writes usage-<w>-after.txt
and route-reconcile-<w>.txt, and runs route_reconcile's graded pass once, after every store is accepted and the
final export is clean.
"""
import glob
import importlib.util
import json
import os
import sys

BOUND_S = 120.0
WATCHED = ("probe", "post")  # kind `other` never carries a usage +1 (E9 D17a OPEN-1)
OK, FAIL, FAIL_ENV, REREAD, FINAL = 0, 1, 3, 10, 11

_spec = importlib.util.spec_from_file_location(
    "osh_f64c_route_reconcile", os.path.join(os.path.dirname(os.path.abspath(__file__)), "route_reconcile.py"))
_rr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_rr)
reconcile = _rr.reconcile
home_name = _rr.home_name


class Stop(Exception):
    """A verdict decided before the graded pass: (exit status, rule, detail)."""


def fmt(x):
    return "-" if x is None else "%.3f" % x


def manifest(guard_dir, t):
    """{"t", "files": {name: line count}} of one extracted guard/ dir, or {"failed": why} (E9 Terms)."""
    try:
        t = float(t)
    except ValueError:
        return {"failed": "export clock"}
    if not os.path.isdir(guard_dir):
        return {"failed": "no guard dir"}
    files = {}
    for path in sorted(glob.glob(os.path.join(guard_dir, "*.jsonl"))):
        name = os.path.basename(path)
        with open(path, "rb") as fh:
            raw = fh.read()
        if raw and not raw.endswith(b"\n"):
            return {"failed": "partial line in %s" % name}
        lines = raw.decode("utf-8", errors="replace").splitlines()
        for n, line in enumerate(lines, 1):
            try:
                rec = json.loads(line)
            except ValueError:
                return {"failed": "non-json line %s:%d" % (name, n)}
            if not isinstance(rec, dict) or "kind" not in rec:
                return {"failed": "line without kind %s:%d" % (name, n)}
        files[name] = len(lines)
    return {"t": t, "files": files}


def read_export(label, guard_dir, manifest_path):
    """One export as {"label", "failed", "t", "files": {name: [records]}}; an unreadable one is failed."""
    exp = {"label": label, "failed": None, "t": None, "files": {}}
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            man = json.load(fh)
        exp["failed"], exp["t"] = man.get("failed"), man.get("t")
        for name, count in ({} if exp["failed"] else man["files"]).items():
            with open(os.path.join(guard_dir, name), encoding="utf-8") as fh:
                recs = [json.loads(line) for line in fh.read().splitlines()]
            if len(recs) < count:
                exp["failed"] = "%s has fewer lines than its manifest" % name
            exp["files"][name] = recs[:count]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        exp["failed"] = "unreadable (%s)" % type(exc).__name__
    return exp


def counts(exp):
    return {name: len(recs) for name, recs in exp["files"].items()}


def appended(exp, ref):
    """Records past ref's line count for their file, or in a file ref does not list."""
    return [r for name, recs in sorted(exp["files"].items()) for r in recs[ref.get(name, 0):]]


def short(exp, ref):
    return [name for name, n in sorted(ref.items()) if len(exp["files"].get(name, [])) < n]


def store(rec):
    return home_name(rec.get("home"))


def at(rec):
    return rec.get("sent", rec["ts"])


def two_xx(recs):
    out = {}
    for r in recs:
        if (r.get("kind") == "post" and r.get("decision") == "allowed" and isinstance(r.get("status"), int)
                and 200 <= r["status"] < 300):
            out[store(r)] = out.get(store(r), 0) + 1
    return out


def export_line(exp, ref, outcome):
    if exp["failed"]:
        return "export %s t - files - lines - new_probe_post - max_ts - -> %s" % (exp["label"], outcome)
    recs = [r for rs in exp["files"].values() for r in rs]
    max_ts = max((r["ts"] for r in recs if r.get("kind") in WATCHED), default=None)
    new = [r for r in appended(exp, ref) if r.get("kind") in WATCHED]
    return "export %s t %s files %d lines %d new_probe_post %d max_ts %s -> %s" % (
        exp["label"], fmt(exp["t"]), len(exp["files"]), len(recs), len(new), fmt(max_ts), outcome)


def fail_export(exp, ref, lines, why):
    lines.append(export_line(exp, ref, "FAIL(env) export %s" % why))
    raise Stop(FAIL_ENV, "export", "%s %s" % (exp["label"], why))


def read_snap(path):
    """(t, {store: api_call_count sum}, {store: usage lines}) of one usage_snap file; None when incomplete."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        t = float(lines[0].split()[1]) if lines and lines[0].startswith("t ") else None
    except (OSError, ValueError, IndexError):
        return None
    if t is None or not any(line.startswith("controls_ok ") for line in lines):
        return None
    totals, rows = {}, {}
    for line in lines:
        parts = line.split()
        if parts[:1] == ["usage"] and len(parts) == 6:
            totals[parts[1]] = totals.get(parts[1], 0) + int(parts[5])
            rows.setdefault(parts[1], []).append(line)
    return t, totals, rows


def compose(path, accepted):
    """usage-<w>-{before,after}.txt: each store's lines from the snapshot that accepted it (E9.1 step 10, E9.4 step 5)."""
    with open(path, "w", encoding="utf-8") as fh:
        for s in sorted(accepted):
            fh.writelines(line + "\n" for line in accepted[s][1])
        fh.write("composed_from %s\n" % (" ".join("%s=%d" % (s, accepted[s][0]) for s in sorted(accepted)) or "-"))


def write(path, lines):
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(line + "\n" for line in lines)


def check(exp, complete_ref, append_ref, marker, stores, reported, w0, w1, lines):
    """E9.2 (a)-(c) plus the E9.4 step-7 backlog check on one export; `stores` None means every store."""
    gaps = short(exp, complete_ref)
    if exp["failed"] or gaps:
        fail_export(exp, append_ref, lines, exp["failed"] or "short " + ",".join(gaps))
    found = [("backlog", "", r) for r in appended(exp, marker) if r.get("kind") == "post" and at(r) < w0]
    found += [("quiet", "ts", r) for recs in exp["files"].values() for r in recs
              if r.get("kind") in WATCHED and r["ts"] > w1]
    found += [("quiet", "appended", r) for r in appended(exp, append_ref) if r.get("kind") in WATCHED]
    hit, notes = None, []
    for rule, what, r in found:
        if rule == "quiet" and store(r) == reported:
            notes.append("quiet %s t %s store %s %s pid %s kind %s sent %s ts %s -> reported" % (
                exp["label"], fmt(exp["t"]), store(r), what, r.get("pid"), r.get("kind"), fmt(r.get("sent")),
                fmt(r["ts"])))
        elif hit is None and (rule == "backlog" or stores is None or store(r) in stores):
            hit = (rule, what, r)
    if hit is None:
        lines.append(export_line(exp, append_ref, "reported" if notes else "clean"))
        lines.extend(notes)
        return
    rule, what, r = hit
    lines.append(export_line(exp, append_ref, "FAIL(env) %s" % (rule + (" " + what if what else ""))))
    lines.extend(notes)
    if rule == "backlog":
        raise Stop(FAIL_ENV, "backlog", "store %s pid %s sent %s < W0 %s in export %s" % (
            store(r), r.get("pid"), fmt(at(r)), fmt(w0), exp["label"]))
    raise Stop(FAIL_ENV, "quiet", "%s store %s pid %s kind %s sent %s ts %s W1 %s in export %s" % (
        what, store(r), r.get("pid"), r.get("kind"), fmt(r.get("sent")), fmt(r["ts"]), fmt(w1), exp["label"]))


def live_export(scen, w, label, name):
    d = os.path.join(scen, "guard-live", w, label)
    return read_export(name, os.path.join(d, "guard"), os.path.join(d, "manifest.json"))


def records(exp):
    return [r for recs in exp["files"].values() for r in recs]


def load_parity(scen, w, k, final=False):
    """The files win_open has left for window <w> up to before-snapshot k, as decide_parity's inputs."""
    return {
        "marker": read_export("marker", os.path.join(scen, "guard-live", w, "marker", "guard"),
                              os.path.join(scen, "guard-marker-%s.json" % w)),
        "snaps": [("%s-before.%d" % (w, j), read_snap(os.path.join(scen, "usage-%s-before.%d.txt" % (w, j))))
                  for j in range(k + 1)],
        "exports": [live_export(scen, w, "p%d" % j, "p%d" % j) for j in range(k + 1)],
        "final": live_export(scen, w, "pfinal", "pfinal") if final else None,
    }


def decide_parity(marker, snaps, exports, final):
    """E9.4 steps 1-4 over injected snapshots and exports: (status, lines, {store: (snapshot, usage lines)})."""
    lines, accepted = [], {}
    try:
        rc = _decide_parity(marker, snaps, exports, final, lines, accepted)
    except Stop as stop:
        rc, rule, detail = stop.args
        lines.append("reconcile_ok no %s %s" % (rule, detail))
    return rc, lines, accepted


def _decide_parity(marker, snaps, exports, final, lines, accepted):
    ref = counts(marker)
    if marker["failed"]:
        fail_export(marker, ref, lines, marker["failed"])
    lines.append(export_line(marker, ref, "clean"))
    cum = two_xx(records(marker))

    def parity_export(exp, stores):
        gaps = short(exp, ref)
        if exp["failed"] or gaps:
            fail_export(exp, ref, lines, exp["failed"] or "short " + ",".join(gaps))
        hits = [r for r in appended(exp, ref) if r.get("kind") in WATCHED and (stores is None or store(r) in stores)]
        if hits:
            r = hits[0]
            lines.append(export_line(exp, ref, "FAIL(env) parity appended"))
            raise Stop(FAIL_ENV, "parity", "appended store %s pid %s kind %s ts %s in export %s" % (
                store(r), r.get("pid"), r.get("kind"), fmt(r["ts"]), exp["label"]))
        lines.append(export_line(exp, ref, "clean"))

    t0, pending = None, []
    for j, ((name, snap), exp) in enumerate(zip(snaps, exports)):
        if snap is None:
            raise Stop(FAIL_ENV, "snapshot", "usage-%s.txt has no clock line or is incomplete" % name)
        t, totals, rows = snap
        t0 = t if t0 is None else t0
        stores = sorted((set(cum) | set(totals)) - set(accepted))

        def line(s, outcome):
            lines.append("parity %d t %s store %s usage_before %d cum_2xx %d -> %s" % (
                j, fmt(t), s, totals.get(s, 0), cum.get(s, 0), outcome))

        def export_or_stop():
            try:
                parity_export(exp, stores)
            except Stop as stop:
                for s in stores:
                    line(s, "FAIL(env) %s" % stop.args[1])
                raise

        if j:
            export_or_stop()
        for s in stores:
            if totals.get(s, 0) > cum.get(s, 0):
                line(s, "FAIL(env) parity over-count")
                raise Stop(FAIL_ENV, "parity", "over-count store %s usage_before %d cum_2xx %d" % (
                    s, totals.get(s, 0), cum.get(s, 0)))
        if not j:
            export_or_stop()
        pending = []
        for s in stores:
            if totals.get(s, 0) == cum.get(s, 0):
                accepted[s] = (j, rows.get(s, []))
                line(s, "accept")
            elif t - t0 >= BOUND_S:
                line(s, "FAIL(env) parity short")
                raise Stop(FAIL_ENV, "parity", "short store %s usage_before %d cum_2xx %d at %.0f s" % (
                    s, totals.get(s, 0), cum.get(s, 0), t - t0))
            else:
                pending.append(s)
                line(s, "reread")
    if pending:
        return REREAD
    if final is None:
        return FINAL
    parity_export(final, None)
    lines.append("parity_ok yes")
    return OK


def finish_parity(scen, w, rc, lines, accepted):
    """Write parity-<w>.txt; on OK the composed usage-<w>-before.txt, on a stop route-reconcile-<w>.txt too."""
    if rc == OK:
        compose(os.path.join(scen, "usage-%s-before.txt" % w), accepted)
    elif rc not in (REREAD, FINAL):
        write(os.path.join(scen, "route-reconcile-%s.txt" % w), lines)
    write(os.path.join(scen, "parity-%s.txt" % w), lines)


def parity(scen, w, k, final=False):
    """win_open <w> after before-snapshot k and live export p<k> (or pfinal): E9.4 steps 1-5."""
    rc, lines, accepted = decide_parity(**load_parity(scen, w, int(k), final))
    finish_parity(scen, w, rc, lines, accepted)
    return rc, lines


def load_close(scen, w, w0, w1, k, final=False):
    """The files win_open and win_close have left for window <w> up to after-snapshot k, as decide_close's inputs."""
    try:
        with open(os.path.join(scen, "parity-%s.txt" % w), encoding="utf-8") as fh:
            opened = fh.read().splitlines()
    except OSError:
        opened = []
    try:
        with open(os.path.join(scen, "guard-marker-%s.json" % w), encoding="utf-8") as fh:
            marker = json.load(fh)["files"]
    except (OSError, ValueError, KeyError, TypeError):
        marker = None
    return {
        "opened": opened, "marker": marker, "w0": float(w0), "w1": float(w1),
        "frozen": read_export("frozen", os.path.join(scen, "guard"), os.path.join(scen, "guard-frozen-%s.json" % w)),
        "before": _rr.usage(os.path.join(scen, "usage-%s-before.txt" % w)),
        "snaps": [("%s-after.%d" % (w, j), read_snap(os.path.join(scen, "usage-%s-after.%d.txt" % (w, j))))
                  for j in range(int(k) + 1)],
        "exports": [live_export(scen, w, "c%d" % j, str(j)) for j in range(int(k) + 1)],
        "final": live_export(scen, w, "cfinal", "final") if final else None,
    }


def decide_close(opened, marker, frozen, w0, w1, before, snaps, exports, final, reported=None):
    """E9.1 steps 1-9, E9.2 and E9.4 step 7 over injected inputs: (status, lines, {store: (snapshot, usage lines)}).

    OK means every graded store is accepted and the final export is clean, so the graded pass may run.
    """
    lines, accepted = list(opened), {}
    try:
        rc = _decide_close(opened, marker, frozen, w0, w1, before, snaps, exports, final, reported, lines, accepted)
    except Stop as stop:
        rc, rule, detail = stop.args
        lines.append("reconcile_ok no %s %s" % (rule, detail))
    return rc, lines, accepted


def _decide_close(opened, marker, frozen, w0, w1, before, snaps, exports, final, reported, lines, accepted):
    if opened[-1:] != ["parity_ok yes"]:
        raise Stop(FAIL_ENV, "parity", "the window has no accepted baseline parity")
    if marker is None:
        raise Stop(FAIL_ENV, "export", "the W0 marker manifest is unreadable or failed")
    ref = counts(frozen)
    check(frozen, ref, ref, marker, None, reported, w0, w1, lines)
    g2xx = two_xx([r for r in records(frozen) if r.get("kind") in WATCHED and w0 <= at(r) <= w1])
    t0, pending = None, []
    for j, ((name, snap), exp) in enumerate(zip(snaps, exports)):
        if snap is None:
            raise Stop(FAIL_ENV, "snapshot", "usage-%s.txt has no clock line or is incomplete" % name)
        t, totals, rows = snap
        t0 = t if t0 is None else t0
        stores = sorted((set(g2xx) | set(before) | set(totals)) - set(accepted) - {reported})

        def line(s, outcome):
            lines.append("snap %d t %s store %s delta %d guard_2xx %d -> %s" % (
                j, fmt(t), s, totals.get(s, 0) - before.get(s, 0), g2xx.get(s, 0), outcome))

        def export_or_stop():
            try:
                check(exp, ref, ref, marker, stores, reported, w0, w1, lines)
            except Stop as stop:
                for s in stores:
                    line(s, "FAIL(env) %s" % stop.args[1])
                raise

        if j:
            export_or_stop()
        if not j and reported is not None:
            accepted[reported] = (0, rows.get(reported, []))
            line(reported, "reported")
        for s in stores:
            delta = totals.get(s, 0) - before.get(s, 0)
            if delta > g2xx.get(s, 0):
                line(s, "FAIL usage over")
                raise Stop(FAIL, "usage", "over store %s delta %d guard_2xx %d" % (s, delta, g2xx.get(s, 0)))
        if not j:
            export_or_stop()
        pending = []
        for s in stores:
            delta = totals.get(s, 0) - before.get(s, 0)
            if delta == g2xx.get(s, 0):
                accepted[s] = (j, rows.get(s, []))
                line(s, "accept")
            elif t - t0 >= BOUND_S:
                line(s, "FAIL usage short at bound")
                raise Stop(FAIL, "usage", "short at bound store %s delta %d guard_2xx %d at %.0f s" % (
                    s, delta, g2xx.get(s, 0), t - t0))
            else:
                pending.append(s)
                line(s, "reread")
    if pending:
        return REREAD
    if final is None:
        return FINAL
    check(final, ref, ref, marker, None, reported, w0, w1, lines)
    return OK


def finish_close(scen, w, w0, w1, rc, lines, accepted, reported=None):
    """On OK compose usage-<w>-after.txt and run the one graded pass (E9.5); always write route-reconcile-<w>.txt."""
    if rc == OK:
        after = os.path.join(scen, "usage-%s-after.txt" % w)
        compose(after, accepted)
        ok, table = reconcile(os.path.join(scen, "guard"), os.path.join(scen, "openshell.log"),
                              os.path.join(scen, "usage-%s-before.txt" % w), after, float(w0), float(w1), reported)
        lines = lines + table
        rc = OK if ok else FAIL
    write(os.path.join(scen, "route-reconcile-%s.txt" % w), lines)
    return rc, lines


def close(scen, w, w0, w1, k, final=False, reported=None):
    """win_close <w> after after-snapshot k and live export c<k> (or cfinal): E9.1, E9.2, E9.4 step 7, E9.5."""
    rc, lines, accepted = decide_close(**load_close(scen, w, w0, w1, k, final), reported=reported)
    return finish_close(scen, w, w0, w1, rc, lines, accepted, reported)


def main():
    args = sys.argv[1:]
    reported = None
    if "--reported-store" in args:
        i = args.index("--reported-store")
        reported = args[i + 1]
        del args[i:i + 2]
    final = args[-1:] == ["final"]
    if final:
        args = args[:-1]
    mode = args[0]
    if mode == "manifest":
        print(json.dumps(manifest(args[1], args[2]), sort_keys=True))
        return OK
    if mode == "parity":
        rc, lines = parity(args[1], args[2], args[3], final)
    elif mode == "close":
        rc, lines = close(args[1], args[2], args[3], args[4], args[5], final, reported)
    else:
        print("unknown mode", mode, file=sys.stderr)
        return 2
    print("\n".join(lines))
    return rc


if __name__ == "__main__":
    sys.exit(main())
