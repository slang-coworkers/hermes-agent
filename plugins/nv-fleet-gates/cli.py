"""Argparse setup for the ``hermes wire`` CLI verb.

Parser-only. The handler lives in the package ``__init__`` and is resolved there
at dispatch time, so it closes over the profile's ``edges_db_path``.
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
