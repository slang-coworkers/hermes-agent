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
from typing import Any, Dict, Iterator, List, Set, Tuple

import yaml

# Pure, side-effect-free core symbols the RT-F01 enforcement consumes. Neither
# reads disk nor mutates os.environ, so the renderer stays a pure offline file
# writer: platform_binds_port is a frozenset+conditional-mode check and Platform
# is an Enum; normalize/validate_profile_name are string predicates. The render
# never calls load_gateway_config / GatewayConfig.from_dict (those read disk and
# mutate the environment) — it writes config the native readers later consume.
from gateway.config import Platform, platform_binds_port
from hermes_cli.profiles import normalize_profile_name, validate_profile_name

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


# --- RT-F01: Hermes-native entity model + human-privilege enforcement ---------
#
# The fleet's entity model IS Hermes's own: coworkers are profiles under ONE
# gateway in multiplex mode; human privilege is the per-platform allowlist the
# native ``_is_user_authorized`` config fallback reads. These render steps make
# that model explicit and refuse the spec spellings that would break it. Each
# step drives a real native reader (``profiles_to_serve`` via the allowlist,
# ``platform_binds_port``, ``_is_user_authorized``) rather than re-implementing
# one, so the render tracks core with zero drift.


def _require_canonical_profile_names(
    types: Dict[str, Any], default_profile: str, orchestrator_profile: str
) -> None:
    """Refuse a spec whose profile names would desynchronize the multiplex joins.

    A coworker type name is used byte-identically as its profile directory, its
    ``multiplex_profile_allowlist`` entry, and any webhook ``profile:`` binding,
    so all three must agree with what ``profiles_to_serve`` will serve: the root
    is always served as the literal ``"default"`` and named profiles are stored
    canonical (lowercase, validated). A ``default_profile`` other than
    ``"default"``, a coworker type named ``default`` (whose dir would collide
    with the multiplexer root), an ``orchestrator_profile`` absent from the
    roster, or a non-canonical / reserved type name each breaks a join.
    """
    if default_profile != "default":
        raise CompositionError(
            f"default_profile must be 'default' (the multiplexer root), got {default_profile!r}"
        )
    if "default" in types:
        raise CompositionError(
            "a coworker type may not be named 'default' — it collides with the multiplexer root"
        )
    if orchestrator_profile not in types:
        raise CompositionError(
            f"orchestrator_profile {orchestrator_profile!r} is not one of the declared coworker types"
        )
    for tname in types:
        if not isinstance(tname, str):
            raise CompositionError(f"coworker type name is not a string: {tname!r}")
        try:
            canonical = normalize_profile_name(tname)
            validate_profile_name(tname)
        except ValueError as exc:
            raise CompositionError(
                f"coworker type {tname!r} is not a valid profile name: {exc}"
            ) from exc
        if canonical != tname:
            raise CompositionError(
                f"coworker type {tname!r} is not canonical (expected {canonical!r})"
            )


def _enforce_multiplex(default_config: Dict[str, Any], roster: List[str]) -> None:
    """Force the DEFAULT profile into multiplexer mode serving exactly ``roster``.

    Written explicitly and never by assumption: a spec that omits the keys — or
    sets a hostile root ``multiplex_profiles: false`` — must still render a
    multiplexing default. Core reads the ROOT spelling with precedence over the
    nested ``gateway.*`` one, so a surviving root value would win; POP both
    spellings, then write only the canonical nested keys the effective parsed
    config resolves to. (The ``GATEWAY_MULTIPLEX_PROFILES`` env override is the
    operator's deployment box, outside a render's reach.)
    """
    nested = default_config.get("gateway")
    for key in ("multiplex_profiles", "multiplex_profile_allowlist"):
        default_config.pop(key, None)
        if isinstance(nested, dict):
            nested.pop(key, None)
    _set_dotted(default_config, "gateway.multiplex_profiles", True)
    _set_dotted(default_config, "gateway.multiplex_profile_allowlist", roster)


def _iter_noncanonical_platform_keys(config: Dict[str, Any]) -> Iterator[Tuple[str, str]]:
    """Yield ``(location, platform)`` for every platform-named key spelled outside
    the canonical ``platforms.<x>`` map: a top-level key, a ``gateway.<x>`` key,
    or a ``gateway.platforms.<x>`` key.

    Platform membership is tested with the ``Platform(key)`` constructor (which
    also resolves dynamically-registered plugin platforms), never by iterating
    the enum. It deliberately does NOT consult ``platform_binds_port``: that
    symbol is monkeypatched in the port-binding acceptance test, and coupling the
    layout check to it would let the patch mask the distinct port-binding check.
    """
    def _is_platform(key: Any) -> bool:
        if not isinstance(key, str):
            return False
        try:
            Platform(key)
            return True
        except ValueError:
            return False

    for key in config:
        if _is_platform(key):
            yield "top-level", key
    gateway = config.get("gateway")
    if isinstance(gateway, dict):
        for key in gateway:
            if _is_platform(key):
                yield "gateway", key
        gw_platforms = gateway.get("platforms")
        if isinstance(gw_platforms, dict):
            for key in gw_platforms:
                if _is_platform(key):
                    yield "gateway.platforms", key


def _require_canonical_platform_layout(config: Dict[str, Any]) -> None:
    """Refuse any platform block spelled outside the canonical ``platforms.<x>``.

    Core honors top-level ``<platform>``, ``gateway.<platform>`` and
    ``gateway.platforms.<platform>`` too, so a port-binding platform or a webhook
    route could otherwise hide from the coworker / route checks under an alias.
    Forbidding the aliases leaves exactly one place either can live, making those
    checks alias-proof.
    """
    for location, platform in _iter_noncanonical_platform_keys(config):
        raise CompositionError(
            f"platform {platform!r} must be declared under 'platforms.{platform}', "
            f"not as {location}.{platform}"
        )


def _validate_webhook_routes(default_config: Dict[str, Any], served: Set[str]) -> None:
    """Every webhook route on the DEFAULT profile carries its OWN non-empty secret
    and an EXPLICIT ``profile:`` binding to a served profile.

    Stricter than native (which permits global-secret inheritance and treats an
    omitted ``profile`` as ``default``) so a fleet route can never fall back to a
    shared secret or an implicit binding.
    """
    platforms = default_config.get("platforms")
    if not isinstance(platforms, dict):
        return
    webhook = platforms.get("webhook")
    if not isinstance(webhook, dict):
        return
    routes = (webhook.get("extra") or {}).get("routes")
    if not routes:
        return
    if not isinstance(routes, dict):
        raise CompositionError(
            "platforms.webhook.extra.routes must be a mapping of route name -> route"
        )
    for name, route in routes.items():
        secret = route.get("secret") if isinstance(route, dict) else None
        if not isinstance(secret, str) or not secret:
            raise CompositionError(
                f"webhook route {name!r}: 'secret' must be a non-empty string"
            )
        profile = route.get("profile") if isinstance(route, dict) else None
        if not isinstance(profile, str) or profile not in served:
            raise CompositionError(
                f"webhook route {name!r}: 'profile' must explicitly bind a served profile "
                f"(one of {sorted(served)})"
            )


def _forbid_coworker_port_binding(config: Dict[str, Any], tname: str) -> None:
    """No coworker profile may enable a port-binding platform.

    In a multiplexer the DEFAULT profile owns the single shared listener and
    serves every profile through ``/p/<profile>/``; a secondary profile that
    binds a host port is always a misconfiguration. Uses core's own
    ``platform_binds_port`` predicate — no vendored port set — so the policy
    tracks core exactly.
    """
    platforms = config.get("platforms")
    if not isinstance(platforms, dict):
        return
    for pname, block in platforms.items():
        if not isinstance(block, dict) or not block.get("enabled"):
            continue
        if platform_binds_port(pname, block.get("extra")):
            raise CompositionError(
                f"coworker {tname!r} may not enable port-binding platform {pname!r} — "
                f"only the DEFAULT multiplexer profile owns the shared listener"
            )


def _forbid_coworker_webhook_routes(config: Dict[str, Any], tname: str) -> None:
    """Webhook routes belong only on the DEFAULT profile — refuse any on a
    coworker even when the block is disabled.

    ``_forbid_coworker_port_binding`` only fires on an *enabled* platform, so a
    ``webhook`` block with ``enabled: false`` that still carries routes would
    otherwise leak scope. A disabled-but-present route is still a route on the
    wrong profile.
    """
    platforms = config.get("platforms")
    if not isinstance(platforms, dict):
        return
    webhook = platforms.get("webhook")
    if not isinstance(webhook, dict):
        return
    if (webhook.get("extra") or {}).get("routes"):
        raise CompositionError(
            f"coworker {tname!r} may not carry webhook routes — "
            f"routes live only on the DEFAULT profile"
        )


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
    default_profile = _safe_name("profile", data.get("default_profile", "default"))
    types = data.get("types") or {}

    # RT-F01: gate profile names up front so the roster, the allowlist entries,
    # the webhook profile: bindings and the served profile dirs are one join.
    _require_canonical_profile_names(types, default_profile, orchestrator_profile)
    roster = sorted(types)
    served: Set[str] = {"default", *roster}

    rendered: Dict[str, str] = {}
    for tname, tinfo in types.items():
        tname = _safe_name("type", tname)
        resolved = _resolve_type(tname, tinfo or {}, spines)
        _inject_self_plugin(resolved["config"], orchestrator_profile)
        _enforce_retention(resolved["config"])
        # RT-F01: a coworker is a secondary multiplex profile — it may not spell a
        # platform outside platforms.<x>, bind a host port, or carry a route.
        _require_canonical_platform_layout(resolved["config"])
        _forbid_coworker_port_binding(resolved["config"], tname)
        _forbid_coworker_webhook_routes(resolved["config"], tname)
        pdir = out_root / tname
        _render_coworker(pdir, tname, resolved, skills_root, workflows_root, overlays_root)
        rendered[tname] = str(pdir)

    default_config = _deep_merge(_merged_spine_config(spines), data.get("default_config") or {})
    _enforce_retention(default_config)
    # RT-F01: the DEFAULT profile is the fleet's multiplexer and the sole home of
    # webhook routes; enforce multiplex, then validate routes (layout first, so
    # an alias-spelled route cannot evade route validation).
    _enforce_multiplex(default_config, roster)
    _require_canonical_platform_layout(default_config)
    _validate_webhook_routes(default_config, served)
    ddir = out_root / default_profile
    _render_default(ddir, default_profile, default_config)
    rendered[default_profile] = str(ddir)

    return rendered
