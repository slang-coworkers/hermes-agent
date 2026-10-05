"""The worker image maps the provider's session credential onto the GitHub CLI's names for login shells.

The variable names come only from the shipped fragment: the test parses them, so no name is spelled here,
and failures report positions, never names (pytest would otherwise print them in the assertion diff).
"""

import importlib.util
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
OPENSHELL_DIR = REPO_ROOT / "plugins" / "nv-coworker-compose" / "openshell"
DOCKERFILE = OPENSHELL_DIR / "Dockerfile.worker"
LINT_GOLDEN = REPO_ROOT / "tests" / "e2e-scenarios" / "OSH-F64" / "fixtures" / "dockerfile.worker.lint.txt"
SYNTHETIC = "fleet-f62d-synthetic-not-a-credential"
_LITERAL_TOKEN_RE = re.compile(r"gh[opsur]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}")
_AUTH_VALUE_RE = re.compile(r"(?i)\b(?:basic|bearer|token)\s+[A-Za-z0-9+/=_\-]{24,}")
# `export NAME="${SOURCE}"`: the target is a CLI name, the source the provider's name.
_EXPORT_RE = re.compile(r'(?m)^\s*export\s+([A-Za-z_][A-Za-z0-9_]*)="\$\{([A-Za-z_][A-Za-z0-9_]*)\}"\s*$')


def _instructions():
    logical = re.sub(r"\\\s*\n", " ", DOCKERFILE.read_text(encoding="utf-8"))
    return [ln.strip() for ln in logical.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def _profile_d_copies():
    lines = _instructions()
    copies = []
    for i, ln in enumerate(lines):
        parts = ln.split()
        if parts[0].upper() == "COPY" and len(parts) == 3 and parts[1].startswith("profile.d/") \
                and parts[2].startswith("/etc/profile.d/"):
            copies.append((i, parts[1], parts[2]))
    users = [i for i, ln in enumerate(lines) if ln.split()[0].upper() == "USER"]
    return copies, users


def _fragment():
    copies, _ = _profile_d_copies()
    assert len(copies) == 1, f"Dockerfile.worker must install exactly one profile.d fragment, got {copies}"
    path = OPENSHELL_DIR / copies[0][1]
    assert path.is_file(), f"fragment source missing: {path}"
    return path


def _names():
    pairs = _EXPORT_RE.findall(_fragment().read_text(encoding="utf-8"))
    assert pairs, "the fragment must export the CLI names as references to the provider's name"
    sources = {src for _dst, src in pairs}
    assert len(sources) == 1, f"every CLI name must map from the one provider name, got {len(sources)} sources"
    cli = sorted({dst for dst, _src in pairs})
    provider = sources.pop()
    assert len(cli) == 2 and provider not in cli, "expected two CLI names distinct from the provider"
    return cli, provider


def _run(extra_env):
    sh = shutil.which("sh")
    assert sh, "a POSIX sh is required"
    script = f'. "{_fragment()}"; env'
    env = {"PATH": "/usr/bin:/bin", **extra_env}
    return subprocess.run([sh, "-c", script], capture_output=True, text=True, encoding="utf-8", env=env, stdin=subprocess.DEVNULL,
                          timeout=30)


def _set_names(stdout):
    return dict(ln.split("=", 1) for ln in stdout.splitlines() if "=" in ln)


def test_a_image_installs_one_profile_d_fragment_before_user():
    copies, users = _profile_d_copies()
    assert len(copies) == 1, f"Dockerfile.worker must COPY exactly one profile.d/ fragment into /etc/profile.d/, got {copies}"
    assert users, "Dockerfile.worker must drop to an unprivileged USER"
    assert copies[0][0] < users[-1], "the fragment must be installed before the final USER (root writes /etc/profile.d)"
    assert (OPENSHELL_DIR / copies[0][1]).is_file(), f"fragment source missing in the build context: {copies[0][1]}"


@pytest.mark.linux_only
def test_b_provider_credential_reaches_every_cli_name():
    cli, provider = _names()
    proc = _run({provider: SYNTHETIC})
    assert proc.returncode == 0, proc.stderr
    got = _set_names(proc.stdout)
    unmapped = [i for i, name in enumerate(cli) if got.get(name) != SYNTHETIC]
    assert not unmapped, f"CLI names at {unmapped} were not mapped from the provider's name"


@pytest.mark.linux_only
@pytest.mark.parametrize("provider_value", [None, ""])
def test_c_no_provider_credential_sets_no_cli_name(provider_value):
    cli, provider = _names()
    proc = _run({} if provider_value is None else {provider: provider_value})
    assert proc.returncode == 0, proc.stderr
    got = _set_names(proc.stdout)
    leaked = [i for i, name in enumerate(cli) if name in got]
    assert not leaked, f"CLI names at {leaked} were set without a provider credential"


@pytest.mark.linux_only
@pytest.mark.parametrize("preset_value", ["preset-by-operator", ""])
def test_d_an_already_set_cli_name_is_never_overridden(preset_value):
    cli, provider = _names()
    overridden = []
    for i, name in enumerate(cli):
        proc = _run({provider: SYNTHETIC, name: preset_value})
        assert proc.returncode == 0, proc.stderr
        if _set_names(proc.stdout).get(name) != preset_value:
            overridden.append(i)
    assert not overridden, f"the fragment overrode already-set CLI names at {overridden}"


@pytest.mark.linux_only
@pytest.mark.parametrize("with_provider", [True, False])
def test_e_fragment_prints_nothing(with_provider):
    _cli, provider = _names()
    sh = shutil.which("sh")
    env = {"PATH": "/usr/bin:/bin", **({provider: SYNTHETIC} if with_provider else {})}
    proc = subprocess.run([sh, "-c", f'. "{_fragment()}"'], capture_output=True, text=True, encoding="utf-8", env=env,
                          stdin=subprocess.DEVNULL, timeout=30)
    assert proc.returncode == 0 and proc.stdout == "" and proc.stderr == "", "the fragment must be silent"


def test_f_fragment_holds_no_credential_and_is_a_redactor_fixed_point():
    from agent.redact import redact_sensitive_text

    text = _fragment().read_text(encoding="utf-8")
    assert not _LITERAL_TOKEN_RE.search(text) and not _AUTH_VALUE_RE.search(text), "literal credential shape"
    assert redact_sensitive_text(text, force=True, code_file=True) == text, "the redactor would mask part of the fragment"


def test_g_dockerfile_still_lints_clean():
    spec = importlib.util.spec_from_file_location("fleet_f62d_cred_lint", OPENSHELL_DIR / "dockerfile_lint.py")
    lint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lint)
    result = lint.lint(str(DOCKERFILE))
    assert result.errors == 0, f"Dockerfile.worker lint findings: {result.findings}"
    assert result.report == LINT_GOLDEN.read_text(encoding="utf-8"), "the lint report must stay the zero-finding golden"
