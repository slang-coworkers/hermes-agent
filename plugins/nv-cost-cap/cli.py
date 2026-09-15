"""Argparse setup for the ``hermes cost-cap`` CLI verb.

Parser-only. The handler lives in the package ``__init__`` and is resolved at
dispatch time so it can self-gate against the active profile and reach the
plugin's runtime policy helpers.
"""

from __future__ import annotations

import argparse


def setup_cost_cap(parser: argparse.ArgumentParser) -> None:
    """Build ``hermes cost-cap {show|set}``."""
    subs = parser.add_subparsers(dest="cost_cap_command")

    subs.add_parser(
        "show", help="Show the resolved Tier-2 ceiling and per-session state"
    )

    set_p = subs.add_parser(
        "set", help="Set cost-cap policy (orchestrator profile only)"
    )
    set_p.add_argument(
        "--fleet-ceiling", dest="fleet_ceiling_usd", type=float, default=None,
        help="Owner-pinned fleet-wide Tier-2 ceiling in USD (refused when pinned by managed scope)",
    )
    set_p.add_argument(
        "--profile", dest="profile", default=None,
        help="Target profile for a per-profile ceiling override",
    )
    set_p.add_argument(
        "--ceiling", dest="profile_ceiling_usd", type=float, default=None,
        help="Per-profile Tier-2 ceiling override in USD (requires --profile)",
    )
