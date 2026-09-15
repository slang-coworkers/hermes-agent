"""Non-acceptance unit tests for bin/podman-onecli-wrap (CRED-F28).

These cover the v2 wrapper requirements that are not among the five acceptance
criteria: the nv.hermes-ppid launch label (+ accepted-launch logging and proxy
redaction), the `sweep` verb (dead-ppid removal only), and the `ps` template
rewrite `{{.Label "K"}}` -> `{{index .Labels "K"}}`. Each runs the shipped
wrapper against a stub podman — behaviour contracts, no real container runtime.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "plugins" / "podman-onecli" / "bin" / "podman-onecli-wrap"

EXPECTED_PROXY = "172.17.0.1:10255"
EXPECTED_CA = "/etc/ssl/certs/hermes-egress-ca.crt"
GOOD_PROXY = "http://x:aoc_bota@172.17.0.1:10255"
PROXY_NAMES = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
CA_NAMES = (
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "HERMES_CA_BUNDLE",
    "DENO_CERT",
)


def _record_stub(tmp_path: Path) -> Path:
    """A podman stub that records its argv line and prints a container id."""
    stub = tmp_path / "bin" / "podman-stub"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$STUB_REC"\necho deadbeefcafe\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


def _launch_env(tmp_path: Path, stub: Path, rec: Path, log: Path) -> dict:
    env = {
        **os.environ,
        "PODMAN_ONECLI_PODMAN": str(stub),
        "PODMAN_ONECLI_EXPECTED_PROXY": EXPECTED_PROXY,
        "PODMAN_ONECLI_EXPECTED_CA": EXPECTED_CA,
        "PODMAN_ONECLI_LOG": str(log),
        "STUB_REC": str(rec),
    }
    for name in PROXY_NAMES:
        env[name] = GOOD_PROXY
    for name in CA_NAMES:
        env[name] = EXPECTED_CA
    return env


@pytest.mark.linux_only
def test_wrapper_adds_nv_hermes_ppid_label(tmp_path):
    """An accepted long-lived launch gains --label nv.hermes-ppid, is logged, and
    never leaks the proxy userinfo token to the log or stderr."""
    stub = _record_stub(tmp_path)
    rec = tmp_path / "rec.txt"
    log = tmp_path / "wrapper.log"
    rec.write_text("", encoding="utf-8")
    log.write_text("", encoding="utf-8")
    ca_host = tmp_path / "ca.crt"
    ca_host.write_text("--CA--", encoding="utf-8")

    argv = ["run", "-d", "--name", "s"]
    for name in PROXY_NAMES + CA_NAMES:
        argv += ["-e", name]
    argv += ["-v", f"{ca_host}:{EXPECTED_CA}:ro", "img", "sleep", "infinity"]

    result = subprocess.run(
        [str(WRAPPER), *argv],
        env=_launch_env(tmp_path, stub, rec, log),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    forwarded = rec.read_text(encoding="utf-8")
    assert "nv.hermes-ppid=" in forwarded
    assert "sleep infinity" in forwarded
    logged = log.read_text(encoding="utf-8")
    assert "launch:" in logged and "nv.hermes-ppid=" in logged
    # The aoc_ agent token rides only in the proxy env value, never the argv/log.
    assert "aoc_bota" not in logged and "aoc_bota" not in result.stderr
    assert GOOD_PROXY not in logged and GOOD_PROXY not in result.stderr


@pytest.mark.linux_only
def test_wrapper_sweep_removes_dead_ppid_only(tmp_path):
    """`sweep` removes hermes-agent=1 containers whose nv.hermes-ppid is dead and
    leaves containers whose recorded ppid is still alive."""
    # A genuinely dead pid: a child we spawn and reap, so kill -0 fails on it.
    child = subprocess.Popen(["sh", "-c", "exit 0"])
    child.wait()
    dead_pid = child.pid
    live_pid = os.getpid()

    ps_out = f"cid_dead {dead_pid}\ncid_live {live_pid}\n"
    rm_rec = tmp_path / "rm.txt"
    rm_rec.write_text("", encoding="utf-8")

    stub = tmp_path / "bin" / "podman-stub"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text(
        '#!/bin/sh\n'
        'case "$1" in\n'
        '  ps) printf "%s" "$PS_OUT" ;;\n'
        '  rm) shift; printf "%s\\n" "$*" >> "$RM_REC" ;;\n'
        'esac\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)

    env = {
        **os.environ,
        "PODMAN_ONECLI_PODMAN": str(stub),
        "PS_OUT": ps_out,
        "RM_REC": str(rm_rec),
    }
    result = subprocess.run(
        [str(WRAPPER), "sweep"], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0
    removed = rm_rec.read_text(encoding="utf-8")
    assert "cid_dead" in removed
    assert "cid_live" not in removed


@pytest.mark.linux_only
def test_wrapper_refuses_broad_run_rm(tmp_path):
    """A `run --rm` that is not the pinned cgroup probe (here it ends `sleep
    infinity`, not `sleep 0`) is treated as a long-lived launch and refused —
    it is not allowlisted, and the real podman is never invoked."""
    stub = _record_stub(tmp_path)
    rec = tmp_path / "rec.txt"
    log = tmp_path / "wrapper.log"
    rec.write_text("", encoding="utf-8")
    log.write_text("", encoding="utf-8")
    argv = ["run", "--rm", "--cpus", "0.5", "--memory", "64m",
            "--pids-limit", "32", "img", "sleep", "infinity"]
    result = subprocess.run(
        [str(WRAPPER), *argv],
        env=_launch_env(tmp_path, stub, rec, log),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert rec.read_text(encoding="utf-8") == ""


@pytest.mark.linux_only
def test_wrapper_refuses_ro_rw_ca_mount(tmp_path):
    """A CA mount whose mode carries both `ro` and `rw` (`:ro,rw`) is not
    read-only and is refused, even though `ro` is present."""
    stub = _record_stub(tmp_path)
    rec = tmp_path / "rec.txt"
    log = tmp_path / "wrapper.log"
    rec.write_text("", encoding="utf-8")
    log.write_text("", encoding="utf-8")
    ca_host = tmp_path / "ca.crt"
    ca_host.write_text("--CA--", encoding="utf-8")

    argv = ["run", "-d", "--name", "s"]
    for name in PROXY_NAMES + CA_NAMES:
        argv += ["-e", name]
    argv += ["-v", f"{ca_host}:{EXPECTED_CA}:ro,rw", "img", "sleep", "infinity"]

    result = subprocess.run(
        [str(WRAPPER), *argv],
        env=_launch_env(tmp_path, stub, rec, log),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert rec.read_text(encoding="utf-8") == ""


@pytest.mark.linux_only
def test_wrapper_refuses_compact_env_override(tmp_path):
    """A launch that carries every required name-only -e AND a compact
    `-e=HTTPS_PROXY=<attacker proxy>` override is refused before podman runs,
    and the attacker token never reaches the log or stderr."""
    stub = _record_stub(tmp_path)
    rec = tmp_path / "rec.txt"
    log = tmp_path / "wrapper.log"
    rec.write_text("", encoding="utf-8")
    log.write_text("", encoding="utf-8")
    ca_host = tmp_path / "ca.crt"
    ca_host.write_text("--CA--", encoding="utf-8")

    argv = ["run", "-d", "--name", "s"]
    for name in PROXY_NAMES + CA_NAMES:
        argv += ["-e", name]
    argv += ["-e=HTTPS_PROXY=http://x:aoc_override@10.9.9.9:1"]
    argv += ["-v", f"{ca_host}:{EXPECTED_CA}:ro", "img", "sleep", "infinity"]

    result = subprocess.run(
        [str(WRAPPER), *argv],
        env=_launch_env(tmp_path, stub, rec, log),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert rec.read_text(encoding="utf-8") == ""
    logged = log.read_text(encoding="utf-8")
    assert "aoc_override" not in logged and "aoc_override" not in result.stderr


@pytest.mark.linux_only
def test_wrapper_refuses_env_file(tmp_path):
    """An `--env-file` on a long-lived launch is forbidden outright (its contents
    are opaque and could override a protected proxy/CA var)."""
    stub = _record_stub(tmp_path)
    rec = tmp_path / "rec.txt"
    log = tmp_path / "wrapper.log"
    rec.write_text("", encoding="utf-8")
    log.write_text("", encoding="utf-8")
    ca_host = tmp_path / "ca.crt"
    ca_host.write_text("--CA--", encoding="utf-8")

    argv = ["run", "-d", "--name", "s", "--env-file", "/tmp/evil.env"]
    for name in PROXY_NAMES + CA_NAMES:
        argv += ["-e", name]
    argv += ["-v", f"{ca_host}:{EXPECTED_CA}:ro", "img", "sleep", "infinity"]

    result = subprocess.run(
        [str(WRAPPER), *argv],
        env=_launch_env(tmp_path, stub, rec, log),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert rec.read_text(encoding="utf-8") == ""


@pytest.mark.linux_only
def test_wrapper_refuses_env_host(tmp_path):
    """`--env-host` imports the whole host env (ONECLI_API_KEY, provider creds)
    into the sandbox, so it is refused on a long-lived launch even when the
    proxy/CA arguments are otherwise valid."""
    stub = _record_stub(tmp_path)
    rec = tmp_path / "rec.txt"
    log = tmp_path / "wrapper.log"
    rec.write_text("", encoding="utf-8")
    log.write_text("", encoding="utf-8")
    ca_host = tmp_path / "ca.crt"
    ca_host.write_text("--CA--", encoding="utf-8")

    argv = ["run", "-d", "--name", "s", "--env-host"]
    for name in PROXY_NAMES + CA_NAMES:
        argv += ["-e", name]
    argv += ["-v", f"{ca_host}:{EXPECTED_CA}:ro", "img", "sleep", "infinity"]

    result = subprocess.run(
        [str(WRAPPER), *argv],
        env=_launch_env(tmp_path, stub, rec, log),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert rec.read_text(encoding="utf-8") == ""


@pytest.mark.linux_only
def test_wrapper_refuses_nonprotected_value_env(tmp_path):
    """A value-bearing env argument for a NON-protected var (e.g.
    `-e API_TOKEN=sk-live-canary`) is refused too: only name-only `-e NAME` is
    accepted, so no secret value is ever forwarded or logged."""
    stub = _record_stub(tmp_path)
    rec = tmp_path / "rec.txt"
    log = tmp_path / "wrapper.log"
    rec.write_text("", encoding="utf-8")
    log.write_text("", encoding="utf-8")
    ca_host = tmp_path / "ca.crt"
    ca_host.write_text("--CA--", encoding="utf-8")

    argv = ["run", "-d", "--name", "s"]
    for name in PROXY_NAMES + CA_NAMES:
        argv += ["-e", name]
    argv += ["-e", "API_TOKEN=sk-live-canary"]
    argv += ["-v", f"{ca_host}:{EXPECTED_CA}:ro", "img", "sleep", "infinity"]

    result = subprocess.run(
        [str(WRAPPER), *argv],
        env=_launch_env(tmp_path, stub, rec, log),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert rec.read_text(encoding="utf-8") == ""
    logged = log.read_text(encoding="utf-8")
    assert "sk-live-canary" not in logged and "sk-live-canary" not in result.stderr


@pytest.mark.linux_only
def test_wrapper_ps_rewrites_label_template(tmp_path):
    """`ps --format` with the docker backend's {{.Label "K"}} is rewritten to the
    podman-3.4.4-compatible {{index .Labels "K"}} before being forwarded."""
    stub = _record_stub(tmp_path)
    rec = tmp_path / "rec.txt"
    rec.write_text("", encoding="utf-8")
    fmt = '{{.ID}} {{.Label "hermes-egress"}}'

    result = subprocess.run(
        [str(WRAPPER), "ps", "-a", "--format", fmt],
        env={**os.environ, "PODMAN_ONECLI_PODMAN": str(stub), "STUB_REC": str(rec)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    forwarded = rec.read_text(encoding="utf-8")
    assert '{{index .Labels "hermes-egress"}}' in forwarded
    assert '{{.Label "hermes-egress"}}' not in forwarded
