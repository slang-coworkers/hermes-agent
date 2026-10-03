"""AC-OSH-F64-4 G4: reconcile the route guard, the OpenShell router and session_model_usage for one window.

Usage: python3 route_reconcile.py <guard-dir> <openshell.log> <usage-before.txt> <usage-after.txt> <t0> <t1>

<guard-dir> holds the G3 guard's <pid>.jsonl files; <openshell.log> is `openshell logs` for osh-f64c-gw
covering the window; the usage files are route_record.py output taken at the window's start and end.
Router lines carry no PID, so a guard record matches a router record by method + path and by time,
within TOL seconds of the guard's dispatch time ("sent"), in time order. Rules, in order:

  1. each guard probe (allowed) consumes one router `GET /v1/models` and precedes its PID's first POST. A PID
     whose probe fell in an earlier window (steps 2-3 reuse step 1's PTY child) may rely on that 200 probe;
     the earlier window already matched it to its router GET;
  2. each guard POST that was allowed consumes one router `POST /v1/chat/completions`;
  3. a rejected guard POST has no router record (nothing unconsumed within TOL of it);
  4. every router POST in the window is consumed;
  5. per HERMES_HOME basename, the 2xx guard POSTs equal the api_call_count increment.

The first miss is the verdict. Prints the match table, then "reconcile_ok yes" or "reconcile_ok no
<rule> <detail>", and the skew of the first probe pair. Exits 0 only when every rule holds.
"""
import glob
import json
import os
import re
import sys

TOL = 2.0
ROUTER = re.compile(r"^\[(?P<ts>\d+(?:\.\d+)?)\].*routing proxy inference request.*\bmethod=(?P<method>[A-Z]+) path=(?P<path>\S+)")


def router_records(path, t0, t1):
    out = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = ROUTER.search(line)
            if m and t0 - TOL <= float(m["ts"]) <= t1 + TOL:
                out.append({"ts": float(m["ts"]), "method": m["method"], "path": m["path"], "used": False})
    return sorted(out, key=lambda r: r["ts"])


def guard_records(guard_dir, t0, t1):
    out = []
    for path in glob.glob(os.path.join(guard_dir, "*.jsonl")):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                rec = json.loads(line)
                if rec.get("kind") in ("probe", "post") and t0 <= rec.get("sent", rec["ts"]) <= t1:
                    out.append(rec)
    return sorted(out, key=lambda r: r.get("sent", r["ts"]))


def usage(path):
    totals = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            parts = line.split()
            if parts[:1] == ["usage"] and len(parts) == 6:
                totals[parts[1]] = totals.get(parts[1], 0) + int(parts[5])
    return totals


def consume(router, method, path, when):
    for rec in router:
        if not rec["used"] and rec["method"] == method and rec["path"] == path and abs(rec["ts"] - when) <= TOL:
            rec["used"] = True
            return rec
    return None


def home_name(home):
    # route_record.py names the gateway/default store "default"; HX resolves its HERMES_HOME to
    # $HOME/.hermes, whose basename is ".hermes". A served profile's home basename is its name.
    return "default" if home == ".hermes" else home


def main():
    guard_dir, log_path, before_path, after_path = sys.argv[1:5]
    t0, t1 = float(sys.argv[5]), float(sys.argv[6])
    router = router_records(log_path, t0, t1)
    guards = guard_records(guard_dir, t0, t1)
    table, miss, skew = [], None, None
    probe_at, posted, ok_posts = {}, set(), {}
    earlier = {}
    for g in guard_records(guard_dir, float("-inf"), t0):
        if g["kind"] == "probe" and g["decision"] == "allowed" and g["status"] == 200:
            earlier.setdefault(g["pid"], g.get("sent", g["ts"]))

    def fail(rule, detail):
        nonlocal miss
        miss = miss or (rule, detail)

    # Pass 1, in dispatch order: probes and allowed POSTs consume router records.
    for g in guards:
        when = g.get("sent", g["ts"])
        hit = None
        if g["kind"] == "probe":
            if g["decision"] != "allowed" or g["status"] != 200:
                fail("probe", "pid %s probe status %s" % (g["pid"], g["status"]))
            hit = consume(router, "GET", "/v1/models", when)
            if hit is None:
                fail("probe", "pid %s probe at %.3f has no router GET /v1/models" % (g["pid"], when))
            else:
                probe_at.setdefault(g["pid"], hit["ts"])
                if skew is None:
                    skew = round(hit["ts"] - when, 3)
        elif g["decision"] == "allowed":
            hit = consume(router, "POST", "/v1/chat/completions", when)
            if hit is None:
                fail("post", "pid %s POST at %.3f has no router record" % (g["pid"], when))
            elif g["pid"] not in posted:
                first_probe = probe_at.get(g["pid"], earlier.get(g["pid"]))
                if first_probe is None or first_probe >= hit["ts"]:
                    fail("order", "pid %s first POST router record does not follow its probe" % g["pid"])
            posted.add(g["pid"])
            if isinstance(g["status"], int) and 200 <= g["status"] < 300:
                ok_posts[home_name(g["home"])] = ok_posts.get(home_name(g["home"]), 0) + 1
        if g["decision"] == "allowed":
            table.append("%s pid %s home %s %s allowed %s router %s" % (
                g["kind"], g["pid"], g["home"], g["method"], g["status"], "%.3f" % hit["ts"] if hit else "-"))
    # Pass 2: a rejected POST must have left no router record (any path) near its decision time.
    for g in guards:
        if g["kind"] == "post" and g["decision"] != "allowed":
            when = g.get("sent", g["ts"])
            near = [r for r in router if not r["used"] and r["method"] == "POST" and abs(r["ts"] - when) <= TOL]
            if near:
                fail("rejected", "pid %s rejected POST at %.3f has a router record" % (g["pid"], when))
            table.append("post pid %s home %s POST rejected %s router %s" % (
                g["pid"], g["home"], g.get("reason"), "%.3f" % near[0]["ts"] if near else "-"))
    for rec in router:
        if rec["method"] == "POST" and not rec["used"] and t0 <= rec["ts"] <= t1:
            fail("unclaimed", "router POST %s at %.3f has no guard record" % (rec["path"], rec["ts"]))
    before, after = usage(before_path), usage(after_path)
    for home in sorted(set(before) | set(after) | set(ok_posts)):
        delta = after.get(home, 0) - before.get(home, 0)
        table.append("usage home %s guard_2xx %d api_call_count_delta %d" % (home, ok_posts.get(home, 0), delta))
        if delta != ok_posts.get(home, 0):
            fail("usage", "home %s guard_2xx %d != delta %d" % (home, ok_posts.get(home, 0), delta))
    print("\n".join(table))
    print("first_probe_skew_s", skew if skew is not None else "-")
    print("reconcile_ok", "yes" if miss is None else "no %s %s" % miss)
    sys.exit(0 if miss is None else 1)


if __name__ == "__main__":
    main()
