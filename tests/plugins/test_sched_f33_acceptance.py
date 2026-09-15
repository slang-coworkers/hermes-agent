"""Acceptance test for SCHED-F33 — pre-task script gates with failure backoff and auto-pause.

One test per acceptance criterion (node id ``test_ac_sched_f33_<n>``); the first
docstring line is the criterion text in prose form (Markdown formatting
omitted). Behaviour contract only: no
snapshots, model lists, version literals or enumeration counts; no source-file
reads; no network; every fixture roots at ``tmp_path`` (nothing under ~/.hermes).

Cron modules cache ``get_hermes_home()`` at import, so the run_job criteria
reload them after pointing HERMES_HOME at the temp home (the pattern the
in-tree monitor-kind tests use). Scripts are ``.py`` (run via ``sys.executable``)
so the gate/monitor criteria are cross-platform.
"""

import json
from pathlib import Path

import pytest
import yaml

PLUGIN_KEY = "nv-coworker-compose"
# The plugin is installed into an isolated HERMES_HOME (not discovered from the
# repo bundled dir), so the test runs the same way whether or not it is bundled.
PLUGIN_SRC = Path(__file__).resolve().parents[2] / "plugins" / PLUGIN_KEY
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "sched-f33"
FIXTURE_SPEC = FIXTURE_DIR / "coworker-types.yaml"


# --------------------------------------------------------------------------- #
# Compose harness (AC-1, AC-2) — isolated discovery of the bundled plugin
# --------------------------------------------------------------------------- #
def _load_compose(tmp_path, monkeypatch):
    import shutil

    from hermes_cli.plugins import PluginManager

    home = tmp_path / "hermes-home"
    plugins_dir = home / "plugins"
    plugins_dir.mkdir(parents=True)
    shutil.copytree(PLUGIN_SRC, plugins_dir / PLUGIN_KEY)
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": [PLUGIN_KEY]}}), encoding="utf-8"
    )
    empty_bundled = tmp_path / "bundled-plugins"
    empty_bundled.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(empty_bundled))
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled is True
    assert loaded.error is None
    assert loaded.module is not None
    return loaded.module


def _profile_dir(rendered, out, name):
    # resolve the SPECIFIC requested profile — never fall back to an arbitrary
    # profile, which would let a wrong-profile render read as a pass
    if isinstance(rendered, dict) and name in rendered:
        d = Path(rendered[name])
    else:
        d = Path(out) / name
    assert (d / "config.yaml").exists(), f"no rendered profile dir for {name!r} under {out}"
    return d


def _read_jobs(cron_dir):
    # load_jobs() consumes only the "jobs" member, so the render must produce
    # the canonical {"jobs": [...]} shape — any other top-level shape loads as
    # zero jobs and must not pass.
    data = json.loads((cron_dir / "jobs.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict) and isinstance(data.get("jobs"), list), (
        "cron/jobs.json must be the canonical {'jobs': [...]} shape load_jobs consumes"
    )
    return {j["name"]: j for j in data["jobs"] if isinstance(j, dict) and j.get("name")}


# --------------------------------------------------------------------------- #
# run_job harness (AC-5, AC-6, AC-7) — temp HERMES_HOME + fake agent
# --------------------------------------------------------------------------- #
@pytest.fixture
def agent_env(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "scripts").mkdir(parents=True)
    (home / "cron").mkdir(parents=True)
    # run_job fails fast with "no model configured" (cron/scheduler.py:5988)
    # before the stubbed agent is reached unless a model resolves from
    # job/env/config.yaml. The agent itself is stubbed (see _install_agent_stubs),
    # so a bare model.default is enough to pass that gate hermetically.
    (home / "config.yaml").write_text(
        yaml.safe_dump({"model": {"default": "stub-model"}}), encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    # cron modules cache get_hermes_home() at import — reload so storage,
    # scripts and monitor snapshots resolve under the temp home.
    import importlib

    import hermes_constants

    importlib.reload(hermes_constants)
    import cron.jobs

    importlib.reload(cron.jobs)
    import cron.monitor

    importlib.reload(cron.monitor)
    import cron.scheduler

    importlib.reload(cron.scheduler)
    return home


def _install_agent_stubs(monkeypatch, observed):
    """Fake run_agent + neutralize delivery/provider so run_job stays hermetic.

    run_job imports AIAgent from run_agent at fire time, so replacing the module
    in sys.modules captures every agent invocation and the prompt it received.
    """
    import sys

    import cron.scheduler as sched

    observed.setdefault("prompts", [])
    observed.setdefault("agent_runs", 0)

    class FakeAgent:
        def __init__(self, **kwargs):
            pass

        def run_conversation(self, prompt, *_a, **_kw):
            observed["agent_runs"] += 1
            observed["prompts"].append(prompt)
            return {"final_response": "agent done", "messages": []}

        def get_activity_summary(self):
            return {"seconds_since_activity": 0.0}

    fake_mod = type(sys)("run_agent")
    fake_mod.AIAgent = FakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_mod)

    from hermes_cli import runtime_provider as _rtp

    monkeypatch.setattr(
        _rtp,
        "resolve_runtime_provider",
        lambda **_kw: {
            "provider": "test",
            "api_key": "k",
            "base_url": "http://test.local",
            "api_mode": "chat_completions",
        },
    )
    monkeypatch.setattr(sched, "_resolve_origin", lambda job: None)
    monkeypatch.setattr(sched, "_resolve_delivery_target", lambda job: None)
    monkeypatch.setattr(sched, "_resolve_cron_enabled_toolsets", lambda job, cfg: None)
    monkeypatch.setenv("HERMES_CRON_TIMEOUT", "0")
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_a, **_kw: True)


def _write_script(home, name, body):
    (home / "scripts" / name).write_text(body, encoding="utf-8")
    return name


# --------------------------------------------------------------------------- #
def test_ac_sched_f33_1(tmp_path, monkeypatch):
    """The compose plugin renders the fleet's cron config keys — cron.script_timeout_seconds: 30 (NanoClaw parity) and cron.failure_nudge_threshold: 3 — by value into each rendered coworker profile's config.yaml."""
    module = _load_compose(tmp_path, monkeypatch)
    out = tmp_path / "out"
    rendered = module.compose(str(FIXTURE_SPEC), str(out))
    # every coworker profile that inherits the spine carries the keys by value
    # (the rendered file itself, not a code default)
    for prof in ("orchestrator", "fixer"):
        cfg = yaml.safe_load((_profile_dir(rendered, out, prof) / "config.yaml").read_text(encoding="utf-8"))
        cron_cfg = (cfg or {}).get("cron") or {}
        assert cron_cfg.get("script_timeout_seconds") == 30, prof
        assert cron_cfg.get("failure_nudge_threshold") == 3, prof


# --------------------------------------------------------------------------- #
def test_ac_sched_f33_2(tmp_path, monkeypatch):
    """The compose plugin renders a coworker type's declared pre-task cron jobs into that profile's cron/jobs.json, carrying the script field and the tool-facing monitor alias normalized to monitor_script/monitor_url."""
    from cron.jobs import compute_next_run

    module = _load_compose(tmp_path, monkeypatch)
    out = tmp_path / "out"
    rendered = module.compose(str(FIXTURE_SPEC), str(out))
    cron_dir = _profile_dir(rendered, out, "fixer") / "cron"
    assert (cron_dir / "jobs.json").exists(), "compose must render cron/jobs.json for declared cron jobs"
    by_name = _read_jobs(cron_dir)

    # append order across the extends chain: the spine job first, then the type's
    # own in declaration order (append semantics, not a merge or a sort)
    payload = json.loads((cron_dir / "jobs.json").read_text(encoding="utf-8"))
    assert [j["name"] for j in payload["jobs"]] == [
        "fleet_doctor", "prewarm", "watch_url", "watch_script"
    ]
    # no now-based field is rendered — the distribution must re-render
    # byte-identically on any host at any time (the byte-identical check below can
    # pass with a now-based field when two renders land in the same cron interval,
    # so assert absence directly). next_run_at/failure_streak are recovered by the
    # scheduler's due-scan, not written by the render.
    for j in payload["jobs"]:
        for now_based in ("next_run_at", "created_at", "last_run_at", "failure_streak"):
            assert now_based not in j, f"{j['name']}: {now_based} must not be rendered"

    # every declared job (spine 'fleet_doctor' appended to the type's three) must
    # render as a schedulable record: a parsed dict schedule, a non-empty id, and
    # a computable next run (a string schedule is repaired to {} on scan and never
    # fires — compute_next_run is exactly the recovery get_due_jobs performs)
    expected_expr = {
        "fleet_doctor": "0 */12 * * *",
        "prewarm": "*/15 * * * *",
        "watch_url": "0 * * * *",
        "watch_script": "0 0 * * *",
    }
    assert set(expected_expr) <= set(by_name)
    for name, expr in expected_expr.items():
        job = by_name[name]
        assert isinstance(job.get("id"), str) and job["id"], f"{name}: non-empty string id"
        sched = job.get("schedule")
        assert isinstance(sched, dict) and sched.get("kind") == "cron" and sched.get("expr") == expr, f"{name}: parsed dict schedule"
        assert compute_next_run(sched) is not None, f"{name}: schedulable"

    # a constant id would pass the byte-identical re-render below but is wrong
    ids = [by_name[n]["id"] for n in expected_expr]
    assert len(set(ids)) == len(ids)

    assert by_name["prewarm"].get("script") == "prewarm.py"
    # `monitor` alias -> monitor_url (http/https) else monitor_script; the alias never persists
    assert by_name["watch_url"].get("monitor_url") == "https://example.invalid/health"
    assert by_name["watch_url"].get("monitor_script") in (None, "")
    assert "monitor" not in by_name["watch_url"]
    assert by_name["watch_script"].get("monitor_script") == "healthcheck.py"
    assert by_name["watch_script"].get("monitor_url") in (None, "")
    assert "monitor" not in by_name["watch_script"]

    # the spine's fleet-wide job is inherited (with its script) by every coworker profile
    assert by_name["fleet_doctor"].get("script") == "doctor.py"
    orch_jobs = _read_jobs(_profile_dir(rendered, out, "orchestrator") / "cron")
    assert orch_jobs["fleet_doctor"].get("script") == "doctor.py"

    # the profile is part of the deterministic id seed: the inherited fleet_doctor
    # gets a DIFFERENT id per profile (execution history is keyed by id), so the
    # id is not a function of the job name alone
    assert orch_jobs["fleet_doctor"]["id"] != by_name["fleet_doctor"]["id"]

    # deterministic ids: re-rendering the unchanged spec is byte-identical
    # (compose writes no now-based fields; next_run_at is recovered on scan)
    out2 = tmp_path / "out2"
    rendered2 = module.compose(str(FIXTURE_SPEC), str(out2))
    j2 = (_profile_dir(rendered2, out2, "fixer") / "cron" / "jobs.json").read_bytes()
    assert (cron_dir / "jobs.json").read_bytes() == j2, "re-render must be byte-identical"


# --------------------------------------------------------------------------- #
def test_ac_sched_f33_3(monkeypatch):
    """The scheduler honors the rendered cron.script_timeout_seconds: with no module or env override, script-timeout resolution returns the configured value (30) and falls back to 3600 when the key is absent."""
    import cron.scheduler as scheduler

    # skip the higher-precedence module-override and env branches so config wins
    monkeypatch.setattr(scheduler, "_SCRIPT_TIMEOUT", scheduler._DEFAULT_SCRIPT_TIMEOUT)
    monkeypatch.delenv("HERMES_CRON_SCRIPT_TIMEOUT", raising=False)
    monkeypatch.setattr(scheduler, "load_config", lambda: {"cron": {"script_timeout_seconds": 30}})
    assert scheduler._get_script_timeout() == 30
    monkeypatch.setattr(scheduler, "load_config", lambda: {"cron": {}})
    assert scheduler._get_script_timeout() == scheduler._DEFAULT_SCRIPT_TIMEOUT


# --------------------------------------------------------------------------- #
def test_ac_sched_f33_4(monkeypatch):
    """The scheduler fires the failure-streak review nudge at the rendered cron.failure_nudge_threshold: a recurring job whose prospective streak reaches the threshold gets an advisory nudge; below threshold, one-shot jobs, and threshold <= 0 get none."""
    import cron.scheduler as scheduler

    # a non-default threshold (5) proves config drives it, not the hardcoded default of 3
    monkeypatch.setattr(scheduler, "load_config", lambda: {"cron": {"failure_nudge_threshold": 5}})

    def recurring(streak):
        return {"name": "j", "schedule": {"kind": "cron"}, "failure_streak": streak}

    # prospective streak = stored + 1
    assert scheduler._failure_streak_nudge(recurring(4)) != ""
    assert scheduler._failure_streak_nudge(recurring(3)) == ""
    assert scheduler._failure_streak_nudge({"name": "o", "schedule": {"kind": "once"}, "failure_streak": 99}) == ""

    monkeypatch.setattr(scheduler, "load_config", lambda: {"cron": {"failure_nudge_threshold": 0}})
    assert scheduler._failure_streak_nudge(recurring(99)) == ""


# --------------------------------------------------------------------------- #
def test_ac_sched_f33_5(agent_env, monkeypatch):
    """A pre-task script gate skips the agent run entirely (silent marker, no LLM, no delivery) only when its last non-empty stdout line is JSON {"wakeAgent": false}; any other last line (missing key, true, non-JSON, non-dict) wakes it."""
    import cron.scheduler as scheduler

    assert scheduler._parse_wake_gate('{"wakeAgent": false}') is False
    assert scheduler._parse_wake_gate('{"wakeAgent": true}') is True
    assert scheduler._parse_wake_gate('{"other": 1}') is True
    assert scheduler._parse_wake_gate("not json at all") is True
    assert scheduler._parse_wake_gate("") is True
    assert scheduler._parse_wake_gate("[1, 2, 3]") is True
    assert scheduler._parse_wake_gate('noise\n{"wakeAgent": false}') is False
    assert scheduler._parse_wake_gate('{"wakeAgent": false}\ntrailing') is True

    from cron.jobs import create_job
    from cron.scheduler import SILENT_MARKER, run_job

    observed = {}
    _install_agent_stubs(monkeypatch, observed)
    _write_script(agent_env, "gate.py", 'print(\'{"wakeAgent": false}\')\n')
    job = create_job(prompt="do it", schedule="every 5m", script="gate.py", deliver="local")
    success, doc, final, error = run_job(job)
    assert final == SILENT_MARKER
    assert observed["agent_runs"] == 0

    _write_script(agent_env, "gate.py", 'print(\'{"wakeAgent": true}\')\n')
    job2 = create_job(prompt="do it", schedule="every 5m", script="gate.py", deliver="local")
    run_job(job2)
    assert observed["agent_runs"] == 1


# --------------------------------------------------------------------------- #
def test_ac_sched_f33_6(agent_env, monkeypatch):
    """A pre-task script that exits nonzero does NOT gate the run — its failure is surfaced as ## Script Error context and the agent still runs (the NanoClaw-parity difference: only wakeAgent:false gates)."""
    import cron.scheduler as scheduler

    _write_script(agent_env, "fail.py", "import sys\nsys.stderr.write('boom')\nsys.exit(3)\n")
    ok, output = scheduler._run_job_script("fail.py")
    assert ok is False
    assert "exited with code 3" in output

    # the parity difference: a failed pre-task script is context, not a skip
    from cron.jobs import create_job
    from cron.scheduler import SILENT_MARKER, run_job

    observed = {}
    _install_agent_stubs(monkeypatch, observed)
    job = create_job(prompt="analyze the feed", schedule="every 5m", script="fail.py", deliver="local")
    success, doc, final, error = run_job(job)
    assert final != SILENT_MARKER
    assert observed["agent_runs"] == 1
    assert "## Script Error" in observed["prompts"][0]


# --------------------------------------------------------------------------- #
def test_ac_sched_f33_7(agent_env, monkeypatch):
    """A monitor change-detector gate suppresses the agent run when the monitor output is unchanged from the previous agent-triggering tick (first tick / changed output wakes it)."""
    from cron.jobs import create_job, get_job
    from cron.scheduler import SILENT_MARKER, run_job

    observed = {}
    _install_agent_stubs(monkeypatch, observed)
    _write_script(agent_env, "mon.py", "print('state A')\n")
    job = create_job(prompt="summarize the change", schedule="every 5m", monitor_script="mon.py", deliver="local")

    run_job(get_job(job["id"]) or job)
    assert observed["agent_runs"] == 1

    success, doc, final, error = run_job(get_job(job["id"]) or job)
    assert final == SILENT_MARKER
    assert observed["agent_runs"] == 1

    _write_script(agent_env, "mon.py", "print('state B')\n")
    run_job(get_job(job["id"]) or job)
    assert observed["agent_runs"] == 2


# --------------------------------------------------------------------------- #
# Beside unit tests — fail-closed rendering of malformed cron_jobs declarations.
# Each builds an inline spec under tmp_path; the architect's frozen fixture
# (whose ids are the chain join-key) is never mutated.
# --------------------------------------------------------------------------- #
def _write_inline_spec(spec_dir, types, spine):
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spines").mkdir(exist_ok=True)
    for sub in ("skills", "workflows", "overlays"):
        (spec_dir / sub).mkdir(exist_ok=True)
    (spec_dir / "spines" / "base.yaml").write_text(yaml.safe_dump(spine), encoding="utf-8")
    spec = {
        "project": "f33-inline",
        "default_profile": "default",
        "orchestrator_profile": "orchestrator",
        "spines": {"base": {"source": "spines/base.yaml"}},
        "skills_root": "skills",
        "workflows_root": "workflows",
        "overlays_root": "overlays",
        "types": types,
    }
    path = spec_dir / "coworker-types.yaml"
    path.write_text(yaml.safe_dump(spec), encoding="utf-8")
    return path


def test_cron_jobs_duplicate_name_rejected(tmp_path, monkeypatch):
    """A duplicate cron job name across the resolved extends chain is a render error."""
    module = _load_compose(tmp_path, monkeypatch)
    spine = {
        "identity": "BASE",
        "cron_jobs": [{"name": "dup", "schedule": "*/5 * * * *", "prompt": "p", "script": "a.py"}],
    }
    types = {
        "orchestrator": {"extends": ["base"], "identity": "O"},
        "worker": {
            "extends": ["base"],
            "identity": "W",
            "cron_jobs": [{"name": "dup", "schedule": "0 * * * *", "prompt": "q", "script": "b.py"}],
        },
    }
    spec = _write_inline_spec(tmp_path / "spec", types, spine)
    with pytest.raises(module.CompositionError, match="duplicate.*dup"):
        module.compose(str(spec), str(tmp_path / "out"))


def test_cron_jobs_no_agent_invariants_rejected(tmp_path, monkeypatch):
    """no_agent combined with a monitor gate, and no_agent without a script, are render errors (native execution-mode invariants)."""
    module = _load_compose(tmp_path, monkeypatch)
    spine = {"identity": "BASE"}
    base_types = {"orchestrator": {"extends": ["base"], "identity": "O"}}

    # no_agent + monitor gate → rejected
    types_monitor = dict(base_types)
    types_monitor["worker"] = {
        "extends": ["base"], "identity": "W",
        "cron_jobs": [{"name": "bad", "schedule": "*/5 * * * *", "script": "s.py",
                       "monitor": "m.py", "no_agent": True}],
    }
    spec1 = _write_inline_spec(tmp_path / "spec1", types_monitor, spine)
    with pytest.raises(module.CompositionError, match="no_agent"):
        module.compose(str(spec1), str(tmp_path / "out1"))

    # no_agent without a script → rejected
    types_noscript = dict(base_types)
    types_noscript["worker"] = {
        "extends": ["base"], "identity": "W",
        "cron_jobs": [{"name": "bad", "schedule": "*/5 * * * *", "prompt": "p", "no_agent": True}],
    }
    spec2 = _write_inline_spec(tmp_path / "spec2", types_noscript, spine)
    with pytest.raises(module.CompositionError, match="no_agent requires a script"):
        module.compose(str(spec2), str(tmp_path / "out2"))


def test_cron_jobs_relative_oneshot_rejected(tmp_path, monkeypatch):
    """A relative one-shot schedule ('in 30m') is rejected — its run_at is computed from render time and is not deterministic in a distribution."""
    module = _load_compose(tmp_path, monkeypatch)
    spine = {"identity": "BASE"}
    types = {
        "orchestrator": {"extends": ["base"], "identity": "O"},
        "worker": {
            "extends": ["base"], "identity": "W",
            "cron_jobs": [{"name": "relative", "schedule": "in 30m", "prompt": "p"}],
        },
    }
    spec = _write_inline_spec(tmp_path / "spec", types, spine)
    with pytest.raises(module.CompositionError, match="relative one-shot"):
        module.compose(str(spec), str(tmp_path / "out"))
