"""Hermetic acceptance tests for FLEET-F62.f: the installer's policy-mirror check works without a host mount.

One ``test_ac_fleet_f62_f_<n>`` per ``pytest:`` row of the ADR's ``## Acceptance criteria`` (AC-1..AC-8, in order);
the docstring's first line is the criterion text. AC-9 is a ``live:`` scenario file, not here.

Shape: the plugin loads through ``PluginManager().discover_and_load()`` from an isolated ``HERMES_HOME`` with an EMPTY
bundled dir (``test_plugin_api_compat`` shape). The installer is then imported by file path from the loaded plugin's
directory, the way the ``install-openshell`` subaction loads it, and driven through ``apply()`` with every subprocess
answered by ``_Broker``. ``_Broker`` stands in for the lane broker: a ``--policy`` path under ``root`` resolves to
``view``, the broker's host-side policy root, which the installer cannot read unless ``root`` is ``view``. The
installer is told the policy root; the test decides what the broker answers.

Behaviour contract, not a snapshot: no network, nothing under ``~/.hermes``, no plugin source is read. The runbook
page is read as the shipped artefact whose content AC-8 names.

"""

import hashlib
import importlib.util
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import hermes_yaml as yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_KEY = "nv-coworker-compose"
SPEC = REPO_ROOT / "tests" / "e2e-scenarios" / "OSH-F64" / "spec" / "coworker-types.yaml"
RUNBOOK = REPO_ROOT / "website" / "docs" / "user-guide" / "fleet-openshell.md"
SHA = "a" * 40
ABSENT = (2, "", "flag not allowed: policy digest\n")   # the shim's refusal of the verb itself


# --------------------------------------------------------------------------- plugin + installer
@pytest.fixture
def installer(tmp_path, monkeypatch):
    """The installer module of the plugin as discovery loaded it (stock tree: the key is absent → KeyError)."""
    home, bundled = tmp_path / "discover-home", tmp_path / "empty-bundled"
    (home / "plugins").mkdir(parents=True)
    bundled.mkdir()
    shutil.copytree(REPO_ROOT / "plugins" / PLUGIN_KEY, home / "plugins" / PLUGIN_KEY)
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": [PLUGIN_KEY]}}), encoding="utf-8")
    with monkeypatch.context() as mp:
        mp.setenv("HOME", str(tmp_path))
        mp.setenv("HERMES_HOME", str(home))
        mp.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
        mp.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
        from hermes_cli.plugins import PluginManager

        manager = PluginManager()
        manager.discover_and_load()
        loaded = manager._plugins[PLUGIN_KEY]
        assert loaded.enabled is True and loaded.error is None and loaded.module is not None
        assert manager._cli_commands["coworker"]["plugin_key"] == PLUGIN_KEY, "the install-openshell entry point is registered"
    path = Path(loaded.module.__file__).resolve().parent / "openshell" / "installer.py"
    spec = importlib.util.spec_from_file_location(f"fleet_f62f_installer_{tmp_path.name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _roles(inst):
    return inst._coworker_roles(yaml.safe_load(SPEC.read_text(encoding="utf-8")))


def _project():
    return yaml.safe_load(SPEC.read_text(encoding="utf-8"))["project"]


def _render_bytes(role: str) -> bytes:
    return f"version: 1\n# {role} render\n".encode()


def _want(roles):
    return {f"{_project()}-{r}": hashlib.sha256(_render_bytes(r)).hexdigest() for r in roles}


def _host_view(tmp_path, case, roles, *, stale=(), missing=()) -> Path:
    """The broker's host policy root for one case: every role's render, except `stale` (one extra line) and
    `missing` (absent)."""
    view = tmp_path / f"{case}-host-root"
    for r in roles:
        f = view / "render" / r / f"policy-{r}.yaml"
        f.parent.mkdir(parents=True, exist_ok=True)
        if r not in missing:
            f.write_bytes(_render_bytes(r) + (b"# stale host mirror\n" if r in stale else b""))
    return view


# --------------------------------------------------------------------------- the lane broker
class _Broker:
    """Answers every subprocess the installer starts. `policy digest --policy F` prints a sha256sum line for the
    resolved file, or the scripted `digest` triple (rc, stdout, stderr) when one is set (a callable gets the path and
    may return None for the real answer); `sandbox create|replace`
    reads the resolved file at bind time and, given `--policy-sha256`, denies on a mismatch (a replace denies before
    it deletes); without it, it binds whatever the file holds. `bound` maps each sandbox the broker created or replaced
    to the digest of the bytes it bound. `on_call(cmd)` runs before each answer (a host-side write during the install)."""

    def __init__(self, root: Path, view: Path, roles, *, existing=(), digest=None, on_call=None):
        self.root, self.view, self.roles = str(root), view, list(roles)
        self.existing, self.digest, self.on_call = set(existing), digest, on_call
        self.calls: list = []
        self.bound: dict = {}

    def resolve(self, path: str) -> Path:
        return self.view / os.path.relpath(path, self.root) if path.startswith(self.root + os.sep) else Path(path)

    @staticmethod
    def _done(cmd, kwargs, rc=0, out="", err=""):
        if not (kwargs.get("text") or kwargs.get("encoding") or kwargs.get("universal_newlines")):
            out, err = out.encode("utf-8"), err.encode("utf-8")
        if rc and kwargs.get("check"):
            raise subprocess.CalledProcessError(rc, cmd, out, err)
        return subprocess.CompletedProcess(cmd, rc, out, err)

    def run(self, cmd, *_args, **kwargs):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        if self.on_call:
            self.on_call(cmd)
        head = cmd[:3]
        if head == ["hermes", "coworker", "compose"]:
            out = Path(cmd[cmd.index("--out") + 1])
            for role in self.roles:
                (out / role).mkdir(parents=True, exist_ok=True)
                (out / role / f"policy-{role}.yaml").write_bytes(_render_bytes(role))
            return self._done(cmd, kwargs)
        if head == ["hermes", "gateway", "status"]:
            return self._done(cmd, kwargs, out="Gateway is running\n")
        if head == ["openshell", "sandbox", "list"]:
            return self._done(cmd, kwargs, out="NAME\n" + "".join(f"{n}\n" for n in sorted(self.existing)))
        if head == ["openshell", "policy", "digest"]:
            path = cmd[cmd.index("--policy") + 1]
            scripted = self.digest(path) if callable(self.digest) else self.digest
            if scripted is not None:
                rc, out, err = scripted
                return self._done(cmd, kwargs, rc=rc, out=out, err=err)
            f = self.resolve(path)
            if not f.is_file():
                return self._done(cmd, kwargs, rc=1, err=f"policy unreadable: {path}\n")
            return self._done(cmd, kwargs, out=f"{hashlib.sha256(f.read_bytes()).hexdigest()}  {path}\n")
        if head in (["openshell", "sandbox", "create"], ["openshell", "sandbox", "replace"]):
            name = cmd[cmd.index("--name") + 1]
            f = self.resolve(cmd[cmd.index("--policy") + 1])
            got = hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else None
            if got is None or ("--policy-sha256" in cmd and cmd[cmd.index("--policy-sha256") + 1] != got):
                return self._done(cmd, kwargs, rc=1, err=f"policy mirror stale: sha256 {got} != expected\n")
            if head[2] == "create" and name in self.existing:
                return self._done(cmd, kwargs, rc=1, err=f"sandbox exists: {name}\n")
            self.existing.add(name)
            self.bound[name] = got
            return self._done(cmd, kwargs)
        if head == ["openshell", "sandbox", "delete"]:
            self.existing.discard(cmd[3])
            return self._done(cmd, kwargs)
        if head == ["openshell", "sandbox", "ssh-config"]:
            return self._done(cmd, kwargs, out=f"Host openshell-{cmd[3]}\n    User sandbox\n")
        return self._done(cmd, kwargs)

    def sandbox_ops(self):
        return [c for c in self.calls if c[:2] == ["openshell", "sandbox"] and len(c) > 2
                and c[2] in ("create", "replace", "delete")]

    def digest_asked(self):
        """Roles whose broker digest was asked before the first sandbox create, replace or delete."""
        ops = self.sandbox_ops()
        stop = self.calls.index(ops[0]) if ops else len(self.calls)
        return {Path(c[c.index("--policy") + 1]).parent.name for c in self.calls[:stop]
                if c[:3] == ["openshell", "policy", "digest"] and "--policy" in c}


class _NoPopen:
    def __init__(self, *_a, **_k):
        self.pid, self.returncode = 4242, None

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0


# --------------------------------------------------------------------------- one install
_HARNESS_ERRORS = (AssertionError, AttributeError, IndexError, KeyError, NameError, TypeError)


def _install(tmp_path, monkeypatch, inst, case, broker, policy_root, *, unreadable_under=None):
    """One `apply()` of the OSH-F64 lane spec in the home `<tmp>/<case>`, every subprocess answered by `broker`.
    `unreadable_under`: any installer read of a file below it raises PermissionError, as a Landlock read deny does
    (stat still succeeds); the broker's own reads are not affected. Returns the refusal, or None when it completed."""
    base = tmp_path / case
    for d in ("user", "home", "managed"):
        (base / d).mkdir(parents=True, exist_ok=True)
    with monkeypatch.context() as mp:
        mp.setenv("HOME", str(base / "user"))
        mp.setenv("HERMES_HOME", str(base / "home"))
        mp.setenv("HERMES_MANAGED_DIR", str(base / "managed"))
        mp.setattr(subprocess, "run", broker.run)
        mp.setattr(subprocess, "Popen", _NoPopen)
        mp.setattr(time, "sleep", lambda _s: None)
        if unreadable_under is not None:
            _deny_reads(mp, Path(unreadable_under), broker)
        try:
            inst.apply(str(SPEC), SHA, room_creator=lambda *_a: None, room_exists=lambda _rid: False,
                       policy_root=None if policy_root is None else str(policy_root))
        except _HARNESS_ERRORS:
            raise
        except Exception as exc:
            return exc
    assert any(c[:3] == ["hermes", "coworker", "compose"] for c in broker.calls), "the install never composed"
    return None


def _deny_reads(mp, root: Path, broker):
    import builtins
    import io

    real = io.open
    prefix = str(root) + os.sep
    serving = broker.run

    def guarded(file, *args, **kwargs):
        if not getattr(broker, "serving", False) and str(file).startswith(prefix):
            raise PermissionError(13, "Permission denied", str(file))
        return real(file, *args, **kwargs)

    def run(cmd, *args, **kwargs):
        broker.serving = True
        try:
            return serving(cmd, *args, **kwargs)
        finally:
            broker.serving = False

    mp.setattr(io, "open", guarded)
    mp.setattr(builtins, "open", guarded)
    mp.setattr(subprocess, "run", run)


def _records(tmp_path, case) -> dict:
    home = tmp_path / case / "home"
    return {p.name: p.read_text(encoding="utf-8").strip() for p in sorted(home.rglob("*.sha256"))}


def _seed_drifted(tmp_path, case, names) -> dict:
    """Every sandbox exists and its recorded digest is not the render's, so each one is due a re-create."""
    rec = tmp_path / case / "home" / ".osh-f64" / "sandbox-policy"
    rec.mkdir(parents=True, exist_ok=True)
    for n in names:
        (rec / f"{n}.sha256").write_text("0" * 64 + "\n", encoding="utf-8")
    return _records(tmp_path, case)


def _opt(cmd, flag):
    return cmd[cmd.index(flag) + 1] if flag in cmd else None


def _refused(tmp_path, monkeypatch, inst, case, broker, policy_root, why, **kw):
    """A refusal performs no sandbox create, replace or delete, binds nothing and leaves every recorded digest as it
    was. Returns the refusal text."""
    before = _records(tmp_path, case)
    exc = _install(tmp_path, monkeypatch, inst, case, broker, policy_root, **kw)
    assert exc is not None, f"{why}: the rooted install must refuse"
    assert not isinstance(exc, OSError), f"{why}: a raw {type(exc).__name__} is a crash, not a refusal: {exc}"
    assert broker.sandbox_ops() == [], f"{why}: no sandbox operation may run, got {broker.sandbox_ops()}"
    assert broker.bound == {}, f"{why}: nothing may be bound, got {broker.bound}"
    assert _records(tmp_path, case) == before, f"{why}: recorded digests must not change"
    return str(exc)


def _plan_creates(tmp_path, monkeypatch, inst, case, policy_root):
    """The provision-create argv vectors `build_plan` emits for this home and root."""
    with monkeypatch.context() as mp:
        mp.setenv("HOME", str(tmp_path / case / "user"))
        mp.setenv("HERMES_HOME", str(tmp_path / case / "home"))
        mp.setenv("HERMES_MANAGED_DIR", str(tmp_path / case / "managed"))
        steps = inst.build_plan({}, str(SPEC), SHA, policy_root=None if policy_root is None else str(policy_root))
    return [list(s.command) for s in steps if getattr(s, "tag", None) == "provision_create"]


# ============================================================================ AC-1 .. AC-3 (brief floor a-c)
def test_ac_fleet_f62_f_1(installer, tmp_path, monkeypatch):
    """With the `--policy-root` mirror unreadable from the installer (no host mount) and a broker digest that matches the render for every role, the rooted install completes and every create or replace binds the host mirror path carrying the render's sha256."""
    inst = installer
    roles = _roles(inst)
    names, want = list(_want(roles)), _want(roles)
    gw_root = tmp_path / "host-only" / "policy-root"   # what the installer is told; absent in its filesystem
    broker = _Broker(gw_root, _host_view(tmp_path, "a", roles), roles)
    assert _install(tmp_path, monkeypatch, inst, "a", broker, gw_root) is None, "a broker-verified install must complete"
    ops = broker.sandbox_ops()
    assert [c[2] for c in ops] == ["create"] * len(roles), f"a fresh fleet needs one create per role, got {ops}"
    assert {_opt(c, "--name"): _opt(c, "--policy-sha256") for c in ops} == want, "each create carries its render digest"
    assert all(_opt(c, "--policy").startswith(str(gw_root) + os.sep) for c in ops), "creates bind the host mirror path"
    assert broker.digest_asked() == set(roles), "every role's host digest is asked before any sandbox operation"
    assert broker.bound == want
    assert _records(tmp_path, "a") == {f"{n}.sha256": want[n] for n in names}

    _seed_drifted(tmp_path, "a-drift", names)
    broker = _Broker(gw_root, _host_view(tmp_path, "a-drift", roles), roles, existing=names)
    assert _install(tmp_path, monkeypatch, inst, "a-drift", broker, gw_root) is None
    ops = broker.sandbox_ops()
    assert [c[2] for c in ops] == ["replace"] * len(roles), f"a drifted sandbox is replaced, never deleted first: {ops}"
    assert all(_opt(c, "--policy").startswith(str(gw_root) + os.sep) for c in ops), "replaces bind the host mirror path"
    assert {_opt(c, "--name"): _opt(c, "--policy-sha256") for c in ops} == want
    assert broker.bound == want and _records(tmp_path, "a-drift") == {f"{n}.sha256": want[n] for n in names}


def test_ac_fleet_f62_f_2(installer, tmp_path, monkeypatch):
    """A broker digest that disagrees with the render for any one role refuses the whole rooted install before any sandbox create, replace or delete, leaves every recorded digest unchanged, and the refusal names that role's sandbox, also when the installer's own readable copy of the mirror matches the render."""
    inst = installer
    roles = _roles(inst)
    names = list(_want(roles))
    project = _project()
    gw_root = tmp_path / "host-only" / "policy-root"
    for stale in (roles[-1], roles[0]):
        for case, existing in ((f"b-fresh-{stale}", ()), (f"b-drift-{stale}", names)):
            if existing:
                _seed_drifted(tmp_path, case, names)
            broker = _Broker(gw_root, _host_view(tmp_path, case, roles, stale=[stale]), roles, existing=existing)
            msg = _refused(tmp_path, monkeypatch, inst, case, broker, gw_root, f"{case}: broker digest stale")
            assert f"{project}-{stale}" in msg, f"{case}: the refusal must name the stale role's sandbox: {msg}"
    # The installer can read a copy at the --policy path that matches the render, while the broker's file is stale:
    # a readable path does not prove the broker's file, so a broker that can attest is still asked.
    local = _host_view(tmp_path, "b-local", roles)
    broker = _Broker(local, _host_view(tmp_path, "b-local-broker", roles, stale=[roles[-1]]), roles)
    msg = _refused(tmp_path, monkeypatch, inst, "b-local", broker, local, "a matching local copy, a stale broker file")
    assert f"{project}-{roles[-1]}" in msg, msg


def test_ac_fleet_f62_f_3(installer, tmp_path, monkeypatch):
    """With the mirror unreadable from the installer and no broker digest (the broker refuses the `policy digest` verb itself, or gives an answer that is not a digest), the rooted install halts before any sandbox operation, leaves every recorded digest unchanged, and the refusal names the missing broker verb `openshell policy digest` as the fix."""
    inst = installer
    roles = _roles(inst)
    names = list(_want(roles))
    gw_root = tmp_path / "host-only" / "policy-root"
    answers = {"absent": ABSENT, "other-flag": (2, "", "flag not allowed: --policy\n")}
    for label, answer in answers.items():
        for case, existing in ((f"c-fresh-{label}", ()), (f"c-drift-{label}", names)):
            if existing:
                _seed_drifted(tmp_path, case, names)
            broker = _Broker(gw_root, _host_view(tmp_path, case, roles), roles, existing=existing, digest=answer)
            msg = _refused(tmp_path, monkeypatch, inst, case, broker, gw_root, f"{case}: unreadable, no digest")
            assert "openshell policy digest" in msg, f"{case}: the refusal must name the fix: {msg}"
            assert str(gw_root) in msg, f"{case}: the refusal must name the unreadable host path: {msg}"


# ============================================================================ AC-4, AC-5 (every non-digest answer; "unreadable")
def test_ac_fleet_f62_f_4(installer, tmp_path, monkeypatch):
    """(derived) Every broker answer that is not a `sha256sum` line for exactly the requested file refuses the rooted install before any sandbox operation, even when every mirror is readable and byte-identical to the render: the verb refused (exit 2, stderr `flag not allowed: policy digest`), a deny, another flag refused, extra output, an unparsable success, a line for another path, or that answer for one role only; the refusal names `openshell policy digest`."""
    inst = installer
    roles = _roles(inst)
    root = _host_view(tmp_path, "d-shared", roles)   # readable by the installer AND the broker's file
    other = str(root / "render" / roles[0] / "other.yaml")
    answers = {
        "verb-absent": ABSENT,
        "deny": (1, "", "denied: policy digest outside the policy root\n"),
        "other-flag": (2, "", "flag not allowed: --policy\n"),
        "extra-stdout": (2, "flag not allowed\n", "flag not allowed: policy digest\n"),
        "extra-stderr": (2, "", "flag not allowed: policy digest --policy\n"),
        "rc1-same-text": (1, "", "flag not allowed: policy digest\n"),
        "unparsable": (0, "policy digest: ok\n", ""),
        "other-path": (0, f"{hashlib.sha256(_render_bytes(roles[0])).hexdigest()}  {other}\n", ""),
    }
    for label, answer in answers.items():
        broker = _Broker(root, root, roles, digest=answer)
        msg = _refused(tmp_path, monkeypatch, inst, f"d-{label}", broker, root, f"{label}: not a digest")
        assert "openshell policy digest" in msg, f"{label}: the refusal must name the fix: {msg}"
    first = str(root / "render" / roles[0])

    def one_role(path):
        return ABSENT if path.startswith(first + os.sep) else None

    broker = _Broker(root, root, roles, digest=one_role)
    msg = _refused(tmp_path, monkeypatch, inst, "d-one-role", broker, root, "no digest for one role, digests for the rest")
    assert "openshell policy digest" in msg and f"{_project()}-{roles[0]}" in msg, msg


def test_ac_fleet_f62_f_5(installer, tmp_path, monkeypatch):
    """(derived) A mirror path the installer can stat but not read (a permission error, as a Landlock read deny raises) counts as unreadable, never as a crash or a pass: with a matching broker digest the install completes on the broker's attestation; with the verb absent it halts before any sandbox operation and names `openshell policy digest`."""
    inst = installer
    roles = _roles(inst)
    want = _want(roles)
    root = _host_view(tmp_path, "e-root", roles)
    broker = _Broker(root, root, roles)
    assert _install(tmp_path, monkeypatch, inst, "e-ok", broker, root, unreadable_under=root) is None, \
        "a permission-denied mirror with a matching broker digest must install"
    assert {_opt(c, "--name"): _opt(c, "--policy-sha256") for c in broker.sandbox_ops()} == want
    assert broker.bound == want
    broker = _Broker(root, root, roles, digest=ABSENT)
    msg = _refused(tmp_path, monkeypatch, inst, "e-absent", broker, root, "permission-denied mirror, no verb",
                   unreadable_under=root)
    assert "openshell policy digest" in msg, msg


# ============================================================================ AC-6, AC-7 (rooted always attests; unrooted unchanged)
def test_ac_fleet_f62_f_6(installer, tmp_path, monkeypatch):
    """With every rooted mirror readable by the installer and byte-identical to the render but the broker's `policy digest` verb absent, the rooted install refuses before any sandbox create, replace or delete, leaves every recorded digest unchanged, and the refusal names `openshell policy digest`: a readable mirror never stands in for the broker's digest."""
    inst = installer
    roles = _roles(inst)
    names = list(_want(roles))
    root = _host_view(tmp_path, "f-root", roles)
    for case, existing in (("f-fresh", ()), ("f-drift", names), ("f-match", names)):
        if case == "f-drift":
            _seed_drifted(tmp_path, case, names)
        if case == "f-match":
            rec = tmp_path / case / "home" / ".osh-f64" / "sandbox-policy"
            rec.mkdir(parents=True, exist_ok=True)
            for n, d in _want(roles).items():
                (rec / f"{n}.sha256").write_text(d + "\n", encoding="utf-8")
        broker = _Broker(root, root, roles, existing=existing, digest=ABSENT)
        msg = _refused(tmp_path, monkeypatch, inst, case, broker, root, f"{case}: readable mirror, verb absent")
        assert "openshell policy digest" in msg, f"{case}: the refusal must name the fix: {msg}"
        assert [c for c in broker.calls if c[:3] == ["openshell", "policy", "digest"]], f"{case}: the broker is asked"


def test_ac_fleet_f62_f_7(installer, tmp_path, monkeypatch):
    """With `--policy-root` unset, the install plan is byte-identical to the plan without the option, every create binds the gateway-internal render path, and an apply asks the broker for no digest and carries no `--policy-sha256`."""
    inst = installer
    roles = _roles(inst)
    with monkeypatch.context() as mp:
        mp.setenv("HOME", str(tmp_path / "g" / "user"))
        mp.setenv("HERMES_HOME", str(tmp_path / "g" / "home"))
        mp.setenv("HERMES_MANAGED_DIR", str(tmp_path / "g" / "managed"))
        (tmp_path / "g" / "home").mkdir(parents=True)
        out = inst._render_out_dir()
        default = inst.render_transcript(inst.build_plan({}, str(SPEC), SHA))
        assert inst.render_transcript(inst.build_plan({}, str(SPEC), SHA, policy_root=None)) == default
    creates = _plan_creates(tmp_path, monkeypatch, inst, "g", None)
    assert {_opt(c, "--policy") for c in creates} == {f"{out}/{r}/policy-{r}.yaml" for r in roles}
    broker = _Broker(tmp_path / "unused", tmp_path / "unused", roles)
    assert _install(tmp_path, monkeypatch, inst, "g", broker, None) is None
    assert not [c for c in broker.calls if c[:3] == ["openshell", "policy", "digest"]], "an unrooted install asks no digest"
    assert [c[2] for c in broker.sandbox_ops()] == ["create"] * len(roles)
    assert not [c for c in broker.sandbox_ops() if "--policy-sha256" in c]
    assert {_opt(c, "--policy") for c in broker.sandbox_ops()} == {f"{out}/{r}/policy-{r}.yaml" for r in roles}, \
        "executed creates bind the gateway-internal render path"


# ============================================================================ AC-8 (floor e: the runbook)
def _operator_section() -> str:
    text = RUNBOOK.read_text(encoding="utf-8")
    start = text.find("### What the operator runs")
    assert start != -1, "the runbook must keep its `What the operator runs` section"
    nxt = text.find("\n### ", start + 1)
    return text[start:(nxt if nxt != -1 else len(text))]


def test_ac_fleet_f62_f_8():
    """The runbook's `What the operator runs` section says a rooted install verifies every worker policy against the broker's `openshell policy digest` without requiring access to the host root, that each create carries `--policy-sha256`, that a mirror the installer can read is also byte-compared with the render, that any non-digest answer (naming `flag not allowed: policy digest`) refuses, and that an alias or copy alone attests nothing; it gives no instruction to create a symlink or a copy of the policy root."""
    section = _operator_section()
    flat = re.sub(r"\s+", " ", section)
    assert "`openshell policy digest" in section, "the section must name the broker digest verb"
    assert "--policy-sha256" in section, "the section must say each create carries the render's sha256"
    assert "flag not allowed: policy digest" in section, "the section must name the broker's no-verb answer"
    assert re.search(r"(?i)\b(?:does not|need not|without)\s+(?:require|requiring|needing)\b[^.]{0,60}\bhost root", flat), \
        "the section must say rooted verification does not require access to the host root"
    assert re.search(r"(?i)\bbyte for byte\b|\bbyte-for-byte\b", flat) and re.search(r"(?i)\balso\b[^.]{0,40}\bcompar", flat), \
        "the section must say a readable mirror is also byte-compared with the render"
    assert re.search(r"(?i)\b(?:alias|copy)\b[^.]{0,120}\battests nothing\b", flat), \
        "the section must say an alias or copy alone attests nothing about the broker's file"
    assert not re.search(r"(?i)\b(?:create|make|add|place|put|ln -s)\b[^.]{0,40}\b(?:symlink|symbolic link|copy)\b", flat), \
        "the section must give no instruction to create a symlink or copy"
