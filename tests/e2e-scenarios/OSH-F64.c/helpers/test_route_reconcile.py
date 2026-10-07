"""AC-OSH-F64-4 G4 under erratum E9: the window decisions in route_window.py and the s3 waiver in route_reconcile.py."""
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _load(name):
    spec = importlib.util.spec_from_file_location("osh_f64c_" + name, HERE / (name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rw = _load("route_window")

W0, W1 = 100.0, 200.0
STEP_S = 6.0  # the ~5 s cadence plus sx overhead; snapshot 20 is the first at the 120 s bound


class Sandbox:
    """The in-sandbox guard dir and state.db totals a test drives, exported into a tester-side scenario dir the way
    AC-OSH-F64-4.md's gexport and usage_snap leave them."""

    def __init__(self, scen):
        scen.mkdir(parents=True, exist_ok=True)
        self.scen = scen
        self.files, self.usage, self.router, self.faults, self.probed = {}, {}, [], {}, set()

    def add(self, pid, home, kind="post", sent=150.0, ts=None, status=200, decision="allowed", router=True):
        rec = {"kind": kind, "method": "POST" if kind == "post" else "GET", "pid": pid, "home": home,
               "decision": decision, "status": status, "ts": sent + 0.4 if ts is None else ts}
        if decision == "allowed":
            rec["sent"] = sent
        else:
            rec.update(status=None, reason="url")
        self.files.setdefault("%d.jsonl" % pid, []).append(rec)
        if router and decision == "allowed" and kind in ("probe", "post"):
            path = "/v1/models" if kind == "probe" else "/v1/chat/completions"
            self.router.append("[%.3f] routing proxy inference request method=%s path=%s" % (
                sent + 0.1, rec["method"], path))
        return rec

    def post(self, pid, home, sent, ts=None, router=True):
        """An allowed 2xx POST, after a 200 probe the first time this PID posts (route_reconcile rule 1)."""
        if pid not in self.probed:
            self.probed.add(pid)
            self.add(pid, home, kind="probe", sent=sent - 1.0, ts=sent - 0.9)
        return self.add(pid, home, sent=sent, ts=ts, router=router)

    def land(self, store, n=1):
        """n usage +1s land in state.db: the release's async writer has applied them (E9 § Why)."""
        self.usage[store] = self.usage.get(store, 0) + n

    def snap(self, name, t):
        body = "t %.3f\n" % t + "".join("usage %s sess-%s - https://inference.local/v1 %d\n" % (s, s, n)
                                        for s, n in sorted(self.usage.items()))
        (self.scen / ("usage-%s.txt" % name)).write_text(
            body + "route_ok yes off_route_rows 0\ncontrols_ok yes stores_with_calls -\n", encoding="utf-8")

    def export(self, guard, manifest, t, fault=None):
        guard.mkdir(parents=True, exist_ok=True)
        for name, recs in self.files.items():
            lines = [json.dumps(r, sort_keys=True) + "\n" for r in recs]
            if fault == "truncated":
                lines = lines[:-1]
            if fault == "non-json":
                lines.append("{not json\n")
            (guard / name).write_text("".join(lines), encoding="utf-8")
        man = {"failed": "export stage"} if fault == "stage" else rw.manifest(str(guard), t)
        manifest.write_text(json.dumps(man), encoding="utf-8")

    def live(self, w, label, t):
        d = self.scen / "guard-live" / w / label
        self.export(d / "guard", d / "manifest.json", t, self.faults.get(label))

    def frozen(self, w, t):
        """E9.1 steps 2-3: the frozen export into guard/, then the router capture, never re-taken."""
        self.export(self.scen / "guard", self.scen / ("guard-frozen-%s.json" % w), t)
        (self.scen / "openshell.log").write_text("\n".join(self.router) + "\n", encoding="utf-8")


def parity_step(sb, w, k, final=False):
    """One win_open call: the helper's loaders parse the fixture files, the pure decision runs on what they return."""
    rc, lines, accepted = rw.decide_parity(**rw.load_parity(str(sb.scen), w, k, final))
    rw.finish_parity(str(sb.scen), w, rc, lines, accepted)
    return rc, lines


def close_step(sb, w, k, final=False, reported=None):
    """One win_close call, the same way: load, the pure decision, then finish (the one graded pass, on OK)."""
    rc, lines, accepted = rw.decide_close(**rw.load_close(str(sb.scen), w, W0, W1, k, final), reported=reported)
    return rw.finish_close(str(sb.scen), w, W0, W1, rc, lines, accepted, reported)


def open_window(sb, w, t=90.0, before_snap=None):
    """win_open <w> as AC-OSH-F64-4.md runs it (E9.4); before_snap(k) mutates the sandbox before re-read k."""
    marker = sb.scen / "guard-live" / w / "marker"
    sb.export(marker / "guard", sb.scen / ("guard-marker-%s.json" % w), t)
    k = 0
    sb.snap("%s-before.0" % w, t + 0.1)
    while True:
        sb.live(w, "p%d" % k, t + 0.2 + STEP_S * k)
        rc, lines = parity_step(sb, w, k)
        if rc != 10:
            break
        assert k < 40, lines
        k += 1
        if before_snap:
            before_snap(k)
        sb.snap("%s-before.%d" % (w, k), t + 0.1 + STEP_S * k)
    if rc == 11:
        sb.live(w, "pfinal", t + 0.3 + STEP_S * k)
        rc, lines = parity_step(sb, w, k, final=True)
    return rc, lines


def drive_close(sb, w, after_frozen=None, before_snap=None, reported=None):
    """win_close <w> as AC-OSH-F64-4.md runs it (E9.1); the hooks mutate the sandbox at fixed points."""
    t0 = W1 + 1.0
    sb.snap("%s-after.0" % w, t0)
    sb.frozen(w, t0 + 0.2)
    if after_frozen:
        after_frozen()
    k = 0
    while True:
        sb.live(w, "c%d" % k, t0 + 0.4 + STEP_S * k)
        rc, lines = close_step(sb, w, k, reported=reported)
        if rc != 10:
            break
        assert k < 40, lines
        k += 1
        if before_snap:
            before_snap(k)
        sb.snap("%s-after.%d" % (w, k), t0 + STEP_S * k)
    if rc == 11:
        sb.live(w, "cfinal", t0 + 0.6 + STEP_S * k)
        rc, lines = close_step(sb, w, k, final=True, reported=reported)
    return rc, lines, k


def opened(tmp_path, w="s4"):
    """A sandbox with one landed pre-window orchestrator call and window <w> open under baseline parity."""
    sb = Sandbox(tmp_path / "scenario")
    sb.post(3788, "orchestrator", sent=10.0)
    sb.land("orchestrator")
    rc, lines = open_window(sb, w)
    assert rc == 0, lines
    return sb


@pytest.fixture
def graded(monkeypatch):
    """Every graded route_reconcile pass route_window runs."""
    calls, real = [], rw.reconcile

    def spy(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(rw, "reconcile", spy)
    return calls


def lines_like(lines, prefix, needle=""):
    return [line for line in lines if line.startswith(prefix) and needle in line]


def verdict(lines):
    return [line for line in lines if line.startswith("reconcile_ok")][-1]


def numbered(lines, kind):
    return [line for line in lines if re.match(r"%s \d+ " % kind, line)]


def test_t_eq0_equal_at_snapshot_0_accepts_and_grades_once(tmp_path, graded):
    """T-EQ0: equal at snapshot 0 with a clean live export -> accept; the graded pass runs once and passes."""
    sb = opened(tmp_path)
    sb.post(3788, "orchestrator", sent=150.0)
    sb.land("orchestrator")
    rc, lines, k = drive_close(sb, "s4")
    assert (rc, k) == (0, 0), lines
    assert len(graded) == 1
    assert lines_like(lines, "snap 0 ", "store orchestrator delta 1 guard_2xx 1 -> accept")
    assert lines_like(lines, "export 0 ", "-> clean") and lines_like(lines, "export final ", "-> clean")
    assert verdict(lines) == "reconcile_ok yes"
    assert lines.index(lines_like(lines, "parity 0 ")[0]) < lines.index(lines_like(lines, "snap 0 ")[0])


def test_t_over_delta_above_guard_fails_at_once(tmp_path, graded):
    """T-OVER: delta > guard_2xx at snapshot 0 -> FAIL usage, with no re-read taken."""
    sb = opened(tmp_path)
    sb.post(3788, "orchestrator", sent=150.0)
    sb.land("orchestrator", 2)
    rc, lines, k = drive_close(sb, "s4")
    assert (rc, k) == (1, 0), lines
    assert verdict(lines).startswith("reconcile_ok no usage over store orchestrator")
    assert not (sb.scen / "usage-s4-after.1.txt").exists()
    assert not graded


def test_decisions_take_injected_snapshot_and_export_lists(tmp_path):
    """The two decisions are pure: injected snapshot and export lists alone decide them, with no scenario dir."""
    def exp(label, *recs):
        return {"label": label, "failed": None, "t": 1.0, "files": {"1.jsonl": list(recs)}}

    def snap(t, n):
        return ("x", (t, {"orchestrator": n}, {"orchestrator": ["usage orchestrator s - u %d" % n]}))

    old = {"kind": "post", "pid": 1, "home": "orchestrator", "decision": "allowed", "status": 200, "sent": 10.0, "ts": 10.2}
    new = dict(old, sent=150.0, ts=150.2)
    rc, lines, accepted = rw.decide_parity(exp("marker", old), [snap(90.0, 0), snap(96.0, 1)],
                                           [exp("p0", old), exp("p1", old)], exp("pfinal", old))
    assert (rc, lines[-1], accepted["orchestrator"][0]) == (0, "parity_ok yes", 1)
    args = dict(opened=["parity_ok yes"], marker={"1.jsonl": 1}, frozen=exp("frozen", old, new), w0=W0, w1=W1,
                before={"orchestrator": 1})
    rc, lines, _ = rw.decide_close(snaps=[snap(201.0, 1)], exports=[exp("0", old, new)], final=None, **args)
    assert rc == 10 and lines[-1].endswith("-> reread")
    rc, lines, accepted = rw.decide_close(snaps=[snap(201.0, 1), snap(207.0, 2)],
                                          exports=[exp("0", old, new), exp("1", old, new)],
                                          final=exp("final", old, new), **args)
    assert rc == 0 and accepted["orchestrator"][0] == 1
    late = dict(new, sent=W1 - 1.0, ts=W1 - 0.5)
    rc, lines, _ = rw.decide_close(snaps=[snap(201.0, 1), snap(207.0, 2)],
                                   exports=[exp("0", old, new), exp("1", old, new, late)], final=None, **args)
    assert rc == 3 and verdict(lines).startswith("reconcile_ok no quiet appended")


def test_t_lag_short_then_equal_at_reread_2(tmp_path, graded):
    """T-LAG: short at 0, equal at re-read 2 with clean exports -> accept; 3 snap and 3 export lines."""
    sb = opened(tmp_path)
    sb.post(3788, "orchestrator", sent=150.0)
    rc, lines, k = drive_close(sb, "s4", before_snap=lambda k: k == 2 and sb.land("orchestrator"))
    assert (rc, k) == (0, 2), lines
    assert len(numbered(lines, "snap")) == 3 and len(numbered(lines, "export")) == 3
    assert [line.rsplit("-> ", 1)[1] for line in numbered(lines, "snap")] == ["reread", "reread", "accept"]
    assert len(graded) == 1 and verdict(lines) == "reconcile_ok yes"


def test_t_bound_short_through_120_s_fails(tmp_path, graded):
    """T-BOUND: a store short through 120 s from snapshot 0 -> FAIL usage short at bound."""
    sb = opened(tmp_path)
    sb.post(3788, "orchestrator", sent=150.0)
    rc, lines, k = drive_close(sb, "s4")
    assert (rc, k) == (1, round(120 / STEP_S)), lines
    assert verdict(lines).startswith("reconcile_ok no usage short at bound store orchestrator")
    assert not graded


def test_t_ts_frozen_record_after_w1_is_not_quiet(tmp_path, graded):
    """T-TS: a frozen-export record with ts > W1 (a POST in flight at W1) -> FAIL(env) quiet ts."""
    sb = opened(tmp_path)
    sb.post(3788, "orchestrator", sent=199.5, ts=W1 + 0.5)
    sb.land("orchestrator")
    rc, lines, _ = drive_close(sb, "s4")
    assert rc == 3, lines
    assert lines_like(lines, "export frozen ", "-> FAIL(env) quiet ts")
    assert verdict(lines).startswith("reconcile_ok no quiet ts store orchestrator")
    assert not graded


def test_t_append_new_post_in_live_export_is_not_quiet(tmp_path, graded):
    """T-APPEND: a newly appended same-store POST in a live export with ts <= W1 -> FAIL(env) quiet appended."""
    sb = opened(tmp_path)
    sb.post(3788, "orchestrator", sent=150.0)
    sb.land("orchestrator")
    rc, lines, _ = drive_close(sb, "s4", after_frozen=lambda: sb.post(3788, "orchestrator", sent=199.0, ts=199.5))
    assert rc == 3, lines
    assert lines_like(lines, "export 0 ", "-> FAIL(env) quiet appended")
    assert verdict(lines).startswith("reconcile_ok no quiet appended store orchestrator")
    assert not graded


def test_t_interleave_record_stamped_before_w1_but_appended_after_the_frozen_export(tmp_path, graded):
    """T-INTERLEAVE: ts < W1, appended after the frozen export, its +1 in re-read 1 offsets a missing one -> FAIL(env)."""
    sb = opened(tmp_path)
    sb.post(3788, "orchestrator", sent=150.0)

    def interleave(k):
        if k == 1:
            sb.post(3788, "orchestrator", sent=199.5, ts=199.8)
            sb.land("orchestrator")

    rc, lines, k = drive_close(sb, "s4", before_snap=interleave)
    assert (rc, k) == (3, 1), lines
    assert lines_like(lines, "export 1 ", "-> FAIL(env) quiet appended")
    assert lines_like(lines, "snap 1 ", "store orchestrator delta 1 guard_2xx 1 -> FAIL(env) quiet")
    assert verdict(lines).startswith("reconcile_ok no quiet appended")
    assert not graded


@pytest.mark.parametrize("fault", ["stage", "truncated", "non-json"])
def test_t_export_failed_live_export(tmp_path, graded, fault):
    """T-EXPORT: a failed live export (a non-zero stage, a truncated file, a non-JSON line) -> FAIL(env) export."""
    sb = opened(tmp_path)
    sb.post(3788, "orchestrator", sent=150.0)
    sb.land("orchestrator")
    sb.faults["c0"] = fault
    rc, lines, _ = drive_close(sb, "s4")
    assert rc == 3, lines
    assert lines_like(lines, "export 0 ", "-> FAIL(env) export")
    assert verdict(lines).startswith("reconcile_ok no export")
    assert not graded


def test_t_parity_ok_equal_or_lagging_opens_the_window(tmp_path):
    """T-PARITY-OK: usage_before equals cum_2xx (at once, or after a lagging +1 lands) -> the window opens."""
    sb = Sandbox(tmp_path / "now")
    sb.post(3788, "orchestrator", sent=10.0)
    sb.land("orchestrator")
    rc, lines = open_window(sb, "s4")
    assert rc == 0, lines
    assert lines_like(lines, "parity 0 ", "store orchestrator usage_before 1 cum_2xx 1 -> accept")
    assert lines_like(lines, "export pfinal ", "-> clean") and lines[-1] == "parity_ok yes"
    assert "usage orchestrator " in (sb.scen / "usage-s4-before.txt").read_text(encoding="utf-8")
    lag = Sandbox(tmp_path / "lag")
    lag.post(3788, "orchestrator", sent=10.0)
    rc, lines = open_window(lag, "s4", before_snap=lambda k: k == 1 and lag.land("orchestrator"))
    assert rc == 0, lines
    assert [line.rsplit("-> ", 1)[1] for line in lines_like(lines, "parity ")] == ["reread", "accept"]


def test_t_parity_short_at_the_bound_never_opens(tmp_path, graded):
    """T-PARITY-SHORT: usage_before stays below cum_2xx through 120 s -> FAIL(env) parity short."""
    sb = Sandbox(tmp_path / "scenario")
    sb.post(3788, "orchestrator", sent=10.0)
    rc, lines = open_window(sb, "s4")
    assert rc == 3, lines
    assert verdict(lines).startswith("reconcile_ok no parity short store orchestrator")
    assert not (sb.scen / "usage-s4-before.txt").exists()
    assert not graded


def test_t_parity_over_count_never_opens(tmp_path, graded):
    """T-PARITY-OVER: usage_before above cum_2xx -> FAIL(env) parity over-count."""
    sb = Sandbox(tmp_path / "scenario")
    sb.post(3788, "orchestrator", sent=10.0)
    sb.land("orchestrator", 2)
    rc, lines = open_window(sb, "s4")
    assert rc == 3, lines
    assert verdict(lines).startswith("reconcile_ok no parity over-count store orchestrator")
    assert not graded


def test_parity_appended_post_while_parity_re_reads_never_opens(tmp_path, graded):
    """E9.4 step 3: a newly appended same-store POST during parity -> FAIL(env) parity appended."""
    sb = Sandbox(tmp_path / "scenario")
    sb.post(3788, "orchestrator", sent=10.0)

    def late(k):
        sb.post(3788, "orchestrator", sent=95.0)
        sb.land("orchestrator", 2)

    rc, lines = open_window(sb, "s4", before_snap=late)
    assert rc == 3, lines
    assert lines_like(lines, "parity 1 ", "store orchestrator usage_before 2 cum_2xx 1 -> FAIL(env) parity")
    assert verdict(lines).startswith("reconcile_ok no parity appended store orchestrator")
    assert not graded


def test_parity_final_export_catches_a_store_accepted_earlier(tmp_path, graded):
    """E9.4 step 4: store A holds parity at once, then posts while B still re-reads -> FAIL(env) at the final export."""
    sb = Sandbox(tmp_path / "scenario")
    sb.post(3788, "orchestrator", sent=10.0)
    sb.land("orchestrator")
    sb.post(4555, "builder", sent=20.0)

    def late(k):
        sb.post(3788, "orchestrator", sent=96.0)
        sb.land("builder")

    rc, lines = open_window(sb, "s4", before_snap=late)
    assert rc == 3, lines
    assert lines_like(lines, "export pfinal ", "-> FAIL(env) parity appended")
    assert verdict(lines).startswith("reconcile_ok no parity appended store orchestrator")
    assert not graded


def test_t_backlog_post_between_the_marker_and_w0_fails(tmp_path, graded):
    """T-BACKLOG: a POST appended after the W0 marker with sent < W0 -> FAIL(env) backlog."""
    sb = opened(tmp_path)
    sb.post(3788, "orchestrator", sent=W0 - 5.0)
    sb.land("orchestrator")
    rc, lines, _ = drive_close(sb, "s4")
    assert rc == 3, lines
    assert lines_like(lines, "export frozen ", "-> FAIL(env) backlog")
    assert verdict(lines).startswith("reconcile_ok no backlog")
    assert not graded


def test_t_offset_pre_w0_plus_one_cannot_hide_a_missing_in_window_plus_one(tmp_path, graded):
    """T-OFFSET: a pre-W0 POST's +1 lands in the window and one in-window +1 is missing (2 = 2 by sums) -> FAIL(env)."""
    sb = opened(tmp_path)
    sb.post(3788, "orchestrator", sent=W0 - 5.0)
    sb.post(3788, "orchestrator", sent=150.0)
    sb.post(3788, "orchestrator", sent=160.0)
    sb.land("orchestrator", 2)
    rc, lines, _ = drive_close(sb, "s4")
    assert rc == 3, lines
    assert verdict(lines).startswith("reconcile_ok no backlog")
    assert not graded


def test_parity_absorbs_a_pre_marker_lag_so_an_in_window_miss_stays_visible(tmp_path, graded):
    """A pre-marker POST whose +1 lags: parity waits for it, so the window's own missing +1 cannot be offset."""
    sb = Sandbox(tmp_path / "scenario")
    sb.post(3788, "orchestrator", sent=10.0)
    sb.land("orchestrator")
    sb.post(3788, "orchestrator", sent=80.0)
    rc, lines = open_window(sb, "s4", before_snap=lambda k: k == 2 and sb.land("orchestrator"))
    assert rc == 0, lines
    assert "https://inference.local/v1 2\n" in (sb.scen / "usage-s4-before.txt").read_text(encoding="utf-8")
    sb.post(3788, "orchestrator", sent=150.0)
    sb.post(3788, "orchestrator", sent=160.0)
    sb.land("orchestrator")
    rc, lines, _ = drive_close(sb, "s4")
    assert rc == 1, lines
    assert verdict(lines).startswith("reconcile_ok no usage short at bound store orchestrator")
    assert not graded


def test_t_lock_an_accepted_store_keeps_its_snapshot_value(tmp_path, graded):
    """T-LOCK: A is accepted at snapshot 0, B equalises at re-read 1; A's value stays the snapshot-0 one."""
    sb = opened(tmp_path)
    sb.post(3788, "orchestrator", sent=150.0)
    sb.land("orchestrator")
    sb.post(4555, "builder", sent=160.0)

    def later(k):
        sb.land("builder")
        sb.land("orchestrator")

    rc, lines, k = drive_close(sb, "s4", before_snap=later)
    assert (rc, k) == (0, 1), lines
    assert lines_like(lines, "snap 0 ", "store orchestrator delta 1 guard_2xx 1 -> accept")
    assert lines_like(lines, "snap 1 ", "store builder delta 1 guard_2xx 1 -> accept")
    assert not lines_like(lines, "snap 1 ", "store orchestrator")
    after = (sb.scen / "usage-s4-after.txt").read_text(encoding="utf-8")
    assert "usage orchestrator sess-orchestrator - https://inference.local/v1 2\n" in after
    assert len(graded) == 1 and verdict(lines) == "reconcile_ok yes"


def test_t_lock_late_post_by_a_locked_store_fails_the_final_export(tmp_path, graded):
    """T-LOCK-LATE: A accepted at 0; while B re-reads, an A POST sent before W1 is appended -> FAIL(env), no grading."""
    sb = opened(tmp_path)
    sb.post(3788, "orchestrator", sent=150.0)
    sb.land("orchestrator")
    sb.post(4555, "builder", sent=160.0)

    def later(k):
        sb.post(3788, "orchestrator", sent=W1 - 1.0, ts=W1 - 0.5)
        sb.land("builder")

    rc, lines, k = drive_close(sb, "s4", before_snap=later)
    assert (rc, k) == (3, 1), lines
    assert lines_like(lines, "export 1 ", "-> clean")
    assert lines_like(lines, "export final ", "-> FAIL(env) quiet appended")
    assert verdict(lines).startswith("reconcile_ok no quiet appended store orchestrator")
    assert not graded


@pytest.mark.parametrize("kind, rc", [("other", 0), ("probe", 3)])
def test_t_other_non_model_records_never_fail_a_window(tmp_path, graded, kind, rc):
    """T-OTHER: an `other` record with ts > W1 appended during re-reads passes; a `probe` there fails (FAIL(env))."""
    sb = opened(tmp_path)
    sb.post(3788, "orchestrator", sent=150.0)

    def later(k):
        sb.add(3788, "orchestrator", kind=kind, sent=W1 + 4.0, ts=W1 + 4.2, router=False)
        sb.land("orchestrator")

    got, lines, _ = drive_close(sb, "s4", before_snap=later)
    assert got == rc, lines
    if rc == 0:
        assert verdict(lines) == "reconcile_ok yes" and len(graded) == 1
    else:
        assert verdict(lines).startswith("reconcile_ok no quiet") and not graded


def s3_window(tmp_path, name):
    """s3 open under parity with one in-window orchestrator POST whose +1 has not landed yet."""
    sb = Sandbox(tmp_path / name)
    sb.post(3788, "orchestrator", sent=10.0)
    sb.land("orchestrator")
    sb.post(4555, "builder", sent=20.0)
    sb.land("builder")
    rc, lines = open_window(sb, "s3")
    assert rc == 0, lines
    sb.post(3788, "orchestrator", sent=150.0)
    return sb


def land_orchestrator_at_1(sb):
    return lambda k: k == 1 and sb.land("orchestrator")


def test_t_s3_overlap_builder_usage_and_quiet_are_reported_not_graded(tmp_path, graded):
    """T-S3-OVERLAP: with the s3 waiver, a builder usage mismatch and an appended builder POST are reported; s3 passes."""
    sb = s3_window(tmp_path, "waived")
    sb.post(4555, "builder", sent=160.0)
    rc, lines, k = drive_close(sb, "s3", reported="builder", before_snap=land_orchestrator_at_1(sb),
                               after_frozen=lambda: sb.post(4555, "builder", sent=195.0))
    assert (rc, k) == (0, 1), lines
    assert lines_like(lines, "snap 0 ", "store builder delta 0 guard_2xx 1 -> reported")
    quiet = lines_like(lines, "quiet ", "store builder")
    assert quiet and all(line.endswith("-> reported") for line in quiet)
    assert lines_like(lines, "export 0 ", "-> reported")
    assert "usage home builder guard_2xx 1 api_call_count_delta 0 reported (s3 overlap)" in lines
    assert not [line for line in lines if line.startswith("snap ") and "store builder" in line
                and not line.endswith("-> reported")]
    assert len(graded) == 1 and verdict(lines) == "reconcile_ok yes"


def test_t_s3_overlap_a_builder_over_count_is_reported_too(tmp_path, graded):
    """T-S3-OVERLAP: with the waiver, a builder delta above its guard_2xx is reported, never FAIL usage over."""
    sb = s3_window(tmp_path, "over")
    sb.post(4555, "builder", sent=160.0)
    sb.land("builder", 3)
    rc, lines, _ = drive_close(sb, "s3", reported="builder", before_snap=land_orchestrator_at_1(sb))
    assert rc == 0, lines
    assert lines_like(lines, "snap 0 ", "store builder delta 3 guard_2xx 1 -> reported")
    assert len(graded) == 1 and verdict(lines) == "reconcile_ok yes"


def test_t_s3_overlap_without_the_waiver_the_builder_mismatch_fails(tmp_path, graded):
    """T-S3-OVERLAP: the same builder mismatch without the waiver argument -> FAIL usage."""
    sb = s3_window(tmp_path, "unwaived")
    sb.post(4555, "builder", sent=160.0)
    rc, lines, _ = drive_close(sb, "s3", before_snap=land_orchestrator_at_1(sb))
    assert rc == 1, lines
    assert verdict(lines).startswith("reconcile_ok no usage short at bound store builder")
    assert not graded


def _rejected_with_router(sb):
    sb.add(4555, "builder", kind="post", sent=180.0, decision="rejected")
    sb.router.append("[180.500] routing proxy inference request method=POST path=/v1/chat/completions")


def _unclaimed_router_post(sb):
    sb.router.append("[175.000] routing proxy inference request method=POST path=/v1/chat/completions")


def _post_without_router(sb):
    sb.post(4555, "builder", sent=170.0, router=False)


def _probe_not_200(sb):
    sb.add(4555, "builder", kind="probe", sent=158.0, status=401)


def _excluded_pid(sb):
    (sb.scen / "pty-select-s3.txt").write_text(
        "fork_descendant 4556 starttime 1 parent 4555/1 born 1.0 exited 1.1\ng2_ok yes\n", encoding="utf-8")
    sb.post(4556, "builder", sent=170.0)


@pytest.mark.parametrize("defect, rule", [
    (_rejected_with_router, "rejected"),
    (_unclaimed_router_post, "unclaimed"),
    (_post_without_router, "post"),
    (_probe_not_200, "probe"),
    (_excluded_pid, "excluded"),
], ids=["rule3", "rule4", "rule2", "rule1", "rule6"])
def test_t_s3_overlap_the_waiver_keeps_rules_1_to_4_and_6_graded(tmp_path, graded, defect, rule):
    """T-S3-OVERLAP: the s3 waiver leaves the builder's rules 1-4 and 6 graded; exactly one graded pass, and it fails."""
    sb = s3_window(tmp_path, rule)
    sb.post(4555, "builder", sent=160.0)
    sb.land("builder")
    defect(sb)
    rc, lines, _ = drive_close(sb, "s3", reported="builder", before_snap=land_orchestrator_at_1(sb))
    assert rc == 1, lines
    assert len(graded) == 1
    assert verdict(lines).startswith("reconcile_ok no %s " % rule)


def test_snapshot_without_its_clock_line_is_fail_env(tmp_path, graded):
    """A usage snapshot with no sandbox-clock `t` line (a failed sx) cannot be timed against the bound -> FAIL(env)."""
    sb = opened(tmp_path)
    sb.post(3788, "orchestrator", sent=150.0)
    t0 = W1 + 1.0
    sb.snap("s4-after.0", t0)
    sb.frozen("s4", t0 + 0.2)
    sb.live("s4", "c0", t0 + 0.4)
    assert close_step(sb, "s4", 0)[0] == 10
    (sb.scen / "usage-s4-after.1.txt").write_text("", encoding="utf-8")
    sb.live("s4", "c1", t0 + 6.4)
    rc, lines = close_step(sb, "s4", 1)
    assert rc == 3, lines
    assert verdict(lines).startswith("reconcile_ok no snapshot")
    assert not graded


def test_route_reconcile_cli_reports_the_waived_store_only_when_asked(tmp_path):
    """route_reconcile.py --reported-store prints the waived store's rule-5 line as reported; without it, rule 5 fails."""
    sb = Sandbox(tmp_path / "scenario")
    sb.post(3788, "orchestrator", sent=150.0)
    sb.post(4555, "builder", sent=160.0)
    sb.snap("before", 99.0)
    sb.land("orchestrator")
    sb.snap("after", 201.0)
    sb.frozen("s3", 201.2)
    args = [sys.executable, str(HERE / "route_reconcile.py"), str(sb.scen / "guard"), str(sb.scen / "openshell.log"),
            str(sb.scen / "usage-before.txt"), str(sb.scen / "usage-after.txt"), str(W0), str(W1)]
    waived = subprocess.run(args + ["--reported-store", "builder"], capture_output=True, text=True, encoding="utf-8")
    assert waived.returncode == 0, waived.stdout + waived.stderr
    assert "usage home builder guard_2xx 1 api_call_count_delta 0 reported (s3 overlap)" in waived.stdout.splitlines()
    graded_cli = subprocess.run(args, capture_output=True, text=True, encoding="utf-8")
    assert graded_cli.returncode == 1
    assert "reconcile_ok no usage home builder guard_2xx 1 != delta 0" in graded_cli.stdout
