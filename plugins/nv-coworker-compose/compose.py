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
        # keys that override the scope allowlist (not mode keys): neutralized only
        # when sender_scope is declared, so a declared scope stays authoritative.
        # reaction_trigger_target rewrites a reaction turn's channel and can route a
        # reaction from outside the scope into an allowlisted target (adapter.py:5703).
        "scope_bypass": {"reaction_trigger_target": ""},
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
        "scope_bypass": {},  # no config-key allowlist bypass exists (voice-linked free-response is runtime state)
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
        # guest_mode:true admits an @mention from a chat OUTSIDE allowed_chats
        # (adapter.py:9853-9855), so it bypasses a declared sender_scope.
        "scope_bypass": {"guest_mode": False},
        # free_response_topics is the topic-granularity twin of free_response_chats:
        # both admit an unmentioned message before the require_mention gate
        # (adapter.py:9861 before :9863), so it is a mode key reset in every mode.
        "modes": {
            "mention": {"require_mention": True, "mention_patterns": [],
                        "free_response_chats": [], "free_response_topics": [],
                        "observe_unmentioned_group_messages": False},
            "pattern": {"require_mention": True, "mention_patterns": [],
                        "free_response_chats": [], "free_response_topics": [],
                        "observe_unmentioned_group_messages": False},
            "always-on": {"require_mention": True, "mention_patterns": [],
                          "free_response_chats": [], "free_response_topics": [],
                          "observe_unmentioned_group_messages": False},
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


def _strip_engage_key(config: Dict[str, Any], platform: str, key: str) -> Set[int]:
    """Pop ``key`` from every alias location and its nested ``extra`` block. Return
    the ``id()``s of alias nodes a value was actually removed from, so only a block
    the strip itself emptied is later pruned — a pre-existing empty noncanonical
    block must survive for RT-F01's canonical-layout check to reject it."""
    removed: Set[int] = set()
    for node in _engage_alias_nodes(config, platform):
        hit = False
        if key in node:
            del node[key]
            hit = True
        extra = node.get("extra")
        if isinstance(extra, dict) and key in extra:
            del extra[key]
            hit = True
        if hit:
            removed.add(id(node))
    return removed


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
    pattern matches everything, and a syntactically-invalid one is silently dropped
    by the adapter (degrading ``pattern`` → mention-only), so every element must be
    a non-empty string that compiles as a regex."""
    if isinstance(values, str) or not isinstance(values, list) or not values:
        raise CompositionError(
            f"engage mode 'pattern' for platform {platform!r} requires a non-empty "
            "'patterns' list")
    if any(not isinstance(p, str) or not p.strip() for p in values):
        raise CompositionError(
            f"engage mode 'pattern' for platform {platform!r} requires non-empty "
            "string patterns (a blank pattern matches everything)")
    for p in values:
        try:
            re.compile(p)
        except re.error as exc:
            raise CompositionError(
                f"engage mode 'pattern' for platform {platform!r}: invalid regex {p!r} "
                f"({exc}) — the adapter would drop it, degrading pattern to mention-only"
            ) from exc
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
    _detach_shared_engage_blocks(config, [str(p) for p in engage])
    for platform, block in engage.items():
        _render_engage_platform(config, str(platform), block)


def _detach_shared_engage_blocks(config: Dict[str, Any], platform_names: List[str]) -> None:
    """Give every engage platform block its own dict identity at each loader-alias
    location, so an in-place render of one platform cannot pollute another that
    shares the same object via a YAML anchor (``platforms: {slack: &a {...},
    telegram: *a}`` survives ``_deep_merge``'s subtree-insert path). A deep copy of
    an already-unique block is value-equal, so a spec without cross-referenced blocks
    renders identically; copying each block also detaches a shared nested ``extra``."""
    parents: List[Dict[str, Any]] = [config]
    platforms = config.get("platforms")
    if isinstance(platforms, dict):
        parents.append(platforms)
    gateway = config.get("gateway")
    if isinstance(gateway, dict):
        parents.append(gateway)
        gplatforms = gateway.get("platforms")
        if isinstance(gplatforms, dict):
            parents.append(gplatforms)
    for parent in parents:
        for platform in platform_names:
            block = parent.get(platform)
            if isinstance(block, dict):
                parent[platform] = copy.deepcopy(block)


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
    stripped: Set[int] = set()
    for key in controlled:
        stripped |= _strip_engage_key(config, platform, key)
    target = _engage_extra_target(config, platform)
    target.update(canonical)

    # Scope + scope-bypass keys are handled apart from the mode keys. The scope key
    # (allowed_channels/allowed_chats) and each per-platform bypass key (a key that
    # overrides the scope allowlist — Telegram guest_mode, Slack reaction_trigger_target)
    # are written to their neutral value ONLY when the coworker declares sender_scope,
    # making the declared scope authoritative. When sender_scope is omitted the scope
    # is PRESERVED (never reset, which would widen access).
    bypasses = caps.get("scope_bypass", {})
    if "sender_scope" in block:
        scope = _bounded_ids(
            block.get("sender_scope"),
            f"engage sender_scope for platform {platform!r}")
        scope_key = caps["scope_key"]
        stripped |= _strip_engage_key(config, platform, scope_key)
        target[scope_key] = scope
        for bkey, breset in bypasses.items():
            stripped |= _strip_engage_key(config, platform, bkey)
            target[bkey] = copy.deepcopy(breset)
    else:
        # sender_scope omitted: there is no declared scope to protect, so PRESERVE
        # the operator's inherited bypass value rather than resetting it — but still
        # canonicalize it (a value left at a noncanonical alias would trip RT-F01's
        # _require_canonical_platform_layout). Prefer the canonical .extra value;
        # otherwise take a consistent inherited value from the aliases (rejecting a
        # conflicting one), strip every alias, and write it to platforms.<p>.extra.
        for bkey in bypasses:
            values: List[Any] = []
            for node in _engage_alias_nodes(config, platform):
                if bkey in node:
                    values.append(node[bkey])
                extra = node.get("extra")
                if isinstance(extra, dict) and bkey in extra:
                    values.append(extra[bkey])
            if not values:
                continue
            if bkey in target:
                preserved = copy.deepcopy(target[bkey])
            elif any(value != values[0] for value in values[1:]):
                raise CompositionError(
                    f"conflicting inherited {bkey!r} values for platform {platform!r}")
            else:
                preserved = copy.deepcopy(values[0])
            stripped |= _strip_engage_key(config, platform, bkey)
            target[bkey] = preserved

    _prune_vacuous_noncanonical(config, platform, stripped)


def _is_vacuous_platform_block(block: Any) -> bool:
    """A platform block the engage strip emptied: no keys, or only an empty
    ``extra`` mapping."""
    if not isinstance(block, dict):
        return False
    if not block:
        return True
    return set(block) == {"extra"} and isinstance(block.get("extra"), dict) and not block["extra"]


def _prune_vacuous_noncanonical(config: Dict[str, Any], platform: str, stripped_ids: Set[int]) -> None:
    """Remove a NONCANONICAL platform alias (root ``<p>``, ``gateway.<p>``,
    ``gateway.platforms.<p>``) ONLY when the controlled-key strip is what emptied it
    (its ``id()`` is in ``stripped_ids``), so it does not trip RT-F01's
    ``_require_canonical_platform_layout``. A pre-existing empty block, a block still
    carrying unrelated config, and the canonical ``platforms.<p>`` are all left for
    RT-F01 to reject — pruning any of them would weaken that check."""
    def _prune(parent: Any) -> None:
        if not isinstance(parent, dict):
            return
        block = parent.get(platform)
        if (isinstance(block, dict) and id(block) in stripped_ids
                and _is_vacuous_platform_block(block)):
            parent.pop(platform, None)

    _prune(config)
    gateway = config.get("gateway")
    if isinstance(gateway, dict):
        _prune(gateway)
        gplatforms = gateway.get("platforms")
        if isinstance(gplatforms, dict):
            _prune(gplatforms)


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
    also resolves dynamically-registered plugin platforms), never by iterating the
    enum. Layout detection is kept independent of ``platform_binds_port``:
    recognizing WHERE a platform is declared is a separate concern from deciding
    WHETHER it binds a port, and the two checks must not share a predicate.
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
    """Refuse a platform block spelled non-canonically or carrying a non-mapping
    ``extra``.

    A platform can hide from the port-binding / route checks two ways: an alias
    location (top-level ``<platform>``, ``gateway.<platform>``,
    ``gateway.platforms.<platform>``), and a non-canonical spelling of the key
    under ``platforms`` — core normalizes ``"Webhook"`` and ``" webhook "`` to
    ``Platform.WEBHOOK`` and enables the listener, but the checks below match the
    canonical value string. Both are refused, so exactly one canonical place
    remains where a port binder or a route can live. A present ``extra`` that is
    not a mapping is refused too: left in place it would be written to
    ``config.yaml`` and break the runtime platform merge, discarding the
    multiplex/allowlist enforcement the loader would otherwise apply.
    """
    platforms = config.get("platforms")
    if isinstance(platforms, dict):
        for key, block in platforms.items():
            if isinstance(key, str):
                try:
                    canonical = Platform(key).value
                except ValueError:
                    canonical = None
                if canonical is not None and key != canonical:
                    raise CompositionError(
                        f"platform {key!r} under 'platforms' must use its canonical name {canonical!r}"
                    )
            if isinstance(block, dict) and "extra" in block and not isinstance(block["extra"], dict):
                raise CompositionError(f"platforms.{key}: 'extra' must be a mapping")
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
    extra = webhook.get("extra")
    routes = extra.get("routes") if isinstance(extra, dict) else None
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
        extra = block.get("extra")
        if platform_binds_port(pname, extra if isinstance(extra, dict) else None):
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
    extra = webhook.get("extra")
    if isinstance(extra, dict) and extra.get("routes"):
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

    _require_canonical_profile_names(types, default_profile, orchestrator_profile)
    roster = list(types)
    served: Set[str] = {"default", *roster}

    rendered: Dict[str, str] = {}
    for tname, tinfo in types.items():
        tname = _safe_name("type", tname)
        resolved = _resolve_type(tname, tinfo or {}, spines)
        _inject_self_plugin(resolved["config"], orchestrator_profile)
        _enforce_retention(resolved["config"])
        _render_engage(resolved["config"])
        _require_canonical_platform_layout(resolved["config"])
        _forbid_coworker_port_binding(resolved["config"], tname)
        _forbid_coworker_webhook_routes(resolved["config"], tname)
        pdir = out_root / tname
        _render_coworker(pdir, tname, resolved, skills_root, workflows_root, overlays_root)
        rendered[tname] = str(pdir)

    default_config = _deep_merge(_merged_spine_config(spines), data.get("default_config") or {})
    _enforce_retention(default_config)
    _render_engage(default_config)
    _enforce_multiplex(default_config, roster)
    _require_canonical_platform_layout(default_config)
    _validate_webhook_routes(default_config, served)
    ddir = out_root / default_profile
    _render_default(ddir, default_profile, default_config)
    rendered[default_profile] = str(ddir)

    return rendered
