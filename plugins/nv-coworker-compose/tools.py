"""Gateway-tool JSON schemas for nv-coworker-compose.

Schemas only — pure data, no seams. ``get_definitions`` adds the ``name`` key
at call time (tools/registry.py:1090), so these carry only ``description`` and
``parameters``.
"""

from __future__ import annotations

ONBOARD_COWORKER_SCHEMA = {
    "description": (
        "Render the coworker-types.yaml lego spec into profile distributions, "
        "install them, and write each coworker's Bot-Mode identity "
        "(ui_meta['hermes-bots']), canonical Bot Chat session and standing rooms "
        "into the running gateway. Orchestrator-only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "spec": {
                "type": "string",
                "description": "Path to the coworker-types.yaml spec to onboard.",
            },
        },
        "required": ["spec"],
    },
}

ONBOARD_PROJECT_SCHEMA = {
    "description": (
        "Scaffold a Hermes project distribution from a local path or git URL: a "
        "bounded, binary-skipping scan of the source becomes skill capabilities. "
        "Orchestrator-only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "Local repository path or git URL to scaffold from.",
            },
            "max_files": {
                "type": "integer",
                "description": "Upper bound on eligible source files surfaced into the scaffold.",
            },
        },
        "required": ["source"],
    },
}

CREATE_AGENT_SCHEMA = {
    "description": (
        "Create a new coworker profile in the fleet (wraps profiles.create). "
        "Orchestrator-only."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "New profile name."},
            "description": {"type": "string", "description": "Role description for the coworker."},
            "role": {"type": "string", "description": "Fleet role (e.g. worker, reviewer)."},
        },
        "required": ["name"],
    },
}
