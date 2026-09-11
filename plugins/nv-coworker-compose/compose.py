"""Offline renderer for nv-coworker-compose.

Resolves a ``coworker-types.yaml`` lego spec (identity leaf-wins;
invariants/context/skills/workflows/overlays append+dedup; trait-domain
bindings to config) and writes, per coworker type, a complete Hermes profile
distribution SOURCE tree. Pure, offline file output.

Spec-controlled names and source paths are constrained to safe relative
components under the spec directory, so a hostile spec cannot read or overwrite
files outside its own tree.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Dict, List

import yaml

# Spec-controlled names become directory/file components and profile names.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

# Traits are a small, closed domain — an unknown trait is a spec error, never a
# silently-dropped (inert) config key. The dotted target must be a key a reader
# actually consults: the command allowlist is read TOP-LEVEL via
# config.get("command_allowlist") (approval.py:3188), so a nested
# agent.command_allowlist would be inert. Extend this map only when a new trait
# domain is genuinely wired to a config reader.
_TRAIT_DOMAIN: Dict[str, str] = {
    "command_allowlist": "command_allowlist",
}

# This plugin's own key. The render enables it fleet-wide on the coworker
# profiles so its orchestrator-gated onboard tools LOAD everywhere; check_fn then
# keeps them usable only on the orchestrator profile.
_SELF_PLUGIN = "nv-coworker-compose"

# Transcript-retention keys enforced as fleet invariants on every rendered
# profile (the DEFAULT multiplexer and every coworker), written whether the input
# spec omits them or declares a different value. Pinned explicitly so a re-pin to
# a tree whose sessions.auto_prune default has flipped to True cannot silently
# start deleting on-disk transcripts + request_dump_* files. Every key has a real
# reader at the pin (see the MEM-F44 fleet-transcript-retention runbook).
_RETENTION_INVARIANTS: Dict[str, Any] = {
    "sessions.auto_prune": False,
    "sessions.auto_archive": True,
    "compression.in_place": True,
    "checkpoints.auto_prune": False,
}

# Engage-mode → per-platform gate keys (RT-F02). ``config.engage`` is the plugin's
# OWN spec vocabulary — a pseudo-key with no core reader — so _render_engage
# translates it into the concrete keys each platform adapter reads under
# ``platforms.<p>.extra`` and removes it. This table is fail-closed: a
# (platform, mode) pair absent here raises CompositionError rather than rendering
# an unrecognised or unrestricting gate. The key sets DIVERGE per platform because
# the adapters diverge — Slack carries the full strict/thread/pattern set; Discord
# has no strict_mention/mention_patterns resolver (so ``pattern`` is unsupported);
# Telegram is chat-scoped (free_response_chats/allowed_chats) with no strict/thread
# sticky model (so ``mention-sticky`` is unsupported). ``mention`` keeps sticky OFF
# (strict + thread both true); ``mention-sticky`` turns it ON (both false), which is
# what enables Slack's _register_mentioned_thread wake.
_ALL_ENGAGE_MODES = ("mention", "mention-sticky", "pattern", "always-on")

_ENGAGE_CAPS: Dict[str, Dict[str, Any]] = {
    "slack": {
        "scope_key": "allowed_channels",
        "free_response_key": "free_response_channels",
        "modes": {
            "mention": {"require_mention": True, "strict_mention": True,
                        "thread_require_mention": True, "mention_patterns": [],
                        "free_response_channels": [], "require_mention_channels": []},
            "mention-sticky": {"require_mention": True, "strict_mention": False,
                               "thread_require_mention": False, "mention_patterns": [],
                               "free_response_channels": [], "require_mention_channels": []},
            "pattern": {"require_mention": True, "strict_mention": True,
                        "thread_require_mention": True, "mention_patterns": [],
                        "free_response_channels": [], "require_mention_channels": []},
            "always-on": {"require_mention": True, "strict_mention": False,
                          "thread_require_mention": False, "mention_patterns": [],
                          "free_response_channels": [], "require_mention_channels": []},
        },
    },
    "discord": {
        "scope_key": "allowed_channels",
        "free_response_key": "free_response_channels",
        "modes": {
            "mention": {"require_mention": True, "thread_require_mention": True,
                        "free_response_channels": []},
            "mention-sticky": {"require_mention": True, "thread_require_mention": False,
                               "free_response_channels": []},
            "always-on": {"require_mention": True, "thread_require_mention": False,
                          "free_response_channels": []},
        },
    },
    "telegram": {
        "scope_key": "allowed_chats",
        "free_response_key": "free_response_chats",
        "modes": {
            "mention": {"require_mention": True, "mention_patterns": [],
                        "free_response_chats": [], "observe_unmentioned_group_messages": False},
            "pattern": {"require_mention": True, "mention_patterns": [],
                        "free_response_chats": [], "observe_unmentioned_group_messages": False},
            "always-on": {"require_mention": True, "mention_patterns": [],
                          "free_response_chats": [], "observe_unmentioned_group_messages": False},
        },
    },
}

# distribution.yaml distribution_owned: the stock DEFAULT_DIST_OWNED
# (hermes_cli/profile_distribution.py:88-95) plus the two this render adds.
_DIST_OWNED: List[str] = [
    "SOUL.md", "config.yaml", "mcp.json", "skills", "cron",
    "distribution.yaml", "skill-bundles", ".env.template",
]

_ENV_TEMPLATE_BODY = (
    "# Environment variables for this Hermes coworker distribution.\n"
    "# The installer renames this file to .env.EXAMPLE; copy it to .env and\n"
    "# fill in real values before running.\n"
    "\n"
    "# Fleet raw-request-capture default: dump preflight request payloads and\n"
    "# terminal API-error response bodies (secret-redacted) beside each session\n"
    "# file. This documents the default; the active .env the installer writes is\n"
    "# what enables capture for spawned surfaces. See the fleet raw-request-\n"
    "# capture runbook (website/docs/user-guide/fleet-raw-request-capture.md).\n"
    "HERMES_DUMP_REQUESTS=true\n"
)

# Source workflow bodies carry this marker where render-time overlay bodies land.
_OVERLAY_SPLICE_MARKER = "OVERLAY-SPLICE-POINT"


class CompositionError(Exception):
    """Raised when a coworker-types.yaml spec cannot be composed."""


def _safe_name(kind: str, name: Any) -> str:
    """A spec name safe to use as a path component and a profile name."""
    if not isinstance(name, str) or not _SAFE_NAME_RE.match(name):
        raise CompositionError(f"unsafe {kind} name: {name!r}")
    return name


def _safe_join(root: Path, *parts: str) -> Path:
    """Resolve ``parts`` beneath ``root``, rejecting absolute paths and ``..``
    escapes so a spec cannot reach outside its own tree."""
    resolved_root = root.resolve()
    target = resolved_root.joinpath(*parts).resolve()
    if target != resolved_root and resolved_root not in target.parents:
        raise CompositionError(f"path escapes {resolved_root}: {parts!r}")
    return target


def load_spec(spec: str) -> Dict[str, Any]:
    """Parse a coworker-types.yaml spec file into a plain dict."""
    return yaml.safe_load(Path(spec).read_text(encoding="utf-8")) or {}


def _dedup(items: List[Any]) -> List[Any]:
    seen = set()
    out = []
    for it in items:
        key = json.dumps(it, sort_keys=True) if isinstance(it, (dict, list)) else it
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _deep_merge(base: Any, overlay: Any) -> Any:
    """Recursively merge ``overlay`` onto ``base``; leaf (overlay) wins.

    Dicts merge key-wise; every other type (including lists) is replaced by the
    overlay value — the leaf-wins semantics the extends chain needs for
    identity, traits and config scalars/lists.
    """
    if isinstance(base, dict) and isinstance(overlay, dict):
        out = dict(base)
        for k, v in overlay.items():
            out[k] = _deep_merge(out[k], v) if k in out else copy.deepcopy(v)
        return out
    return copy.deepcopy(overlay)


def _set_dotted(mapping: Dict[str, Any], dotted: str, value: Any) -> None:
    node = mapping
    parts = dotted.split(".")
    for key in parts[:-1]:
        nxt = node.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            node[key] = nxt
        node = nxt
    node[parts[-1]] = copy.deepcopy(value)


def _enforce_retention(config: Dict[str, Any]) -> None:
    """Force the fleet-safe retention values onto ``config``, overriding a
    declared value and inserting an omitted one alike — the requirement's
    "explicitly and never by assumption". A setdefault-style fill would leave a
    stale unsafe value untouched, so this always overwrites the leaf."""
    for dotted, value in _RETENTION_INVARIANTS.items():
        _set_dotted(config, dotted, value)


def _engage_alias_nodes(config: Dict[str, Any], platform: str) -> List[Dict[str, Any]]:
    """Every loader-alias location a controlled key can occupy for ``platform``:
    root ``<p>``, ``platforms.<p>``, ``gateway.<p>``, ``gateway.platforms.<p>``.
    The loader bridges values from these locations over ``platforms.<p>.extra`` at
    load time (gateway/config.py:1676-1809 lifts require_mention/mention_patterns/
    free_response_channels), so a stale inherited value left in any of them would
    override the rendered gate — which is why they are all stripped."""
    nodes: List[Dict[str, Any]] = []

    def _add(node: Any) -> None:
        if isinstance(node, dict) and not any(node is seen for seen in nodes):
            nodes.append(node)

    _add(config.get(platform))
    platforms = config.get("platforms")
    if isinstance(platforms, dict):
        _add(platforms.get(platform))
    gateway = config.get("gateway")
    if isinstance(gateway, dict):
        _add(gateway.get(platform))
        gplatforms = gateway.get("platforms")
        if isinstance(gplatforms, dict):
            _add(gplatforms.get(platform))
    return nodes


def _strip_engage_key(config: Dict[str, Any], platform: str, key: str) -> None:
    """Pop ``key`` from every alias location and its nested ``extra`` block."""
    for node in _engage_alias_nodes(config, platform):
        node.pop(key, None)
        extra = node.get("extra")
        if isinstance(extra, dict):
            extra.pop(key, None)


def _engage_extra_target(config: Dict[str, Any], platform: str) -> Dict[str, Any]:
    """The canonical write destination ``config["platforms"][<p>]["extra"]``,
    created as needed. Never replaces an existing block, so unrelated keys (token,
    enabled, a hand-set extra value, an inherited scope) are preserved."""
    platforms = config.setdefault("platforms", {})
    if not isinstance(platforms, dict):
        raise CompositionError(
            f"config 'platforms' must be a mapping to render engage keys, "
            f"got {type(platforms).__name__}")
    block = platforms.setdefault(platform, {})
    if not isinstance(block, dict):
        raise CompositionError(
            f"config 'platforms.{platform}' must be a mapping, got {type(block).__name__}")
    extra = block.setdefault("extra", {})
    if not isinstance(extra, dict):
        raise CompositionError(
            f"config 'platforms.{platform}.extra' must be a mapping, got {type(extra).__name__}")
    return extra


def _is_blank_or_wildcard(value: Any) -> bool:
    """A member meaning "no restriction" to an adapter: a blank/whitespace id or the
    ``*`` wildcard. An empty allowlist is treated as unrestricted by the resolvers,
    so such a member silently widens access — blanket-forward."""
    return str(value).strip() in ("", "*")


def _bounded_ids(values: Any, what: str) -> List[Any]:
    """Validate a bounded, non-wildcard allowlist (always-on channels or
    sender_scope). Missing/empty, a scalar, or any blank/``*`` member all mean "no
    restriction" and are refused rather than rendered."""
    if isinstance(values, str) or not isinstance(values, list) or not values:
        raise CompositionError(
            f"{what} must be a non-empty list of ids; empty, missing or a bare "
            "scalar would blanket-forward (an empty allowlist is unrestricted)")
    if any(_is_blank_or_wildcard(v) for v in values):
        raise CompositionError(
            f"{what} contains a blank or '*' wildcard member (blanket-forward); "
            "list exact ids")
    return list(values)


def _bounded_patterns(values: Any, platform: str) -> List[str]:
    """Validate the non-empty pattern list a ``pattern`` mode requires. A blank
    pattern compiles to a match-everything regex, so every element must be a
    non-empty string."""
    if isinstance(values, str) or not isinstance(values, list) or not values:
        raise CompositionError(
            f"engage mode 'pattern' for platform {platform!r} requires a non-empty "
            "'patterns' list")
    if any(not isinstance(p, str) or not p.strip() for p in values):
        raise CompositionError(
            f"engage mode 'pattern' for platform {platform!r} requires non-empty "
            "string patterns (a blank pattern matches everything)")
    return list(values)


def _render_engage(config: Dict[str, Any]) -> None:
    """Translate the plugin's ``config.engage`` block into the concrete per-platform
    gate keys the adapters read under ``platforms.<p>.extra``, then remove the inert
    ``engage`` pseudo-key. Deterministic (canonical reset + alias strip), fail-closed
    for unsupported platform/mode combinations, and never emits an unrestricting
    (blanket-forward) gate. Registers no routing hook — engagement stays per-profile
    adapter config. See website/docs/user-guide/fleet-engage-modes.md."""
    engage = config.pop("engage", None)
    if engage is None:
        return
    if not isinstance(engage, dict):
        raise CompositionError(
            f"engage must be a mapping of platform -> spec, got {type(engage).__name__}")
    for platform, block in engage.items():
        _render_engage_platform(config, str(platform), block)


def _render_engage_platform(config: Dict[str, Any], platform: str, block: Any) -> None:
    caps = _ENGAGE_CAPS.get(platform)
    if caps is None:
        raise CompositionError(
            f"engage-mode rendering not supported for platform {platform!r}; "
            "supported: slack, discord, telegram")
    if not isinstance(block, dict):
        raise CompositionError(
            f"engage spec for platform {platform!r} must be a mapping, "
            f"got {type(block).__name__}")

    mode = block.get("mode")
    modes = caps["modes"]
    # isinstance guard first: a non-string mode (e.g. a YAML list) is unhashable
    # and ``in`` would raise TypeError instead of failing closed.
    if not isinstance(mode, str) or mode not in _ALL_ENGAGE_MODES:
        raise CompositionError(
            f"unknown engage mode {mode!r} for platform {platform!r}; "
            f"supported modes: {', '.join(_ALL_ENGAGE_MODES)}")
    if mode not in modes:
        raise CompositionError(
            f"engage mode {mode!r} not supported for platform {platform!r}; "
            f"supported for {platform}: {', '.join(modes)}")

    canonical = copy.deepcopy(modes[mode])
    if mode == "pattern":
        canonical["mention_patterns"] = _bounded_patterns(block.get("patterns"), platform)
    elif mode == "always-on":
        canonical[caps["free_response_key"]] = _bounded_ids(
            block.get("channels"),
            f"engage always-on channels for platform {platform!r}")

    # Strip every controlled key (the union of the platform's mode-dict keys) from
    # ALL aliases, then write the canonical set to platforms.<p>.extra so nothing
    # bridges over it at load time. update() preserves unrelated keys in that extra.
    controlled = {key for md in modes.values() for key in md}
    for key in controlled:
        _strip_engage_key(config, platform, key)
    _engage_extra_target(config, platform).update(canonical)

    # Scope is handled apart from the mode keys: written only when the coworker
    # declares sender_scope (else an inherited channel/chat restriction is PRESERVED,
    # never reset — resetting it would widen access).
    if "sender_scope" in block:
        scope = _bounded_ids(
            block.get("sender_scope"),
            f"engage sender_scope for platform {platform!r}")
        scope_key = caps["scope_key"]
        _strip_engage_key(config, platform, scope_key)
        _engage_extra_target(config, platform)[scope_key] = scope


def _resolve_type(tname: str, tinfo: Dict[str, Any], spines: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve one coworker type's extends chain into concrete artifacts."""
    identity: Any = None
    invariants: List[Any] = []
    context: List[Any] = []
    skills: List[Any] = []
    workflows: List[Any] = []
    overlays: List[Any] = []
    traits: Dict[str, Any] = {}
    config: Dict[str, Any] = {}

    def _apply(layer: Dict[str, Any]) -> None:
        nonlocal identity, traits, config
        if layer.get("identity") is not None:
            identity = layer["identity"]
        invariants.extend(layer.get("invariants") or [])
        context.extend(layer.get("context") or [])
        skills.extend(layer.get("skills") or [])
        workflows.extend(layer.get("workflows") or [])
        overlays.extend(layer.get("overlays") or [])
        traits = _deep_merge(traits, layer.get("traits") or {})
        config = _deep_merge(config, layer.get("config") or {})

    for pname in (tinfo.get("extends") or []):
        spine = spines.get(pname)
        if spine is None:
            raise CompositionError(f"{tname}: extends unknown spine {pname!r}")
        _apply(spine)
    _apply(tinfo)

    for tk, tv in traits.items():
        dotted = _TRAIT_DOMAIN.get(tk)
        if dotted is None:
            raise CompositionError(f"{tname}: unsupported trait {tk!r}")
        _set_dotted(config, dotted, tv)

    return {
        "identity": identity,
        "invariants": _dedup(invariants),
        "context": _dedup(context),
        "skills": _dedup(skills),
        "workflows": _dedup(workflows),
        "overlays": _dedup(overlays),
        "config": config,
        "ui_meta": tinfo.get("ui_meta") or {},
    }


def _write_yaml(path: Path, data: Any) -> None:
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )


def _write_soul(path: Path, name: str, identity: Any, context: List[Any], invariants: List[Any]) -> None:
    lines = [f"# {name}", "", str(identity or ""), ""]
    if context:
        lines.append("## Context")
        lines.extend(f"- {c}" for c in context)
        lines.append("")
    if invariants:
        lines.append("## Invariants")
        lines.extend(f"- {i}" for i in invariants)
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _write_distribution(pdir: Path, name: str, description: str) -> None:
    _write_yaml(pdir / "distribution.yaml", {
        "name": name,
        "version": "0.1.0",
        "description": description,
        "distribution_owned": list(_DIST_OWNED),
    })


def _write_common(pdir: Path) -> None:
    (pdir / "mcp.json").write_text(json.dumps({"mcpServers": {}}, indent=2) + "\n", encoding="utf-8")
    (pdir / ".env.template").write_text(_ENV_TEMPLATE_BODY, encoding="utf-8")
    (pdir / "cron").mkdir(exist_ok=True)


def _load_overlays(names: List[Any], overlays_root: Path) -> List[Dict[str, Any]]:
    out = []
    for name in names:
        src = _safe_join(overlays_root, f"{_safe_name('overlay', name)}.yaml")
        if src.is_file():
            out.append(yaml.safe_load(src.read_text(encoding="utf-8")) or {})
    return out


def _splice_overlays(body: str, workflow: str, tname: str, overlays: List[Dict[str, Any]]) -> str:
    applicable = [
        ov for ov in overlays
        if ov.get("target-workflow") == workflow and tname in (ov.get("applies-to") or [])
    ]
    splice = "\n".join(str(ov.get("body", "")) for ov in applicable if ov.get("body"))
    trailing_nl = body.endswith("\n")
    if _OVERLAY_SPLICE_MARKER in body:
        kept = []
        for line in body.splitlines():
            if _OVERLAY_SPLICE_MARKER in line:
                if splice:
                    kept.append(splice)
            else:
                kept.append(line)
        result = "\n".join(kept)
    else:
        result = body + (("\n" + splice) if splice else "")
    if trailing_nl and not result.endswith("\n"):
        result += "\n"
    return result


def _render_coworker(pdir: Path, tname: str, resolved: Dict[str, Any],
                     skills_root: Path, workflows_root: Path, overlays_root: Path) -> None:
    pdir.mkdir(parents=True, exist_ok=True)
    _write_soul(pdir / "SOUL.md", tname, resolved["identity"],
                resolved["context"], resolved["invariants"])
    _write_yaml(pdir / "config.yaml", resolved["config"])
    _write_common(pdir)

    skills_dir = pdir / "skills"
    skills_dir.mkdir(exist_ok=True)
    for skill in resolved["skills"]:
        skill = _safe_name("skill", skill)
        src = _safe_join(skills_root, skill, "SKILL.md")
        if not src.is_file():
            raise CompositionError(f"{tname}: skill {skill!r} has no SKILL.md at {src}")
        dest = skills_dir / skill
        dest.mkdir(exist_ok=True)
        (dest / "SKILL.md").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    overlays = _load_overlays(resolved["overlays"], overlays_root)
    for workflow in resolved["workflows"]:
        workflow = _safe_name("workflow", workflow)
        src = _safe_join(workflows_root, workflow, "SKILL.md")
        if not src.is_file():
            raise CompositionError(f"{tname}: workflow {workflow!r} has no SKILL.md at {src}")
        body = _splice_overlays(src.read_text(encoding="utf-8"), workflow, tname, overlays)
        dest = skills_dir / workflow
        dest.mkdir(exist_ok=True)
        (dest / "SKILL.md").write_text(body, encoding="utf-8")

    sb_dir = pdir / "skill-bundles"
    sb_dir.mkdir(exist_ok=True)
    primary = str(resolved["workflows"][0]) if resolved["workflows"] else ""
    _write_yaml(sb_dir / f"{tname}.yaml", {
        "skills": list(resolved["workflows"]),
        "instruction": f"Run the {primary} workflow anchored for the {tname} coworker.",
    })

    _write_distribution(pdir, tname, f"Composed Hermes coworker profile: {tname}")


def _render_default(pdir: Path, name: str, config: Dict[str, Any]) -> None:
    pdir.mkdir(parents=True, exist_ok=True)
    _write_soul(pdir / "SOUL.md", name, "Multiplexer profile for the Bot-Mode gateway.", [], [])
    _write_yaml(pdir / "config.yaml", config)
    _write_common(pdir)
    (pdir / "skills").mkdir(exist_ok=True)
    (pdir / "skill-bundles").mkdir(exist_ok=True)
    _write_distribution(pdir, name, "Composed Hermes multiplexer (DEFAULT) profile")


def _inject_self_plugin(config: Dict[str, Any], orchestrator_profile: str) -> None:
    """Enable this plugin on a rendered coworker config and record which profile
    is the orchestrator, so its onboard tools load fleet-wide and check_fn can
    gate them. Append-if-absent preserves the fixture's list order."""
    plugins = config.setdefault("plugins", {})
    enabled = plugins.setdefault("enabled", [])
    if _SELF_PLUGIN not in enabled:
        enabled.append(_SELF_PLUGIN)
    settings = plugins.setdefault("entries", {}).setdefault(_SELF_PLUGIN, {}).setdefault("settings", {})
    settings["orchestrator_profile"] = orchestrator_profile


def _merged_spine_config(spines: Dict[str, Any]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for spine in spines.values():
        merged = _deep_merge(merged, spine.get("config") or {})
    return merged


def compose(spec: str, out: str) -> Dict[str, str]:
    """Render each coworker type + the DEFAULT profile into ``out``.

    Returns ``{profile_name: rendered_dir}`` for every profile (the DEFAULT
    multiplexer plus one per coworker type in the spec).
    """
    spec_path = Path(spec)
    spec_dir = spec_path.parent
    data = load_spec(spec)

    out_root = Path(out)
    out_root.mkdir(parents=True, exist_ok=True)

    skills_root = _safe_join(spec_dir, str(data.get("skills_root", "skills")))
    workflows_root = _safe_join(spec_dir, str(data.get("workflows_root", "workflows")))
    overlays_root = _safe_join(spec_dir, str(data.get("overlays_root", "overlays")))

    spines: Dict[str, Any] = {}
    for sname, sinfo in (data.get("spines") or {}).items():
        source = (sinfo or {}).get("source")
        if not source:
            raise CompositionError(f"spine {sname!r} has no source")
        src = _safe_join(spec_dir, str(source))
        spines[sname] = yaml.safe_load(src.read_text(encoding="utf-8")) or {}

    orchestrator_profile = _safe_name("profile", data.get("orchestrator_profile", "orchestrator"))
    rendered: Dict[str, str] = {}
    types = data.get("types") or {}
    for tname, tinfo in types.items():
        tname = _safe_name("type", tname)
        resolved = _resolve_type(tname, tinfo or {}, spines)
        _inject_self_plugin(resolved["config"], orchestrator_profile)
        _enforce_retention(resolved["config"])
        _render_engage(resolved["config"])
        pdir = out_root / tname
        _render_coworker(pdir, tname, resolved, skills_root, workflows_root, overlays_root)
        rendered[tname] = str(pdir)

    default_profile = _safe_name("profile", data.get("default_profile", "default"))
    default_config = _deep_merge(_merged_spine_config(spines), data.get("default_config") or {})
    _enforce_retention(default_config)
    _render_engage(default_config)
    ddir = out_root / default_profile
    _render_default(ddir, default_profile, default_config)
    rendered[default_profile] = str(ddir)

    return rendered
