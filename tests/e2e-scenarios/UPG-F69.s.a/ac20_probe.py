#!/usr/bin/env python3
"""AC-UPG-F69.s.a-20 live leg (AC-UPG-F69.s.a-19 steps 5-8): dispatch every PR approve/merge form of
``ac20-forms.json`` through the gateway's own tool path, once in the approver's per-turn scope and once in the root
context, and record whether nv-fleet-gates refused it with the human-only owner rule. No model call is made.

    HERMES_HOME=<scratch home> HERMES_MANAGED_DIR=<scratch managed dir> HOME=<scratch launch home> \
        python3 ac20_probe.py --forms ac20-forms.json --out ac20-probe.jsonl \
        [--scratch /sandbox/scratch/fleet-f62d-s5a-ac20] [--root-host <host>] \
        [--root-backend <resolved> --root-expected-backend <expected>]

Exit 0 only when all form rows of both passes are blocked, every approver preflight executed in its exact target, and
every root preflight either executed on ``--root-host`` or found no target (erratum E3: both root preflights refused by
SANDBOX naming the recorded ``--root-backend`` and ``--root-expected-backend``); 1 when a form was not blocked; 2 when a
start-up precondition refused the run; 3 (FAIL(env)) when the gateway path is not usable or any other preflight did not
execute, executed outside its exact target, or the two root preflights disagree (erratum E2 Q1 (b), E3).

AC-20's live leg (AC-19 step 6) runs as the scratch GW approver at the head, inside GW, plus the scratch root context.
The shipped GW approver is never probed; it gets AC-20 at the next ship install. The refusal to start without a
``fleet-f62d-`` HERMES_HOME and HERMES_MANAGED_DIR is what keeps the shipped home and the shipped managed map
(``expected_ssh_host``) out of reach.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import re
import socket
import sys
import uuid
from pathlib import Path

FAILSAFE_PR = "999999999"
ABSENT_PREFIX = "/tmp/ac20-absent"  # no-tmp: ok — the erratum's fail-safe body path, asserted absent
SCRATCH_DEFAULT = "/sandbox/scratch/fleet-f62d-s5a-ac20"
APPROVER_HOST = "fleet-f62d-s5a-approver"
PASSES = ("approver", "root")
FORMS_EXPECTED = 23  # AC20_FORMS of the acceptance file; the live oracle is 46 rows = 23 forms x 2 passes
STAGES = ("DIAGNOSIS_REVIEW", "PLAN_REVIEW", "CODE_REVIEW", "DECISION_REVIEW", "OUTPUT_REVIEW")
GATE_MARKS = ("approval gate:", "human-only (owner rule)")
_DEQUOTE = str.maketrans({c: " " for c in "'\",[](){}"})
_PREFLIGHT = {
    "terminal": ("printf 'AC20PF {\"host\": \"%s\", \"home\": \"%s\", \"gh\": \"%s\", \"n\": %s}\\n' \"$(uname -n)\" "
                 "\"$HOME\" \"${GH_CONFIG_DIR:-$HOME/.config/gh}\" "
                 "\"$(ls -A \"${GH_CONFIG_DIR:-$HOME/.config/gh}\" 2>/dev/null | wc -l | tr -d ' ')\""),
    "execute_code": ("import json, os, socket\n"
                     "g = os.environ.get('GH_CONFIG_DIR') or os.path.join(os.environ.get('HOME', ''), '.config', 'gh')\n"
                     "n = len(os.listdir(g)) if os.path.isdir(g) else 0\n"
                     "print('AC20PF ' + json.dumps({'host': socket.gethostname(), 'home': os.environ.get('HOME'),"
                     " 'gh': g, 'n': n}))\n"),
}


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="milliseconds")


def _refuse(code: int, why: str) -> None:
    sys.stderr.write(f"ac20_probe: refused: {why}\n")
    raise SystemExit(code)


def _failsafe_problems(form_id: str, tool: str, form: str) -> list[str]:
    """Why ``form`` could act on something real (the erratum's fail-safe rule); empty when it cannot."""
    flat = form.translate(_DEQUOTE)
    problems = [] if tool in _PREFLIGHT else [f"{form_id}: unknown tool {tool!r}"]
    numbers = re.findall(r"pulls/(\d+)", flat) + re.findall(r"\bpr\s+[a-z-]+\s+(\d+)", flat)
    numbers += re.findall(r"\bnumber:\s*(\d+)", flat)
    problems += [f"{form_id}: PR {n} is not {FAILSAFE_PR}" for n in numbers if n != FAILSAFE_PR]
    for name, value in re.findall(r"\b(\w+Id)\s*:\s*([^\s,})]+)", form):
        if value != "null":
            problems.append(f"{form_id}: GraphQL input {name} is {value!r}, not null")
    paths = re.findall(r"@([^\s'\"]+)", form) + re.findall(r"(?:--input|-T)\s+([^\s'\"]+)", form)
    for path in paths:
        if not path.startswith(ABSENT_PREFIX):
            problems.append(f"{form_id}: body path {path} is not under {ABSENT_PREFIX}")
        elif os.path.lexists(path):
            problems.append(f"{form_id}: body path {path} exists")
    return problems


def _parse(result: str) -> dict:
    try:
        data = json.loads(result)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _gate_message(data: dict) -> str | None:
    err = data.get("error")
    return err if isinstance(err, str) and all(mark in err for mark in GATE_MARKS) else None


def _dispatch(stores, scope, handle, pass_name: str, tool: str, text: str, *, plan: bool = False) -> dict:
    """One call through the gateway tool path in a fresh session, inside ``scope``: the plugin's stores resolve their
    DB from the active home, so the session's critique (and the preflight's plan) must be recorded where the gate
    reads them."""
    sid = f"ac20-{pass_name}-{uuid.uuid4().hex[:12]}"
    key = "code" if tool == "execute_code" else "command"
    with scope():
        for stage in STAGES:
            stores.record_critique(sid, stage)
        if plan:
            stores.record_plan(sid)
        return _parse(handle(tool, {key: text}, task_id=sid, session_id=sid))


def _preflight_ok(pf: dict, pass_name: str, fresh: Path, root_host: str) -> str | None:
    """None when the tool process ran where it must and sees no gh state but the pass's own; else why not."""
    want = APPROVER_HOST if pass_name == "approver" else root_host
    if pf.get("host") != want:
        return f"executed on {pf.get('host')!r}, not {want!r}"
    if pf.get("n") not in (0, "0"):
        return f"gh config dir {pf.get('gh')!r} is not empty"
    if pf.get("host") == socket.gethostname():
        if pf.get("home") != str(fresh) or pf.get("gh") != str(fresh / "gh"):
            return f"HOME/GH_CONFIG_DIR are {pf.get('home')!r}/{pf.get('gh')!r}, not the pass's fresh {fresh}"
    return None


def _root_no_target(backend: str | None, expected: str | None) -> str | None:
    """The SANDBOX refusal a no-target root must name, from the Setup-recorded backends (erratum E3); None without."""
    if not (backend or expected):
        return None
    if not (backend and expected) or backend == expected:
        _refuse(2, "--root-backend and --root-expected-backend go together and must differ (a no-target root)")
    return f"resolved backend {backend!r} != expected {expected!r}"


def _preflight_row(data: dict, pass_name: str, fresh: Path, root_host: str, root_no_target: str | None) -> dict:
    """The preflight record of one pass and tool; it carries ``abort`` when the run must stop (exit 3)."""
    line = next((ln[len("AC20PF "):] for ln in str(data.get("output") or "").splitlines()
                 if ln.startswith("AC20PF ")), None)
    if line is not None:
        pf = {"executed": True, **_parse(line)}
        if pass_name == "root" and root_no_target:
            pf["abort"] = "root preflight executed although Setup recorded no root target"
            return pf
        why = _preflight_ok(pf, pass_name, fresh, root_host)
        if why:
            pf["abort"] = why
        return pf
    first = (str(data.get("error") or "").splitlines() or [""])[0][:300]
    if pass_name == "root" and root_no_target and first.startswith("sandbox: refused") and root_no_target in first:
        # A sandbox-enforcing root whose backend is not the expected one has nowhere to execute (erratum E3).
        return {"executed": False, "no_target": "sandbox", "message": first}
    # Ran nowhere, so the pass never reached its exact target: the precondition did not hold.
    return {"executed": False, "error": first or "no preflight output", "abort": "preflight did not execute"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ac20_probe")
    parser.add_argument("--forms", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--scratch", default=SCRATCH_DEFAULT)
    parser.add_argument("--root-host", default=socket.gethostname(),
                        help="where the root pass must execute: the target the scratch root home's terminal policy "
                             "names (default: this host, i.e. a local backend)")
    parser.add_argument("--root-backend", help="the scratch root's resolved terminal backend, as Setup recorded it")
    parser.add_argument("--root-expected-backend",
                        help="the scratch root's nv-fleet-gates expected_backend, as Setup recorded it")
    ns = parser.parse_args(argv)

    home = os.environ.get("HERMES_HOME", "")
    if "fleet-f62d-" not in home:
        _refuse(2, f"HERMES_HOME {home!r} is not a fleet-f62d- scratch home")
    # An unset or missing override lets managed_scope fall back to /etc/hermes or to no map at all.
    managed = os.environ.get("HERMES_MANAGED_DIR", "").strip()
    if "fleet-f62d-" not in managed or not Path(managed).is_dir():
        _refuse(2, f"HERMES_MANAGED_DIR {managed!r} is not an existing fleet-f62d- scratch managed dir")
    root_no_target = _root_no_target(ns.root_backend, ns.root_expected_backend)
    approver_home = Path(home) / "profiles" / "approver"
    if not approver_home.is_dir():
        _refuse(2, f"no approver profile at {approver_home}")
    forms = json.loads(Path(ns.forms).read_text(encoding="utf-8"))
    if len(forms) != FORMS_EXPECTED:
        _refuse(2, f"{ns.forms} holds {len(forms)} forms, not {FORMS_EXPECTED}")
    problems = [p for fid, (tool, form) in sorted(forms.items()) for p in _failsafe_problems(fid, tool, form)]
    if problems:
        _refuse(2, "; ".join(problems))
    fresh = {name: Path(ns.scratch) / f"home-{name}" for name in PASSES}
    for name, path in fresh.items():
        if os.path.lexists(path):
            _refuse(2, f"{path} already exists; each pass needs a fresh HOME")
    for path in fresh.values():
        (path / "gh").mkdir(mode=0o700, parents=True)

    try:
        import model_tools
        from gateway.run import _profile_runtime_scope
        from hermes_cli.plugins import get_plugin_manager
    except ImportError as exc:
        _refuse(3, f"the gateway tool path is not importable here ({exc})")
    loaded = get_plugin_manager()._plugins.get("nv-fleet-gates")
    if loaded is None or not loaded.enabled or loaded.error is not None or loaded.module is None:
        _refuse(3, "nv-fleet-gates did not load from this home")
    if "fleet-f62d-" not in str(getattr(loaded.module, "__file__", "")):
        _refuse(3, f"nv-fleet-gates loaded from {loaded.module.__file__}, not the scratch home")
    stores = loaded.module.stores

    def _scope(name):
        return lambda: _profile_runtime_scope(approver_home) if name == "approver" else contextlib.nullcontext()

    rows, isolated, all_blocked = [], True, True
    for name in PASSES:
        os.environ["HOME"] = str(fresh[name])
        os.environ["GH_CONFIG_DIR"] = str(fresh[name] / "gh")
        found = []
        for tool in ("terminal", "execute_code"):
            data = _dispatch(stores, _scope(name), model_tools.handle_function_call, name, tool, _PREFLIGHT[tool],
                             plan=True)
            found.append(_preflight_row(data, name, fresh[name], ns.root_host, root_no_target))
            rows.append({"pass": name, "tool": tool, "preflight": found[-1]})
        if len({pf["executed"] for pf in found}) > 1:
            for pf in found:
                pf.setdefault("abort", "mixed preflight result: one tool executed, the other did not")
        if any("abort" in pf for pf in found):
            isolated = False
            break
        for fid, (tool, form) in sorted(forms.items()):
            t_start = _now()
            data = _dispatch(stores, _scope(name), model_tools.handle_function_call, name, tool, form)
            message = _gate_message(data)
            other = (str(data.get("error") or "").splitlines() or ["no error"])[0][:300]
            rows.append({"pass": name, "id": fid, "tool": tool, "pid": os.getpid(), "t_start": t_start,
                         "t_end": _now(), "blocked": message is not None,
                         "message": message if message is not None else f"not blocked by the approval gate: {other}"})
            all_blocked = all_blocked and message is not None
    with open(ns.out, "w", encoding="utf-8") as fh:
        fh.writelines(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    forms_run = sum(1 for row in rows if "id" in row)
    sys.stdout.write(f"ac20_probe: {forms_run} form rows, all blocked: {all_blocked and isolated}\n")
    if not isolated:
        return 3
    return 0 if all_blocked and forms_run == len(PASSES) * len(forms) else 1


if __name__ == "__main__":
    raise SystemExit(main())
