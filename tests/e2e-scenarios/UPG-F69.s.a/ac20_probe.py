#!/usr/bin/env python3
"""AC-UPG-F69.s.a-20 live leg (AC-UPG-F69.s.a-19 steps 5-8): dispatch every PR approve/merge form of
``ac20-forms.json`` through the gateway's own tool path, once in the approver's per-turn scope and once in the root
context, and record whether nv-fleet-gates refused it with the human-only owner rule. No model call is made.

    HERMES_HOME=<scratch home> HOME=<scratch launch home> python3 ac20_probe.py --forms ac20-forms.json \
        --out ac20-probe.jsonl [--scratch /sandbox/scratch/fleet-f62d-s5a-ac20] [--root-host <host>]

Exit 0 only when every isolation preflight passed and all form rows of both passes are blocked; 1 when a form was
not blocked; 2 when a start-up precondition refused the run; 3 when the gateway path is not usable (FAIL(env)).
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ac20_probe")
    parser.add_argument("--forms", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--scratch", default=SCRATCH_DEFAULT)
    parser.add_argument("--root-host", default=socket.gethostname(),
                        help="where the root pass must execute: the target the scratch root home's terminal policy "
                             "names (default: this host, i.e. a local backend)")
    ns = parser.parse_args(argv)

    home = os.environ.get("HERMES_HOME", "")
    if "fleet-f62d-" not in home:
        _refuse(2, f"HERMES_HOME {home!r} is not a fleet-f62d- scratch home")
    approver_home = Path(home) / "profiles" / "approver"
    if not approver_home.is_dir():
        _refuse(2, f"no approver profile at {approver_home}")
    forms = json.loads(Path(ns.forms).read_text(encoding="utf-8"))
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
        return _profile_runtime_scope(approver_home) if name == "approver" else contextlib.nullcontext()

    def _dispatch(name, tool, text, *, plan=False):
        sid = f"ac20-{name}-{uuid.uuid4().hex[:12]}"
        for stage in STAGES:
            stores.record_critique(sid, stage)
        if plan:
            stores.record_plan(sid)
        key = "code" if tool == "execute_code" else "command"
        with _scope(name):
            return _parse(model_tools.handle_function_call(tool, {key: text}, task_id=sid, session_id=sid))

    rows, isolated, all_blocked = [], True, True
    for name in PASSES:
        os.environ["HOME"] = str(fresh[name])
        os.environ["GH_CONFIG_DIR"] = str(fresh[name] / "gh")
        for tool in ("terminal", "execute_code"):
            data = _dispatch(name, tool, _PREFLIGHT[tool], plan=True)
            line = next((ln[len("AC20PF "):] for ln in str(data.get("output") or "").splitlines()
                         if ln.startswith("AC20PF ")), None)
            if line is None:
                # The benign call never ran (a gate refused it), so no form can run in this target either.
                pf = {"executed": False, "error": str(data.get("error") or "no preflight output")[:300]}
            else:
                pf = {"executed": True, **_parse(line)}
                why = _preflight_ok(pf, name, fresh[name], ns.root_host)
                if why:
                    pf["abort"] = why
                    isolated = False
            rows.append({"pass": name, "tool": tool, "preflight": pf})
        if not isolated:
            break
        for fid, (tool, form) in sorted(forms.items()):
            t_start = _now()
            data = _dispatch(name, tool, form)
            message = _gate_message(data)
            rows.append({"pass": name, "id": fid, "tool": tool, "pid": os.getpid(), "t_start": t_start,
                         "t_end": _now(), "blocked": message is not None,
                         "message": message if message is not None else "not blocked by the approval gate"})
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
