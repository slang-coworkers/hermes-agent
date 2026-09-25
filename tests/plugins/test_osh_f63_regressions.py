"""Fail-closed regressions for OSH-F63 (nv-coworker-compose) — NOT AC ids.

Kept in a separate module so tests/plugins/test_osh_f63_acceptance.py stays
byte-identical to the architect's checksum-bound authoritative artifact
(sha256 d537095e…). These guard nv-coworker-compose behaviour the authoritative
acceptance test does not cover: the canonical forbidden-egress rejection in
_validate_openshell_egress (in EITHER egress position), and the all-or-nothing
mode-0600 ssh_key placeholder. The few helpers below are duplicated (not
imported from the acceptance module) so this file runs standalone under the
per-file subprocess isolation of scripts/run_tests.sh.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGINS_SRC = REPO_ROOT / "plugins"
SCENARIO_DIR = REPO_ROOT / "tests" / "e2e-scenarios" / "OSH-F63"
OPENSHELL_SPEC = SCENARIO_DIR / "spec" / "openshell" / "coworker-types.yaml"
FLEET_PLUGINS = ("nv-coworker-compose", "nv-fleet-gates")
COWORKER_PROFILES = ("orchestrator", "architect", "builder", "tester", "reviewer")


def _bootstrap_home(tmp_path: Path) -> Path:
    """An isolated HERMES_HOME with the fleet plugins enabled so ``hermes
    coworker compose`` can run: copy each plugin dir into <home>/plugins/<name>
    and enable them in config.yaml. The empty bundled dir +
    HERMES_ENABLE_PROJECT_PLUGINS=0 are set by _render so only these load."""
    home = tmp_path / "hermes-home"
    (home / "plugins").mkdir(parents=True, exist_ok=True)
    for name in FLEET_PLUGINS:
        shutil.copytree(PLUGINS_SRC / name, home / "plugins" / name)
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": list(FLEET_PLUGINS)}}),
        encoding="utf-8",
    )
    return home


def _render(spec: Path, out: Path, home: Path, *extra_args: str) -> subprocess.CompletedProcess:
    """Run ``hermes coworker compose <spec> --out <out> [extra]`` under the
    isolated home with an empty bundled-plugins dir and project plugins off."""
    empty_bundled = home.parent / "empty-bundled"
    empty_bundled.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update(
        HOME=str(home.parent),
        HERMES_HOME=str(home),
        HERMES_BUNDLED_PLUGINS=str(empty_bundled),
        HERMES_ENABLE_PROJECT_PLUGINS="0",
    )
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "coworker", "compose", str(spec), "--out", str(out), *extra_args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )


def _profile_config(out: Path, profile: str) -> dict:
    matches = list(out.glob(f"*{profile}*/config.yaml"))
    assert matches, f"no rendered config for profile {profile!r} under {out}"
    return yaml.safe_load(matches[0].read_text(encoding="utf-8"))


def _dotted(d: dict, path: str):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


# a control-plane / gateway / wildcard / inbound-ssh host must be refused as an egress
# target, in EITHER configurable position, before any profile is written.
_FORBIDDEN_EGRESS_TARGETS = (
    "host.openshell.internal:8080",   # OpenShell gateway control plane (denied to workers)
    "172.17.0.1:10256",               # OneCLI control plane
    "172.17.0.1:22",                  # ssh control path — INBOUND via the proxy socket, never egress
    "0.0.0.0/0:443",                  # IPv4 wildcard CIDR
    "::/0:443",                       # IPv6 wildcard CIDR
    "0.0.0.0:443",                    # IPv4 unspecified
    "*:443",                          # bare wildcard host
    "*.example.com:443",              # wildcard subdomain — a '*' anywhere is refused
    "HOST.OPENSHELL.INTERNAL:8080",   # case variant of the gateway control plane (DNS is case-insensitive)
    "host.openshell.internal.:8080",  # trailing-root-dot variant of the gateway control plane
    "[::]:443",                       # IPv6 unspecified, bracketed
)


def test_openshell_egress_rejects_forbidden_hosts(tmp_path):
    """Unit (not an AC id): the render fails CLOSED when a spec's egress names a
    control-plane / gateway / wildcard / inbound-ssh host — in EITHER position
    (proxy_addr or inference_route) — so a tampered spec cannot render worker access
    to a forbidden host. A pure substring wildcard check would miss 0.0.0.0/0:<port>
    and host.openshell.internal:8080; the parsed (host, port) refusal catches them."""
    home = _bootstrap_home(tmp_path)
    for position in ("proxy_addr", "inference_route"):
        for i, target in enumerate(_FORBIDDEN_EGRESS_TARGETS):
            spec_dir = tmp_path / f"openshell-forbidden-{position}-{i}"
            shutil.copytree(OPENSHELL_SPEC.parent, spec_dir)
            spec = spec_dir / OPENSHELL_SPEC.name
            data = yaml.safe_load(spec.read_text(encoding="utf-8"))
            data["egress"][position] = target
            spec.write_text(yaml.safe_dump(data), encoding="utf-8")
            out = tmp_path / f"out-forbidden-{position}-{i}"
            proc = _render(spec, out, home)
            assert proc.returncode != 0, f"{position}={target!r}: render must reject a forbidden egress target"
            assert not list(out.glob("*/config.yaml")), f"{position}={target!r}: no distribution may be written on the refusal"


def test_openshell_ssh_key_placeholder_empty_0600(tmp_path):
    """Unit (not an AC id): the render creates each terminal.ssh_key as an empty,
    mode-0600, non-symlink regular file (never key material), and fails CLOSED on a
    pre-existing non-empty file or a dangling symlink at that path (a dangling symlink
    is invisible to Path.exists(), so a naive touch would follow it and write outside
    the render-owned keys dir). All-or-nothing: a collision on ANY profile — including a
    LATE one in write order — aborts before a single profile config is written, so a
    partial fleet can never be left on disk."""
    import stat as _stat

    # happy path: every rendered ssh_key is an empty 0600 regular file
    home = _bootstrap_home(tmp_path)
    out = tmp_path / "out"
    assert _render(OPENSHELL_SPEC, out, home).returncode == 0
    for profile in COWORKER_PROFILES:
        key = Path(_dotted(_profile_config(out, profile), "terminal.ssh_key"))
        assert key.is_file() and not key.is_symlink(), f"{profile}: ssh_key {key} is not a regular file"
        assert key.stat().st_size == 0, f"{profile}: ssh_key {key} is not empty ({key.stat().st_size} bytes)"
        assert _stat.S_IMODE(key.stat().st_mode) == 0o600, f"{profile}: ssh_key {key} mode != 0600"

    # fail-closed: a pre-existing NON-EMPTY file at the first profile's key path
    home2 = _bootstrap_home(tmp_path / "h2")
    keys2 = home2 / "openshell" / "keys"
    keys2.mkdir(parents=True, exist_ok=True)
    (keys2 / COWORKER_PROFILES[0]).write_text("not-a-placeholder", encoding="utf-8")
    out2 = tmp_path / "out2"
    proc2 = _render(OPENSHELL_SPEC, out2, home2)
    assert proc2.returncode != 0, "render must fail on a pre-existing non-empty ssh_key"
    assert not list(out2.glob("*/config.yaml")), "no distribution may be written on the non-empty-key refusal"

    # fail-closed: a DANGLING SYMLINK at the first profile's key path — render must
    # raise WITHOUT following it (no target file created) and write no config.
    home3 = _bootstrap_home(tmp_path / "h3")
    keys3 = home3 / "openshell" / "keys"
    keys3.mkdir(parents=True, exist_ok=True)
    target = home3 / "escape-target"
    (keys3 / COWORKER_PROFILES[0]).symlink_to(target)
    out3 = tmp_path / "out3"
    proc3 = _render(OPENSHELL_SPEC, out3, home3)
    assert proc3.returncode != 0, "render must fail on a dangling ssh_key symlink"
    assert not target.exists(), "render must NOT follow the symlink and create its target"
    assert not list(out3.glob("*/config.yaml")), "no distribution may be written on the symlink refusal"

    # fail-closed ALL-OR-NOTHING: a collision on a LATER profile (last in COWORKER_PROFILES)
    # must abort before any profile config is written.
    home4 = _bootstrap_home(tmp_path / "h4")
    keys4 = home4 / "openshell" / "keys"
    keys4.mkdir(parents=True, exist_ok=True)
    (keys4 / COWORKER_PROFILES[-1]).write_text("not-a-placeholder", encoding="utf-8")
    out4 = tmp_path / "out4"
    proc4 = _render(OPENSHELL_SPEC, out4, home4)
    assert proc4.returncode != 0, "render must fail on a non-empty ssh_key for a LATE profile"
    assert not list(out4.glob("*/config.yaml")), "no profile config may be written when a LATER profile's key collides (all-or-nothing)"
