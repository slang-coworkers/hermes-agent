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


def _valid_argv(ca_host: Path, *, mode: str = "ro") -> list:
    """A well-formed long-lived launch: every required name-only -e and a
    read-only CA mount, before the image; `sleep infinity` after it."""
    argv = ["run", "-d", "--name", "s"]
    for name in PROXY_NAMES + CA_NAMES:
        argv += ["-e", name]
    argv += ["-v", f"{ca_host}:{EXPECTED_CA}:{mode}", "img", "sleep", "infinity"]
    return argv


@pytest.mark.linux_only
def test_wrapper_refuses_container_run_bypass(tmp_path):
    """AC-CRED-F28-1 (dispatch): `podman container run …` is normalised to `run`
    and validated, not passed through unvalidated. A `container run -d … sleep
    infinity` with no proxy env or CA mount is refused and podman is never
    invoked (a naive first-token dispatch would forward it verbatim)."""
    stub = _record_stub(tmp_path)
    rec = tmp_path / "rec.txt"
    log = tmp_path / "wrapper.log"
    rec.write_text("", encoding="utf-8")
    log.write_text("", encoding="utf-8")

    argv = ["container", "run", "-d", "--name", "s", "img", "sleep", "infinity"]
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
@pytest.mark.parametrize("prefix", [["--log-level=error"], ["--log-level", "error"]])
def test_wrapper_refuses_global_option_prefixed_launch(tmp_path, prefix):
    """AC-CRED-F28-1 (dispatch): a leading podman global option before the verb
    — whether `--flag=value` or a `--flag value` separated form — is refused.
    Hermes always emits the verb first, so a global-prefixed launch is a bypass
    attempt and must not reach an unvalidated dispatch path."""
    stub = _record_stub(tmp_path)
    rec = tmp_path / "rec.txt"
    log = tmp_path / "wrapper.log"
    rec.write_text("", encoding="utf-8")
    log.write_text("", encoding="utf-8")
    ca_host = tmp_path / "ca.crt"
    ca_host.write_text("--CA--", encoding="utf-8")

    argv = [*prefix, *_valid_argv(ca_host)]
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
def test_wrapper_ignores_post_image_env_and_volume(tmp_path):
    """AC-CRED-F28-1 (image boundary): `-e`/`-v` tokens AFTER the image are the
    container command, not podman flags, so they do not count toward the proxy /
    CA requirement. A launch whose only proxy `-e` names sit after the image is
    refused for missing controls; podman is never invoked."""
    stub = _record_stub(tmp_path)
    rec = tmp_path / "rec.txt"
    log = tmp_path / "wrapper.log"
    rec.write_text("", encoding="utf-8")
    log.write_text("", encoding="utf-8")
    ca_host = tmp_path / "ca.crt"
    ca_host.write_text("--CA--", encoding="utf-8")

    # No pre-image -e / -v; the proxy names and CA mount are all after `img`.
    argv = ["run", "-d", "--name", "s", "img", "sleep", "infinity"]
    for name in PROXY_NAMES + CA_NAMES:
        argv += ["-e", name]
    argv += ["-v", f"{ca_host}:{EXPECTED_CA}:ro"]

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
def test_wrapper_refuses_control_plane_key_env(tmp_path):
    """AC-CRED-F28-3 (no secret in sandbox): an otherwise-valid launch that also
    carries `-e ONECLI_API_KEY` is refused — the control-plane bootstrap key
    lives in the wrapper's process env, so forwarding its name would inject it
    into the sandbox. Its value never reaches the log or stderr."""
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
    argv += ["-e", "ONECLI_API_KEY"]
    argv += ["-v", f"{ca_host}:{EXPECTED_CA}:ro", "img", "sleep", "infinity"]

    env = _launch_env(tmp_path, stub, rec, log)
    env["ONECLI_API_KEY"] = "aoc_bootstrap_canary_DO_NOT_LEAK"
    result = subprocess.run(
        [str(WRAPPER), *argv], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode != 0
    assert rec.read_text(encoding="utf-8") == ""
    logged = log.read_text(encoding="utf-8")
    assert "aoc_bootstrap_canary_DO_NOT_LEAK" not in logged
    assert "aoc_bootstrap_canary_DO_NOT_LEAK" not in result.stderr


@pytest.mark.linux_only
def test_wrapper_refuses_nonallowlisted_env(tmp_path):
    """AC-CRED-F28-3 (no secret in sandbox): a name-only `-e` for a var that is
    neither a rendered egress/CA/NO_PROXY name nor a declared provider
    placeholder is refused — only allowlisted names may ride into the sandbox,
    so an arbitrary host var cannot be forwarded by name."""
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
    argv += ["-e", "AWS_SECRET_ACCESS_KEY"]
    argv += ["-v", f"{ca_host}:{EXPECTED_CA}:ro", "img", "sleep", "infinity"]

    env = _launch_env(tmp_path, stub, rec, log)
    env["AWS_SECRET_ACCESS_KEY"] = "host-only-secret-canary"
    result = subprocess.run(
        [str(WRAPPER), *argv], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode != 0
    assert rec.read_text(encoding="utf-8") == ""


@pytest.mark.linux_only
def test_wrapper_allows_declared_provider_placeholder(tmp_path):
    """AC-CRED-F28-3 (allowlist is additive): a provider placeholder name the
    substrate declares via PODMAN_ONECLI_ALLOWED_ENV is accepted on an otherwise
    valid launch — the render forwards placeholder provider keys, not secrets."""
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
    argv += ["-e", "ANTHROPIC_API_KEY"]
    argv += ["-v", f"{ca_host}:{EXPECTED_CA}:ro", "img", "sleep", "infinity"]

    env = _launch_env(tmp_path, stub, rec, log)
    env["PODMAN_ONECLI_ALLOWED_ENV"] = "ANTHROPIC_API_KEY"
    env["ANTHROPIC_API_KEY"] = "onecli-injects-at-egress"  # placeholder, not a secret
    result = subprocess.run(
        [str(WRAPPER), *argv], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0
    assert "ANTHROPIC_API_KEY" in rec.read_text(encoding="utf-8")


@pytest.mark.linux_only
def test_wrapper_refuses_malformed_proxy_without_leak(tmp_path):
    """AC-CRED-F28-1 (redaction): a malformed proxy value with no scheme/userinfo
    delimiters (so authority parsing cannot strip it) is refused, and its token
    never reaches the refusal message on stderr or the audit log."""
    stub = _record_stub(tmp_path)
    rec = tmp_path / "rec.txt"
    log = tmp_path / "wrapper.log"
    rec.write_text("", encoding="utf-8")
    log.write_text("", encoding="utf-8")
    ca_host = tmp_path / "ca.crt"
    ca_host.write_text("--CA--", encoding="utf-8")

    env = _launch_env(tmp_path, stub, rec, log)
    for name in PROXY_NAMES:
        env[name] = "aoc_LEAK_ME_TOKEN"  # malformed: no scheme, no userinfo '@'
    result = subprocess.run(
        [str(WRAPPER), *_valid_argv(ca_host)], env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0
    assert rec.read_text(encoding="utf-8") == ""
    assert "LEAK_ME_TOKEN" not in log.read_text(encoding="utf-8")
    assert "LEAK_ME_TOKEN" not in result.stderr


@pytest.mark.linux_only
def test_wrapper_refuses_wildcard_no_proxy(tmp_path):
    """AC-CRED-F28-1 (egress guarantee): when the launch forwards `-e NO_PROXY`
    and the process value is a broadened `*`, the launch is refused — a wildcard
    NO_PROXY would make the sandbox bypass the OneCLI proxy for every host."""
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
    argv += ["-e", "NO_PROXY"]
    argv += ["-v", f"{ca_host}:{EXPECTED_CA}:ro", "img", "sleep", "infinity"]

    env = _launch_env(tmp_path, stub, rec, log)
    env["NO_PROXY"] = "*"
    result = subprocess.run(
        [str(WRAPPER), *argv], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode != 0
    assert rec.read_text(encoding="utf-8") == ""


@pytest.mark.linux_only
def test_wrapper_accepts_loopback_no_proxy(tmp_path):
    """AC-CRED-F28-1 (egress guarantee): a forwarded `-e NO_PROXY` whose value is
    exactly the expected loopback bypass list is accepted — only a broadened
    value is refused, not the rendered loopback set."""
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
    argv += ["-e", "NO_PROXY"]
    argv += ["-v", f"{ca_host}:{EXPECTED_CA}:ro", "img", "sleep", "infinity"]

    env = _launch_env(tmp_path, stub, rec, log)
    env["NO_PROXY"] = "127.0.0.1,localhost,::1"
    result = subprocess.run(
        [str(WRAPPER), *argv], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0
    assert "NO_PROXY" in rec.read_text(encoding="utf-8")


@pytest.mark.linux_only
def test_wrapper_refuses_detach_keys_value_before_env(tmp_path):
    """AC-CRED-F28-1 (known value-flag boundary): a KNOWN value-taking flag
    (`--detach-keys ctrl-x`) has its value consumed as a value, so the image
    boundary is not shifted and a following pre-image `-e API_TOKEN=value` is
    still caught by the value-bearing-env rule. Podman is never invoked and the
    secret never reaches the log or stderr."""
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
    argv += ["--detach-keys", "ctrl-x", "-e", "API_TOKEN=sk-live-canary"]
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
def test_wrapper_refuses_unknown_pre_image_option(tmp_path):
    """AC-CRED-F28-1 (fail-closed dispatch): a pre-image option that is neither a
    known value-taking nor a known boolean flag is refused outright — an unknown
    value-taking flag must not be able to consume the image token and let later
    pre-image `-e`/`-v` escape the scan. `--future-value-flag ctrl-x` is refused;
    podman is never invoked."""
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
    argv += ["--future-value-flag", "ctrl-x"]  # unknown to both allowlists
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
def test_wrapper_refuses_proxy_authority_in_path(tmp_path):
    """AC-CRED-F28-1 (authority parsing): a proxy value whose real host is hidden
    in the path (`http://evil.example/path@172.17.0.1:10255` — a URL parser
    resolves the host as evil.example) is refused, so the aoc_ token cannot be
    sent to an attacker-controlled proxy that spoofs the expected authority."""
    stub = _record_stub(tmp_path)
    rec = tmp_path / "rec.txt"
    log = tmp_path / "wrapper.log"
    rec.write_text("", encoding="utf-8")
    log.write_text("", encoding="utf-8")
    ca_host = tmp_path / "ca.crt"
    ca_host.write_text("--CA--", encoding="utf-8")

    env = _launch_env(tmp_path, stub, rec, log)
    for name in PROXY_NAMES:
        env[name] = f"http://evil.example/path@{EXPECTED_PROXY}"
    result = subprocess.run(
        [str(WRAPPER), *_valid_argv(ca_host)], env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0
    assert rec.read_text(encoding="utf-8") == ""


@pytest.mark.linux_only
def test_wrapper_exec_logs_value_free_marker_and_forwards_argv(tmp_path):
    """FLEET-F62 C1: a `docker exec` through the wrapper (it IS HERMES_DOCKER_BINARY) logs a
    value-free `exec` audit marker — the exec-mediation witness that a sandbox command ran
    through a Hermes exec, not a raw `podman exec` — and forwards its argv unchanged. The
    marker never carries the exec command, so a token in the command never reaches the log."""
    stub = _record_stub(tmp_path)
    rec = tmp_path / "rec.txt"
    log = tmp_path / "wrapper.log"
    rec.write_text("", encoding="utf-8")
    log.write_text("", encoding="utf-8")

    token_cmd = "curl -s http://x:aoc_secret_DO_NOT_LEAK@172.17.0.1:10255/v1/models"
    argv = ["exec", "hermes-cid", "sh", "-c", token_cmd]
    result = subprocess.run(
        [str(WRAPPER), *argv],
        env={
            **os.environ,
            "PODMAN_ONECLI_PODMAN": str(stub),
            "PODMAN_ONECLI_LOG": str(log),
            "STUB_REC": str(rec),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "exec hermes-cid sh -c" in rec.read_text(encoding="utf-8")  # argv forwarded unchanged
    logged = log.read_text(encoding="utf-8")
    assert "exec" in [line.strip() for line in logged.splitlines() if line.strip()]
    # Value-free: the exec command (and any token inside it) is never written to the audit log.
    assert "aoc_secret_DO_NOT_LEAK" not in logged


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
