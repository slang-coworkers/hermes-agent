"""Hermetic acceptance tests for UPG-F69.s.a: the v0.22.5 ship's installer gaps close without a manual box step.

One ``test_ac_upg_f69_s_a_<n>`` per ``pytest:`` row of the ADR's ``## Acceptance criteria`` (AC-1..AC-17, in order);
the docstring's first line is the criterion text. AC-18 and AC-19 are ``live:`` scenario files, not here.

Shape: nv-coworker-compose (and, where a criterion needs the gate, nv-fleet-gates) loads through
``PluginManager().discover_and_load()`` from an isolated ``HERMES_HOME`` with an EMPTY bundled dir (the
``tests/hermes_cli/test_plugin_api_compat.py`` shape). The installer is imported by file path from the loaded plugin, the
way ``install-openshell`` loads it, and is driven through ``apply()`` / ``phase_a()``. Every subprocess is answered by
``_Broker``, the FLEET-F62.f stateful fake extended for this row:
- it un-wraps a lane-wrapped openshell argv (the argv names the declared shim; the openshell args follow it);
- it can SGR-colour its answers, as openshell 0.0.72 does even when piped;
- it passes ``git`` through to the real binary;
- it answers ``hermes … plugins install`` with Hermes' own ``_clone_plugin_repo`` under the env the installer passed.

"The lane spec" is the ship's shape (ADR B5): the head FLEET-F62.c ``spec/openshell`` tree copied into ``tmp_path``,
with ``egress`` taken from the head ING-F66 spec (the lane egress of record) and ``project`` = ``fleet-f62d-t``.

Behaviour contract, not a snapshot: no network (git runs with ``GIT_ALLOW_PROTOCOL=file``), nothing outside
``tmp_path``, and no plugin source is read. Rendered skills and spec trees are read as the shipped artefacts the
criteria name. A missing stock ``_Broker`` answer is harness mechanics the builder may add; no assertion may be weakened.
"""

import contextlib
import hashlib
import importlib.util
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
import hermes_yaml as yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
F62C = REPO_ROOT / "tests" / "e2e-scenarios" / "FLEET-F62.c" / "spec" / "openshell"
ING = REPO_ROOT / "tests" / "e2e-scenarios" / "ING-F66" / "spec" / "openshell"
SPEC_TREES = {"FLEET-F62.c": F62C, "ING-F66": ING}
COMPOSE_KEY = "nv-coworker-compose"
GATES_KEY = "nv-fleet-gates"
PROJECT = "fleet-f62d-t"
SHA = "a" * 40
FORK_URL = "https://github.com/slang-coworkers/hermes-agent.git"
_REAL_RUN = subprocess.run
_REAL_POPEN = subprocess.Popen
_REAL_GIT = shutil.which("git")
_ESC = "\x1b"


def _sgr(text: str) -> str:
    return f"\x1b[32m{text}\x1b[39m"


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _load_yaml(path: Path):
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def _dump_yaml(path: Path, data) -> None:
    Path(path).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


# --------------------------------------------------------------------------- plugin + installer
@pytest.fixture
def installer(tmp_path, monkeypatch):
    """The installer module of nv-coworker-compose as discovery loaded it (stock tree: absent key → KeyError)."""
    home, bundled = tmp_path / "discover-home", tmp_path / "empty-bundled"
    (home / "plugins").mkdir(parents=True)
    bundled.mkdir(exist_ok=True)
    shutil.copytree(REPO_ROOT / "plugins" / COMPOSE_KEY, home / "plugins" / COMPOSE_KEY)
    _dump_yaml(home / "config.yaml", {"plugins": {"enabled": [COMPOSE_KEY]}})
    with monkeypatch.context() as mp:
        mp.setenv("HOME", str(tmp_path))
        mp.setenv("HERMES_HOME", str(home))
        mp.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
        mp.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
        from hermes_cli.plugins import PluginManager

        manager = PluginManager()
        manager.discover_and_load()
        loaded = manager._plugins[COMPOSE_KEY]
        assert loaded.enabled is True and loaded.error is None and loaded.module is not None
        assert manager._cli_commands["coworker"]["plugin_key"] == COMPOSE_KEY
    path = Path(loaded.module.__file__).resolve().parent / "openshell" / "installer.py"
    spec = importlib.util.spec_from_file_location(f"upg_f69sa_installer_{tmp_path.name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _gates(tmp_path, monkeypatch, home: Path, settings: dict):
    """nv-fleet-gates loaded through discovery with ``HERMES_HOME=home`` (a root or ``<root>/profiles/<p>``)."""
    from hermes_cli.plugins import PluginManager
    import model_tools

    (home / "plugins").mkdir(parents=True, exist_ok=True)
    if not (home / "plugins" / GATES_KEY).exists():
        shutil.copytree(REPO_ROOT / "plugins" / GATES_KEY, home / "plugins" / GATES_KEY)
    cfg = {"plugins": {"enabled": [GATES_KEY], "entries": {GATES_KEY: {"settings": dict(settings)}}}}
    _dump_yaml(home / "config.yaml", cfg)
    bundled = tmp_path / "empty-bundled"
    bundled.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path / "gates-user"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
    model_tools.discover_builtin_tools()
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[GATES_KEY]
    assert loaded.enabled is True and loaded.error is None and "pre_tool_call" in loaded.hooks_registered
    return manager, loaded


def _gate(manager, tool_name, args, session_id="s-f69sa"):
    results = manager.invoke_hook("pre_tool_call", tool_name=tool_name, args=args, session_id=session_id,
                                  task_id="t-1", tool_call_id="tc-1", turn_id="turn-1")
    directives = [r for r in (results or []) if isinstance(r, dict) and r.get("action")]
    return directives[0] if directives else None


def _wire(manager, *argv):
    import argparse

    entry = manager._cli_commands["wire"]
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="cmd")
    entry["setup_fn"](sub.add_parser("wire"))
    ns = parser.parse_args(["wire", *argv])
    handler = entry.get("handler_fn") or getattr(ns, "func", None)
    return handler(ns)


def _wire_in_home(a: str, b: str):
    """`hermes wire add a b` in the CURRENT HERMES_HOME, through nv-fleet-gates as that home's config enables it."""
    from hermes_cli.plugins import PluginManager
    import model_tools

    cfg = _load_yaml(Path(os.environ["HERMES_HOME"]) / "config.yaml")
    store = (((cfg.get("plugins") or {}).get("entries") or {}).get(GATES_KEY) or {}).get("settings", {}).get("edges_db_path")
    assert not str(store).startswith("/tmp"), f"harness guard: `wire add` would write the real /tmp store {store}"
    model_tools.discover_builtin_tools()
    manager = PluginManager()
    manager.discover_and_load()
    loaded = manager._plugins[GATES_KEY]
    assert loaded.enabled is True and loaded.error is None, "the gateway home's nv-fleet-gates did not load"
    return _wire(manager, "add", a, b)


def _edges_module():
    path = REPO_ROOT / "plugins" / GATES_KEY / "edges.py"
    spec = importlib.util.spec_from_file_location("upg_f69sa_edges", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- specs and the lane bundle
def _manifest_pairs(spec_dir: Path) -> set:
    pairs = set()
    for raw in (spec_dir / "wire-manifest.txt").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            parts = line.split()
            assert parts[:3] == ["hermes", "wire", "add"] and len(parts) == 5, line
            pairs.add(frozenset(parts[3:5]))
    assert len(pairs) == 5
    return pairs


def _bundle(tmp_path: Path, lane: dict, *, drop=()) -> Path:
    """A test copy of the lane bundle with the declared layout: env file, shim and members, each non-empty."""
    src = tmp_path / "osh-lane"
    for rel in (lane["env_file"], lane["shim"], *lane.get("members", [])):
        if rel in drop:
            continue
        f = src / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"# test lane file {rel}\n", encoding="utf-8")
    if (src / lane["shim"]).exists():
        os.chmod(src / lane["shim"], 0o700)
    return src


# The ADR's documented `ssh_lane` shape (B1). It is injected only where a criterion tests installer BEHAVIOUR, so the
# stock tree fails on that behaviour rather than on the absent key; AC-7 reads each spec's OWN declaration.
_DOC_LANE = {"bundle": "/tmp/osh-lane", "env_file": "lane.env", "shim": "bin/openshell",
             "interpreter": "/opt/hermes/.venv/bin/python3", "members": ["token", "ca-bundle.pem"]}


def _lane_spec(tmp_path: Path, tree: str = "FLEET-F62.c", *, name: str = "spec", drop=(), inject: bool = True):
    """The lane spec (module docstring) from one spec tree; returns (coworker-types.yaml path, lane dict | None)."""
    dst = tmp_path / name
    shutil.copytree(SPEC_TREES[tree], dst)
    path = dst / "coworker-types.yaml"
    data = _load_yaml(path)
    if tree == "FLEET-F62.c":
        data["egress"] = _load_yaml(ING / "coworker-types.yaml")["egress"]
    data["project"] = PROJECT
    lane = data.get("ssh_lane")
    if not isinstance(lane, dict) and inject:
        lane = _DOC_LANE
    if isinstance(lane, dict):
        lane = dict(lane)
        lane["bundle"] = str(_bundle(tmp_path, lane, drop=drop))
        data["ssh_lane"] = lane
    _dump_yaml(path, data)
    return path, lane


def _roles(inst, spec_path: Path):
    return inst._coworker_roles(_load_yaml(spec_path))


def _lane_line(lane: dict, sandbox: str) -> str:
    b = lane["bundle"]
    return (f"/bin/sh -c '. {b}/{lane['env_file']} && exec {lane['interpreter']} {b}/{lane['shim']} "
            f"ssh-proxy --gateway-name nemoclaw --name {sandbox}'")


def _proxy_commands(include: Path) -> dict:
    """alias → ProxyCommand value, parsed from one ssh include file."""
    out, host = {}, None
    for line in include.read_text(encoding="utf-8").splitlines():
        parts = line.strip().split(None, 1)
        if not parts:
            continue
        key = parts[0].lower()
        if key == "host" and len(parts) == 2:
            host = parts[1].strip()
        elif key == "proxycommand" and host and len(parts) == 2:
            out[host] = parts[1].strip()
    return out


def _render_bytes(role: str) -> bytes:
    return f"version: 1\n# {role} render\n".encode()


# --------------------------------------------------------------------------- the lane broker
class _NoPopen:
    def __init__(self, *_a, **_k):
        self.pid, self.returncode = 4242, None

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0


class _Broker:
    """Answers every subprocess the installer starts (FLEET-F62.f `_Broker`, extended):
    - a lane-wrapped openshell argv is un-wrapped to `openshell <args>` and remembered in `wrapped`;
    - `color=True` SGR-wraps sandbox-list columns, the digest and the ssh-config Host alias;
    - `git` runs for real; `hermes … plugins install` runs Hermes' `_clone_plugin_repo` when `clone=True`;
    - `hermes wire add` runs through nv-fleet-gates in the current home when `real_wires=True`;
    - `hermes profile install … --name <p>` creates `<HERMES_HOME>/profiles/<p>/`."""

    def __init__(self, roles, *, existing=(), color=False, lane=None, root=None, view=None,
                 clone=False, real_wires=False, scratch=None):
        self.roles, self.existing, self.color, self.lane = list(roles), set(existing), color, lane
        self.root, self.view = (str(root) if root else None), view
        self.clone, self.real_wires, self.scratch = clone, real_wires, scratch
        self.raw, self.calls, self.wrapped, self.popened, self.installs = [], [], [], [], []
        self.bound: dict = {}

    def resolve(self, path: str) -> Path:
        if self.root and path.startswith(self.root + os.sep):
            return self.view / os.path.relpath(path, self.root)
        return Path(path)

    @staticmethod
    def _done(cmd, kwargs, rc=0, out="", err=""):
        if not (kwargs.get("text") or kwargs.get("encoding") or kwargs.get("universal_newlines")):
            out, err = out.encode("utf-8"), err.encode("utf-8")
        if rc and kwargs.get("check"):
            raise subprocess.CalledProcessError(rc, cmd, out, err)
        return subprocess.CompletedProcess(cmd, rc, out, err)

    def _unwrap(self, cmd):
        if not self.lane:
            return cmd
        shim = str(Path(self.lane["bundle"]) / self.lane["shim"])
        if shim not in cmd:
            return cmd
        i = cmd.index(shim)
        env_file = str(Path(self.lane["bundle"]) / self.lane["env_file"])
        self.wrapped.append({"argv": cmd, "sources_env": any(env_file in c for c in cmd[:i]),
                             "interpreter": cmd[i - 1] if i else None})
        return ["openshell", *cmd[i + 1:]]

    def popen(self, cmd, *a, **k):
        self.popened.append([str(c) for c in cmd])
        return _NoPopen(cmd, *a, **k)

    def sandbox_ops(self):
        return [c for c in self.calls if c[:2] == ["openshell", "sandbox"] and len(c) > 2
                and c[2] in ("create", "replace", "delete")]

    def plugin_installs(self):
        return [c for c in self.calls if "plugins" in c and c[c.index("plugins") + 1:c.index("plugins") + 2] == ["install"]]

    def wire_adds(self):
        return [c for c in self.calls if c[:3] == ["hermes", "wire", "add"]]

    def _plugin_install(self, cmd, kwargs):
        """Resolve `--ref` exactly as `hermes plugins install` does, under the env the installer passed."""
        from hermes_cli import plugins_cmd
        from hermes_cli.plugins_cmd_git import _clone_plugin_repo

        src, ref = cmd[cmd.index("install") + 1], cmd[cmd.index("--ref") + 1]
        subdir = "/".join(src.split("/")[2:]) or None
        env = dict(kwargs.get("env") or os.environ)
        env["GIT_ALLOW_PROTOCOL"] = "file"
        dest = Path(tempfile.mkdtemp(dir=self.scratch)) / "clone"
        exe = None
        try:
            with _swapped_environ(env):
                # Hermes caches the resolved git binary per process; each install must resolve it from ITS env.
                plugins_cmd._resolve_git_executable.cache_clear()
                exe = plugins_cmd._resolve_git_executable()
                head = _clone_plugin_repo(dest, FORK_URL, ref, subdir)
        except Exception as exc:  # noqa: BLE001 - any clone failure is the install's failure
            self.installs.append({"cmd": cmd, "head": None, "git": exe})
            return self._done(cmd, kwargs, rc=1, err=f"plugin clone failed: {exc}\n")
        finally:
            plugins_cmd._resolve_git_executable.cache_clear()
        self.installs.append({"cmd": cmd, "head": head, "git": exe})
        if head != ref:
            return self._done(cmd, kwargs, rc=1, err=f"installed HEAD {head} != --ref {ref}\n")
        return self._done(cmd, kwargs)

    def run(self, cmd, *args, **kwargs):
        cmd = [str(c) for c in cmd]
        self.raw.append(cmd)
        if Path(cmd[0]).name == "git":
            with _real_popen():   # subprocess.run spawns through the module's Popen, which the harness patched
                return _REAL_RUN(cmd, *args, **kwargs)
        cmd = self._unwrap(cmd)
        self.calls.append(cmd)
        c = self.color
        head = cmd[:3]
        if head == ["hermes", "coworker", "compose"]:
            out = Path(cmd[cmd.index("--out") + 1])
            for role in self.roles:
                (out / role).mkdir(parents=True, exist_ok=True)
                (out / role / f"policy-{role}.yaml").write_bytes(_render_bytes(role))
            return self._done(cmd, kwargs)
        if head == ["hermes", "gateway", "status"]:
            return self._done(cmd, kwargs, out="Gateway is running\n")
        if head == ["hermes", "profile", "install"]:
            (Path(os.environ["HERMES_HOME"]) / "profiles" / cmd[cmd.index("--name") + 1]).mkdir(parents=True, exist_ok=True)
            return self._done(cmd, kwargs)
        if head == ["hermes", "wire", "add"] and self.real_wires:
            _wire_in_home(cmd[3], cmd[4])
            return self._done(cmd, kwargs, out="{}\n")
        if self.clone and cmd[0] == "hermes" and "plugins" in cmd and cmd[cmd.index("plugins") + 1] == "install":
            return self._plugin_install(cmd, kwargs)
        if head == ["openshell", "sandbox", "list"]:
            rows = "".join(f"{_sgr(n) if c else n}   {_sgr('Ready') if c else 'Ready'}\n" for n in sorted(self.existing))
            return self._done(cmd, kwargs, out="NAME   PHASE\n" + rows)
        if head == ["openshell", "policy", "digest"]:
            path = cmd[cmd.index("--policy") + 1]
            f = self.resolve(path)
            if not f.is_file():
                return self._done(cmd, kwargs, rc=1, err=f"policy unreadable: {path}\n")
            hexd = hashlib.sha256(f.read_bytes()).hexdigest()
            return self._done(cmd, kwargs, out=f"{_sgr(hexd) if c else hexd}  {path}\n")
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
            alias = f"openshell-{cmd[3]}"
            return self._done(cmd, kwargs, out=(
                f"Host {_sgr(alias) if c else alias}\n    User sandbox\n"
                f"    ProxyCommand openshell ssh-proxy --gateway-name nemoclaw --name {cmd[3]}\n"))
        return self._done(cmd, kwargs)


@contextlib.contextmanager
def _real_popen():
    patched = subprocess.Popen
    subprocess.Popen = _REAL_POPEN
    try:
        yield
    finally:
        subprocess.Popen = patched


@contextlib.contextmanager
def _swapped_environ(env: dict):
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


# --------------------------------------------------------------------------- one install
_HARNESS_ERRORS = (AssertionError, AttributeError, IndexError, KeyError, NameError, TypeError)
_UNSET = object()


def _install(tmp_path, monkeypatch, inst, case, spec, broker, *, ref=SHA, policy_root=None, fork_source=_UNSET,
             path=None, phase="apply", seed=None):
    """One `apply()` (or `phase_a()`) of `spec` in the home `<tmp>/<case>/home`, every subprocess answered by
    `broker`. `seed(base)` prepares the home first. Returns the refusal, or None when it completed. On the stock
    tree a `fork_source=` keyword raises TypeError, which is re-raised (red)."""
    base = tmp_path / case
    for d in ("user", "home", "managed"):
        (base / d).mkdir(parents=True, exist_ok=True)
    (tmp_path / "empty-bundled").mkdir(exist_ok=True)
    if seed is not None:
        seed(base)
    kw = {} if fork_source is _UNSET else {"fork_source": fork_source}
    with monkeypatch.context() as mp:
        mp.setenv("HOME", str(base / "user"))
        mp.setenv("HERMES_HOME", str(base / "home"))
        mp.setenv("HERMES_MANAGED_DIR", str(base / "managed"))
        mp.setenv("HERMES_BUNDLED_PLUGINS", str(tmp_path / "empty-bundled"))
        mp.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "0")
        if path is not None:
            mp.setenv("PATH", path)
        mp.setattr(subprocess, "run", broker.run)
        mp.setattr(subprocess, "Popen", broker.popen)
        mp.setattr(time, "sleep", lambda _s: None)
        try:
            if phase == "phase_a":
                inst.phase_a(str(spec), ref, **kw)
            else:
                inst.apply(str(spec), ref, room_creator=lambda *_a: None, room_exists=lambda _rid: False,
                           policy_root=None if policy_root is None else str(policy_root), **kw)
        except _HARNESS_ERRORS:
            raise
        except Exception as exc:  # noqa: BLE001 - a refusal is the observable
            return exc
    return None


def _no_openshell_path() -> str:
    return os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep)
                           if p and not (Path(p) / "openshell").exists())


def _seed_gates_plugin(base: Path) -> None:
    dst = base / "home" / "plugins" / GATES_KEY
    if not dst.exists():
        shutil.copytree(REPO_ROOT / "plugins" / GATES_KEY, dst)


def _seed_routes(script: str, body, *, in_root: bool = True):
    """The ship's hand-configured routes (ADR B6) in the gateway home, the script only in the root scripts/."""
    def seed(base: Path) -> None:
        home = base / "home"
        routes = {
            "github": {"events": ["issues"], "deliver": "log", "secret": "s-1", "prompt": "summarize"},
            "github-orch": {"events": ["issues"], "deliver": "log", "secret": "s-2", "prompt": "summarize",
                            "profile": "orchestrator", "script": script},
        }
        _dump_yaml(home / "config.yaml", {"platforms": {"webhook": {"enabled": True, "extra": {"port": 8644, "routes": routes}}}})
        if in_root:
            (home / "scripts").mkdir(parents=True, exist_ok=True)
            (home / "scripts" / script).write_text(body, encoding="utf-8")
    return seed


def _profile_dir(out: Path, role: str) -> Path:
    dirs = [p.parent for p in out.glob("*/config.yaml") if p.parent.name != "managed"]
    hits = [d for d in dirs if d.name == role or d.name.endswith("-" + role)]
    assert hits, f"no rendered profile dir for {role!r} under {out}"
    return hits[0]


def _types(tree: str) -> list:
    return list((_load_yaml(SPEC_TREES[tree] / "coworker-types.yaml").get("types") or {}))


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    """Both specs of record rendered by `hermes coworker compose` from an isolated home (tree → out dir)."""
    base = tmp_path_factory.mktemp("upg-f69sa-render")
    home = base / "home" / ".hermes"
    (home / "plugins").mkdir(parents=True)
    keys = (COMPOSE_KEY, GATES_KEY, "nv-ingress")
    for key in keys:
        shutil.copytree(REPO_ROOT / "plugins" / key, home / "plugins" / key)
    _dump_yaml(home / "config.yaml", {"plugins": {"enabled": list(keys)}})
    (base / "empty-bundled").mkdir()
    env = dict(os.environ, HOME=str(home.parent), HERMES_HOME=str(home),
               HERMES_BUNDLED_PLUGINS=str(base / "empty-bundled"), HERMES_ENABLE_PROJECT_PLUGINS="0")
    outs = {}
    for tree, src in SPEC_TREES.items():
        dst = base / "out" / tree
        proc = subprocess.run([sys.executable, "-m", "hermes_cli.main", "coworker", "compose",
                               str(src / "coworker-types.yaml"), "--out", str(dst)],
                              capture_output=True, text=True, env=env, cwd=str(REPO_ROOT), check=False)
        assert proc.returncode == 0, f"{tree}: compose failed:\n{proc.stdout[-1500:]}\n{proc.stderr[-1500:]}"
        outs[tree] = dst
    return outs


def _skill(out: Path, role: str, workflow: str) -> str:
    path = _profile_dir(out, role) / "skills" / workflow / "SKILL.md"
    assert path.is_file(), f"rendered {role} profile has no {workflow} workflow at {path}"
    return path.read_text(encoding="utf-8")


_TRIAGE_POST = re.compile(r"gh issue comment (\S+) --repo (\S+) --body-file (\S+)")
_REVIEW_POST = re.compile(r"gh pr review (\S+) --repo (\S+) --comment --body-file (\S+)")


def _names_restart(argv) -> bool:
    """True for `hermes gateway restart` in any launcher form (bare, `setsid -f …`, a shell wrapper)."""
    flat = " ".join(argv)
    return "hermes gateway restart" in flat or ("hermes" in argv and "restart" in argv and "gateway" in argv)


# =========================================================================== item 1
def test_ac_upg_f69_s_a_1(installer, tmp_path, monkeypatch):
    """(item 1) The installed DEFAULT config has a webhook route bound to a coworker profile whose relative `script:` exists only in the gateway root `scripts/` (the shipped `github-orch` → `orchestrator` → `github_to_botchat.py` shape). After apply, `<root>/profiles/<profile>/scripts/<script>` is byte-identical to the root copy, the v0.22.5 resolver run in that profile's home returns it, and a second apply leaves it byte-identical."""
    spec, lane = _lane_spec(tmp_path)
    body = "#!/usr/bin/env python3\nprint('{}')\n"
    broker = _Broker(_roles(installer, spec), lane=lane, scratch=tmp_path)
    assert _install(tmp_path, monkeypatch, installer, "r1", spec, broker,
                    seed=_seed_routes("github_to_botchat.py", body)) is None
    home = tmp_path / "r1" / "home"
    target = home / "profiles" / "orchestrator" / "scripts" / "github_to_botchat.py"
    assert target.is_file(), "the install left the routed script outside the bound profile's scripts/"
    assert _sha(target) == _sha(home / "scripts" / "github_to_botchat.py")
    with monkeypatch.context() as mp:
        mp.setenv("HERMES_HOME", str(home / "profiles" / "orchestrator"))
        from gateway.platforms.webhook_filters import _resolve_script_path

        found, err = _resolve_script_path("github_to_botchat.py")
    assert err is None and Path(found) == target.resolve(), (found, err)
    first = _sha(target)
    broker2 = _Broker(_roles(installer, spec), lane=lane, scratch=tmp_path, existing=broker.existing)
    assert _install(tmp_path, monkeypatch, installer, "r1", spec, broker2) is None
    assert _sha(target) == first


def test_ac_upg_f69_s_a_2(installer, tmp_path, monkeypatch):
    """(item 1, derived) An installed DEFAULT route whose relative `script:` has no source (neither in the render nor in the gateway root `scripts/`) makes apply refuse before `hermes gateway restart`, naming the route, the script and the profile."""
    spec, lane = _lane_spec(tmp_path)
    broker = _Broker(_roles(installer, spec), lane=lane, scratch=tmp_path)
    exc = _install(tmp_path, monkeypatch, installer, "r2", spec, broker,
                   seed=_seed_routes("missing_route_script.py", "", in_root=False))
    assert exc is not None, "a route whose script exists nowhere installed silently"
    msg = str(exc)
    assert "github-orch" in msg and "missing_route_script.py" in msg and "orchestrator" in msg, msg
    restarts = [c for c in broker.popened + broker.raw if _names_restart(c)]
    assert restarts == [], f"the gateway was restarted with a broken route: {restarts}"


# =========================================================================== item 3
def test_ac_upg_f69_s_a_3(installer, tmp_path, monkeypatch):
    """(item 3) Applying the lane spec (no `wires:` key, like the ship, B5) on an empty gateway home issues `hermes wire add` for exactly the five pairs of its sibling `wire-manifest.txt`. Run through nv-fleet-gates' own `wire` handler, those writes make `message_agent` pass the wired-only gate along every manifest pair, in both directions, and the gate still refuses an unwired pair."""
    spec, lane = _lane_spec(tmp_path)
    assert "wires" not in _load_yaml(spec)
    pairs = _manifest_pairs(spec.parent)
    broker = _Broker(_roles(installer, spec), lane=lane, scratch=tmp_path, real_wires=True)
    assert _install(tmp_path, monkeypatch, installer, "w1", spec, broker, seed=_seed_gates_plugin) is None
    adds = broker.wire_adds()
    assert len(adds) == 5 and {frozenset(c[3:5]) for c in adds} == pairs, adds
    home = tmp_path / "w1" / "home"
    settings = _load_yaml(home / "config.yaml")["plugins"]["entries"][GATES_KEY]["settings"]
    assert not str(settings["edges_db_path"]).startswith("/tmp"), "harness guard: never open the real /tmp store"
    probe = {**settings, "enforce_sandbox": False, "plan_gate": False}
    members = sorted({r for p in pairs for r in p})
    for a in members:
        manager, _ = _gates(tmp_path, monkeypatch, home / "profiles" / a, probe)
        for b in members:
            if b == a:
                continue
            verdict = _gate(manager, "message_agent", {"target": b, "message": "hi"})
            if frozenset((a, b)) in pairs:
                assert verdict is None or verdict.get("action") != "block", (a, b, verdict)
            else:
                assert verdict and verdict.get("action") == "block" and "wired-only" in verdict["message"], (a, b)


def test_ac_upg_f69_s_a_4(installer, tmp_path, monkeypatch):
    """(item 3) The installer's already-wired check reads the store the spec targets, resolved like the gate and the `wire` CLI resolve it. On a re-install over a home whose INSTALLED config still names an older absolute `edges_db_path` holding all five pairs, all five `hermes wire add` are issued. A second apply issues none."""
    spec, lane = _lane_spec(tmp_path)
    pairs = _manifest_pairs(spec.parent)
    old = tmp_path / "old-tmp" / "hermes-nanoclaw-base" / "edges.db"   # stands in for the ship's /tmp store
    edges = _edges_module()
    for a, b in (sorted(p) for p in pairs):
        edges.add_edge(str(old), a, b)

    def seed(base):
        _seed_gates_plugin(base)
        _dump_yaml(base / "home" / "config.yaml",
                   {"plugins": {"enabled": [GATES_KEY], "entries": {GATES_KEY: {"settings": {"edges_db_path": str(old)}}}}})

    broker = _Broker(_roles(installer, spec), lane=lane, scratch=tmp_path, real_wires=True)
    assert _install(tmp_path, monkeypatch, installer, "w2", spec, broker, seed=seed) is None
    adds = broker.wire_adds()
    assert len(adds) == 5 and {frozenset(c[3:5]) for c in adds} == pairs, \
        "the old store the installed config names suppressed writes into the spec's target store"
    again = _Broker(_roles(installer, spec), lane=lane, scratch=tmp_path, real_wires=True, existing=broker.existing)
    assert _install(tmp_path, monkeypatch, installer, "w2", spec, again) is None
    assert again.wire_adds() == [], "a re-install re-added edges the target store already holds"


def test_ac_upg_f69_s_a_5(rendered, tmp_path, monkeypatch):
    """(item 3) For both specs, the rendered fleet's `edges_db_path` resolves to ONE file under the gateway's Hermes root. That holds for nv-fleet-gates loaded with `HERMES_HOME=<root>` (default) and with `HERMES_HOME=<root>/profiles/<role>` for every role; the file is never under `/tmp`."""
    edges = _edges_module()
    for tree, out in rendered.items():
        roles = _types(tree)
        settings = {p: _load_yaml(_profile_dir(out, p) / "config.yaml")["plugins"]["entries"][GATES_KEY]["settings"]
                    for p in ["default", *roles]}
        values = {str(s["edges_db_path"]) for s in settings.values()}
        assert len(values) == 1, f"{tree}: profiles disagree on edges_db_path: {values}"
        assert not next(iter(values)).startswith("/tmp"), f"{tree}: the edges store is in /tmp: {values}"
        root = tmp_path / f"root-{tree}"
        ring = [sorted(p) for p in _manifest_pairs(SPEC_TREES[tree])]
        manager, _ = _gates(tmp_path, monkeypatch, root, {**settings["default"], "enforce_sandbox": False, "plan_gate": False})
        for a, b in ring:
            _wire(manager, "add", a, b)
        stores = [db for db in root.rglob("*.db") if _is_edges_store(edges, db)]
        assert len(stores) == 1 and stores[0].resolve().is_relative_to(root.resolve()), stores
        for role in roles:
            partner = next(b if a == role else a for a, b in ring if role in (a, b))
            mgr, _ = _gates(tmp_path, monkeypatch, root / "profiles" / role,
                            {**settings[role], "enforce_sandbox": False, "plan_gate": False})
            verdict = _gate(mgr, "message_agent", {"target": partner, "message": "hi"})
            assert verdict is None or verdict.get("action") != "block", (tree, role, partner, verdict)


def _is_edges_store(edges, db: Path) -> bool:
    try:
        return bool(edges.list_edges(str(db)))
    except Exception:  # noqa: BLE001 - another plugin's db
        return False


# =========================================================================== item 4
def test_ac_upg_f69_s_a_6(installer, tmp_path, monkeypatch):
    """(item 4) With the lane spec's `ssh_lane` declared and no `openshell` on PATH, every openshell invocation the installer makes runs through the lane: its argv sources `<bundle>/<env_file>` and execs `<interpreter> <bundle>/<shim>` with the openshell arguments. No bare `openshell` argv is spawned."""
    spec, lane = _lane_spec(tmp_path)
    broker = _Broker(_roles(installer, spec), lane=lane, scratch=tmp_path)
    assert _install(tmp_path, monkeypatch, installer, "l6", spec, broker, path=_no_openshell_path()) is None
    bare = [c for c in broker.raw if Path(c[0]).name == "openshell"]
    assert bare == [], f"bare openshell argvs: {bare[:3]}"
    assert broker.wrapped, "the installer made no openshell call at all"
    for call in broker.wrapped:
        assert call["sources_env"] and call["interpreter"] == lane["interpreter"], call["argv"]


def test_ac_upg_f69_s_a_7(installer, tmp_path, monkeypatch):
    """(item 4) For each spec of record's own `ssh_lane` declaration (only `bundle` re-pointed at a test copy), an apply of the lane spec persists, for every `openshell-<project>-<role>` alias, exactly the B1 lane line `ProxyCommand /bin/sh -c '. <bundle>/<env_file> && exec <interpreter> <bundle>/<shim> <args>'`, where `<args>` are the `ssh-proxy …` arguments openshell rendered. No persisted ssh line invokes a bare `openshell`, even when the broker SGR-colours its `ssh-config` output."""
    for tree in SPEC_TREES:
        sub = tmp_path / tree
        sub.mkdir()
        spec, lane = _lane_spec(sub, tree, inject=False)
        assert lane is not None, f"{tree}: the spec of record declares no ssh_lane"
        roles = _roles(installer, spec)
        broker = _Broker(roles, lane=lane, color=True, scratch=sub)
        assert _install(sub, monkeypatch, installer, "l7", spec, broker) is None
        include = sub / "l7" / "user" / ".ssh" / "config.d" / f"openshell-{PROJECT}.conf"
        text = include.read_text(encoding="utf-8")
        assert _ESC not in text, f"{tree}: SGR bytes persisted into the ssh config"
        assert not re.search(r"(?im)^\s*proxycommand\s+openshell\b", text), f"{tree}: a bare openshell ProxyCommand"
        got = _proxy_commands(include)
        for role in roles:
            sandbox = f"{PROJECT}-{role}"
            assert got.get(f"openshell-{sandbox}") == _lane_line(lane, sandbox), (tree, role, got.get(f"openshell-{sandbox}"))


def test_ac_upg_f69_s_a_8(installer, tmp_path, monkeypatch):
    """(item 4, derived) A spec whose declared `ssh_lane` bundle lacks a declared file (env file, shim or member), or has one empty, refuses before any `openshell sandbox create`, `replace` or `delete`, naming the missing path."""
    probe = tmp_path / "probe-lane"
    probe.mkdir()
    _, lane = _lane_spec(probe)
    for case, missing in (("m-env", lane["env_file"]), ("m-shim", lane["shim"]), ("m-member", (lane.get("members") or ["?"])[0])):
        sub = tmp_path / case
        sub.mkdir()
        spec, sub_lane = _lane_spec(sub, drop=(missing,))
        broker = _Broker(_roles(installer, spec), lane=sub_lane, scratch=sub)
        exc = _install(sub, monkeypatch, installer, "inst", spec, broker)
        assert exc is not None, f"{case}: an incomplete lane bundle installed"
        assert str(Path(sub_lane["bundle"]) / missing) in str(exc), (case, str(exc))
        assert broker.sandbox_ops() == [], f"{case}: sandbox ops before the refusal: {broker.sandbox_ops()}"
    sub = tmp_path / "m-empty"
    sub.mkdir()
    spec, sub_lane = _lane_spec(sub)
    (Path(sub_lane["bundle"]) / sub_lane["env_file"]).write_text("", encoding="utf-8")
    broker = _Broker(_roles(installer, spec), lane=sub_lane, scratch=sub)
    exc = _install(sub, monkeypatch, installer, "inst", spec, broker)
    assert exc is not None and str(Path(sub_lane["bundle"]) / sub_lane["env_file"]) in str(exc), exc
    assert broker.sandbox_ops() == []


# =========================================================================== item 5
def test_ac_upg_f69_s_a_9(installer, tmp_path, monkeypatch):
    """(item 5) When every `openshell sandbox list` answer SGR-wraps its NAME and PHASE columns, a re-install over a home whose five worker sandboxes already exist issues zero `openshell sandbox create`, `replace` or `delete`."""
    spec, lane = _lane_spec(tmp_path)
    roles = _roles(installer, spec)
    names = {f"{PROJECT}-{r}": r for r in roles}

    def seed(base):
        records = base / "home" / ".osh-f64" / "sandbox-policy"
        records.mkdir(parents=True, exist_ok=True)
        for name, role in names.items():   # each live sandbox was created with the current render
            (records / f"{name}.sha256").write_text(hashlib.sha256(_render_bytes(role)).hexdigest() + "\n", encoding="utf-8")

    broker = _Broker(roles, lane=lane, color=True, existing=names, scratch=tmp_path)
    exc = _install(tmp_path, monkeypatch, installer, "a9", spec, broker, seed=seed)
    assert broker.sandbox_ops() == [], f"a coloured listing hid live sandboxes: {broker.sandbox_ops()[:2]}"
    assert exc is None, exc


def test_ac_upg_f69_s_a_10(installer, tmp_path, monkeypatch):
    """(item 5) When `openshell policy digest` answers with an SGR-coloured sha256sum line, a rooted install binds every create with `--policy-sha256 <that role's render digest>`."""
    spec, lane = _lane_spec(tmp_path)
    roles = _roles(installer, spec)
    root, view = tmp_path / "broker-root", tmp_path / "a10-host-root"   # root is the broker's; never readable here
    for r in roles:
        f = view / "render" / r / f"policy-{r}.yaml"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(_render_bytes(r))
    broker = _Broker(roles, lane=lane, color=True, root=root, view=view, scratch=tmp_path)
    exc = _install(tmp_path, monkeypatch, installer, "a10", spec, broker, policy_root=root)
    assert exc is None, exc
    want = {f"{PROJECT}-{r}": hashlib.sha256(_render_bytes(r)).hexdigest() for r in roles}
    creates = [c for c in broker.sandbox_ops() if c[2] == "create"]
    assert len(creates) == len(roles)
    for c in creates:
        assert c[c.index("--policy-sha256") + 1] == want[c[c.index("--name") + 1]], c
    assert broker.bound == want


# =========================================================================== item 6
def _git(*args, cwd=None):
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_NOSYSTEM="1",
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    return _REAL_RUN([_REAL_GIT, *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def _fork_repos(tmp_path: Path, plugins):
    """A sealed mirror at the base commit, a fork source holding one more commit (the row head), and the image's
    PATH-first git wrapper (ADR B4) that maps the plugin source URL onto the sealed mirror. Returns
    (sealed, source, wrapper dir, head sha)."""
    sealed = tmp_path / "opt-hermes" / "fork"
    sealed.mkdir(parents=True)
    _git("init", "-q", "-b", "main", str(sealed))
    for name in plugins:
        f = sealed / "plugins" / name / "plugin.yaml"
        f.parent.mkdir(parents=True)
        f.write_text(f"name: {name}\n", encoding="utf-8")
    _git("-C", str(sealed), "add", "-A")
    _git("-C", str(sealed), "commit", "-q", "-m", "sealed base")
    work = tmp_path / "row-work"
    _git("clone", "-q", str(sealed), str(work))
    (work / "plugins" / plugins[0] / "ROW-HEAD").write_text("row head\n", encoding="utf-8")
    _git("-C", str(work), "add", "-A")
    _git("-C", str(work), "commit", "-q", "-m", "row head")
    head = _git("-C", str(work), "rev-parse", "HEAD")
    source = tmp_path / "fork-source.git"
    _git("init", "-q", "--bare", str(source))
    _git("--git-dir", str(source), "fetch", "-q", str(work), "HEAD:refs/heads/row-head")
    _git("--git-dir", str(source), "config", "uploadpack.allowAnySHA1InWant", "true")
    imagebin = tmp_path / "imagebin"
    imagebin.mkdir()
    wrapper = imagebin / "git"
    wrapper.write_text(f'#!/bin/sh\nexec {_REAL_GIT} -c url.{sealed}.insteadOf={FORK_URL} '
                       f'-c url.{sealed}.insteadOf={FORK_URL[:-4]} "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    return sealed, source, imagebin, head


def _tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_symlink():
            h.update(f"L {rel} {os.readlink(p)}\n".encode())
        elif p.is_file():
            h.update(f"F {rel} {stat.S_IMODE(p.stat().st_mode):o}\n".encode())
            h.update(p.read_bytes())
        else:
            h.update(f"D {rel}\n".encode())
    return h.hexdigest()


def test_ac_upg_f69_s_a_11(installer, tmp_path, monkeypatch):
    """(item 6) The lane's PATH-first git wrapper (the image's, B4) maps the plugin source URL to a sealed mirror that lacks `--ref`; the install is given `fork_source` holding it. Every `hermes plugins install … --ref <ref>` of phase-a and of apply then resolves `<ref>` through an installer-owned writable mirror and git wrapper under `<HERMES_HOME>/openshell/`. That holds with the Hermes clone code path itself (`noninteractive_git_env` included). The sealed mirror's files and refs are byte-unchanged."""
    assert _REAL_GIT, "git is required"
    spec, lane = _lane_spec(tmp_path)
    plugins = list(installer.enabled_plugins(_load_yaml(spec), str(spec)))
    sealed, source, imagebin, head = _fork_repos(tmp_path, plugins)
    sealed_before, wrapper_before = _tree_digest(sealed), _sha(imagebin / "git")
    path = f"{imagebin}{os.pathsep}{os.environ.get('PATH', '')}"
    for phase, case in (("phase_a", "f11-phase-a"), ("apply", "f11-apply")):
        broker = _Broker(_roles(installer, spec), lane=lane, clone=True, scratch=tmp_path)
        exc = _install(tmp_path, monkeypatch, installer, case, spec, broker, ref=head,
                       fork_source=str(source), path=path, phase=phase)
        assert exc is None, f"{phase}: {exc}"
        assert broker.installs, f"{phase}: no plugin install ran"
        owned = (tmp_path / case / "home" / "openshell").resolve()
        for rec in broker.installs:
            assert rec["head"] == head, (phase, rec["cmd"], rec["head"])
            assert rec["git"] and Path(rec["git"]).resolve().is_relative_to(owned), (phase, rec["git"])
    assert _tree_digest(sealed) == sealed_before, "the sealed mirror was written"
    assert _sha(imagebin / "git") == wrapper_before, "the image's git wrapper was written"


def test_ac_upg_f69_s_a_12(installer, tmp_path, monkeypatch):
    """(item 6, derived) When `--ref` is in neither the sealed mirror nor the given `fork_source` (or none is given), apply refuses before any `hermes plugins install` and any sandbox op, naming the ref and the sealed mirror path."""
    assert _REAL_GIT, "git is required"
    spec, lane = _lane_spec(tmp_path)
    plugins = list(installer.enabled_plugins(_load_yaml(spec), str(spec)))
    sealed, source, imagebin, head = _fork_repos(tmp_path, plugins)
    path = f"{imagebin}{os.pathsep}{os.environ.get('PATH', '')}"
    nowhere = "c0ffee" + "0" * 34
    for case, ref, kw in (("f12-nowhere", nowhere, {"fork_source": str(source)}), ("f12-no-source", head, {})):
        broker = _Broker(_roles(installer, spec), lane=lane, clone=True, scratch=tmp_path)
        exc = _install(tmp_path, monkeypatch, installer, case, spec, broker, ref=ref, path=path, **kw)
        assert exc is not None, f"{case}: installed a ref the sealed mirror lacks"
        assert ref in str(exc) and str(sealed) in str(exc), (case, str(exc))
        assert broker.plugin_installs() == [], f"{case}: a plugin install ran before the refusal"
        assert broker.sandbox_ops() == [], f"{case}: a sandbox op ran before the refusal"


# =========================================================================== item 7
def test_ac_upg_f69_s_a_13(installer, tmp_path, monkeypatch):
    """(item 7) A re-install over a home whose ssh include already carries the B1 lane ProxyCommands (the `f2s-lane-reapply.sh` state, nothing else changed) keeps every alias's ProxyCommand on that lane line. No alias is reverted to a bare `openshell`."""
    spec, lane = _lane_spec(tmp_path)
    roles = _roles(installer, spec)
    include_rel = f"config.d/openshell-{PROJECT}.conf"

    def seed(base):
        ssh = base / "user" / ".ssh"
        (ssh / "config.d").mkdir(parents=True, exist_ok=True)
        blocks = "".join(
            f"# >>> openshell-{PROJECT}-{r}\nHost openshell-{PROJECT}-{r}\n    User sandbox\n"
            f"    ProxyCommand {_lane_line(lane, f'{PROJECT}-{r}')}\n# <<< openshell-{PROJECT}-{r}\n" for r in roles)
        (ssh / include_rel).write_text(blocks, encoding="utf-8")
        (ssh / "config").write_text(f"Include {include_rel}\n", encoding="utf-8")

    broker = _Broker(roles, lane=lane, scratch=tmp_path)
    assert _install(tmp_path, monkeypatch, installer, "s13", spec, broker, seed=seed) is None
    include = tmp_path / "s13" / "user" / ".ssh" / include_rel
    got = _proxy_commands(include)
    for r in roles:
        sandbox = f"{PROJECT}-{r}"
        assert got.get(f"openshell-{sandbox}") == _lane_line(lane, sandbox), (r, got.get(f"openshell-{sandbox}"))
    assert not re.search(r"(?im)^\s*proxycommand\s+openshell\b", include.read_text(encoding="utf-8"))


# =========================================================================== items 8 and 9 (fleet content)
def test_ac_upg_f69_s_a_14(rendered):
    """(item 8) For both specs, the rendered triager profile's `triage-issue` workflow tells it to post the outcome on the issue by running `gh issue comment <n> --repo <owner>/<repo> --body-file <file>` through `terminal`."""
    for tree, out in rendered.items():
        text = _skill(out, "triager", "triage-issue")
        assert _TRIAGE_POST.search(text), f"{tree}: the rendered triage-issue workflow names no issue-comment post"
        assert "terminal" in text, f"{tree}: the post is not run through terminal"


def test_ac_upg_f69_s_a_15(rendered):
    """(item 9) For both specs, the rendered reviewer profile's `plan` workflow tells it, after its findings (and, in ING-F66, the ci-gate verdict), to run `codex_critique` with stage OUTPUT_REVIEW. It then posts the review with `gh pr review <N> --repo <owner>/<repo> --comment --body-file <file>`."""
    for tree, out in rendered.items():
        text = _skill(out, "reviewer", "plan")
        post = _REVIEW_POST.search(text)
        assert post, f"{tree}: the rendered reviewer plan workflow names no comment-mode review post"
        before = text[:post.start()]
        assert "codex_critique" in before and "OUTPUT_REVIEW" in before, \
            f"{tree}: no OUTPUT_REVIEW critique is named before the review post"


def test_ac_upg_f69_s_a_16(rendered, tmp_path, monkeypatch):
    """(item 9, derived) The exact review command the rendered reviewer skill names, with its placeholders filled, is refused by nv-fleet-gates' `pre_tool_call` without a fresh OUTPUT_REVIEW critique in the session, and allowed once one is recorded."""
    for tree, out in rendered.items():
        post = _REVIEW_POST.search(_skill(out, "reviewer", "plan"))
        assert post, f"{tree}: no review command to drive"
        command = post.group(0)
        for group, value in zip(post.groups(), ("7", "slang-coworkers/nanoclaw", str(tmp_path / "review.md"))):
            command = command.replace(group, value, 1)
        settings = _load_yaml(_profile_dir(out, "reviewer") / "config.yaml")["plugins"]["entries"][GATES_KEY]["settings"]
        home = tmp_path / f"g16-{tree}" / "profiles" / "reviewer"
        probe = {**settings, "enforce_sandbox": False, "plan_gate": False,
                 "edges_db_path": str(tmp_path / f"g16-{tree}" / "edges.db"),
                 "skills_root": str(home / "skills"), "shared_learnings_path": str(home / "shared-learnings")}
        manager, loaded = _gates(tmp_path, monkeypatch, home, probe)
        session = f"s16-{tree}"
        refused = _gate(manager, "terminal", {"command": command}, session_id=session)
        assert refused and refused.get("action") == "block" and "critique" in refused["message"], (tree, refused)
        loaded.module.stores.record_critique(session, "OUTPUT_REVIEW")
        allowed = _gate(manager, "terminal", {"command": command}, session_id=session)
        assert allowed is None or allowed.get("action") != "block", (tree, allowed)


_APPROVAL = re.compile(r"--approve\b|event=APPROVE|grants? the final approval|exit 0:\s*approve\b", re.IGNORECASE)


def test_ac_upg_f69_s_a_17(rendered):
    """(owner ruling, derived) No rendered profile of either spec instructs a PR approval: no `--approve`, no `event=APPROVE`, no "grants the final approval". The ING-F66 approver's `approve` workflow hands a checks verdict to the human approver, and the approver carries no `github` provider."""
    for tree, out in rendered.items():
        for skill in sorted(out.glob("*/skills/**/SKILL.md")):
            hit = _APPROVAL.search(skill.read_text(encoding="utf-8"))
            assert hit is None, f"{tree}: {skill.relative_to(out)} instructs a PR approval ({hit.group(0)!r})"
        approver = (_load_yaml(SPEC_TREES[tree] / "coworker-types.yaml").get("types") or {}).get("approver") or {}
        assert "github" not in (approver.get("providers") or []), f"{tree}: the approver carries the github provider"
    approve = _skill(rendered["ING-F66"], "approver", "approve")
    assert "human approver" in approve.lower(), "the approver's checks verdict is not handed to the human approver"

