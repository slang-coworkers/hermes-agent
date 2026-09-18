"""Acceptance test for OPS-F58 — ncl admin CLI (adopt the native `hermes` CLI).

ADOPT / no-plugin / no-core-edit row. Proves the native `hermes` CLI admin
verbs that stand in for ncl's admin resources actually function through the
production dispatch path (profiles = agent groups, cron = tasks, kanban =
board, sessions), that the mapped verbs are wired into the production top-level
parser while no room/group admin verb is, and that the mapping doc page covers
every ncl resource and records the room-admin P8 gap.

Fail-before / pass-after (inverted for an adopt row): on stock v2026.8.31 the
file fails because test_ac_ops_f58_6 errors (mapping page absent); the other
five are green-on-stock behaviour-contract conformance proofs of the adopted
native CLI.
"""
from __future__ import annotations

import io
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

DOC = Path("website/docs/user-guide/ncl-to-hermes-admin-cli.md")


def _run_cli(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> int:
    """Drive the production CLI (`hermes <argv>`) in-process; return exit code.

    Plugin CLI discovery is patched off (as the --help oracle does) to keep the
    run hermetic; SystemExit(None|0) is success.
    """
    from hermes_cli import main as _main

    monkeypatch.setattr(sys, "argv", ["hermes", *argv])
    with patch.object(_main, "_plugin_cli_discovery_needed", return_value=False):
        try:
            _main.main()
            return 0
        except SystemExit as exc:
            if exc.code is None:
                return 0
            return exc.code if isinstance(exc.code, int) else 1


def _live_subcommand_names() -> set[str]:
    """Top-level subcommands from `hermes --help` (source-free parity oracle).

    Replicates tests/hermes_cli/test_startup_plugin_gating.py:43-67.
    """
    from hermes_cli import main as _main

    argv_backup = sys.argv[:]
    sys.argv = ["hermes", "--help"]
    buf = io.StringIO()
    try:
        with patch.object(_main, "_plugin_cli_discovery_needed", return_value=False):
            with redirect_stdout(buf):
                with pytest.raises(SystemExit):
                    _main.main()
    finally:
        sys.argv = argv_backup
    m = re.search(r"\{([a-zA-Z0-9_,\-]+)\}", buf.getvalue())
    assert m, f"no subcommand group in --help:\n{buf.getvalue()[:500]}"
    return set(m.group(1).split(","))


def _parse_mapping_table(text: str) -> list[dict[str, str]]:
    """Parse the 4-column ncl->hermes mapping table into dict rows (strict).

    Locates the table whose header row mentions 'nanoclaw', verifies the header
    is exactly the four columns resource/hermes/anchor/disposition, then reads
    the pipe-delimited body rows (skipping the |---| separator). Every body row
    MUST have exactly four cells — a row that splits into any other count (e.g.
    a stray `main:` pipe corrupting the columns) fails the parse.
    """
    lines = text.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("|") and "nanoclaw" in line.lower():
            header_idx = i
            break
    assert header_idx is not None, "mapping table header (mentioning 'NanoClaw') not found"

    header = [c.strip().lower() for c in lines[header_idx].strip().strip("|").split("|")]
    expected_header = [
        "nanoclaw resource / verb", "hermes equivalent", "native anchor", "disposition / owner",
    ]
    assert header == expected_header, f"mapping table header must be {expected_header}, got {header}"

    rows: list[dict[str, str]] = []
    for line in lines[header_idx + 1:]:
        s = line.strip()
        if not s.startswith("|"):
            break
        cells = [c.strip() for c in s.strip("|").split("|")]
        if set("".join(cells)) <= set("-: "):  # the |---|---| separator row
            continue
        assert len(cells) == 4, f"mapping row must have exactly 4 cells, got {len(cells)}: {s!r}"
        rows.append(
            {"resource": cells[0], "hermes": cells[1], "anchor": cells[2], "disposition": cells[3]}
        )
    return rows


def test_ac_ops_f58_1(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    """`hermes profile` creates, lists and deletes a profile (the agent-group analog).

    A profile created through the CLI appears in the profile registry and is
    absent after `profile delete -y`, scoped to a temp HERMES_HOME.
    """
    import hermes_constants
    from hermes_cli.profiles import list_profiles

    # profile_env shape (AGENTS.md:1566-1573): Path.home + HERMES_HOME both, so
    # no alias wrapper touches the real ~/.local/bin.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(hermes_constants, "_default_hermes_root_memo", None, raising=False)

    name = "f58bot"
    assert name not in {p.name for p in list_profiles()}

    assert _run_cli(monkeypatch, ["profile", "create", name]) == 0
    assert name in {p.name for p in list_profiles()}, "created profile not in registry"

    capsys.readouterr()  # drop create noise
    assert _run_cli(monkeypatch, ["profile", "list"]) == 0
    assert name in capsys.readouterr().out, "created profile not shown by `profile list`"

    assert _run_cli(monkeypatch, ["profile", "delete", name, "-y"]) == 0
    assert name not in {p.name for p in list_profiles()}, "deleted profile still present"


def test_ac_ops_f58_2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    """`hermes cron` creates, lists and removes a scheduled job (the tasks analog).

    A job created through the CLI is shown by `cron list` and gone after
    `cron remove`, scoped to a temp cron store.
    """
    from cron.jobs import list_jobs, use_cron_store

    home = tmp_path / "cronhome"
    home.mkdir()
    name = "f58job"
    with use_cron_store(home):
        assert not [j for j in list_jobs() if j.get("name") == name]

        assert _run_cli(
            monkeypatch, ["cron", "create", "every 1h", "OPS-F58 smoke", "--name", name]
        ) == 0
        created = [j for j in list_jobs() if j.get("name") == name]
        assert len(created) == 1, "created cron job not persisted"
        job_id = created[0]["id"]

        capsys.readouterr()  # drop create noise
        assert _run_cli(monkeypatch, ["cron", "list"]) == 0
        assert name in capsys.readouterr().out, "created job not shown by `cron list`"

        assert _run_cli(monkeypatch, ["cron", "remove", job_id]) == 0
        assert not [j for j in list_jobs() if j.get("id") == job_id], "removed job still present"


def test_ac_ops_f58_3(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    """`hermes kanban boards` creates and lists a board (the board analog).

    A board created through the CLI is shown by `boards list` and drops out of
    the active (non-archived) set after `boards rm`.
    """
    import os

    import hermes_constants
    from hermes_cli import kanban_db as kb

    home = tmp_path / "kbhome"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # fresh_home shape (test_kanban_boards.py:48-63): clear HERMES_KANBAN_* overrides,
    # reset the root-dir memo and the kanban path cache so the board lands under `home`.
    for var in [k for k in os.environ if k.startswith("HERMES_KANBAN_")]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(hermes_constants, "_default_hermes_root_memo", None, raising=False)
    kb._INITIALIZED_PATHS.clear()

    slug = "f58board"
    assert slug not in {b["slug"] for b in kb.list_boards()}

    assert _run_cli(monkeypatch, ["kanban", "boards", "create", slug]) == 0
    assert slug in {b["slug"] for b in kb.list_boards()}, "created board not persisted"

    capsys.readouterr()  # drop create noise
    assert _run_cli(monkeypatch, ["kanban", "boards", "list"]) == 0
    assert slug in capsys.readouterr().out, "created board not shown by `boards list`"

    assert _run_cli(monkeypatch, ["kanban", "boards", "rm", slug]) == 0
    assert slug not in {b["slug"] for b in kb.list_boards(include_archived=False)}, (
        "removed board still active"
    )


def test_ac_ops_f58_4(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """`hermes sessions list` lists a persisted session (the sessions analog).

    A session seeded in the state DB is shown in the CLI list output.
    """
    from hermes_state import SessionDB

    # conftest _hermetic_environment repins SessionDB's DEFAULT_DB_PATH to the
    # temp home's state.db, so an argless SessionDB() and the CLI share one DB.
    sess_id = "f58sess"
    db = SessionDB()
    db.create_session(sess_id, source="cli")
    db.close()

    capsys.readouterr()  # drop setup noise
    assert _run_cli(monkeypatch, ["sessions", "list", "--limit", "100"]) == 0
    out = capsys.readouterr().out
    assert sess_id in out, f"seeded session not listed by the CLI:\n{out[:500]}"


def test_ac_ops_f58_5() -> None:
    """The mapped admin verbs are wired into the production top-level `hermes` parser.

    profile/cron/sessions/kanban/pairing/approvals/config/gateway are reachable
    operator verbs (a subset of the live top-level subcommands), not merely
    importable internals.
    """
    live = _live_subcommand_names()
    mapped = {
        "profile", "cron", "sessions", "kanban", "pairing", "approvals", "config", "gateway",
        "webhook", "secrets", "egress", "monitoring", "insights", "plugins", "skills",
    }
    missing = mapped - live
    assert not missing, f"mapped admin verbs absent from `hermes --help`: {sorted(missing)}"

    # The P8 absence proof: the production top-level parser exposes NO room/group admin
    # verb (a missing _BUILTIN_SUBCOMMANDS entry would merely trigger discovery, so the
    # live --help set — not that allowlist — is what proves the gap).
    unexpected = {"rooms", "groups"} & live
    assert not unexpected, f"unexpected built-in room-admin verbs (P8 gap violated): {sorted(unexpected)}"


def test_ac_ops_f58_6() -> None:
    """The mapping doc covers every ncl admin resource and records the room-admin gap.

    Every required ncl resource is a table row with non-placeholder cells and a
    real pinned-tag anchor, the mapped native verbs are named, and the
    room-admin P8 gap boundary (gateway-hosted groups.* RPC, no
    membership-mutation op) is documented inside its own gap section.
    """
    assert DOC.exists(), f"mapping doc page missing: {DOC}"
    text = DOC.read_text(encoding="utf-8")

    rows = _parse_mapping_table(text)
    by_resource = {
        r["resource"].strip().lower().split()[0].rstrip(":"): r
        for r in rows if r["resource"].strip()
    }
    required = {
        "groups", "messaging-groups", "wirings", "users", "roles", "members",
        "destinations", "dropped-messages", "pr-mappings", "cost-cap",
        "policies", "tasks", "sessions",
    }
    missing = required - set(by_resource)
    assert not missing, f"mapping table missing ncl resources: {sorted(missing)}"

    placeholders = {"", "-", "—", "todo", "tbd", "?", "…", "...", "n/a", "x", "xx"}
    valid_disp = ("adopted", "owned", "gap", "no analog", "no-analog", "baseline", "defer")
    # Exact pinned-tag citation token per resource (native `path:line`, or an owning req
    # id for OWNED/DEFER rows). Tokens are extracted whole from the cell before matching,
    # so a mis-lined anchor (e.g. profile.py:120000) yields a different token and fails.
    token_re = re.compile(r"[\w./-]+\.(?:py|md|ts):\d+|plugins/[\w./-]+|[a-z0-9]+-f\d+")
    anchor_tokens = {
        "groups": ("hermes_cli/subcommands/profile.py:12",),
        "messaging-groups": ("tui_gateway/methods_groups.py:508",),
        "wirings": ("plugins/nv-fleet-gates", "a2a-f18"),
        "users": ("hermes_cli/subcommands/pairing.py:21",),
        "roles": ("gov-f26",),
        "members": ("gateway/hosted_rooms.py:1414",),
        "destinations": ("a2a-f18", "ch-f51"),
        "dropped-messages": ("rt-f06",),
        "pr-mappings": ("gov-f25",),
        "cost-cap": ("cost-f29", "cost-f30"),
        "policies": ("hermes_cli/subcommands/approvals.py:14", "a2a-f19"),
        "tasks": ("hermes_cli/subcommands/cron.py:15",),
        "sessions": ("hermes_cli/sessions_cmd.py:124",),
    }
    for res in required:
        row = by_resource[res]
        for col in ("hermes", "anchor", "disposition"):
            assert row[col].strip().lower() not in placeholders, (
                f"placeholder cell for resource {res!r} column {col!r}"
            )
        found = set(token_re.findall(row["anchor"].lower()))
        assert found.intersection(anchor_tokens[res]), (
            f"{res!r} anchor names none of the pinned tokens {anchor_tokens[res]}: {row['anchor']!r}"
        )
        disp = row["disposition"].lower()
        assert any(k in disp for k in valid_disp), (
            f"{res!r} disposition not classified: {row['disposition']!r}"
        )

    expected_verbs = {
        "groups": ("hermes profile list", "hermes profile create", "hermes profile delete"),
        "tasks": ("hermes cron create", "hermes cron list", "hermes cron remove"),
        "sessions": ("hermes sessions list",),
    }
    for res, cmds in expected_verbs.items():
        cell = by_resource[res]["hermes"].lower()
        for cmd in cmds:
            assert cmd in cell, f"{res!r} row's Hermes cell must name {cmd!r}: {cell!r}"

    for res in ("messaging-groups", "members"):
        disp = by_resource[res]["disposition"].lower()
        assert "gap" in disp or "p8" in disp, f"{res!r} must be dispositioned GAP/P8: {disp!r}"

    # The room-administration P8 gap boundary, asserted ONLY within its own section.
    hm = re.search(r"^##+ .*(room-administration gap|p8 upstream ask)", text, re.I | re.M)
    assert hm, "doc lacks the '## No native analog — the room-administration gap (P8 upstream ask)' section"
    after = text[hm.end():]
    nxt = re.search(r"^##+ ", after, re.M)
    gap = (text[hm.start(): hm.end() + nxt.start()] if nxt else text[hm.start():]).lower()
    assert "groups.create" in gap or "groups.*" in gap, "gap section must name the groups.* RPC"
    assert "gateway" in gap, "gap section must state the room service is gateway-hosted"
    assert "membership" in gap, "gap section must state the missing membership-mutation op"
    assert "desktop" in gap or "dashboard" in gap, "gap section must name the desktop/dashboard-only surface"
    assert "p8" in gap, "gap section must mark the upstream ask (P8)"
