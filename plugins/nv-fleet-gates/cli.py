"""Argparse setup for the ``hermes wire`` and ``hermes learnings`` CLI verbs.

Parser-only. The handlers live in the package ``__init__`` and in ``learnings.py``
and are resolved there at dispatch time, so they close over the profile's settings
(``edges_db_path``, the ``learnings`` block).
"""

from __future__ import annotations

import argparse


def setup_wire(parser: argparse.ArgumentParser) -> None:
    """Build ``hermes wire {add|remove|list}``."""
    subs = parser.add_subparsers(dest="wire_command")

    add_p = subs.add_parser("add", help="Wire two profiles (bidirectional)")
    add_p.add_argument("frm", metavar="from", help="Source profile")
    add_p.add_argument("to", help="Target profile")
    add_p.add_argument(
        "--gated", action="store_true",
        help="Require human approval each time this edge is traversed",
    )

    rm_p = subs.add_parser("remove", help="Unwire two profiles (bidirectional)")
    rm_p.add_argument("frm", metavar="from", help="Source profile")
    rm_p.add_argument("to", help="Target profile")

    subs.add_parser("list", help="List the fleet's wiring edges")


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


def setup_learnings(parser: argparse.ArgumentParser) -> None:
    """Build ``hermes learnings {list|show|quarantine|release|purge|compact|verify|scan|stats}`` (operator only)."""
    subs = parser.add_subparsers(dest="learnings_command")

    list_p = subs.add_parser("list", help="List entries, newest first (active ones by default)")
    which = list_p.add_mutually_exclusive_group()
    which.add_argument("--all", action="store_true", help="Active and quarantined entries")
    which.add_argument("--quarantined", action="store_true", help="Quarantined entries only")
    list_p.add_argument("--limit", type=_positive, default=50, help="At most this many (default 50)")

    show_p = subs.add_parser("show", help="One entry with its stamps, hashes and events")
    show_p.add_argument("id", type=_positive)

    for verb, text in (("quarantine", "Hide an entry from search (an event; the entry stays as it is)"),
                       ("release", "Return a quarantined entry to search (an event)")):
        state_p = subs.add_parser(verb, help=text)
        state_p.add_argument("id", type=_positive)
        state_p.add_argument("--reason", required=True, help="Why, in one technical line (no names)")

    purge_p = subs.add_parser("purge", help="Delete an entry for good; a purge event keeps the chain checkpoint "
                                            "(exit 1 when compact must run again; snapshots keep their copies)")
    purge_p.add_argument("id", type=_positive)
    purge_p.add_argument("--reason", required=True, help="Why, in one technical line (no names)")
    purge_p.add_argument("--confirm", action="store_true", help="Required: a purged entry cannot be restored")

    subs.add_parser("compact", help="Rewrite the store so no freed page or write-ahead log keeps purged text (exit 1: "
                                    "a reader held the store, run it again)")

    subs.add_parser("verify", help="Check both hash chains and the append-only triggers (exit 1 on a break)")

    scan_p = subs.add_parser("scan", help="Re-run the write-time content checks on active entries (exit 1 on a hit)")
    scan_p.add_argument("--apply", action="store_true",
                        help="Quarantine each hit, except entries an operator released")

    stats_p = subs.add_parser("stats", help="Counts for the daily report")
    stats_p.add_argument("--hours", type=_positive, default=24, help="The window for new entries and events")
