"""Vendored, dependency-free, deterministic Dockerfile linter for the worker image.

`hadolint` is not in the coworker image and the checker must run with no network and no
optional binary, so it is small and self-contained: a FIXED, sorted ruleset over a parsed
Dockerfile that returns a structured result (error/warning counts + findings) and a
deterministic, path-independent text report.

Route (a) security invariant — the worker sandbox is reached by gateway-brokered ssh
(ProxyCommand), so the image must carry NO sshd and NO key material — is enforced by the
``no-sshd`` and ``no-key-material`` rules (obfuscated key filenames and multi-stage
``COPY --from`` forms included)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Tuple

ERROR = "ERROR"
WARN = "WARN"

# Rule ids are the stable contract; the report lists them so adding/removing a rule
# changes the deterministic output.
RULES = (
    "apt-no-install-recommends",
    "from-pinned-digest",
    "no-apt-upgrade",
    "no-key-material",
    "no-secret-arg-env",
    "no-sshd",
    "non-root-user",
    "pip-pinned",
    "workdir-absolute",
)

_DIGEST_FROM = re.compile(r"@sha256:[0-9a-f]{64}\b")
_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.IGNORECASE)
_KEY_SRC = re.compile(
    r"(^|/)(id_rsa|id_ed25519|id_ecdsa|id_dsa|authorized_keys|[^/\s]*_rsa)\b"
    r"|\.(pem|key|p12|pfx|ppk)\b"
    r"|[^/\s]*(private[-_.]?key|secret|credential)[^/\s]*",
    re.IGNORECASE,
)
_SSHD = re.compile(r"\b(openssh-server|ssh-server|sshd|authorized_keys)\b", re.IGNORECASE)


class LintResult:
    """Plain class (not a dataclass) so the module imports via
    ``importlib.util.module_from_spec`` without sys.modules registration."""

    __slots__ = ("errors", "warnings", "findings", "report")

    def __init__(self, errors: int, warnings: int, findings: List[Tuple[str, str, str, int]], report: str) -> None:
        self.errors = errors
        self.warnings = warnings
        self.findings = findings
        self.report = report


def _logical_lines(text: str) -> List[Tuple[int, str]]:
    """Dockerfile instructions with backslash-continuations joined, keeping the FIRST
    physical line number, dropping comments and blanks."""
    out: List[Tuple[int, str]] = []
    buf, start = "", 0
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip("\n")
        stripped = line.strip()
        if not buf and (not stripped or stripped.startswith("#")):
            continue
        if not buf:
            start = i
        if stripped.endswith("\\"):
            buf += stripped[:-1] + " "
            continue
        buf += stripped
        out.append((start, buf.strip()))
        buf = ""
    if buf:
        out.append((start, buf.strip()))
    return out


def lint(path) -> LintResult:
    text = Path(path).read_text(encoding="utf-8")
    instrs = _logical_lines(text)
    findings: List[Tuple[str, str, str, int]] = []

    def add(level: str, rule: str, msg: str, lineno: int) -> None:
        findings.append((level, rule, msg, lineno))

    last_user_nonroot = False
    saw_user = False
    for lineno, instr in instrs:
        upper = instr.split(None, 1)[0].upper() if instr.split() else ""
        rest = instr[len(upper):].strip()
        low = instr.lower()

        if upper == "FROM":
            ref = rest.split(" AS ")[0].split()[0] if rest else ""
            # A stage alias reference (FROM <prior-stage>) has no registry path; only an
            # external image (contains a '/' or a tag) must be digest-pinned.
            external = "/" in ref or ":" in ref
            if external and not _DIGEST_FROM.search(ref):
                add(ERROR, "from-pinned-digest", f"FROM must pin by @sha256 digest: {ref}", lineno)

        if upper in ("RUN",):
            if re.search(r"\bapt-get\b[^\n]*\b(upgrade|dist-upgrade)\b", low):
                add(ERROR, "no-apt-upgrade", "apt-get (dist-)upgrade is forbidden (pin the base image instead)", lineno)
            if re.search(r"\bapt-get\b[^\n]*\binstall\b", low) and "--no-install-recommends" not in low:
                add(ERROR, "apt-no-install-recommends", "apt-get install must use --no-install-recommends", lineno)
            if re.search(r"\bpip3?\b[^\n]*\binstall\b", low):
                # each non-flag token after `install` must be pinned (== or a requirements file)
                for tok in re.split(r"\s+", rest):
                    if tok in ("pip", "pip3", "install", "--no-cache-dir", "--upgrade", "-U") or tok.startswith("-"):
                        continue
                    if tok.lower() in ("run",):
                        continue
                    if tok and not tok.startswith(("&", "|", "\\")) and "install" not in tok.lower():
                        if "==" not in tok and not tok.endswith(".txt") and tok not in ("pip", "&&", "python", "python3", "-m"):
                            if re.match(r"^[A-Za-z0-9._-]+$", tok):
                                add(ERROR, "pip-pinned", f"pip install must pin the version (pkg==x.y.z): {tok}", lineno)
            if _SSHD.search(low):
                add(ERROR, "no-sshd", "route-a image must not install/enable sshd or authorized_keys", lineno)

        if upper == "WORKDIR" and rest and not rest.startswith("/"):
            add(ERROR, "workdir-absolute", f"WORKDIR must be absolute: {rest}", lineno)

        if upper in ("ARG", "ENV"):
            for pair in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(\S+)", rest):
                name, value = pair.group(1), pair.group(2)
                if _SECRET_NAME.search(name) and value and value not in ("", '""', "''"):
                    add(ERROR, "no-secret-arg-env", f"secret-like {upper} with an inline value: {name}", lineno)
            # `ARG NAME` with no default is fine (build-time, no baked secret)

        if upper in ("COPY", "ADD"):
            args = [a for a in re.split(r"\s+", rest) if a and not a.startswith("--")]
            sources = args[:-1] if len(args) >= 2 else args
            for src in sources:
                if _KEY_SRC.search(src):
                    add(ERROR, "no-key-material", f"route-a image must not COPY/ADD key material: {src}", lineno)
            if _SSHD.search(low):
                add(ERROR, "no-sshd", "route-a image must not add authorized_keys", lineno)

        if upper == "USER":
            saw_user = True
            last_user_nonroot = bool(rest) and rest.split(":")[0] not in ("root", "0")

    if not saw_user or not last_user_nonroot:
        add(ERROR, "non-root-user", "image must end running as a non-root USER", 0)

    findings.sort(key=lambda f: (f[1], f[3], f[0], f[2]))
    errors = sum(1 for f in findings if f[0] == ERROR)
    warnings = sum(1 for f in findings if f[0] == WARN)
    report = _render(Path(path).name, findings, errors, warnings)
    return LintResult(errors, warnings, findings, report)


def _render(name: str, findings, errors: int, warnings: int) -> str:
    lines = [
        f"OSH-F64 worker Dockerfile lint — {name}",
        "checks: " + ", ".join(RULES),
        f"errors: {errors}",
        f"warnings: {warnings}",
        "findings:",
    ]
    if findings:
        for level, rule, msg, lineno in findings:
            lines.append(f"  [{level}] {rule} (line {lineno}): {msg}")
    else:
        lines.append("  none")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":  # pragma: no cover - manual use / fixture regeneration
    import sys
    res = lint(sys.argv[1])
    sys.stdout.write(res.report)
    raise SystemExit(1 if res.errors else 0)
