"""UPG-F68 acceptance: the fork on Hermes v0.22.2 (tag commit d923a5eb) with UPG-F67's carried contracts.

Ships as tests/plugins/test_upg_f68_acceptance.py. One test per `pytest:` row of the ADR's `## Acceptance criteria`
(reports/upg-f68.md), in id order; docstring line 1 is the criterion. Runs on Python 3.14 in the dev-group venv with no
extras (ADR §Environment E2). Hermetic: an isolated HERMES_HOME per test, an EMPTY bundled-plugins dir, loopback sockets
only, nothing under the real home, no plugin source read. Certificates are throwaway CAs minted in memory per test.
"""
import asyncio
import contextlib
import datetime
import http.server
import ipaddress
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_yaml
from hermes_cli.plugins import PluginManager

REPO = Path(__file__).resolve().parents[2]
PLUGINS_SRC = REPO / "plugins"
FORK_PLUGINS = (
    "hello", "nv-approval-ledger", "nv-artifact", "nv-cost-cap",
    "nv-coworker-compose", "nv-fleet-gates", "nv-workflow-modes", "podman-onecli",
)
COMPOSE = "nv-coworker-compose"
F62C = REPO / "tests" / "e2e-scenarios" / "FLEET-F62.c"
F62C_SPEC = F62C / "spec" / "openshell" / "coworker-types.yaml"
F62C_GOLDEN = F62C / "fixtures" / "openshell"
OSH_F64_SPEC = REPO / "tests" / "e2e-scenarios" / "OSH-F64" / "spec" / "coworker-types.yaml"
HOME_TOKEN = "@HERMES_HOME@"
SHA40 = "a" * 40
# v0.22.2: an ancestor of every candidate head, so present in any full clone.
TAG_COMMIT = "d923a5eb487a17c0a68801bba9ef0f56c81040a8"
TRUST_SOURCE = "openshell_trust"
CA_ENV_NAMES = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "HERMES_CA_BUNDLE",
                "DENO_CERT")
LEGACY_MARKS = ("linux_only", "macos_only", "windows_only")

# A child interpreter that cannot import PyYAML: the import condition of a base install, whose lock closure has none.
_NO_YAML = '''
import sys
class _NoYaml:
    def find_spec(self, name, path=None, target=None):
        if name == "yaml" or name.startswith("yaml."):
            raise ModuleNotFoundError("No module named 'yaml' (blocked: base-install condition)", name=name)
        return None
sys.meta_path.insert(0, _NoYaml())
'''


@pytest.fixture
def no_yaml_dir(tmp_path):
    d = tmp_path / "no-yaml-site"
    d.mkdir()
    (d / "sitecustomize.py").write_text(_NO_YAML, encoding="utf-8")
    return d


def _child_env(tmp_path, home, no_yaml_dir=None):
    bundled = tmp_path / "bundled-plugins"
    bundled.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_")}
    env.update(HOME=str(tmp_path / "os-home"), HERMES_HOME=str(home), HERMES_BUNDLED_PLUGINS=str(bundled),
               HERMES_ENABLE_PROJECT_PLUGINS="0", HERMES_DISABLE_LAZY_INSTALLS="1")
    paths = [str(no_yaml_dir)] if no_yaml_dir else []
    env["PYTHONPATH"] = os.pathsep.join([*paths, str(REPO)])
    return env


def _home_with(tmp_path, keys, tag="home"):
    home = tmp_path / tag / ".hermes"
    (home / "plugins").mkdir(parents=True)
    for key in keys:
        src = PLUGINS_SRC / key
        if not (src / "plugin.yaml").is_file():
            pytest.fail(f"plugins/{key} is not present")
        shutil.copytree(src, home / "plugins" / key)
    (home / "config.yaml").write_text(hermes_yaml.safe_dump({"plugins": {"enabled": list(keys)}}), encoding="utf-8")
    return home


_LOAD_PROBE = '''
import json
from hermes_cli.plugins import PluginManager
m = PluginManager(); m.discover_and_load()
print(json.dumps({k: {"enabled": bool(v.enabled), "error": (str(v.error) if v.error else None)}
                  for k, v in m._plugins.items()}))
'''


def _load_in_child(tmp_path, keys, no_yaml_dir=None, tag="home"):
    home = _home_with(tmp_path, keys, tag=tag)
    proc = subprocess.run([sys.executable, "-c", _LOAD_PROBE], capture_output=True, text=True, encoding="utf-8",
                          env=_child_env(tmp_path / tag, home, no_yaml_dir), cwd=str(tmp_path), timeout=300,
                          stdin=subprocess.DEVNULL, check=False)
    assert proc.returncode == 0, f"load probe failed: {proc.stderr[-2000:]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_ac_upg_f68_1(tmp_path, no_yaml_dir):
    """(derived, msg 8 (e)(i)) Each of the 8 fork plugins (`hello`, `nv-approval-ledger`, `nv-artifact`, `nv-cost-cap`, `nv-coworker-compose`, `nv-fleet-gates`, `nv-workflow-modes`, `podman-onecli`), enabled alone in an isolated `HERMES_HOME`, is enabled with no load error, in a child interpreter whose `yaml` import is blocked (the base-install closure has no PyYAML)."""
    blocked = subprocess.run([sys.executable, "-c", "import yaml"], capture_output=True, text=True, encoding="utf-8",
                             env=_child_env(tmp_path, tmp_path, no_yaml_dir), timeout=60, check=False)
    assert blocked.returncode != 0 and "No module named 'yaml'" in blocked.stderr, "the yaml blocker must be in force"
    for key in FORK_PLUGINS:
        state = _load_in_child(tmp_path, (key,), no_yaml_dir, tag=f"h-{key}")
        assert key in state, f"{key} was not discovered"
        assert state[key] == {"enabled": True, "error": None}, f"{key} without PyYAML: {state[key]}"


def test_ac_upg_f68_2(tmp_path, monkeypatch):
    """All 8 fork plugins enabled together in one isolated home load with no error, each within the tag's default `plugins.load_timeout_seconds` (no "load timed out" reason)."""
    home = _home_with(tmp_path, FORK_PLUGINS)
    bundled = tmp_path / "bundled-plugins"
    bundled.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    manager = PluginManager()
    manager.discover_and_load()
    for key in FORK_PLUGINS:
        loaded = manager._plugins.get(key)
        assert loaded is not None, f"{key} was not discovered"
        assert "timed out" not in str(loaded.error or ""), f"{key} hit the load deadline: {loaded.error}"
        assert loaded.enabled is True and loaded.error is None and loaded.module is not None, f"{key}: {loaded.error}"


def test_ac_upg_f68_3():
    """`hermes_cli.plugin_dev.doctor_plugin(<dir>)` reports `ok` with no error-level finding for each of the 8 fork plugin directories."""
    from hermes_cli.plugin_dev import doctor_plugin

    for key in FORK_PLUGINS:
        report = doctor_plugin(PLUGINS_SRC / key)
        assert report.ok is True, f"doctor {key}: {report}"


def _tree_files(root):
    return sorted(p.relative_to(root) for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts)


def test_ac_upg_f68_4(tmp_path, no_yaml_dir):
    """(msg 8 (e)) Neither fork plugin module that used PyYAML (`nv-coworker-compose`'s `compose.py` and `openshell/installer.py`) needs PyYAML: `hermes coworker compose` renders the FLEET-F62.c openshell spec in a child with `yaml` blocked. Every rendered YAML file loads under `hermes_yaml.safe_load` and is byte-identical to the merged golden after the home token is substituted."""
    home = _home_with(tmp_path, (COMPOSE, "nv-fleet-gates"))
    out = tmp_path / "out"
    proc = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "coworker", "compose", str(F62C_SPEC), "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(REPO), timeout=600,
        env=_child_env(tmp_path, home, no_yaml_dir), stdin=subprocess.DEVNULL, check=False)
    assert proc.returncode == 0, f"compose without PyYAML failed: {proc.stderr[-2000:]}"
    subs = sorted(p.parent.name for p in out.glob("*/config.yaml"))
    assert subs, "the render produced no profile"
    for sub in subs:
        rendered, golden = out / sub, F62C_GOLDEN / sub
        assert golden.is_dir(), f"golden dir missing for {sub}"
        assert _tree_files(rendered) == _tree_files(golden), f"file set differs from the golden under {sub}"
        for rel in _tree_files(rendered):
            got = (rendered / rel).read_bytes().replace(str(home).encode(), HOME_TOKEN.encode())
            assert got == (golden / rel).read_bytes(), f"golden byte mismatch: {sub}/{rel}"
            if rel.suffix in (".yaml", ".yml"):
                hermes_yaml.safe_load((rendered / rel).read_text(encoding="utf-8-sig"))


def test_ac_upg_f68_5(tmp_path, no_yaml_dir):
    """The OpenShell installer's in-sandbox entrypoint `install-into-sandbox.sh <spec> --ref <sha> --dry-run`, given `HERMES_PYTHON` = an interpreter whose `yaml` import is blocked and an explicit scratch `HERMES_HOME`, exits 0 for the OSH-F64 lane spec (`tests/e2e-scenarios/OSH-F64/spec/coworker-types.yaml`) and prints its `openshell sandbox create` plan steps."""
    home = tmp_path / "ihome"
    (home / "plugins").mkdir(parents=True)
    (home / "config.yaml").write_text("plugins:\n  enabled: [nv-coworker-compose]\n", encoding="utf-8")
    env = _child_env(tmp_path, home, no_yaml_dir)
    env["HERMES_MANAGED_DIR"] = str(tmp_path / "managed")
    (tmp_path / "managed").mkdir()
    wrapper = tmp_path / "py-no-yaml"
    wrapper.write_text(f'#!/bin/sh\nPYTHONPATH="{no_yaml_dir}{os.pathsep}{REPO}" exec "{sys.executable}" "$@"\n',
                       encoding="utf-8")
    wrapper.chmod(0o755)
    env["HERMES_PYTHON"] = str(wrapper)
    script = PLUGINS_SRC / COMPOSE / "openshell" / "install-into-sandbox.sh"
    proc = subprocess.run(["bash", str(script), str(OSH_F64_SPEC), "--ref", SHA40, "--dry-run"],
                          capture_output=True, text=True, encoding="utf-8", cwd=str(tmp_path), env=env, timeout=300,
                          stdin=subprocess.DEVNULL, check=False)
    assert proc.returncode == 0, f"install-into-sandbox.sh --dry-run without PyYAML failed: {proc.stderr[-2000:]}"
    creates = [ln for ln in proc.stdout.splitlines() if "openshell sandbox create" in ln]
    assert creates, f"the plan must list sandbox creates; got: {proc.stdout[-1500:]}"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for name in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(name, raising=False)
    kb.init_db()
    return home


def _sub(conn, task_id, chat_id):
    from hermes_cli import kanban_db_notify as kbn

    return next(s for s in kbn.list_notify_subs(conn, task_id) if s["chat_id"] == chat_id)


def test_ac_upg_f68_6(kanban_home, tmp_path, monkeypatch):
    """(msg 8 conflict 1) The notifier's per-subscription cursor write resolves the DB through the env pin: with `HERMES_KANBAN_DB` pinned and a sub on a non-default board slug, `GatewayRunner._kanban_advance` moves that sub's cursor in the pinned DB (upstream `387c78c8`)."""
    from gateway.kanban_watchers import GatewayKanbanWatchersMixin
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_notify as kbn

    pinned = tmp_path / "pinned" / "kanban.db"
    pinned.parent.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_DB", str(pinned))
    kb.init_db()
    conn = kbc.connect()
    try:
        task_id = kb.create_task(conn, title="upg-f68 pin", assignee="worker")
        kbn.add_notify_sub(conn, task_id=task_id, platform="telegram", chat_id="chat-pin")
        sub = _sub(conn, task_id, "chat-pin")
        before = int(sub.get("last_event_id") or 0)
    finally:
        conn.close()

    GatewayKanbanWatchersMixin()._kanban_advance(sub, before + 7, board="side-board")

    conn = kbc.connect()
    try:
        assert int(_sub(conn, task_id, "chat-pin")["last_event_id"]) == before + 7, \
            "the cursor write must land in the env-pinned DB the notifier reads"
    finally:
        conn.close()


def test_ac_upg_f68_7(kanban_home):
    """(msg 8 conflict 1) The same helper returns the notify op's result: `GatewayRunner._kanban_claim` returns `True` for the first lease claim on a durable `api_server` sub and `False` for a second claim with the same expected cursor while the lease is held (GOV-F25 `return getattr(...)`)."""
    from gateway.kanban_watchers import GatewayKanbanWatchersMixin
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_notify as kbn

    conn = kbc.connect()
    try:
        task_id = kb.create_task(conn, title="upg-f68 lease", assignee="reviewer")
        kbn.add_notify_sub(conn, task_id=task_id, platform="api_server", chat_id="sess-owner",
                           delivery_mode="wake", retry_policy="durable")
        sub = _sub(conn, task_id, "sess-owner")
    finally:
        conn.close()
    cursor = int(sub.get("last_event_id") or 0)
    runner = GatewayKanbanWatchersMixin()
    first = runner._kanban_claim(sub, cursor, "drainer-a", lease_until=2_000_000_000, now=1_000_000_000)
    second = runner._kanban_claim(sub, cursor, "drainer-b", lease_until=2_000_000_000, now=1_000_000_001)
    assert first is True, f"the first claim must report it took the lease, got {first!r}"
    assert second is False, f"a second claim while the lease is held must report False, got {second!r}"


def test_ac_upg_f68_8():
    """(msg 8 conflict 2) `_run_idempotent` accepts `require_persist=True` as a keyword. With it, a same-key retry recomputes an unpersisted result; without it, the result is served from the cache (upstream default)."""
    import uuid
    from gateway.platforms.api_server_openai_routes import OpenAICompatRoutesMixin

    class _Routes(OpenAICompatRoutesMixin):
        # The pre-authority path: v0.22.5's _run_idempotent reads the runner's session authority.
        gateway_runner = SimpleNamespace(session_authority=None)

        def _run_idempotency_scope(self, request):
            return "upg-f68-scope"

    runs = []

    async def compute():
        runs.append(1)
        return {"turn_persisted": False, "final_response": "x"}, {}

    async def drive(key, **kw):
        request = SimpleNamespace(headers={"Idempotency-Key": key})
        out, err = await _Routes()._run_idempotent(request, {"m": 1}, compute, log_label="ac8",
                                                    fingerprint_keys=["m"], route="/v1/upg-f68", **kw)
        assert err is None and out[0]["turn_persisted"] is False
        return out

    durable, plain = f"d-{uuid.uuid4().hex}", f"p-{uuid.uuid4().hex}"
    asyncio.run(drive(durable, require_persist=True))
    asyncio.run(drive(durable, require_persist=True))
    assert len(runs) == 2, "require_persist: an unpersisted result must not be cached"
    asyncio.run(drive(plain))
    asyncio.run(drive(plain))
    assert len(runs) == 3, "without require_persist the completed result is served from the cache"


def _child_pytest(tmp_path, *args, timeout=3600, extra_path=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env["HOME"] = str(tmp_path / "pytest-home")
    if extra_path:
        env["PYTHONPATH"] = os.pathsep.join([str(extra_path), *filter(None, [env.get("PYTHONPATH")])])
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=", *args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(REPO),
                          env=env, timeout=timeout, stdin=subprocess.DEVNULL, check=False)


def _assert_suite_passes(tmp_path, rel):
    path = REPO / rel
    assert path.is_file(), f"{rel} is not present"
    proc = _child_pytest(tmp_path, "-m", "not integration and not live", str(path))
    tail = proc.stdout[-3000:]
    summary = re.findall(r"(\d+) (passed|failed|error|errors)", tail)
    counts = {kind.rstrip("s"): int(n) for n, kind in summary}
    assert proc.returncode == 0, f"{rel} on this interpreter: rc {proc.returncode}\n{tail}"
    assert counts.get("passed", 0) >= 1 and not counts.get("failed") and not counts.get("error"), f"{rel}: {tail}"


def test_ac_upg_f68_9(tmp_path):
    """(#78 replay) The `nv-cost-cap` resume card gate holds at the tag: COST-F30.b's acceptance file collects and passes on Python 3.14."""
    _assert_suite_passes(tmp_path, "tests/plugins/test_nv_cost_cap_resume_card_acceptance.py")


def test_ac_upg_f68_10(tmp_path):
    """(#77 replay) FLEET-F62.d's acceptance file collects and passes on Python 3.14."""
    _assert_suite_passes(tmp_path, "tests/plugins/test_fleet_f62_d_acceptance.py")


def test_ac_upg_f68_11(tmp_path):
    """(#80 replay) FLEET-F62.e's acceptance file collects and passes on Python 3.14. Its AC-4 and AC-5 are re-pointed per §Design D3: each simulated process start runs in a fresh interpreter, and trust is observed through TLS handshakes, not `get_ca_certs()`."""
    _assert_suite_passes(tmp_path, "tests/plugins/test_fleet_f62e_acceptance.py")


def _tag_commit():
    found = subprocess.run(["git", "-C", str(REPO), "rev-parse", "-q", "--verify", f"{TAG_COMMIT}^{{commit}}"],
                           capture_output=True, text=True, encoding="utf-8", check=False)
    if found.returncode != 0:
        pytest.fail(f"the selected tag commit {TAG_COMMIT} is not in this clone (a shallow clone?)")
    return found.stdout.strip()


def test_ac_upg_f68_12(tmp_path):
    """(msg 8 (e)(ii), (f)) Every fork-retained or fork-replayed pytest module, meaning each `test_*.py` under `tests/` that differs from the tag, copies and renames included (`git diff --name-only --diff-filter=ACMR <tag> HEAD -- tests/`, basename `test_*.py`, the same selector as gate 6(b)), collects with no error in the dev-group, no-extras venv, together with its `conftest.py` chain. No collected item carries a legacy `linux_only` / `macos_only` / `windows_only` marker."""
    diff = subprocess.run(["git", "-C", str(REPO), "diff", "--name-only", "--diff-filter=ACMR", _tag_commit(), "HEAD",
                           "--", "tests/"], capture_output=True, text=True, check=True).stdout.split()
    modules = sorted(p for p in diff if Path(p).name.startswith("test_") and p.endswith(".py") and (REPO / p).is_file())
    assert modules, "no fork test module differs from the tag (wrong HEAD?)"
    probe = tmp_path / "conftest_marks.py"
    probe.write_text(
        f"LEGACY = {LEGACY_MARKS!r}\n"
        "def pytest_collection_modifyitems(session, config, items):\n"
        "    hits = [f'{i.nodeid}: {m}' for i in items for m in LEGACY if i.get_closest_marker(m)]\n"
        "    if hits:\n"
        "        raise SystemExit('LEGACY-MARK ' + '; '.join(hits[:20]))\n", encoding="utf-8")
    proc = _child_pytest(tmp_path, "-p", "conftest_marks", "--collect-only", *modules, extra_path=tmp_path)
    out = proc.stdout + proc.stderr
    errors = sorted(set(re.findall(r"ERROR collecting (\S+)", out)))
    assert "LEGACY-MARK" not in out, f"legacy platform markers remain: {out[out.find('LEGACY-MARK'):][:1500]}"
    assert "UsageError" not in out, f"collection usage error: {out[-1500:]}"
    assert not errors and proc.returncode == 0, f"{len(errors)} of {len(modules)} modules fail collection: {errors[:30]}"


def _mint(common_name, sign_leaf_for=None):
    """A throwaway CA (PEM, key) or, with ``sign_leaf_for=(ca_key, ca_cert)``, a 127.0.0.1 leaf it signs."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.datetime.now(datetime.UTC)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    builder = (x509.CertificateBuilder().subject_name(name).public_key(key.public_key())
               .serial_number(x509.random_serial_number())
               .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1)))
    if sign_leaf_for is None:
        cert = (builder.issuer_name(name)
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                .sign(key, hashes.SHA256()))
    else:
        ca_key, ca_cert = sign_leaf_for
        cert = (builder.issuer_name(ca_cert.subject)
                .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                               critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
                .sign(ca_key, hashes.SHA256()))
    pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    return SimpleNamespace(cert=cert, key=key, pem=pem, key_pem=key_pem)


@contextlib.contextmanager
def _tls_server(tmp_path, ca, tag):
    """A loopback HTTPS server whose leaf is signed by ``ca``. Yields its URL."""
    leaf = _mint(f"upg-f68-leaf-{tag}", sign_leaf_for=(ca.key, ca.cert))
    (tmp_path / f"{tag}.pem").write_bytes(leaf.pem)
    (tmp_path / f"{tag}.key").write_bytes(leaf.key_pem)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(tmp_path / f"{tag}.pem"), str(tmp_path / f"{tag}.key"))

    class _Ok(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _Ok)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


_HANDSHAKE = '''
import json, sys
import httpx
from agent.ssl_verify import resolve_httpx_verify
out = {}
for tag, url in json.loads(sys.argv[1]).items():
    try:
        httpx.get(url, verify=resolve_httpx_verify(), timeout=10)
        out[tag] = "ok"
    except Exception as exc:
        out[tag] = type(exc).__name__
print(json.dumps(out))
'''


def _handshakes_in_fresh_process(tmp_path, environ, urls):
    """One Hermes HTTPS client in a NEW process whose env is ``environ``: which loopback servers it can verify."""
    env = {k: v for k, v in environ.items() if k in (*CA_ENV_NAMES, "PATH")}
    env.update(HOME=str(tmp_path / "os-home"), HERMES_HOME=str(tmp_path / "client-home"), PYTHONPATH=str(REPO),
               NO_PROXY="127.0.0.1,localhost", HERMES_DISABLE_LAZY_INSTALLS="1")
    proc = subprocess.run([sys.executable, "-c", _HANDSHAKE, json.dumps(urls)], capture_output=True, text=True, encoding="utf-8",
                          env=env, cwd=str(tmp_path), timeout=120, stdin=subprocess.DEVNULL, check=False)
    assert proc.returncode == 0, f"handshake probe failed: {proc.stderr[-1500:]}"
    return {k: v == "ok" for k, v in json.loads(proc.stdout.strip().splitlines()[-1]).items()}


def test_ac_upg_f68_13(tmp_path, monkeypatch):
    """The `openshell_trust` secret source makes a 0.22 Hermes HTTPS client trust the live sandbox CA after a re-mint, without editing a file. After two process starts with the live bundle re-minted between them, an `httpx` client built with `agent.ssl_verify.resolve_httpx_verify()` completes a TLS handshake to a loopback server signed by the new CA and fails against one signed by the superseded CA."""
    from agent.secret_sources import registry
    from hermes_cli.env_loader import reset_secret_source_cache

    for name in CA_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    home = _home_with(tmp_path, (COMPOSE,), tag="trust")
    bundled = tmp_path / "bundled-plugins"
    bundled.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    manager = PluginManager()
    manager.discover_and_load()
    assert manager._plugins[COMPOSE].enabled is True, manager._plugins[COMPOSE].error

    ca_stale, ca_b, ca_c = (_mint(f"upg-f68-{n}") for n in ("CA-A-stale", "CA-B", "CA-C"))
    stale = tmp_path / "persisted" / "copied-ca.pem"
    stale.parent.mkdir()
    stale.write_bytes(ca_stale.pem)
    live = tmp_path / "openshell-tls" / "ca-bundle.pem"
    live.parent.mkdir()
    live.write_bytes(ca_b.pem)
    cfg = {"sources": [TRUST_SOURCE], TRUST_SOURCE: {"enabled": True, "override_existing": True, "bundle": str(live)}}

    def start():
        environ = {**{n: str(stale) for n in CA_ENV_NAMES}, "PATH": os.environ.get("PATH", "")}
        reset_secret_source_cache()
        registry.apply_all(cfg, home, environ=environ)
        assert environ["SSL_CERT_FILE"] == str(live), f"start must point SSL_CERT_FILE at the live bundle: {environ}"
        return environ

    with _tls_server(tmp_path, ca_stale, "a") as url_a, _tls_server(tmp_path, ca_b, "b") as url_b, \
            _tls_server(tmp_path, ca_c, "c") as url_c:
        first = _handshakes_in_fresh_process(tmp_path, start(), {"a": url_a, "b": url_b})
        assert first == {"a": False, "b": True}, f"start 1 must trust the live CA only: {first}"
        live.write_bytes(ca_c.pem)
        second = _handshakes_in_fresh_process(tmp_path, start(), {"a": url_a, "b": url_b, "c": url_c})
        assert second == {"a": False, "b": False, "c": True}, f"after the re-mint only the new CA verifies: {second}"


_DEFAULT_CTX = '''
import json, socket, ssl, sys
from agent.ssl_verify import install_truststore, platform_ssl_context
install_truststore()
host, port = sys.argv[1], int(sys.argv[2])
try:
    with socket.create_connection((host, port), timeout=10) as raw:
        with platform_ssl_context().wrap_socket(raw, server_hostname=host):
            print(json.dumps({"ok": True}))
except ssl.SSLError as exc:
    print(json.dumps({"ok": False, "error": type(exc).__name__}))
'''


def test_ac_upg_f68_14(tmp_path):
    """(derived; adopt-proof for AC-15/AC-16) Under the tag's TLS policy, a CA bundle named only by `SSL_CERT_FILE` is trusted by the default context on top of the platform store, while the same bundle named only by `REQUESTS_CA_BUNDLE` or `HERMES_CA_BUNDLE` is not trusted."""
    ca = _mint("upg-f68-policy-CA")
    bundle = tmp_path / "ca.pem"
    bundle.write_bytes(ca.pem)
    results = {}
    with _tls_server(tmp_path, ca, "p") as url:
        port = url.rsplit(":", 1)[1].strip("/")
        for name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "HERMES_CA_BUNDLE"):
            env = {"PATH": os.environ.get("PATH", ""), name: str(bundle), "PYTHONPATH": str(REPO),
                   "HOME": str(tmp_path / "os-home"), "HERMES_HOME": str(tmp_path / "h")}
            proc = subprocess.run([sys.executable, "-c", _DEFAULT_CTX, "127.0.0.1", port], capture_output=True,
                                  text=True, encoding="utf-8", env=env, timeout=120, stdin=subprocess.DEVNULL, check=False)
            assert proc.returncode == 0, f"{name}: {proc.stderr[-1500:]}"
            results[name] = json.loads(proc.stdout.strip().splitlines()[-1])["ok"]
    assert results == {"SSL_CERT_FILE": True, "REQUESTS_CA_BUNDLE": False, "HERMES_CA_BUNDLE": False}, results


_EGRESS_ENV = {
    "HTTPS_PROXY": "http://egress-proxy.invalid:3128", "HTTP_PROXY": "http://egress-proxy.invalid:3128",
    "NO_PROXY": "localhost,127.0.0.1", "SSL_CERT_FILE": "/etc/ssl/certs/fleet-ca.pem",
    "REQUESTS_CA_BUNDLE": "/etc/ssl/certs/fleet-ca.pem", "NODE_EXTRA_CA_CERTS": "/etc/ssl/certs/fleet-ca.pem",
}


LIVE_FIXTURE_CONFIG = REPO / "tests" / "e2e-scenarios" / "FLEET-F62.a" / "fixtures" / "orchestrator" / "config.yaml"


def _served_home(tmp_path, monkeypatch, config_text):
    root = tmp_path / "hermes-root"
    home = root / "profiles" / "orchestrator"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(config_text, encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(root))
    return home


def _bot_chat_target(home, monkeypatch):
    """Fire a bare `deliver: bot-chat` from a tick bound to the served home: (owner home, RPC params).

    Any child process the delivery tries to start is recorded and refused."""
    import cron.scheduler_delivery as delivery
    from agent.secret_scope import set_multiplex_active
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools import bot_live_delivery as live

    seen, spawned = [], []

    def owner_of(profile_home):
        return dict(profile_home=str(Path(profile_home).resolve()), session_id="bot-chat", canonical=True,
                    lease_id="authority-1", live_session_id="bot-chat")

    def rpc(target_home, params):
        seen.append((Path(target_home).resolve(), dict(params)))
        return {"status": "settled", "delivery_id": params["id"], "message": params["message"]}

    def no_spawn(*args, **kwargs):
        spawned.append(args[0] if args else kwargs.get("args"))
        raise AssertionError("a bot-chat delivery to a live owner must not spawn a child process")

    monkeypatch.setattr(live, "find_canonical_live_owner", owner_of)
    monkeypatch.setattr(live, "authority_delivery", rpc)
    set_multiplex_active(True)
    bound = set_hermes_home_override(str(home))
    try:
        with pytest.MonkeyPatch.context() as guard:
            guard.setattr(subprocess, "run", no_spawn)
            guard.setattr(subprocess, "Popen", no_spawn)
            assert delivery._deliver_to_bot_chat({"id": "job-1", "name": "supervise-issues"}, "brief", "") is None
    finally:
        reset_hermes_home_override(bound)
        set_multiplex_active(False)
    assert spawned == [], f"no child process may be spawned, got {spawned}"
    assert len(seen) == 1, f"exactly one authority delivery, got {len(seen)}"
    return seen[0]


def _executed_provider(home):
    """The provider block an admission for `home` executes under: the authority's own owner-scope resolution.

    Inside that scope the process's proxy and CA variables (`_EGRESS_ENV`, set by the caller) are unchanged."""
    from gateway.session_ingress import _execution_scope
    from hermes_cli.config import load_config

    authority = SimpleNamespace(runner=SimpleNamespace(config=SimpleNamespace(multiplex_profiles=True)),
                                profile_id=str(home.resolve()))

    async def run():
        with await _execution_scope(authority, None):
            cfg = load_config()
            egress = {k: os.environ.get(k) for k in _EGRESS_ENV}
        name = (cfg.get("model") or {}).get("provider")
        return name, (cfg.get("providers") or {}).get(name) if name else None, egress

    name, block, egress = asyncio.run(run())
    assert egress == _EGRESS_ENV, f"the admission's scope changed the proxy/CA environment: {egress}"
    return name, block


def _live_provider_ok(name, block, expected):
    return (name == "coworkers-live" and isinstance(block, dict)
            and block.get("base_url") == expected["base_url"]
            and block.get("api_mode") == "anthropic_messages"
            and block.get("default_model") == expected["default_model"]
            and isinstance(block.get("api_key"), str) and bool(block.get("api_key").strip()))


def test_ac_upg_f68_15(tmp_path, monkeypatch):
    """(derived; adopt-proof for AC-LOOP-F39-6) A bare `deliver: bot-chat` cron delivery fired from a multiplexed tick bound to a served profile is delivered to that profile's authority: `deliver_to_live_owner` targets that profile's home, the `bot_relay.deliver` params name that profile, and no child process is spawned. The admission executes under that profile's own runtime scope, inside which the process's proxy and CA environment variables are unchanged, and whose effective provider is that profile's: with the scenario's orchestrator fixture it is `providers.coworkers-live` with the fixture's `base_url`, `api_mode: anthropic_messages`, `default_model` and a non-empty inline key field. A `model: {}` control profile fails that check."""
    fixture_text = LIVE_FIXTURE_CONFIG.read_text(encoding="utf-8")
    fixture = hermes_yaml.safe_load(fixture_text)["providers"]["coworkers-live"]
    expected = {"base_url": fixture["base_url"], "default_model": fixture["default_model"]}
    for key, value in _EGRESS_ENV.items():
        monkeypatch.setenv(key, value)

    home = _served_home(tmp_path, monkeypatch, fixture_text)
    target, params = _bot_chat_target(home, monkeypatch)
    assert target == home.resolve(), f"delivered to {target}, not the served profile's home"
    assert params["profile"] == "orchestrator", params
    assert _live_provider_ok(*_executed_provider(home), expected) is True

    bare = _served_home(tmp_path / "control", monkeypatch, "model: {}\n")
    target2, params2 = _bot_chat_target(bare, monkeypatch)
    assert target2 == bare.resolve() and params2["profile"] == "orchestrator"
    assert _live_provider_ok(*_executed_provider(bare), expected) is False


_BOT = "researcher"
_DM_BODY = "status? PAYLOAD_R8 " + "x" * 400 + " TAIL_R8"
_LAUNCHER = '''#!{python}
import sys
sys.path.insert(0, {repo!r})
from hermes_cli.main import main
sys.exit(main())
'''


class _ModelStub:
    """An OpenAI-compatible loopback endpoint. Each request blocks on `gate` and answers `reply`."""

    def __init__(self, reply):
        self.reply, self.gate, self.served, self.requests = reply, threading.Event(), threading.Event(), []
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.requests.append(request)
                stub.gate.wait(120)
                message = {"role": "assistant", "content": stub.reply}
                body = {"id": "chatcmpl-r8", "object": "chat.completion", "created": 1, "model": "test-model",
                        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
                kind = "application/json"
                if request.get("stream"):
                    body["object"] = "chat.completion.chunk"
                    body["choices"][0]["delta"] = body["choices"][0].pop("message")
                    raw, kind = ("data: " + json.dumps(body) + "\n\ndata: [DONE]\n\n").encode(), "text/event-stream"
                else:
                    raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                stub.served.set()

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def close(self):
        self.gate.set()
        self.server.shutdown()
        self.server.server_close()


class _SenderTranscript:
    """The sender's session store: a Bot Chat, recording every appended row."""

    def __init__(self):
        self.rows = []

    def get_session(self, session_id):
        return {"id": session_id, "title": "Bot Chat"}

    def get_session_title(self, session_id):
        return "Bot Chat"

    def append_message(self, session_id, role, content=None, **kwargs):
        self.rows.append({"session_id": session_id, "role": role, "content": content, **kwargs})


def _bot_sender():
    return SimpleNamespace(_session_db=_SenderTranscript(), session_id="sess-r8-sender", _session_title_hint=None,
                           _bot_mode_protocol=True, tools=[], valid_tool_names=set())


@pytest.fixture
def bot_world(tmp_path, monkeypatch):
    """A Bot-Mode-managed root: the sender is the root profile, the target is the teammate `researcher`, whose `hermes`
    launcher is this checkout's real CLI. The DM dir lives under the test's temp root; teardown stops every runner."""
    import tempfile

    from tools import bot_relay
    from tools import bot_mode_probe
    from tools.process_registry import process_registry

    root = tmp_path / ".hermes"
    target = root / "profiles" / _BOT
    target.mkdir(parents=True)
    (target / "profile.yaml").write_text("description: teammate\nui_meta:\n  hermes-bots:\n    shape: cloud\n",
                                         encoding="utf-8")
    (tmp_path / "os-home").mkdir()
    (tmp_path / "tmp").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    launcher = tmp_path / "bin" / "hermes"
    launcher.parent.mkdir()
    launcher.write_text(_LAUNCHER.format(python=sys.executable, repo=str(REPO)), encoding="utf-8")
    launcher.chmod(0o755)
    monkeypatch.setattr(bot_relay, "_hermes_cli", lambda: str(launcher))
    monkeypatch.setattr(process_registry, "pending_watchers", [])
    bot_mode_probe._reset_cache_for_tests()
    world = SimpleNamespace(root=root, target=target, stop=threading.Event(), closers=[])
    world.with_redrive = lambda: _with_redrive(world, monkeypatch)
    yield world
    world.stop.set()
    process_registry.kill_all(consume_output=True)
    for close in reversed(world.closers):
        close()
    bot_mode_probe._reset_cache_for_tests()


def _target_db(world):
    from hermes_state import SessionDB

    db = SessionDB(db_path=world.target / "state.db")
    world.closers.append(db.close)
    return db


def _model_for_target(world, reply):
    """The target profile's model is a loopback stub held on its gate; no credential, nothing beyond 127.0.0.1."""
    stub = _ModelStub(reply)
    world.closers.append(stub.close)
    (world.target / "config.yaml").write_text(
        f"model:\n  provider: custom\n  base_url: {stub.url}\n  api_mode: chat_completions\n  default: test-model\n"
        "memory:\n  memory_enabled: false\n  user_profile_enabled: false\n"
        "terminal:\n  env: local\n", encoding="utf-8")
    (world.target / ".env").write_text(f"OPENAI_API_KEY=local-test-only\nOPENAI_BASE_URL={stub.url}\n", encoding="utf-8")
    return stub


def _live_owner(world, db, session_id, reply, *, outcome=None, persist_row=True):
    """A Desktop-shaped live owner of `session_id`: a real lease advertised as a delivery consumer, polled by the real
    `_poll_bot_live_delivery_once`. Its turn body appends the turn's user row to the claimed session (as the live turn
    persists it; with `persist_row` false it writes nothing, a target-side storage failure the turn survives), then
    settles the receipt with `reply` (or ends it with `outcome`) once `gate` is set."""
    from hermes_cli.active_sessions import try_acquire_active_session
    from tui_gateway import session_notifications
    from tui_gateway.method_ctx import rebind
    from tui_gateway.session_lifecycle import _session_turn_admission

    lease, refusal = try_acquire_active_session(
        session_id=session_id, surface="desktop", config={}, registry_home=world.target,
        metadata={"live_session_id": "live-r8", "bot_live_delivery_consumer": True})
    assert refusal is None, refusal
    world.closers.append(lease.release)
    owner = SimpleNamespace(gate=threading.Event(), claims=[])

    def submit(rid, sid, session, text, **kwargs):
        owner.claims.append(text)
        if persist_row:
            db.append_message(session["session_key"], "user", content=text)

        def finish():
            owner.gate.wait(120)
            kwargs["terminal_callback"](outcome or {"status": "settled", "text": reply})
            session["running"] = False

        threading.Thread(target=finish, daemon=True).start()
        return True

    poll = rebind(session_notifications._poll_bot_live_delivery_once, {
        "_session_home": lambda session: world.target,
        "_session_turn_admission": _session_turn_admission,
        "_run_prompt_submit": submit,
        "_notif_release_turn": lambda session: session.update(running=False),
    })
    session = {"history_lock": threading.RLock(), "agent": object(), "session_key": session_id,
               "active_session_lease": lease}

    def loop():
        while not world.stop.wait(0.05):
            if not session.get("running"):
                poll("live-r8", session)

    threading.Thread(target=loop, daemon=True).start()
    return owner


def _resumed_bot_chat(db):
    """The session `chat -c 'Bot Chat'` resumes: the CLI's own resolver, projected to the compression tip."""
    sid = db.resolve_session_by_title("Bot Chat")
    return (db.get_compression_tip(sid) or sid) if sid else None


def _is_full(row):
    content = (row.get("content") or "").strip()
    return row.get("role") == "user" and content.startswith("Message from ") and content.endswith(_DM_BODY)


def _full_rows(db, sid):
    return [m for m in db.get_messages(sid) if _is_full(m)] if sid else []


def _seed_shape(db, shape):
    """Arrange the target transcript for one AC-21 shape. Returns (the session the delivery must land in, or None for
    the one `--create-if-missing` mints; the sessions that must never receive it)."""
    if shape == "fresh":
        return None, []
    db.create_session("bc-root", source="cli")
    db.set_session_title("bc-root", "Bot Chat")
    if shape == "continuation":
        db.create_session("bc-2", source="cli")
        db.set_session_title("bc-2", "Bot Chat #2")  # a numbered sibling is not the Bot Mode chat (hermes_state_titles.py:197-205)
        return "bc-root", ["bc-2"]
    if shape == "compressed":
        db.end_session("bc-root", "compression")
        db.create_session("bc-tip", source="cli", parent_session_id="bc-root")
        return "bc-tip", ["bc-root"]
    if shape == "duplicate":
        db.append_message("bc-root", "user", content=_delivered())
    return "bc-root", []  # prefix_collision: its prefix-only row is written after the send (the test body)


def _delivered():
    return f"Message from 🤖 hermes (@hermes): {_DM_BODY}"


def _send(agent, task_id):
    from tools import bot_mode_dm

    return json.loads(bot_mode_dm.message_agent_tool(target=f"@{_BOT}", message=_DM_BODY, task_id=task_id,
                                                     agent=agent))


def _wait_for(predicate, why, timeout=90):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(why)


def _completions(registry, proc_id, *, timeout=120):
    """Wait for the runner to exit, then drain its completion events (others are put back) and return them."""
    import queue
    import time

    assert registry.get(proc_id)._completion_event.wait(timeout=timeout), "the delivery runner never exited"
    mine, others = [], []
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            evt = registry.completion_queue.get(timeout=0.2)
        except queue.Empty:
            if mine:
                break
            continue
        (mine if evt.get("session_id") == proc_id and evt.get("type", "completion") == "completion" else others).append(evt)
    for evt in others:
        registry.completion_queue.put(evt)
    return mine


def _settled_completion(evt):
    """A live delivery's success: exit 0 AND a `settled` outcome. The runner also exits 0 on a still-pending `queued` /
    `claimed` receipt (`_wait_live_dm`), which is not a success."""
    if evt.get("exit_code") != 0:
        return False
    try:
        return json.loads((evt.get("output") or "").strip().splitlines()[-1]).get("status") == "settled"
    except (ValueError, IndexError):
        return False


def _queued_completion(registry, proc_id):
    return [e for e in list(registry.completion_queue.queue) if e.get("session_id") == proc_id]



@pytest.mark.platforms("posix")
def test_ac_upg_f68_19(bot_world):
    """(R4 re-spec of AC-FLEET-F62.g-11) A pending local delivery survives its sender process and a killed runner, because a durable owner holds it. If the target exits before accepting, exits 0 without persisting, or its runner is killed, the delivery ends in a re-attempt on the same `delivery_id` or in one typed final failure in the sender's session. Before target acceptance, no success completion and no target transcript row exist. If a re-attempt then succeeds, exactly one target row and one final outcome exist.

    Proves the sender-turn-end half (E6.4): the ack is `queued` with a `delivery_id`, and the runner survives the
    sender's lifecycle sweeps (`agent_close`, `gateway_turn_timeout`, `kill_all`) while an explicit stop still ends it.
    The process-exit and killed-runner halves are AC-FLEET-F62.h-5 / -11 (carried)."""
    import logging

    from tools.process_registry import process_registry

    db = _target_db(bot_world)
    db.create_session("bc-live", source="desktop")
    db.set_session_title("bc-live", "Bot Chat")
    _live_owner(bot_world, db, "bc-live", "REPLY_R8_19")
    out = _send(_bot_sender(), "turn-ac19")
    assert out.get("status") == "queued" and out.get("delivery_id"), out
    proc_id = out["process_id"]
    for source in ("agent_close", "gateway_turn_timeout", "kill_all"):
        process_registry.kill_all("turn-ac19", source=source)
        assert process_registry.get(proc_id).exited is False, f"the runner died in the {source} sweep"
    # A refused signal and a slow reap return the same dict; only the kill's log lines tell them apart.
    kill_log = logging.getLogger("tools.process_registry")
    seen: list[logging.LogRecord] = []
    tap = logging.Handler(logging.INFO)
    tap.emit = seen.append
    prior_level = kill_log.level
    # macOS keeps `bash -lic` above the runner: once core SIGKILLs that root, the runner is reparented out of the pytest
    # subtree and the tag's live-system guard refuses core's next signal (CI 39129288055). For this one call only, a
    # refused SIGTERM/SIGKILL goes through when its PID was in the runner's tree while the root was alive and a child of
    # this test, and still has that start time; any other refusal stands.
    import posix
    import signal

    import psutil

    root = psutil.Process(process_registry.get(proc_id).pid)
    tree = {root.pid: root.create_time()} if os.getpid() in {p.pid for p in root.parents()} else {}
    guarded_kill = os.kill

    def _runner_tree_kill(pid, sig, *args):
        with contextlib.suppress(psutil.Error):
            if tree and root.is_running():
                tree.update({p.pid: p.create_time() for p in root.children(recursive=True)})
        try:
            return guarded_kill(pid, sig, *args)
        except RuntimeError:
            try:
                ours = int(sig) in (signal.SIGTERM, signal.SIGKILL) and psutil.Process(pid).create_time() == tree.get(pid)
            except psutil.Error:
                ours = False
            seen.append(logging.makeLogRecord({"msg": f"test: guard refused os.kill({pid}, {int(sig)}), "
                                                      f"{'let through' if ours else 'kept'}; runner tree {tree}"}))
            if not ours:
                raise
        return posix.kill(pid, sig, *args)

    kill_log.addHandler(tap)
    kill_log.setLevel(logging.INFO)
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(os, "kill", _runner_tree_kill)
            killed = process_registry.kill_process(proc_id, source="process.kill")
    finally:
        kill_log.removeHandler(tap)
        kill_log.setLevel(prior_level)
    assert process_registry.get(proc_id).exited is True, (killed, [record.getMessage() for record in seen])

_AC21_SHAPES = ("fresh", "continuation", "compressed", "duplicate", "prefix_collision")


@pytest.mark.platforms("posix")
@pytest.mark.parametrize(("shape", "path"), [
    *((shape, path) for path in ("cli_fallback", "live_owner") for shape in _AC21_SHAPES),
    ("fresh", "live_owner_unpersisted"),
])
def test_ac_upg_f68_21(bot_world, shape, path):
    """(R4 re-spec of AC-FLEET-F62.g-13) A delivery's success outcome is reported only once the target's resumed Bot Chat holds a `role='user'` row with the entire delivered attributed content, newer than the delivery's baseline. The resumed Bot Chat is the latest continuation, taken at its compression tip. An earlier identical message never yields that outcome, and neither does a newer row holding only a prefix (cases: fresh / continuation / compressed / duplicate / prefix_collision).

    At v0.22.2 the success outcome is the runner's one exit-0 completion carrying the reply, `settled` on the live path (E6.3). Both 0.22.2
    paths: `cli_fallback` (no live owner: a real `hermes -p researcher chat -c 'Bot Chat'` turn under the turn lock,
    its loopback model held until the test releases it) and `live_owner` (a real consumer lease, the real mailbox poll
    and claim; only the owner's turn body is stood in). `fresh` on `live_owner` has no chat to own, so its owner holds
    a Bot Chat with no prior rows. `live_owner_unpersisted`: the owner's turn completes but its user row never lands
    (both turn-start and turn-end persists log and continue, `agent/turn_context.py:508-524`,
    `agent/turn_finalizer.py:635-637`); no success outcome may follow. For the unpersisted case, this node's world loads
    nv-delivery-redrive with tracking live and sends through the real tool executor, a push sender, so the plugin's gate is
    what keeps core's `settled` from reaching the sender."""
    from tools.process_registry import process_registry

    db = _target_db(bot_world)
    landing, never = _seed_shape(db, shape)
    if path.startswith("live_owner") and landing is None:
        db.create_session("bc-live", source="desktop")
        db.set_session_title("bc-live", "Bot Chat")
        landing = "bc-live"
    baseline = len(_full_rows(db, landing))
    reply = f"REPLY_R8_21_{shape}"
    if path == "cli_fallback":
        gate = _model_for_target(bot_world, reply).gate
    else:
        owner = _live_owner(bot_world, db, landing, reply, persist_row=path == "live_owner")
        gate = owner.gate

    if path == "live_owner_unpersisted":
        out = bot_world.with_redrive()(f"turn-ac21-{shape}-{path}")
    else:
        out = _send(_bot_sender(), f"turn-ac21-{shape}-{path}")
    assert out.get("status") == "queued" and out.get("delivery_id"), out
    proc_id = out["process_id"]
    if path == "live_owner_unpersisted":
        from tools.bot_live_delivery import read_delivery_result

        assert out.get("redrive", {}).get("tracked") is True, out
        gate.set()
        mine = _completions(process_registry, proc_id)
        receipt = read_delivery_result(bot_world.target, out["delivery_id"]) or {}
        assert owner.claims and receipt.get("status") == "settled", ("core must have settled the turn", owner.claims, receipt)
        assert len(mine) == 1, mine
        payload = json.loads((mine[0].get("output") or "").strip().splitlines()[-1])
        assert (payload.get("status"), payload.get("reason"), payload.get("delivery_id")) == (
            "ambiguous", "unknown", out["delivery_id"]), payload
        assert _full_rows(db, landing) == [] and not [e for e in mine if _settled_completion(e)], (
            "a success outcome while the resumed Bot Chat holds no row with the delivered content", mine)
        return
    if shape == "prefix_collision":
        db.append_message(landing, "user", content=_delivered()[: len(_delivered()) // 2])
    _wait_for(lambda: len(_full_rows(db, landing or _resumed_bot_chat(db))) > baseline,
              "the target turn never persisted the delivered row")
    assert not process_registry.get(proc_id).exited and not _queued_completion(process_registry, proc_id), (
        "a success outcome before the target turn finished")

    gate.set()
    mine = _completions(process_registry, proc_id)
    assert len(mine) == 1 and mine[0].get("exit_code") == 0 and reply in (mine[0].get("output") or ""), mine
    if path == "live_owner":
        assert _settled_completion(mine[0]), mine[0]
    target = _resumed_bot_chat(db)
    assert target == (landing or target) and target is not None, (target, landing)
    rows = _full_rows(db, target)
    assert len(rows) == baseline + 1, db.get_messages(target)
    for sid in never:
        assert not [m for m in db.get_messages(sid) if "TAIL_R8" in (m.get("content") or "")], sid


def test_ac_upg_f68_22(bot_world):
    """(R4 re-spec of AC-FLEET-F62.g-15) The DM-cache sweep covers every delivery-state file kind: aged `*.signal` and `*.signal.tmp` sidecars and aged DM files are removed, and so is any delivery-state file kind the item-4 port adds. A fresh file of each kind is kept, and the durable state of a delivery that is still pending is never removed (F62.g-15's cases, `test_fleet_f62_g_acceptance.py:660-671` @0615c1e7, kept)."""
    from tools import bot_live_delivery as live
    from tools import bot_mode_dm

    dm_dir = bot_mode_dm._dm_dir()
    now = 2_000_000_000.0
    kinds = ("a.txt", "a.txt.live.json", "a.txt.signal", "a.txt.signal.tmp")
    for name in kinds:
        for age, prefix in ((48 * 3600, "old-"), (60, "new-")):
            path = dm_dir / f"{prefix}{name}"
            path.write_text("x", encoding="utf-8")
            os.utime(path, (now - age, now - age))
    pending = live._root(bot_world.target) / ("a" * 32 + ".json")
    pending.parent.mkdir(parents=True, exist_ok=True)
    pending.write_text('{"status": "queued"}', encoding="utf-8")
    os.utime(pending, (now - 48 * 3600, now - 48 * 3600))

    bot_mode_dm.cleanup_bot_dm_cache(now=now)

    left = {p.name for p in dm_dir.iterdir()}
    assert not any(n.startswith("old-") for n in left), left
    assert {f"new-{n}" for n in kinds} <= left, left
    assert pending.exists(), "a pending delivery's durable receipt must never be swept"


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("outcome", ["reply", "typed_failure"])
@pytest.mark.parametrize("sender", ["stateless", "kanban_worker"])
def test_ac_upg_f68_23(bot_world, monkeypatch, sender, outcome):
    """(R4 re-spec of AC-FLEET-F62.g-16; narrowed by user ruling, operator msg 1356) A sender whose session cannot receive an async completion (a stateless channel or a kanban worker) gets the queued ack with `reply_delivery: "poll"`. For a sender still running when the delivery settles, the delivery's final outcome, a reply or a typed failure, is persisted as a delivery row in that sender's transcript; for a sender that exits first, that row is explicitly waived. For both stateless-channel and kanban senders, no sender-session async completion watcher is armed, `notify_on_complete` remains false, and the one-shot linger does not wait on the runner."""
    import contextvars

    import gateway.session_context as sc
    from tools.process_registry import process_registry

    db = _target_db(bot_world)
    db.create_session("bc-live", source="desktop")
    db.set_session_title("bc-live", "Bot Chat")
    failure = {"status": "failed", "error": "Error code: 429 - rate limit exceeded", "text": ""}
    owner = _live_owner(bot_world, db, "bc-live", "REPLY_R8_23", outcome=failure if outcome == "typed_failure" else None)
    agent = _bot_sender()
    if sender == "kanban_worker":
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_ac23")
        out = _send(agent, f"turn-ac23-{sender}")
    else:
        def stateless_turn():
            sc.declare_stateless_channel()
            return _send(agent, f"turn-ac23-{sender}")

        out = contextvars.copy_context().run(stateless_turn)
    assert out.get("status") == "queued" and out.get("reply_delivery") == "poll", out
    proc_id = out["process_id"]
    session = process_registry.get(proc_id)
    assert session.notify_on_complete is False
    assert not [w for w in process_registry.pending_watchers if w.get("session_id") == proc_id]
    lingered = process_registry.wait_for_pending_completions(timeout=1, poll_interval=0.1)
    assert proc_id not in lingered.get("waited", []), lingered
    owner.gate.set()
    assert session._completion_event.wait(timeout=120), "the delivery runner never exited"
    _wait_for(lambda: agent._session_db.rows, "no delivery row in the sender's transcript", timeout=10)
    rows = agent._session_db.rows
    assert len(rows) == 1 and rows[0]["display_kind"] == "process_complete", rows
    assert rows[0]["session_id"] == "sess-r8-sender" and rows[0]["role"] == "user"
    expect = "REPLY_R8_23" if outcome == "reply" else "provider_rate_limit"
    assert expect in (rows[0]["content"] or ""), "the delivery row carries the outcome, not just a marker"


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("arming", ["normal", "delayed", "gateway"])
def test_ac_fleet_f62_g_14(bot_world, monkeypatch, arming):
    """(regression) An accepted delivery stays asynchronous: message_agent returns while the target turn is still running (or holding), and the target's reply arrives afterwards as exactly one unconsumed completion notification carrying the reply text, also when the target replies before the sender has armed notification. A gateway-hosted sender gets exactly one `pending_watchers` entry for the process, carrying its routing fields.

    At v0.22.2 (E6.3), on the spawn item 4 marks `persist_on_release`: the ack is `queued` + `delivery_id` +
    `reply_delivery: "notification"` (R4), not F62.g's `sent`. The target is a live owner that settles from the real
    mailbox. `delayed`: the owner settles inside the admission, before the runner is spawned and notification armed."""
    import contextvars

    import gateway.session_context as sc
    import tools.terminal_tool_background as background
    from tools import bot_live_delivery as live
    from tools.process_registry import process_registry

    db = _target_db(bot_world)
    db.create_session("bc-live", source="desktop")
    db.set_session_title("bc-live", "Bot Chat")
    reply = "REPLY_SENTINEL_14"
    owner = _live_owner(bot_world, db, "bc-live", reply)
    if arming == "delayed":
        owner.gate.set()
        real_arm, armed_after_reply = background._apply_async_support, []

        def arm(*a, **k):
            _wait_for(lambda: any((live._read(p) or {}).get("status") == "settled"
                                  for p in live._root(bot_world.target).glob("*.json")), "the owner never settled")
            armed_after_reply.append(True)
            return real_arm(*a, **k)

        monkeypatch.setattr(background, "_apply_async_support", arm)
    route = {"platform": "telegram", "chat_id": "chat-77", "thread_id": "th-9", "user_id": "u-1", "user_name": "op",
             "message_id": "m-5", "session_id": "sess-db-42", "session_key": "gw:chat-77"}
    agent = _bot_sender()
    if arming == "gateway":
        monkeypatch.setattr(sc, "_session_context_engaged", sc._session_context_engaged)

        def in_gateway_turn():
            sc.set_session_vars(**route)
            return _send(agent, "turn-f62g14")

        out = contextvars.copy_context().run(in_gateway_turn)  # the bound vars never leak into later tests
    else:
        out = _send(agent, "turn-f62g14")
    assert out.get("status") == "queued" and out.get("delivery_id") and out.get("reply_delivery") == "notification", out
    proc_id = out["process_id"]
    session = process_registry.get(proc_id)
    assert session.persist_on_release is True, "item 4: the delivery runner must survive the sender's lifecycle sweeps"
    if arming == "delayed":
        assert armed_after_reply == [True], "the target must have replied before notification was armed"
    else:
        assert not session.exited and owner.gate.is_set() is False, "message_agent must not wait for the reply"
    if arming == "gateway":
        mine = [w for w in process_registry.pending_watchers if w.get("session_id") == proc_id]
        assert len(mine) == 1, process_registry.pending_watchers
        w = mine[0]
        assert (w["platform"], w["chat_id"], w["thread_id"], w["user_id"], w["user_name"], w["message_id"]) == (
            "telegram", "chat-77", "th-9", "u-1", "op", "m-5")
        assert w["notify_on_complete"] is True and w["parent_session_id"] == "sess-db-42"
        assert w["session_key"] == session.session_key and w["check_interval"] == 5
        assert session.watcher_platform == "telegram"
    owner.gate.set()
    completions = _completions(process_registry, proc_id)
    assert len(completions) == 1, completions
    assert reply in (completions[0].get("output") or ""), completions[0]
    assert not process_registry.is_completion_consumed(proc_id)


_F62H_MODULE = "tests.plugins.fleet_f62_h_rig"
try:
    import importlib

    _f62h = importlib.import_module(_F62H_MODULE)
    fleet_fixture = _f62h.fleet_fixture  # registers F62.h's `fleet` fixture in this module
except ModuleNotFoundError as exc:
    if exc.name != _F62H_MODULE:
        raise
    _f62h = None


def _redrive(request):
    """F62.h's `fleet`: nv-delivery-redrive enabled, a real AIAgent sender (the root profile), the target `researcher`
    behind a fake CLI, the profile `orchestrator`, core's real runner and turn flock, drainer ticks via the real hook."""
    assert _f62h is not None, "the FLEET-F62.h carry (tests/plugins/fleet_f62_h_rig.py) is not at this head"
    return request.getfixturevalue("fleet")


def _sender_outcomes(world, ack):
    """Everything the sender was told about one delivery: core's completions for its runner, the plugin's notices."""
    session = world.registry.get(ack["process_id"])
    assert session._completion_event.wait(timeout=30), "the delivery runner never finished"
    queue, queued = world.registry.completion_queue, []
    while not queue.empty():
        queued.append(queue.get_nowait())
    for evt in queued:
        queue.put(evt)
    mine = [e for e in queued if e.get("session_id") == ack["process_id"] and e.get("type", "completion") == "completion"]
    return mine, _f62h._notices(world, "default", ack["delivery_id"])


def _busy_past_runner(world):
    """The target's turn outlasts core's runner: the runner's live wait (shortened inside the runner process) ends with
    "Delivery remains pending", so only the drainer is left to report the outcome."""
    (world.state_dir / "runner.json").write_text(
        json.dumps({"_LIVE_WAIT_SECONDS": 0.2, "_LIVE_WAIT_MAX_SECONDS": 0.5}), encoding="utf-8")


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("case", ["queued_at_bound", "delivered_once"])
def test_ac_upg_f68_18(request, case):
    """A local delivery admitted to the FIFO while its target's Bot Chat is busy is never silently dropped: the sender's session and the orchestrator profile each receive exactly one final outcome for its delivery_id: settled with the reply, a typed failure from the receipt, or unknown at the bound followed by at most one late notice."""
    world = _redrive(request)
    if case == "queued_at_bound":
        world.settings(redrive_window_seconds=8)
    _busy_past_runner(world)
    body = f"status? PAYLOAD_F68_18_{case}"
    ack = world.send(body)
    did = ack["delivery_id"]
    assert ack.get("status") == "queued" and ack.get("redrive", {}).get("tracked") is True, ack
    completions, _ = _sender_outcomes(world, ack)
    assert len(completions) == 1 and _outcome_completions(completions) == [], completions   # core's "pending"
    assert world.authority.receipt(did)["status"] == "queued"
    if case == "delivered_once":
        world.authority.settle(did, reply="REPLY-F68-18")
        assert world.until(lambda: world.ticket(did)["state"] == "settled", timeout=60.0), world.ticket(did)
    else:
        assert world.until(lambda: world.ticket(did)["state"] != "pending", timeout=60.0), world.ticket(did)
        t = world.ticket(did)
        assert (t["state"], t["reason"], t["late_open"]) == ("ambiguous", "unknown", 1), t
        assert world.authority.receipt(did)["status"] == "queued"
    assert world.until(lambda: len(_f62h._notices(world, "default", did)) == 1
                       and len(_f62h._notices(world, _f62h.ORCH, did)) == 1, timeout=30.0)
    world.tick(3, pause=0.3)
    for who in ("default", _f62h.ORCH):
        notes = _f62h._notices(world, who, did)
        assert len(notes) == 1 and did in str(notes[0]["content"]), (who, notes)
        if case == "delivered_once":
            assert "REPLY-F68-18" in str(notes[0]["content"]), notes
    if case == "queued_at_bound":                         # at most one late notice follows
        world.authority.settle(did, reply="LATE-F68-18")
        assert world.until(lambda: world.ticket(did)["late_open"] == 0, timeout=30.0), world.ticket(did)
        world.tick(3, pause=0.3)
        for who in ("default", _f62h.ORCH):
            assert len(_f62h._notices(world, who, did)) == 2, who
    assert [r["delivery_id"] for r in _f62h._admissions(world.homes[_f62h.TARGET], body)] == [did]
    assert len(world.rows(_f62h.TARGET, body)) == 1


def _outcome_completions(completions):
    """Core completions that carry a delivery outcome: a typed live result other than "still pending"."""
    found = []
    for evt in completions:
        out = str(evt.get("output") or "")
        try:
            payload = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
        except ValueError:
            payload = {}
        if isinstance(payload, dict) and payload.get("status") not in (None, "queued", "claimed"):
            found.append(evt)
    return found


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("case", ["same_id_once", "receipt_failed_final", "may_still_land"])
def test_ac_upg_f68_20(request, case):
    """The plugin never copies a tracked delivery: it makes no admission, and a same-id replay returns the existing admission. A delivery that settles with a claimed row leaves exactly one copy of the message in the target transcript. The sender receives exactly one final outcome, followed by at most one late notice. A delivery the plugin reports as a final failure is one whose receipt core itself ended `failed` or `cancelled`, and no copy of its message appears in the target transcript after that failure. An attempt that may still land never reaches a failure: at the bound it ends `ambiguous`/`unknown` (AC-FLEET-F62.h-13)."""
    import time

    world = _redrive(request)
    body = f"status? PAYLOAD_F68_20_{case}"
    if case == "may_still_land":
        world.settings(redrive_window_seconds=8)
    if case != "same_id_once":
        _busy_past_runner(world)
    ack = world.send(body)
    did = ack["delivery_id"]
    if case == "same_id_once":
        # Core's runner re-delivers under the same id from its pinned intent: the authority answers with the existing
        # admission, so one receipt stands for both calls.
        assert world.until(lambda: len(world.authority.calls(did=did)) >= 2, ticks=False, timeout=60.0)
        world.authority.settle(did, reply="REPLY-F68-20")
        assert world.until(lambda: world.ticket(did)["state"] == "settled", timeout=60.0), world.ticket(did)
    elif case == "receipt_failed_final":
        _sender_outcomes(world, ack)                      # core's runner is gone ("Delivery remains pending")
        world.authority.fail(did, "provider_rate_limit", "429 from the provider")
        assert world.until(lambda: world.ticket(did)["state"] == "failed", timeout=60.0), world.ticket(did)
        assert world.ticket(did)["reason"] == "provider_rate_limit", world.ticket(did)
        world.tick(4, pause=1.0)
        time.sleep(2.0)  # health: allow HX013 -- absence of a copy has no event to await; the window lets a wrongly scheduled re-send land first
    else:
        assert world.until(lambda: world.ticket(did)["state"] != "pending", timeout=60.0), world.ticket(did)
        t = world.ticket(did)
        assert (t["state"], t["reason"]) == ("ambiguous", "unknown"), t
    world.tick(3, pause=0.3)
    completions, to_sender = _sender_outcomes(world, ack)
    assert len(_outcome_completions(completions)) + len([n for n in to_sender if (
        n.get("display_metadata") or {})["nv_delivery_redrive"]["notice_kind"] == "final"]) == 1, (completions, to_sender)
    assert [r["delivery_id"] for r in _f62h._admissions(world.homes[_f62h.TARGET], body)] == [did]
    assert len(world.authority.tool_admissions(did)) == 1                    # no plugin admission
    assert len(world.rows(_f62h.TARGET, body)) == (1 if case == "same_id_once" else 0)


def _with_redrive(world, monkeypatch):
    """A tracked-delivery world: nv-delivery-redrive enabled with tracking live (a valid
    `orchestrator_profile`, a fresh drainer heartbeat) in this root, and a real AIAgent sender whose `message_agent` runs
    through the real tool executor, so the plugin's middleware and `transform_terminal_output` see the delivery. The
    sender is a push sender (`reply_delivery: "notification"`). Returns `send(task_id) -> ack`."""
    from hermes_cli import plugins as plugins_mod

    assert _f62h is not None, "the FLEET-F62.h carry (tests/plugins/fleet_f62_h_rig.py) is not at this head"
    orch = _f62h._profile(world.root, _f62h.ORCH)
    (world.root / "profile.yaml").write_text("description: sender\nui_meta:\n  hermes-bots:\n    shape: cloud\n",
                                             encoding="utf-8")
    settings = {"redrive_window_seconds": 40, "max_attempts": 6, "retry_backoff_seconds": 1,
                "heartbeat_stale_seconds": 30, "orchestrator_profile": _f62h.ORCH}
    for home in (world.root, orch):
        shutil.copytree(_f62h.PLUGIN_SRC, home / "plugins" / _f62h.PLUGIN_KEY)
        (home / "config.yaml").write_text(hermes_yaml.safe_dump(
            {"plugins": {"enabled": [_f62h.PLUGIN_KEY], "entries": {_f62h.PLUGIN_KEY: {"settings": settings}}}}),
            encoding="utf-8")
    bundled = world.root.parent / "bundled-empty"
    bundled.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    _f62h._seed_bot_chat(world.root, _f62h.SENDER_SID)
    _f62h._seed_bot_chat(orch, "bc-orchestrator")
    plugins_mod._reset_plugin_managers_for_tests()
    world.closers.append(plugins_mod._reset_plugin_managers_for_tests)
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[_f62h.PLUGIN_KEY]
    assert loaded.enabled is True and loaded.error is None, loaded.error
    monkeypatch.setattr(plugins_mod, "_plugin_manager", manager)
    manager.invoke_hook("on_kanban_dispatch_tick", board=None, profile_name="default", dry_run=False, outcome="idle",
                        result=None)
    agent = _f62h._make_sender_agent()

    def send(task_id):
        messages = []
        agent._execute_tool_calls_sequential(_f62h._assistant_turn(f"@{_BOT}", _DM_BODY, f"call-{task_id}"), messages,
                                             task_id)
        content = [m for m in messages if m.get("role") == "tool"][-1]["content"]
        return json.loads(content if isinstance(content, str) else content[0]["text"])

    return send
