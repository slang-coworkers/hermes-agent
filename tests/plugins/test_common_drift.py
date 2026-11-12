"""scripts/common_drift.py on throwaway git repos: identical, diverged, missing, unreachable, recorded hashes."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "common_drift.py"
PATHS = "common/COMMON_PATHS\nplugins/demo\ntests/plugins/test_demo*.py\n"
BASE = {
    "common/COMMON_PATHS": PATHS,
    "plugins/demo/__init__.py": "VALUE = 1\n",
    "plugins/demo/plugin.yaml": "name: demo\n",
    "plugins/demo/blob.bin": b"\x00\x01\x02",
    "tests/plugins/test_demo_unit.py": "def test_ok():\n    assert True\n",
    "plugins/not-shared/__init__.py": "LOCAL = 1\n",
}


@pytest.fixture
def git_env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "drift-test",
        "GIT_AUTHOR_EMAIL": "drift-test@example.invalid",
        "GIT_COMMITTER_NAME": "drift-test",
        "GIT_COMMITTER_EMAIL": "drift-test@example.invalid",
    }


def _git(env, *args):
    proc = subprocess.run(["git", *args], env=env, capture_output=True, check=True)
    return proc.stdout.decode().strip()


def _commit(env, repo: Path, files: dict, message: str) -> str:
    for rel, content in files.items():
        path = repo / rel
        if content is None:
            path.unlink()
        elif isinstance(content, bytes):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    _git(env, "-C", str(repo), "add", "-A")
    _git(env, "-C", str(repo), "commit", "-q", "-m", message)
    return _git(env, "-C", str(repo), "rev-parse", "HEAD")


def _pin(repo: Path, sha: str, url: str = "", name: str = "other") -> None:
    (repo / "common" / "COMMON_PIN").write_text(f"name={name}\nrepo={url}\nsha={sha}\n", encoding="utf-8")


def _drift(env, root: Path, *args: str, extra_env: dict | None = None):
    report_file = root.parent / "report.json"
    report_file.unlink(missing_ok=True)
    proc = subprocess.run([sys.executable, str(SCRIPT), "--root", str(root), "--json", str(report_file), *args],
                          env={**env, **(extra_env or {})}, capture_output=True, encoding="utf-8", check=False)
    report = json.loads(report_file.read_text(encoding="utf-8-sig")) if report_file.exists() else None
    return proc, report


@pytest.fixture
def pair(tmp_path, git_env):
    """A local repo and a served other repo with the same shared files and different commits."""
    other = tmp_path / "other"
    _git(git_env, "init", "-q", str(other))
    _git(git_env, "-C", str(other), "config", "uploadpack.allowFilter", "true")
    _git(git_env, "-C", str(other), "config", "uploadpack.allowAnySHA1InWant", "true")
    other_sha = _commit(git_env, other, BASE, "other")
    local = tmp_path / "local"
    _git(git_env, "init", "-q", str(local))
    _commit(git_env, local, {**BASE, "plugins/not-shared/__init__.py": "LOCAL = 2\n"}, "local")
    _pin(local, other_sha, other.as_uri())
    return local, other, other_sha


def _by_path(report):
    return {row["path"]: row for row in report["files"]}


def test_identical_is_clean_even_strict(pair, git_env):
    local, _other, _sha = pair
    proc, report = _drift(git_env, local, "--strict")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (report["status"], report["mode"]) == ("CLEAN", "fetched")
    assert report["summary"]["files"] == report["summary"]["identical"] == 5
    assert "plugins/not-shared/__init__.py" not in _by_path(report)
    assert "status=CLEAN" in proc.stdout


def test_other_url_from_env_when_the_pin_has_none(pair, git_env):
    local, other, sha = pair
    _pin(local, sha)
    proc, report = _drift(git_env, local, extra_env={"COMMON_OTHER_URL": other.as_uri()})
    assert proc.returncode == 0, proc.stderr
    assert (report["status"], report["mode"]) == ("CLEAN", "fetched")


def test_diverged_counts_lines_and_fails_strict(pair, git_env):
    local, other, _sha = pair
    new_sha = _commit(git_env, other, {"plugins/demo/__init__.py": "VALUE = 2\nEXTRA = 3\n",
                                       "plugins/demo/blob.bin": b"\x00\x09"}, "diverge")
    _pin(local, new_sha, other.as_uri())
    proc, report = _drift(git_env, local)
    assert proc.returncode == 0, proc.stderr
    assert report["status"] == "DRIFT"
    rows = _by_path(report)
    assert (rows["plugins/demo/__init__.py"]["status"], rows["plugins/demo/__init__.py"]["added"],
            rows["plugins/demo/__init__.py"]["removed"]) == ("diverged", 2, 1)
    assert rows["plugins/demo/blob.bin"]["binary"] is True
    assert rows["plugins/demo/plugin.yaml"]["status"] == "identical"
    assert "plugins/demo/__init__.py  +2 -1" in proc.stdout
    assert _drift(git_env, local, "--strict")[0].returncode == 1


def test_missing_on_each_side_and_empty_entry(pair, git_env):
    local, other, _sha = pair
    paths = PATHS + "plugins/nothing-here\n"
    _commit(git_env, local, {"common/COMMON_PATHS": paths, "tests/plugins/test_demo_local.py": "X = 1\n"}, "local")
    new_sha = _commit(git_env, other, {"common/COMMON_PATHS": paths, "plugins/demo/extra.py": "Y = 1\n"}, "other")
    _pin(local, new_sha, other.as_uri())
    proc, report = _drift(git_env, local)
    assert proc.returncode == 0, proc.stderr
    rows = _by_path(report)
    assert rows["tests/plugins/test_demo_local.py"]["status"] == "missing-there"
    assert rows["plugins/demo/extra.py"]["status"] == "missing-here"
    entries = {entry["entry"]: entry["status"] for entry in report["entries"]}
    assert entries == {"common/COMMON_PATHS": "identical", "plugins/demo": "drift",
                       "tests/plugins/test_demo*.py": "drift", "plugins/nothing-here": "empty"}
    summary = report["summary"]
    assert (summary["missing_there"], summary["missing_here"], summary["empty_entries"]) == (1, 1, 1)
    assert "missing on other  tests/plugins/test_demo_local.py" in proc.stdout
    assert _drift(git_env, local, "--strict")[0].returncode == 1


def test_unreachable_reports_and_passes(pair, git_env, tmp_path):
    local, _other, sha = pair
    _pin(local, sha, (tmp_path / "gone.git").as_uri(), name="fork")
    proc, report = _drift(git_env, local, "--strict")
    assert proc.returncode == 0, proc.stderr
    assert (report["status"], report["mode"]) == ("UNREACHABLE", "unreachable")
    assert "fork unreachable:" in proc.stdout
    assert _drift(git_env, local, "--fail-unreachable")[0].returncode == 3


def test_no_repo_url_is_unreachable(pair, git_env):
    local, _other, sha = pair
    _pin(local, sha)
    proc, report = _drift(git_env, local)
    assert proc.returncode == 0, proc.stderr
    assert report["status"] == "UNREACHABLE"
    assert "other unreachable: no repo URL" in proc.stdout


def test_record_pin_then_compare_offline(pair, git_env, tmp_path):
    local, other, sha = pair
    proc, _ = _drift(git_env, local, "--record-pin")
    assert proc.returncode == 0, proc.stderr
    pin_text = (local / "common" / "COMMON_PIN").read_text(encoding="utf-8-sig")
    assert f"repo={other.as_uri()}\nsha={sha}\n" in pin_text
    assert pin_text.count("\nentry ") == 3
    assert _drift(git_env, local)[1]["pin_hashes"] == "ok"

    _commit(git_env, local, {"plugins/demo/__init__.py": "VALUE = 9\n"}, "local change")
    gone = {"COMMON_OTHER_URL": (tmp_path / "gone.git").as_uri()}
    proc, report = _drift(git_env, local, extra_env=gone)
    assert proc.returncode == 0, proc.stderr
    assert (report["status"], report["mode"]) == ("DRIFT", "recorded")
    assert {entry["entry"]: entry["status"] for entry in report["entries"]} == {
        "common/COMMON_PATHS": "identical", "plugins/demo": "diverged", "tests/plugins/test_demo*.py": "identical"}
    assert "other unreachable:" in proc.stdout
    assert _drift(git_env, local, "--strict", extra_env=gone)[0].returncode == 1


def test_stale_recorded_hashes_warn(pair, git_env):
    local, _other, _sha = pair
    assert _drift(git_env, local, "--record-pin")[0].returncode == 0
    pin_file = local / "common" / "COMMON_PIN"
    entry_line = next(line for line in pin_file.read_text(encoding="utf-8-sig").splitlines()
                      if line.endswith(" plugins/demo"))
    pin_text = pin_file.read_text(encoding="utf-8-sig")
    pin_file.write_text(pin_text.replace(entry_line, "entry 0000 plugins/demo"), encoding="utf-8")
    proc, report = _drift(git_env, local)
    assert report["pin_hashes"] == "stale"
    assert report["status"] == "DRIFT"
    assert "WARN: the entry hashes" in proc.stdout


def test_record_pin_needs_the_other_commit(pair, git_env, tmp_path):
    local, _other, sha = pair
    _pin(local, sha, (tmp_path / "gone.git").as_uri())
    proc, _ = _drift(git_env, local, "--record-pin")
    assert proc.returncode == 3
    assert "cannot record" in proc.stderr


def test_pin_in_the_local_object_store_needs_no_fetch(pair, git_env, tmp_path):
    local, _other, _sha = pair
    base = _git(git_env, "-C", str(local), "rev-parse", "HEAD")
    side = _commit(git_env, local, {"plugins/demo/plugin.yaml": "name: demo\nversion: 2\n"}, "side")
    _git(git_env, "-C", str(local), "reset", "-q", "--hard", base)
    _pin(local, side, (tmp_path / "gone.git").as_uri())
    proc, report = _drift(git_env, local)
    assert proc.returncode == 0, proc.stderr
    assert report["mode"] == "local"
    row = _by_path(report)["plugins/demo/plugin.yaml"]
    assert (row["status"], row["added"], row["removed"]) == ("diverged", 1, 0)


def test_unknown_sha_on_a_reachable_repo_is_a_config_error(pair, git_env):
    local, other, _sha = pair
    _pin(local, "1" * 40, other.as_uri())
    proc, report = _drift(git_env, local)
    assert proc.returncode == 2
    assert report is None
    assert "has no commit" in proc.stderr


def test_inherited_git_environment_and_config_are_ignored(pair, git_env, tmp_path):
    local, other, _sha = pair
    gone = (tmp_path / "gone.git").as_uri()
    (Path(git_env["HOME"]) / ".gitconfig").write_text(f'[url "{gone}"]\n\tinsteadOf = {other.as_uri()}\n',
                                                     encoding="utf-8")
    proc, report = _drift(git_env, local, extra_env={"GIT_DIR": str(tmp_path / "nowhere"),
                                                     "GIT_OBJECT_DIRECTORY": str(tmp_path / "nowhere")})
    assert proc.returncode == 0, proc.stderr
    assert (report["status"], report["mode"]) == ("CLEAN", "fetched")


@pytest.mark.parametrize("url", ["http://example.invalid/r.git", "ssh://git@example.invalid/r.git",
                                 "https://user:pw@example.invalid/r.git", "https://example.invalid/r.git?t=1"])
def test_repo_must_be_an_anonymous_https_url(pair, git_env, url):
    local, _other, sha = pair
    _pin(local, sha, url)
    proc, _ = _drift(git_env, local)
    assert proc.returncode == 2
    assert "anonymous https" in proc.stderr
    assert "pw@" not in proc.stderr


@pytest.mark.parametrize("value", ["nan", "inf", "0", "-5"])
def test_timeout_must_be_positive_and_finite(pair, git_env, value):
    local, _other, _sha = pair
    assert _drift(git_env, local, "--timeout", value)[0].returncode == 2


@pytest.mark.parametrize("bad", ["/abs/path", "plugins/../x", "plugins//x"])
def test_bad_paths_entry_is_a_config_error(pair, git_env, bad):
    local, _other, _sha = pair
    (local / "common" / "COMMON_PATHS").write_text(PATHS + bad + "\n", encoding="utf-8")
    proc, _ = _drift(git_env, local)
    assert proc.returncode == 2
    assert "not a repo-relative path" in proc.stderr


def test_redact_masks_credentials(monkeypatch):
    spec = importlib.util.spec_from_file_location("common_drift", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "common_drift", module)
    spec.loader.exec_module(module)
    text = module.redact("fatal: unable to access 'https://user:pass1234@example.invalid/x.git/': "
                         "Authorization: Bearer abc.def.ghi")
    assert "pass1234" not in text
    assert "abc.def.ghi" not in text
    assert "https://***@example.invalid/x.git/" in text


def _load(monkeypatch):
    spec = importlib.util.spec_from_file_location("common_drift", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "common_drift", module)
    spec.loader.exec_module(module)
    return module


class _FakeProc:
    def __init__(self, pid=424242, hang=False):
        self.pid, self.hang, self.calls = pid, hang, []

    def kill(self):
        self.calls.append("kill")

    def wait(self, timeout=None):
        self.calls.append(("wait", timeout))
        if self.hang:
            raise subprocess.TimeoutExpired("git", timeout)
        return -9


def test_kill_falls_back_to_the_process_without_killpg(monkeypatch):
    module = _load(monkeypatch)
    monkeypatch.delattr(module.os, "killpg", raising=False)
    proc = _FakeProc()
    module.kill_and_reap(proc)
    assert proc.calls == ["kill", ("wait", module.KILL_WAIT)]


def test_kill_signals_the_session_and_tolerates_a_gone_process(monkeypatch):
    module = _load(monkeypatch)
    sent = []

    def killpg(pid, sig):
        sent.append((pid, sig))
        raise ProcessLookupError

    monkeypatch.setattr(module.os, "killpg", killpg, raising=False)
    proc = _FakeProc(pid=4242)
    module.kill_and_reap(proc)
    assert sent == [(4242, getattr(module.signal, "SIGKILL", module.signal.SIGTERM))]
    assert proc.calls == [("wait", module.KILL_WAIT)]


def test_reap_wait_is_bounded(monkeypatch):
    module = _load(monkeypatch)
    monkeypatch.delattr(module.os, "killpg", raising=False)
    proc = _FakeProc(hang=True)
    module.kill_and_reap(proc)
    assert proc.calls == ["kill", ("wait", module.KILL_WAIT)]


@pytest.mark.skipif(not hasattr(os, "killpg") or not __import__("shutil").which("setsid"),
                    reason="needs a POSIX session and setsid to hold the pipe")
def test_git_timeout_returns_while_a_grandchild_holds_the_pipe(monkeypatch, tmp_path):
    import time

    module = _load(monkeypatch)
    fake = tmp_path / "bin"
    fake.mkdir()
    # The grandchild leaves the session, so the session kill misses it and it keeps the pipes open for 15 s;
    # it exits by itself. Draining or closing the pipes would make the caller wait for it.
    (fake / "git").write_text("#!/bin/sh\nsetsid sleep 15 &\nsleep 30\n", encoding="utf-8")
    (fake / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}{os.pathsep}{os.environ.get('PATH', '')}")
    started = time.monotonic()
    with pytest.raises(module.GitFailed) as err:
        module.git(tmp_path, "status", timeout=0.5)
    assert err.value.timed_out
    assert time.monotonic() - started < 5


def test_read_text_tolerates_a_bom(monkeypatch, tmp_path):
    module = _load(monkeypatch)
    paths = tmp_path / "COMMON_PATHS"
    paths.write_bytes(b"\xef\xbb\xbfplugins/demo\n")
    assert module.read_text(paths) == "plugins/demo\n"  # windows-footgun: ok (the script's own read_text, not Path.read_text)
