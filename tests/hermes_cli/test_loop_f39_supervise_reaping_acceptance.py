"""LOOP-F39 — supervise-issues nudger and self-healing worktree reaping.

One test_ac_loop_f39_<n> per ADR §Acceptance criteria id, same order. AC-3 renders the
committed fixture through nv-coworker-compose; AC-1/2/4/5 drive the native reaper, kanban
and diagnostics primitives with differential controls. No network; nothing written under
a real ~/.hermes.
"""

import json
import os
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path


def _git(cwd, *args):
    """Run git in cwd with a hermetic, host-config-free environment."""
    env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.email=t@e", "-c", "user.name=t", *args],
        capture_output=True, text=True, env=env, timeout=30,
    )


def _init_kanban(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an initialised kanban DB."""
    import hermes_cli.kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return kb


_PLUGIN_KEY = "nv-coworker-compose"
# the checkout UNDER TEST, so AC-3 exercises the tree that carries the proposed change
_PLUGIN_SRC = Path(__file__).resolve().parents[2] / "plugins" / _PLUGIN_KEY
_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "loop-f39"


def _load_compose(tmp_path, monkeypatch):
    """Load the nv-coworker-compose plugin from an isolated HERMES_HOME and return
    its module, so the render runs the same whether or not the plugin is bundled."""
    import shutil

    from hermes_cli.plugins import PluginManager

    home = tmp_path / "hermes-home"
    plugins_dir = home / "plugins"
    plugins_dir.mkdir(parents=True)
    shutil.copytree(_PLUGIN_SRC, plugins_dir / _PLUGIN_KEY)
    (home / "config.yaml").write_text(
        json.dumps({"plugins": {"enabled": [_PLUGIN_KEY]}}), encoding="utf-8"
    )
    empty_bundled = tmp_path / "bundled-plugins"
    empty_bundled.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(empty_bundled))
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[_PLUGIN_KEY]
    assert loaded.enabled is True and loaded.error is None and loaded.module is not None
    return loaded.module


def _profile_dir(rendered, out, name):
    # resolve the SPECIFIC requested profile — a wrong-profile fallback would let a
    # mis-rendered distribution read as a pass
    d = Path(rendered[name]) if isinstance(rendered, dict) and name in rendered else Path(out) / name
    assert (d / "config.yaml").exists(), f"no rendered profile dir for {name!r} under {out}"
    return d


def _read_jobs(cron_dir):
    # load_jobs consumes only the "jobs" member, so any other top-level shape loads
    # as zero jobs and must not pass
    data = json.loads((cron_dir / "jobs.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict) and isinstance(data.get("jobs"), list)
    return {j["name"]: j for j in data["jobs"] if isinstance(j, dict) and j.get("name")}


def test_ac_loop_f39_1(tmp_path, monkeypatch):
    """hermes worktree reaping never reaps a tree with uncommitted tracked changes or unique unpushed commits, and a clean merged/pushed tree carrying untracked scratch is classified reap-archive with its untracked files archived (never destroyed) before removal.

    Uses REAL git worktrees so worktree_gc._dirty_split runs live `git status`; only
    the offline/remote/merge leaf probes on `cli` are stubbed for offline determinism.
    """
    from hermes_cli import worktree_gc as wg
    import cli

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-q", "-m", "init")

    wtdir = repo / ".worktrees"
    for name in ("tracked-dirty", "unpushed", "reap-me"):
        assert _git(repo, "worktree", "add", "-q", str(wtdir / name), "-b", name).returncode == 0

    (wtdir / "tracked-dirty" / "README").write_text("edited\n", encoding="utf-8")
    (wtdir / "unpushed" / "feature.txt").write_text("x\n", encoding="utf-8")
    assert _git(wtdir / "unpushed", "add", "feature.txt").returncode == 0
    assert _git(wtdir / "unpushed", "commit", "-q", "-m", "feature").returncode == 0
    (wtdir / "reap-me" / "scratch.txt").write_text("draft pr body\n", encoding="utf-8")

    monkeypatch.setattr(cli, "_repo_is_shallow", lambda *a, **k: False)
    monkeypatch.setattr(cli, "_load_worktree_merge_cache", lambda *a, **k: {})
    monkeypatch.setattr(cli, "_save_worktree_merge_cache", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_fetch_remote_branch_heads", lambda *a, **k: {})
    monkeypatch.setattr(cli, "_worktree_lock_is_live", lambda *a, **k: "none")
    monkeypatch.setattr(cli, "_worktree_has_unpushed_commits",
                        lambda path, *a, **k: Path(path).name == "unpushed")
    monkeypatch.setattr(cli, "_worktree_commits_all_merged_upstream", lambda *a, **k: False)
    monkeypatch.setattr(cli, "_worktree_branch_pushed_exact", lambda *a, **k: False)

    records = {r.name: r for r in wg.audit_worktrees(str(repo), with_sizes=False)}
    assert records["tracked-dirty"].verdict == "keep"
    assert records["unpushed"].verdict == "keep"
    assert records["reap-me"].verdict == "reap-archive"
    assert "scratch.txt" in records["reap-me"].untracked

    # The reaper archives under Path.home()/.hermes, not the active HERMES_HOME (UA-24):
    # pin Path.home at a scratch dir so the archive lands there, never the real home.
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    actions = wg.reclaim_worktrees(str(repo), records=[records["reap-me"]])
    archived = list((fake_home / ".hermes" / "archive" / "worktree-prune").rglob("scratch.txt"))
    assert archived, f"untracked scratch must be archived before removal; actions={actions}"
    assert not (wtdir / "reap-me").exists()


def test_ac_loop_f39_2(tmp_path, monkeypatch):
    """Worker-liveness reaping is native: a running kanban task whose claim TTL expired and whose worker PID is dead is reclaimed to ready by both release_stale_claims (TTL rung) and detect_crashed_workers (crash rung), while a task whose TTL expired but whose worker PID is live with a fresh heartbeat is extended, not reclaimed.
    """
    kb = _init_kanban(tmp_path, monkeypatch)
    host = kb._claimer_id().split(":", 1)[0]
    live_pid = os.getpid()
    dead_pid = 2_147_480_000
    monkeypatch.setattr(kb, "_pid_alive", lambda pid: pid == live_pid)

    with kb.connect() as conn:
        crashed = kb.create_task(conn, title="crashed worker", assignee="a")
        kb.claim_task(conn, crashed, claimer=f"{host}:worker")
        kb._set_worker_pid(conn, crashed, dead_pid)

        healthy = kb.create_task(conn, title="live worker", assignee="a")
        kb.claim_task(conn, healthy, claimer=f"{host}:worker")
        kb._set_worker_pid(conn, healthy, live_pid)

        now = int(time.time())
        conn.execute("UPDATE tasks SET claim_expires=?, last_heartbeat_at=? WHERE id=?",
                     (now - 3600, now - 7200, crashed))
        conn.execute("UPDATE tasks SET claim_expires=?, last_heartbeat_at=? WHERE id=?",
                     (now - 3600, now - 10, healthy))
        conn.commit()

        assert kb.release_stale_claims(conn, signal_fn=lambda _p, _s: None) == 1
        assert kb.get_task(conn, crashed).status == "ready"
        healthy_after = kb.get_task(conn, healthy)
        assert healthy_after.status == "running"
        # extended, not merely left alone: the claim window moved forward and the
        # extension was recorded — a regression that ignored the live PID would fail here
        assert healthy_after.claim_expires is not None and healthy_after.claim_expires > now
        assert any(e.kind == "claim_extended" for e in kb.list_events(conn, healthy))

        monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")   # disable the launch-window grace
        crashed2 = kb.create_task(conn, title="crashed worker 2", assignee="a")
        kb.claim_task(conn, crashed2, claimer=f"{host}:worker")
        kb._set_worker_pid(conn, crashed2, dead_pid)
        conn.commit()
        assert crashed2 in kb.detect_crashed_workers(conn)
        assert kb.get_task(conn, crashed2).status == "ready"
        assert kb.get_task(conn, healthy).status == "running"


def test_ac_loop_f39_3(tmp_path, monkeypatch):
    """nv-coworker-compose renders the supervise-issues skill and its 12h skill-backed --deliver bot-chat AGENT cron into the orchestrator distribution: the rendered profile carries skills/supervise-issues/SKILL.md byte-identical to the committed fixture AND a recurring cron/jobs.json entry with skills=['supervise-issues'], deliver='bot-chat', no_agent falsey, interval 720, and the rendered job loads and re-arms.
    """
    import importlib

    module = _load_compose(tmp_path, monkeypatch)
    out = tmp_path / "out"
    rendered = module.compose(str(_FIXTURE_DIR / "coworker-types.yaml"), str(out))
    orch = _profile_dir(rendered, out, "orchestrator")

    # byte-identity proves the FULL supervisor skill ships, not a placeholder the render
    # could satisfy with any content
    rendered_skill = orch / "skills" / "supervise-issues" / "SKILL.md"
    source_skill = _FIXTURE_DIR / "skills" / "supervise-issues" / "SKILL.md"
    assert rendered_skill.read_bytes() == source_skill.read_bytes()
    job = _read_jobs(orch / "cron")["supervise-issues"]
    assert job["skills"] == ["supervise-issues"]
    assert job["deliver"] == "bot-chat"
    assert not job.get("no_agent"), "the nudger must run the agent, not a no_agent script"
    assert job["schedule"]["kind"] == "interval" and job["schedule"]["minutes"] == 720

    # negative control: the supervisor lands only in the orchestrator distribution
    worker = _profile_dir(rendered, out, "worker")
    assert not (worker / "skills" / "supervise-issues" / "SKILL.md").exists()
    worker_jobs = worker / "cron" / "jobs.json"
    if worker_jobs.exists():
        assert "supervise-issues" not in _read_jobs(worker / "cron")

    # Deploy the rendered jobs.json into a run home exactly as `profile update` would,
    # then prove it loads unchanged and the recurring job re-arms forward.
    run_home = tmp_path / "run-home"
    (run_home / "cron").mkdir(parents=True)
    (run_home / "cron" / "jobs.json").write_bytes((orch / "cron" / "jobs.json").read_bytes())
    (run_home / "config.yaml").write_text(json.dumps({"model": {"default": "stub-model"}}), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(run_home))
    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.jobs as cj
    importlib.reload(cj)

    loaded = {j["name"]: j for j in cj.load_jobs()}
    assert "supervise-issues" in loaded, "the rendered jobs.json must load through load_jobs unchanged"
    jobs = cj.load_jobs()
    for j in jobs:
        if j["name"] == "supervise-issues":
            j["next_run_at"] = (cj._hermes_now() - timedelta(minutes=5)).isoformat()
    cj.save_jobs(jobs)
    jid = loaded["supervise-issues"]["id"]
    assert cj.advance_next_run(jid) is True
    new_next = cj._ensure_aware(datetime.fromisoformat(cj.get_job(jid)["next_run_at"]))
    assert new_next > cj._hermes_now(), "rendered recurring cron must re-arm into the future"


def test_ac_loop_f39_4(tmp_path, monkeypatch):
    """A nudge is durably recorded on the supervised kanban card via the kanban_comment tool and is readable back from the card (the on-card nudge artifact survives; not fire-and-forget only).

    Drives the real kanban_comment tool handler (tools.kanban_tools._handle_comment),
    not add_comment directly, so the test fails if the handler/validation regresses.
    """
    kb = _init_kanban(tmp_path, monkeypatch)
    import tools.kanban_tools as kt

    monkeypatch.setenv("HERMES_PROFILE", "orchestrator")   # handler derives author from this
    with kb.connect() as conn:
        target = kb.create_task(conn, title="silent chain", assignee="a")
        quiet = kb.create_task(conn, title="untouched", assignee="a")
        conn.commit()

    result = kt._handle_comment({"task_id": target, "body": "rebase master — CI stale 2 days"})
    assert "comment_id" in json.loads(result), f"kanban_comment tool did not record a comment: {result}"

    with kb.connect() as conn:
        bodies = [c.body for c in kb.list_comments(conn, target)]
        assert any("rebase master" in b for b in bodies)
        assert kb.list_comments(conn, quiet) == []   # negative control


def test_ac_loop_f39_5(tmp_path, monkeypatch):
    """The nudger's silent-chain detection is native: compute_task_diagnostics flags a card stuck in blocked past the stale threshold (a stuck_in_blocked signal the nudger acts on), while a healthy card is not flagged.

    Pure function over (task, events, runs) — no DB, no network.
    """
    from hermes_cli import kanban_diagnostics as kd

    now = 1_000_000_000
    blocked_at = now - 48 * 3600   # blocked 48h ago; _rule_stuck_in_blocked threshold is 24h
    blocked_task = {"id": "t_1", "title": "silent chain", "status": "blocked"}
    events = [{"kind": "blocked", "created_at": blocked_at}]
    assert "stuck_in_blocked" in {d.kind for d in kd.compute_task_diagnostics(blocked_task, events, [], now=now)}

    running_task = {"id": "t_2", "title": "healthy", "status": "running"}
    assert "stuck_in_blocked" not in {d.kind for d in kd.compute_task_diagnostics(running_task, events, [], now=now)}

    commented = events + [{"kind": "commented", "created_at": now - 3600}]
    assert "stuck_in_blocked" not in {d.kind for d in kd.compute_task_diagnostics(blocked_task, commented, [], now=now)}
