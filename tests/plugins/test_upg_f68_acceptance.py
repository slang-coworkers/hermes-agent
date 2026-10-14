"""UPG-F68 acceptance: re-pin the fork to Hermes v0.22.0 (tag commit e06df9c2), carrying UPG-F67's #79 forward.

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
# The selected tag v0.22.0 (ADR header); an ancestor of every candidate head, so present in any full clone.
TAG_COMMIT = "e06df9c2eae6901f4c88f464e0ae6e753c0428b8"
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
                          stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, f"load probe failed: {proc.stderr[-2000:]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_ac_upg_f68_1(tmp_path, no_yaml_dir):
    """(derived, msg 8 (e)(i)) Each of the 8 fork plugins (`hello`, `nv-approval-ledger`, `nv-artifact`, `nv-cost-cap`, `nv-coworker-compose`, `nv-fleet-gates`, `nv-workflow-modes`, `podman-onecli`), enabled alone in an isolated `HERMES_HOME`, is enabled with no load error, in a child interpreter whose `yaml` import is blocked (the base-install closure has no PyYAML)."""
    blocked = subprocess.run([sys.executable, "-c", "import yaml"], capture_output=True, text=True,
                             env=_child_env(tmp_path, tmp_path, no_yaml_dir), timeout=60)
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
        env=_child_env(tmp_path, home, no_yaml_dir), stdin=subprocess.DEVNULL)
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
                hermes_yaml.safe_load((rendered / rel).read_text(encoding="utf-8"))


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
                          stdin=subprocess.DEVNULL)
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


def _child_pytest(tmp_path, *args, timeout=1800, extra_path=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env["HOME"] = str(tmp_path / "pytest-home")
    if extra_path:
        env["PYTHONPATH"] = os.pathsep.join([str(extra_path), *filter(None, [env.get("PYTHONPATH")])])
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=", *args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(REPO),
                          env=env, timeout=timeout, stdin=subprocess.DEVNULL)


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
                           capture_output=True, text=True)
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
        "LEGACY = %r\n"
        "def pytest_collection_modifyitems(session, config, items):\n"
        "    hits = [f'{i.nodeid}: {m}' for i in items for m in LEGACY if i.get_closest_marker(m)]\n"
        "    if hits:\n"
        "        raise SystemExit('LEGACY-MARK ' + '; '.join(hits[:20]))\n" % (LEGACY_MARKS,), encoding="utf-8")
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
    now = datetime.datetime.now(datetime.timezone.utc)
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
    proc = subprocess.run([sys.executable, "-c", _HANDSHAKE, json.dumps(urls)], capture_output=True, text=True,
                          encoding="utf-8", env=env, cwd=str(tmp_path), timeout=120, stdin=subprocess.DEVNULL)
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
                                  text=True, encoding="utf-8", env=env, timeout=120, stdin=subprocess.DEVNULL)
            assert proc.returncode == 0, f"{name}: {proc.stderr[-1500:]}"
            results[name] = json.loads(proc.stdout.strip().splitlines()[-1])["ok"]
    assert results == {"SSL_CERT_FILE": True, "REQUESTS_CA_BUNDLE": False, "HERMES_CA_BUNDLE": False}, results


_EGRESS_ENV = {
    "HTTPS_PROXY": "http://egress-proxy.invalid:3128", "HTTP_PROXY": "http://egress-proxy.invalid:3128",
    "NO_PROXY": "localhost,127.0.0.1", "SSL_CERT_FILE": "/etc/ssl/certs/fleet-ca.pem",
    "REQUESTS_CA_BUNDLE": "/etc/ssl/certs/fleet-ca.pem", "NODE_EXTRA_CA_CERTS": "/etc/ssl/certs/fleet-ca.pem",
}


def test_ac_upg_f68_15(tmp_path, monkeypatch):
    """(derived; adopt-proof for AC-LOOP-F39-6) A bare `deliver: bot-chat` cron delivery fired from a multiplexed tick bound to a served profile spawns its turn with `HERMES_HOME` = that profile's home and no `-p`, and the child env keeps the proxy and CA variables."""
    import cron.scheduler_delivery as delivery
    from agent.secret_scope import set_multiplex_active
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    for key, value in _EGRESS_ENV.items():
        monkeypatch.setenv(key, value)
    root = tmp_path / "hermes-root"
    home = root / "profiles" / "orchestrator"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("HERMES_HOME", str(root))

    spawned = []

    def record_turn(argv, env, report_path, timeout):
        spawned.append((list(argv), dict(env)))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(delivery, "_run_bot_chat_turn", record_turn)
    set_multiplex_active(True)
    bound = set_hermes_home_override(str(home))
    try:
        delivery._deliver_to_bot_chat({"id": "job-1", "name": "supervise-issues"}, "brief", "")
    finally:
        reset_hermes_home_override(bound)
        set_multiplex_active(False)

    assert len(spawned) == 1, f"exactly one Bot Chat turn must be spawned, got {len(spawned)}"
    argv, env = spawned[0]
    assert Path(env["HERMES_HOME"]).resolve() == home.resolve(), f"child HERMES_HOME: {env.get('HERMES_HOME')}"
    assert "-p" not in argv and "--profile" not in argv, f"a served profile's own delivery takes no -p: {argv}"
    assert {k: env.get(k) for k in _EGRESS_ENV} == _EGRESS_ENV, "the child must keep the proxy and CA variables"
