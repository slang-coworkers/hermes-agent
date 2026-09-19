"""Acceptance test for LOOP-F40 — PR review coworkers, CI-gated release, human-invited post-back.

Ships to: tests/plugins/test_loop_f40_acceptance.py. It renders the single canonical
GitHub-ingress fixture (tests/plugins/fixtures/ch-f52/coworker-types.yaml) — the same
fixture the CH-F52 routing test renders — and asserts the reconciled six-route gated
contract: the reviewer pull_request route carries the CI-gate script, the check_suite
route carries the promote script, and gh-pr-review-invite carries deliver: github_comment
gated by a human-mention route filter.

Shape mirrors tests/plugins/test_nv_coworker_compose_acceptance.py: load nv-coworker-compose
from an isolated HERMES_HOME with an EMPTY bundled dir through the real discovery path,
render the fixture, then DRIVE the rendered routes + route scripts and assert each
criterion's observable behaviour.

One test function per pytest: criterion, named test_ac_loop_f40_1 .. test_ac_loop_f40_4.
AC-LOOP-F40-5 is live: (tests/e2e-scenarios/LOOP-F40/AC-LOOP-F40-5.md) — no function here.

Acceptance asymmetry: when the plugin is absent the render produces none of these
routes/scripts, so the assertions raise (KeyError/AssertionError); when it is present they
pass. Behavior contract, not snapshot: no model lists / version literals / enumeration
counts; never reads plugin source; no network; nothing under ~/.hermes.
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

PLUGIN_KEY = "nv-coworker-compose"
FIXTURE_SPEC = Path(__file__).parent / "fixtures" / "ch-f52" / "coworker-types.yaml"

REVIEWER = "reviewer"
FIXER = "fixer"
REVIEWER_HANDLE = "@slang-reviewer"        # the invite route filters on a boundary mention of this
REVIEWER_LOGIN = "slang-reviewer[bot]"      # the reviewer bot's own comment login (self-loop guard)
REPO = "slang-coworkers/hermes-agent"
PR = 12
SHA_A = "a" * 40
SHA_B = "b" * 40

# Actions each route must admit, character-for-character (equality, not subset).
PR_ACTIONS = {"opened", "reopened", "synchronize", "ready_for_review"}
CHECK_ACTIONS = {"completed"}
COMMENT_ACTIONS = {"created"}


def _render(tmp_path, monkeypatch):
    import shutil

    from hermes_cli.plugins import PluginManager

    hermes_home = tmp_path / "home"
    (hermes_home / "plugins").mkdir(parents=True)
    plugin_src = _fork_root() / "plugins" / PLUGIN_KEY
    shutil.copytree(plugin_src, hermes_home / "plugins" / PLUGIN_KEY)
    (hermes_home / "config.yaml").write_text(
        "plugins:\n  enabled: [%s]\n" % PLUGIN_KEY, encoding="utf-8"
    )
    empty_bundled = tmp_path / "bundled"
    empty_bundled.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(empty_bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")

    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[PLUGIN_KEY]
    assert loaded.enabled is True and loaded.error is None and loaded.module is not None

    out_root = tmp_path / "out"
    out_root.mkdir()
    rendered = loaded.module.compose(str(FIXTURE_SPEC), str(out_root))
    return rendered, Path(rendered["default"])


def _fork_root():
    p = Path(__file__).resolve()
    for parent in p.parents:
        if (parent / "plugins" / PLUGIN_KEY).is_dir():
            return parent
    raise AssertionError("could not locate the fork root containing plugins/" + PLUGIN_KEY)


def _cfg(profile_dir):
    import yaml

    return yaml.safe_load((Path(profile_dir) / "config.yaml").read_text(encoding="utf-8"))


def _routes(default_dir):
    return (
        ((_cfg(default_dir).get("platforms") or {}).get("webhook") or {})
        .get("extra", {})
        .get("routes", {})
    )


def _action_filters(route):
    return [
        spec
        for spec in route.get("filters") or []
        if isinstance(spec, dict) and spec.get("field") == "action" and "in" in spec
    ]


def _sole_action_set(route):
    filters = _action_filters(route)
    assert len(filters) == 1, "exactly one `action in [...]` filter (union of two is deny-all at runtime)"
    return set(filters[0]["in"])


def _load_script(default_dir, name):
    path = default_dir / "scripts" / name
    assert path.is_file(), f"rendered route script missing: {path}"
    spec = importlib.util.spec_from_file_location(f"loop_f40_{name[:-3]}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _key(head_sha):
    return f"loop-f40-review:{REPO}#{PR}:{head_sha}"


def _card_by_key(kanban_db_path, key):
    con = sqlite3.connect(kanban_db_path)
    try:
        return con.execute(
            "SELECT status, assignee FROM tasks WHERE idempotency_key = ?", (key,)
        ).fetchone()
    finally:
        con.close()


def _pr_event(action, head_sha, pr=PR, updated_at="2026-01-01T00:00:00Z"):
    # updated_at defaults to a fixed stamp so the ordering guard (strictly-older ignored)
    # never trips for equal-timestamp events; the ordering-guard arm passes explicit values.
    return {
        "action": action,
        "repository": {"full_name": REPO},
        "number": pr,
        "pull_request": {"number": pr, "head": {"sha": head_sha}, "updated_at": updated_at},
    }


def _check_suite(conclusion, head_sha, pr=PR):
    return {
        "action": "completed",
        "repository": {"full_name": REPO},
        "check_suite": {
            "head_sha": head_sha,
            "conclusion": conclusion,
            "pull_requests": [{"number": pr}],
        },
    }


def _comment(body, user_type="User", login="octocat", is_pr=True):
    issue = {"number": PR}
    if is_pr:
        issue["pull_request"] = {"url": "https://api.github.com/x"}
    return {
        "action": "created",
        "repository": {"full_name": REPO},
        "issue": issue,
        "comment": {"body": body, "user": {"type": user_type, "login": login}},
    }


def test_ac_loop_f40_1(tmp_path, monkeypatch):
    """The rendered DEFAULT-profile config declares the CI-gated reviewer pull-request
    route, the check-suite route, and the human-invitation route (deliver: github_comment
    + deliver_extra + issue.pull_request predicate + toolsets [terminal, hermes-webhook]);
    both scripts render under the DEFAULT profile's scripts/ and "scripts" is
    distribution-owned; no reviewer pull_request route lacks the CI-gate script; and no
    coworker profile enables any port-binding platform."""
    from gateway.config import platform_binds_port

    rendered, default_dir = _render(tmp_path, monkeypatch)

    webhook = (_cfg(default_dir).get("platforms") or {}).get("webhook") or {}
    assert webhook.get("enabled") is True

    routes = _routes(default_dir)

    pr_route = routes["gh-pull-request"]
    assert pr_route["profile"] == REVIEWER
    assert pr_route["events"] == ["pull_request"]
    assert pr_route.get("script") == "pr_ci_gate.py"
    assert not pr_route.get("prompt")
    assert _sole_action_set(pr_route) == PR_ACTIONS

    check_route = routes["gh-check-suite"]
    assert check_route["profile"] == FIXER
    assert check_route["events"] == ["check_suite"]
    assert check_route.get("script") == "check_gate.py"
    assert _sole_action_set(check_route) == CHECK_ACTIONS

    invite = routes["gh-pr-review-invite"]
    assert invite["profile"] == REVIEWER
    assert invite["events"] == ["issue_comment"]
    assert _sole_action_set(invite) == COMMENT_ACTIONS
    assert invite.get("deliver") == "github_comment"
    extra = invite.get("deliver_extra") or {}
    assert "{repository.full_name}" in extra.get("repo", "")
    assert "{issue.number}" in str(extra.get("pr_number", ""))
    fields = {f.get("field") for f in (invite.get("filters") or []) if isinstance(f, dict)}
    assert "issue.pull_request" in fields  # never fire on a plain issue comment
    # the invoked reviewer must be able to shell the read-only `gh pr diff`; the webhook
    # default toolset `hermes-webhook` has no `terminal`, so the route grants it explicitly.
    assert set(invite.get("toolsets") or []) == {"terminal", "hermes-webhook"}

    secrets = [r["secret"] for r in routes.values()]
    assert all(secrets) and len(set(secrets)) == len(secrets)

    assert (default_dir / "scripts" / "pr_ci_gate.py").is_file()
    assert (default_dir / "scripts" / "check_gate.py").is_file()
    import yaml

    dist = yaml.safe_load((default_dir / "distribution.yaml").read_text(encoding="utf-8"))
    owned = dist.get("distribution_owned") or []
    assert "scripts" in owned

    for name, r in routes.items():
        if r.get("profile") == REVIEWER and "pull_request" in (r.get("events") or []):
            assert r.get("script") == "pr_ci_gate.py", f"ungated reviewer PR route {name}"

    for pname, pdir in rendered.items():
        if pname == "default":
            continue
        platforms = _cfg(pdir).get("platforms") or {}
        for plat_name, block in platforms.items():
            if block.get("enabled"):
                assert not platform_binds_port(plat_name, block.get("extra"))


def _latest(gate_db_path, pr=PR):
    con = sqlite3.connect(gate_db_path)
    try:
        return con.execute(
            "SELECT latest_head_sha, current_task_id FROM pr_gate WHERE repo = ? AND pr = ?",
            (REPO, pr),
        ).fetchone()
    finally:
        con.close()


def _card_id(kanban_db_path, key):
    con = sqlite3.connect(kanban_db_path)
    try:
        row = con.execute("SELECT id FROM tasks WHERE idempotency_key = ?", (key,)).fetchone()
    finally:
        con.close()
    return row[0] if row else None


def _card_count(kanban_db_path, key):
    con = sqlite3.connect(kanban_db_path)
    try:
        return con.execute(
            "SELECT COUNT(*) FROM tasks WHERE idempotency_key = ?", (key,)
        ).fetchone()[0]
    finally:
        con.close()


def _card_skills_raw(kanban_db_path, key):
    con = sqlite3.connect(kanban_db_path)
    try:
        row = con.execute(
            "SELECT skills FROM tasks WHERE idempotency_key = ?", (key,)
        ).fetchone()
    finally:
        con.close()
    return row[0] if row else None


def test_ac_loop_f40_2(tmp_path, monkeypatch):
    """pr_ci_gate on a pull_request `opened` records pr_gate.latest_head_sha and the created
    card's real task_id (pr_gate.current_task_id == tasks.id), upserts a per-head blocked
    review card (assignee == reviewer) carrying sdlc-review in its skills (so the ordinary
    lane spawn loads the review skill), and returns [SILENT]; two workers released
    simultaneously against the same head both terminate and leave exactly one card."""
    import threading

    rendered, default_dir = _render(tmp_path, monkeypatch)
    gate_db = str(tmp_path / "gate.db")
    kanban_db = str(tmp_path / "kanban.db")
    gate = _load_script(default_dir, "pr_ci_gate.py")

    keep, _ = gate.evaluate(_pr_event("opened", SHA_A), gate_db_path=gate_db, kanban_db_path=kanban_db)
    assert keep is False
    assert _card_by_key(kanban_db, _key(SHA_A)) == ("blocked", REVIEWER)
    # the card must carry sdlc-review so the ordinary-lane spawn emits --skills for the reviewer.
    assert "sdlc-review" in (_card_skills_raw(kanban_db, _key(SHA_A)) or "")

    latest = _latest(gate_db)
    assert latest is not None and latest[0] == SHA_A
    assert latest[1] == _card_id(kanban_db, _key(SHA_A))

    # The parked card must survive the dispatcher's promotion sweep: it was created
    # sticky-blocked atomically with its `blocked` event, so recompute_ready leaves
    # it blocked (a card blocked WITHOUT a sticky event would be auto-promoted to
    # ready, defeating the CI gate).
    from hermes_cli import kanban_db as _kdb

    with _kdb.connect_closing(db_path=Path(kanban_db)) as _kconn:
        _kdb.recompute_ready(_kconn)
    assert _card_by_key(kanban_db, _key(SHA_A)) == ("blocked", REVIEWER)

    barrier = threading.Barrier(2)
    results = []

    def _worker():
        barrier.wait()
        results.append(
            gate.evaluate(_pr_event("opened", SHA_B), gate_db_path=gate_db, kanban_db_path=kanban_db)
        )

    threads = [threading.Thread(target=_worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert all(not t.is_alive() for t in threads)
    assert len(results) == 2 and all(keep is False for keep, _ in results)
    assert _card_count(kanban_db, _key(SHA_B)) == 1


def test_ac_loop_f40_3(tmp_path, monkeypatch):
    """check_gate promotes (via kanban_db.unblock_task on the stored task_id) only on a
    success whose head_sha == latest; a stale-head success does not promote; a non-success
    leaves the card blocked and RETURNS the payload (fixer wake); on re-sync to a new head
    the still-ready prior card is archived while a running/done one is left alone; a success
    arriving before its pull_request event still ends with the head's card ready; a green
    invalidated by a later authoritative failure (success->failure->PR) leaves the card
    blocked; and independent PRs keep independent pr_gate rows."""
    rendered, default_dir = _render(tmp_path, monkeypatch)
    gate_db = str(tmp_path / "gate.db")
    kanban_db = str(tmp_path / "kanban.db")
    gate = _load_script(default_dir, "pr_ci_gate.py")
    check = _load_script(default_dir, "check_gate.py")

    gate.evaluate(_pr_event("opened", SHA_A), gate_db_path=gate_db, kanban_db_path=kanban_db)

    keep, payload = check.evaluate(_check_suite("failure", SHA_A), gate_db_path=gate_db, kanban_db_path=kanban_db)
    assert keep is True and payload is not None
    assert _card_by_key(kanban_db, _key(SHA_A))[0] == "blocked"

    keep, _ = check.evaluate(_check_suite("success", SHA_A), gate_db_path=gate_db, kanban_db_path=kanban_db)
    assert keep is False
    assert _card_by_key(kanban_db, _key(SHA_A)) == ("ready", REVIEWER)

    # re-sync to a new head while card(A) is still ready: card(A) must be archived (the
    # ordinary dispatcher spawns every ready card regardless of head), card(B) parked blocked.
    gate.evaluate(_pr_event("synchronize", SHA_B), gate_db_path=gate_db, kanban_db_path=kanban_db)
    assert _card_by_key(kanban_db, _key(SHA_A))[0] == "archived"
    assert _card_by_key(kanban_db, _key(SHA_B)) == ("blocked", REVIEWER)

    # a stale-head success (A no longer latest) neither promotes nor revives the archived card.
    check.evaluate(_check_suite("success", SHA_A), gate_db_path=gate_db, kanban_db_path=kanban_db)
    assert _card_by_key(kanban_db, _key(SHA_A))[0] == "archived"
    assert _card_by_key(kanban_db, _key(SHA_B))[0] == "blocked"

    check.evaluate(_check_suite("success", SHA_B), gate_db_path=gate_db, kanban_db_path=kanban_db)
    assert _card_by_key(kanban_db, _key(SHA_B))[0] == "ready"

    # a prior-head card that is still BLOCKED (re-synced before CI turns green) is retired too.
    sha_i, sha_j = "3" * 40, "4" * 40
    gate.evaluate(_pr_event("opened", sha_i, pr=55), gate_db_path=gate_db, kanban_db_path=kanban_db)
    assert _card_by_key(kanban_db, f"loop-f40-review:{REPO}#55:{sha_i}")[0] == "blocked"
    gate.evaluate(_pr_event("synchronize", sha_j, pr=55), gate_db_path=gate_db, kanban_db_path=kanban_db)
    assert _card_by_key(kanban_db, f"loop-f40-review:{REPO}#55:{sha_i}")[0] == "archived"
    assert _card_by_key(kanban_db, f"loop-f40-review:{REPO}#55:{sha_j}")[0] == "blocked"

    # a check_suite success delivered before its pull_request event (out-of-order webhooks):
    # the green head is recorded, and the later pull_request event promotes on card creation.
    sha_c = "c" * 40
    check.evaluate(_check_suite("success", sha_c, pr=77), gate_db_path=gate_db, kanban_db_path=kanban_db)
    gate.evaluate(_pr_event("opened", sha_c, pr=77), gate_db_path=gate_db, kanban_db_path=kanban_db)
    con = sqlite3.connect(kanban_db)
    try:
        st = con.execute(
            "SELECT status FROM tasks WHERE idempotency_key = ?",
            (f"loop-f40-review:{REPO}#77:{sha_c}",),
        ).fetchone()
    finally:
        con.close()
    assert st and st[0] == "ready"

    # a later authoritative failure invalidates the earlier green, so a PR event arriving
    # after success-then-failure for the same head leaves the card blocked, not promoted.
    sha_d = "d" * 40
    check.evaluate(_check_suite("success", sha_d, pr=88), gate_db_path=gate_db, kanban_db_path=kanban_db)
    check.evaluate(_check_suite("failure", sha_d, pr=88), gate_db_path=gate_db, kanban_db_path=kanban_db)
    gate.evaluate(_pr_event("opened", sha_d, pr=88), gate_db_path=gate_db, kanban_db_path=kanban_db)
    assert _card_by_key(kanban_db, f"loop-f40-review:{REPO}#88:{sha_d}")[0] == "blocked"

    # running/done cards are NOT archived on re-sync (only blocked/ready are retired).
    sha_e, sha_f = "e" * 40, "f" * 40
    gate.evaluate(_pr_event("opened", sha_e, pr=99), gate_db_path=gate_db, kanban_db_path=kanban_db)
    _set_status(kanban_db, f"loop-f40-review:{REPO}#99:{sha_e}", "running")
    gate.evaluate(_pr_event("synchronize", sha_f, pr=99), gate_db_path=gate_db, kanban_db_path=kanban_db)
    assert _card_by_key(kanban_db, f"loop-f40-review:{REPO}#99:{sha_e}")[0] == "running"
    assert _card_by_key(kanban_db, f"loop-f40-review:{REPO}#99:{sha_f}")[0] == "blocked"

    # a done card is likewise preserved on re-sync (the review for the old head completed).
    sha_g, sha_h = "1" * 40, "2" * 40
    gate.evaluate(_pr_event("opened", sha_g, pr=111), gate_db_path=gate_db, kanban_db_path=kanban_db)
    _set_status(kanban_db, f"loop-f40-review:{REPO}#111:{sha_g}", "done")
    gate.evaluate(_pr_event("synchronize", sha_h, pr=111), gate_db_path=gate_db, kanban_db_path=kanban_db)
    assert _card_by_key(kanban_db, f"loop-f40-review:{REPO}#111:{sha_g}")[0] == "done"
    assert _card_by_key(kanban_db, f"loop-f40-review:{REPO}#111:{sha_h}")[0] == "blocked"

    # a non-success for the CURRENT head demotes an already-ready, unclaimed card back to
    # blocked (upholds "review only on green for the current head").
    sha_k = "5" * 40
    gate.evaluate(_pr_event("opened", sha_k, pr=120), gate_db_path=gate_db, kanban_db_path=kanban_db)
    check.evaluate(_check_suite("success", sha_k, pr=120), gate_db_path=gate_db, kanban_db_path=kanban_db)
    assert _card_by_key(kanban_db, f"loop-f40-review:{REPO}#120:{sha_k}") == ("ready", REVIEWER)
    check.evaluate(_check_suite("failure", sha_k, pr=120), gate_db_path=gate_db, kanban_db_path=kanban_db)
    assert _card_by_key(kanban_db, f"loop-f40-review:{REPO}#120:{sha_k}")[0] == "blocked"
    # The demoted card must ALSO survive recompute_ready: the demote writes a sticky-block
    # event, so a dispatcher promotion sweep leaves it blocked (without it the card would be
    # auto-re-promoted, silently re-queuing the reviewer on the red head).
    from hermes_cli import kanban_db as _kdb

    with _kdb.connect_closing(db_path=Path(kanban_db)) as _kconn:
        _kdb.recompute_ready(_kconn)
    assert _card_by_key(kanban_db, f"loop-f40-review:{REPO}#120:{sha_k}") == ("blocked", REVIEWER)

    # a card already CLAIMED (running) for the current head is left alone on a red (guard no-ops).
    sha_m = "6" * 40
    gate.evaluate(_pr_event("opened", sha_m, pr=121), gate_db_path=gate_db, kanban_db_path=kanban_db)
    check.evaluate(_check_suite("success", sha_m, pr=121), gate_db_path=gate_db, kanban_db_path=kanban_db)
    _set_status(kanban_db, f"loop-f40-review:{REPO}#121:{sha_m}", "running")
    check.evaluate(_check_suite("failure", sha_m, pr=121), gate_db_path=gate_db, kanban_db_path=kanban_db)
    assert _card_by_key(kanban_db, f"loop-f40-review:{REPO}#121:{sha_m}")[0] == "running"

    # out-of-order guard: a redelivered older pull_request (strictly-older updated_at) for a
    # superseded head is ignored — the current head's card is neither archived nor superseded.
    sha_bb, sha_aa = "7" * 40, "8" * 40
    gate.evaluate(_pr_event("synchronize", sha_bb, pr=130, updated_at="2026-02-02T00:00:00Z"), gate_db_path=gate_db, kanban_db_path=kanban_db)
    gate.evaluate(_pr_event("opened", sha_aa, pr=130, updated_at="2026-01-01T00:00:00Z"), gate_db_path=gate_db, kanban_db_path=kanban_db)
    assert _card_by_key(kanban_db, f"loop-f40-review:{REPO}#130:{sha_bb}") == ("blocked", REVIEWER)
    assert _card_by_key(kanban_db, f"loop-f40-review:{REPO}#130:{sha_aa}") is None

    con = sqlite3.connect(gate_db)
    try:
        prs = [r[0] for r in con.execute(
            "SELECT pr FROM pr_gate WHERE repo = ? ORDER BY pr", (REPO,)
        ).fetchall()]
        latest_130 = con.execute(
            "SELECT latest_head_sha FROM pr_gate WHERE repo = ? AND pr = ?", (REPO, 130)
        ).fetchone()
    finally:
        con.close()
    assert prs == [PR, 55, 77, 88, 99, 111, 120, 121, 130]
    assert latest_130 is not None and latest_130[0] == sha_bb


def _set_status(kanban_db_path, key, status):
    con = sqlite3.connect(kanban_db_path)
    try:
        con.execute("UPDATE tasks SET status = ? WHERE idempotency_key = ?", (status, key))
        con.commit()
    finally:
        con.close()


def test_ac_loop_f40_4(tmp_path, monkeypatch):
    """The gh-pr-review-invite route matches an issue_comment.created on a PR with a
    boundary mention + comment.user.type == User + non-self login, and does NOT match a
    no-mention comment, a bot-authored comment, a self-authored comment, a comment on a
    plain issue (no issue.pull_request), or a boundary near-miss handle."""
    from gateway.platforms.webhook_filters import WebhookRouteProcessor

    rendered, default_dir = _render(tmp_path, monkeypatch)
    invite = _routes(default_dir)["gh-pr-review-invite"]
    processor = WebhookRouteProcessor()

    def matches(payload):
        return processor.route_filters_match(invite, payload, "issue_comment", {})

    assert matches(_comment(f"hey {REVIEWER_HANDLE} please review")) is True
    assert matches(_comment("please review this PR")) is False
    assert matches(_comment(f"{REVIEWER_HANDLE} auto", user_type="Bot")) is False
    assert matches(_comment(f"{REVIEWER_HANDLE} self", login=REVIEWER_LOGIN)) is False
    assert matches(_comment(f"{REVIEWER_HANDLE} on an issue", is_pr=False)) is False
    assert matches(_comment("cc @slang-reviewer-evil not you")) is False


def test_pr_ci_gate_subprocess_contract(tmp_path, monkeypatch):
    """Production path (not just the imported evaluate): the rendered pr_ci_gate.py, run as a
    subprocess the way the webhook adapter runs it (stdin JSON, HERMES_HOME/HERMES_KANBAN_DB
    in env), resolves its own stores, prints [SILENT] on stdout, and parks the blocked card —
    proving the __main__ shim + path resolution, so a broken shim cannot pass on evaluate()
    coverage alone."""
    import json as _json
    import os
    import subprocess
    import sys

    rendered, default_dir = _render(tmp_path, monkeypatch)
    script = default_dir / "scripts" / "pr_ci_gate.py"
    assert script.is_file()

    kanban_db = tmp_path / "kanban.db"
    env = dict(os.environ)
    env["HERMES_HOME"] = str(tmp_path / "home")  # gate DB resolves under plugin-data here
    env["HERMES_KANBAN_DB"] = str(kanban_db)     # kanban resolver honours this pin first
    proc = subprocess.run(
        [sys.executable, str(script)],
        input=_json.dumps(_pr_event("opened", SHA_A)),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[SILENT]"
    assert _card_by_key(str(kanban_db), _key(SHA_A)) == ("blocked", REVIEWER)

    # The gate store must resolve through the sanctioned plugin_db API under HERMES_HOME
    # (<home>/plugin-data/<plugin>/<file>), not an ad-hoc path — assert the DB landed there
    # and carries the pr_gate row the run wrote.
    gate_file = Path(env["HERMES_HOME"]) / "plugin-data" / PLUGIN_KEY / "loop-f40-ci-gate.db"
    assert gate_file.is_file(), f"gate DB not resolved under plugin-data: {gate_file}"
    con = sqlite3.connect(str(gate_file))
    try:
        row = con.execute(
            "SELECT latest_head_sha FROM pr_gate WHERE repo = ? AND pr = ?", (REPO, PR)
        ).fetchone()
    finally:
        con.close()
    assert row is not None and row[0] == SHA_A
