#!/usr/bin/env python3
"""Report drift in the paths two repositories keep byte-identical.

common/COMMON_PATHS lists the shared paths, one per line: a file, a directory (every file under it) or an
fnmatch glob over repo-relative paths; '#' starts a comment. A file belongs to the first entry that
matches it. common/COMMON_PIN names the other repository and the commit to compare with:

    name=<label for the other repository in the report>
    repo=<anonymous https:// (or file://) URL; COMMON_OTHER_URL overrides it>
    sha=<full commit sha; COMMON_OTHER_SHA overrides it>
    entry <hash> <COMMON_PATHS entry>      one per entry, written by --record-pin

Each shared file is compared by git mode and blob id at --local-ref (default HEAD) and at the pinned
commit. The pinned commit is read from the local object store when it is there, otherwise from a shallow,
blob-less fetch into a temporary repository that borrows the local objects; only the contents of the
files that differ are fetched, for the line counts. Network git calls run without the user's or the
system's git configuration and without credentials. When the other repository cannot be reached, each
entry is compared with the hash recorded for it in COMMON_PIN (when the record is for the pinned sha);
without a record the report says only that the other repository is unreachable.

File statuses: identical; diverged (+A -R: lines the other side adds and removes relative to this repo,
or binary, or a mode change); missing-there (only in this repo); missing-here (only in the other one).
An entry that matches no file on either side is empty, which counts as drift.

Exit status: 0 report written, drift or not, reachable or not; 1 drift under --strict; 2 usage or
configuration error, including a pinned sha that the other repository does not have; 3 other
repository unreachable under --fail-unreachable or --record-pin.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

PATHS_FILE = "common/COMMON_PATHS"
PIN_FILE = "common/COMMON_PIN"
EXIT_OK, EXIT_DRIFT, EXIT_CONFIG, EXIT_UNREACHABLE = 0, 1, 2, 3
SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
GLOB_CHARS = frozenset("*?[")
NO_FILES = "-"
LOCAL_TIMEOUT = 120.0
PATHS_PER_DIFF = 500
PIN_HEADER = """\
# The other repository's commit that common/COMMON_PATHS is compared with (scripts/common_drift.py).
# name: its label in the report. repo: anonymous https URL; COMMON_OTHER_URL overrides it.
# sha: the pinned commit; COMMON_OTHER_SHA overrides it.
# entry lines: each COMMON_PATHS entry's hash at sha, written by `scripts/common_drift.py --record-pin`
# and compared when the other repository cannot be reached.
"""
KEEP_GIT_ENV = frozenset({"GIT_SSL_CAINFO", "GIT_SSL_CAPATH"})
REJECTED_SHA = re.compile(r"not our ref|couldn't find remote ref|unadvertised object", re.IGNORECASE)
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_REDACTIONS = (
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@"), r"\1***@"),
    (re.compile(r"(?i)\b(bearer\s+)\S+"), r"\1***"),
    (re.compile(r"\b(?:gh[opsu]_|github_pat_|glpat-|xox[abp]-|sk-)[A-Za-z0-9_-]{8,}"), "***"),
)


class ConfigError(Exception):
    """Usage or configuration problem (exit 2)."""


class GitFailed(Exception):
    """A git command failed. str() is its redacted last stderr line."""

    def __init__(self, message: str, stderr: str = "", timed_out: bool = False):
        super().__init__(message)
        self.stderr = stderr
        self.timed_out = timed_out


class Unreachable(Exception):
    """The other repository could not be read."""


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def printable(text: str) -> str:
    return CONTROL_CHARS.sub(lambda match: f"\\x{ord(match.group()):02x}", text)


def git_env(isolated_home: Path | None) -> dict[str, str]:
    """The caller's environment without GIT_* variables and askpass helpers. With isolated_home (network
    calls): no global or system git config and no ~/.netrc, so no credential, header or URL rewrite applies."""
    env = {key: value for key, value in os.environ.items()
           if (not key.startswith("GIT_") or key in KEEP_GIT_ENV) and key != "SSH_ASKPASS"}
    env.update(GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    if isolated_home is None:
        env["GIT_NO_LAZY_FETCH"] = "1"
    else:
        env.update(HOME=str(isolated_home), XDG_CONFIG_HOME=str(isolated_home), GIT_CONFIG_NOSYSTEM="1",
                   GIT_CONFIG_GLOBAL=os.devnull)
    return env


def git(repo: Path, *args: str, timeout: float = LOCAL_TIMEOUT, isolated_home: Path | None = None) -> bytes:
    command = next((arg for arg in args if not arg.startswith("-")), "")
    try:
        proc = subprocess.Popen(["git", "-C", str(repo), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=git_env(isolated_home), start_new_session=True)
    except OSError as exc:
        raise GitFailed(f"cannot run git {command}: {exc.strerror}") from None
    with proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            raise GitFailed(f"git {command} timed out after {timeout:g}s", timed_out=True) from None
    if proc.returncode:
        stderr = err.decode("utf-8", "replace")
        lines = [line.strip() for line in stderr.splitlines() if line.strip()]
        raise GitFailed(redact(lines[-1])[:300] if lines else f"git {command} exited {proc.returncode}", stderr)
    return out


def tree_files(repo: Path, commit: str, **kwargs) -> dict[str, tuple[str, str]]:
    """Every file at commit: path -> (mode, object id)."""
    files: dict[str, tuple[str, str]] = {}
    for record in git(repo, "ls-tree", "-r", "-z", "--full-tree", commit, **kwargs).split(b"\0"):
        if record:
            meta, _, path = record.partition(b"\t")
            mode, _kind, oid = meta.decode("ascii").split()
            files[path.decode("utf-8", "surrogateescape")] = (mode, oid)
    return files


def parse_paths(text: str) -> list[str]:
    entries: list[str] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        entry = line.rstrip("/")
        parts = entry.split("/")
        if not entry or "" in parts or "." in parts or ".." in parts:
            raise ConfigError(f"{PATHS_FILE} line {number}: not a repo-relative path: {printable(line)}")
        if entry in entries:
            raise ConfigError(f"{PATHS_FILE} line {number}: duplicate entry {printable(entry)}")
        entries.append(entry)
    if not entries:
        raise ConfigError(f"{PATHS_FILE} has no entries")
    return entries


def matches(entry: str, path: str) -> bool:
    if GLOB_CHARS.intersection(entry):
        return fnmatch.fnmatchcase(path, entry)
    return path == entry or path.startswith(entry + "/")


def assign(entries: list[str], paths) -> dict[str, list[str]]:
    """Sorted paths per entry; a path goes to the first entry that matches it."""
    owned: dict[str, list[str]] = {entry: [] for entry in entries}
    for path in sorted(paths):
        for entry in entries:
            if matches(entry, path):
                owned[entry].append(path)
                break
    return owned


def entry_hash(files: dict[str, tuple[str, str]], paths: list[str]) -> str:
    if not paths:
        return NO_FILES
    digest = hashlib.sha256()
    for path in paths:
        mode, oid = files[path]
        digest.update(f"{mode} {oid} {path}\n".encode("utf-8", "surrogateescape"))
    return digest.hexdigest()


@dataclass
class Pin:
    name: str = ""
    repo: str = ""
    sha: str = ""
    hashes: dict[str, str] = field(default_factory=dict)


def parse_pin(text: str) -> Pin:
    pin = Pin()
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("entry "):
            parts = line.split(None, 2)
            if len(parts) != 3:
                raise ConfigError(f"{PIN_FILE} line {number}: expected 'entry <hash> <path>'")
            pin.hashes[parts[2]] = parts[1]
            continue
        key, sep, value = line.partition("=")
        if not sep or key.strip() not in ("name", "repo", "sha"):
            raise ConfigError(f"{PIN_FILE} line {number}: expected name=, repo=, sha= or an entry line")
        setattr(pin, key.strip(), value.strip())
    return pin


def render_pin(pin: Pin, entries: list[str]) -> str:
    lines = [PIN_HEADER.rstrip("\n"), f"name={pin.name}", f"repo={pin.repo}", f"sha={pin.sha}"]
    lines += [f"entry {pin.hashes[entry]} {entry}" for entry in entries]
    return "\n".join(lines) + "\n"


def check_url(url: str) -> None:
    """An anonymous https:// URL (file:// for local mirrors): no credentials, query or fragment."""
    if not url:
        return
    try:
        parts = urlsplit(url)
        anonymous = parts.username is None and parts.password is None
    except ValueError:
        parts, anonymous = None, False
    if (parts is None or parts.scheme not in ("https", "file") or not anonymous or parts.query or parts.fragment
            or (parts.scheme == "https" and not parts.hostname) or any(c.isspace() or ord(c) < 32 for c in url)):
        raise ConfigError(f"repo must be an anonymous https:// URL with no query: {redact(printable(url))}")


@dataclass
class Other:
    gitdir: Path
    files: dict[str, tuple[str, str]]
    mode: str
    isolated_home: Path | None = None


def open_other(root: Path, url: str, sha: str, object_format: str, workdir: Path, timeout: float) -> Other:
    """The pinned commit's file list, plus a repository that holds both commits for the line counts."""
    try:
        return Other(root, tree_files(root, sha), "local")
    except GitFailed:
        pass
    if not url:
        raise Unreachable("no repo URL (repo= in COMMON_PIN, or COMMON_OTHER_URL)")
    common_dir = Path(git(root, "rev-parse", "--git-common-dir").decode().strip())
    bare = workdir / "other.git"
    git(workdir, "init", "--quiet", "--bare", f"--object-format={object_format}", str(bare), isolated_home=workdir)
    (bare / "objects" / "info" / "alternates").write_text(f"{(root / common_dir).resolve() / 'objects'}\n",
                                                          encoding="utf-8")
    for key, value in (("remote.origin.url", url), ("remote.origin.promisor", "true"),
                       ("remote.origin.partialclonefilter", "blob:none"), ("fetch.negotiationAlgorithm", "noop"),
                       ("protocol.allow", "never"), ("protocol.https.allow", "always"),
                       ("protocol.file.allow", "always"), ("credential.helper", ""),
                       ("gc.auto", "0"), ("maintenance.auto", "false")):
        git(bare, "config", key, value, isolated_home=workdir)
    try:
        git(bare, "fetch", "--quiet", "--no-tags", "--depth=1", "--filter=blob:none", "origin", sha,
            timeout=timeout, isolated_home=workdir)
    except GitFailed as error:
        if error.timed_out or not REJECTED_SHA.search(error.stderr):
            raise Unreachable(str(error)) from None
        raise ConfigError(f"{redact(url)} has no commit {sha}: {error}") from None
    return Other(bare, tree_files(bare, sha, isolated_home=workdir), "fetched", workdir)


def line_counts(other: Other, local_sha: str, other_sha: str, paths: list[str], timeout: float):
    """path -> (added, removed) as git numstat prints them ('-' for binary); None when git diff fails."""
    counts: dict[str, tuple[str, str]] = {}
    for start in range(0, len(paths), PATHS_PER_DIFF):
        try:
            out = git(other.gitdir, "--literal-pathspecs", "diff", "--numstat", "-z", "--no-renames",
                      "--no-ext-diff", "--no-textconv", local_sha, other_sha, "--",
                      *paths[start:start + PATHS_PER_DIFF], timeout=timeout, isolated_home=other.isolated_home)
        except GitFailed:
            return None
        for record in out.split(b"\0"):
            if record:
                added, removed, path = record.split(b"\t", 2)
                counts[path.decode("utf-8", "surrogateescape")] = (added.decode(), removed.decode())
    return counts


@dataclass
class FileRow:
    path: str
    entry: str
    status: str
    added: int | None = None
    removed: int | None = None
    binary: bool = False
    mode_change: str = ""


def compare_files(entries, local_files, other: Other, local_sha, other_sha, timeout):
    owned = assign(entries, set(local_files) | set(other.files))
    rows: list[FileRow] = []
    for entry in entries:
        for path in owned[entry]:
            here, there = local_files.get(path), other.files.get(path)
            if here is None:
                rows.append(FileRow(path, entry, "missing-here"))
            elif there is None:
                rows.append(FileRow(path, entry, "missing-there"))
            elif here == there:
                rows.append(FileRow(path, entry, "identical"))
            else:
                mode_change = f"{here[0]}->{there[0]}" if here[0] != there[0] else ""
                rows.append(FileRow(path, entry, "diverged", mode_change=mode_change))
    counts = line_counts(other, local_sha, other_sha, [row.path for row in rows if row.status == "diverged"], timeout)
    for row in rows:
        if row.status == "diverged" and counts and row.path in counts:
            added, removed = counts[row.path]
            if added == "-":
                row.binary = True
            else:
                row.added, row.removed = int(added), int(removed)
    entry_rows = []
    for entry in entries:
        statuses = Counter(row.status for row in rows if row.entry == entry)
        total = sum(statuses.values())
        entry_rows.append({
            "entry": entry, "files": total, "identical": statuses["identical"], "diverged": statuses["diverged"],
            "missing_there": statuses["missing-there"], "missing_here": statuses["missing-here"],
            "status": "empty" if not total else "identical" if statuses["identical"] == total else "drift",
        })
    return entry_rows, rows


def compare_recorded(entries, local_files, recorded: dict[str, str]):
    owned = assign(entries, local_files)
    entry_rows = []
    for entry in entries:
        here = entry_hash(local_files, owned[entry])
        if entry not in recorded:
            status = "unrecorded"
        elif here == recorded[entry]:
            status = "empty" if here == NO_FILES else "identical"
        else:
            status = "diverged"
        entry_rows.append({"entry": entry, "files_here": len(owned[entry]), "status": status})
    return entry_rows


def table(rows: list[list[str]]) -> list[str]:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return ["  ".join([row[0].ljust(widths[0])] + [cell.rjust(width) for cell, width in zip(row[1:], widths[1:])])
            .rstrip() for row in rows]


def file_note(row: FileRow) -> str:
    if row.binary:
        note = "binary"
    elif row.added is None:
        note = "line counts unavailable"
    else:
        note = f"+{row.added} -{row.removed}"
    return f"{note} mode {row.mode_change}" if row.mode_change else note


def render(report: dict, rows: list[FileRow]) -> str:
    name = printable(report["other"]["name"])
    out = [f"common drift: {printable(report['local']['ref'])} {report['local']['sha'][:12]} vs {name} "
           f"{report['other']['sha'][:12]}"]
    if report["other"]["unreachable"]:
        out.append(f"{name} unreachable: {printable(report['other']['unreachable'])}")
    if report["mode"] in ("local", "fetched"):
        where = "the local object store" if report["mode"] == "local" else printable(report["other"]["repo"])
        out.append(f"{name} read from {where}")
    if report["pin_hashes"] == "stale":
        out.append(f"WARN: the entry hashes in {PIN_FILE} do not match {name} {report['other']['sha'][:12]}; "
                   "re-record them with --record-pin")
    out.append(f"{PATHS_FILE}: {report['summary']['entries']} entries")
    if report["mode"] == "unreachable":
        out.append(f"no entry hashes recorded in {PIN_FILE} for this sha; nothing compared")
    elif report["mode"] == "recorded":
        out.append(f"compared with the entry hashes recorded in {PIN_FILE} (no per-file detail)")
        out.append("")
        out += table([["ENTRY", "FILES HERE", "STATUS"]]
                     + [[printable(e["entry"]), str(e["files_here"]), e["status"]] for e in report["entries"]])
    else:
        out.append("")
        out += table([["ENTRY", "FILES", "IDENTICAL", "DIVERGED", f"MISSING ON {name.upper()}", "MISSING HERE"]]
                     + [[printable(e["entry"]), str(e["files"]), str(e["identical"]), str(e["diverged"]),
                         str(e["missing_there"]), str(e["missing_here"])] for e in report["entries"]])
        drift_rows = [row for row in rows if row.status != "identical"]
        empty = [e["entry"] for e in report["entries"] if e["status"] == "empty"]
        if drift_rows or empty:
            out += ["", f"not identical (+A -R: lines {name} adds and removes relative to this repo):"]
            labels = {"diverged": "diverged", "missing-there": f"missing on {name}", "missing-here": "missing here"}
            width = max(len(label) for label in labels.values())
            for row in drift_rows:
                note = f"  {file_note(row)}" if row.status == "diverged" else ""
                out.append(f"  {labels[row.status].ljust(width)}  {printable(row.path)}{note}")
            out += [f"  {'empty entry'.ljust(width)}  {printable(entry)}" for entry in empty]
    out += ["", "SUMMARY " + " ".join(f"{key}={value}" for key, value in report["summary"].items())]
    return "\n".join(out) + "\n"


def find_root(root: Path | None) -> Path:
    start = root or Path.cwd()
    try:
        return Path(git(start, "rev-parse", "--show-toplevel").decode().strip())
    except (GitFailed, OSError) as exc:
        raise ConfigError(f"{start} is not in a git work tree: {exc}") from None


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror}") from None
    except UnicodeError:
        raise ConfigError(f"{path} is not UTF-8") from None


def run(args: argparse.Namespace) -> tuple[int, str, dict | None]:
    root = find_root(args.root)
    entries = parse_paths(read_text(args.paths or root / PATHS_FILE))
    pin_file = args.pin or root / PIN_FILE
    pin = parse_pin(read_text(pin_file)) if pin_file.is_file() or args.record_pin or not args.sha else Pin()
    name = pin.name or "other"
    url = args.repo or os.environ.get("COMMON_OTHER_URL") or pin.repo
    check_url(url)
    sha = (args.sha or os.environ.get("COMMON_OTHER_SHA") or pin.sha).lower()
    object_format = git(root, "rev-parse", "--show-object-format").decode().strip() or "sha1"
    if not SHA_RE.fullmatch(sha) or len(sha) != (64 if object_format == "sha256" else 40):
        raise ConfigError(f"the pinned sha must be a full {object_format} commit id, got {printable(sha)!r}")
    try:
        local_sha = git(root, "rev-parse", "--verify", "--quiet", f"{args.local_ref}^{{commit}}").decode().strip()
    except GitFailed:
        raise ConfigError(f"--local-ref {printable(args.local_ref)} is not a commit in {root}") from None
    local_files = tree_files(root, local_sha)
    recorded = pin.hashes if pin.hashes and sha == pin.sha.lower() else {}

    with tempfile.TemporaryDirectory(prefix="common-drift-") as workdir:
        try:
            other, reason = open_other(root, url, sha, object_format, Path(workdir), args.timeout), ""
        except Unreachable as exc:
            other, reason = None, str(exc)

        if args.record_pin:
            if other is None:
                return EXIT_UNREACHABLE, f"cannot record: {name} unreachable: {reason}\n", None
            owned = assign(entries, other.files)
            pin.sha, pin.hashes = sha, {entry: entry_hash(other.files, owned[entry]) for entry in entries}
            pin_file.write_text(render_pin(pin, entries), encoding="utf-8")
            return EXIT_OK, f"recorded {len(entries)} entry hashes for {name} {sha[:12]} in {pin_file}\n", None

        report = {
            "status": "", "mode": "",
            "local": {"ref": args.local_ref, "sha": local_sha},
            "other": {"name": name, "repo": redact(url), "sha": sha, "unreachable": reason or None},
            "pin_hashes": "absent" if not pin.hashes else "recorded" if recorded else "other-sha",
            "entries": [], "files": [], "summary": {},
        }
        rows: list[FileRow] = []
        if other is not None:
            report["mode"] = other.mode
            report["entries"], rows = compare_files(entries, local_files, other, local_sha, sha, args.timeout)
            report["files"] = [asdict(row) for row in rows]
            if recorded:
                owned = assign(entries, other.files)
                current = {entry: entry_hash(other.files, owned[entry]) for entry in entries}
                report["pin_hashes"] = "ok" if current == recorded else "stale"
            statuses = Counter(row.status for row in rows)
            drift_entries = sum(e["status"] != "identical" for e in report["entries"])
            report["summary"] = {
                "entries": len(entries), "identical_entries": len(entries) - drift_entries,
                "drift_entries": drift_entries,
                "empty_entries": sum(e["status"] == "empty" for e in report["entries"]),
                "files": len(rows), "identical": statuses["identical"], "diverged": statuses["diverged"],
                "missing_there": statuses["missing-there"], "missing_here": statuses["missing-here"],
            }
        elif recorded:
            report["mode"] = report["pin_hashes"] = "recorded"
            report["entries"] = compare_recorded(entries, local_files, recorded)
            drift_entries = sum(e["status"] != "identical" for e in report["entries"])
            report["summary"] = {
                "entries": len(entries), "identical_entries": len(entries) - drift_entries,
                "drift_entries": drift_entries,
                "unrecorded_entries": sum(e["status"] == "unrecorded" for e in report["entries"]),
            }
        else:
            report["mode"] = "unreachable"
            report["summary"] = {"entries": len(entries)}

    drift = report["mode"] != "unreachable" and (report["summary"]["drift_entries"] > 0
                                                 or report["pin_hashes"] == "stale")
    report["status"] = "UNREACHABLE" if report["mode"] == "unreachable" else "DRIFT" if drift else "CLEAN"
    report["summary"] = {"status": report["status"], "mode": report["mode"], **report["summary"]}
    if report["pin_hashes"] == "stale":
        report["summary"]["pin_hashes"] = "stale"
    if report["mode"] == "unreachable":
        code = EXIT_UNREACHABLE if args.fail_unreachable else EXIT_OK
    else:
        code = EXIT_DRIFT if drift and args.strict else EXIT_OK
    return code, render(report, rows), report


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, help="repository to compare (default: the enclosing work tree)")
    parser.add_argument("--paths", type=Path, help=f"shared-paths file (default: <root>/{PATHS_FILE})")
    parser.add_argument("--pin", type=Path, help=f"pin file (default: <root>/{PIN_FILE})")
    parser.add_argument("--repo", help="other repository URL (default: COMMON_OTHER_URL, else repo= in the pin)")
    parser.add_argument("--sha", help="other commit (default: COMMON_OTHER_SHA, else sha= in the pin)")
    parser.add_argument("--local-ref", default="HEAD", help="this repository's commit to compare (default: HEAD)")
    parser.add_argument("--strict", action="store_true", help="exit 1 on drift")
    parser.add_argument("--fail-unreachable", action="store_true",
                        help="exit 3 when the other repository cannot be read and nothing was compared")
    parser.add_argument("--record-pin", action="store_true",
                        help="write sha= and the entry hashes at that sha into the pin file; keeps name= and repo=")
    parser.add_argument("--report", type=Path, help="also write the text report to this file")
    parser.add_argument("--json", type=Path, help="write the report as JSON to this file")
    parser.add_argument("--timeout", type=float, default=120.0,
                        help="seconds allowed for each network git call (default: 120)")
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a positive number of seconds")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        code, text, report = run(args)
    except (ConfigError, GitFailed) as exc:
        code, text, report = EXIT_CONFIG, f"common_drift: error: {exc}\n", None
    except OSError as exc:
        code, text, report = EXIT_CONFIG, f"common_drift: error: {exc}\n", None
    text = text.encode("utf-8", "backslashreplace").decode("utf-8")
    (sys.stdout if code in (EXIT_OK, EXIT_DRIFT) or report else sys.stderr).write(text)
    try:
        if args.report:
            args.report.write_text(text, encoding="utf-8")
        if args.json and report is not None:
            args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(f"common_drift: error: {exc}\n")
        return EXIT_CONFIG
    return code


if __name__ == "__main__":
    sys.exit(main())
