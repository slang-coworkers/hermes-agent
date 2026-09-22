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
import shlex
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

import yaml

from gateway.config import Platform, coerce_systemd_watchdog_seconds, platform_binds_port
from hermes_cli.profiles import normalize_profile_name, validate_profile_name
from hermes_constants import get_default_hermes_root, get_hermes_home

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

# Raised fleet baseline for per-agent curated memory. Stock is 2200/1375
# (config_defaults.py memory.memory_char_limit / memory.user_char_limit) — too
# small for the coworker types — so the render floors every profile up to these.
# A type that legitimately needs more may declare a higher value: it is preserved
# unchanged and per-key (never clamped down, never coupled). Both keys have real
# readers (MemoryStore.__init__ and the _char_limit truncation gate).
_MEMORY_FLOOR: Dict[str, int] = {
    "memory.memory_char_limit": 4000,
    "memory.user_char_limit": 2000,
}
# Coworkers and the DEFAULT multiplexer are unattended, so a memory write must
# not stall on a human approval prompt: write_approval is forced off on every
# rendered profile, overriding a spine/type that declared it true.
_MEMORY_INVARIANTS: Dict[str, Any] = {"memory.write_approval": False}

# Curator SAFETY invariants (MEM-F42). The curator maintains the agent-created
# skills index that is loaded into every turn's system prompt, so enabling it and
# its rollback-able pre-run backup on every profile is the skills-index leg of
# bounded always-loaded memory. Forced true (never setdefault) so a re-pin to a
# tree whose curator default flipped off cannot silently leave a profile's skills
# index unbounded, and so every real pass attempts a recoverable snapshot. The
# per-role TUNING keys (interval_hours/min_idle_hours/consolidate/prune_builtins)
# are deliberately NOT here — they flow from the resolved spec per profile.
_CURATOR_INVARIANTS: Dict[str, Any] = {
    "curator.enabled": True,
    "curator.backup.enabled": True,
}
# backup.keep is a FLOOR, not a fixed value: a spine that legitimately retains a
# deeper snapshot history is preserved (a fixed 5 would delete older snapshots —
# data loss), while a below-floor/0/absent keep is raised to 5 so retention depth
# is a deliberate, visible fleet floor rather than the runtime's implicit 0->1
# clamp. Same floor-MAX shape as _MEMORY_FLOOR.
_CURATOR_BACKUP_KEEP_FLOOR = 5

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
# (hermes_cli/profile_distribution.py:88-95) plus the extras this render adds.
# "scripts" carries the DEFAULT profile's rendered webhook route scripts to
# $HERMES_HOME/scripts/ on install (webhook_filters resolves scripts there); the
# installer skips a declared owned path that a profile does not have, so coworker
# profiles without a scripts/ dir are unaffected (profile_distribution.py:613-614).
_DIST_OWNED: List[str] = [
    "SOUL.md", "config.yaml", "mcp.json", "skills", "cron",
    "distribution.yaml", "skill-bundles", ".env.template", "scripts",
]

# LOOP-F40 CI-gate route scripts materialized into the DEFAULT profile's
# scripts/ dir; a route's `script:` in the fleet spec references them by name.
_ROUTE_SCRIPTS = ("pr_ci_gate.py", "check_gate.py")

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


def _get_dotted(mapping: Dict[str, Any], dotted: str) -> Any:
    """Read counterpart to ``_set_dotted``: return the leaf named by a dotted
    path, or ``None`` if any segment is absent or blocked by a non-mapping node.
    Used to compare a declared value against the floor before enforcing it."""
    node: Any = mapping
    for key in dotted.split("."):
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


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


def _ensure_list_member(config: Dict[str, Any], dotted: str, value: str) -> None:
    """Append ``value`` to the list at ``dotted`` iff absent, preserving every
    pre-existing entry — the opposite of ``_set_dotted``'s overwrite. A missing
    or non-list leaf is replaced with a fresh single-element list."""
    current = _get_dotted(config, dotted)
    items = list(current) if isinstance(current, list) else []
    if value not in items:
        items.append(value)
    _set_dotted(config, dotted, items)


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


# The sandbox substrate is a DATA switch: the spec's `substrate` selects one descriptor
# that keeps the terminal.backend value, the terminal.* key prefix egress/mount write
# into, and the veto's expected_backend derived from ONE source, so rendered sandbox
# settings and the veto's expected backend cannot drift.
@dataclass(frozen=True)
class _SubstrateDescriptor:
    substrate: str
    backend: str          # terminal.backend value; the veto's expected_backend is this same value
    terminal_prefix: str  # substrate-specific terminal.* key prefix (podman -> "docker")
    # How the fleet reaches the sandbox: "container" (a local rootless container the
    # docker-prefixed egress/mount writers configure) or "remote_ssh" (an OpenShell
    # sandbox reached over the builtin ssh backend, whose egress is a per-profile
    # `openshell policy` file, not a `docker_*` env). The render branches on this so
    # no container-mount key is emitted for a remote backend.
    kind: str = "container"

    def key(self, leaf: str) -> str:
        """Dotted config key a substrate-specific terminal leaf serialises into."""
        return f"terminal.{self.terminal_prefix}_{leaf}"

    def leaf(self, name: str) -> str:
        """Bare terminal.<leaf> name (for a ``terminal.get(...)`` membership check)."""
        return f"{self.terminal_prefix}_{name}"


_DEFAULT_SUBSTRATE = "podman"
_SUBSTRATE_DESCRIPTORS: Dict[str, _SubstrateDescriptor] = {
    "podman": _SubstrateDescriptor(
        substrate="podman", backend="docker", terminal_prefix="docker", kind="container"),
    # OSH-F63: OpenShell-native sandbox per profile, reached over the builtin ssh
    # backend (terminal.backend: ssh; the veto's render-derived expected_backend is
    # this same "ssh"). No nested podman — egress is a per-profile openshell policy.
    "openshell": _SubstrateDescriptor(
        substrate="openshell", backend="ssh", terminal_prefix="ssh", kind="remote_ssh"),
}
_FLEET_GATES_PLUGIN = "nv-fleet-gates"
_FLEET_GATES_SETTINGS = "plugins.entries.nv-fleet-gates.settings"


def _resolve_substrate(data: Dict[str, Any]) -> _SubstrateDescriptor:
    """Resolve the fleet spec's ``substrate`` DATA field to its descriptor.

    Absent -> ``podman`` (back-compat). A known value -> its descriptor. An unknown
    value -> ``CompositionError`` (fail closed). Called at the TOP of ``compose``,
    before any distribution is written, so an unknown substrate leaves no partial
    fleet on disk."""
    raw = data.get("substrate", _DEFAULT_SUBSTRATE)
    if isinstance(raw, str) and raw in _SUBSTRATE_DESCRIPTORS:
        return _SUBSTRATE_DESCRIPTORS[raw]
    known = ", ".join(sorted(_SUBSTRATE_DESCRIPTORS))
    raise CompositionError(
        f"unknown substrate {raw!r}: the fleet render supports [{known}]; a new "
        f"substrate is a new _SUBSTRATE_DESCRIPTORS entry plus its spec/fixtures"
    )


def _validate_sandbox_scope(data: Dict[str, Any], descriptor: _SubstrateDescriptor) -> None:
    """Validate the top-level ``sandbox_scope`` DATA field for the remote-ssh
    substrate, at the TOP of ``compose`` before any distribution is written.

    OSH-F63 delivers ``profile`` (one OpenShell sandbox per profile; the default).
    ``session`` is a RECOGNISED value that fails closed — its per-session provider is
    deferred to the ``register_terminal_environment_provider`` path — so it raises
    naming that deferral rather than silently mis-rendering as ``profile``. Any other
    value is unknown. The container substrate has no ``sandbox_scope`` (its scope is
    container-per-session already), so the key is ignored there."""
    if descriptor.kind != "remote_ssh":
        return
    scope = data.get("sandbox_scope", "profile")
    if scope == "profile":
        return
    if scope == "session":
        raise CompositionError(
            "sandbox_scope 'session' is a recognised value but is deferred: its "
            "per-session OpenShell sandbox is rendered by the "
            "register_terminal_environment_provider provider path, which OSH-F63 does "
            "not build. Use sandbox_scope: profile (the default; one OpenShell sandbox "
            "per profile) — 'session' support ships with that provider."
        )
    raise CompositionError(
        f"unknown sandbox_scope {scope!r}: the openshell substrate supports "
        f"[profile]; 'session' is recognised but deferred to the provider path"
    )


def _reject_openshell_container_fields(data: Dict[str, Any], descriptor: _SubstrateDescriptor) -> None:
    """Refuse a remote-ssh spec that sets container-only mount fields. The
    remote-ssh substrate skips the docker mount/egress writers, so honouring these
    would be impossible — failing closed here is safer than silently ignoring them."""
    if descriptor.kind != "remote_ssh":
        return
    unsupported = [
        key for key in ("shared_learnings_root", "workspace_root")
        if data.get(key) not in (None, "")
    ]
    if data.get("install_surfaces") not in (None, []):
        unsupported.append("install_surfaces")
    if unsupported:
        raise CompositionError(
            f"substrate 'openshell' does not support container mount fields: "
            f"{', '.join(unsupported)}; these bind into a local container that the "
            f"remote-ssh substrate does not create"
        )


def _enforce_session_driver(config: Dict[str, Any], profile_name: str,
                            descriptor: _SubstrateDescriptor) -> None:
    """Force the fleet session-driver invariants onto one profile's ``config``
    (ISO-F15), rejecting any spec that fights them.

    ``terminal.backend`` is forced to ``descriptor.backend``: an omitted key is
    written (overriding the stock ``local`` default) and an explicit matching value
    is kept, but any other value — any other string or any non-string — raises
    ``CompositionError``. The substrate's shared-container-key
    (``terminal.<prefix>_shared_container_key``) is forced to the empty string: only
    an omitted key or the exact string ``""`` is accepted; every other value is
    refused the same way, including falsey non-strings (``False``/``0``/``[]``/
    ``{}``/``None``) that the config->env bridge would serialise to a NON-empty env
    string and so collapse per-profile container identity to one shared container.
    The content type-check runs before any comparison, so a non-string YAML value
    raises ``CompositionError`` rather than a raw ``TypeError``. FLEET-F62 also
    derives the veto's ``expected_backend`` from ``descriptor.backend`` here — the
    SAME source that writes ``terminal.backend`` — so a worker cannot make the veto
    compare against a value it chose; emitted only for a profile that enables the
    veto, so a non-fleet spec gets no orphan settings block.
    """
    backend = descriptor.backend
    terminal = config.get("terminal")
    if isinstance(terminal, dict):
        if "backend" in terminal and not (
            isinstance(terminal["backend"], str)
            and terminal["backend"] == backend
        ):
            raise CompositionError(
                f"{profile_name}: terminal.backend must be {backend!r} "
                f"(the fleet's per-profile {descriptor.substrate} sandbox backend); "
                f"got {terminal['backend']!r}"
            )
        # A remote-ssh substrate has no local container, so any inherited/spec-declared
        # terminal.docker_* key cannot be honoured — refuse it rather than silently
        # serialize a dead key (fail closed, and keep the no-docker_* invariant true).
        if descriptor.kind == "remote_ssh":
            docker_keys = sorted(
                k for k in terminal if isinstance(k, str) and k.startswith("docker_")
            )
            if docker_keys:
                raise CompositionError(
                    f"{profile_name}: substrate 'openshell' does not support container "
                    f"terminal keys {docker_keys}; the remote-ssh backend has no local "
                    f"container to configure"
                )
        # The shared-container-key is a container-substrate concept: a remote-ssh
        # sandbox has no shared-container identity to collapse, so the guard and the
        # empty-string write below are container-only (no terminal.<prefix>_* key is
        # emitted for the remote-ssh substrate).
        if descriptor.kind == "container":
            shared_key_leaf = descriptor.leaf("shared_container_key")
            if shared_key_leaf in terminal and not (
                isinstance(terminal[shared_key_leaf], str)
                and terminal[shared_key_leaf] == ""
            ):
                raise CompositionError(
                    f"{profile_name}: terminal.{shared_key_leaf} must be "
                    f"empty; got {terminal[shared_key_leaf]!r}"
                )
    _set_dotted(config, "terminal.backend", backend)
    if descriptor.kind == "container":
        _set_dotted(config, descriptor.key("shared_container_key"), "")
    if _FLEET_GATES_PLUGIN in ((config.get("plugins") or {}).get("enabled") or []):
        _set_dotted(config, f"{_FLEET_GATES_SETTINGS}.expected_backend", backend)


# ISO-F14 egress lockdown (OPTION A). The render supplies the sandbox's egress
# posture DIRECTLY (proxy.enabled is false, so the stock iron-proxy apparatus is a
# no-op at the pin): the OneCLI proxy + CA-bundle env, one read-only CA mount, a
# pinned image and cgroup limits, and docker_persist_across_processes false. No raw
# credential is ever written — each provider name carries a non-secret placeholder
# and OneCLI injects the real value at request time.
_EGRESS_PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
_EGRESS_NO_PROXY_VARS = ("NO_PROXY", "no_proxy")
_EGRESS_NO_PROXY_VALUE = "127.0.0.1,localhost,::1"
_EGRESS_CA_BUNDLE_VARS = (
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "HERMES_CA_BUNDLE",
    "DENO_CERT",
)
_EGRESS_PROVIDER_PLACEHOLDER = "onecli-injects-at-request-time"
# Swap-token keys would create a second credential path, so any HERMES_PROXY_TOKEN_*
# key in a spec is refused.
_EGRESS_FORBIDDEN_ENV_PREFIX = "HERMES_PROXY_TOKEN_"
# The OneCLI control-plane key authenticates the render to OneCLI and must stay
# host-only (C1, § Security condition 3). It is treated as a controlled egress name so
# EVERY host-env-forwarding path (docker_env, docker_forward_env, env_passthrough,
# docker_extra_args -e, provider_env) refuses it uniformly — never just one path.
_EGRESS_CONTROL_PLANE_VARS = ("ONECLI_API_KEY",)
# podman 3.4.4 has no host.docker.internal mapping, so a proxy address carrying the
# docker-desktop alias is rewritten to the literal docker0 bridge gateway IP.
_DOCKER_BRIDGE_HOST = "host.docker.internal"
_DOCKER_BRIDGE_IP = "172.17.0.1"
# A well-formed environment variable name. The docker backend strips whitespace off
# env names/values, so a controlled name is compared AFTER strip (a padded name would
# otherwise slip the collision check and become live at runtime).
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _normalize_proxy_addr(addr: str) -> str:
    return addr.replace(_DOCKER_BRIDGE_HOST, _DOCKER_BRIDGE_IP)


def _validate_egress_spec(egress: Any) -> Dict[str, Any]:
    """Validate + normalise the spec's top-level ``egress:`` block into the
    parameters ``_enforce_egress`` writes. Fail-closed: a missing or malformed
    field raises ``CompositionError`` rather than rendering a half-formed egress
    posture. Every type-check precedes any use so a hostile YAML value raises
    ``CompositionError``, never a raw ``TypeError``."""
    if not isinstance(egress, dict):
        raise CompositionError(f"egress must be a mapping, got {type(egress).__name__}")

    def _req_str(key: str) -> str:
        val = egress.get(key)
        if not isinstance(val, str) or not val.strip():
            raise CompositionError(f"egress.{key} must be a non-empty string, got {val!r}")
        return val

    proxy_addr = _req_str("proxy_addr")
    ca_host_path = _req_str("ca_host_path")
    ca_container_path = _req_str("ca_container_path")
    sandbox_image = _req_str("sandbox_image")

    providers = egress.get("provider_env", [])
    if not isinstance(providers, list):
        raise CompositionError(
            f"egress.provider_env must be a list of names, got {type(providers).__name__}")
    reserved = (
        set(_EGRESS_PROXY_VARS) | set(_EGRESS_NO_PROXY_VARS)
        | set(_EGRESS_CA_BUNDLE_VARS) | set(_EGRESS_CONTROL_PLANE_VARS)
    )
    normalized_providers: List[str] = []
    for name in providers:
        if not isinstance(name, str):
            raise CompositionError(
                f"egress.provider_env entries must be strings, got {name!r}")
        stripped = name.strip()
        if not _ENV_NAME_RE.match(stripped):
            raise CompositionError(
                f"egress.provider_env entries must be valid env var names, got {name!r}")
        # A provider name that is itself a reserved egress control would clobber the proxy/CA
        # value with the placeholder (the provider loop runs last), or emit a forbidden token.
        if stripped in reserved or stripped.startswith(_EGRESS_FORBIDDEN_ENV_PREFIX):
            raise CompositionError(
                f"egress.provider_env must not name a reserved egress control, got {name!r}")
        normalized_providers.append(stripped)

    memory = egress.get("container_memory")
    if isinstance(memory, bool) or not isinstance(memory, int) or memory <= 0:
        raise CompositionError(
            f"egress.container_memory must be a positive integer (MiB), got {memory!r}")

    cpu = egress.get("container_cpu")
    if isinstance(cpu, bool) or not isinstance(cpu, (int, float)) or cpu <= 0:
        raise CompositionError(
            f"egress.container_cpu must be a positive number, got {cpu!r}")

    return {
        "proxy_url": "http://" + _normalize_proxy_addr(proxy_addr),
        "ca_host_path": ca_host_path,
        "ca_container_path": ca_container_path,
        "ca_mount": f"{ca_host_path}:{ca_container_path}:ro",
        "provider_env": normalized_providers,
        "sandbox_image": sandbox_image,
        "container_memory": memory,
        "container_cpu": cpu,
    }


def _targets_ca(entry: Any, ca_container_path: str) -> bool:
    """True iff a ``docker_volumes`` / ``-v`` entry mounts something AT
    ``ca_container_path`` — the container-dest field of ``host:container[:opts]``.
    A colon-less entry is a target-only spec whose single field IS the container dest."""
    parts = str(entry).split(":")
    dest = parts[1] if len(parts) >= 2 else parts[0]
    return dest == ca_container_path


def _egress_controlled_names(params: Dict[str, Any]) -> Set[str]:
    return (
        set(_EGRESS_PROXY_VARS)
        | set(_EGRESS_NO_PROXY_VARS)
        | set(_EGRESS_CA_BUNDLE_VARS)
        | set(_EGRESS_CONTROL_PLANE_VARS)
        | set(params["provider_env"])
    )


def _is_controlled_env_name(name: Any, controlled: Set[str]) -> bool:
    if not isinstance(name, str):
        return False
    # Compare after strip: the docker backend strips whitespace off env names, so a
    # padded controlled name (" HTTPS_PROXY ") would otherwise slip this check and
    # become a live override at runtime.
    stripped = name.strip()
    return stripped in controlled or stripped.startswith(_EGRESS_FORBIDDEN_ENV_PREFIX)


def _extra_arg_env_name(tok: str, nxt: Any) -> Optional[str]:
    """The env var NAME a ``docker_extra_args`` token would set, or None. Handles the
    separate (``-e KEY``), joined (``--env=KEY``) and attached (``-eKEY``) forms."""
    if tok in ("-e", "--env"):
        return str(nxt).split("=", 1)[0] if nxt is not None else ""
    if tok.startswith("--env="):
        return tok[len("--env="):].split("=", 1)[0]
    if tok.startswith("-e") and tok != "-e":
        rest = tok[2:]
        rest = rest[1:] if rest.startswith("=") else rest
        return rest.split("=", 1)[0]
    return None


def _extra_arg_mount_dest(tok: str, nxt: Any) -> Optional[str]:
    """The container destination a ``docker_extra_args`` mount token targets, or None.
    Handles ``-v``/``--volume`` (separate, joined, attached) and ``--mount`` (its
    ``dst=``/``destination=``/``target=`` field). A colon-less ``-v`` spec is a
    target-only anonymous volume whose single field IS the container destination
    (``-v /p`` mounts an anonymous volume AT ``/p``), so it must not read as "no mount"."""
    spec: Optional[str] = None
    is_mount = False
    if tok in ("-v", "--volume"):
        spec = None if nxt is None else str(nxt)
    elif tok.startswith("--volume="):
        spec = tok[len("--volume="):]
    elif tok.startswith("-v") and tok != "-v":
        rest = tok[2:]
        spec = rest[1:] if rest.startswith("=") else rest
    elif tok == "--mount":
        spec, is_mount = (None if nxt is None else str(nxt)), True
    elif tok.startswith("--mount="):
        spec, is_mount = tok[len("--mount="):], True
    if spec is None:
        return None
    if is_mount:
        for field in spec.split(","):
            key, _, value = field.partition("=")
            if key.strip() in ("dst", "destination", "target"):
                return value.strip()
        return None
    parts = spec.split(":")
    if len(parts) == 1:
        return parts[0]
    return parts[1]


def _consumes_next_extra_arg(tok: str) -> bool:
    """True for the value-taking flag spellings whose value is the NEXT token, so the
    scan skips it (an attached/joined form carries its value in the same token)."""
    return tok in ("-e", "--env", "-v", "--volume", "--mount")


def _assert_extra_args_egress_safe(extra: List[Any], params: Dict[str, Any],
                                   profile_name: str, controlled: Set[str]) -> None:
    """Reject a ``docker_extra_args`` list that would inject a competing egress control
    the render does not own: a non-string entry (the backend drops it, shifting the
    argv into a valid override), a controlled env var (any ``-e``/``--env`` spelling),
    an ``--env-file`` (its contents are opaque here), a network override, an override
    of the pinned resource limits, or ANY mount at the CA path (the render owns that
    mount via ``docker_volumes``, so even a duplicate canonical one is refused)."""
    ca_container_path = params["ca_container_path"]
    # Reject non-strings up front: the backend discards them, which would shift a value
    # token (e.g. `["-e", 7, "HTTPS_PROXY=…"]`) into a valid override the scan below
    # would otherwise consume as `-e`'s (harmless) value.
    for tok in extra:
        if not isinstance(tok, str):
            raise CompositionError(
                f"{profile_name}: terminal.docker_extra_args entries must be strings, got {tok!r}")
    # Default-deny: apply ISO-F13's mount-safe allowlist to EVERY profile. The DEFAULT profile skips
    # _enforce_mount_composition (which runs the allowlist for coworkers), and under OPTION A
    # (proxy.enabled:false) the runtime egress guards are inert, so this offline belt is the sole gate
    # for a DEFAULT profile's docker_extra_args — the allowlist refuses an engine-socket -v, --tmpfs,
    # --use-api-socket, --privileged, --rootfs, a positional image, and anything else not vetted. The
    # egress-specific checks below still reject an ALLOWLISTED flag that overrides a managed control
    # (--memory/--cpus resource pins) or a controlled env name / network.
    _reject_disallowed_extra_args(extra, profile_name)
    i = 0
    while i < len(extra):
        tok = extra[i]
        nxt = extra[i + 1] if i + 1 < len(extra) else None

        if tok in ("--network", "--net") or tok.startswith(("--network=", "--net=")):
            raise CompositionError(
                f"{profile_name}: egress collision — docker_extra_args must not set the container "
                f"network ({tok!r}); the fleet topology is render-owned")
        # -m is the only single-dash `-m*` podman flag (memory), so its attached form
        # (`-m512m`) is caught by the prefix alongside --memory/--cpus.
        if tok in ("--memory", "--cpus") or tok.startswith(("--memory=", "--cpus=", "-m")):
            raise CompositionError(
                f"{profile_name}: egress collision — docker_extra_args must not override the pinned "
                f"resource limits ({tok!r})")
        if tok == "--env-file" or tok.startswith("--env-file="):
            raise CompositionError(
                f"{profile_name}: egress collision — docker_extra_args must not pass --env-file "
                f"(its contents can smuggle a controlled egress var)")

        env_name = _extra_arg_env_name(tok, nxt)
        if env_name is not None and _is_controlled_env_name(env_name, controlled):
            raise CompositionError(
                f"{profile_name}: egress collision — docker_extra_args sets a controlled env var "
                f"({env_name!r})")

        dest = _extra_arg_mount_dest(tok, nxt)
        if dest is not None and dest == ca_container_path:
            raise CompositionError(
                f"{profile_name}: egress collision — docker_extra_args must not mount at the CA path "
                f"{ca_container_path!r}; the render owns that mount")

        i += 2 if _consumes_next_extra_arg(tok) else 1


def _assert_egress_safe(config: Dict[str, Any], params: Dict[str, Any], profile_name: str,
                        descriptor: _SubstrateDescriptor) -> None:
    """Fail-closed pre-pass (ISO-F14): refuse a resolved profile whose merged
    inputs would fight the render-owned egress posture, BEFORE any profile is
    written, so a collision on a late-resolved profile (DEFAULT is validated last)
    leaves no partial fleet on disk. Pure — mutates nothing; ``_enforce_egress``
    does the writing in the Phase-B loop once every profile has passed. Every
    type-check precedes membership so a hostile YAML value raises
    ``CompositionError``, never a raw ``TypeError``. The substrate-specific terminal
    key names (env/forward_env/volumes/extra_args) come from ``descriptor``."""
    terminal = config.get("terminal")
    if not isinstance(terminal, dict):
        return
    controlled = _egress_controlled_names(params)

    env = terminal.get(descriptor.leaf("env"))
    if env is not None:
        if not isinstance(env, dict):
            raise CompositionError(
                f"{profile_name}: terminal.docker_env must be a mapping, got {type(env).__name__}")
        for key in env:
            if _is_controlled_env_name(key, controlled):
                raise CompositionError(
                    f"{profile_name}: egress collision on terminal.docker_env[{key!r}] — the render "
                    f"owns the egress env; remove it from the spec")

    fwd = terminal.get(descriptor.leaf("forward_env"))
    if fwd is not None:
        if not isinstance(fwd, list):
            raise CompositionError(
                f"{profile_name}: terminal.docker_forward_env must be a list, "
                f"got {type(fwd).__name__}")
        for name in fwd:
            if not isinstance(name, str):
                raise CompositionError(
                    f"{profile_name}: terminal.docker_forward_env entries must be strings, "
                    f"got {name!r}")
            # C1 (FLEET-F62): the render forwards EXACTLY the four proxy spellings by name so the
            # live token-bearing value wins over the docker_env placeholder at exec; an exact
            # proxy spelling is therefore allowed. Every OTHER controlled name — a padded proxy
            # alias, a CA var, NO_PROXY, a provider name, the ONECLI_API_KEY control-plane key,
            # or a swap token — still collides via the controlled-name check below.
            if name in _EGRESS_PROXY_VARS:
                continue
            if _is_controlled_env_name(name, controlled):
                raise CompositionError(
                    f"{profile_name}: egress collision — docker_forward_env forwards a controlled "
                    f"egress var ({name!r})")

    # env_passthrough is another host-env-forwarding path (values ride into the sandbox at
    # exec), so a controlled name there would override the rendered proxy/CA env.
    passthrough = terminal.get("env_passthrough")
    if passthrough is not None:
        if not isinstance(passthrough, list):
            raise CompositionError(
                f"{profile_name}: terminal.env_passthrough must be a list, "
                f"got {type(passthrough).__name__}")
        for name in passthrough:
            if _is_controlled_env_name(name, controlled):
                raise CompositionError(
                    f"{profile_name}: egress collision — env_passthrough forwards a controlled "
                    f"egress var ({name!r})")

    volumes = terminal.get(descriptor.leaf("volumes"))
    if volumes is not None:
        if not isinstance(volumes, list):
            raise CompositionError(
                f"{profile_name}: terminal.docker_volumes must be a list, "
                f"got {type(volumes).__name__}")
        for v in volumes:
            if _targets_ca(v, params["ca_container_path"]):
                raise CompositionError(
                    f"{profile_name}: egress collision on the CA mount — a mount targets "
                    f"{params['ca_container_path']!r}; the render owns the CA mount, so no profile "
                    f"may declare one and no composed mount may resolve to that path")

    extra = terminal.get(descriptor.leaf("extra_args"))
    if extra is not None:
        if not isinstance(extra, list):
            raise CompositionError(
                f"{profile_name}: terminal.docker_extra_args must be a list, "
                f"got {type(extra).__name__}")
        _assert_extra_args_egress_safe(extra, params, profile_name, controlled)


def _enforce_egress(config: Dict[str, Any], params: Dict[str, Any], profile_name: str,
                    descriptor: _SubstrateDescriptor) -> None:
    """Force the OPTION-A egress posture onto one profile's ``config`` (ISO-F14),
    overwriting a declared leaf and inserting an omitted one alike (mirrors
    ``_enforce_retention``). Runs in the Phase-B write loop after
    ``_assert_egress_safe`` has cleared every profile, so it never rejects here —
    the CA-divergence guard below is defensive and unreachable for a spec that
    passed the pre-pass. The substrate-specific terminal key names (env/image/
    persist/volumes) come from ``descriptor``; ``container_memory``/``container_cpu``
    are backend-neutral resource pins and stay literal."""
    _set_dotted(config, "proxy.enabled", False)

    current_env = _get_dotted(config, descriptor.key("env"))
    env = dict(current_env) if isinstance(current_env, dict) else {}
    for var in _EGRESS_PROXY_VARS:
        env[var] = params["proxy_url"]
    for var in _EGRESS_NO_PROXY_VARS:
        env[var] = _EGRESS_NO_PROXY_VALUE
    for var in _EGRESS_CA_BUNDLE_VARS:
        env[var] = params["ca_container_path"]
    for name in params["provider_env"]:
        env[name] = _EGRESS_PROVIDER_PLACEHOLDER
    _set_dotted(config, descriptor.key("env"), env)

    # C1 (FLEET-F62): forward EXACTLY the four proxy spellings BY NAME so the docker
    # client resolves each to the live token-bearing value from the per-profile secret
    # scope at exec, winning over the tokenless placeholder written above. The render
    # owns docker_forward_env like it owns the egress docker_env, so it is set to
    # exactly the four (any spec-declared forward_env is overwritten). Name-only — no
    # value is written here; the value travels through the client subprocess env.
    _set_dotted(config, descriptor.key("forward_env"), list(_EGRESS_PROXY_VARS))

    _set_dotted(config, descriptor.key("persist_across_processes"), False)
    _set_dotted(config, "terminal.container_memory", params["container_memory"])
    _set_dotted(config, "terminal.container_cpu", params["container_cpu"])
    _set_dotted(config, descriptor.key("image"), params["sandbox_image"])

    ca_container_path = params["ca_container_path"]
    ca_mount = params["ca_mount"]
    current_vols = _get_dotted(config, descriptor.key("volumes"))
    kept: List[Any] = []
    for v in (current_vols if isinstance(current_vols, list) else []):
        if _targets_ca(v, ca_container_path):
            if str(v) != ca_mount:
                raise CompositionError(
                    f"{profile_name}: egress collision on the CA mount — a divergent source targets "
                    f"{ca_container_path!r}")
            continue  # drop every CA-target entry; exactly one canonical is re-appended below
        kept.append(v)
    kept.append(ca_mount)
    _set_dotted(config, descriptor.key("volumes"), kept)


# --- OSH-F63 openshell (remote-ssh) substrate render -----------------------
#
# The openshell substrate reaches one OpenShell sandbox per profile over the
# builtin ssh backend. Unlike the container path there is no local container to
# configure: the per-profile egress posture is a standalone `openshell policy`
# file, and the terminal block carries the builtin ssh coordinates (host/user/
# port/key-path). Materialising the on-box `openshell policy` from that file, and
# installing the ssh-config/ProxyCommand, are operator provisioning steps — not
# render steps.

_OPENSHELL_SSH_USER = "sandbox"
_OPENSHELL_SSH_PORT = 22
# The OneCLI control plane hop must never be a policy tool-egress allow (only the
# OneCLI request proxy 172.17.0.1:10255 is); refused if a spec's egress names it.
_OPENSHELL_CONTROL_PLANE_HOSTPORT = "172.17.0.1:10256"


def _openshell_keys_dir() -> Path:
    """Directory the per-profile ssh key PATHS are rooted at — under the gateway
    ``$HERMES_HOME`` (render-time ``get_hermes_home()``). The render writes a path
    here per profile; it never writes key material (the builtin ssh backend takes
    ``-i <path>``, and the operator provisions the key files at that path)."""
    return get_hermes_home() / "openshell" / "keys"


def _openshell_sandbox_name(fleet_name: str, profile_name: str) -> str:
    """The OpenShell sandbox alias for a profile: ``<fleet>-<profile>``. The single
    source of this name for the three places that MUST agree — the ssh render
    (``terminal.ssh_host``), the veto's managed ``expected_ssh_host`` anchor, and the
    provision plan's ``--name`` — so a create / ssh-config / veto can never drift."""
    return f"{fleet_name}-{profile_name}"


def _enforce_openshell_ssh(config: Dict[str, Any], profile_name: str, fleet_name: str,
                           keys_dir: Path) -> None:
    """Write one coworker's builtin-ssh terminal block for its OWN OpenShell sandbox:
    a distinct ``ssh_host`` (``<fleet>-<profile>``, the sandbox name), a uniform
    ``ssh_user``/``ssh_port``, and an ``ssh_key`` PATH under ``keys_dir`` (distinct
    leaf per profile, never key material)."""
    _set_dotted(config, "terminal.ssh_host", _openshell_sandbox_name(fleet_name, profile_name))
    _set_dotted(config, "terminal.ssh_user", _OPENSHELL_SSH_USER)
    _set_dotted(config, "terminal.ssh_port", _OPENSHELL_SSH_PORT)
    _set_dotted(config, "terminal.ssh_key", str(keys_dir / profile_name))


def _validate_openshell_egress(egress: Any) -> Dict[str, Any]:
    """Validate the remote-ssh substrate's ``egress`` block into the openshell policy
    parameters. Fail-closed like ``_validate_egress_spec``: a missing/malformed field
    raises ``CompositionError`` rather than emitting a half-formed policy. The policy
    ALLOW set is EXACTLY {the OneCLI request hop, the inference route, the ssh control
    path}; a wildcard or the OneCLI control plane (…:10256) is refused."""
    if not isinstance(egress, dict):
        raise CompositionError(f"egress must be a mapping, got {type(egress).__name__}")

    def _req_str(key: str) -> str:
        val = egress.get(key)
        if not isinstance(val, str) or not val.strip():
            raise CompositionError(f"egress.{key} must be a non-empty string, got {val!r}")
        # No newline/CR/NUL: these values are interpolated into the provisioning plan,
        # so a control char could inject an extra command line (defeating the
        # no-container-engine guarantee). The provision plan also shlex-quotes them.
        if any(c in val for c in "\n\r\x00"):
            raise CompositionError(
                f"egress.{key} must not contain control characters, got {val!r}")
        return val.strip()

    proxy_addr = _normalize_proxy_addr(_req_str("proxy_addr"))
    inference_route = _req_str("inference_route")
    ssh_control_path = _req_str("ssh_control_path")
    sandbox_image = _req_str("sandbox_image")
    allow = [proxy_addr, inference_route, ssh_control_path]
    for entry in allow:
        if "*" in entry:
            raise CompositionError(
                f"openshell policy allow entry must not be a wildcard, got {entry!r}")
        if _OPENSHELL_CONTROL_PLANE_HOSTPORT in entry:
            raise CompositionError(
                f"openshell policy must not allow the OneCLI control plane "
                f"{_OPENSHELL_CONTROL_PLANE_HOSTPORT!r} as tool egress, got {entry!r}")
    return {"allow": allow, "sandbox_image": sandbox_image}


def _enforce_openshell_policy(pdir: Path, profile_name: str, allow: List[str]) -> None:
    """Write one coworker's per-profile ``openshell policy`` file
    (``policy-<profile>.yaml``) beside its ``config.yaml`` — the allow-listed egress
    endpoints, exactly the declared set (the render's ``egress.allow`` contract; the
    operator materialises the on-box ``openshell policy`` from it at provisioning).

    The policy file is also added to the distribution manifest's ``distribution_owned``
    so ``install_distribution`` carries it into the installed profile rather than
    dropping it. ``config.yaml`` (and thus ``distribution.yaml``) were already written
    by ``_render_coworker`` before this runs."""
    policy_name = f"policy-{profile_name}.yaml"
    _write_yaml(pdir / policy_name, {"egress": {"allow": list(allow)}})
    dist_path = pdir / "distribution.yaml"
    dist = yaml.safe_load(dist_path.read_text(encoding="utf-8")) or {}
    owned = dist.get("distribution_owned")
    if isinstance(owned, list) and policy_name not in owned:
        owned.append(policy_name)
        _write_yaml(dist_path, dist)


def build_provision_plan(data: Dict[str, Any], descriptor: _SubstrateDescriptor) -> List[str]:
    """Build the deterministic ``openshell`` provisioning plan for a remote-ssh fleet:
    per coworker profile, a sandbox-create + policy-set + ssh-config line, then the
    teardown (sandbox-delete + policy-delete). Returns ``[]`` for any non-remote-ssh
    substrate (only openshell has a plan). Computed from spec DATA only (roster, fleet
    name, pinned image) so it is byte-stable across runs — no ``--out`` path appears,
    and no container engine (podman/docker) is invoked."""
    if descriptor.kind != "remote_ssh":
        return []
    fleet_name = _safe_name("project", data.get("project", "fleet"))
    # shlex-quote the spec-supplied image so a metachar cannot alter the plan (the
    # egress validator already refuses control chars). fleet/role names are
    # _safe_name-validated, so they carry no shell metachars.
    image = shlex.quote(_validate_openshell_egress(data.get("egress") or {})["sandbox_image"])
    roster = sorted(_safe_name("type", t) for t in (data.get("types") or {}))
    lines: List[str] = []
    # `--policy` names the BARE `policy-<role>.yaml` (each profile's distribution ships
    # that file; the operator materialises it at that name before running the plan). The
    # create `--name` is the sandbox alias == that profile's rendered `terminal.ssh_host`,
    # and the ssh-config / delete target that same name — so the plan joins to the render
    # per profile. All setup lines (create, policy-set, ssh-config) precede all teardown
    # lines (sandbox-delete, policy-delete).
    for role in roster:
        lines.append(
            f"openshell sandbox create --name {_openshell_sandbox_name(fleet_name, role)} "
            f"--from {image} --policy policy-{role}.yaml")
    for role in roster:
        lines.append(f"openshell policy set policy-{role}.yaml")
    for role in roster:
        lines.append(f"openshell sandbox ssh-config {_openshell_sandbox_name(fleet_name, role)}")
    for role in roster:
        lines.append(f"openshell sandbox delete {_openshell_sandbox_name(fleet_name, role)}")
    for role in roster:
        lines.append(f"openshell policy delete policy-{role}.yaml")
    return lines


# Container path the shared-learnings clone is mounted at (MEM-F43). Kept OUTSIDE
# /workspace because tools/environments/docker.py matches ":/workspace" as a
# substring, so any ":/workspace..." mount would suppress the coworker's auto
# cwd->/workspace bind.
WIKI_MOUNT = "/mnt/shared-learnings"


def _enforce_shared_learnings(config: Dict[str, Any], clone: str, is_orchestrator: bool) -> None:
    """Surface the fleet's shared-learnings clone into a coworker profile (MEM-F43).

    Three edits, each idempotent: add ``<clone>/skills`` to ``skills.external_dirs``
    (host path — external-dir discovery runs host-side) and ``WIKI_PATH`` to
    ``terminal.env_passthrough`` (so the per-profile sandbox forwards it), both
    preserving any pre-existing entries; and mount the clone ROOT into the sandbox
    via ``terminal.docker_volumes`` — read-write for the orchestrator (which builds
    the wiki), read-only for every other coworker (defence-in-depth). The mount is
    access-mode-sensitive, so any inherited clone mount (rw or ro) is dropped before
    the single role-appropriate form is appended — an inherited rw form must never
    survive onto a worker."""
    _ensure_list_member(config, "skills.external_dirs", f"{clone}/skills")
    _ensure_list_member(config, "terminal.env_passthrough", "WIKI_PATH")

    rw_mount = f"{clone}:{WIKI_MOUNT}"
    ro_mount = f"{rw_mount}:ro"
    desired = rw_mount if is_orchestrator else ro_mount
    current = _get_dotted(config, "terminal.docker_volumes")
    volumes = [
        entry for entry in (current if isinstance(current, list) else [])
        if not (isinstance(entry, str) and entry.strip() in {rw_mount, ro_mount})
    ]
    volumes.append(desired)
    _set_dotted(config, "terminal.docker_volumes", volumes)


def _reject_malformed_terminal(config: Dict[str, Any], profile_name: str) -> None:
    """Fail closed on a non-mapping ``terminal`` or a non-list ``terminal.docker_volumes``.

    Runs in the Phase-A pre-pass BEFORE any enforcer normalizes those values:
    ``_enforce_session_driver`` would replace a scalar ``terminal`` via ``_set_dotted``,
    and ``_enforce_shared_learnings`` coerces a non-list ``docker_volumes`` to ``[]`` —
    either of which would mask malformed spec input from the mount-composition check,
    so the shape is validated on the raw resolved value first."""
    terminal = config.get("terminal")
    if terminal is None:
        return
    if not isinstance(terminal, dict):
        raise CompositionError(
            f"{profile_name}: terminal must be a mapping; got {type(terminal).__name__}"
        )
    if "docker_volumes" in terminal and not isinstance(terminal["docker_volumes"], list):
        raise CompositionError(
            f"{profile_name}: terminal.docker_volumes must be a list; "
            f"got {type(terminal['docker_volumes']).__name__}"
        )


def _require_abs_mount_path(value: Any, label: str, profile_name: str) -> Path:
    """Validate a spec-declared mount host path before it is interpolated into a
    ``host:container:mode`` volume string. It must be a non-blank string, contain
    no ``:`` (which would smuggle extra colon-delimited fields such as a
    ``:/workspace`` destination), be absolute, and carry no ``..`` component. The
    content type-check runs before any use, so a non-string YAML value raises
    ``CompositionError`` rather than a raw ``TypeError``."""
    if not isinstance(value, str) or not value.strip():
        raise CompositionError(
            f"{profile_name}: {label} must be a non-empty path string; got {value!r}"
        )
    if ":" in value:
        raise CompositionError(f"{profile_name}: {label} must not contain ':'; got {value!r}")
    path = Path(value)
    if not path.is_absolute():
        raise CompositionError(f"{profile_name}: {label} must be an absolute path; got {value!r}")
    if value.startswith("//"):
        # POSIX preserves exactly two leading slashes, so "//workspace" evades the
        # `.startswith("/workspace")` and `:/workspace` substring guards below while
        # resolve() still collapses it to "/workspace". Require a canonical single
        # leading slash so those string guards and the clone-destination check hold.
        raise CompositionError(
            f"{profile_name}: {label} must be canonical with a single leading slash; got {value!r}"
        )
    if ".." in path.parts:
        raise CompositionError(f"{profile_name}: {label} must not contain '..'; got {value!r}")
    return path


def _mount_conflicts(host_path: Path, protected_root: Path) -> bool:
    """True when the resolved ``host_path`` equals ``protected_root``, lies under it,
    or is an ancestor of it — any of which exposes the protected subtree. The fleet
    Hermes root contains every ``<root>/profiles/<name>`` dir, so this bidirectional
    check covers "no path under $HERMES_HOME, the profile dir or their parents" in one
    test. Mirrors tools/path_security.py:15-34 (``resolve()`` follows symlinks and
    normalizes ``..``; ``relative_to`` detects containment), applied both directions
    and reimplemented locally to keep the offline renderer free of a core import. An
    unresolvable path fails closed."""
    try:
        resolved = host_path.resolve()
        root = protected_root.resolve()
    except (OSError, RuntimeError, ValueError):
        # RuntimeError: symlink loop (3.11/3.12); ValueError: embedded NUL. Fail closed.
        return True
    if resolved == root:
        return True
    try:
        resolved.relative_to(root)
        return True
    except ValueError:
        pass
    try:
        root.relative_to(resolved)
        return True
    except ValueError:
        pass
    return False


# terminal.docker_extra_args is appended VERBATIM to the container `run`
# (docker.py:1368-1401; the egress filter at :636-666 only touches env/network), a
# second channel beside docker_volumes. Allowlist only flags whose name AND value
# cannot select a host path, mount, socket, device, or namespace; every other flag, and
# every short flag, is refused, so the channel is closed by construction. Mounts keep
# exactly one policed channel, docker_volumes.
# Value True = the flag consumes a value (inline ``=value`` or the next token); False =
# boolean. Each permitted flag is a resource-limit, lifecycle, or hardening flag.
_EXTRA_ARG_ALLOWLIST: Dict[str, bool] = {
    "--shm-size": True,
    "--cap-drop": True,
    "--memory": True,
    "--memory-swap": True,
    "--memory-reservation": True,
    "--cpus": True,
    "--cpu-shares": True,
    "--cpuset-cpus": True,
    "--pids-limit": True,
    "--ulimit": True,
    "--restart": True,
    "--stop-signal": True,
    "--stop-timeout": True,
    "--read-only": False,
    "--init": False,
    "--no-healthcheck": False,
}


def _reject_disallowed_extra_args(extra_args: List[str], profile_name: str) -> None:
    """Refuse any ``terminal.docker_extra_args`` flag not on ``_EXTRA_ARG_ALLOWLIST``.

    Tokens are walked left to right exactly as the container runtime parses them: a
    value-taking allowed flag consumes its value (an inline ``=value`` or the next
    token), and that value is never re-examined as a flag — so a mount flag can only
    take effect in a flag position, where the allowlist rejects it. A value-taking flag
    with an empty inline value, a missing value, or a space-separated value that is
    itself flag-like is refused (fail closed); the last case removes the only path by
    which a ``-v`` could be swallowed as a value instead of being rejected."""
    i = 0
    n = len(extra_args)
    while i < n:
        name, sep, inline = extra_args[i].strip().partition("=")
        arity = _EXTRA_ARG_ALLOWLIST.get(name)
        if arity is None:
            raise CompositionError(
                f"{profile_name}: terminal.docker_extra_args flag {name!r} is not on the "
                f"mount-safe allowlist (only vetted non-mount flags are permitted; mounts "
                f"must go through the policed docker_volumes set)"
            )
        if arity:
            if sep:
                value = inline
            else:
                value = extra_args[i + 1].strip() if i + 1 < n else ""
                if value and not value.startswith("-"):
                    i += 1
            if not value or (not sep and value.startswith("-")):
                raise CompositionError(
                    f"{profile_name}: terminal.docker_extra_args flag {name!r} must be "
                    f"followed by a value"
                )
        i += 1


def _enforce_mount_composition(
    config: Dict[str, Any],
    profile_name: str,
    workspace_root: Any,
    install_surfaces: Any,
    shared_learnings_root: Any,
    is_orchestrator: bool,
    descriptor: _SubstrateDescriptor,
) -> None:
    """Render one coworker profile's ``terminal.docker_volumes`` as an explicit,
    policed, closed set — the single final owner of the key (ISO-F13).

    The set is exactly: the profile's workspace root mounted read-write same-path,
    the MEM-F43 shared-learnings clone mount when a clone is configured (unchanged
    role split — rw orchestrator / ro worker), and each declared install surface
    mounted read-only same-path — and nothing else. Every host path must resolve
    OUTSIDE the fleet Hermes root ``get_default_hermes_root()`` (which contains every
    sibling ``<root>/profiles/<name>`` dir), and no completed entry may carry the
    ``:/workspace`` substring that ``docker.py:998`` reads as an explicit /workspace
    mount (which would suppress the auto cwd bind). Fails closed: an absent/null/blank
    ``workspace_root`` or a missing/non-bool ``container_persistent`` is a
    ``CompositionError``, never a silent default. Also forces
    ``docker_mount_cwd_to_workspace: true``."""
    protected_root = get_default_hermes_root()

    persistent = _get_dotted(config, "terminal.container_persistent")
    if not isinstance(persistent, bool):
        raise CompositionError(
            f"{profile_name}: terminal.container_persistent must be set explicitly to a "
            f"bool per role; got {persistent!r}"
        )

    # docker_volumes is not the only mount channel: terminal.docker_extra_args is
    # appended verbatim to `docker run` (docker.py:1368-1401), so a mount/host-exposure
    # flag there would bypass the closed set. Own that channel too — allowlist it.
    extra_args = _get_dotted(config, descriptor.key("extra_args"))
    if extra_args is not None:
        if not isinstance(extra_args, list):
            raise CompositionError(
                f"{profile_name}: terminal.docker_extra_args must be a list; "
                f"got {type(extra_args).__name__}"
            )
        for arg in extra_args:
            if not isinstance(arg, str):
                raise CompositionError(
                    f"{profile_name}: terminal.docker_extra_args entries must be strings; "
                    f"got {arg!r}"
                )
        _reject_disallowed_extra_args(extra_args, profile_name)

    ws_root = _require_abs_mount_path(workspace_root, "workspace_root", profile_name)
    ws = ws_root / profile_name / "workspace"

    if install_surfaces is None:
        install_surfaces = []
    if not isinstance(install_surfaces, list):
        raise CompositionError(
            f"{profile_name}: install_surfaces must be a list; "
            f"got {type(install_surfaces).__name__}"
        )
    surfaces = [
        _require_abs_mount_path(surface, "install_surface", profile_name)
        for surface in install_surfaces
    ]

    # A same-path mount whose host path begins with /workspace would put ":/workspace"
    # in its volume string (docker.py:998) and suppress the auto cwd->/workspace bind.
    for host in [ws, *surfaces]:
        if str(host).startswith("/workspace"):
            raise CompositionError(
                f"{profile_name}: mount host path {str(host)!r} must not begin with "
                f"'/workspace' (it would suppress docker_mount_cwd_to_workspace)"
            )

    # The shared-learnings clone (MEM-F43) is the ONLY entry an earlier writer may
    # legitimately have added. Recompute the permitted mount (recompute-and-compare,
    # never substring recognition) and refuse unless the current docker_volumes is
    # exactly that (clone configured) or empty (not) — this rejects any spec-injected
    # non-clone entry.
    clone_mount: Optional[str] = None
    if shared_learnings_root:
        _require_abs_mount_path(shared_learnings_root, "shared_learnings_root", profile_name)
        rw_mount = f"{shared_learnings_root}:{WIKI_MOUNT}"
        clone_mount = rw_mount if is_orchestrator else f"{rw_mount}:ro"

    current = _get_dotted(config, descriptor.key("volumes"))
    if current is not None and not isinstance(current, list):
        raise CompositionError(f"{profile_name}: terminal.docker_volumes must be a list")
    current_list = [str(v).strip() for v in current] if isinstance(current, list) else []
    expected_pre = [clone_mount] if clone_mount is not None else []
    if current_list != expected_pre:
        raise CompositionError(
            f"{profile_name}: terminal.docker_volumes carries an unexpected entry "
            f"{current_list!r}; only the shared-learnings mount may pre-exist mount composition"
        )

    checked = [("workspace_root", ws)]
    if shared_learnings_root:
        checked.append(("shared_learnings_root", Path(str(shared_learnings_root))))
    checked.extend(("install_surface", surface) for surface in surfaces)
    for label, host in checked:
        if _mount_conflicts(host, protected_root):
            raise CompositionError(
                f"{profile_name}: {label} {str(host)!r} resolves into or over the fleet "
                f"Hermes root {str(protected_root)!r}"
            )

    # No two -v may target the same container destination. Same-path mounts land at
    # their own host path; the clone lands at WIKI_MOUNT — an install surface equal to
    # WIKI_MOUNT would shadow the clone. Rejecting the collision keeps the set well-formed.
    destinations = [("workspace_root", str(ws))]
    if clone_mount is not None:
        destinations.append(("shared_learnings_root", WIKI_MOUNT))
    destinations.extend(("install_surface", str(surface)) for surface in surfaces)
    seen: Dict[str, str] = {}
    for label, dest in destinations:
        if dest in seen:
            raise CompositionError(
                f"{profile_name}: {label} container destination {dest!r} collides with "
                f"an earlier mount ({seen[dest]})"
            )
        seen[dest] = label

    volumes = [f"{ws}:{ws}:rw"]
    if clone_mount is not None:
        volumes.append(clone_mount)
    volumes.extend(f"{surface}:{surface}:ro" for surface in surfaces)

    for entry in volumes:
        if ":/workspace" in entry:
            raise CompositionError(
                f"{profile_name}: composed volume {entry!r} contains ':/workspace'"
            )

    _set_dotted(config, descriptor.key("volumes"), volumes)
    _set_dotted(config, descriptor.key("mount_cwd_to_workspace"), True)


def _enforce_memory_policy(config: Dict[str, Any]) -> None:
    """Raise per-agent memory caps to the fleet floor (never clamp a higher
    declared value down; each key independently) and force write_approval off.
    Mirrors ``_enforce_retention``: always writes the leaf so a stale or omitted
    value can never survive to disk. A non-integer declared cap floors to the
    minimum rather than propagating a malformed value."""
    for dotted, floor in _MEMORY_FLOOR.items():
        current = _get_dotted(config, dotted)
        try:
            current_int = int(current) if current is not None else 0
        except (TypeError, ValueError):
            current_int = 0
        _set_dotted(config, dotted, max(current_int, floor))
    for dotted, value in _MEMORY_INVARIANTS.items():
        _set_dotted(config, dotted, value)


def _enforce_curator(config: Dict[str, Any]) -> None:
    """Force the curator safety invariants and floor backup.keep on ``config``,
    overriding a declared value and inserting an omitted one alike. Mirrors
    ``_enforce_memory_policy``: the two booleans are always overwritten so a stale
    or omitted value can never survive to disk, and keep is floor-MAX — a higher
    declared retention depth is preserved, a lower/0/absent/non-integer one is
    raised to the floor. Only the three safety leaves are touched, so per-role
    tuning merged from the resolved spec is left intact."""
    for dotted, value in _CURATOR_INVARIANTS.items():
        _set_dotted(config, dotted, value)
    declared = _get_dotted(config, "curator.backup.keep")
    try:
        declared_int = int(declared) if declared is not None else 0
    except (TypeError, ValueError, OverflowError):
        declared_int = 0
    _set_dotted(config, "curator.backup.keep", max(declared_int, _CURATOR_BACKUP_KEEP_FLOOR))


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


def _strip_default_timezone(config: Dict[str, Any]) -> None:
    """Drop any ``timezone`` from the DEFAULT/multiplexer profile config. The gateway
    promotes a non-empty ``timezone`` in the launch (DEFAULT) profile's config.yaml to
    the PROCESS-GLOBAL ``HERMES_TIMEZONE`` env var (gateway/run.py:2862-2865,
    presence-sensitive per :2654-2656); hermes_time._timezone_cache_identity then pins
    that single ("environment", tz) identity for EVERY multiplexed profile
    (hermes_time.py:48-51, :60-63), clobbering each coworker's own zone. The DEFAULT
    config inherits the base spine's timezone via ``_merged_spine_config``, so it is
    deleted here (empty == absent for both the bridge and hermes_time, both ``.strip()``)
    to leave the fleet zone unset and let each coworker profile resolve under its own
    ("config", path) identity. DEFAULT-only — coworker profiles keep their own timezone."""
    config.pop("timezone", None)


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


def _build_managed_fragment(profile_roles: Dict[str, str],
                            expected_ssh_host: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Build the machine-wide managed-scope fragment: the eight fleet-uniform
    approval keys (GOV-F23) plus the FLEET-F62 fleet role map. The operator installs
    this one file to ``$HERMES_MANAGED_DIR/config.yaml`` (else
    ``/etc/hermes/config.yaml``); native ``_load_config_impl`` deep-merges it
    (managed-wins) onto every profile.

    ``profile_roles`` ({profile: "orchestrator"|"worker"}) is emitted into the
    nv-fleet-gates settings at the MANAGED scope, not per-profile config, because the
    veto's fleet-admin gate reads the role from there — a worker must not be able to
    rewrite its own role by editing its own config (nv-fleet-gates prefers the managed
    map over the per-profile fallback).

    ``expected_ssh_host`` ({profile: sandbox host}), for the remote-ssh substrate
    (OSH-F63), is pinned at the same managed scope so the veto's host-binding predicate
    anchors each profile to its OWN sandbox host from a map a worker cannot forge via
    its per-profile config."""
    fragment: Dict[str, Any] = {}
    for dotted, value in _GOV_APPROVALS_MANAGED.items():
        _set_dotted(fragment, dotted, value)
    _set_dotted(fragment, f"{_FLEET_GATES_SETTINGS}.profile_roles", dict(profile_roles))
    if expected_ssh_host:
        _set_dotted(fragment, f"{_FLEET_GATES_SETTINGS}.expected_ssh_host", dict(expected_ssh_host))
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


# Supervision floor (FLEET-F62). The DEFAULT multiplexer gateway runs as a
# systemd Type=notify service, which emits WatchdogSec (and Type=notify, not
# Type=simple) ONLY when gateway.systemd_watchdog_seconds > 0
# (hermes_cli/gateway.py). An absent/0/below-floor value would ship no watchdog (or
# one too tight), so the render FLOORS it to >= 30s on the DEFAULT profile: a value the
# core coercer drops (bool, out-of-range, malformed string, absent/0) or a coerced value
# below the floor is forced to the floor; a coerced value >= 30 — an int or a valid
# decimal string like "90" — is kept. Root and nested spellings are both popped first (core reads
# the root spelling with precedence over gateway.*), then only the canonical nested
# key is written — the same pop-both shape as _enforce_multiplex, so a hostile root
# value cannot defeat the floor.
_SYSTEMD_WATCHDOG_FLOOR = 30


def _enforce_watchdog_floor(default_config: Dict[str, Any]) -> None:
    # Take the value the loader would resolve: the ROOT spelling wins over the nested
    # gateway.* one (gateway/config.py from_dict), then run it through the SAME core
    # coercion the runtime uses so a bool, an out-of-range int (which the runtime would
    # coerce to 0 = supervision disabled) or a malformed string cannot slip below the
    # floor. Anything the coercion drops, or a valid value under 30, is raised to 30; a
    # valid value >= 30 is kept. Both spellings are popped so a surviving root value
    # cannot override the nested one written back.
    if "systemd_watchdog_seconds" in default_config:
        effective = default_config["systemd_watchdog_seconds"]
    else:
        effective = _get_dotted(default_config, "gateway.systemd_watchdog_seconds")
    coerced = coerce_systemd_watchdog_seconds(effective)
    value = coerced if coerced >= _SYSTEMD_WATCHDOG_FLOOR else _SYSTEMD_WATCHDOG_FLOOR
    default_config.pop("systemd_watchdog_seconds", None)
    nested = default_config.get("gateway")
    if isinstance(nested, dict):
        nested.pop("systemd_watchdog_seconds", None)
    _set_dotted(default_config, "gateway.systemd_watchdog_seconds", value)


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
    cron_jobs: List[Any] = []
    traits: Dict[str, Any] = {}
    config: Dict[str, Any] = {}
    mcp: Any = None

    def _apply(layer: Dict[str, Any]) -> None:
        nonlocal identity, traits, config, mcp
        if layer.get("identity") is not None:
            identity = layer["identity"]
        # The mcp block is a whole-declaration leaf-wins layer (like identity),
        # not an append/merge domain: a coworker's own server set replaces a
        # spine's rather than silently unioning across the extends chain.
        if layer.get("mcp") is not None:
            mcp = layer["mcp"]
        invariants.extend(layer.get("invariants") or [])
        context.extend(layer.get("context") or [])
        skills.extend(layer.get("skills") or [])
        workflows.extend(layer.get("workflows") or [])
        overlays.extend(layer.get("overlays") or [])
        # cron_jobs append across the chain (like skills/workflows), NOT deduped:
        # a duplicate job name is a render error resolved in _render_cron_jobs.
        # A non-list container fails closed rather than coercing to [] — a falsey
        # {}/""/0 would otherwise silently drop the fleet's gates on re-render.
        declared_cron_jobs = layer.get("cron_jobs", [])
        if not isinstance(declared_cron_jobs, list):
            raise CompositionError(
                f"{tname}: 'cron_jobs' must be a list, got {type(declared_cron_jobs).__name__}"
            )
        cron_jobs.extend(declared_cron_jobs)
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
        "cron_jobs": cron_jobs,
        "config": config,
        "mcp": mcp,
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


# Keys a cron_jobs declaration may carry. An unknown key fails closed: a
# misspelled 'script' would otherwise be silently dropped and render a pre-task
# gate that never gates.
_CRON_JOB_KEYS = frozenset(
    {
        "name", "schedule", "prompt", "script", "monitor", "monitor_url", "monitor_script",
        "no_agent",
        # skill-backed jobs (optionally Bot-Chat-delivered), mirroring native create_job
        "skill", "skills", "deliver",
    }
)


def _normalize_monitor(name: str, decl: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Resolve the model-facing ``monitor`` alias into the stored
    ``(monitor_script, monitor_url)`` pair, mirroring
    ``tools/cronjob_tools.py:_split_monitor_arg``: an ``http(s)://`` value is a
    URL source, anything else is a script path. An explicit ``monitor_url`` /
    ``monitor_script`` in the declaration passes through unchanged. Declaring
    the alias alongside an explicit field, or two explicit sources, is ambiguous
    and rejected. Each returned member is ``None`` or a non-empty string."""
    monitor = decl.get("monitor")
    explicit_script = decl.get("monitor_script")
    explicit_url = decl.get("monitor_url")
    if monitor is not None and (explicit_script is not None or explicit_url is not None):
        raise CompositionError(
            f"cron job {name!r}: declare either 'monitor' or an explicit "
            "monitor_url/monitor_script, not both"
        )
    if monitor is not None:
        if not isinstance(monitor, str):
            raise CompositionError(f"cron job {name!r}: 'monitor' must be a string")
        value = monitor.strip()
        if not value:
            return None, None
        if value.lower().startswith(("http://", "https://")):
            return None, value
        return value, None

    def _clean(raw: Any, field: str) -> Optional[str]:
        if raw is None:
            return None
        if not isinstance(raw, str):
            raise CompositionError(f"cron job {name!r}: {field!r} must be a string")
        return raw.strip() or None

    return _clean(explicit_script, "monitor_script"), _clean(explicit_url, "monitor_url")


def _build_cron_job(profile: str, decl: Any) -> Dict[str, Any]:
    """Render one declared cron job into the persisted record ``cron.jobs`` loads.

    A deterministic ``id`` (uuid5 of profile+name, never uuid4) and the parsed
    dict ``schedule`` keep an unchanged distribution byte-identical on re-render;
    no now-based field is written (the scheduler's due-scan recovers
    ``next_run_at`` for cron/interval jobs). Fail-closed on a malformed
    declaration, and execution-mode invariants mirror
    ``cron/jobs.py:_validate_job_mode_invariants`` so a rendered job can never be
    a shape the runtime would silently auto-pause or mis-gate."""
    from cron.jobs import _normalize_skill_list, compute_next_run, parse_schedule

    if not isinstance(decl, dict):
        raise CompositionError(
            f"{profile}: each cron_jobs entry must be a mapping, got {type(decl).__name__}"
        )
    unknown = set(decl) - _CRON_JOB_KEYS
    if unknown:
        raise CompositionError(
            f"{profile}: cron job has unknown key(s): "
            f"{', '.join(sorted(repr(k) for k in unknown))}; "
            f"supported: {', '.join(sorted(_CRON_JOB_KEYS))}"
        )
    name = decl.get("name")
    if not isinstance(name, str) or not name.strip():
        raise CompositionError(
            f"{profile}: cron job 'name' must be a non-empty string, got {name!r}"
        )
    raw_schedule = decl.get("schedule")
    if not isinstance(raw_schedule, str) or not raw_schedule.strip():
        raise CompositionError(
            f"{profile}: cron job {name!r} 'schedule' must be a non-empty string"
        )
    raw_schedule = raw_schedule.strip()
    # A relative one-shot ("in 30m") parses to a run_at computed from the current
    # time (cron/jobs.py:1090-1103), which is non-deterministic across renders; a
    # git-versioned distribution carries only recurring or absolute schedules.
    if raw_schedule.lower().startswith("in "):
        raise CompositionError(
            f"{profile}: cron job {name!r}: relative one-shot schedule {raw_schedule!r} is "
            "not deterministic in a rendered distribution (its run_at is computed from "
            "render time); use a recurring cron/interval schedule"
        )
    try:
        schedule = parse_schedule(raw_schedule)
    except ValueError as exc:
        raise CompositionError(
            f"{profile}: cron job {name!r} has an invalid schedule {raw_schedule!r}: {exc}"
        ) from exc
    if schedule.get("kind") == "once":
        # An absolute one-shot must be timezone-qualified: parse_schedule anchors
        # a naive timestamp to the compositor's active timezone, so its run_at
        # would differ across hosts — breaking the host-independent render.
        try:
            declared_dt = datetime.fromisoformat(raw_schedule.replace("Z", "+00:00"))
        except ValueError:
            declared_dt = None
        if declared_dt is None or declared_dt.tzinfo is None:
            raise CompositionError(
                f"{profile}: cron job {name!r}: absolute one-shot schedule {raw_schedule!r} must "
                "include a timezone offset (or 'Z') — a naive timestamp is anchored to the "
                "compositor's timezone and would not render identically across hosts"
            )
        # Native create_job rejects a one-shot whose run_at is already past the
        # scheduler's grace window: compute_next_run returns None and the due-scan
        # would never fire it, leaving a scheduled-but-dead record
        # (cron/jobs.py:2378-2390). Mirror that check so the render cannot admit a
        # job the runtime refuses. A future one-shot returns its fixed declared
        # run_at, so accepted output stays byte-identical across renders.
        if compute_next_run(schedule) is None:
            raise CompositionError(
                f"{profile}: cron job {name!r}: absolute one-shot schedule {raw_schedule!r} "
                "is already in the past (outside the scheduler's grace window) and would "
                "never fire; native create_job rejects it"
            )

    script = decl.get("script")
    if script is not None:
        if not isinstance(script, str) or not script.strip():
            raise CompositionError(
                f"{profile}: cron job {name!r} 'script' must be a non-empty string"
            )
        script = script.strip()
    prompt = decl.get("prompt")
    if prompt is not None and not isinstance(prompt, str):
        raise CompositionError(
            f"{profile}: cron job {name!r} 'prompt' must be a string, got {type(prompt).__name__}"
        )
    has_prompt = isinstance(prompt, str) and bool(prompt.strip())
    # skill-backed job (optionally Bot-Chat-delivered): mirror native create_job's
    # persisted shape (_normalize_skill_list cron/jobs.py:500-514; skills/skill
    # :2395-2397; deliver :2432) so a rendered skill-backed cron loads and fires like a
    # runtime one. Validate on KEY PRESENCE and fail closed: a declared-but-null or blank
    # value is an operator error, not "absent" — rejecting it (rather than letting a
    # value-based .get() and _normalize_skill_list silently drop it) keeps the render honest.
    raw_skill = None
    if "skill" in decl:
        raw_skill = decl["skill"]
        if not isinstance(raw_skill, str) or not raw_skill.strip():
            raise CompositionError(
                f"{profile}: cron job {name!r}: 'skill' must be a non-empty string, got {raw_skill!r}"
            )
    raw_skills = None
    if "skills" in decl:
        raw_skills = decl["skills"]
        if isinstance(raw_skills, str):
            if not raw_skills.strip():
                raise CompositionError(
                    f"{profile}: cron job {name!r}: 'skills' must be a non-empty string or a list of non-empty strings"
                )
        elif isinstance(raw_skills, list):
            if not raw_skills:
                raise CompositionError(
                    f"{profile}: cron job {name!r}: 'skills' must be a non-empty string or a non-empty list of non-empty strings"
                )
            for item in raw_skills:
                if not isinstance(item, str) or not item.strip():
                    raise CompositionError(
                        f"{profile}: cron job {name!r}: every 'skills' entry must be a non-empty string, got {item!r}"
                    )
        else:
            raise CompositionError(
                f"{profile}: cron job {name!r}: 'skills' must be a string or a list of strings, "
                f"got {type(raw_skills).__name__}"
            )
    skills = _normalize_skill_list(raw_skill, raw_skills)
    deliver = None
    if "deliver" in decl:
        # validate the shape but store verbatim — native create_job persists deliver
        # exactly as given (cron/jobs.py:2432) and applies no strip.
        deliver = decl["deliver"]
        if not isinstance(deliver, str) or not deliver.strip():
            raise CompositionError(
                f"{profile}: cron job {name!r}: 'deliver' must be a non-empty string, got {deliver!r}"
            )
    # A skill-backed agent job is a valid payload on its own — the skill drives the turn —
    # matching native create_job, which accepts skills with no prompt/script.
    if not (has_prompt or script or skills):
        raise CompositionError(
            f"{profile}: cron job {name!r} needs a prompt, a script, or a skill (an empty payload has nothing to run)"
        )
    monitor_script, monitor_url = _normalize_monitor(name, decl)
    # 'false'/'0' are truthy strings, so a bare bool() would flip an intended
    # off to on; require an actual boolean (fail closed).
    raw_no_agent = decl.get("no_agent", False)
    if not isinstance(raw_no_agent, bool):
        raise CompositionError(
            f"{profile}: cron job {name!r}: 'no_agent' must be a boolean, got {type(raw_no_agent).__name__}"
        )
    no_agent = raw_no_agent

    # Execution-mode invariants (cron/jobs.py:2192-2204): a rendered job must be a
    # shape the runtime accepts, else it is silently auto-paused or mis-gated.
    if monitor_script and monitor_url:
        raise CompositionError(
            f"{profile}: cron job {name!r}: monitor_script and monitor_url are mutually exclusive"
        )
    if (monitor_script or monitor_url) and no_agent:
        raise CompositionError(
            f"{profile}: cron job {name!r}: a monitor gate cannot be combined with no_agent "
            "(a monitor job suppresses or wakes the agent)"
        )
    if no_agent and not script:
        raise CompositionError(
            f"{profile}: cron job {name!r}: no_agent requires a script (the script is the job)"
        )

    job: Dict[str, Any] = {
        "id": uuid.uuid5(uuid.NAMESPACE_URL, f"{_SELF_PLUGIN}:{profile}:{name}").hex[:12],
        "name": name,
        "schedule": schedule,
    }
    # A one-shot carries a finite repeat budget, matching native create_job
    # (cron/jobs.py:2307-2309); a recurring job defaults to forever when omitted.
    if schedule.get("kind") == "once":
        job["repeat"] = {"times": 1, "completed": 0}
    if has_prompt:
        job["prompt"] = prompt
    if script:
        job["script"] = script
    if monitor_script:
        job["monitor_script"] = monitor_script
    if monitor_url:
        job["monitor_url"] = monitor_url
    if no_agent:
        job["no_agent"] = True
    # Emit skills/deliver only when declared, so a job without them renders byte-identically
    # (an unchanged distribution must re-render identically). skill is the legacy singular
    # alias native create_job also persists (cron/jobs.py:2395-2397).
    if skills:
        job["skills"] = skills
        job["skill"] = skills[0]
    if deliver:
        job["deliver"] = deliver
    return job


def _render_cron_jobs(cron_dir: Path, profile: str, cron_jobs: List[Any]) -> None:
    """Serialize a coworker type's resolved cron_jobs to ``<profile>/cron/jobs.json``
    in the canonical ``{"jobs": [...]}`` shape ``load_jobs`` consumes. A duplicate
    job name across the resolved extends chain is a render error — job identity
    must be unambiguous, and names are compared case-insensitively because the
    runtime resolves a job reference that way (``cron/jobs.py`` ``resolve_job_ref``
    lower-cases both sides and raises ``AmbiguousJobReference`` on >1 match). The
    file is written unconditionally (an empty declaration
    renders ``{"jobs": []}``) so re-rendering into a persistent, git-tracked output
    tree clears a job removed from the spec instead of leaving a stale record that
    ``profile update`` would then redeploy."""
    jobs: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for decl in cron_jobs:
        job = _build_cron_job(profile, decl)
        name_key = job["name"].lower()
        if name_key in seen:
            raise CompositionError(
                f"{profile}: duplicate cron job name {job['name']!r} across the extends chain "
                "(names are matched case-insensitively by the runtime)"
            )
        seen.add(name_key)
        jobs.append(job)
    (cron_dir / "jobs.json").write_text(
        json.dumps({"jobs": jobs}, indent=2) + "\n", encoding="utf-8"
    )


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
    _render_cron_jobs(pdir / "cron", tname, resolved["cron_jobs"])

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


def _render_route_scripts(pdir: Path) -> None:
    """Materialize the webhook route scripts into the DEFAULT profile's scripts/.

    On install they land in $HERMES_HOME/scripts/, where the webhook adapter's
    resolver looks them up by the `script:` name declared on a route. They are
    inert unless a rendered route references them.
    """
    src_dir = Path(__file__).resolve().parent / "route_scripts"
    scripts_dir = pdir / "scripts"
    scripts_dir.mkdir(exist_ok=True)
    for script in _ROUTE_SCRIPTS:
        (scripts_dir / script).write_text(
            (src_dir / script).read_text(encoding="utf-8"), encoding="utf-8"
        )


def _render_default(pdir: Path, name: str, config: Dict[str, Any]) -> None:
    pdir.mkdir(parents=True, exist_ok=True)
    _write_soul(pdir / "SOUL.md", name, "Multiplexer profile for the Bot-Mode gateway.", [], [])
    _write_yaml(pdir / "config.yaml", config)
    _write_common(pdir)
    (pdir / "skills").mkdir(exist_ok=True)
    (pdir / "skill-bundles").mkdir(exist_ok=True)
    _render_route_scripts(pdir)
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


# --- GOV-F27: per-role MCP allow-list / server registry --------------------
# A role declares its MCP servers under ``types.<role>.mcp`` (a structured block,
# not a scalar trait). The render emits, per profile, a scoped ``mcp_servers``
# block (only that role's servers) with a fail-safe ``trust``/utility policy, a
# validated UNION on the DEFAULT/launch profile (the process-global multiplex
# registry the shared gateway discovers once at startup), and — into EVERY
# profile — the full profile-keyed ``mcp_scope`` the single nv-fleet-gates
# pre_tool_call predicate enforces at call time.

# ``${...}`` refs core resolves WITHOUT an env read (tools/mcp_tool.py:710-728
# _context_var_value). Exempt from the env-ref refusal because they never trigger
# the unscoped-secret read that zeroes all MCP at multiplex startup.
_MCP_CONTEXT_REFS = frozenset(
    {"userHome", "workspaceFolder", "workspaceFolderBasename", "pathSeparator", "/"}
)
_MCP_ENV_REF_RE = re.compile(r"\$\{([^}]*)\}")
_MCP_GLOB_CHARS = ("*", "?", "[", "]")
# Transport strings by connection class: tools/mcp_tool.py:3656 selects SSE for a
# url server; a url server otherwise speaks Streamable HTTP; a command server is
# stdio. Only a declared transport whose class CONTRADICTS the connection is a
# render error — an unknown/future value is preserved verbatim, not rejected.
_MCP_REMOTE_TRANSPORTS = frozenset(
    {"sse", "http", "https", "streamable-http", "streamable_http", "streamable", "ws", "websocket"}
)
_MCP_STDIO_TRANSPORTS = frozenset({"stdio"})
# Utility tool names core grants when resources/prompts are enabled
# (tools/mcp_tool.py:6905-6963); listed in a role's scope ONLY when that role
# grants the capability (else rendered false, closing the utility bypass).
_MCP_UTILITY_TOOLS: Dict[str, Tuple[str, ...]] = {
    "resources": ("list_resources", "read_resource"),
    "prompts": ("list_prompts", "get_prompt"),
}
# Declaration keys the render OWNS (translated into tools.*/trust); every other
# key is a connection field copied into the stanza verbatim (url/headers/command/
# args/env/transport/ssl_verify/…), so SSE/auth/TLS/lifecycle survive unchanged.
_MCP_MANAGED_KEYS = frozenset({"include", "resources", "prompts", "trust"})
_MCP_SCOPE_SETTINGS = "plugins.entries.nv-fleet-gates.settings"


def _mcp_sanitize(value: Any) -> str:
    # Verbatim replica of core sanitize_mcp_name_component (tools/mcp_tool.py:6841-6849)
    # so a rendered scope key equals the model-facing name the gateway registers.
    return re.sub(r"[^A-Za-z0-9_]", "_", str(value or ""))


def _mcp_prefixed(server: str, tool: str) -> str:
    return f"mcp__{_mcp_sanitize(server)}__{_mcp_sanitize(tool)}"


def _mcp_iter_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for sub in value.values():
            yield from _mcp_iter_strings(sub)
    elif isinstance(value, (list, tuple)):
        for sub in value:
            yield from _mcp_iter_strings(sub)


def _mcp_reject_env_refs(server: str, connection: Dict[str, Any]) -> None:
    from agent.secret_scope import _is_global_env

    for text in _mcp_iter_strings(connection):
        for match in _MCP_ENV_REF_RE.finditer(text):
            ref = match.group(1).strip()
            if ref in _MCP_CONTEXT_REFS:
                continue
            if ref.startswith("env:"):
                var = ref[len("env:"):].strip()
                if _is_global_env(var):
                    continue
                raise CompositionError(
                    f"mcp server {server!r}: non-global env-ref '${{env:{var}}}' in a connection "
                    "value; authenticated remote MCP uses an OneCLI-proxied URL (credential injected "
                    "at egress), not a startup secret read"
                )
            if _is_global_env(ref):
                continue
            raise CompositionError(
                f"mcp server {server!r}: non-global env-ref '${{{ref}}}' in a connection value; a "
                "non-global secret read raises at unscoped multiplex startup and zeroes ALL MCP"
            )


def _mcp_transport(server: str, decl: Dict[str, Any]) -> str:
    """Policy transport ('remote'|'stdio'); fail-closed on an ambiguous connection."""
    def _present(key: str) -> bool:
        if key not in decl or decl[key] is None:
            return False
        value = decl[key]
        if not isinstance(value, str) or not value.strip():
            raise CompositionError(f"mcp server {server!r}: {key!r} must be a non-empty string")
        return True

    has_url = _present("url")
    has_cmd = _present("command")
    if has_url and has_cmd:
        raise CompositionError(
            f"mcp server {server!r}: both 'url' and 'command' set — exactly one transport "
            "connection (url|command) is required"
        )
    if not has_url and not has_cmd:
        raise CompositionError(
            f"mcp server {server!r}: neither 'url' nor 'command' set — exactly one transport "
            "connection (url|command) is required"
        )
    policy = "remote" if has_url else "stdio"
    declared = decl.get("transport")
    if declared is not None:
        text = str(declared).strip().lower()
        if policy == "remote" and text in _MCP_STDIO_TRANSPORTS:
            raise CompositionError(
                f"mcp server {server!r}: declared transport {declared!r} disagrees with a url "
                "connection (remote)"
            )
        if policy == "stdio" and text in _MCP_REMOTE_TRANSPORTS:
            raise CompositionError(
                f"mcp server {server!r}: declared transport {declared!r} disagrees with a command "
                "connection (stdio)"
            )
    return policy


def _mcp_validate_include(profile: str, server: str, include_raw: Any) -> List[str]:
    # Missing/non-list include is refused, never forwarded: core treats an
    # inactive include filter as "register ALL tools" (tools/mcp_tool.py:7194),
    # a fail-open the exact-set contract cannot allow. include: [] is kept (core
    # registers nothing). isinstance is checked before any membership test so an
    # unhashable YAML value raises CompositionError, not a raw TypeError.
    if not isinstance(include_raw, list):
        raise CompositionError(
            f"{profile}: mcp server {server!r} 'include' must be a list of exact tool names "
            f"(present, list-valued), got {type(include_raw).__name__}"
        )
    out: List[str] = []
    for entry in include_raw:
        if not isinstance(entry, str) or not entry.strip():
            raise CompositionError(
                f"{profile}: mcp server {server!r} include entry must be a non-empty string, got {entry!r}"
            )
        if any(ch in entry for ch in _MCP_GLOB_CHARS):
            raise CompositionError(
                f"{profile}: mcp server {server!r} include entry {entry!r} contains glob metacharacters; "
                "an exact tool name is required (the veto's exact-name map cannot represent a glob)"
            )
        out.append(entry)
    return out


def _mcp_trust(value: Any) -> str:
    # Fail-safe: only an explicit trust: full survives as "full"; anything else
    # (absent, "trusted", misspelled) becomes "untrusted". Core defaults an ABSENT
    # trust to permissive "full" (_normalize_server_trust tools/mcp_tool.py:4615),
    # so writing untrusted explicitly is the non-vacuous enforcement.
    if isinstance(value, str) and value.strip().lower() == "full":
        return "full"
    return "untrusted"


def _mcp_server_stanza(server: str, decl: Dict[str, Any]) -> Tuple[Dict[str, Any], str, List[str], bool, bool]:
    transport = _mcp_transport(server, decl)
    connection = {k: v for k, v in decl.items() if k not in _MCP_MANAGED_KEYS}
    _mcp_reject_env_refs(server, connection)
    include = _mcp_validate_include(server, server, decl.get("include"))
    resources = decl.get("resources") is True
    prompts = decl.get("prompts") is True
    trust = _mcp_trust(decl.get("trust"))
    stanza = dict(connection)
    stanza["tools"] = {"include": include, "resources": resources, "prompts": prompts}
    stanza["trust"] = trust
    return stanza, transport, include, resources, prompts


def _mcp_scope_entries(include: List[str], resources: bool, prompts: bool) -> List[Tuple[str, str]]:
    entries: List[Tuple[str, str]] = [("native", tool) for tool in include]
    if resources:
        entries.extend(("utility", tool) for tool in _MCP_UTILITY_TOOLS["resources"])
    if prompts:
        entries.extend(("utility", tool) for tool in _MCP_UTILITY_TOOLS["prompts"])
    return entries


def _mcp_connection_identity(stanza: Dict[str, Any]) -> Dict[str, Any]:
    # Identity = every emitted field except the four merged policy fields. Two
    # roles declaring the same server id must agree on it, else they must use
    # distinct ids (a shared registry key cannot hold two connections).
    return {k: v for k, v in stanza.items() if k not in ("tools", "trust")}


def _mcp_merge_into_union(union: Dict[str, Dict[str, Any]], server: str, stanza: Dict[str, Any]) -> None:
    identity = _mcp_connection_identity(stanza)
    if server not in union:
        union[server] = copy.deepcopy(stanza)
        return
    if _mcp_connection_identity(union[server]) != identity:
        raise CompositionError(
            f"mcp server {server!r}: divergent connection identity across roles (same server id, "
            "different connection) — use distinct server ids or align the connection"
        )
    merged = union[server]["tools"]
    for tool in stanza["tools"]["include"]:
        if tool not in merged["include"]:
            merged["include"].append(tool)
    merged["resources"] = merged["resources"] or stanza["tools"]["resources"]
    merged["prompts"] = merged["prompts"] or stanza["tools"]["prompts"]
    # trust: full only if EVERY declaration is full (any non-full → untrusted).
    if stanza["trust"] != "full":
        union[server]["trust"] = "untrusted"


def _mcp_write_scope(config: Dict[str, Any], full_scope: Dict[str, Dict[str, str]]) -> None:
    # Deep-navigate to the fleet-gates settings and REPLACE only mcp_scope (a
    # stale/inherited map is overwritten, never merged into — a leftover entry
    # would silently widen the boundary), preserving sibling settings.
    settings = config
    for part in _MCP_SCOPE_SETTINGS.split("."):
        settings = settings.setdefault(part, {})
        if not isinstance(settings, dict):
            raise CompositionError(
                f"cannot render mcp_scope: {_MCP_SCOPE_SETTINGS} is not a mapping in this profile"
            )
    settings["mcp_scope"] = copy.deepcopy(full_scope)


def _render_mcp_scope(
    resolved_by_type: Dict[str, Dict[str, Any]],
    default_config: Dict[str, Any],
    default_profile: str,
) -> None:
    """Emit per-role mcp_servers + the DEFAULT union + the profile-keyed mcp_scope."""
    # Strip any inherited/pre-existing mcp_servers so a no-MCP profile carries
    # ONLY its role's servers (atomic replace, matching mcp_scope below).
    default_config.pop("mcp_servers", None)
    for resolved in resolved_by_type.values():
        resolved["config"].pop("mcp_servers", None)

    role_servers: Dict[str, Dict[str, Any]] = {}
    role_scope: Dict[str, Dict[str, str]] = {}
    union: Dict[str, Dict[str, Any]] = {}
    sanitized_servers: Dict[str, str] = {}
    # Fleet-wide map from the full model-facing name to its (server, kind, tool)
    # owner. The mcp__<server>__<tool> delimiter is ambiguous when a component
    # contains "__" ((a, b__c) and (a__b, c) both yield mcp__a__b__c), and a
    # native tool can clash with a generated resource/prompt utility; either
    # would let one profile's scope key match another's registered tool, so a
    # divergent owner for the same key is refused fleet-wide.
    scope_owners: Dict[str, Tuple[str, str, str]] = {}

    for profile, resolved in resolved_by_type.items():
        mcp = resolved.get("mcp")
        servers_out: Dict[str, Any] = {}
        scope_out: Dict[str, str] = {}
        if mcp is not None:
            if not isinstance(mcp, dict):
                raise CompositionError(f"{profile}: 'mcp' must be a mapping of server-id -> stanza")
            for server, decl in mcp.items():
                if not isinstance(server, str) or not server.strip():
                    raise CompositionError(
                        f"{profile}: mcp server id must be a non-empty string, got {server!r}"
                    )
                if not isinstance(decl, dict):
                    raise CompositionError(
                        f"{profile}: mcp server {server!r} declaration must be a mapping"
                    )
                sanitized = _mcp_sanitize(server)
                prior = sanitized_servers.get(sanitized)
                if prior is not None and prior != server:
                    raise CompositionError(
                        f"mcp server sanitized-name collision: {prior!r} and {server!r} both sanitize "
                        f"to {sanitized!r}"
                    )
                sanitized_servers[sanitized] = server
                stanza, transport, include, resources, prompts = _mcp_server_stanza(server, decl)
                servers_out[server] = stanza
                for kind, tool in _mcp_scope_entries(include, resources, prompts):
                    key = _mcp_prefixed(server, tool)
                    owner = (server, kind, tool)
                    prior = scope_owners.get(key)
                    if prior is not None and prior != owner:
                        raise CompositionError(
                            f"mcp model-name collision: {key!r} maps to both {prior!r} and {owner!r} "
                            "(the mcp__server__tool delimiter is ambiguous across these declarations)"
                        )
                    scope_owners[key] = owner
                    if key in scope_out:
                        raise CompositionError(
                            f"mcp sanitized-name collision: {key!r} produced twice in profile {profile!r}"
                        )
                    scope_out[key] = transport
                _mcp_merge_into_union(union, server, stanza)
        role_servers[profile] = servers_out
        role_scope[profile] = scope_out

    # The full profile-keyed map, written into EVERY profile (ctx.get_config
    # resolves against the active profile home, so each profile must carry the
    # whole map; the predicate selects mcp_scope[_current_profile()]). The
    # DEFAULT/multiplexer profile is the registrar, not a caller → empty scope.
    full_scope: Dict[str, Dict[str, str]] = {p: role_scope.get(p, {}) for p in resolved_by_type}
    full_scope[default_profile] = {}

    for profile, resolved in resolved_by_type.items():
        cfg = resolved["config"]
        if role_servers.get(profile):
            cfg["mcp_servers"] = role_servers[profile]
        _mcp_write_scope(cfg, full_scope)
    if union:
        default_config["mcp_servers"] = union
    _mcp_write_scope(default_config, full_scope)


def compose(spec: str, out: str) -> Dict[str, str]:
    """Render each coworker type + the DEFAULT profile into ``out``.

    Returns ``{profile_name: rendered_dir}`` for every profile (the DEFAULT
    multiplexer plus one per coworker type in the spec).
    """
    spec_path = Path(spec)
    spec_dir = spec_path.parent
    data = load_spec(spec)

    # Resolve the sandbox substrate BEFORE any output dir / spine read, so an unknown
    # value fails closed naming the value with no partial fleet on disk. The
    # remote-ssh substrate's scope + container-field checks fail closed here too.
    descriptor = _resolve_substrate(data)
    _validate_sandbox_scope(data, descriptor)
    _reject_openshell_container_fields(data, descriptor)
    is_remote = descriptor.kind == "remote_ssh"

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

    # ISO-F15 session-driver seam, enforced as an all-or-nothing pre-pass: force
    # terminal.backend: docker + terminal.docker_shared_container_key: "" on EVERY
    # rendered profile (every coworker type AND the DEFAULT multiplexer) and reject
    # any other backend or a non-empty shared key. Validated + normalised here,
    # before the Phase-B write loop, so a violation on a later-resolved profile
    # (DEFAULT is resolved and written last) raises before out_root holds any
    # config.yaml — no partial fleet on disk.
    for driver_tname, driver_resolved in resolved_by_type.items():
        _reject_malformed_terminal(driver_resolved["config"], driver_tname)
        _enforce_session_driver(driver_resolved["config"], driver_tname, descriptor)
    _enforce_session_driver(default_config, default_profile, descriptor)

    # ISO-F14 egress lockdown, all-or-nothing: validate the egress block once, then
    # assert EVERY profile is egress-safe here — before Phase B writes anything — so
    # a collision on a late-resolved profile (DEFAULT is asserted last) raises with
    # no partial fleet on disk. _enforce_egress does the writing per profile in the
    # Phase-B loop below. An absent egress block leaves enforcement off, so dev/test
    # specs render unchanged.
    egress_block = data.get("egress")
    if is_remote:
        # OSH-F63: the remote-ssh substrate's egress is a per-profile `openshell
        # policy` file (not the container docker_* env), so an openshell fleet must
        # declare its egress; the container egress pipeline stays off (egress_params
        # None). fleet_name/keys_dir feed the per-profile ssh block + provision plan.
        if egress_block is None:
            raise CompositionError(
                "substrate 'openshell' requires an egress block (the per-profile "
                "openshell policy allow-set: proxy_addr, inference_route, ssh_control_path)")
        openshell_params = _validate_openshell_egress(egress_block)
        egress_params = None
        fleet_name = _safe_name("project", data.get("project", "fleet"))
        keys_dir = _openshell_keys_dir()
    else:
        egress_params = _validate_egress_spec(egress_block) if egress_block is not None else None
        openshell_params = None
        fleet_name = None
        keys_dir = None

    # Shared-learnings clone (MEM-F43) — enforced per coworker type below, never on
    # the DEFAULT/multiplexer gateway root. Mount composition (ISO-F13) owns each
    # coworker's docker_volumes. Both are resolved here, before the egress pre-pass,
    # so the pre-pass can project them onto a copy of every coworker; the Phase-B
    # write loop reuses the same values.
    shared_learnings_root = data.get("shared_learnings_root")
    workspace_root = data.get("workspace_root")
    install_surfaces = data.get("install_surfaces")

    if egress_params is not None:
        for eg_tname, eg_resolved in resolved_by_type.items():
            # A coworker's docker_volumes is composed by ISO-F13's _enforce_mount_composition
            # (plus the shared-learnings clone) in the write loop, so a spec-declared or a
            # composition-generated CA-target mount only surfaces there. Project both onto a
            # deep copy and assert against THAT, so such a mount is rejected before any profile
            # is written; the copy leaves the resolved config untouched for the write loop,
            # which composes it again.
            projected = copy.deepcopy(eg_resolved["config"])
            if shared_learnings_root:
                _enforce_shared_learnings(
                    projected, str(shared_learnings_root),
                    is_orchestrator=(eg_tname == orchestrator_profile),
                )
            _enforce_mount_composition(
                projected, eg_tname, workspace_root, install_surfaces,
                shared_learnings_root, is_orchestrator=(eg_tname == orchestrator_profile),
                descriptor=descriptor,
            )
            _assert_egress_safe(projected, egress_params, eg_tname, descriptor)
        # DEFAULT never goes through mount composition, so its raw config IS what is written.
        _assert_egress_safe(default_config, egress_params, default_profile, descriptor)

    session_flags = _resolve_session_mode(default_config, resolved_by_type)
    # Per-role MCP scope needs the whole fleet's declarations (per-role subsets,
    # the DEFAULT union, and the profile-keyed veto policy in every profile), so
    # it runs ONCE here — after Phase A resolve, before the per-profile writes.
    _render_mcp_scope(resolved_by_type, default_config, default_profile)

    # Phase B: render each profile, applying the fleet session mode after
    # retention and before the canonical-layout / port / route checks.
    rendered: Dict[str, str] = {}
    for tname, resolved in resolved_by_type.items():
        _inject_self_plugin(resolved["config"], orchestrator_profile)
        _enforce_retention(resolved["config"])
        # The shared-learnings clone and mount composition are container-substrate
        # writers of terminal.docker_volumes; the remote-ssh substrate has no local
        # container to bind into (container-mount fields are refused up front), so
        # both are skipped for it — no docker_* key is emitted for openshell.
        if shared_learnings_root and not is_remote:
            _enforce_shared_learnings(
                resolved["config"], str(shared_learnings_root),
                is_orchestrator=(tname == orchestrator_profile),
            )
        # The single, final owner of terminal.docker_volumes — runs after
        # _enforce_shared_learnings (the only earlier writer) so it composes the closed
        # policed set on top of the permitted clone mount.
        if not is_remote:
            _enforce_mount_composition(
                resolved["config"], tname, workspace_root, install_surfaces,
                shared_learnings_root, is_orchestrator=(tname == orchestrator_profile),
                descriptor=descriptor,
            )
        _enforce_memory_policy(resolved["config"])
        _enforce_curator(resolved["config"])
        _validate_approval_lists(resolved["config"], include_allowlist=True)
        _strip_fleet_uniform_approvals(resolved["config"])
        _enforce_deny_floor(resolved["config"])
        _render_engage(resolved["config"])
        _apply_session_mode(resolved["config"], session_flags, is_default=False)
        _require_canonical_platform_layout(resolved["config"])
        _forbid_coworker_port_binding(resolved["config"], tname)
        _forbid_coworker_webhook_routes(resolved["config"], tname)
        if egress_params is not None:
            _enforce_egress(resolved["config"], egress_params, tname, descriptor)
        # OSH-F63: the builtin-ssh terminal block must be written BEFORE the config
        # is serialized; the per-profile openshell policy is a separate file written
        # AFTER (so _render_coworker does not need to know about it).
        if is_remote:
            _enforce_openshell_ssh(resolved["config"], tname, fleet_name, keys_dir)
        pdir = out_root / tname
        _render_coworker(pdir, tname, resolved, skills_root, workflows_root, overlays_root)
        if is_remote:
            _enforce_openshell_policy(pdir, tname, openshell_params["allow"])
        rendered[tname] = str(pdir)

    _enforce_retention(default_config)
    _enforce_memory_policy(default_config)
    _enforce_curator(default_config)
    _validate_approval_lists(default_config, include_allowlist=False)
    _strip_fleet_uniform_approvals(default_config)
    _force_default_allowlist_empty(default_config)
    _strip_default_timezone(default_config)
    _enforce_deny_floor(default_config)
    _render_engage(default_config)
    _apply_session_mode(default_config, session_flags, is_default=True)
    _enforce_multiplex(default_config, roster)
    _enforce_watchdog_floor(default_config)
    _require_canonical_platform_layout(default_config)
    _validate_webhook_routes(default_config, served)
    if egress_params is not None:
        _enforce_egress(default_config, egress_params, default_profile, descriptor)
    ddir = out_root / default_profile
    _render_default(ddir, default_profile, default_config)
    rendered[default_profile] = str(ddir)

    # Emit the machine-wide managed-scope fragment after every profile validated
    # and rendered. It is NOT added to `rendered`: the mapping is profile-name ->
    # distribution dir, and the fragment is a machine-wide config file, not a
    # profile (onboarding installs only the coworker TYPE profiles).
    managed_dir = out_root / _MANAGED_FRAGMENT_DIR
    managed_dir.mkdir(parents=True, exist_ok=True)
    # FLEET-F62: the security-critical fleet role map lives at the operator-owned
    # managed scope (nv-fleet-gates reads it there), so a worker cannot rewrite its
    # own role via per-profile config.
    profile_roles_map = {
        tname: ("orchestrator" if tname == orchestrator_profile else "worker")
        for tname in roster
    }
    # Derive the managed authorization map from the same helper used for
    # terminal.ssh_host so the two values cannot drift.
    expected_ssh_host_map = (
        {tname: _openshell_sandbox_name(fleet_name, tname) for tname in roster}
        if is_remote else None
    )
    managed_fragment = _build_managed_fragment(profile_roles_map, expected_ssh_host_map)
    if egress_params is not None:
        # ISO-F14: the managed-scope layer deep-merges managed-wins onto every
        # profile at load, so proxy.enabled:false here forces the egress topology
        # un-overridably even if a per-profile value later diverged.
        _set_dotted(managed_fragment, "proxy.enabled", False)
    _write_yaml(managed_dir / "config.yaml", managed_fragment)

    return rendered
