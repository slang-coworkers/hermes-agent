"""openshell 0.0.72 CLI-contract checks for nv-ingress (ING-F66 E16, ADR §Acceptance test (A)-(C), (E)).

The fake, lint, and mutation fixtures check the 0.0.72 argv contract offline: the fake CLI (A) refuses
what 0.0.72 refuses, the lint (B) finds it in source, and both are proven on two failing forms kept as
fixtures (C): a positional sandbox name and a multi-line exec argument. (D) lives in the unit file.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.plugins import fake_openshell

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_SRC = REPO_ROOT / "plugins" / "nv-ingress"
MUTANTS = Path(__file__).resolve().parent / "fixtures" / "ing-f66-openshell"
F767641 = MUTANTS / "gateway_boot-f767641.py"
C16D47C = MUTANTS / "gateway_boot-c16d47c.py"
_SEP = "\x1f"
_INIT = f"1\tS\topenshell-sandb\topenshell-sandbox{_SEP}--policy{_SEP}/etc/policy.yaml{_SEP}\n#end\n"
_LIVE = f"9025\tS\thermes.real\t/opt/hermes/bin/hermes.real{_SEP}gateway{_SEP}run{_SEP}"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _lint():
    return _load(PLUGIN_SRC / "edge" / "openshell_lint.py", "_nv_ingress_openshell_lint")


def _ticks(tmp_path, module_path: Path, *, healthy: bool, inspect: str, offsets=(0,)):
    boot = _load(module_path, f"_gateway_boot_{module_path.stem.replace('-', '_')}")
    fake = fake_openshell.install(tmp_path, probe=[{"rc": 0 if healthy else 7}], inspect=[{"rc": 0, "stdout": inspect}])
    gw = {"openshell_bin": fake.bin, "sandbox": "ing-f66-gw", "port": 18644,
          "lock_file": str(tmp_path / "boot.lock"), "start": fake.start}
    t0 = time.time()
    return fake, [boot.tick(gw, t0 + o) for o in offsets]


# (B) the lint ---------------------------------------------------------------------------------------

def _shipped_scripts():
    """``plugins/`` plus the scenario and fixture scripts under ``tests/`` (test code holds deliberate negatives)."""
    tests = REPO_ROOT / "tests"
    return [REPO_ROOT / "plugins", tests / "e2e-scenarios", *sorted(tests.glob("**/fixtures"))]


def test_openshell_lint_finds_nothing_in_plugins_and_shipped_scripts():
    """(B) CI: no `sandbox exec` under plugins/ or the scenario and fixture scripts (mutants aside) breaks a 0.0.72 rule."""
    findings, sites = _lint().lint_paths(_shipped_scripts(), exclude=[MUTANTS])
    assert findings == []
    assert sites >= 1, "the plugin's own gateway-boot exec must be among the checked sites"


def test_openshell_lint_flags_the_f767641_positional_probe():
    """(C) The lint names f767641's positional `sandbox exec` at :45."""
    findings, _ = _lint().lint_paths([F767641])
    assert [(line, rule) for _, line, rule, _ in findings] == [(45, "positional-name")]


def test_openshell_lint_flags_the_c16d47c_multiline_inspection():
    """(C) The lint names c16d47c's multi-line exec argument at :51 and the call that binds it."""
    findings, _ = _lint().lint_paths([C16D47C])
    assert [(line, rule) for _, line, rule, _ in findings] == [(51, "multiline-arg")]
    assert "line 137" in findings[0][3]


@pytest.mark.parametrize("name, body, rules", [
    ("positional.sh", 'openshell sandbox exec "$SB" -- curl -fsS http://127.0.0.1:1/health\n', ["positional-name"]),
    ("named.sh", 'openshell sandbox exec -n "$SB" --no-tty -- sh -c "a; b"\n', []),
    ("long-name.sh", 'openshell sandbox exec --name "$SB" -- true\n', []),
    ("continued.sh", 'openshell sandbox exec \\\n  -n "$SB" --no-tty -- true\n', []),
    ("multiline.sh", "openshell sandbox exec -n \"$SB\" -- sh -c 'echo a\necho b'\n", ["multiline-arg"]),
    ("comment.sh", '# openshell sandbox exec "$SB" -- true\necho done\n', []),
    ("python-c.sh", "python3 -c '\nimport sys\nprint(1)\n'\nopenshell sandbox exec -n x -- true\n", []),
    ("unit.service", '[Service]\nExecStart=/bin/sh -c "openshell sandbox exec -n gw -- true"\n', []),
    ("open.service", '[Service]\nExecStart=/bin/sh -c "echo a\n', ["multiline-arg"]),
    ("unit-positional.service", "[Service]\nExecStart=openshell sandbox exec gw -- true\n", ["positional-name"]),
    ("split-verb.sh", "openshell sandbox \\\n  exec gw -- true\n", ["positional-name"]),
    ("subst-name.sh", 'openshell sandbox exec -n $(printf gw) -- sh -c "echo a\necho b"\n', ["multiline-arg"]),
])
def test_openshell_lint_shell_and_unit_rules(tmp_path, name, body, rules):
    """(B) Shell and unit files: the name rule on both, the open-quote rule on `.sh` only within an exec argv."""
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    findings, sites = _lint().lint_paths([path])
    assert [rule for *_, rule, _ in findings] == rules
    assert sites == (0 if name in ("comment.sh", "open.service") else 1), "each live exec is one checked site"


def test_openshell_lint_cli_exits_by_finding(tmp_path):
    """(B) `python3 -m edge.openshell_lint` exits 1 naming each finding, 0 on a clean tree, 2 on a missing path."""
    def run(*paths):
        return subprocess.run([sys.executable, "-m", "edge.openshell_lint", *map(str, paths)], cwd=PLUGIN_SRC,
                              capture_output=True, text=True, encoding="utf-8", timeout=120)
    bad = run(F767641, C16D47C)
    assert bad.returncode == 1 and ":45: positional-name" in bad.stdout and ":51: multiline-arg" in bad.stdout
    assert run(PLUGIN_SRC).returncode == 0
    assert run(tmp_path / "absent.sh").returncode == 2


# (A) the fake ---------------------------------------------------------------------------------------

@pytest.mark.linux_only
@pytest.mark.parametrize("argv, refused, reason", [
    (["sandbox", "exec", "-n", "gw", "--no-tty", "--", "true"], False, None),
    (["sandbox", "exec", "--name", "gw", "--", "true"], False, None),
    (["sandbox", "list"], False, None),
    (["sandbox", "exec", "gw", "--", "true"], True, "positional"),
    (["sandbox", "exec", "--", "true"], True, "-n/--name"),
    (["sandbox", "exec", "-n", "gw", "--tty", "--", "true"], True, "unknown"),
    (["sandbox", "get", "gw"], True, "unknown"),
    (["sandbox", "list", "--all"], True, "unknown"),
    (["sandbox", "exec", "-n", "gw", "--", "sh", "-c", "a\nb"], True, "command argument 2 contains newline"),
    (["sandbox", "exec", "-n", "gw", "--", "sh", "-c", "a\rb"], True, "command argument 2 contains newline"),
], ids=["n", "name", "list", "positional", "unnamed", "unknown-flag", "unknown-verb", "list-flag", "lf", "cr"])
def test_fake_openshell_enforces_the_0_0_72_contract(tmp_path, argv, refused, reason):
    """(A) The fake accepts only the two 0.0.72 calls gateway-boot makes, and records every call."""
    fake = fake_openshell.install(tmp_path)
    proc = subprocess.run([fake.bin, *argv], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert fake.calls() == [argv]
    if refused:
        assert proc.returncode != 0
        (entry,) = fake.refusals()
        assert entry["argv"] == argv and reason in entry["reason"]
    else:
        assert proc.returncode == 0 and fake.refusals() == []


@pytest.mark.linux_only
def test_fake_openshell_refuses_the_f767641_probe(tmp_path):
    """(C) Through the fake, f767641's positional probe is refused, so a healthy gateway reads down and is launched beside."""
    fake, actions = _ticks(tmp_path, F767641, healthy=True, inspect=_INIT)
    assert actions == ["start"]
    assert [r["reason"] for r in fake.refusals()][:1] and "positional" in fake.refusals()[0]["reason"]
    assert fake.launches(expect=1) == 1


@pytest.mark.linux_only
def test_fake_openshell_refuses_the_c16d47c_inspection(tmp_path):
    """(C) Through the fake, c16d47c's multi-line inspection is refused, so a restarted sandbox gets no start."""
    fake, actions = _ticks(tmp_path, C16D47C, healthy=False, inspect=_INIT)
    assert actions == ["unknown"]
    assert [r for r in fake.refusals() if "command argument 2 contains newline" in r["reason"]]
    assert fake.launches() == 0


@pytest.mark.linux_only
@pytest.mark.parametrize("healthy, actions, launches", [(True, ["idle"], 0), (False, ["start"], 1)],
                         ids=["healthy", "restarted"])
def test_fake_openshell_accepts_every_call_at_the_fix_head(tmp_path, healthy, actions, launches):
    """(C) At the fix head the fake refuses nothing: a healthy gateway gets 0 launches, an empty sandbox exactly 1."""
    fake, got = _ticks(tmp_path, PLUGIN_SRC / "edge" / "gateway_boot.py", healthy=healthy, inspect=_INIT)
    assert got == actions and fake.refusals() == []
    assert fake.launches(expect=launches) == launches


# (E) the storm guard --------------------------------------------------------------------------------

@pytest.mark.linux_only
def test_storm_guard_never_starts_or_kills_beside_a_live_gateway_through_the_fake(tmp_path, monkeypatch):
    """(E) A live `hermes.real gateway run` with a failing probe, checked at 0, 60, 130, 250 s → 0 launches, no kill."""
    boot = _load(PLUGIN_SRC / "edge" / "gateway_boot.py", "_gateway_boot_storm_guard")
    kills: list = []
    monkeypatch.setattr(boot, "os", SimpleNamespace(path=os.path, kill=lambda *a: kills.append(a),
                                                    killpg=lambda *a: kills.append(a)))
    fake = fake_openshell.install(tmp_path, probe=[{"rc": 7}], inspect=[{"rc": 0, "stdout": f"{_LIVE}\n#end\n"}])
    gw = {"openshell_bin": fake.bin, "sandbox": "ing-f66-gw", "port": 18644,
          "lock_file": str(tmp_path / "boot.lock"), "start": fake.start}
    t0 = time.time()
    assert [boot.tick(gw, t0 + o) for o in (0, 60, 130, 250)] == ["held"] * 4
    assert fake.launches() == 0 and kills == [] and fake.refusals() == []
    assert len(fake.calls("inspect")) == 4
