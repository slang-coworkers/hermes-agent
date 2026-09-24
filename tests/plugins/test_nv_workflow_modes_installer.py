"""Ownership and safety tests for the nv-workflow-modes installer.

Two groups: pure-planner tests that call build_install_plan / the ownership
predicate directly, and installer tests that drive _install() against a
temp-profile HERMES_HOME. A foreign marker that merely contains the plugin key
as a substring, a body that differs only in line endings, and a symlinked
destination are all user-owned/unsafe and must never be silently followed or
overwritten; a containment failure must refuse before any destructive op. No
network is touched.
"""

from __future__ import annotations

import importlib.util
import shutil
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


def test_symlinked_dest_nonforce_refused_no_write_outside_profile(tmp_path, monkeypatch):
    """AC-6 profile-scoped: a symlinked mode dir (even one whose target body is
    byte-identical to the shipped one) is refused without --force, and NOTHING is
    written through it — no marker outside the profile, external target untouched,
    and no other mode written (atomic)."""
    profile = tmp_path / "profile"
    (profile / "skills").mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    shutil.copyfile(SRC / "implement" / "SKILL.md", external / "SKILL.md")  # the byte-identical trap
    (profile / "skills" / "implement").symlink_to(external, target_is_directory=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))

    rc = M._install(force=False)

    assert rc not in (None, 0), "non-force must refuse a symlinked dest"
    assert (profile / "skills" / "implement").is_symlink(), "the symlink was disturbed"
    assert not (external / ".installed-by").exists(), "wrote a marker outside the profile"
    assert sorted(p.name for p in external.iterdir()) == ["SKILL.md"], "external dir mutated"
    for mode in ("plan-investigate", "plan-review", "plan-research"):
        assert not (profile / "skills" / mode).exists(), f"refusal not atomic — {mode} written"


def test_symlinked_dest_force_replaced_in_profile_external_untouched(tmp_path, monkeypatch):
    """AC-6 never-clobbers: with --force a symlinked mode dir is replaced by a real
    dir UNDER the profile; the write never follows the symlink, so external user
    data is left byte-for-byte intact."""
    profile = tmp_path / "profile"
    (profile / "skills").mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    user_body = b"EXTERNAL USER DATA -- must not be clobbered\n"
    (external / "SKILL.md").write_bytes(user_body)
    (profile / "skills" / "implement").symlink_to(external, target_is_directory=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))

    rc = M._install(force=True)

    assert rc in (None, 0), f"--force install failed (rc={rc!r})"
    dest = profile / "skills" / "implement"
    assert not dest.is_symlink() and dest.is_dir(), "symlink not replaced by a real dir"
    assert (dest / ".installed-by").is_file(), "marker missing after --force"
    assert (dest / "SKILL.md").read_bytes() == (SRC / "implement" / "SKILL.md").read_bytes()
    assert (external / "SKILL.md").read_bytes() == user_body, "external data was clobbered"
    assert not (external / ".installed-by").exists(), "wrote a marker outside the profile"


def test_containment_refusal_is_preflight_and_destroys_nothing(tmp_path, monkeypatch):
    """A destination that fails the profile-containment check is refused BEFORE any
    destructive op: an existing dir at a to-copy mode path must survive the refusal
    (ordering guard — no unlink/rmtree before the containment gate)."""
    profile = tmp_path / "profile"
    victim = profile / "skills" / "plan-investigate"  # first mode processed
    victim.mkdir(parents=True)
    (victim / "SENTINEL").write_text("keep me", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setattr(M, "_within", lambda base, p: False)

    rc = M._install(force=True)

    assert rc not in (None, 0), "must refuse when a dest escapes the skills tree"
    assert (victim / "SENTINEL").is_file(), "destroyed an existing dir before the containment refusal"


def test_installed_bodies_are_byte_complete(tmp_path, monkeypatch):
    """The full shipped workflow body is installed byte-for-byte for every mode."""
    profile = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(profile))
    rc = M._install(force=False)
    assert rc in (None, 0)
    for mode in M.MODES:
        installed = (profile / "skills" / mode / "SKILL.md").read_bytes()
        assert installed == (SRC / mode / "SKILL.md").read_bytes(), f"{mode} body not byte-complete"
