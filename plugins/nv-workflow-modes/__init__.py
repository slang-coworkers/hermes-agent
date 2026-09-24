"""nv-workflow-modes — port the NanoClaw plan-modes with no stock builtin
(investigate, review, research) plus /implement as /slash workflow-body skills.

``register(ctx)`` registers one CLI subcommand, ``hermes workflow-modes install``,
that copies the four bundled ``SKILL.md`` workflow bodies into the active
profile's scanned skills tree (``get_hermes_home()/skills``). The skill scanner
then mints ``/plan-investigate``, ``/plan-review``, ``/plan-research`` and
``/implement`` and seeds a turn with each body (the mode is fixed by the slug;
the user's trailing text becomes the skill ``user_instruction``). Stock ``/plan``
and ``/review`` are the pre-existing, non-equivalent surface and are untouched.
See website/docs/developer-guide/plugins/nv-workflow-modes.md.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from hermes_constants import get_hermes_home

PLUGIN_KEY = "nv-workflow-modes"
MODES = ("plan-investigate", "plan-review", "plan-research", "implement")
_MARKER = ".installed-by"


def _bundled_skills_dir() -> Path:
    return Path(__file__).resolve().parent / "skills"


def _is_ours(dest_mode: Path, src_skill: Path) -> bool:
    """A dest dir is safe to overwrite only when this plugin created it (its
    ownership marker names us) or its body is byte-identical to what we ship —
    otherwise it is a user's own skill and must not be touched without --force."""
    marker = dest_mode / _MARKER
    if marker.is_file() and PLUGIN_KEY in marker.read_text(encoding="utf-8"):
        return True
    dest_skill = dest_mode / "SKILL.md"
    if dest_skill.is_file() and src_skill.is_file():
        return dest_skill.read_text(encoding="utf-8") == src_skill.read_text(encoding="utf-8")
    return False


def build_install_plan(src_dir: Path, dest_dir: Path, force: bool) -> tuple[list[str], list[str]]:
    """Pure planner, no side effects. Returns ``(to_copy, conflicts)``.

    Per mode: dest absent -> copy; dest present and ours/byte-identical -> copy
    (idempotent); a user-owned/divergent dir -> conflict. With ``force`` every
    mode is copied and there are no conflicts.
    """
    to_copy: list[str] = []
    conflicts: list[str] = []
    for mode in MODES:
        dest_mode = dest_dir / mode
        if force or not dest_mode.exists() or _is_ours(dest_mode, src_dir / mode / "SKILL.md"):
            to_copy.append(mode)
        else:
            conflicts.append(mode)
    return to_copy, conflicts


def _install(force: bool) -> int:
    src_dir = _bundled_skills_dir()
    dest_dir = get_hermes_home() / "skills"
    to_copy, conflicts = build_install_plan(src_dir, dest_dir, force)

    # Atomic preflight: a single unmarked/divergent dir refuses the WHOLE install
    # (nothing below runs) so a user's own skill is never partially clobbered.
    if conflicts:
        print(
            "workflow-modes: refusing to install — these skill dirs already exist "
            "and were not created by this plugin:\n  "
            + "\n  ".join(str(dest_dir / m) for m in conflicts)
            + "\nRe-run with --force to replace them (their contents will be lost)."
        )
        return 2

    dest_dir.mkdir(parents=True, exist_ok=True)
    for mode in to_copy:
        dest_mode = dest_dir / mode
        dest_mode.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src_dir / mode / "SKILL.md", dest_mode / "SKILL.md")
        (dest_mode / _MARKER).write_text(PLUGIN_KEY + "\n", encoding="utf-8")

    print(
        "workflow-modes: installed "
        + ", ".join(f"/{m}" for m in to_copy)
        + f"\n  into {dest_dir}\n"
        + "Run /reload-skills (or restart the gateway) to register the new commands."
    )
    return 0


def _list() -> int:
    dest_dir = get_hermes_home() / "skills"
    print("workflow-modes — ported plan-mode / implement workflow bodies:")
    for mode in MODES:
        dest_mode = dest_dir / mode
        marker = dest_mode / _MARKER
        if marker.is_file() and PLUGIN_KEY in marker.read_text(encoding="utf-8"):
            state = "installed"
        elif dest_mode.exists():
            state = "present (not ours)"
        else:
            state = "not installed"
        print(f"  /{mode:<17} {state}")
    return 0


def _handle(args: argparse.Namespace) -> int:
    action = getattr(args, "action", None)
    if action == "install":
        return _install(force=getattr(args, "force", False))
    if action == "list":
        return _list()
    print("usage: hermes workflow-modes {install [--force] | list}")
    return 2


def _setup(subparser: argparse.ArgumentParser) -> None:
    actions = subparser.add_subparsers(dest="action")
    p_install = actions.add_parser(
        "install", help="Install the workflow-mode skills into this profile's skills tree"
    )
    p_install.add_argument(
        "--force", action="store_true", help="Replace a user-owned skill dir at a mode path"
    )
    actions.add_parser("list", help="Show each mode's install status")
    subparser.set_defaults(func=_handle)


def register(ctx) -> None:
    ctx.register_cli_command(
        name="workflow-modes",
        help="Install the ported plan-mode and /implement workflow bodies",
        setup_fn=_setup,
        handler_fn=_handle,
        description=(
            "Copy the four ported workflow-body skills into the active profile's "
            "skills tree so they auto-register as /plan-investigate, /plan-review, "
            "/plan-research and /implement. Idempotent and atomic; refuses to "
            "clobber a user-owned skill dir unless --force is given."
        ),
    )
