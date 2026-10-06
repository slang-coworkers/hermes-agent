"""Hermetic acceptance tests for FLEET-F62.e — the sandbox CA survives a sandbox restart (E2E-13), and NO_PROXY never
lists the docker bridge (ships to ``tests/plugins/test_fleet_f62e_acceptance.py``).

One ``test_ac_fleet_f62_e_<n>`` per ``pytest:`` row of the ADR's ``## Acceptance criteria`` (AC-1..AC-12, AC-14..AC-17, in id order); each
docstring summarizes its criterion. AC-13 is a ``live:`` scenario file, not here.

Shape: render rows drive the real ``hermes coworker compose`` subprocess under an isolated ``HERMES_HOME`` with the fleet
plugins copied in and an EMPTY bundled dir (the FLEET-F62.c/d harness). Start-time rows load nv-coworker-compose through
``PluginManager().discover_and_load()`` and drive the release orchestrator
``agent.secret_sources.registry.apply_all(cfg, home, environ=...)`` — the call ``load_hermes_dotenv()`` makes at every
process start. A "process start" is one ``apply_all`` on a fresh env; a "re-mint" rewrites the live bundle with a newly
minted throwaway CA between two starts. Installer rows import ``openshell/installer.py`` by path.

Behaviour contract, not a snapshot: no network, no socket, nothing under ``~/.hermes``, no plugin source is read. Every
certificate is a throwaway self-signed CA minted in ``tmp_path`` by the pinned ``cryptography``. Wording rule: no
credential value, header or variable name appears here.
"""

import copy
import datetime
import importlib.util
import os
import re
import shutil
import ssl
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGINS_SRC = REPO_ROOT / "plugins"
COMPOSE_KEY = "nv-coworker-compose"
GATES_KEY = "nv-fleet-gates"
ONECLI_KEY = "podman-onecli"
FLEET_PLUGINS = (COMPOSE_KEY, GATES_KEY)
OPENSHELL_DIR = PLUGINS_SRC / COMPOSE_KEY / "openshell"
SCENARIOS = REPO_ROOT / "tests" / "e2e-scenarios"
F62C_SPEC = SCENARIOS / "FLEET-F62.c" / "spec" / "openshell" / "coworker-types.yaml"
OSH_F63_SPEC = SCENARIOS / "OSH-F63" / "spec" / "openshell" / "coworker-types.yaml"
OSH_F64_SPEC = SCENARIOS / "OSH-F64" / "spec" / "coworker-types.yaml"
OSH_F64B_SPEC = SCENARIOS / "OSH-F64.b" / "spec" / "coworker-types.yaml"
RUNBOOK = REPO_ROOT / "website" / "docs" / "user-guide" / "fleet-openshell.md"

SOURCE_NAME = "openshell_trust"
LIVE_BUNDLE = "/etc/openshell-tls/ca-bundle.pem"
LANE_COPY = "/etc/osh-lane/ca-bundle.pem"
LANE_ONECLI_CA = "/etc/osh-lane/onecli-ca.pem"
CA_ENV_NAMES = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "HERMES_CA_BUNDLE",
                "DENO_CERT")
NO_PROXY_NAMES = ("NO_PROXY", "no_proxy")
KEPT_ENTRIES = ("localhost", "127.0.0.1", "::1", "10.200.0.1", ".svc.cluster.local")
SHA = "a" * 40


def _mint_ca(common_name):
    """A throwaway self-signed CA, PEM text. Minted per test in memory; nothing real is ever loaded."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


def _pem_blocks(text):
    return re.findall(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", text, re.S)


def _trusted_cns(ctx):
    """Common names of the CAs an ssl.SSLContext trusts."""
    assert isinstance(ctx, ssl.SSLContext), f"resolver returned {ctx!r}, not an SSLContext"
    names = set()
    for cert in ctx.get_ca_certs():
        for rdn in cert.get("subject", ()):
            for key, value in rdn:
                if key == "commonName":
                    names.add(value)
    return names


def _isolated_home(tmp_path, plugins=FLEET_PLUGINS):
    home = tmp_path / "home" / ".hermes"
    (home / "plugins").mkdir(parents=True)
    for key in plugins:
        src = PLUGINS_SRC / key
        if src.is_dir():
            shutil.copytree(src, home / "plugins" / key)
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": list(plugins)}}), encoding="utf-8")
    (tmp_path / "empty-bundled").mkdir(exist_ok=True)
    return home


def _compose(spec, out, home, *extra):
    env = dict(os.environ)
    env.update(HOME=str(home.parent), HERMES_HOME=str(home),
               HERMES_BUNDLED_PLUGINS=str(home.parent.parent / "empty-bundled"), HERMES_ENABLE_PROJECT_PLUGINS="0")
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "coworker", "compose", str(spec), "--out", str(out), *extra],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT), stdin=subprocess.DEVNULL,
    )


def _spec_copy(tmp_path, spec, mutate=None, tag="spec"):
    """Copy a committed spec tree into tmp and apply ``mutate(data)`` to its coworker-types.yaml."""
    dst = tmp_path / tag
    shutil.copytree(spec.parent, dst)
    path = dst / spec.name
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if mutate:
        mutate(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _with_trust(**extra):
    def _mutate(data):
        data.setdefault("egress", {})["openshell_trust"] = {"bundle": LIVE_BUNDLE, **extra}
    return _mutate


def _render(tmp_path, spec, tag="out"):
    home = _isolated_home(tmp_path / f"h-{tag}")
    out = tmp_path / tag
    proc = _compose(spec, out, home)
    assert proc.returncode == 0, f"compose failed for {spec}: {proc.stderr[-2000:]}"
    return out


def _profile_configs(out):
    """{profile_dir_name: parsed config.yaml} for every rendered profile (managed fragment excluded)."""
    return {p.parent.name: (yaml.safe_load(p.read_text(encoding="utf-8")) or {})
            for p in sorted(out.glob("*/config.yaml")) if p.parent.name != "managed"}


def _strip_trust(cfg):
    """A config with the FLEET-F62.e keys removed (``secrets.openshell_trust`` and its ``secrets.sources`` entry)."""
    out = copy.deepcopy(cfg)
    secrets = out.get("secrets")
    if isinstance(secrets, dict):
        secrets.pop(SOURCE_NAME, None)
        sources = secrets.get("sources")
        if isinstance(sources, list):
            rest = [s for s in sources if s != SOURCE_NAME]
            if rest:
                secrets["sources"] = rest
            else:
                secrets.pop("sources", None)
        if not secrets:
            out.pop("secrets", None)
    return out


def _drop_onecli_ca_bundle(cfg):
    out = copy.deepcopy(cfg)
    settings = ((((out.get("plugins") or {}).get("entries") or {}).get(ONECLI_KEY) or {}).get("settings"))
    if isinstance(settings, dict):
        settings.pop("ca_bundle", None)
    return out


def _tree(out):
    return sorted(str(p.relative_to(out)) for p in out.rglob("*") if p.is_file() and "__pycache__" not in p.parts)


def test_ac_fleet_f62_e_1(tmp_path):
    """The fleet spec of record declares the live OpenShell bundle, and its render gives every served profile and the default profile a `secrets.openshell_trust` block (enabled, `override_existing: true`, `bundle` = `/etc/openshell-tls/ca-bundle.pem`) with `openshell_trust` first in `secrets.sources`. No rendered file names the built-once lane bundle `/etc/osh-lane/ca-bundle.pem`."""
    spec = yaml.safe_load(F62C_SPEC.read_text(encoding="utf-8")) or {}
    trust = (spec.get("egress") or {}).get("openshell_trust")
    assert isinstance(trust, dict) and trust.get("bundle") == LIVE_BUNDLE, \
        f"the spec of record must declare egress.openshell_trust.bundle = {LIVE_BUNDLE}, got {trust!r}"
    out = _render(tmp_path, F62C_SPEC)
    cfgs = _profile_configs(out)
    assert len(cfgs) == 6, f"expected the DEFAULT + five coworker configs, got {sorted(cfgs)}"
    for name, cfg in cfgs.items():
        secrets = cfg.get("secrets") or {}
        block = secrets.get(SOURCE_NAME)
        assert isinstance(block, dict), f"{name}: no secrets.{SOURCE_NAME} block"
        assert block.get("enabled") is True, f"{name}: the trust source must be enabled"
        assert block.get("override_existing") is True, f"{name}: the trust source must override a stale .env/shell value"
        assert block.get("bundle") == LIVE_BUNDLE, f"{name}: bundle must be the live OpenShell bundle, got {block.get('bundle')!r}"
        sources = secrets.get("sources")
        assert isinstance(sources, list) and sources[:1] == [SOURCE_NAME], \
            f"{name}: {SOURCE_NAME} must be first in secrets.sources (first claim wins), got {sources!r}"
    for path in out.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            assert LANE_COPY not in path.read_text(encoding="utf-8", errors="replace"), \
                f"{path.relative_to(out)} names the built-once lane bundle"


@pytest.mark.parametrize("spec", [OSH_F63_SPEC, OSH_F64_SPEC, OSH_F64B_SPEC], ids=["osh-f63", "osh-f64", "osh-f64b"])
def test_ac_fleet_f62_e_2(tmp_path, spec):
    """(derived, default-off) A spec without `egress.openshell_trust` renders no trust block anywhere. The same spec with the key renders the block, and is otherwise identical once the block and its `secrets.sources` entry are removed: every `config.yaml` compared as parsed YAML, every other file and the provision plan byte for byte. On the legacy OSH-F64 spec the one further delta is the dropped `podman-onecli` `ca_bundle` (AC-7). This holds for the OSH-F63, OSH-F64 (legacy §D7) and OSH-F64.b (single-authority) specs."""
    assert spec.is_file(), f"committed spec missing: {spec}"
    plain_spec = _spec_copy(tmp_path, spec, tag="plain-spec")
    keyed_spec = _spec_copy(tmp_path, spec, _with_trust(), tag="keyed-spec")
    home = _isolated_home(tmp_path / "h-render")
    plain, keyed = tmp_path / "plain", tmp_path / "keyed"
    for s, o in ((plain_spec, plain), (keyed_spec, keyed)):
        proc = _compose(s, o, home)
        assert proc.returncode == 0, f"compose failed for {s}: {proc.stderr[-2000:]}"
    for path in plain.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            assert SOURCE_NAME not in path.read_text(encoding="utf-8", errors="replace"), \
                f"default-off: {path.relative_to(plain)} mentions {SOURCE_NAME} with no key declared"
    keyed_cfgs = _profile_configs(keyed)
    assert keyed_cfgs and all(isinstance((c.get("secrets") or {}).get(SOURCE_NAME), dict) for c in keyed_cfgs.values()), \
        "with the key, every rendered profile must carry the trust block"
    assert _tree(plain) == _tree(keyed), "the key must add or remove no rendered file"
    for rel in _tree(plain):
        a, b = plain / rel, keyed / rel
        if rel.endswith("config.yaml") and not rel.startswith("managed"):
            got = _strip_trust(yaml.safe_load(b.read_text(encoding="utf-8")) or {})
            want = _strip_trust(yaml.safe_load(a.read_text(encoding="utf-8")) or {})
            if spec == OSH_F64_SPEC:
                got, want = _drop_onecli_ca_bundle(got), _drop_onecli_ca_bundle(want)
            assert got == want, f"{rel}: the key changed more than the trust block"
        else:
            assert a.read_bytes() == b.read_bytes(), f"{rel}: the key changed a non-config file"
    home = _isolated_home(tmp_path / "h-plan")
    plans = [_compose(s, tmp_path / f"plan-{i}", home, "--provision-dry-run") for i, s in enumerate((plain_spec, keyed_spec))]
    assert all(p.returncode == 0 for p in plans), [p.stderr[-800:] for p in plans]
    assert plans[0].stdout == plans[1].stdout, "the key must not change the provision plan"


_BAD_TRUST = {
    "not-a-mapping": "/etc/openshell-tls/ca-bundle.pem",
    "missing-bundle": {"merge": [LANE_ONECLI_CA]},
    "relative-bundle": {"bundle": "etc/openshell-tls/ca-bundle.pem"},
    "glob-bundle": {"bundle": "/etc/openshell-tls/*.pem"},
    "dotdot-bundle": {"bundle": "/etc/openshell-tls/../shadow"},
    "control-char": {"bundle": "/etc/openshell-tls/ca-bundle.pem\n"},
    "merge-not-list": {"bundle": LIVE_BUNDLE, "merge": LANE_ONECLI_CA},
    "merge-relative": {"bundle": LIVE_BUNDLE, "merge": ["osh-lane/onecli-ca.pem"]},
    "unknown-key": {"bundle": LIVE_BUNDLE, "copy_to": "/tmp/ca.pem"},
}


@pytest.mark.parametrize("case", sorted(_BAD_TRUST) + ["compose-not-enabled"])
def test_ac_fleet_f62_e_3(tmp_path, case):
    """(derived) A malformed `egress.openshell_trust` fails the render non-zero before any profile is written. Malformed means: a non-mapping, a missing or relative `bundle`, a glob, `..`, a control character, a non-list or non-absolute `merge`, or an unknown key. It also fails when a profile carrying the block does not enable `nv-coworker-compose`."""
    def _mutate(data):
        if case == "compose-not-enabled":
            data.setdefault("egress", {})["openshell_trust"] = {"bundle": LIVE_BUNDLE}
            role = next(iter(data["types"]))
            data["types"][role].setdefault("config", {})["plugins"] = {"enabled": [GATES_KEY]}
        else:
            data.setdefault("egress", {})["openshell_trust"] = _BAD_TRUST[case]
    spec = _spec_copy(tmp_path, F62C_SPEC, _mutate)
    home = _isolated_home(tmp_path / "h")
    out = tmp_path / "out"
    proc = _compose(spec, out, home)
    assert proc.returncode != 0, f"{case}: a malformed openshell_trust must fail the render"
    assert "openshell_trust" in (proc.stderr + proc.stdout), f"{case}: the error must name the key: {proc.stderr[-600:]}"
    assert not list(out.glob("*/config.yaml")), f"{case}: no profile may be written before the refusal"


def _trust_cfg(bundle, merge=None):
    block = {"enabled": True, "override_existing": True, "bundle": str(bundle)}
    if merge:
        block["merge"] = [str(m) for m in merge]
    return {"sources": [SOURCE_NAME], SOURCE_NAME: block}


def _load_trust_home(tmp_path, monkeypatch, secrets_cfg, extra_plugins=(), settings=None):
    """Isolated HERMES_HOME with nv-coworker-compose (+ extras) copied in and enabled, loaded through the real discovery
    path. Returns ``(home, manager)``. No ``secrets:`` block is written to disk (the OSH-F64 ``_load_onecli`` rule), so
    discovery's re-pull hydrates nothing into the test process; each start passes ``secrets_cfg`` to ``apply_all`` exactly
    as ``load_hermes_dotenv()`` would read it from the rendered ``config.yaml``."""
    for name in (*CA_ENV_NAMES, *NO_PROXY_NAMES):
        monkeypatch.delenv(name, raising=False)
    plugins = (COMPOSE_KEY, *extra_plugins)
    home = tmp_path / "start-home"
    (home / "plugins").mkdir(parents=True)
    for key in plugins:
        src = PLUGINS_SRC / key
        if not (src / "plugin.yaml").is_file():
            pytest.fail(f"plugins/{key} not present")
        shutil.copytree(src, home / "plugins" / key)
    cfg = {"plugins": {"enabled": list(plugins)}}
    if settings:
        cfg["plugins"]["entries"] = {k: {"settings": v} for k, v in settings.items()}
    (home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    bundled = tmp_path / "start-bundled"
    bundled.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    from hermes_cli.plugins import PluginManager

    manager = PluginManager()
    manager.discover_and_load()
    for key in plugins:
        loaded = manager._plugins[key]
        assert loaded.enabled is True and loaded.error is None and loaded.module is not None, f"{key} did not load"
    from agent.secret_sources import registry
    from hermes_constants import hermes_home_key

    assert registry.get_source(SOURCE_NAME, scope=hermes_home_key(home)) is not None, \
        f"{COMPOSE_KEY} must register the {SOURCE_NAME} secret source"
    return home, manager


def _start(home, secrets_cfg, environ):
    """One Hermes process start: the release orchestrator applied to ``environ`` (mutated in place)."""
    from agent.secret_sources import registry
    from hermes_cli.env_loader import reset_secret_source_cache

    reset_secret_source_cache()
    return registry.apply_all(secrets_cfg, home, environ=environ)


def _source_report(report):
    hits = [s for s in report.sources if s.name == SOURCE_NAME]
    assert len(hits) == 1, f"{SOURCE_NAME} must run exactly once per start, got {[s.name for s in report.sources]}"
    return hits[0]


def _resolver_trust(monkeypatch, environ):
    """What the release TLS resolver trusts for a process whose env is ``environ``."""
    from agent import ssl_verify

    for name in CA_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    for name in CA_ENV_NAMES:
        if name in environ:
            monkeypatch.setenv(name, environ[name])
    return _trusted_cns(ssl_verify.resolve_httpx_verify())


@pytest.fixture
def lane(tmp_path):
    """A fake lane: a stale copied bundle (CA-A, from a persisted .env) and the live OpenShell bundle (CA-B)."""
    tls = tmp_path / "etc" / "openshell-tls"
    tls.mkdir(parents=True)
    stale = tmp_path / "persisted" / "copied-ca-bundle.pem"
    stale.parent.mkdir(parents=True)
    stale.write_text(_mint_ca("fleet-f62e-CA-A-stale"), encoding="utf-8")
    live = tls / "ca-bundle.pem"
    live.write_text(_mint_ca("fleet-f62e-CA-B"), encoding="utf-8")
    return {"stale": stale, "live": live}


def _restarted_env(stale):
    """A fresh process env as a restart sees it: every CA name still pointing at the stale copy from a persisted .env."""
    return {**{name: str(stale) for name in CA_ENV_NAMES}, "PATH": os.environ.get("PATH", "")}


def test_ac_fleet_f62_e_4(tmp_path, monkeypatch, lane):
    """Across two process starts with the live bundle re-minted between them, the registered `openshell_trust` source points all six CA env names at the live bundle and overrides a stale copied path seeded as a pre-existing value. After the second start, the release TLS resolver trusts the new CA and not the old one."""
    cfg = _trust_cfg(lane["live"])
    home, _ = _load_trust_home(tmp_path, monkeypatch, cfg)

    env1 = _restarted_env(lane["stale"])
    sr1 = _source_report(_start(home, cfg, env1))
    for name in CA_ENV_NAMES:
        assert env1[name] == str(lane["live"]), f"start 1: {name} must point at the live bundle, got {env1[name]!r}"
        assert name in sr1.applied, f"start 1: {name} must be applied by {SOURCE_NAME} (override of the stale copy)"
    assert _resolver_trust(monkeypatch, env1) >= {"fleet-f62e-CA-B"}

    lane["live"].write_text(_mint_ca("fleet-f62e-CA-C"), encoding="utf-8")
    env2 = _restarted_env(lane["stale"])
    _source_report(_start(home, cfg, env2))
    for name in CA_ENV_NAMES:
        assert env2[name] == str(lane["live"]), f"start 2: {name} must point at the live bundle, got {env2[name]!r}"
    trusted = _resolver_trust(monkeypatch, env2)
    assert "fleet-f62e-CA-C" in trusted, f"after the re-mint the resolver must trust the new CA, got {sorted(trusted)}"
    assert not trusted & {"fleet-f62e-CA-B", "fleet-f62e-CA-A-stale"}, \
        f"after the re-mint the resolver must not trust a superseded CA, got {sorted(trusted)}"


def test_ac_fleet_f62_e_5(tmp_path, monkeypatch, lane):
    """With a `merge` list, each start writes a derived bundle under the plugin's data dir. Its content is the live bundle followed by every merge PEM, and all six CA env names point at it. A second start after a re-mint replaces the derived file atomically with the new live CA plus the same merge CAs."""
    onecli_ca = tmp_path / "etc" / "osh-lane" / "onecli-ca.pem"
    onecli_ca.parent.mkdir(parents=True)
    onecli_ca.write_text(_mint_ca("fleet-f62e-ONECLI"), encoding="utf-8")
    cfg = _trust_cfg(lane["live"], merge=[onecli_ca])
    home, _ = _load_trust_home(tmp_path, monkeypatch, cfg)
    data_dir = home / "plugin-data" / COMPOSE_KEY

    env1 = _restarted_env(lane["stale"])
    _source_report(_start(home, cfg, env1))
    derived = Path(env1["SSL_CERT_FILE"])
    assert data_dir in derived.parents, f"the derived bundle must live under {data_dir}, got {derived}"
    assert all(env1[name] == str(derived) for name in CA_ENV_NAMES), "all six CA names must name the derived bundle"
    live_b = _pem_blocks(lane["live"].read_text(encoding="utf-8"))
    merged = _pem_blocks(onecli_ca.read_text(encoding="utf-8"))
    assert _pem_blocks(derived.read_text(encoding="utf-8")) == live_b + merged, \
        "the derived bundle must be the live bundle followed by the merge PEMs, in order"
    assert _resolver_trust(monkeypatch, env1) >= {"fleet-f62e-CA-B", "fleet-f62e-ONECLI"}

    lane["live"].write_text(_mint_ca("fleet-f62e-CA-C"), encoding="utf-8")
    env2 = _restarted_env(lane["stale"])
    _source_report(_start(home, cfg, env2))
    assert env2["SSL_CERT_FILE"] == str(derived), "the derived path is stable across starts"
    assert _pem_blocks(derived.read_text(encoding="utf-8")) == \
        _pem_blocks(lane["live"].read_text(encoding="utf-8")) + merged, "start 2 must re-derive from the re-minted bundle"
    trusted = _resolver_trust(monkeypatch, env2)
    assert {"fleet-f62e-CA-C", "fleet-f62e-ONECLI"} <= trusted and "fleet-f62e-CA-B" not in trusted, sorted(trusted)
    leftovers = [p.name for p in derived.parent.iterdir() if p != derived]
    assert not leftovers, f"the atomic replace must leave no temp file beside the derived bundle: {leftovers}"


@pytest.mark.parametrize("state", ["absent", "empty"])
def test_ac_fleet_f62_e_6(tmp_path, monkeypatch, lane, state):
    """A missing or empty live bundle fails closed for trust. The source sets no CA name and writes no derived file, but it still applies the NO_PROXY strip. The startup report carries a warning that names the bundle path."""
    if state == "absent":
        lane["live"].unlink()
    else:
        lane["live"].write_text("", encoding="utf-8")
    cfg = _trust_cfg(lane["live"], merge=[lane["stale"]])
    home, _ = _load_trust_home(tmp_path, monkeypatch, cfg)
    env = {**_restarted_env(lane["stale"]), "NO_PROXY": "localhost,172.17.0.1"}
    sr = _source_report(_start(home, cfg, env))
    assert not set(sr.applied) & set(CA_ENV_NAMES), f"no CA name may be set from a {state} live bundle: {sr.applied}"
    assert all(env[name] == str(lane["stale"]) for name in CA_ENV_NAMES), "the source must not rewrite any CA name"
    assert env["NO_PROXY"] == "localhost", "the NO_PROXY strip still applies when trust fails closed"
    notes = " ".join([*(sr.result.warnings or []), sr.result.error or ""])
    assert str(lane["live"]) in notes, f"the startup report must name the {state} bundle path, got {notes!r}"
    data_dir = home / "plugin-data" / COMPOSE_KEY
    assert not data_dir.exists() or not any(p.is_file() for p in data_dir.rglob("*")), \
        "no derived bundle may be written from a missing or empty live bundle"


def test_ac_fleet_f62_e_7(tmp_path, monkeypatch, lane):
    """On the legacy chain-dial lane, a spec that declares the key renders no `podman-onecli` `ca_bundle` (the built-once lane bundle is no longer forced), keeps `proxy_rewrite`, and orders `openshell_trust` before `onecli`. A start with both sources active takes every CA name from `openshell_trust`."""
    spec = _spec_copy(tmp_path, OSH_F64_SPEC, _with_trust(merge=[LANE_ONECLI_CA]))
    out = _render(tmp_path, spec)
    served = {n: c for n, c in _profile_configs(out).items()
              if (c.get("secrets") or {}).get("onecli", {}).get("override_existing") is True}
    assert served, "the legacy lane must still render the chain-dial on its served coworkers"
    for name, cfg in served.items():
        settings = (((cfg.get("plugins") or {}).get("entries") or {}).get(ONECLI_KEY) or {}).get("settings") or {}
        assert "ca_bundle" not in settings, f"{name}: the built-once lane bundle must no longer be forced ({settings})"
        assert settings.get("proxy_rewrite"), f"{name}: the chain-dial proxy_rewrite must be kept"
        secrets = cfg.get("secrets") or {}
        assert (secrets.get(SOURCE_NAME) or {}).get("merge") == [LANE_ONECLI_CA], f"{name}: merge list not rendered"
        order = secrets.get("sources") or []
        assert SOURCE_NAME in order and order.index(SOURCE_NAME) == 0, f"{name}: {SOURCE_NAME} must be first: {order}"

    cfg = {**_trust_cfg(lane["live"]), "sources": [SOURCE_NAME, "onecli"],
           "onecli": {"enabled": True, "override_existing": True}}
    home, manager = _load_trust_home(tmp_path, monkeypatch, cfg, extra_plugins=(ONECLI_KEY,),
                                     settings={ONECLI_KEY: {"proxy_rewrite": "127.0.0.1:18255", "ca_bundle": LANE_COPY}})
    oneclient = manager._plugins[ONECLI_KEY].module.secret_source.oneclient
    monkeypatch.setattr(oneclient, "get_container_config",
                        lambda *, agent: {"env": {"HTTPS_PROXY": "http://onecli.invalid:10255"}})
    env = _restarted_env(lane["stale"])
    report = _start(home, cfg, env)
    for name in CA_ENV_NAMES:
        prov = report.provenance.get(name)
        assert prov is not None and prov.source == SOURCE_NAME, \
            f"{name}: must be claimed by {SOURCE_NAME}, got {getattr(prov, 'source', None)!r}"
        assert env[name] == str(lane["live"]), f"{name}: must point at the live bundle, not the lane copy"


def test_ac_fleet_f62_e_8(tmp_path, monkeypatch, lane):
    """At each start the source claims both `NO_PROXY` and `no_proxy`: it removes every docker-bridge entry (every token the release matcher would use to bypass the proxy for `172.17.0.1` or `host.docker.internal`: the literal with or without a port, a CIDR containing it, `*`, `.172.17.0.1`, `*.docker.internal`) from the inherited value and keeps every other entry in order, or sets the loopback default `127.0.0.1,localhost,::1` when none is inherited or nothing survives the strip. The release proxy matcher then routes `172.17.0.1:18777` through the proxy and still bypasses loopback. A later source that returns a bridge-listing NO_PROXY (OneCLI plain mode) cannot replace the claim."""
    cfg = _trust_cfg(lane["live"])
    home, _ = _load_trust_home(tmp_path, monkeypatch, cfg)
    mixed = ",".join(["localhost", "172.17.0.1", "127.0.0.1", "host.docker.internal", "::1", "172.17.0.0/16", "*",
                      "10.200.0.1", "HOST.DOCKER.INTERNAL.", ".172.17.0.1", ".svc.cluster.local", "*.docker.internal",
                      "172.17.0.1:18777", ".docker.internal", "172.17.0.1:10256", "host.docker.internal:10256"])
    env = {**_restarted_env(lane["stale"]), "NO_PROXY": mixed, "no_proxy": mixed.replace(",", ", ")}
    sr = _source_report(_start(home, cfg, env))
    for name in NO_PROXY_NAMES:
        kept = [e.strip() for e in env[name].split(",") if e.strip()]
        assert kept == list(KEPT_ENTRIES), f"{name}: bridge entries must be removed and the rest kept in order, got {kept}"
        assert name in sr.applied, f"{name}: the cleaned value must be applied by {SOURCE_NAME}"
    from gateway.platforms import base as gw_base

    monkeypatch.setenv("NO_PROXY", env["NO_PROXY"])
    monkeypatch.setenv("no_proxy", env["no_proxy"])
    for hop in ("172.17.0.1:18777", "172.17.0.1:18255", "172.17.0.1", "host.docker.internal"):
        assert gw_base.should_bypass_proxy([hop]) is False, f"{hop} must go through the proxy"
        assert gw_base.is_host_excluded_by_no_proxy(hop.split(":")[0]) is False, f"{hop} excluded by the suffix matcher"
    assert gw_base.should_bypass_proxy(["127.0.0.1"]) is True, "loopback must still bypass the proxy"
    only_bridge = {**_restarted_env(lane["stale"]), "NO_PROXY": "*,172.17.0.1", "no_proxy": ".docker.internal"}
    _source_report(_start(home, cfg, only_bridge))
    for name in NO_PROXY_NAMES:
        assert only_bridge[name] == "127.0.0.1,localhost,::1", f"{name}: an all-bridge value must fall back to loopback"

    absent = _restarted_env(lane["stale"])
    _source_report(_start(home, cfg, absent))
    for name in NO_PROXY_NAMES:
        assert absent.get(name) == "127.0.0.1,localhost,::1", f"{name}: an absent value must get the loopback default"

    # OneCLI plain mode: a later source returning a bridge-listing NO_PROXY cannot replace the first claim.
    both = {**_trust_cfg(lane["live"]), "sources": [SOURCE_NAME, "onecli"],
            "onecli": {"enabled": True, "override_existing": True}}
    home2, manager = _load_trust_home(tmp_path / "with-onecli", monkeypatch, both, extra_plugins=(ONECLI_KEY,))
    oneclient = manager._plugins[ONECLI_KEY].module.secret_source.oneclient
    monkeypatch.setattr(oneclient, "get_container_config", lambda *, agent: {"env": {
        "HTTPS_PROXY": "http://onecli.invalid:10255", "NO_PROXY": "localhost,172.17.0.1", "no_proxy": "172.17.0.1"}})
    env = _restarted_env(lane["stale"])
    report = _start(home2, both, env)
    for name in NO_PROXY_NAMES:
        assert "172.17.0.1" not in env.get(name, ""), f"{name}: OneCLI re-planted a bridge entry: {env.get(name)!r}"
        prov = report.provenance.get(name)
        assert prov is not None and prov.source == SOURCE_NAME, f"{name}: must be claimed by {SOURCE_NAME} first"


def _inject_no_proxy(where, name, value):
    def _mutate(data):
        if where == "type":
            data["types"]["fixer"].setdefault("config", {})[name] = value
        else:
            data.setdefault("default_config", {})[name] = value
    return _mutate


def _spec_with_no_proxy(tmp_path, where, name, value):
    spec = _spec_copy(tmp_path, F62C_SPEC, None if where == "spine" else _inject_no_proxy(where, name, value))
    if where == "spine":
        data = yaml.safe_load(spec.read_text(encoding="utf-8"))
        spine = spec.parent / data["spines"]["base"]["source"]
        sdoc = yaml.safe_load(spine.read_text(encoding="utf-8")) or {}
        sdoc.setdefault("config", {})[name] = value
        spine.write_text(yaml.safe_dump(sdoc, sort_keys=False), encoding="utf-8")
    return spec


@pytest.mark.parametrize("entry", ["172.17.0.1", "*", ".172.17.0.1", "*.docker.internal", "172.17.0.1:10256",
                                   "host.docker.internal:10256", "172.17.0.0/16", "host.docker.internal"])
@pytest.mark.parametrize("where", ["spine", "type", "default"])
@pytest.mark.parametrize("name", NO_PROXY_NAMES)
def test_ac_fleet_f62_e_9(tmp_path, where, name, entry):
    """An openshell render refuses a top-level `NO_PROXY` or `no_proxy` config scalar that lists a docker-bridge entry (as AC-8 defines it, `*` and suffix forms included), wherever it comes from (spine, type `config`, `default_config`). It names the profile and the entry. A loopback-only value renders."""
    bad = _spec_with_no_proxy(tmp_path / "bad", where, name, f"localhost,127.0.0.1,{entry}")
    home = _isolated_home(tmp_path / "h")
    out = tmp_path / "out-bad"
    proc = _compose(bad, out, home)
    assert proc.returncode != 0, f"{where}/{name}: a docker-bridge NO_PROXY entry must fail the render"
    text = proc.stderr + proc.stdout
    assert entry in text, f"{where}/{name}: the error must name the entry {entry!r}: {text[-600:]}"
    profile = {"type": "fixer", "default": "default"}.get(where)
    if profile:
        assert profile in text, f"{where}/{name}: the error must name the profile {profile!r}: {text[-600:]}"
    assert not list(out.glob("*/config.yaml")), f"{where}/{name}: no profile may be written before the refusal"
    ok = _spec_with_no_proxy(tmp_path / "ok", where, name, "127.0.0.1,localhost,::1")
    assert _compose(ok, tmp_path / "out-ok", home).returncode == 0, f"{where}/{name}: a loopback-only value must render"


def _installer(tag):
    path = OPENSHELL_DIR / "installer.py"
    if not path.is_file():
        pytest.fail(f"installer missing at {path}")
    spec = importlib.util.spec_from_file_location(tag, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def inst_env(tmp_path, monkeypatch):
    """Isolated HERMES_HOME / HERMES_MANAGED_DIR / HOME for the installer (never the real home)."""
    home = tmp_path / "ihome"
    (home / "plugins").mkdir(parents=True)
    managed = tmp_path / "managed"
    managed.mkdir()
    user = tmp_path / "user"
    user.mkdir()
    monkeypatch.setenv("HOME", str(user))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    return home, managed, user


def test_ac_fleet_f62_e_10(tmp_path, inst_env):
    """The installer's in-place default-home edit carries the trust block for a spec that declares it, so the multiplexer process derives at its own start. An unrelated `secrets.*` key survives. A second plan is empty, and a spec without the key plans no `secrets.openshell_trust` edit."""
    inst = _installer("fleet_f62e_inst_ac10")
    existing = {"secrets": {"preserve_existing": ["OPERATOR_KEEP"], "sources": ["operator_vault"]},
                "operator_top_level": {"survives": True}}
    _, diff, _ = inst.default_config_diff(copy.deepcopy(existing), str(F62C_SPEC))
    block = diff.get(f"secrets.{SOURCE_NAME}")
    assert isinstance(block, dict) and block.get("bundle") == LIVE_BUNDLE and block.get("enabled") is True \
        and block.get("override_existing") is True, f"the default edit must carry the trust block, got {block!r}"
    applied = inst.apply_config_diff(copy.deepcopy(existing), diff)
    secrets = applied.get("secrets") or {}
    assert secrets.get("sources", [])[:1] == [SOURCE_NAME], f"{SOURCE_NAME} must lead secrets.sources: {secrets}"
    assert "operator_vault" in secrets.get("sources", []), "an operator-listed source must survive"
    assert secrets.get("preserve_existing") == ["OPERATOR_KEEP"], "an unrelated secrets key must survive"
    assert applied.get("operator_top_level") == {"survives": True}
    _, second, _ = inst.default_config_diff(copy.deepcopy(applied), str(F62C_SPEC))
    assert second == {}, f"a second default-home edit must be empty: {second}"

    keyless = _spec_copy(tmp_path, F62C_SPEC, lambda d: d["egress"].pop("openshell_trust", None), tag="keyless")
    _, diff_keyless, _ = inst.default_config_diff(copy.deepcopy(existing), str(keyless))
    assert not any(k.startswith(f"secrets.{SOURCE_NAME}") or k == "secrets.sources" for k in diff_keyless), \
        f"a spec without the key must plan no trust edit: {sorted(diff_keyless)}"


class _CP:
    def __init__(self, stdout=""):
        self.stdout, self.stderr, self.returncode = stdout, "", 0


def test_ac_fleet_f62_e_11(inst_env, monkeypatch):
    """The installer's `hermes gateway restart` child gets `NO_PROXY` and `no_proxy` with the docker-bridge entries removed and every other entry kept. The rest of its env is unchanged apart from the existing refused names."""
    inst = _installer("fleet_f62e_inst_ac11")
    spec = yaml.safe_load(F62C_SPEC.read_text(encoding="utf-8"))
    mixed = "localhost,172.17.0.1,127.0.0.1,host.docker.internal,::1,172.17.0.0/16,10.200.0.1"
    monkeypatch.setenv("NO_PROXY", mixed)
    monkeypatch.setenv("no_proxy", mixed)
    monkeypatch.setenv("FLEET_F62E_UNRELATED", "kept-verbatim")
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, *a, **kw: calls.append(("run", list(cmd), kw)) or _CP())

    class _Popen:
        def __init__(self, cmd, *a, **kw):
            calls.append(("popen", list(cmd), kw))
            self.pid, self.returncode = 4242, None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(subprocess, "Popen", _Popen)
    import time

    monkeypatch.setattr(time, "sleep", lambda _s: None)  # a detached-restart status poll must not slow the test
    restart = next(s for s in inst.build_plan({}, str(F62C_SPEC), SHA) if getattr(s, "tag", None) == "restart")
    inst._execute_step(restart, inst._apply_ctx(spec, str(F62C_SPEC)))
    launches = [c for c in calls if "gateway" in c[1] and ("restart" in c[1] or "--replace" in c[1])]
    assert launches, f"no gateway restart launch captured: {[c[1] for c in calls]}"
    env = launches[0][2].get("env")
    assert env is not None, "the restart child must get an explicit env"
    for name in NO_PROXY_NAMES:
        kept = [e.strip() for e in env.get(name, "").split(",") if e.strip()]
        assert kept == ["localhost", "127.0.0.1", "::1", "10.200.0.1"], f"{name} in the restart env: {kept}"
    assert env.get("FLEET_F62E_UNRELATED") == "kept-verbatim", "unrelated names must be copied unchanged"


def test_ac_fleet_f62_e_12():
    """The OpenShell runbook's precondition 1 tells the operator to declare `egress.openshell_trust` with the live bundle path, and states that the trust env is re-derived at every Hermes start, so a sandbox restart needs no edit. The same section states that NO_PROXY never lists `172.17.0.1`."""
    text = RUNBOOK.read_text(encoding="utf-8")
    m = re.search(r"(?ms)^### Live-lane preconditions for a served turn\s*$(.*?)(?=^#{2,3} |\Z)", text)
    assert m, "fleet-openshell.md must keep the Live-lane preconditions section"
    section = m.group(1)
    item1 = re.search(r"(?ms)^1\.\s+(.*?)(?=^\d+\.\s|\Z)", section)
    assert item1, "the section must keep a numbered precondition 1"
    body = " ".join(item1.group(1).split())
    assert "egress.openshell_trust" in body and LIVE_BUNDLE in body, f"item 1 must name the key and the live path: {body}"
    assert re.search(r"(?i)re-?derive", body) and re.search(r"(?i)restart", body), \
        f"item 1 must say the trust env is re-derived at start and survives a restart: {body}"
    sentences = re.split(r"(?<=[.!?])\s+", " ".join(section.split()))
    assert any("NO_PROXY" in s and "172.17.0.1" in s for s in sentences), \
        "one sentence of the section must state NO_PROXY never lists 172.17.0.1"


@pytest.mark.parametrize("where", ["type", "default"])
@pytest.mark.parametrize("var", ["SSL_CERT_FILE", " HERMES_CA_BUNDLE ", "NO_PROXY", "no_proxy"])
def test_ac_fleet_f62_e_14(tmp_path, where, var):
    """(derived) The render refuses a profile whose `secrets.preserve_existing` lists a CA env name or a NO_PROXY spelling while `egress.openshell_trust` is declared, because a preserved value outranks the trust source. It names the profile and the variable."""
    def _mutate(data):
        _with_trust()(data)
        block = data["types"]["fixer"].setdefault("config", {}) if where == "type" else data.setdefault("default_config", {})
        block.setdefault("secrets", {})["preserve_existing"] = [var]
    spec = _spec_copy(tmp_path, F62C_SPEC, _mutate)
    home = _isolated_home(tmp_path / "h")
    out = tmp_path / "out"
    proc = _compose(spec, out, home)
    text = proc.stderr + proc.stdout
    assert proc.returncode != 0, f"{where}/{var!r}: a preserved trust/NO_PROXY name must fail the render"
    assert var.strip() in text and ("fixer" if where == "type" else "default") in text, \
        f"{where}/{var!r}: the error must name the profile and the variable: {text[-600:]}"
    assert not list(out.glob("*/config.yaml")), "no profile may be written before the refusal"


def _cli(home, *argv):
    env = dict(os.environ)
    env.update(HOME=str(home.parent), HERMES_HOME=str(home),
               HERMES_BUNDLED_PLUGINS=str(home.parent.parent / "empty-bundled"), HERMES_ENABLE_PROJECT_PLUGINS="0")
    return subprocess.run([sys.executable, "-m", "hermes_cli.main", "coworker", *argv], capture_output=True, text=True,
                          env=env, cwd=str(REPO_ROOT), stdin=subprocess.DEVNULL)


def test_ac_fleet_f62_e_15(tmp_path):
    """(derived) `hermes coworker install-trust <spec>` merges the rendered trust settings into the active gateway home of a non-lane openshell spec (the F62.c fleet of record, which `install-openshell` refuses). It leaves every unrelated key unchanged, and a second run is a no-op."""
    home = _isolated_home(tmp_path)
    cfg_path = home / "config.yaml"
    seeded = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    seeded["operator_top_level"] = {"survives": True}
    seeded["secrets"] = {"sources": ["operator_vault"]}
    cfg_path.write_text(yaml.safe_dump(seeded), encoding="utf-8")
    first = _cli(home, "install-trust", str(F62C_SPEC))
    assert first.returncode == 0, f"install-trust failed on the F62.c spec: {first.stdout[-400:]} {first.stderr[-800:]}"
    after = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    block = (after.get("secrets") or {}).get(SOURCE_NAME) or {}
    assert block.get("bundle") == LIVE_BUNDLE and block.get("enabled") is True and block.get("override_existing") is True
    assert after["secrets"]["sources"][:1] == [SOURCE_NAME] and "operator_vault" in after["secrets"]["sources"]
    assert after.get("operator_top_level") == {"survives": True}, "an unrelated key must survive"
    assert after.get("plugins") == seeded.get("plugins"), "install-trust must not edit plugins.*"
    snapshot = cfg_path.read_bytes()
    second = _cli(home, "install-trust", str(F62C_SPEC))
    assert second.returncode == 0 and cfg_path.read_bytes() == snapshot, "a second run must be a byte-identical no-op"
    keyless = _spec_copy(tmp_path, F62C_SPEC, lambda d: d["egress"].pop("openshell_trust", None), tag="keyless")
    refused = _cli(home, "install-trust", str(keyless))
    assert refused.returncode != 0 and "openshell_trust" in (refused.stdout + refused.stderr), \
        "a spec without egress.openshell_trust must be refused, naming the key"


@pytest.mark.parametrize("line", ["SSL_CERT_FILE=/persisted/copied-ca.pem", "CURL_CA_BUNDLE=/persisted/copied-ca.pem",
                                  "NO_PROXY=localhost,172.17.0.1", "no_proxy=host.docker.internal",
                                  "NO_PROXY=localhost,172.17.0.1:10256"])
def test_ac_fleet_f62_e_16(tmp_path, monkeypatch, line):
    """(derived) `install-trust` and `install-openshell` refuse, before any edit, a managed-scope `.env` that sets a CA env name or a NO_PROXY spelling listing a docker-bridge entry, because the managed `.env` is applied after every secret source. They name the managed file and the variable. The NO_PROXY refusal also applies to a keyless openshell spec."""
    var = line.split("=", 1)[0]
    home = _isolated_home(tmp_path)
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / ".env").write_text(line + "\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    before = (home / "config.yaml").read_bytes()
    proc = _cli(home, "install-trust", str(F62C_SPEC))
    text = proc.stdout + proc.stderr
    assert proc.returncode != 0, f"{var}: install-trust must refuse a managed .env that outranks the trust source"
    assert ".env" in text and var in text, f"{var}: the refusal must name the managed file and the variable: {text[-600:]}"
    assert "/persisted/copied-ca.pem" not in text, "the refusal must name the variable, never echo its value"
    assert (home / "config.yaml").read_bytes() == before, "install-trust must edit nothing before refusing"

    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    lane_spec = _spec_copy(tmp_path, F62C_SPEC, lambda d: d["egress"].update(pinned_offline_lane=True), tag="lane")
    with pytest.raises(Exception) as exc:
        _installer("fleet_f62e_inst_ac16").build_plan({}, str(lane_spec), SHA)
    assert var in str(exc.value), f"build_plan must refuse naming {var}: {exc.value!r}"

    if var.lower() == "no_proxy":
        keyless_lane = _spec_copy(tmp_path, F62C_SPEC, lambda d: (d["egress"].pop("openshell_trust", None),
                                                                  d["egress"].update(pinned_offline_lane=True)), tag="keyless-lane")
        with pytest.raises(Exception) as kexc:
            _installer("fleet_f62e_inst_ac16k").build_plan({}, str(keyless_lane), SHA)
        assert var in str(kexc.value), f"a keyless openshell install must also refuse a bridge {var}: {kexc.value!r}"

    (managed / ".env").write_text("UNRELATED_SETTING=1\n", encoding="utf-8")
    ok = _cli(home, "install-trust", str(F62C_SPEC))
    assert ok.returncode == 0, f"a managed .env with neither must pass: {ok.stderr[-600:]}"


def _discover_home(tmp_path, monkeypatch, cfg):
    """An isolated home holding ``cfg`` as its config.yaml with nv-coworker-compose copied in, then real discovery."""
    home = tmp_path / "disc-home"
    (home / "plugins").mkdir(parents=True)
    shutil.copytree(PLUGINS_SRC / COMPOSE_KEY, home / "plugins" / COMPOSE_KEY)
    (home / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    (tmp_path / "disc-bundled").mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(tmp_path / "disc-bundled"))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    from hermes_cli.plugins import PluginManager

    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[COMPOSE_KEY]
    assert loaded.enabled is True and loaded.error is None


def test_ac_fleet_f62_e_17(tmp_path, monkeypatch):
    """(derived) On an openshell fleet home that does **not** declare `egress.openshell_trust`, plugin discovery strips docker-bridge entries from the process's inherited `NO_PROXY` and `no_proxy`. That covers a keyless OSH-F64 rendered coworker home and a gateway default home after the installer's keyless default edit. The release proxy matcher then routes `172.17.0.1:18777` through the proxy. A non-fleet home (neither the veto setting nor the installer marker) is left untouched."""
    out = _render(tmp_path, OSH_F64_SPEC)
    cfg = _profile_configs(out)["orchestrator"]
    assert SOURCE_NAME not in yaml.safe_dump(cfg), "precondition: the keyless render carries no trust block"
    cfg.setdefault("plugins", {}).setdefault("enabled", [])
    if COMPOSE_KEY not in cfg["plugins"]["enabled"]:
        cfg["plugins"]["enabled"].append(COMPOSE_KEY)
    dirty = ("localhost,172.17.0.1,127.0.0.1,host.docker.internal,::1,*,.172.17.0.1,10.200.0.1,*.docker.internal,"
             "172.17.0.1:10256,host.docker.internal:10256")
    for name in NO_PROXY_NAMES:
        monkeypatch.setenv(name, dirty)
    _discover_home(tmp_path / "fleet", monkeypatch, cfg)
    for name in NO_PROXY_NAMES:
        kept = [e.strip() for e in os.environ.get(name, "").split(",") if e.strip()]
        assert kept == ["localhost", "127.0.0.1", "::1", "10.200.0.1"], f"{name} after discovery: {kept}"
    from gateway.platforms import base as gw_base

    assert gw_base.should_bypass_proxy(["172.17.0.1:18777"]) is False, "the broker hop must go through the proxy"

    inst = _installer("fleet_f62e_inst_ac17")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "inst-home"))
    (tmp_path / "inst-home").mkdir(exist_ok=True)
    keyless_lane = _spec_copy(tmp_path, OSH_F64_SPEC, tag="keyless-lane")
    seeded = {"plugins": {"enabled": [COMPOSE_KEY]}, "model": {"provider": "custom"}}
    _, diff, _ = inst.default_config_diff(copy.deepcopy(seeded), str(keyless_lane))
    installed = inst.apply_config_diff(copy.deepcopy(seeded), diff)
    assert SOURCE_NAME not in yaml.safe_dump(installed), "precondition: the keyless default edit carries no trust block"
    for name in NO_PROXY_NAMES:
        monkeypatch.setenv(name, dirty)
    _discover_home(tmp_path / "default-after-restart", monkeypatch, installed)
    for name in NO_PROXY_NAMES:
        kept = [entry.strip() for entry in os.environ.get(name, "").split(",") if entry.strip()]
        assert kept == ["localhost", "127.0.0.1", "::1", "10.200.0.1"], f"{name}: the installed default home kept {kept}"
    assert gw_base.should_bypass_proxy(["172.17.0.1:18777"]) is False

    plain = {"plugins": {"enabled": [COMPOSE_KEY]}, "terminal": {"backend": "ssh"}}
    for name in NO_PROXY_NAMES:
        monkeypatch.setenv(name, dirty)
    _discover_home(tmp_path / "plain", monkeypatch, plain)
    assert all(os.environ.get(n) == dirty for n in NO_PROXY_NAMES), "a non-fleet home's NO_PROXY must be left verbatim"
