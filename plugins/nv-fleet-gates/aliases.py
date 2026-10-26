"""Vendored tool-name alias map for nv-fleet-gates.

Copied — deliberately NOT imported — from core's private ``_LEGACY_TOOL_ALIASES``.
Core drops that private, un-shimmed map past the 2026-09-14 compat cliff, so the
gate keeps its own copy to canonicalise the legacy spellings it must still match.

One-way rule: canonicalise the names you MATCH (incoming ``tool_name``); resolve
the names you DISPATCH against the live registry, since ``registry.dispatch``
returns an error result (not a raise) on an unknown name.
"""

from __future__ import annotations

# Legacy pinned spelling -> main/canonical spelling.
_LEGACY_TOOL_ALIASES = {
    "todo": "todo_list",
    "cronjob": "cronjob_manage",
    "process": "process_manage",
    "tour": "gui_tour",
    "tip": "show_tip",
}


def canonicalise(name: str) -> str:
    """Return the canonical (main) spelling of *name*, or *name* unchanged."""
    if not isinstance(name, str):
        return ""
    return _LEGACY_TOOL_ALIASES.get(name, name)
