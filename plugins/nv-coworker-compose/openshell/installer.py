"""Two-phase OpenShell fleet installer — a pure, state-aware planner plus its executor.

Targets an EXISTING Hermes sandbox: the default profile is edited in place (never
reinstalled), the substrate is flipped to ``openshell``, and repeated runs are
idempotent. Both the ``--dry-run`` transcript and the real run are produced from
``build_plan`` and executed step-for-step, so they cannot diverge.

Pure planner surface (no side effects):
  - ``build_plan(home_state, spec_path, ref) -> list[Step]``
  - ``default_config_diff(existing_config, spec, *, backup_present=False)``
  - ``apply_config_diff(config, diff)`` — deep merge, preserves unrelated keys
  - ``managed_config_diff(existing_managed, rendered_managed)`` — deep merge of the
    OSH-owned managed settings only

``build_plan`` accepts a ``profiles`` list of installed role names with an optional
``profile_digests`` map for drift; a listed role without drift info is present+unchanged.
"""

from __future__ import annotations

import argparse
import copy
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml
except Exception:  # pragma: no cover - yaml is a Hermes runtime dependency
    yaml = None  # type: ignore

FORK_REPO = "slang-coworkers/hermes-agent"
_PLUGIN_SRC = FORK_REPO + "/plugins/{name}"
# The three OSH-owned plugins whose entries/enabled the launch-profile edit owns.
# Everything else in the default config is left exactly as the operator had it.
_OSH_PLUGINS = ("nv-coworker-compose", "nv-fleet-gates", "podman-onecli")
# OSH-owned managed paths: the veto's role map + the openshell ssh-host anchor.
_FLEET_GATES = "nv-fleet-gates"
_BACKUP_SUFFIX = ".osh-f64.bak"
SHA40 = re.compile(r"^[0-9a-fA-F]{40}$")


class Step:
    """One ordered install step. ``command`` is a hermes/openshell argv vector (executed
    via subprocess) or a ``#``-prefixed action line (backup / managed write / default
    edit / room create, executed in-process). ``tag`` classifies it for the executor;
    ``data`` carries a structured payload (e.g. a room's id/name/members) the executor
    needs beyond the human-readable action line.

    A plain class (not a dataclass) so it imports cleanly via
    ``importlib.util.module_from_spec`` without ``sys.modules`` registration."""

    __slots__ = ("command", "tag", "data")

    def __init__(self, command: Any, tag: str, data: Any = None) -> None:
        self.command = command
        self.tag = tag
        self.data = data

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Step({self.command!r}, {self.tag!r})"

    def __eq__(self, other: object) -> bool:
        return (isinstance(other, Step) and self.command == other.command
                and self.tag == other.tag and self.data == other.data)


def _require_home() -> Path:
    """The active HERMES_HOME for this install. Requires it to be set EXPLICITLY (env or a
    bound override) — the installer targets a specific sandbox home, so it must fail closed
    rather than fall back to ``get_hermes_home()``'s platform default (which would silently
    edit the caller's real ``~/.hermes``) or resolve paths to ``/`` / the cwd. When set, it
    resolves through Hermes's profile-aware resolver."""
    if os.environ.get("HERMES_HOME", "").strip():
        try:
            from hermes_constants import get_hermes_home
            return get_hermes_home()
        except Exception:
            return Path(os.environ["HERMES_HOME"].strip())
    try:
        from hermes_constants import get_hermes_home_override
        override = get_hermes_home_override()
        if override:
            return Path(override)
    except Exception:
        pass
    raise ValueError("HERMES_HOME is not set; the installer refuses to resolve paths to the wrong home")


def _managed_dir() -> Optional[str]:
    """The managed-config dir, via Hermes's resolver (``HERMES_MANAGED_DIR`` then the
    POSIX ``/etc/hermes`` default, existence-gated) when importable, else the env var.
    Returns None when none is resolvable — callers that must write fail closed."""
    try:
        from hermes_cli.managed_scope import get_managed_dir
        resolved = get_managed_dir()
        if resolved is not None:
            return str(resolved)
    except Exception:
        pass
    return os.environ.get("HERMES_MANAGED_DIR", "").strip() or None


def load_spec(spec_path: Any) -> Dict[str, Any]:
    assert yaml is not None
    return yaml.safe_load(Path(spec_path).read_text(encoding="utf-8")) or {}


def _coworker_roles(spec: Dict[str, Any]) -> List[str]:
    """Served coworker roles, in spec-declared order (never the default profile)."""
    types = spec.get("types") or {}
    default_profile = spec.get("default_profile", "default")
    return [name for name in types if name != default_profile]


def _require_openshell(spec: Dict[str, Any]) -> None:
    """Refuse a non-openshell spec before any state collection or install, so a wrong spec
    cannot install/enable plugins and only then fail (the entry points call this first)."""
    substrate = spec.get("substrate")
    if substrate != "openshell":
        raise ValueError(f"install-openshell requires substrate: openshell (spec has {substrate!r})")


def _spine_enabled(spec: Dict[str, Any], spec_path: Any) -> List[str]:
    """plugins.enabled as the RENDER sees it — resolved from the referenced spine's
    ``config`` block (the canonical source; there is no top-level mirror)."""
    enabled: List[str] = []
    if spec_path is None:
        return enabled
    spines = spec.get("spines") or {}
    base = Path(spec_path).parent
    for entry in spines.values():
        src = (entry or {}).get("source")
        if not src:
            continue
        spine = yaml.safe_load((base / src).read_text(encoding="utf-8")) or {}
        for name in ((spine.get("config") or {}).get("plugins") or {}).get("enabled") or []:
            if name not in enabled:
                enabled.append(name)
    return enabled


def enabled_plugins(spec: Dict[str, Any], spec_path: Any) -> List[str]:
    """The installer-visible plugin set: the SPINE's ``config.plugins.enabled`` — the
    canonical source compose renders from. There is no top-level mirror. Fails closed if
    the spine declares none; if a top-level mirror is present anyway it must not diverge,
    so the installed set can never silently differ from the set the render enables."""
    spine = _spine_enabled(spec, spec_path)
    if not spine:
        raise ValueError("spine config.plugins.enabled is empty; the installer has no plugin set to bootstrap")
    top = list((spec.get("plugins") or {}).get("enabled") or [])
    if top and set(top) != set(spine):
        raise ValueError(
            f"top-level plugins.enabled {top} diverges from the spine-resolved set "
            f"{spine}; the installer would install a different plugin set than the render enables"
        )
    return spine


def _spine_entries(spec: Dict[str, Any], spec_path: Any) -> Dict[str, Any]:
    """The ``plugins.entries`` the render merges from the referenced spine's ``config``.
    Seeds the in-place default-profile edit so a setting the veto needs in the default
    home (e.g. ``nv-fleet-gates.settings.edges_db_path``, which `hermes wire add` reads
    with no default) is present there, not only in the rendered coworker profiles."""
    entries: Dict[str, Any] = {}
    if spec_path is None:
        return entries
    base = Path(spec_path).parent
    for entry in (spec.get("spines") or {}).values():
        src = (entry or {}).get("source")
        if not src:
            continue
        spine = yaml.safe_load((base / src).read_text(encoding="utf-8")) or {}
        for name, e in (((spine.get("config") or {}).get("plugins") or {}).get("entries") or {}).items():
            if isinstance(e, dict):
                entries[name] = e
    return entries


def _get_dotted(cfg: Dict[str, Any], dotted: str) -> Tuple[bool, Any]:
    cur: Any = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def _set_dotted(cfg: Dict[str, Any], dotted: str, value: Any) -> None:
    cur = cfg
    parts = dotted.split(".")
    for part in parts[:-1]:
        if part not in cur:
            cur[part] = {}
        elif not isinstance(cur[part], dict):
            # An operator-owned non-mapping value (scalar, list, or explicit YAML null) sits
            # where this edit needs a mapping — fail closed rather than discard it, so a
            # present key is never silently replaced (the preservation contract).
            raise ValueError(
                f"refusing to overwrite the operator's non-mapping value at {part!r} "
                f"while setting {dotted!r}; resolve the conflict by hand, then re-run"
            )
        cur = cur[part]
    cur[parts[-1]] = value


def _desired_default(
    existing: Dict[str, Any], spec: Dict[str, Any], enabled: List[str], entries: Dict[str, Any]
) -> Dict[str, Any]:
    """The OSH-owned default-config values, computed from the spec + the resolved plugin
    set + the spine's plugin entries. ``plugins.enabled`` is a UNION with whatever the
    operator already had, so an unrelated enabled plugin is never dropped; each enabled
    plugin's spine ``settings`` are set under ``plugins.entries.<name>.settings`` so the
    default home carries what the veto/OneCLI need (e.g. ``edges_db_path``) — sibling keys
    on those entries and unrelated entries are left untouched."""
    roles = _coworker_roles(spec)
    have, cur_enabled = _get_dotted(existing, "plugins.enabled")
    merged = list(cur_enabled) if have and isinstance(cur_enabled, list) else []
    for name in enabled:
        if name not in merged:
            merged.append(name)
    desired: Dict[str, Any] = {
        "gateway.multiplex_profiles": True,
        "gateway.multiplex_profile_allowlist": roles,
        "plugins.enabled": merged,
    }
    for name in enabled:
        settings = ((entries.get(name) or {}).get("settings")) if isinstance(entries.get(name), dict) else None
        if isinstance(settings, dict) and settings:
            _, cur = _get_dotted(existing, f"plugins.entries.{name}.settings")
            base = cur if isinstance(cur, dict) else {}
            # Deep-merge onto any existing settings so an unrelated operator key on the
            # same entry survives (never a wholesale replace).
            desired[f"plugins.entries.{name}.settings"] = _deep_merge(base, settings)
    return desired


def default_config_diff(
    existing: Dict[str, Any], spec: Any, *, backup_present: bool = False
) -> Tuple[str, Dict[str, Any], bool]:
    """Compute the OSH-owned default-config edit for an EXISTING default profile.

    Returns ``(backup_path, diff, backup_required)``. ``diff`` maps a dotted key to its
    desired value for every OSH-owned key that is absent or differs; unrelated keys are
    never touched. ``backup_required`` is True only when there is something to change
    AND no backup already exists — so a second run of an installed home takes no second
    backup and plans no edit."""
    data = spec if isinstance(spec, dict) else load_spec(spec)
    spec_path = None if isinstance(spec, dict) else spec
    desired = _desired_default(
        existing, data, enabled_plugins(data, spec_path), _spine_entries(data, spec_path)
    )
    diff: Dict[str, Any] = {}
    for dotted, want in desired.items():
        have, cur = _get_dotted(existing, dotted)
        if not have or cur != want:
            diff[dotted] = want
    backup_path = os.path.join(str(_require_home()), "config.yaml" + _BACKUP_SUFFIX)
    backup_required = bool(diff) and not backup_present
    return backup_path, diff, backup_required


def apply_config_diff(config: Dict[str, Any], diff: Dict[str, Any]) -> Dict[str, Any]:
    """Return a deep copy of ``config`` with the dotted-key ``diff`` applied. Unrelated
    keys are preserved verbatim (never clobbered)."""
    out = copy.deepcopy(config) if isinstance(config, dict) else {}
    for dotted, value in diff.items():
        _set_dotted(out, dotted, copy.deepcopy(value))
    return out


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base) if isinstance(base, dict) else {}
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def managed_config_diff(
    existing_managed: Dict[str, Any], rendered_managed: Dict[str, Any]
) -> Dict[str, Any]:
    """Deep-merge ONLY the OSH-owned managed paths (the ``nv-fleet-gates`` settings the
    render emits: ``profile_roles`` + ``expected_ssh_host``) onto the existing managed
    config, preserving any unrelated machine policy the operator has there."""
    owned: Dict[str, Any] = {}
    settings = (
        ((rendered_managed.get("plugins") or {}).get("entries") or {}).get(_FLEET_GATES) or {}
    ).get("settings") or {}
    if settings:
        owned = {"plugins": {"entries": {_FLEET_GATES: {"settings": settings}}}}
    return _deep_merge(existing_managed or {}, owned)


def _plugin_step(prefix: List[str], name: str, ref: str, state: Optional[Dict[str, Any]]) -> Optional[Step]:
    """One state-aware plugin step. ``state`` is ``{ref, enabled}`` for an already
    installed plugin, or None if absent. Returns None when nothing is needed (present +
    enabled at the pinned ref), so a re-run of an installed home plans no plugin work."""
    src = _PLUGIN_SRC.format(name=name)
    if state is None:
        return Step(prefix + ["plugins", "install", src, "--ref", ref, "--enable"], "plugin_install")
    if state.get("ref") != ref:
        return Step(prefix + ["plugins", "install", src, "--ref", ref, "--force", "--enable"], "plugin_force")
    if not state.get("enabled", False):
        return Step(prefix + ["plugins", "enable", name], "plugin_enable")
    return None


def _snapshot_step(plugins_dir: str, name: str) -> Step:
    """Snapshot a plugin's directory + the shared install-metadata sidecar before a
    ``--force`` reinstall overwrites them, so a rollback can restore the prior version."""
    return Step(
        f"# snapshot {plugins_dir}/{name} + {plugins_dir}/.install-metadata.json before reinstall",
        "plugin_snapshot",
        data={"plugin_dir": f"{plugins_dir}/{name}", "sidecar": f"{plugins_dir}/.install-metadata.json"},
    )


def _render_out_dir() -> str:
    """Deterministic, home-relative staging dir for the compose output — printed in the
    dry-run transcript but never created by it, so two runs are byte-identical."""
    return os.path.join(str(_require_home()), ".osh-f64", "render")


def _profile_drifted(role: str, digests: Dict[str, Any]) -> bool:
    d = digests.get(role) or {}
    cur, want = d.get("current"), d.get("desired")
    return cur is not None and want is not None and cur != want


def build_plan(home_state: Dict[str, Any], spec_path: Any, ref: str) -> List[Step]:
    """The full ordered, state-aware install plan. Backups come FIRST (before any
    ``plugins install/enable`` rewrites config.yaml). A fully-installed home plans no
    mutating step; ``hermes gateway restart`` is emitted only when something changed.

    ``home_state`` gives ``profiles`` (a list of installed role names) and
    ``managed_installed`` (a bool), with an optional ``profile_digests`` map for drift; a
    listed profile with no drift info is present + unchanged.
    """
    spec = load_spec(spec_path)
    plugins = enabled_plugins(spec, spec_path)
    roles = _coworker_roles(spec)
    home = str(_require_home())
    managed_dir = _managed_dir() or ""
    out = _render_out_dir()

    default_config = home_state.get("default_config") or {}
    installed = home_state.get("plugins") or {}
    profile_plugins = home_state.get("profile_plugins") or {}
    installed_profiles = list(home_state.get("profiles") or [])
    profile_digests = home_state.get("profile_digests") or {}
    managed_installed = bool(home_state.get("managed_installed"))
    installed_wires = home_state.get("wires")
    installed_rooms = home_state.get("rooms")
    backup_present = bool(home_state.get("backup_present"))

    steps: List[Step] = []
    mutated = False

    # Step 0 — snapshot the ORIGINAL config(s) BEFORE any mutation: a later `plugins
    # install --enable` rewrites config.yaml, and the managed write overwrites the veto
    # fragment. One backup step; its executor snapshots each file that exists and has no
    # snapshot yet, so a MANAGED-ONLY change (default diff empty) is still backed up.
    _, diff, backup_required = default_config_diff(default_config, spec_path, backup_present=backup_present)
    managed_cfg = Path(managed_dir, "config.yaml") if managed_dir else None
    managed_backup_needed = (
        not managed_installed and managed_cfg is not None and managed_cfg.exists()
        and not managed_cfg.with_suffix(managed_cfg.suffix + _BACKUP_SUFFIX).exists()
    )
    if backup_required or managed_backup_needed:
        steps.append(Step(f"# backup {home}/config.yaml (and the managed fragment) before any mutation", "backup"))
        mutated = True

    # Phase A — bootstrap: install the port's plugins into the default home.
    for name in plugins:
        step = _plugin_step(["hermes"], name, ref, installed.get(name))
        if step:
            if step.tag == "plugin_force":
                steps.append(_snapshot_step(f"{home}/plugins", name))
            steps.append(step)
            mutated = True

    # Phase B — compose the fleet under substrate: openshell. `--provision-dry-run` renders
    # to --out AND prints the deterministic OpenShell provision plan on stdout.
    steps.append(Step(["hermes", "coworker", "compose", str(spec_path), "--out", out, "--provision-dry-run"], "compose"))

    # OpenShell worker-sandbox provisioning — SETUP prefix only (one sandbox create, one
    # policy set, one sandbox ssh-config per served coworker). The teardown suffix (sandbox
    # delete / policy delete) is reserved for rollback and never appears in the install
    # plan. Sandbox names mirror compose's <project>-<role> ssh_host. Skipped for a role
    # whose profile is already present.
    project = spec.get("project") or "fleet"
    image = (spec.get("egress") or {}).get("sandbox_image") or "localhost/hermes-openshell-sandbox:pinned"
    for role in roles:
        if role in installed_profiles:
            continue
        sandbox = f"{project}-{role}"
        # The rendered policy compose writes at <out>/<role>/policy-<role>.yaml — the full
        # path, so openshell finds it (compose's own dry-run plan uses the bare name).
        policy = f"{out}/{role}/policy-{role}.yaml"
        steps.append(Step(["openshell", "sandbox", "create", "--name", sandbox, "--from", image, "--policy", policy], "provision_create"))
        steps.append(Step(["openshell", "policy", "set", policy], "provision_policy"))
        steps.append(Step(["openshell", "sandbox", "ssh-config", sandbox], "provision_sshconfig"))

    # Install the five coworker profiles FROM the rendered distributions (never default).
    for role in roles:
        if role not in installed_profiles:
            steps.append(Step(["hermes", "profile", "install", f"{out}/{role}", "--name", role, "-y"], "profile_install"))
            mutated = True
        elif _profile_drifted(role, profile_digests):
            steps.append(Step(["hermes", "profile", "install", f"{out}/{role}", "--name", role, "--force", "-y"], "profile_force"))
            mutated = True

    # Per served profile x per plugin — distributions exclude plugins, so each served
    # profile needs the veto + compose plugins installed under its own home.
    for role in roles:
        scope = profile_plugins.get(role) or {}
        for name in plugins:
            step = _plugin_step(["hermes", "-p", role], name, ref, scope.get(name))
            if step:
                if step.tag == "plugin_force":
                    steps.append(_snapshot_step(f"{home}/profiles/{role}/plugins", name))
                steps.append(step)
                mutated = True

    # Managed fragment (profile_roles + expected_ssh_host) as a SEPARATE file.
    if not managed_installed:
        steps.append(Step(f"# write managed fragment (profile_roles, expected_ssh_host) -> {managed_dir}/config.yaml", "managed_write"))
        mutated = True

    # Non-managed default edit (already snapshotted in step 0). Prints the diff KEYS
    # (gateway.multiplex_*, plugins.enabled, plugins.entries.<name>.settings).
    if diff:
        keys = ", ".join(sorted(diff))
        steps.append(Step(f"# edit default config {home}/config.yaml: set {keys}", "default_edit"))
        mutated = True

    # Fleet rooms — one groups.create per declared room (the onboarding groups.create path,
    # __init__.py:758-774; not a compose output), each naming its configured name + members.
    # Emitted after compose, before restart; skipped for a room already present.
    if installed_rooms != "all":
        for rid, rmeta in (spec.get("rooms") or {}).items():
            if isinstance(installed_rooms, list) and rid in installed_rooms:
                continue
            rmeta = rmeta or {}
            name = rmeta.get("name", rid)
            members = rmeta.get("members") or []
            steps.append(Step(
                f"# groups.create room {rid} name={name!r} members=[{', '.join(members)}] "
                "(onboarding groups.create path, after compose and before the restart)",
                "rooms",
                data={"room_id": rid, "name": name, "members": list(members)},
            ))
            mutated = True

    # Wires from the spec (skip any already present, undirected). Never orchestrator<->builder.
    installed_edges = {frozenset(p) for p in installed_wires} if isinstance(installed_wires, list) else None
    for pair in (spec.get("wires") or []):
        a, b = pair[0], pair[1]
        if installed_wires == "all":
            continue
        if installed_edges is not None and frozenset((a, b)) in installed_edges:
            continue
        steps.append(Step(["hermes", "wire", "add", a, b], "wire"))
        mutated = True

    # Restart via the shipped lifecycle command — only when something changed.
    if mutated:
        steps.append(Step(["hermes", "gateway", "restart"], "restart"))
    return steps


def render_transcript(steps: List[Step]) -> str:
    """Deterministic one-line-per-step transcript. hermes argv vectors are shlex-joined;
    action lines print verbatim. Byte-identical for the same (state, spec, ref)."""
    out = []
    for step in steps:
        cmd = step.command
        out.append(shlex.join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd))
    return "\n".join(out) + ("\n" if steps else "")


def _install_metadata(home: Path) -> Dict[str, Any]:
    """The shared install-metadata sidecar Hermes writes at
    ``<home>/plugins/.install-metadata.json`` — a JSON object keyed by plugin name whose
    entries carry the pinned commit under ``revision`` (plugins_cmd.py:561-568,855-867)."""
    meta = home / "plugins" / ".install-metadata.json"
    if not meta.exists():
        return {}
    import json
    try:
        return json.loads(meta.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _installed_revision(meta: Dict[str, Any], name: str) -> Optional[str]:
    entry = meta.get(name)
    return entry.get("revision") if isinstance(entry, dict) else None


def _collect_wires(default_config: Dict[str, Any]) -> Optional[List[List[str]]]:
    """Existing wire edges from the nv-fleet-gates edges DB, so a re-run skips edges
    already present. Filesystem-only sqlite read of the ``edges`` table
    (from_profile, to_profile); None when the DB path is absent or unreadable."""
    have, db = _get_dotted(default_config, f"plugins.entries.{_FLEET_GATES}.settings.edges_db_path")
    if not have or not db or not Path(str(db)).exists():
        return None
    import sqlite3
    try:
        conn = sqlite3.connect(str(db))
        try:
            return [[a, b] for a, b in conn.execute("SELECT from_profile, to_profile FROM edges")]
        finally:
            conn.close()
    except Exception:
        return None


def _profile_dir(home: Path, role: str) -> Path:
    """Where ``hermes profile install --name <role>`` lands, mirroring
    profiles.get_profile_dir without importing hermes (plan mode is stdlib-only)."""
    return home / "profiles" / role


def collect_home_state(spec: Dict[str, Any], spec_path: Any) -> Dict[str, Any]:
    """Build the installer's view of an EXISTING home from the filesystem only — no
    render, no hermes import — so the dry-run transcript is producible in a fresh
    sandbox before the plugin exists. Drift for profiles (``profile_digests``) is left
    unpopulated here; the apply path fills it after a render (see ``apply``)."""
    home = _require_home()
    managed_dir = _managed_dir()
    plugins = enabled_plugins(spec, spec_path)
    roles = _coworker_roles(spec)

    default_config: Dict[str, Any] = {}
    cfg = home / "config.yaml"
    if cfg.exists():
        default_config = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    cfg_enabled = set(((default_config.get("plugins") or {}).get("enabled")) or [])

    home_meta = _install_metadata(home)
    installed: Dict[str, Dict[str, Any]] = {}
    for name in plugins:
        if (home / "plugins" / name).exists():
            installed[name] = {"ref": _installed_revision(home_meta, name), "enabled": name in cfg_enabled}

    profile_plugins: Dict[str, Dict[str, Any]] = {}
    present_profiles: List[str] = []
    for role in roles:
        pdir = _profile_dir(home, role)
        if not pdir.exists():
            continue
        present_profiles.append(role)
        pconf = pdir / "config.yaml"
        pcfg = yaml.safe_load(pconf.read_text(encoding="utf-8")) or {} if pconf.exists() else {}
        penabled = set(((pcfg.get("plugins") or {}).get("enabled")) or [])
        pmeta = _install_metadata(pdir)
        scope: Dict[str, Any] = {}
        for name in plugins:
            if (pdir / "plugins" / name).exists():
                scope[name] = {"ref": _installed_revision(pmeta, name), "enabled": name in penabled}
        profile_plugins[role] = scope

    managed_config: Dict[str, Any] = {}
    mcfg = Path(managed_dir, "config.yaml") if managed_dir else None
    if mcfg and mcfg.exists():
        managed_config = yaml.safe_load(mcfg.read_text(encoding="utf-8")) or {}
    fleet_settings = (
        (((managed_config.get("plugins") or {}).get("entries") or {}).get(_FLEET_GATES) or {}).get("settings") or {}
    )
    # An existing FLEET-F62 (podman) managed fragment already carries profile_roles but NOT
    # the openshell expected_ssh_host; treating profile_roles alone as installed would skip
    # the OSH-F64 write and leave the ssh veto fail-closed. Require both.
    managed_installed = bool(fleet_settings.get("profile_roles")) and bool(fleet_settings.get("expected_ssh_host"))
    backup_present = (home / ("config.yaml" + _BACKUP_SUFFIX)).exists()

    return {
        "default_config": default_config,
        "plugins": installed,
        "profile_plugins": profile_plugins,
        "profiles": present_profiles,
        "managed_config": managed_config,
        "managed_installed": managed_installed,
        "backup_present": backup_present,
        "wires": _collect_wires(default_config),
        "rooms": None,  # existence checked at execution via groups.state (idempotent)
    }


def plan_text(spec_path: Any, ref: str) -> str:
    spec = load_spec(spec_path)
    _require_openshell(spec)
    state = collect_home_state(spec, spec_path)
    return render_transcript(build_plan(state, spec_path, ref))


# --------------------------------------------------------------------------------------
# Apply path — the real run. All hermes work goes through subprocess (never an import),
# and every config mutation is a re-read + atomic deep-merge write that preserves
# unrelated operator keys.
# --------------------------------------------------------------------------------------

def _run(cmd: List[str]) -> None:
    import subprocess
    subprocess.run(cmd, check=True)


def _atomic_write_yaml(path: Path, data: Dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".osh-f64.tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    os.replace(tmp, path)


def _digest(path: Path) -> Optional[str]:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _compute_profile_digests(spec_path: Any, ref: str, state: Dict[str, Any]) -> Dict[str, Any]:
    """Production drift detection: render the fleet to a temp dir and compare each
    already-present profile's config against the freshly-rendered one, so a drifted
    profile is planned with ``--force`` rather than silently left stale."""
    import tempfile
    spec = load_spec(spec_path)
    home = _require_home()
    digests: Dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="osh-f64-drift-") as tmp:
        _run(["hermes", "coworker", "compose", str(spec_path), "--out", tmp])
        for role in state.get("profiles") or []:
            desired = _digest(Path(tmp) / role / "config.yaml")
            current = _digest(_profile_dir(home, role) / "config.yaml")
            digests[role] = {"current": current, "desired": desired}
    return digests


def _execute_step(step: Step, ctx: Dict[str, Any]) -> None:
    tag = step.tag
    if isinstance(step.command, (list, tuple)):
        _run(list(step.command))
        return
    home = Path(ctx["home"])
    managed_dir = ctx.get("managed_dir") or ""
    if tag == "backup":
        bases = [home / "config.yaml"]
        if managed_dir:
            bases.append(Path(managed_dir, "config.yaml"))
        for base in bases:
            bak = base.with_suffix(base.suffix + _BACKUP_SUFFIX)
            if base.exists() and not bak.exists():
                import shutil
                shutil.copy2(base, bak)
    elif tag == "plugin_snapshot":
        import shutil
        payload = step.data or {}
        pdir = Path(payload.get("plugin_dir", ""))
        pbak = pdir.with_name(pdir.name + _BACKUP_SUFFIX)
        if pdir.name and pdir.exists() and not pbak.exists():
            shutil.copytree(pdir, pbak)
        side = Path(payload.get("sidecar", ""))
        sbak = side.with_suffix(side.suffix + _BACKUP_SUFFIX)
        if payload.get("sidecar") and side.exists() and not sbak.exists():
            shutil.copy2(side, sbak)
    elif tag == "default_edit":
        cfg_path = home / "config.yaml"
        current = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {} if cfg_path.exists() else {}
        _, diff, _ = default_config_diff(current, ctx["spec_path"], backup_present=True)
        _atomic_write_yaml(cfg_path, apply_config_diff(current, diff))
    elif tag == "managed_write":
        if not managed_dir:
            raise ValueError(
                "no managed-config dir resolved (HERMES_MANAGED_DIR unset and /etc/hermes "
                "absent); refusing to write the fleet-veto fragment to the cwd"
            )
        managed_path = Path(managed_dir, "config.yaml")
        rendered = {}
        rhits = sorted(Path(ctx["out"]).rglob("managed/config.yaml"))
        if rhits:
            rendered = yaml.safe_load(rhits[0].read_text(encoding="utf-8")) or {}
        existing = yaml.safe_load(managed_path.read_text(encoding="utf-8")) or {} if managed_path.exists() else {}
        managed_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_yaml(managed_path, managed_config_diff(existing, rendered))
    elif tag == "rooms":
        creator = ctx.get("room_creator")
        payload = step.data or {}
        if creator is None:
            raise ValueError(
                f"room {payload.get('room_id')!r} cannot be created: apply() was given no "
                "groups.create dispatcher"
            )
        creator(payload.get("room_id"), payload.get("name"), list(payload.get("members") or []))


def _apply_ctx(spec: Dict[str, Any], spec_path: Any, room_creator=None) -> Dict[str, Any]:
    return {
        "home": str(_require_home()),
        "managed_dir": _managed_dir() or "",
        "out": _render_out_dir(),
        "spec": spec,
        "spec_path": spec_path,
        "room_creator": room_creator,
    }


def apply(spec_path: Any, ref: str, *, room_creator=None, room_exists=None) -> None:
    """Execute the full plan. ``room_creator(room_id, name, members)`` performs the
    onboarding ``groups.create`` against the live gateway and ``room_exists(room_id)``
    reports whether a room is already present; the caller (the CLI subaction) injects both
    (the pure planner stays decoupled from the RPC seam). Existing rooms are dropped from
    the plan so a fully-installed home plans no room step and no gateway restart."""
    spec = load_spec(spec_path)
    _require_openshell(spec)
    state = collect_home_state(spec, spec_path)
    # Drift detection needs `coworker compose`, so only when a coworker is already present.
    if state.get("profiles"):
        try:
            state["profile_digests"] = _compute_profile_digests(spec_path, ref, state)
        except Exception as exc:  # a failed drift probe must not abort a valid install
            sys.stderr.write(f"[osh-f64] drift probe skipped: {exc}\n")
    if room_exists is not None:
        spec_rooms = list((spec.get("rooms") or {}).keys())
        present = [rid for rid in spec_rooms if room_exists(rid)]
        state["rooms"] = "all" if spec_rooms and len(present) == len(spec_rooms) else present
    ctx = _apply_ctx(spec, spec_path, room_creator=room_creator)
    for step in build_plan(state, spec_path, ref):
        _execute_step(step, ctx)


def phase_a(spec_path: Any, ref: str) -> None:
    """Bootstrap phase: back up the ORIGINAL config FIRST (a ``plugins install --enable``
    rewrites config.yaml, so a later backup would capture a mutated file), then install
    the port's plugins into the default home so ``hermes coworker`` exists for Phase B.
    Idempotent — a re-run skips both."""
    spec = load_spec(spec_path)
    _require_openshell(spec)
    state = collect_home_state(spec, spec_path)
    ctx = _apply_ctx(spec, spec_path)
    default_plugins_dir = Path(str(_require_home())) / "plugins"
    for step in build_plan(state, spec_path, ref):
        is_bootstrap_plugin = (
            step.tag in ("plugin_install", "plugin_force", "plugin_enable")
            and isinstance(step.command, list) and "-p" not in step.command
        )
        # The snapshot that guards a bootstrap --force targets the DEFAULT home
        # ({home}/plugins/<name>) — identify it structurally (its parent is {home}/plugins),
        # not by a "/profiles/" substring, which would misfire when HERMES_HOME itself sits
        # under a directory named "profiles". Run it too, or a Phase-A force would overwrite
        # a drifted plugin without the rollback snapshot.
        is_bootstrap_snapshot = (
            step.tag == "plugin_snapshot"
            and Path((step.data or {}).get("plugin_dir", "")).parent == default_plugins_dir
        )
        if step.tag == "backup" or is_bootstrap_plugin or is_bootstrap_snapshot:
            _execute_step(step, ctx)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="osh-f64-installer")
    sub = parser.add_subparsers(dest="mode", required=True)
    for mode in ("plan", "apply", "phase-a"):
        p = sub.add_parser(mode)
        p.add_argument("--spec", required=True)
        p.add_argument("--ref", required=True)
    ns = parser.parse_args(argv)
    if not SHA40.match(ns.ref):
        parser.error("--ref must be a full 40-character commit SHA")
    if ns.mode == "plan":
        sys.stdout.write(plan_text(ns.spec, ns.ref))
    elif ns.mode == "apply":
        apply(ns.spec, ns.ref)
    elif ns.mode == "phase-a":
        phase_a(ns.spec, ns.ref)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
