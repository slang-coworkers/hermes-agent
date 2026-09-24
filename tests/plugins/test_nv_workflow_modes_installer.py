"""Ownership tests for the nv-workflow-modes installer planner.

A foreign marker that merely contains the plugin key as a substring, and a body
that differs only in line endings, are user-owned conflicts and must never be
silently overwritten. These call the plugin's pure functions directly; no
HERMES_HOME or network is touched.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_INIT = _REPO / "plugins" / "nv-workflow-modes" / "__init__.py"


def _load_module():
    if not _INIT.is_file():
        pytest.fail(f"nv-workflow-modes plugin missing (stock-tree red): {_INIT}")
    spec = importlib.util.spec_from_file_location("nv_workflow_modes_under_test", _INIT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load_module()
SRC = _INIT.parent / "skills"


def _seed(dest_dir: Path, mode: str, body: bytes, marker: str | None = None) -> None:
    d = dest_dir / mode
    d.mkdir(parents=True)
    (d / "SKILL.md").write_bytes(body)
    if marker is not None:
        (d / ".installed-by").write_text(marker, encoding="utf-8")


def test_fresh_install_copies_all_modes(tmp_path):
    to_copy, conflicts = M.build_install_plan(SRC, tmp_path / "skills", force=False)
    assert set(to_copy) == set(M.MODES)
    assert conflicts == []


def test_our_marker_dir_is_idempotent(tmp_path):
    dest = tmp_path / "skills"
    _seed(dest, "implement", b"stale bytes", marker="nv-workflow-modes\n")
    to_copy, conflicts = M.build_install_plan(SRC, dest, force=False)
    assert "implement" in to_copy and conflicts == []


def test_byte_identical_body_without_marker_is_ours(tmp_path):
    dest = tmp_path / "skills"
    body = (SRC / "implement" / "SKILL.md").read_bytes()
    _seed(dest, "implement", body)
    to_copy, conflicts = M.build_install_plan(SRC, dest, force=False)
    assert "implement" in to_copy and conflicts == []


def test_foreign_marker_substring_is_a_conflict(tmp_path):
    """'not-nv-workflow-modes' contains the key as a substring but is not a
    whole-line match, so the dir is user-owned and must not be overwritten."""
    dest = tmp_path / "skills"
    _seed(dest, "implement", b"USER BODY", marker="not-nv-workflow-modes\n")
    to_copy, conflicts = M.build_install_plan(SRC, dest, force=False)
    assert "implement" in conflicts
    assert "implement" not in to_copy


def test_crlf_only_difference_is_a_conflict(tmp_path):
    """A body identical except for CRLF vs LF is not byte-identical and must not
    be treated as ours."""
    dest = tmp_path / "skills"
    crlf = (SRC / "implement" / "SKILL.md").read_bytes().replace(b"\n", b"\r\n")
    _seed(dest, "implement", crlf)
    to_copy, conflicts = M.build_install_plan(SRC, dest, force=False)
    assert "implement" in conflicts


def test_force_copies_over_a_user_owned_dir(tmp_path):
    dest = tmp_path / "skills"
    _seed(dest, "implement", b"USER BODY", marker="not-nv-workflow-modes\n")
    to_copy, conflicts = M.build_install_plan(SRC, dest, force=True)
    assert set(to_copy) == set(M.MODES)
    assert conflicts == []


def test_has_our_marker_is_line_exact(tmp_path):
    dest = tmp_path / "skills"
    _seed(dest, "implement", b"x", marker="nv-workflow-modes\n")
    assert M._has_our_marker(dest / "implement") is True
    _seed(dest, "plan-review", b"x", marker="prefix-nv-workflow-modes-suffix\n")
    assert M._has_our_marker(dest / "plan-review") is False
