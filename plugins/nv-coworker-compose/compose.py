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

# Fleet-uniform approval policy (GOV-F23). These eight keys are pinned ONCE,
# machine-wide, in the managed-scope fragment (out_root/managed/config.yaml) that
# native _load_config_impl deep-merges (managed-wins) onto every profile
# (hermes_cli/managed_scope.py:137-176), and are STRIPPED from every per-profile
# config so the immutability split is disjoint — fleet policy is root-owned (a
# /approvals mode change refuses under managed policy, hermes_cli/approval_mode.py:63)
# while the per-role command_allowlist and the render-enforced approvals.deny floor
# stay in the profile config. Every value equals the stock default (the six
# approvals.* at hermes_cli/config_defaults.py:2558-2576, the two security.approval.*
# at :2671-2674), so if the fragment is not yet installed the fleet is no more
# permissive: stock Hermes already denies RECOGNIZED unattended dangerous commands
# (deny-if-detected) and fails the transport closed — fragment-absence safety only,
# NOT a claim every dangerous spelling is blocked (AC-7 is unchanged either way).
# Pinned EXPLICITLY (the MEM-F44 "never by assumption" standard) so a re-pin or an
# upstream default flip cannot silently loosen them. Every key has a real reader
# (readers cited in the GOV-F23 runbook).
_GOV_APPROVALS_MANAGED: Dict[str, Any] = {
    "approvals.mode": "smart",
    "approvals.timeout": 300,
    "approvals.cron_mode": "deny",
    "approvals.single_query_mode": "deny",
    "approvals.unattended_mode": "deny",
    "approvals.denial_breaker_threshold": 3,
    # Presentation-path fail-closed hardening. NOT part of the unattended fail-safe:
    # the transport is read only on the human-approval / gateway path
    # (approval.py:4303-4316, from :5138/:5631, both AFTER the non-interactive
    # block), never on the unattended deny path. Pinning them denies flipping
    # transport_fallback to "builtin" (which would re-route a failed plugin
    # transport to a built-in surface instead of denying), so the human-approval
    # path stays fail-closed.
    "security.approval.transport": "builtin",
    "security.approval.transport_fallback": "deny",
}

# Reserved sibling directory under out_root for the managed fragment. A coworker
# type named 'managed' is rejected so its profile dir cannot collide with it.
_MANAGED_FRAGMENT_DIR = "managed"

# Force-push / tag / release deny floor (GOV-F23). These verbs must never run
# unattended, so the render ENFORCES this floor by OVERWRITING approvals.deny on
# EVERY rendered profile (per-role, DEFAULT and orchestrator) with exactly this
# fixed 12-glob list — whatever the coworker type or spine declared. Enforcing
# (not passing through) makes the floor a render-GUARANTEED fleet-safety property:
# a spec that declares a weak or absent deny still ships the full floor.
# approvals.deny is the one guard layer that fires PRE-detection and even under
# --yolo (tools/approval.py:4772, BEFORE the yolo / mode=off bypass at :4781), so a
# profile with no floor has no force-push backstop at all.
#
# approvals.deny rules are matched with fnmatch.fnmatchcase against the WHOLE
# normalized command (tools/approval.py:818-822), anchored whole-string with
# leading/trailing '*', and are ORDER-TOLERANT: they match --force / -f / +refspec
# in ANY argument order. This is a best-effort in-surface backstop for the common,
# combined-flag and global-option force-push/tag/release spellings — NOT an
# exhaustive detector. It complements the core dangerous-command detector
# (tools/approval.py:1227-1228), which covers only --forc*/-f and MISSES +refspec
# and combined -f clusters and has NO tag/release rule at all; a shell-wrapped or
# chained spelling still slips this anchored block-list — the fail-open class is
# recorded honestly by AC-7 and owned by GOV-ENF, NOT closed here.
#
# fnmatch has NO word boundary, so each force short-flag cluster uses the [!- ]
# (not-dash, not-space) class to keep the force 'f' inside a single-dash short
# cluster so '*' cannot cross a space into a branch name: a naive 'git *push* -*f*'
# would over-block a normal 'git push -u origin fix' (the '*' crosses ' -u' into the
# 'f' of 'fix'). One [!- ] per flag letter before 'f' covers 'f'
# at cluster positions 1-3 (-f, -uf, -uqf). The 'git push* *+*' / 'git -*push* *+*'
# pair catches the unquoted and shell-quoted +refspec, contiguous and behind a git
# global option.
_GOV_APPROVALS_DENY_FLOOR: List[str] = [
    "git *push* --forc*",       # --force / --force-with-lease, any argument order
    "git *push* -f",            # standalone -f at command end
    "git *push* -f *",          # standalone -f mid-command
    "git *push* -f[!- ]*",      # force cluster, f-first (-fu, -fq, -fuq…)
    "git *push* -[!- ]f*",      # force cluster, f-second (-uf, -qf, -ufq…)
    "git *push* -[!- ][!- ]f*", # force cluster, f-third (-uqf)
    "git push* *+*",            # +refspec force-push, unquoted or shell-quoted
    "git -*push* *+*",          # +refspec force-push behind a git global option
    "git tag *",                # tag create / delete / move
    "git -* tag *",             # tag verb behind a git global option
    "gh release *",             # gh release create / delete / edit
    "gh -* release *",          # gh release verb behind a gh global option
]

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

# Fleet session-mode policy (RT-F03). A session mode is a FLEET-WIDE property:
# under gateway.multiplex_profiles the gateway builds ONE SessionStore from the
# single DEFAULT-profile config (gateway/run.py:7474) and hands that same store
# to every profile's adapter (gateway/run.py:16823), so the isolation flags are
# gateway-global, never per-profile. The two flags below are the store-lane keys
# SessionStore reads (gateway/config.py:1220-1221 -> gateway/session.py:2010-2017).
_SESSION_FLAG_KEYS: Tuple[str, str] = ("group_sessions_per_user", "thread_sessions_per_user")

# Accepted session modes -> the (group, thread) sessions-per-user flags each
# renders. per-thread is the native default (group split per user, thread shared).
_SESSION_MODE_FLAGS: Dict[str, Dict[str, bool]] = {
    "shared": {"group_sessions_per_user": False, "thread_sessions_per_user": False},
    "per-thread": {"group_sessions_per_user": True, "thread_sessions_per_user": False},
}
_DEFAULT_SESSION_MODE = "per-thread"

# agent-shared is the always-on native Bot Chat lane (one conversation per
# coworker, resolved by exact title, hermes_state.py:10061-10066), NOT a
# session-isolation flag mode; declaring it as a session_mode is rejected so a
# spec cannot silently encode it as flags.
_AGENT_SHARED_MODE = "agent-shared"

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


def _pop_dotted(mapping: Dict[str, Any], dotted: str) -> None:
    """Remove the leaf named by a dotted path, then prune each now-empty mapping
    along the path bottom-up. A sibling key that is still populated stops the
    prune, so an unrelated ``security.*`` value survives while ``security.approval``
    is emptied. A path that is absent or blocked by a non-mapping node is a no-op."""
    parts = dotted.split(".")
    chain: List[Tuple[Dict[str, Any], str]] = []
    node: Any = mapping
    for key in parts[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            return
        chain.append((node, key))
        node = child
    leaf = parts[-1]
    if leaf not in node:
        return
    node.pop(leaf)
    for container, key in reversed(chain):
        branch = container.get(key)
        if isinstance(branch, dict) and not branch:
            container.pop(key, None)
        else:
            break


def _is_platform_key(key: Any) -> bool:
    """True iff ``key`` names a platform (including dynamically-registered plugin
    platforms), tested with the ``Platform(key)`` constructor rather than by
    iterating the enum."""
    if not isinstance(key, str):
        return False
    try:
        Platform(key)
        return True
    except ValueError:
        return False


def _enforce_retention(config: Dict[str, Any]) -> None:
    """Force the fleet-safe retention values onto ``config``, overriding a
    declared value and inserting an omitted one alike — the requirement's
    "explicitly and never by assumption". A setdefault-style fill would leave a
    stale unsafe value untouched, so this always overwrites the leaf."""
    for dotted, value in _RETENTION_INVARIANTS.items():
        _set_dotted(config, dotted, value)


def _validate_approval_lists(config: Dict[str, Any], *, include_allowlist: bool) -> None:
    """Fail closed on a malformed per-role approval list.

    A present top-level ``command_allowlist`` (coworker profiles only — DEFAULT's
    is force-emptied, so its submitted value is not trusted) and a present
    ``approvals.deny`` must each be a list of non-empty, non-whitespace strings: a
    blank member is a silently-inert deny rule or an over-broad allowlist entry,
    and a non-list is a spec error. Type-checked before any iteration so a hostile
    value raises ``CompositionError``, never a raw ``TypeError``.
    """
    to_check: List[Tuple[str, Any]] = []
    if include_allowlist and "command_allowlist" in config:
        to_check.append(("command_allowlist", config["command_allowlist"]))
    approvals = config.get("approvals")
    # A present non-mapping approvals block is a spec error: the strip below
    # skips a non-dict value and it would be written raw, then the managed
    # overlay would silently replace it — so reject it here rather than fail open.
    if "approvals" in config and not isinstance(approvals, dict):
        raise CompositionError(
            f"approvals must be a mapping, got {type(approvals).__name__}"
        )
    if isinstance(approvals, dict) and "deny" in approvals:
        to_check.append(("approvals.deny", approvals["deny"]))
    for label, value in to_check:
        if not isinstance(value, list):
            raise CompositionError(f"{label} must be a list, got {type(value).__name__}")
        for member in value:
            if not isinstance(member, str) or not member.strip():
                raise CompositionError(
                    f"{label} entries must be non-empty strings, got {member!r}"
                )


def _strip_fleet_uniform_approvals(config: Dict[str, Any]) -> None:
    """Remove the eight fleet-uniform keys (six ``approvals.*`` plus
    ``security.approval.transport`` / ``security.approval.transport_fallback``) from
    a per-profile config so the immutability split is disjoint — they live ONLY in
    the managed fragment (AC-3). The per-role ``approvals.deny`` floor is LEFT in
    place (it is read fresh-config per profile, so it is honored in-gateway too).
    Emptied ``approvals`` / ``security.approval`` / ``security`` mappings are pruned
    bottom-up so no empty dict is written and an unrelated ``security.*`` sibling is
    untouched."""
    for dotted in _GOV_APPROVALS_MANAGED:
        _pop_dotted(config, dotted)


def _force_default_allowlist_empty(config: Dict[str, Any]) -> None:
    """Force the DEFAULT/multiplexer profile's top-level ``command_allowlist`` to
    ``[]`` — the in-gateway LAUNCH BASELINE, not steady-state isolation. The gateway
    process loads its process-global permanent allowlist ONCE at import from the
    launch (DEFAULT) profile (tools/approval.py:5970-5971), so an empty list means at
    launch in-gateway multiplex cron/webhook/api find nothing to bypass at the
    allowlist short-circuit (tools/approval.py:4784) and fall through to the deny
    resolvers. It does NOT close the cross-profile allowlist union leak:
    load_permanent_allowlist UNIONs each profile's allowlist into the one shared set
    (tools/approval.py:3056-3065), so a later in-gateway session init widens the
    bypass — a core gap owned by GOV-ENF/P8 (AC-4b), no isolation credit. Written
    explicitly and never by assumption: a declared non-empty allowlist is overwritten
    rather than trusted."""
    config["command_allowlist"] = []


def _enforce_deny_floor(config: Dict[str, Any]) -> None:
    """OVERWRITE ``approvals.deny`` with exactly the fixed force-push/tag/release
    deny floor on a rendered profile config.

    The floor is the never-bypassable backstop (it fires before the yolo / mode=off
    bypass), so it is a render-GUARANTEED fleet-safety property, not a per-role
    passthrough: the render SETS ``approvals.deny`` to ``_GOV_APPROVALS_DENY_FLOOR``
    on EVERY profile (builder, reviewer, DEFAULT), OVERWRITING whatever the coworker
    type or spine declared — so a spec that declares a weak or absent deny still
    ships the full floor. Despite the name, this is a centrally-enforced UNIFORM deny
    policy, NOT a union-minimum: a STRONGER per-profile deny an operator declared is
    intentionally replaced too (uniform fleet policy under central governance), which
    AC-GOV-F23-2 asserts. Runs AFTER the fleet-uniform strip so it operates on — and,
    when the strip pruned an emptied ``approvals`` block, re-creates — the per-profile
    approvals dict."""
    approvals = config.get("approvals")
    if not isinstance(approvals, dict):
        approvals = {}
        config["approvals"] = approvals
    approvals["deny"] = list(_GOV_APPROVALS_DENY_FLOOR)


def _build_managed_fragment() -> Dict[str, Any]:
    """Build the machine-wide managed-scope approvals fragment (the eight
    fleet-uniform keys, override-or-insert via ``_set_dotted``). The operator
    installs this one file to ``$HERMES_MANAGED_DIR/config.yaml`` (else
    ``/etc/hermes/config.yaml``); native ``_load_config_impl`` deep-merges it
    (managed-wins) onto every profile."""
    fragment: Dict[str, Any] = {}
    for dotted, value in _GOV_APPROVALS_MANAGED.items():
        _set_dotted(fragment, dotted, value)
    return fragment
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
    restriction" and are refused rather than rendered. Each member must be a string or
    a non-boolean integer id (Telegram chat ids are numeric); None, a mapping, a float
    or a bool is refused (bool is an int subclass, so it is excluded explicitly)."""
    if isinstance(values, str) or not isinstance(values, list) or not values:
        raise CompositionError(
            f"{what} must be a non-empty list of ids; empty, missing or a bare "
            "scalar would blanket-forward (an empty allowlist is unrestricted)")
    for v in values:
        if not (isinstance(v, str) or (isinstance(v, int) and not isinstance(v, bool))):
            raise CompositionError(
                f"{what} member {v!r} must be a string or non-boolean integer id")
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

    # Reject any key outside the engage vocabulary BEFORE reading mode/rendering, so a
    # misspelled control key (e.g. sender_scpoe) fails closed instead of being silently
    # dropped — dropping an intended sender_scope would render an unrestricted profile.
    unknown = set(block) - {"mode", "patterns", "channels", "sender_scope"}
    if unknown:
        raise CompositionError(
            f"config.engage.{platform} has unknown key(s): "
            f"{', '.join(sorted(repr(k) for k in unknown))}; "
            "supported: mode, patterns, channels, sender_scope")

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

    # Scope + scope-bypass keys are handled apart from the mode keys, and ONLY when the
    # coworker declares sender_scope: the scope key (allowed_channels/allowed_chats) and
    # each per-platform bypass key (a key that overrides the scope allowlist — Telegram
    # guest_mode, Slack reaction_trigger_target) are stripped from every alias and written
    # to their neutral value, making the declared scope authoritative. When sender_scope
    # is OMITTED both are left ENTIRELY UNTOUCHED (ADR §Design §3.2): a canonical bypass
    # value stays active (the operator's explicit choice, with no declared scope to widen),
    # and a bypass left only at a noncanonical alias is NOT relocated — RT-F01's
    # _require_canonical_platform_layout rejects the surviving noncanonical block
    # (fail-closed), so it is never silently relocated to canonical and activated.
    if "sender_scope" in block:
        scope = _bounded_ids(
            block.get("sender_scope"),
            f"engage sender_scope for platform {platform!r}")
        scope_key = caps["scope_key"]
        stripped |= _strip_engage_key(config, platform, scope_key)
        target[scope_key] = scope
        for bkey, breset in caps.get("scope_bypass", {}).items():
            stripped |= _strip_engage_key(config, platform, bkey)
            target[bkey] = copy.deepcopy(breset)

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
    if _MANAGED_FRAGMENT_DIR in types:
        raise CompositionError(
            f"a coworker type may not be named {_MANAGED_FRAGMENT_DIR!r} — it collides "
            f"with the machine-wide managed-scope fragment directory (out_root/{_MANAGED_FRAGMENT_DIR})"
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


def _resolve_session_mode(
    default_config: Dict[str, Any], resolved_by_type: Dict[str, Dict[str, Any]]
) -> Dict[str, bool]:
    """Resolve the single fleet-wide session mode from every EXPLICIT declaration.

    Phase A of the two-phase session-mode render: a session mode is a
    gateway-wide property (one shared SessionStore under multiplex), so it is
    resolved ONCE before any profile is rendered — a per-coworker loop could not
    detect coworker-vs-coworker divergence or propagate a late declaration.

    Collects ``session_mode`` from ``default_config`` (the gateway-level field)
    and from each resolved coworker ``config``. An OMITTED declaration
    contributes no opinion, so a lone coworker declaration sets the gateway-wide
    mode and an omit-vs-explicit pair is not a conflict. Two genuinely divergent
    EXPLICIT declarations (any pair), an unknown value, or ``agent-shared`` (the
    native Bot Chat lane, not a flag mode) each fail closed pre-startup. Returns
    the store-lane flags for the resolved mode.
    """
    declarations: List[Tuple[str, Any]] = []
    if "session_mode" in default_config:
        declarations.append(("default_config.session_mode", default_config["session_mode"]))
    for tname, resolved in resolved_by_type.items():
        cfg = resolved.get("config") or {}
        if "session_mode" in cfg:
            declarations.append((f"coworker {tname!r} config.session_mode", cfg["session_mode"]))

    modes: Set[str] = set()
    for where, mode in declarations:
        # isinstance BEFORE membership: an unhashable value (e.g. a YAML list)
        # would raise a raw TypeError from ``in``, failing OPEN.
        if not isinstance(mode, str) or mode not in _SESSION_MODE_FLAGS:
            if mode == _AGENT_SHARED_MODE:
                raise CompositionError(
                    f"{where}: 'agent-shared' is not a session-isolation mode — it is the "
                    f"always-on native Bot Chat lane (one conversation per coworker, resolved "
                    f"by exact title). Remove session_mode; the Bot Chat lane is always on."
                )
            raise CompositionError(
                f"{where}: unknown session_mode {mode!r} — expected one of "
                f"{sorted(_SESSION_MODE_FLAGS)}"
            )
        modes.add(mode)

    if len(modes) > 1:
        raise CompositionError(
            f"conflicting session_mode declarations {sorted(modes)}: the multiplex gateway "
            f"keys every profile through ONE gateway-wide SessionStore, so the fleet has a "
            f"single session mode. Declare one mode ("
            f"{', '.join(f'{w}={m!r}' for w, m in declarations)}). Per-(profile, platform) "
            f"session modes are the open upstream ask UA-5, not configurable here."
        )
    mode = modes.pop() if modes else _DEFAULT_SESSION_MODE
    return dict(_SESSION_MODE_FLAGS[mode])


def _strip_session_flags(block: Any) -> None:
    """Remove the two controlled flags from a platform block (bare on the block
    or under its ``extra``), pruning an ``extra`` the strip emptied. Any
    unrelated content is left in place so ``_require_canonical_platform_layout``
    still rejects a genuinely malformed block."""
    if not isinstance(block, dict):
        return
    extra = block.get("extra")
    if isinstance(extra, dict):
        for key in _SESSION_FLAG_KEYS:
            extra.pop(key, None)
        if not extra:
            block.pop("extra", None)
    for key in _SESSION_FLAG_KEYS:
        block.pop(key, None)


def _prune_empty(container: Dict[str, Any], key: str) -> None:
    """Remove ``key`` from ``container`` iff it now maps to an empty dict."""
    value = container.get(key)
    if isinstance(value, dict) and not value:
        container.pop(key, None)


def _mirror_and_strip_platform_flags(config: Dict[str, Any], flags: Dict[str, bool]) -> None:
    """Mirror the resolved flags into every canonical ``platforms.<p>.extra`` and
    strip them from every alias location the loader would merge, so the store
    lane and the EFFECTIVE merged adapter lane cannot diverge.

    The loader merges ``gateway.platforms.<p>`` then ``platforms.<p>`` then
    ``gateway.<p>`` into one ``PlatformConfig.extra`` with ``gateway.<p>`` winning
    last (gateway/config.py:1599-1637), so a divergent value at any alias would
    otherwise override the canonical one; stripping all aliases leaves the
    canonical ``platforms.<p>.extra`` as the sole surviving source.
    """
    canon = dict(flags)

    platforms = config.get("platforms")
    if isinstance(platforms, dict):
        for block in platforms.values():
            if not isinstance(block, dict):
                continue
            # Clear any stale flag spelling on the canonical block first (bare on
            # the block or under .extra) so the mirror below is the sole surviving
            # source of both flags.
            _strip_session_flags(block)
            if "extra" in block:
                # A present but non-mapping 'extra' (including None) is malformed;
                # leave it untouched so _require_canonical_platform_layout rejects it.
                if not isinstance(block["extra"], dict):
                    continue
                extra = block["extra"]
            else:
                extra = {}
                block["extra"] = extra
            extra.update(canon)

    gateway = config.get("gateway")
    if isinstance(gateway, dict):
        gw_platforms = gateway.get("platforms")
        if isinstance(gw_platforms, dict):
            for pname in list(gw_platforms):
                _strip_session_flags(gw_platforms[pname])
                _prune_empty(gw_platforms, pname)
            _prune_empty(gateway, "platforms")
        for key in list(gateway):
            if key != "platforms" and _is_platform_key(key):
                _strip_session_flags(gateway[key])
                _prune_empty(gateway, key)

    for key in list(config):
        if key != "platforms" and _is_platform_key(key):
            _strip_session_flags(config[key])
            _prune_empty(config, key)

    _prune_empty(config, "gateway")


def _apply_session_mode(config: Dict[str, Any], flags: Dict[str, bool], is_default: bool) -> None:
    """Serialize the resolved fleet session mode onto one rendered profile.

    Phase B, called per profile after ``_enforce_retention`` and before
    ``_require_canonical_platform_layout``. DEFAULT is the serialization owner: it
    carries the store-lane flags at the top level, the exact config the multiplex
    SessionStore reads (gateway/config.py:1220-1221 -> gateway/session.py:2010-2017).
    A non-default profile carries NO gateway-global copy (the store never reads a
    secondary profile's config), so any it declared is stripped. Every profile
    that wires a platform gets the flags mirrored into its canonical
    ``platforms.<p>.extra`` and every alias spelling stripped. The
    ``session_mode`` pseudo-key (a plugin-own input with no core reader) is
    removed from every profile.
    """
    config.pop("session_mode", None)
    nested_default = config.get("default_config")
    if isinstance(nested_default, dict):
        nested_default.pop("session_mode", None)

    # Strip any raw/aliased store-flag spelling everywhere first — the store
    # reads only the DEFAULT profile's TOP-LEVEL flags, so a top-level or
    # gateway.* copy is either inert-but-hostile (a coworker) or not the store
    # lane (gateway.* on DEFAULT). DEFAULT then re-writes the canonical top-level.
    gateway = config.get("gateway")
    for key in _SESSION_FLAG_KEYS:
        config.pop(key, None)
        if isinstance(gateway, dict):
            gateway.pop(key, None)
    if is_default:
        for key, value in flags.items():
            _set_dotted(config, key, value)

    _mirror_and_strip_platform_flags(config, flags)


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
    for key in config:
        if _is_platform_key(key):
            yield "top-level", key
    gateway = config.get("gateway")
    if isinstance(gateway, dict):
        for key in gateway:
            if _is_platform_key(key):
                yield "gateway", key
        gw_platforms = gateway.get("platforms")
        if isinstance(gw_platforms, dict):
            for key in gw_platforms:
                if _is_platform_key(key):
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

    # Phase A: resolve every coworker type and the DEFAULT config, then resolve
    # the single fleet-wide session mode ONCE — before rendering any profile.
    resolved_by_type: Dict[str, Dict[str, Any]] = {}
    for tname, tinfo in types.items():
        tname = _safe_name("type", tname)
        resolved_by_type[tname] = _resolve_type(tname, tinfo or {}, spines)
    default_config = _deep_merge(_merged_spine_config(spines), data.get("default_config") or {})
    session_flags = _resolve_session_mode(default_config, resolved_by_type)

    # Phase B: render each profile, applying the fleet session mode after
    # retention and before the canonical-layout / port / route checks.
    rendered: Dict[str, str] = {}
    for tname, resolved in resolved_by_type.items():
        _inject_self_plugin(resolved["config"], orchestrator_profile)
        _enforce_retention(resolved["config"])
        _validate_approval_lists(resolved["config"], include_allowlist=True)
        _strip_fleet_uniform_approvals(resolved["config"])
        _enforce_deny_floor(resolved["config"])
        _render_engage(resolved["config"])
        _apply_session_mode(resolved["config"], session_flags, is_default=False)
        _require_canonical_platform_layout(resolved["config"])
        _forbid_coworker_port_binding(resolved["config"], tname)
        _forbid_coworker_webhook_routes(resolved["config"], tname)
        pdir = out_root / tname
        _render_coworker(pdir, tname, resolved, skills_root, workflows_root, overlays_root)
        rendered[tname] = str(pdir)

    _enforce_retention(default_config)
    _validate_approval_lists(default_config, include_allowlist=False)
    _strip_fleet_uniform_approvals(default_config)
    _force_default_allowlist_empty(default_config)
    _enforce_deny_floor(default_config)
    _render_engage(default_config)
    _apply_session_mode(default_config, session_flags, is_default=True)
    _enforce_multiplex(default_config, roster)
    _require_canonical_platform_layout(default_config)
    _validate_webhook_routes(default_config, served)
    ddir = out_root / default_profile
    _render_default(ddir, default_profile, default_config)
    rendered[default_profile] = str(ddir)

    # Emit the machine-wide managed-scope fragment after every profile validated
    # and rendered. It is NOT added to `rendered`: the mapping is profile-name ->
    # distribution dir, and the fragment is a machine-wide config file, not a
    # profile (onboarding installs only the coworker TYPE profiles).
    managed_dir = out_root / _MANAGED_FRAGMENT_DIR
    managed_dir.mkdir(parents=True, exist_ok=True)
    _write_yaml(managed_dir / "config.yaml", _build_managed_fragment())

    return rendered
