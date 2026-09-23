"""Argparse setup for the ``hermes cost-cap`` CLI verb.

Parser-only. The handler lives in the package ``__init__`` and is resolved at
dispatch time so it can self-gate against the active profile and reach the
plugin's runtime policy helpers.
"""

from __future__ import annotations

import argparse


def setup_cost_cap(parser: argparse.ArgumentParser) -> None:
    """Build ``hermes cost-cap {show|set|resolve}``."""
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

    # COST-F30: the panel-independent fallback — resolve an escalation episode from the
    # orchestrator CLI. Pins --profile around the full lookup/authz/CAS/apply.
    res_p = subs.add_parser(
        "resolve", help="Resolve an escalation episode (orchestrator profile only)"
    )
    res_p.add_argument("--profile", dest="profile", required=True,
                       help="Target profile that owns the episode")
    res_p.add_argument("--session", dest="session_id", required=True,
                       help="Session id of the escalation")
    res_p.add_argument("--episode", dest="episode_id", required=True,
                       help="Episode id to resolve")
    res_p.add_argument("--budget-gen", dest="budget_gen", type=int, required=True,
                       help="Budget generation of the episode")
    res_p.add_argument("--decision", dest="decision", required=True,
                       choices=["continue", "stop", "set-ceiling"],
                       help="continue | stop | set-ceiling")
    res_p.add_argument("--amount-usd", dest="amount_usd", type=float, default=None,
                       help="Exact ceiling in USD (required for --decision set-ceiling)")
