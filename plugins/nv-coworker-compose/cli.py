"""Argparse setup functions for nv-coworker-compose CLI verbs.

Parser-only: these build the ``hermes coworker`` and ``hermes onboard``
sub-subparser trees and nothing else. The handlers live in the package
``__init__`` and are resolved there at dispatch time, so the onboarding and
dispatch seams are looked up through the package module's globals rather than
through an alias captured in this module.
"""

from __future__ import annotations

import argparse


def setup_coworker(parser: argparse.ArgumentParser) -> None:
    """Build ``hermes coworker <subcommand>``."""
    subs = parser.add_subparsers(dest="coworker_command")
    compose_p = subs.add_parser("compose", help="Render coworker profile distributions from a coworker-types.yaml spec")
    compose_p.add_argument("spec", help="Path to coworker-types.yaml")
    compose_p.add_argument("--out", default=None, help="Output directory for the rendered distributions")


def setup_onboard(parser: argparse.ArgumentParser) -> None:
    """Build ``hermes onboard <subcommand>``."""
    subs = parser.add_subparsers(dest="onboard_command")

    project_p = subs.add_parser("project", help="Scaffold a project distribution from a repo path or URL")
    project_p.add_argument("src", help="Local path or git URL")
    project_p.add_argument("--out", default=None, help="Output directory for the scaffold")
    project_p.add_argument("--max-files", type=int, default=64, help="Bound on eligible source files scanned")

    coworker_p = subs.add_parser("coworker", help="Render + onboard coworker profiles into a gateway")
    coworker_p.add_argument("spec", help="Path to coworker-types.yaml")
    coworker_p.add_argument("--gateway-url", default=None, help="Gateway /api/ws URL with a ?token or ?ticket credential")
