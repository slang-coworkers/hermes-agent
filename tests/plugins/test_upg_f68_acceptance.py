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
# v0.22.5: an ancestor of every candidate head, so present in any full clone.
TAG_COMMIT = "bf0203a794a07aac3ff764972ab9087baa4e4f4e"
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
        self.errors = []   # HTTP statuses answered, one per chat request, before any reply
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.requests.append(request)
                if stub.errors and request.get("messages"):
                    raw = json.dumps({"error": {"message": "rate limit exceeded", "type": "rate_limit"}}).encode()
                    self.send_response(stub.errors.pop(0))
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
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

    def chats(self):
        """The chat-completion requests (the daemon also probes the model's metadata)."""
        return [r for r in self.requests if r.get("messages")]

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
    world.authority_owner = lambda reply, **kwargs: _authority_owner(world, monkeypatch, reply, **kwargs)
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


def _authority_owner(world, monkeypatch, reply, *, persist_row=True):
    """The target's session authority as the F62.h rig's file-backed stub at core's T seam (`install_authority`, plus
    the runner shim so core's runner process sees it too): core's own `find_canonical_live_owner` and `_door` pick the
    Bot Chat tip and admit the delivery. The stub's FIFO worker claims each queued receipt, records the session core
    pinned (`sessions`), appends the turn's `role='user'` row there (none with `persist_row` false: a storage failure
    the turn survives, `agent/turn_context.py:509-525`) and settles with `reply` once `gate` is set. While `hold` is
    clear it claims nothing, so the receipt stays `queued`."""
    import time

    from tools import bot_live_delivery as mailbox

    assert _f62h is not None, "the FLEET-F62.h carry (tests/plugins/fleet_f62_h_rig.py) is not at this head"
    state_dir = world.root.parent / "authority-state"
    state_dir.mkdir()
    _f62h.install_authority(state_dir, setattr_=monkeypatch.setattr)
    shim = state_dir / "runner_shim.py"
    shim.write_text(_f62h._RUNNER_SHIM.format(repo=str(REPO), state_dir=str(state_dir)), encoding="utf-8")
    _f62h.install_runner_shim(shim, setattr_=monkeypatch.setattr)
    owner = SimpleNamespace(gate=threading.Event(), hold=threading.Event(), sessions=[], claims=[])
    owner.hold.set()
    db = _target_db(world)

    def claim(path):
        with mailbox._locked(world.target):
            record = mailbox._read(path)
            if record is None or record.get("status") != "queued":
                return None
            record.update(status="claimed", claimed_at=time.time_ns())
            mailbox._write(path, record)
        return record

    def loop():
        while not world.stop.wait(0.05):
            if not owner.hold.is_set():
                continue
            for path in sorted(mailbox._root(world.target).glob("*.json")):
                record = claim(path)
                if record is None:
                    continue
                owner.sessions.append(record["session_id"])
                owner.claims.append(record["message"])
                if persist_row:
                    db.append_message(record["session_id"], "user", content=record["message"])
                while not owner.gate.wait(0.05):
                    if world.stop.is_set():
                        return
                mailbox.complete_delivery(world.target, record["delivery_id"], status="settled", reply=reply)

    threading.Thread(target=loop, daemon=True).start()
    return owner


# Upstream's committed-before-schedule barrier (`tests/gateway/fixtures/local_recovery_daemon.py`), narrowed to one
# delivery: its admission commits and is answered, and is never scheduled in this process.
_HOLD_SCHEDULE = '''import json, runpy, sys
sys.path.insert(0, {repo!r})
from gateway.session_authority import SessionAuthority
from hermes_state_runtime import list_session_admissions
schedule = SessionAuthority._schedule
def hold(self, ref):
    pending = list_session_admissions(self.db, session_id=ref.session_id)
    if pending and {mark!r} in json.dumps(pending[-1].get("payload")):
        return
    return schedule(self, ref)
SessionAuthority._schedule = hold
runpy.run_module("gateway.run", run_name="__main__")
'''


def _real_authority(world, reply):
    """The target profile's own session authority, as core runs it: `python -m gateway.run` under the target's
    HERMES_HOME (the shape of upstream's `tests/gateway/fixtures/local_recovery_probe.py:74-96`), its model the loopback
    stub. The tool and core's runner reach it through real discovery and the real `bot_relay.deliver` RPC.
    `start(hold=<mark>)` runs it under `_HOLD_SCHEDULE`; `kill()` is SIGKILL, the crash boundary."""
    import time

    from hermes_cli.gateway_runtime import discover_gateway_endpoint

    model = _ModelStub(reply)
    world.closers.append(model.close)
    (world.target / "config.yaml").write_text(json.dumps({
        "gateway": {"multiplex_profiles": False},
        "model": {"provider": "custom", "default": "test-model", "base_url": model.url, "api_mode": "chat_completions"},
        "auxiliary": {"title_generation": {"enabled": False}}, "platform_toolsets": {"cli": []},
        "memory": {"memory_enabled": False, "user_profile_enabled": False}, "agent": {"api_max_retries": 1}}),
        encoding="utf-8")
    bundled = world.root.parent / "authority-bundled"
    bundled.mkdir(exist_ok=True)
    env = {k: os.environ[k] for k in ("PATH", "LANG", "TZ") if k in os.environ}
    env.update(HOME=str(world.root.parent / "os-home"), HERMES_HOME=str(world.target), PYTHONPATH=str(REPO),
               HERMES_BUNDLED_PLUGINS=str(bundled), OPENAI_API_KEY="loopback-only", OPENAI_BASE_URL=model.url,
               PYTHONUNBUFFERED="1")
    authority = SimpleNamespace(model=model, proc=None, starts=0)

    def start(hold=None):
        command = [sys.executable, "-m", "gateway.run"]
        if hold is not None:
            launcher = world.root.parent / "hold_schedule.py"
            launcher.write_text(_HOLD_SCHEDULE.format(repo=str(REPO), mark=hold), encoding="utf-8")
            command = [sys.executable, str(launcher)]
        authority.starts += 1
        log = open(world.root.parent / f"authority-{authority.starts}.log", "w+", encoding="utf-8")
        world.closers.append(log.close)
        authority.proc = subprocess.Popen(command, cwd=str(REPO), env=env, stdin=subprocess.DEVNULL, stdout=log,
                                          stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and authority.proc.poll() is None:
            if discover_gateway_endpoint(world.target, timeout=2).state == "ready":
                return
            time.sleep(0.1)
        log.seek(0)
        raise AssertionError(f"the target authority never became ready: {log.read()[-4000:]}")

    def kill():
        if authority.proc is not None and authority.proc.poll() is None:
            authority.proc.kill()
            authority.proc.wait(timeout=30)

    authority.start, authority.kill = start, kill
    world.closers.append(kill)
    return authority


# Core's own runner, signalling once its replay of the pinned intent (`_admit_live_dm`) has returned.
_RUNNER_ADMITTED = '''import sys
from pathlib import Path
sys.path.insert(0, {repo!r})
try:
    import hermes_bootstrap  # noqa: F401  (the runner's own boot step)
except Exception:
    pass
from tools import bot_mode_dm
admit = bot_mode_dm._admit_live_dm
def admitted(*args, **kwargs):
    record = admit(*args, **kwargs)
    Path({mark!r}).write_text("", encoding="utf-8")
    return record
bot_mode_dm._admit_live_dm = admitted
raise SystemExit(bot_mode_dm._delivery_main(sys.argv[1:]))
'''


def _runner_admitted(world, monkeypatch):
    """Start core's runner through `_RUNNER_ADMITTED`; returns the file that appears once it is inside its wait."""
    assert _f62h is not None, "the FLEET-F62.h carry (tests/plugins/fleet_f62_h_rig.py) is not at this head"
    mark = world.root.parent / "runner-admitted"
    shim = world.root.parent / "runner_admitted.py"
    shim.write_text(_RUNNER_ADMITTED.format(repo=str(REPO), mark=str(mark)), encoding="utf-8")
    _f62h.install_runner_shim(shim, setattr_=monkeypatch.setattr)
    return mark


def _admissions_for(world, needle):
    """The target's session-admission rows whose payload holds `needle`: `(request_id, status, outcome)`."""
    import sqlite3

    with contextlib.closing(sqlite3.connect(f"file:{world.target / 'state.db'}?mode=ro", uri=True)) as conn:
        rows = conn.execute("SELECT request_id, status, outcome, payload_json FROM session_admissions ORDER BY seq")
        return [tuple(r[:3]) for r in rows if needle in (r[3] or "")]


def _ac21_real_turn(world):
    from tools.bot_live_delivery import read_delivery_result
    from tools.process_registry import process_registry

    reply = "REPLY_R8_21_real_turn"
    authority = _real_authority(world, reply)
    authority.start()
    db = _target_db(world)
    assert db.get_session_by_title("Bot Chat") is None, "the target must have no Bot Chat before its first delivery"
    out = _send(_bot_sender(), "turn-ac21-real-turn")
    assert out.get("status") == "queued" and out.get("delivery_id"), out
    proc_id, did = out["process_id"], out["delivery_id"]
    _wait_for(authority.model.chats, "the target's turn never reached its model")
    assert not process_registry.get(proc_id).exited and not _queued_completion(process_registry, proc_id), (
        "a success outcome before the target turn finished")
    assert (read_delivery_result(world.target, did) or {}).get("status") != "settled"

    authority.model.gate.set()
    mine = _completions(process_registry, proc_id)
    assert len(mine) == 1 and _settled_completion(mine[0]) and reply in (mine[0].get("output") or ""), mine
    tip = _resumed_bot_chat(db)
    assert tip is not None and tip == read_delivery_result(world.target, did)["session_id"], tip
    users = [m for m in db.get_messages(tip) if m.get("role") == "user"]
    assert [m.get("content") for m in users] == [_delivered()], users


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
    """Arrange the target transcript for one AC-21 shape. Returns (the session the delivery must land in, or None when
    the target has no Bot Chat yet; the sessions that must never receive it)."""
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



def _kill_runner(process_registry, proc_id):
    """An explicit stop of core's runner (`process.kill`); asserts it ended."""
    import logging
    import posix
    import signal

    import psutil

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


def _receipt(world, did):
    from tools.bot_live_delivery import read_delivery_result

    return read_delivery_result(world.target, did) or {}


def _ac19_sender_turn_end(request):
    from tools.process_registry import process_registry

    world = request.getfixturevalue("bot_world")
    db = _target_db(world)
    db.create_session("bc-live", source="cli")
    db.set_session_title("bc-live", "Bot Chat")
    owner = world.authority_owner("REPLY_R8_19")
    owner.hold.clear()
    out = _send(_bot_sender(), "turn-ac19")
    assert out.get("status") == "queued" and out.get("delivery_id"), out
    proc_id, did = out["process_id"], out["delivery_id"]
    for source in ("agent_close", "gateway_turn_timeout", "kill_all"):
        process_registry.kill_all("turn-ac19", source=source)
        assert process_registry.get(proc_id).exited is False, f"the runner died in the {source} sweep"
    assert _receipt(world, did)["status"] == "queued", _receipt(world, did)
    assert not _queued_completion(process_registry, proc_id) and _full_rows(db, "bc-live") == [], (
        "a success completion or a target row before target acceptance")
    _kill_runner(process_registry, proc_id)
    assert _receipt(world, did)["status"] == "queued", "the durable owner must still hold the delivery"
    owner.hold.set()
    owner.gate.set()
    _wait_for(lambda: _receipt(world, did).get("status") == "settled", "the released owner never settled it")
    assert owner.sessions == ["bc-live"] and len(_full_rows(db, "bc-live")) == 1, db.get_messages("bc-live")


def _ac19_no_authority(request):
    from hermes_state import SessionDB
    from tools import bot_live_delivery as mailbox
    from tools import bot_mode_dm
    from tools.process_registry import process_registry

    world = request.getfixturevalue("bot_world")
    assert _f62h is not None, "the FLEET-F62.h carry (tests/plugins/fleet_f62_h_rig.py) is not at this head"
    _f62h._seed_bot_chat(world.root, _f62h.SENDER_SID)
    agent = _f62h._make_sender_agent()
    agent._session_db = SessionDB(db_path=world.root / "state.db")
    world.closers.append(agent._session_db.close)
    agent._session_db_created = True
    messages = []
    agent._execute_tool_calls_sequential(_f62h._assistant_turn(f"@{_BOT}", _DM_BODY, "call-ac19-none"), messages,
                                         "turn-ac19-none")
    agent._flush_messages_to_session_db(messages)
    rows = [m for m in agent._session_db.get_messages(_f62h.SENDER_SID) if m.get("role") == "tool"]
    assert len(rows) == 1, rows
    result = json.loads(rows[0]["content"])
    assert result.get("reason") == "runtime_unavailable" and result.get("delivery_id"), result
    assert "the message was NOT sent" in result.get("error", ""), result
    assert not [s for s in process_registry.list_sessions(task_id="turn-ac19-none") if "--run-delivery" in str(s)]
    assert not list(mailbox._root(world.target).glob("*.json")), "a receipt without an authority"
    assert not [p for p in bot_mode_dm._dm_dir().iterdir() if p.is_file()], "the payload or its intent was left behind"
    assert not (world.target / "state.db").exists(), "a target transcript row without an authority"


def _ac19_restart_before_accept(request):
    """The target authority admits and answers, then dies before it schedules the turn."""
    from tools.process_registry import process_registry

    world = request.getfixturevalue("bot_world")
    reply = "REPLY_R8_19_recovered"
    authority = _real_authority(world, reply)
    admitted_mark = _runner_admitted(world, request.getfixturevalue("monkeypatch"))
    authority.model.gate.set()
    authority.start(hold="TAIL_R8")
    out = _send(_bot_sender(), "turn-ac19-restart")
    assert out.get("status") == "queued" and out.get("delivery_id"), out
    proc_id, did = out["process_id"], out["delivery_id"]
    admitted = _receipt(world, did)
    assert admitted.get("status") == "queued" and admitted.get("admission_id"), admitted
    assert [a[1] for a in _admissions_for(world, "TAIL_R8")] == ["queued"]
    assert not authority.model.chats() and not _queued_completion(process_registry, proc_id)
    _wait_for(admitted_mark.exists, "core's runner never reached its wait")
    authority.kill()
    authority.start()
    mine = _completions(process_registry, proc_id)
    assert len(mine) == 1 and _settled_completion(mine[0]) and reply in (mine[0].get("output") or ""), mine
    final = _receipt(world, did)
    assert (final.get("status"), final.get("delivery_id"), final.get("admission_id")) == (
        "settled", did, admitted["admission_id"]), final
    assert [a[1] for a in _admissions_for(world, "TAIL_R8")] == ["terminal"]
    db = _target_db(world)
    assert len(_full_rows(db, _resumed_bot_chat(db))) == 1


def _ac19_restart_mid_turn(request):
    """The target authority dies inside the delivery's turn: never re-executed, and its `ambiguous` stays last."""
    from tools.process_registry import process_registry

    world = request.getfixturevalue("bot_world")
    authority = _real_authority(world, "REPLY_R8_19_never")
    admitted_mark = _runner_admitted(world, request.getfixturevalue("monkeypatch"))
    authority.start()
    out = _send(_bot_sender(), "turn-ac19-midturn")
    assert out.get("status") == "queued" and out.get("delivery_id"), out
    proc_id, did = out["process_id"], out["delivery_id"]
    _wait_for(authority.model.chats, "the delivery's turn never reached its model")
    _wait_for(admitted_mark.exists, "core's runner never reached its wait")
    assert not _queued_completion(process_registry, proc_id)
    authority.kill()
    authority.start()
    mine = _completions(process_registry, proc_id)
    assert len(mine) == 1 and mine[0].get("exit_code") == 1, mine
    payload = json.loads((mine[0].get("output") or "").strip().splitlines()[-1])
    assert (payload.get("status"), payload.get("delivery_id")) == ("ambiguous", did), payload
    assert "Do not resend" in payload.get("detail", ""), payload
    first = _admissions_for(world, "TAIL_R8")
    authority.model.gate.set()
    authority.kill()
    authority.start()
    assert _receipt(world, did).get("status") == "ambiguous", _receipt(world, did)
    assert _admissions_for(world, "TAIL_R8") == first and first[0][2] != "completed", (first, _admissions_for(world, "TAIL_R8"))
    assert sum("TAIL_R8" in json.dumps(r["messages"]) for r in authority.model.chats()) == 1


def _ac19_transient_retry(request):
    """The delivery's turn fails transiently (429), and core's one derived retry (`bot:<id>:retry`) answers it."""
    from agent.turn_failure_copy import FAILED_TURN_DISPLAY_KIND
    from tools.process_registry import process_registry

    world = request.getfixturevalue("bot_world")
    reply = "REPLY_R8_19_retry"
    authority = _real_authority(world, reply)
    authority.model.errors[:] = [429]
    authority.model.gate.set()
    authority.start()
    out = _send(_bot_sender(), "turn-ac19-retry")
    assert out.get("status") == "queued" and out.get("delivery_id"), out
    proc_id, did = out["process_id"], out["delivery_id"]
    mine = _completions(process_registry, proc_id)
    assert len(mine) == 1 and _settled_completion(mine[0]) and reply in (mine[0].get("output") or ""), mine
    final = _receipt(world, did)
    assert final.get("status") == "settled" and final.get("retry_admission_id"), final
    admissions = _admissions_for(world, "TAIL_R8")
    assert admissions == [(f"bot:{did}", "terminal", "failed"), (f"bot:{did}:retry", "terminal", "completed")], admissions
    db = _target_db(world)
    messages = db.get_messages(_resumed_bot_chat(db))
    full = [i for i, m in enumerate(messages) if _is_full(m)]
    assert len(full) == 2, messages
    failed_turn, retry_turn = messages[full[0] + 1:full[1]], messages[full[1] + 1:]
    assert [m.get("role") for m in failed_turn if m.get("display_kind") == FAILED_TURN_DISPLAY_KIND] == ["assistant"]
    assert not [m for m in failed_turn if m.get("role") == "assistant" and reply in str(m.get("content"))], failed_turn
    assert [m.get("content") for m in retry_turn if m.get("role") == "assistant"] == [reply], retry_turn


def _ac19_notes(world, did, kind=None):
    """The plugin's notices for `did` to the sender and to the orchestrator, optionally of one `notice_kind`."""
    def of(who):
        notes = _f62h._notices(world, who, did)
        return [n for n in notes if kind is None or n["display_metadata"]["nv_delivery_redrive"]["notice_kind"] == kind]
    return of("default"), of(_f62h.ORCH)


def _ac19_tracked_send(world, body):
    ack = world.send(body)
    did = ack["delivery_id"]
    assert ack.get("redrive", {}).get("tracked") is True and world.ticket(did)["path"] == "live", ack
    return ack, did


def _ac19_kill_core_runner(world, ack):
    """Kill core's runner once it is inside its own wait (it has replayed the pinned intent from its own pid)."""
    did = ack["delivery_id"]
    assert world.until(lambda: [c for c in world.authority.calls(did=did) if c["pid"] != os.getpid()], ticks=False,
                       timeout=60.0), world.authority.calls(did=did)
    world.registry.kill_process(ack["process_id"], consume_output=True)
    _f62h._wait_runner_done(world, ack["process_id"])
    world.tick(2, pause=0.3)
    assert world.ticket(did)["state"] == "pending" and world.authority.receipt(did)["status"] == "queued"


def _ac19_assert_untouched(world, did, body, rows):
    assert [r["delivery_id"] for r in _f62h._admissions(world.homes[_f62h.TARGET], body)] == [did]
    assert len(world.authority.tool_admissions(did)) == 1
    assert len(world.rows(_f62h.TARGET, body)) == rows


def _ac19_tracked_runner_killed(request):
    world = _redrive(request)
    ack, did = _ac19_tracked_send(world, "status? PAYLOAD_F68_19_killed")
    _ac19_kill_core_runner(world, ack)
    world.authority.settle(did, reply="REPLY-F68-19-KILLED")
    assert world.until(lambda: world.ticket(did)["state"] == "settled"), world.ticket(did)
    assert world.until(lambda: [len(n) for n in _ac19_notes(world, did)] == [1, 1], timeout=30.0)
    world.tick(3, pause=0.3)
    for notes in _ac19_notes(world, did):
        assert len(notes) == 1 and did in str(notes[0]["content"]) and "REPLY-F68-19-KILLED" in str(notes[0]["content"])
    _ac19_assert_untouched(world, did, "PAYLOAD_F68_19_killed", rows=1)


def _ac19_tracked_runner_killed_unknown(request):
    world = _redrive(request)
    world.settings(redrive_window_seconds=8)
    ack, did = _ac19_tracked_send(world, "status? PAYLOAD_F68_19_unknown")
    _ac19_kill_core_runner(world, ack)
    assert world.until(lambda: world.ticket(did)["state"] != "pending", timeout=60.0), world.ticket(did)
    t = world.ticket(did)
    assert (t["state"], t["reason"], t["late_open"]) == ("ambiguous", "unknown", 1), t
    assert world.until(lambda: [len(n) for n in _ac19_notes(world, did, "final")] == [1, 1], timeout=30.0)
    assert world.authority.receipt(did)["status"] == "queued", "an admitted receipt is never cancelled"
    world.authority.settle(did, reply="LATE-F68-19")
    assert world.until(lambda: world.ticket(did)["late_open"] == 0, timeout=30.0), world.ticket(did)
    assert world.until(lambda: [len(n) for n in _ac19_notes(world, did, "late")] == [1, 1], timeout=30.0)
    world.tick(4, pause=0.5)
    assert [len(n) for n in _ac19_notes(world, did)] == [2, 2], _ac19_notes(world, did)
    _ac19_assert_untouched(world, did, "PAYLOAD_F68_19_unknown", rows=1)


def _ac19_unpersisted_tracked(request, *, late_row=False):
    world = _redrive(request)
    ack, did = _ac19_tracked_send(world, "status? PAYLOAD_F68_19_unpersisted")
    world.authority.claim(did)
    world.authority.settle(did, reply="UNPERSISTED-F68-19", row_first=False)
    _f62h._wait_runner_done(world, ack["process_id"], timeout=40.0)
    assert world.until(lambda: world.registry.get(ack["process_id"])._completion_event.is_set(), ticks=False)
    out = _f62h._completion_output(world, ack["process_id"])
    assert json.loads(out.strip().splitlines()[-1]).get("status") == "ambiguous" and "Do NOT resend" in out, out
    assert '"status": "settled"' not in out and "UNPERSISTED-F68-19" not in out, out
    assert world.until(lambda: world.ticket(did)["state"] != "pending", timeout=60.0), world.ticket(did)
    assert (world.ticket(did)["state"], world.ticket(did)["reason"]) == ("ambiguous", "unknown"), world.ticket(did)
    assert world.until(lambda: [len(n) for n in _ac19_notes(world, did, "final")] == [1, 1], timeout=30.0)
    if late_row:
        world.authority.row(did)                     # the owner's row lands after the FINAL: one VERIFIED, then final
        assert world.until(lambda: [len(n) for n in _ac19_notes(world, did, "verified")] == [1, 1], timeout=30.0)
    world.tick(4, pause=0.5)
    assert [len(n) for n in _ac19_notes(world, did)] == ([2, 2] if late_row else [1, 1]), _ac19_notes(world, did)
    _ac19_assert_untouched(world, did, "PAYLOAD_F68_19_unpersisted", rows=1 if late_row else 0)


def _ac19_unpersisted_non_push(request):
    """A tracked sender that cannot receive completions: core's raw `settled` row (waived, D-2), then the plugin's one
    correction notice (required)."""
    import contextvars

    from gateway.session_context import declare_stateless_channel
    from hermes_state import SessionDB

    world = _redrive(request)
    world.agent = _f62h._make_sender_agent()
    world.agent._session_db = SessionDB(db_path=world.root / "state.db")   # arms core's durable row (bot_mode_dm.py:739)

    def stateless_send():
        declare_stateless_channel()
        return _ac19_tracked_send(world, "status? PAYLOAD_F68_19_nonpush")

    ack, did = contextvars.copy_context().run(stateless_send)
    world.authority.claim(did)
    world.authority.settle(did, reply="UNPERSISTED-F68-19", row_first=False)
    _f62h._wait_runner_done(world, ack["process_id"], timeout=40.0)
    assert world.until(lambda: world.ticket(did)["state"] != "pending", timeout=60.0), world.ticket(did)
    assert world.until(lambda: [len(n) for n in _ac19_notes(world, did, "final")] == [1, 1], timeout=30.0)
    world.tick(3, pause=0.5)
    assert [len(n) for n in _ac19_notes(world, did)] == [1, 1], _ac19_notes(world, did)
    assert "Do NOT resend" in str(_ac19_notes(world, did)[0][0]["content"])
    _ac19_assert_untouched(world, did, "PAYLOAD_F68_19_nonpush", rows=0)


_AC19 = {"sender_turn_end": _ac19_sender_turn_end, "no_authority": _ac19_no_authority,
         "restart_before_accept": _ac19_restart_before_accept, "restart_mid_turn": _ac19_restart_mid_turn,
         "transient_retry": _ac19_transient_retry, "tracked_runner_killed": _ac19_tracked_runner_killed,
         "tracked_runner_killed_unknown": _ac19_tracked_runner_killed_unknown,
         "unpersisted_tracked": _ac19_unpersisted_tracked,
         "unpersisted_late_row": lambda request: _ac19_unpersisted_tracked(request, late_row=True),
         "unpersisted_non_push": _ac19_unpersisted_non_push}
_AC19_RIG = ("tracked_runner_killed", "tracked_runner_killed_unknown", "unpersisted_tracked", "unpersisted_late_row",
             "unpersisted_non_push")


@pytest.mark.parametrize("case", [pytest.param(c, marks=pytest.mark.platforms("linux" if c in _AC19_RIG else "posix"))
                                  for c in _AC19])
def test_ac_upg_f68_19(request, case):
    """(R4 re-spec of AC-FLEET-F62.g-11) A pending local delivery survives its sender process and a killed runner, because a durable owner holds it. If the target exits before accepting, exits 0 without persisting, or its runner is killed, the delivery ends in a re-attempt on the same `delivery_id` or in one typed final failure in the sender's session. Before target acceptance, no success completion and no target transcript row exist. If a re-attempt then succeeds, exactly one target row and one final outcome exist.

    Rulings applied (msg 596; E4′ §E4.6):
    - D-2: "waive -19 (b) on exactly these paths: E7.1a's three paths plus the raw row a tracked non-push sender sees.
      F62.h-17's correction notice stays required."
    - D-3: "a final `ambiguous` counts as the one typed final failure only when it really is the last outcome."
    - D-4: "waive exactly upstream's retry path (#107070). The failed row stays closed, and the retry's row is the one
      answered row."
    - D-5 (§E4.6): the untracked/stale-sender path is waived: an untracked sender gets no final outcome in its session
      once its runner is dead or has hit core's 30-minute wait. A tracked sender's final outcome stays required.

    At v0.22.5 the durable owner is the target profile's session authority. `sender_turn_end`: the rig's stub authority
    holds the receipt `queued` through the sender's lifecycle sweeps and an explicit kill of core's runner, with no
    success and no row, then settles it with one row. `no_authority`: the target authority is down, so the sender's own
    persisted tool row is the one typed final failure. `restart_before_accept`: the real authority admits and answers,
    dies before scheduling the turn, and after a restart runs the same admission once. `restart_mid_turn`: it dies
    inside the turn, which is never re-executed, and its `ambiguous` survives a second restart. `transient_retry`: a 429
    fails the turn and core's one `bot:<id>:retry` answers it (D-4). The rig cases are tracked senders: core's runner
    killed (the plugin's FINAL with the reply, or `unknown` at the bound and then one late notice); an owner that
    settles without its row (one `ambiguous`/`unknown` FINAL that stays last, or one VERIFIED once the row lands); and
    a non-push sender's one correction notice."""
    _AC19[case](request)


_AC21_SHAPES = ("fresh", "continuation", "compressed", "duplicate", "prefix_collision")


@pytest.mark.platforms("posix")
@pytest.mark.parametrize(("shape", "path"), [
    *((shape, "live_owner") for shape in _AC21_SHAPES),
    ("fresh", "real_turn"),
    ("fresh", "live_owner_unpersisted"),
])
def test_ac_upg_f68_21(bot_world, shape, path):
    """(R4 re-spec of AC-FLEET-F62.g-13) A delivery's success outcome is reported only once the target's resumed Bot Chat holds a `role='user'` row with the entire delivered attributed content, newer than the delivery's baseline. The resumed Bot Chat is the latest continuation, taken at its compression tip. An earlier identical message never yields that outcome, and neither does a newer row holding only a prefix (cases: fresh / continuation / compressed / duplicate / prefix_collision).

    Waived (E7.1a, carried unchanged by ruling D-1, msg 596): the three core-only paths (non-push raw settled, hook
    fail-open, untracked/stale senders). At v0.22.5 every local delivery goes through the target profile's session
    authority (`bot_relay.deliver`), and the success outcome is core's runner's one exit-0 `settled` completion carrying
    the reply. `live_owner`: the authority is the F62.h rig's stub at core's seam, so core's own `find_canonical_live_owner`
    picks the session the owner's row lands in; `fresh` there is an existing Bot Chat with no rows. `real_turn`: the
    target's own `python -m gateway.run` authority and a loopback model; the target has no Bot Chat, so core creates it
    for this first delivery and its turn persists the row. `live_owner_unpersisted`: the owner settles and its user row
    never lands (both turn-start and turn-end persists log and continue, `agent/turn_context.py:509-525`); this node's
    world loads nv-delivery-redrive with tracking live and sends through the real tool executor, a push sender, so the
    plugin's gate is what keeps core's `settled` from reaching the sender."""
    from tools.process_registry import process_registry

    if path == "real_turn":
        _ac21_real_turn(bot_world)
        return
    db = _target_db(bot_world)
    landing, never = _seed_shape(db, shape)
    if landing is None:
        db.create_session("bc-fresh", source="cli")
        db.set_session_title("bc-fresh", "Bot Chat")
        landing = "bc-fresh"
    baseline = len(_full_rows(db, landing))
    reply = f"REPLY_R8_21_{shape}"
    owner = bot_world.authority_owner(reply, persist_row=path == "live_owner")
    if path == "live_owner_unpersisted":
        out = bot_world.with_redrive()(f"turn-ac21-{shape}-{path}")
    else:
        out = _send(_bot_sender(), f"turn-ac21-{shape}-{path}")
    assert out.get("status") == "queued" and out.get("delivery_id"), out
    proc_id = out["process_id"]
    if path == "live_owner_unpersisted":
        from tools.bot_live_delivery import read_delivery_result

        assert out.get("redrive", {}).get("tracked") is True, out
        owner.gate.set()
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
    _wait_for(lambda: len(_full_rows(db, landing)) > baseline, "the owner's turn never persisted the delivered row")
    assert not process_registry.get(proc_id).exited and not _queued_completion(process_registry, proc_id), (
        "a success outcome before the target turn finished")

    owner.gate.set()
    mine = _completions(process_registry, proc_id)
    assert len(mine) == 1 and _settled_completion(mine[0]) and reply in (mine[0].get("output") or ""), mine
    assert owner.sessions == [landing] and owner.claims == [_delivered()], (owner.sessions, owner.claims)
    assert _resumed_bot_chat(db) == landing, (_resumed_bot_chat(db), landing)
    assert len(_full_rows(db, landing)) == baseline + 1, db.get_messages(landing)
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
