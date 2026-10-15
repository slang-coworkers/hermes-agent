"""Acceptance tests for the UPG-F69.c ingress, inference, and fleet-gates port.

Fakes, and nothing else:
  -1..-6, -11: the v0.22.5 door's RPC (``tools.bot_live_delivery.authority_delivery``) and endpoint discovery;
               -6 also ``subprocess.run`` (the owner CLI turn). nv-ingress's bridge, ledger and the door client are real.
  -14, -15: none beyond the carried FLEET-F62 harness (a ready task env), whose helpers they import.
  -10, -12, -13: ``hermes`` / ``openshell`` executables on PATH (the fake_openshell.py pattern) and the detached
               gateway restart (recorded: the live-system guard forbids it); the rest of the installer's ``apply()``
               and its subprocess calls are real, and ``coworker compose`` is forwarded to the real CLI.
"""
from __future__ import annotations

import argparse
import copy
import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_yaml as yaml
from gateway.config import Platform
from hermes_cli.plugins import PluginManager
from tools.registry import invalidate_check_fn_cache

REPO_ROOT = Path(__file__).resolve().parents[2]
INGRESS_KEY = "nv-ingress"
COMPOSE_KEY = "nv-coworker-compose"
INGRESS_SRC = REPO_ROOT / "plugins" / INGRESS_KEY
COMPOSE_SRC = REPO_ROOT / "plugins" / COMPOSE_KEY
ING_SPEC = REPO_ROOT / "tests" / "e2e-scenarios" / "ING-F66" / "spec" / "openshell" / "coworker-types.yaml"
DIRECT_SPEC = REPO_ROOT / "tests" / "e2e-scenarios" / "OSH-F64.e" / "spec" / "coworker-types.yaml"
MANAGED_SPEC = REPO_ROOT / "tests" / "e2e-scenarios" / "OSH-F64.b" / "spec" / "coworker-types.yaml"
F69 = REPO_ROOT / "tests" / "e2e-scenarios" / "UPG-F69"
PORT_TREES = ("plugins/nv-ingress", "tests/e2e-scenarios/ING-F66", "tests/e2e-scenarios/OSH-F64.e",
              "tests/e2e-scenarios/FLEET-F62.f")
ROUTE = "ingress"
ORCH = "orchestrator"
OWNER = "fixer"
REPO = "o/upg-f69c"
PR = 7
ORCH_BOT_CHAT = "sess-orch-botchat"
OWNER_SESSION = "sess-fixer-pr"
_PR_CREATE = " ".join(("gh", "pr", "create"))


def _seed_session(home: Path, session_id: str, *, title=None, messages=()) -> None:
    from hermes_state import SessionDB

    home.mkdir(parents=True, exist_ok=True)
    db = SessionDB(db_path=home / "state.db")
    try:
        db.create_session(session_id, "cli")
        if title:
            db.set_session_title(session_id, title)
        for text in messages:
            db.append_message(session_id, "user", text)
    finally:
        db.close()


def _check_run(conclusion: str = "failure") -> dict:
    return {"action": "completed", "repository": {"full_name": REPO},
            "sender": {"login": "octo", "type": "User"},
            "check_run": {"head_sha": "a" * 40, "status": "completed", "conclusion": conclusion,
                          "name": "check", "pull_requests": [{"number": PR}],
                          "check_suite": {"head_branch": "feat"}}}


def _marker(delivery: str) -> str:
    return f"ingress-delivery: github/{delivery}/{PR}"


class _Door:
    """The authority's ``bot_relay.deliver`` RPC: records each request, answers from a script.

    ``script`` items: a status string (``queued``/``settled``/``failed``) or an exception instance to raise."""

    def __init__(self):
        self.requests: list[dict] = []
        self.script: list = []

    def __call__(self, home, params):
        self.requests.append({"home": str(home), **params})
        step = self.script.pop(0) if self.script else "queued"
        if isinstance(step, BaseException):
            raise step
        return {"status": step, "delivery_id": params["id"], "profile_home": str(home),
                "session_id": params.get("session_id", ""), "admission_id": f"adm-{len(self.requests)}",
                "message": params["message"]}


def _load(keys) -> PluginManager:
    manager = PluginManager()
    manager.discover_and_load()
    for key in keys:
        loaded = manager._plugins[key]
        assert loaded.enabled is True and loaded.error is None and loaded.module is not None, loaded.error
    return manager


@pytest.fixture
def ing(tmp_path, monkeypatch):
    """nv-ingress from an isolated home; the door is real down to its RPC, which ``door`` answers."""
    root = tmp_path / "root"
    (root / "plugins").mkdir(parents=True)
    shutil.copytree(INGRESS_SRC, root / "plugins" / INGRESS_KEY, ignore=shutil.ignore_patterns("__pycache__"))
    settings = {"route": ROUTE, "orchestrator_profile": ORCH, "issue_labels": ["fleet"],
                "per_pr_hourly_budget": 10, "self_logins": ["fleet-bot"], "drain_interval_seconds": 3600}
    (root / "config.yaml").write_text(yaml.safe_dump({
        "plugins": {"enabled": [INGRESS_KEY], "entries": {INGRESS_KEY: {"settings": settings}}},
    }), encoding="utf-8")
    for prof in (ORCH, OWNER):
        (root / "profiles" / prof).mkdir(parents=True)
    (root / "scripts").mkdir()
    shutil.copy(INGRESS_SRC / "route_scripts" / "ingress_stage.py", root / "scripts" / "ingress_stage.py")
    _seed_session(root / "profiles" / ORCH, ORCH_BOT_CHAT, title="Bot Chat")
    _seed_session(root / "profiles" / OWNER, OWNER_SESSION, messages=("opening the PR",))
    bundled = tmp_path / "empty-bundled"
    bundled.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")

    import hermes_cli.gateway_runtime as runtime
    import tools.bot_live_delivery as live

    ready = SimpleNamespace(state="ready", endpoint=SimpleNamespace(instance_id="lease-1"))
    monkeypatch.setattr(runtime, "discover_gateway_endpoint", lambda home, timeout=5, **_kw: ready)
    door = _Door()
    monkeypatch.setattr(live, "authority_delivery", door)

    manager = _load((INGRESS_KEY,))
    loaded = manager._plugins[INGRESS_KEY]
    pkg = loaded.module.__name__
    yield SimpleNamespace(manager=manager, module=loaded.module, root=root, door=door, live=live,
                          ledger=sys.modules[pkg + ".ledger"], envelope=sys.modules[pkg + ".envelope"])
    manager.unload()


def _stage_and_dispatch(ing, event: str, delivery: str, payload: dict) -> None:
    """The real route script, then pre_gateway_dispatch, as the webhook adapter runs them."""
    from gateway.platforms.webhook_filters import WebhookRouteProcessor

    headers = {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery}
    env_payload = ing.envelope.normalize("github", event, headers, payload)
    keep, out = WebhookRouteProcessor(script_timeout_seconds=30).run_route_script("ingress_stage.py", env_payload)
    assert keep is True and isinstance(out, dict)
    hooked = SimpleNamespace(text="", message_id=delivery, raw_message=out,
                             source=SimpleNamespace(platform=Platform.WEBHOOK, chat_type="webhook",
                                                    chat_id=f"webhook:{ROUTE}:{delivery}"))
    ing.manager.invoke_hook("pre_gateway_dispatch", event=hooked, gateway=None, session_store=None,
                            future_additive_field=True)


def _rows(ing, sql: str, args=()) -> list:
    conn = ing.ledger.connect()
    try:
        return list(conn.execute(sql, args))
    finally:
        conn.close()


def _force_due(ing) -> None:
    conn = ing.ledger.connect()
    try:
        conn.execute("UPDATE deliveries SET next_attempt_at = 0 WHERE state = 'pending'")
        conn.commit()
    finally:
        conn.close()


def _only_row(ing) -> tuple:
    rows = _rows(ing, "SELECT state, next_attempt_at FROM deliveries")
    assert len(rows) == 1, rows
    return rows[0]


def _door_to(ing, profile: str) -> list:
    return [r for r in ing.door.requests if r["profile"] == profile]


@pytest.fixture
def spawned(monkeypatch):
    """Every subprocess.run during a drain, answered rc 0 (installed after staging, which runs a real script)."""
    calls: list[list] = []

    def _run(argv, *args, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    return SimpleNamespace(calls=calls, install=lambda: monkeypatch.setattr(subprocess, "run", _run))


def _unowned_ci_failure(ing, spawned, delivery: str = "d-ci-1") -> None:
    _stage_and_dispatch(ing, "check_run", delivery, _check_run())
    spawned.install()
    ing.module.drain_once()


def test_ac_upg_f69_c_1(ing, spawned):
    """An unowned PR event staged through the real route script is delivered to the orchestrator's Bot Chat as exactly one `bot_relay.deliver` request made by the real `tools.bot_live_delivery` client, its message carrying the event's `ingress-delivery:` marker, with no CLI subprocess spawned for it."""
    ing.door.script = ["settled"]
    _unowned_ci_failure(ing, spawned)
    to_orch = _door_to(ing, ORCH)
    assert len(to_orch) == 1 and len(ing.door.requests) == 1
    assert _marker("d-ci-1") in to_orch[0]["message"]
    assert len(to_orch[0]["id"]) in range(32, 65) and set(to_orch[0]["id"]) <= set("0123456789abcdef")
    assert spawned.calls == []
    assert _only_row(ing)[0] == "pending", "a receipt is not completion until the marker is in the Bot Chat"


def test_ac_upg_f69_c_2(ing, spawned):
    """A `queued` door admission does not complete the delivery: the ledger row stays `pending` after the drain that sent it, and turns `done` on the first drain after its marker is present in the orchestrator's Bot Chat."""
    ing.door.script = ["queued", "queued"]
    _unowned_ci_failure(ing, spawned)
    assert len(ing.door.requests) == 1
    assert _only_row(ing)[0] == "pending"
    sent = ing.door.requests[0]["message"]
    _seed_session_message(ing.root / "profiles" / ORCH, ORCH_BOT_CHAT, sent)
    _force_due(ing)
    ing.module.drain_once()
    assert _only_row(ing)[0] == "done"


def _seed_session_message(home: Path, session_id: str, text: str) -> None:
    from hermes_state import SessionDB

    db = SessionDB(db_path=home / "state.db")
    try:
        db.append_message(session_id, "user", text)
    finally:
        db.close()


def test_ac_upg_f69_c_3(ing, spawned):
    """An outcome the sender cannot classify (the RPC raised after it was sent) is retried with the same door delivery id, so the authority never sees two ids for one event."""
    ing.door.script = [ConnectionResetError("reset after the request went out"), "queued"]
    _unowned_ci_failure(ing, spawned)
    assert _only_row(ing)[0] == "pending"
    _force_due(ing)
    ing.module.drain_once()
    ids = [r["id"] for r in ing.door.requests]
    assert len(ids) == 2 and ids[0] == ids[1]


def test_ac_upg_f69_c_4(ing, spawned):
    """A definite no-send (`PreDialError`, or "profile authority is not ready") leaves the row `pending` with a later next attempt, and the next due drain sends it."""
    import time

    ing.door.script = [ing.live.PreDialError("profile authority is not ready"), "queued"]
    before = time.time()
    _unowned_ci_failure(ing, spawned)
    state, next_at = _only_row(ing)
    assert state == "pending" and next_at > before
    _force_due(ing)
    ing.module.drain_once()
    assert len(ing.door.requests) == 2


def test_ac_upg_f69_c_5(ing, spawned):
    """A terminal `failed` receipt whose marker never reached the orchestrator's Bot Chat is retried under a new door delivery id after backoff; the row is never marked done on that receipt."""
    import time

    ing.door.script = ["failed", "queued"]
    before = time.time()
    _unowned_ci_failure(ing, spawned)
    state, next_at = _only_row(ing)
    assert state == "pending" and next_at > before
    _force_due(ing)
    ing.module.drain_once()
    ids = [r["id"] for r in ing.door.requests]
    assert len(ids) == 2 and ids[0] != ids[1]
    assert _only_row(ing)[0] == "pending"


def test_ac_upg_f69_c_6(ing, spawned, monkeypatch):
    """(DN-1 A, ruled) An owned CI failure is delivered into the owner's existing session by exactly one `hermes -p <owner> chat -Q --resume <owner-session-id> --query-file <file>` turn, and no door request is made for it."""
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: OWNER)
    invalidate_check_fn_cache()
    ing.manager.invoke_hook(
        "post_tool_call", tool_name="terminal", args={"command": f"{_PR_CREATE} --draft --fill"},
        result=f"https://github.com/{REPO}/pull/{PR}\n", status="ok", task_id=OWNER_SESSION,
        session_id=OWNER_SESSION, tool_call_id="tc", turn_id="t", duration_ms=1)
    owner = _rows(ing, "SELECT owner_profile, session_id FROM pr_owner WHERE repo = ? AND pr = ?", (REPO, PR))
    assert owner == [(OWNER, OWNER_SESSION)], "precondition: the PR is claimed by the fixer's session"
    _stage_and_dispatch(ing, "check_run", "d-ci-owned", _check_run())
    spawned.install()
    ing.module.drain_once()
    turns = [a for a in spawned.calls if "chat" in a]
    assert len(turns) == 1, spawned.calls
    argv = turns[0]
    assert argv[argv.index("-p") + 1] == OWNER and "-Q" in argv and "--query-file" in argv
    assert argv[argv.index("--resume") + 1] == OWNER_SESSION
    assert ing.door.requests == []


def test_ac_upg_f69_c_7(tmp_path, monkeypatch):
    """(derived, D2) With `tools.bot_live_delivery.deliver_to_live_owner` absent, `PluginManager().discover_and_load()` records nv-ingress with a load error naming `deliver_to_live_owner` and leaves it disabled."""
    root = tmp_path / "root"
    (root / "plugins").mkdir(parents=True)
    shutil.copytree(INGRESS_SRC, root / "plugins" / INGRESS_KEY, ignore=shutil.ignore_patterns("__pycache__"))
    (root / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": [INGRESS_KEY]}}), encoding="utf-8")
    bundled = tmp_path / "empty-bundled"
    bundled.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    _load((INGRESS_KEY,)).unload()  # precondition: the unmodified base loads it

    import tools.bot_live_delivery as live

    monkeypatch.delattr(live, "deliver_to_live_owner")
    manager = PluginManager()
    manager.discover_and_load()
    try:
        loaded = manager._plugins[INGRESS_KEY]
        assert loaded.enabled is False
        assert "deliver_to_live_owner" in (loaded.error or "")
    finally:
        manager.unload()


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True,
                          encoding="utf-8", check=True).stdout


def _inventory() -> list[dict]:
    lines = [ln for ln in (F69 / "path_inventory.tsv").read_text(encoding="utf-8-sig").splitlines()
             if ln and not ln.startswith("#")]
    header = lines[0].split("\t")
    return [dict(zip(header, ln.split("\t"))) for ln in lines[1:]]


def test_ac_upg_f69_c_8():
    """(derived, D4) The UPG-F69 replay checker, run with the bump of record (`tests/e2e-scenarios/UPG-F69/bump.json`) against the port head, exits 0 with no `FAIL` line."""
    bump = json.loads((F69 / "bump.json").read_text(encoding="utf-8-sig"))
    result = subprocess.run(
        [sys.executable, str(F69 / "replay_check.py"), "--repo", str(REPO_ROOT), "--from", bump["from"],
         "--onto", bump["onto"], "--head", bump["head"], "--candidate", "HEAD", "--data-dir", str(F69)],
        capture_output=True, text=True, encoding="utf-8", timeout=900, check=False)
    findings = [ln for ln in result.stdout.splitlines() if ln.startswith("FAIL ")]
    assert result.returncode == 0 and not findings, result.stdout[-4000:] + result.stderr[-2000:]


def test_ac_upg_f69_c_9():
    """(derived, D4) Every file the port adds under `plugins/nv-ingress/`, `tests/e2e-scenarios/ING-F66/`, `tests/e2e-scenarios/OSH-F64.e/` and `tests/e2e-scenarios/FLEET-F62.f/` has exactly one `path_inventory.tsv` row whose `origin` names `UPG-F69.c`."""
    rows = _inventory()
    for tree in PORT_TREES:
        files = [p for p in _git("ls-files", "--", tree).splitlines() if p]
        assert files, f"{tree} is not carried at HEAD"
        for path in files:
            mine = [r for r in rows if r["path"] == path]
            assert len(mine) == 1, f"{path}: {len(mine)} inventory rows"
            assert "UPG-F69.c" in mine[0]["origin"], f"{path}: origin {mine[0]['origin']!r}"


def _route_auth(cfg: dict) -> dict:
    """The route/auth projection the installed default must share with the render."""
    keys = ("provider", "model", "default", "base_url", "api_mode", "key_env", "api_key", "request_overrides")

    def pick(block):
        return {k: (block or {}).get(k) for k in keys if isinstance(block, dict) and k in block}

    aux = cfg.get("auxiliary") or {}
    return {"model": pick(cfg.get("model")), "delegation": pick(cfg.get("delegation")),
            "auxiliary": {task: pick(block) for task, block in aux.items() if isinstance(block, dict)},
            "providers": {name: pick(block) for name, block in (cfg.get("providers") or {}).items()}}


_STALE_DEFAULT = {"model": {"provider": "stale-provider", "default": "stale-model"},
                  "providers": {"stale-provider": {"base_url": "https://stale.invalid/v1", "api_key": "sk-stale"}},
                  "delegation": {"request_overrides": {"api_key": "sk-stale-override"}},
                  "display": {"skin": "operator-kept"}}

# A stand-in for the two CLIs the installer shells out to, installed on PATH (the
# tests/plugins/fake_openshell.py pattern: the installer's own subprocess calls stay real).
# `hermes coworker compose` is forwarded to the REAL CLI so the plan's policies exist;
# `plugins install --enable` edits the target home's config.yaml as the real verb does.
_FAKE_CLI = """#!{python}
import json, os, subprocess, sys
from pathlib import Path
name, args = Path(sys.argv[0]).name, sys.argv[1:]
with open(os.environ["UPG_F69C_FAKE_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps([name, *args]) + "\\n")
if name == "hermes":
    rest = args[2:] if args[:1] == ["-p"] else args
    home = Path(os.environ["HERMES_HOME"]) / ("profiles/" + args[1] if args[:1] == ["-p"] else "")
    if rest[:2] == ["coworker", "compose"]:
        sys.exit(subprocess.run([{python!r}, "-c", "import sys; from hermes_cli.main import main; sys.argv[0] = 'hermes'; main()", *args]).returncode)
    if rest[:2] == ["plugins", "install"] and "--enable" in rest:
        import hermes_yaml
        cfg_path = home / "config.yaml"
        cfg = (hermes_yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else None) or {{}}
        enabled = cfg.setdefault("plugins", {{}}).setdefault("enabled", [])
        plugin = Path(rest[2].rstrip("/")).name
        if plugin not in enabled:
            enabled.append(plugin)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(hermes_yaml.safe_dump(cfg), encoding="utf-8")
    if rest[:2] == ["gateway", "status"]:
        print("Gateway is running")
sys.exit(0)
"""


def _install_default(tmp_path, monkeypatch, spec_path: Path):
    """Run the installer's full ``apply()`` against a stale default home, with ``hermes`` and
    ``openshell`` replaced on PATH. Returns (module, seed, installed config, backup path, CLI log)."""
    home = tmp_path / "home"
    (home / "plugins").mkdir(parents=True)
    shutil.copytree(COMPOSE_SRC, home / "plugins" / COMPOSE_KEY, ignore=shutil.ignore_patterns("__pycache__"))
    seed = {**copy.deepcopy(_STALE_DEFAULT), "plugins": {"enabled": [COMPOSE_KEY]}}
    (home / "config.yaml").write_text(yaml.safe_dump(seed), encoding="utf-8")
    managed = tmp_path / "managed"
    managed.mkdir()
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for cli in ("hermes", "openshell"):
        exe = bin_dir / cli
        exe.write_text(_FAKE_CLI.format(python=sys.executable), encoding="utf-8")
        exe.chmod(0o755)
    log = tmp_path / "cli.log"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("UPG_F69C_FAKE_LOG", str(log))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    manager = _load((COMPOSE_KEY,))
    module = manager._plugins[COMPOSE_KEY].module
    installer = importlib.import_module(module.__name__ + ".openshell.installer")
    monkeypatch.setattr(installer, "_STATUS_POLL_INTERVAL_S", 0.0)
    backup_path, _, _ = installer.default_config_diff(copy.deepcopy(seed), str(spec_path))
    installer.apply(str(spec_path), "a" * 40, room_exists=lambda _room: True)
    installed = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))
    calls = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    assert any(c[:1] == ["openshell"] for c in calls), "precondition: the full plan ran (sandbox steps included)"
    return module, seed, installed, Path(backup_path)


def _rendered_default(module, spec_path: Path, out: Path) -> dict:
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    module.compose(str(spec_path), str(out))
    default_name = spec.get("default_profile", "default")
    paths = [c for c in out.rglob("config.yaml") if c.parent.name == default_name]
    assert len(paths) == 1, paths
    return yaml.safe_load(paths[0].read_text(encoding="utf-8"))


def test_ac_upg_f69_c_10(tmp_path, monkeypatch):
    """Under `egress.inference_provider.mode: direct`, the installer's default-profile edit leaves the installed gateway default's route/auth projection equal to the rendered default's (selected provider, its base_url, api_mode and credential reference; no stale provider entry left behind)."""
    spec = yaml.safe_load(DIRECT_SPEC.read_text(encoding="utf-8"))
    assert spec["egress"]["inference_provider"].get("mode") == "direct", "precondition: the direct-mode spec"
    module, seed, installed, backup = _install_default(tmp_path, monkeypatch, DIRECT_SPEC)
    rendered = _rendered_default(module, DIRECT_SPEC, tmp_path / "out")
    assert _route_auth(installed) == _route_auth(rendered)
    assert "sk-stale" not in json.dumps(installed)
    assert installed["display"]["skin"] == "operator-kept"
    assert yaml.safe_load(backup.read_text(encoding="utf-8")) == seed


def test_ac_upg_f69_c_12(tmp_path, monkeypatch):
    """(derived) With no `egress.inference_provider` in the spec, the installer's default-profile edit leaves the existing default's `model` and `providers` exactly as they were."""
    spec_dir = tmp_path / "spec"
    shutil.copytree(DIRECT_SPEC.parent, spec_dir)
    spec_path = spec_dir / DIRECT_SPEC.name
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    spec["egress"].pop("inference_provider")
    spec_path.write_text(yaml.safe_dump(spec), encoding="utf-8")
    _, seed, installed, _ = _install_default(tmp_path, monkeypatch, spec_path)
    assert installed.get("model") == seed["model"]
    assert installed.get("providers") == seed["providers"]


def test_ac_upg_f69_c_13(tmp_path, monkeypatch):
    """(derived) Under the managed single-authority posture (`egress.inference_provider` present, `mode` absent), the installer's default-profile edit leaves the installed gateway default's route/auth projection equal to the rendered default's."""
    spec = yaml.safe_load(MANAGED_SPEC.read_text(encoding="utf-8"))
    assert "mode" not in spec["egress"]["inference_provider"], "precondition: the OSH-F64.b managed spec"
    module, _, installed, _ = _install_default(tmp_path, monkeypatch, MANAGED_SPEC)
    rendered = _rendered_default(module, MANAGED_SPEC, tmp_path / "out")
    assert _route_auth(installed) == _route_auth(rendered)
    assert "sk-stale" not in json.dumps(installed)


def test_ac_upg_f69_c_11(ing, tmp_path, monkeypatch):
    """(derived, D6) `hermes ingress render-host <spec> --out <dir>` renders the host artefacts while the PyYAML module is unimportable."""
    entry = ing.manager._cli_commands["ingress"]
    parser = argparse.ArgumentParser(prog="hermes ingress")
    entry["setup_fn"](parser)
    out = tmp_path / "render"
    args = parser.parse_args(["render-host", str(ING_SPEC), "--out", str(out), "--json"])
    monkeypatch.setitem(sys.modules, "yaml", None)
    assert entry["handler_fn"](args) == 0
    assert (out / "host").is_dir() and any((out / "host").iterdir())


def _served_turn(tmp_path, monkeypatch):
    """The carried FLEET-F62 render + per-profile load (AC-FLEET-F62-3's harness), the veto loaded under the
    LAUNCH (default) home, and a ready task env: a served worker's turn then has active != launch."""
    from tests.plugins import test_fleet_f62_acceptance as f62
    import tools.terminal_tool as tt

    substrate = f62.SUBSTRATES[0]
    rendered = f62._build_rendered(substrate, tmp_path, monkeypatch)
    home, bundled, managed = rendered["home"], rendered["bundled"], rendered["managed"]
    backend = f62._meta_for(substrate)["backend"]
    monkeypatch.setattr(tt, "ensure_task_env", lambda *a, **k: object())
    manager = f62._load_profile_plugins(home, monkeypatch, bundled, managed)
    role = next(r for r in f62.ROLES if r != "orchestrator")
    return f62, manager, home / "profiles" / role, backend


def _served_block(f62, manager, role_home: Path, scope):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools.terminal_scope import reset_terminal_scope, set_terminal_scope

    home_token = set_hermes_home_override(str(role_home))
    scope_token = set_terminal_scope(scope)
    try:
        # A neutral command: a PR-egress command would also meet nv-fleet-gates' critique gate.
        return f62._block_message(manager, "terminal", {"command": "echo scope probe"})
    finally:
        reset_terminal_scope(scope_token)
        reset_hermes_home_override(home_token)


def test_ac_upg_f69_c_14(tmp_path, monkeypatch):
    """(derived, D10) On a multiplexed gateway turn for a served (non-launch) profile, nv-fleet-gates allows a terminal call when v0.22.5's per-turn terminal scope is bound to that profile's expected backend, whatever the launch process's ambient `TERMINAL_ENV` says."""
    f62, manager, role_home, backend = _served_turn(tmp_path, monkeypatch)
    monkeypatch.setenv("TERMINAL_ENV", "local")  # the launch process's ambient value must not decide
    assert _served_block(f62, manager, role_home, {"TERMINAL_ENV": backend}) is None


def test_ac_upg_f69_c_15(tmp_path, monkeypatch):
    """(derived, D10) On the same served-profile turn, nv-fleet-gates refuses the terminal call when the bound scope names another backend or is a refusal scope; with no scope bound the carried cross-profile refusal still applies."""
    from tools.terminal_scope import TerminalPolicyRefusal

    f62, manager, role_home, backend = _served_turn(tmp_path, monkeypatch)
    monkeypatch.setenv("TERMINAL_ENV", backend)  # ambient matches: only the scope can make it refuse
    mismatched = _served_block(f62, manager, role_home, {"TERMINAL_ENV": "local"})
    assert mismatched and "resolved backend" in mismatched
    refused = _served_block(f62, manager, role_home, TerminalPolicyRefusal("unresolvable profile policy"))
    assert refused and "cross-profile refused" not in refused
    unscoped = _served_block(f62, manager, role_home, None)
    assert unscoped and "cross-profile refused" in unscoped
