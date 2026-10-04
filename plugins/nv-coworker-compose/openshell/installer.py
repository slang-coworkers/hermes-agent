"""Two-phase OpenShell fleet installer — a pure, state-aware planner plus its executor.

Targets an EXISTING Hermes sandbox: the default profile is edited in place (never
reinstalled), the substrate is flipped to ``openshell``, and repeated runs are
idempotent. Both the ``--dry-run`` transcript and the real run are produced from
``build_plan`` and executed step-for-step, so they cannot diverge.

Pure planner surface (no side effects):
  - ``build_plan(home_state, spec_path, ref, policy_root=None) -> list[Step]``
  - ``default_config_diff(existing_config, spec, *, backup_present=False)``
  - ``apply_config_diff(config, diff)`` — deep merge, preserves unrelated keys
  - ``managed_config_diff(existing_managed, rendered_managed)`` — deep merge of the FULL
    rendered managed fragment (GOV-F23 approval floor + nv-fleet-gates settings), rendered
    keys winning, unrelated operator keys preserved

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
# Plugin snapshots live in a sibling of the scanned plugins dir: a copy inside plugins/ keeps
# its manifest name, so discovery would load it under the same key as the fresh install.
_SNAPSHOT_ROOT = ".osh-f64-backups"
_SIDECAR = ".install-metadata.json"
# Marker recording that the managed config did NOT exist before the install. The §D6
# scan-guard write (step 0.5) creates the managed config in Phase A, so without recording
# the original absence a later phase would snapshot the installer's OWN scan-guard file and
# rollback would "restore" plugins.scan_on_install: false instead of the original absence.
# Rollback deletes the managed config when this marker is present rather than restoring a .bak.
_ABSENT_SUFFIX = ".osh-f64.absent"
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


def _type_provider_names(spec: Dict[str, Any], role: str) -> List[str]:
    """The OpenShell providers a role binds at sandbox create, in declared order (deduped),
    each checked against the top-level ``providers:`` catalog. Mirrors compose's provider
    resolution without importing compose (the dry-run runs before the plugin is installed)."""
    names = ((spec.get("types") or {}).get(role) or {}).get("providers")
    if names is None:
        return []
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ValueError(f"{role}: providers must be a list of provider names, got {names!r}")
    catalog = spec.get("providers") or {}
    out: List[str] = []
    for name in names:
        if name not in catalog:
            raise ValueError(
                f"{role}: unknown provider {name!r} (not declared in the top-level providers: catalog)")
        if name not in out:
            out.append(name)
    return out


def _require_openshell(spec: Dict[str, Any]) -> None:
    """Refuse a spec this installer must not act on, before any state collection or install,
    so a wrong spec cannot install/enable plugins and only then fail (the entry points call
    this first). It must be an openshell fleet AND declare the pinned offline lane: the
    Phase-A scan guard and the default-config firecrawl union exist only for that lane, the
    same opt-in the render's lane enforcements key on."""
    substrate = spec.get("substrate")
    if substrate != "openshell":
        raise ValueError(f"install-openshell requires substrate: openshell (spec has {substrate!r})")
    egress = spec.get("egress")
    lane = egress.get("pinned_offline_lane") if isinstance(egress, dict) else None
    if lane is not True:
        raise ValueError(
            f"install-openshell requires egress.pinned_offline_lane: true (spec has {lane!r})")


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
    # OSH-F64.b (ADR §D4): under the single-authority posture the render scrubs podman-onecli from
    # every rendered config (it is inert here), so the installer must not bootstrap it either —
    # else install would re-enable a plugin the render removed. The legacy path (no
    # inference_provider) keeps the spine set unchanged.
    if "inference_provider" in (spec.get("egress") or {}):
        return [name for name in spine if name != "podman-onecli"]
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


# OSH-F64.b: a diff value of this sentinel means "DELETE this dotted key from the existing default
# config", not "set it to None". A null would leave the key present (and `secrets.onecli: null`
# still reads as a configured block); the single-authority upgrade must REMOVE the inherited OneCLI
# state, so the diff carries an explicit delete marker that apply_config_diff honours.
_DELETE = object()


def _del_dotted(cfg: Dict[str, Any], dotted: str) -> bool:
    """Remove ``dotted`` from ``cfg`` if present. Returns True when something was removed.
    Prunes a parent mapping only when it becomes empty AND it is one this edit created the leaf
    under, so an operator's sibling key never drags an unrelated parent away."""
    parts = dotted.split(".")
    stack = []
    cur: Any = cfg
    for part in parts[:-1]:
        if not isinstance(cur, dict) or part not in cur:
            return False
        stack.append((cur, part))
        cur = cur[part]
    if not isinstance(cur, dict) or parts[-1] not in cur:
        return False
    del cur[parts[-1]]
    # Walk back up pruning now-empty mappings this delete emptied.
    for parent, key in reversed(stack):
        if isinstance(parent.get(key), dict) and not parent[key]:
            del parent[key]
        else:
            break
    return True


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
    egress = spec.get("egress") or {}
    gated = "inference_provider" in egress
    have, cur_enabled = _get_dotted(existing, "plugins.enabled")
    merged = list(cur_enabled) if have and isinstance(cur_enabled, list) else []
    for name in enabled:
        if name not in merged:
            merged.append(name)
    # Under single authority podman-onecli is inert and the render removes it (enabled_plugins
    # already drops it from `enabled`), but the UNION above re-adds it from an upgraded home's
    # existing plugins.enabled. Filter it from the union too — else an in-place default upgrade
    # would re-enable the plugin the render scrubbed.
    if gated:
        merged = [name for name in merged if name != "podman-onecli"]
    desired: Dict[str, Any] = {
        "gateway.multiplex_profiles": True,
        "gateway.multiplex_profile_allowlist": roles,
        "plugins.enabled": merged,
    }
    # The single-authority upgrade must also DELETE the inherited OneCLI state an earlier
    # (legacy / non-gated) install left in the default home — the podman-onecli settings entry and
    # the secrets.onecli block — matching the render's scrub (compose.py). Emit a delete only when
    # the key is actually present, so a clean/gated home plans no edit (idempotence AC-1).
    if gated:
        for dotted in ("plugins.entries.podman-onecli", "secrets.onecli"):
            present, _ = _get_dotted(existing, dotted)
            if present:
                desired[dotted] = _DELETE
    for name in enabled:
        settings = ((entries.get(name) or {}).get("settings")) if isinstance(entries.get(name), dict) else None
        if isinstance(settings, dict) and settings:
            _, cur = _get_dotted(existing, f"plugins.entries.{name}.settings")
            base = cur if isinstance(cur, dict) else {}
            # Deep-merge onto any existing settings so an unrelated operator key on the
            # same entry survives (never a wholesale replace).
            desired[f"plugins.entries.{name}.settings"] = _deep_merge(base, settings)
    # §D8 route 1: the render disables the firecrawl providers on the five served configs
    # (installed via `profile install`), but the gateway default is edited in place here, so
    # union plugins.disabled onto it too — else the multiplex HOST still loads firecrawl.
    # Order-stable + dedup'd so a re-run is a no-op (the idempotence AC-1 asserts), and an
    # operator-set disable is preserved.
    have_disabled, cur_disabled = _get_dotted(existing, "plugins.disabled")
    merged_disabled = list(cur_disabled) if have_disabled and isinstance(cur_disabled, list) else []
    for name in ("web-firecrawl", "browser-firecrawl"):
        if name not in merged_disabled:
            merged_disabled.append(name)
    desired["plugins.disabled"] = merged_disabled
    # onecli-onboard reads profile_secret_sets from this gateway default, which is edited in
    # place here, not installed from the render. Carry the served-role grants under openshell,
    # retaining any operator-managed identity, stripped to match the render's validated value.
    secret_ids = egress.get("onecli_secret_ids")
    # OSH-F64.b (ADR §D4): under the single-authority inference posture (egress.inference_provider
    # present) the OneCLI chain-dial is superseded — the gateway default must NOT carry the §D7.1
    # grant map, so profile_secret_sets stays empty (matching the render, compose.py). The legacy
    # §D7 path (no inference_provider) is byte-unchanged.
    if secret_ids and spec.get("substrate") == "openshell" and not gated:
        key = "plugins.entries.podman-onecli.settings"
        if isinstance(desired.get(key), dict):
            settings = dict(desired[key])
        else:
            _, cur = _get_dotted(existing, key)
            settings = dict(cur) if isinstance(cur, dict) else {}
        grants = dict(settings.get("profile_secret_sets") or {})
        for role in roles:
            grants[role] = [s.strip() for s in secret_ids]
        settings["profile_secret_sets"] = grants
        desired[key] = settings
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
    keys are preserved verbatim (never clobbered). A ``_DELETE`` value removes the key
    (OSH-F64.b single-authority upgrade), rather than setting it to ``None``."""
    out = copy.deepcopy(config) if isinstance(config, dict) else {}
    for dotted, value in diff.items():
        if value is _DELETE:
            _del_dotted(out, dotted)
        else:
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
    """Deep-merge the FULL rendered managed fragment onto the existing managed config: the
    fleet-uniform GOV-F23 approval policy (``approvals.*`` / ``security.approval.*``) AND the
    ``nv-fleet-gates`` settings (``profile_roles`` + ``expected_ssh_host``) the render emits.
    Rendered keys win on conflict — the fleet policy is machine-wide and authoritative — while
    any UNRELATED machine policy the operator keeps there is preserved by the deep merge. (An
    nv-fleet-gates-only merge would silently drop the rendered approval floor, GOV-F23.)

    The two fleet maps (``profile_roles``, ``expected_ssh_host``) are replaced whole, not merged:
    a role an older render had but this one does not must not survive into the veto's map."""
    merged = _deep_merge(existing_managed or {}, rendered_managed or {})
    rendered_settings = _get_dotted(rendered_managed or {}, f"plugins.entries.{_FLEET_GATES}.settings")[1]
    if isinstance(rendered_settings, dict):
        for key in ("profile_roles", "expected_ssh_host"):
            if key in rendered_settings:
                merged["plugins"]["entries"][_FLEET_GATES]["settings"][key] = copy.deepcopy(rendered_settings[key])
    return merged


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


def _snapshot_dir(plugins_dir: str) -> str:
    """Where snapshots of ``plugins_dir`` live: ``<plugins parent>/.osh-f64-backups/plugins``."""
    return str(Path(plugins_dir).parent / _SNAPSHOT_ROOT / "plugins")


def _snapshot_step(plugins_dir: str, name: str) -> Step:
    """Snapshot a plugin's directory + the shared install-metadata sidecar before a
    ``--force`` reinstall overwrites them, so a rollback can restore the prior version. The
    snapshot goes outside the scanned plugins dir (``_snapshot_dir``)."""
    target = _snapshot_dir(plugins_dir)
    return Step(
        f"# snapshot {plugins_dir}/{name} + {plugins_dir}/{_SIDECAR} -> {target} before reinstall",
        "plugin_snapshot",
        data={"plugin_dir": f"{plugins_dir}/{name}", "sidecar": f"{plugins_dir}/{_SIDECAR}",
              "snapshot_dir": target},
    )


def _legacy_snapshots(plugins_dir: Path) -> List[str]:
    """Snapshot dirs an older installer left INSIDE ``plugins_dir`` (``<name>.osh-f64.bak``)."""
    if not plugins_dir.is_dir():
        return []
    return sorted(str(p) for p in plugins_dir.iterdir()
                  if p.is_dir() and not p.is_symlink() and p.name.endswith(_BACKUP_SUFFIX))


def _render_out_dir() -> str:
    """Deterministic, home-relative staging dir for the compose output — printed in the
    dry-run transcript but never created by it, so two runs are byte-identical."""
    return os.path.join(str(_require_home()), ".osh-f64", "render")


def _profile_drifted(role: str, digests: Dict[str, Any]) -> bool:
    d = digests.get(role) or {}
    cur, want = d.get("current"), d.get("desired")
    return cur is not None and want is not None and cur != want


def build_plan(home_state: Dict[str, Any], spec_path: Any, ref: str, policy_root: Optional[str] = None) -> List[Step]:
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

    # Legacy snapshots inside a scanned plugins dir shadow the fresh install under the same key,
    # so they move out first — on every run that finds one, --force planned or not.
    legacy = list(home_state.get("legacy_snapshots") or [])
    if legacy:
        dirs = sorted({str(Path(p).parent) for p in legacy})
        steps.append(Step(f"# migrate legacy plugin snapshots out of {', '.join(dirs)}",
                          "legacy_snapshot_migrate", data={"snapshots": legacy}))
        mutated = True

    # Step 0 — snapshot the ORIGINAL config(s) BEFORE any mutation: a later `plugins
    # install --enable` rewrites config.yaml, and the managed write overwrites the veto
    # fragment. One backup step; its executor snapshots each file that exists and has no
    # snapshot yet, so a MANAGED-ONLY change (default diff empty) is still backed up.
    _, diff, backup_required = default_config_diff(default_config, spec_path, backup_present=backup_present)
    managed_cfg = Path(managed_dir, "config.yaml") if managed_dir else None
    # Record the managed config's ORIGINAL state before the §D6 scan-guard write creates/mutates
    # it: a .bak when it exists now, else an .absent marker. Recording absence matters as much as
    # recording content — otherwise a later phase snapshots the installer's own scan-guard file
    # and rollback restores plugins.scan_on_install: false instead of the original absence.
    managed_backup_needed = (
        not managed_installed and managed_cfg is not None
        and not managed_cfg.with_suffix(managed_cfg.suffix + _BACKUP_SUFFIX).exists()
        and not managed_cfg.with_suffix(managed_cfg.suffix + _ABSENT_SUFFIX).exists()
    )
    if backup_required or managed_backup_needed:
        backup_targets = [f"{home}/config.yaml"]
        if managed_backup_needed:
            backup_targets.append(f"{managed_dir}/config.yaml")
        steps.append(Step(f"# backup {' and '.join(backup_targets)} before any mutation", "backup"))
        mutated = True

    # §D6 scan-guard — write plugins.scan_on_install: false to the managed config BEFORE Phase
    # A's first `plugins install`. The install-time scanner returns a DANGEROUS verdict on the
    # first-party fleet plugins (false positives) that --force cannot override
    # (plugin_guard.py:332-335); the durable step-4 managed write runs AFTER the installs, so the
    # guard is established here first (managed_config_diff is a pure deep-merge, so any pre-seeded
    # operator keys survive). Skipped once the managed fragment already carries it (managed_installed).
    if not managed_installed:
        steps.append(Step(
            f"# write plugins.scan_on_install: false -> {managed_dir}/config.yaml "
            "(§D6 install-scan guard, before any plugins install)",
            "scan_guard_write",
        ))
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

    # OpenShell worker-sandbox provisioning — SETUP prefix only (one sandbox create + one
    # sandbox ssh-config per served coworker). `sandbox create --policy policy-<role>.yaml`
    # binds and enforces the per-worker policy inline, so there is NO separate `openshell
    # policy set` step; the provision plan is the 15-line no-policy shape (5 create + 5
    # ssh-config + 5 delete). The
    # teardown suffix (sandbox delete) is reserved for rollback and never appears in the
    # install plan. Sandbox names mirror compose's <project>-<role> ssh_host. A profile can
    # exist without its sandbox, so _sandbox_exists is checked at apply time.
    project = spec.get("project") or "fleet"
    image = (spec.get("egress") or {}).get("sandbox_image") or "localhost/hermes-openshell-sandbox:pinned"
    for role in roles:
        sandbox = f"{project}-{role}"
        # OpenShell validates --policy on the broker host, so an explicit root selects the
        # host-mirrored render tree; unset keeps the gateway-internal render path.
        policy = (
            f"{policy_root}/render/{role}/policy-{role}.yaml"
            if policy_root
            else f"{out}/{role}/policy-{role}.yaml"
        )
        # --no-tty: an executed create must not open an interactive session. Providers bind at
        # create, the same set compose's provisioning plan prints.
        providers = [tok for name in _type_provider_names(spec, role) for tok in ("--provider", name)]
        steps.append(Step(["openshell", "sandbox", "create", "--name", sandbox, "--from", image,
                           "--policy", policy, "--no-tty", *providers], "provision_create",
                          data={"rendered_policy": f"{out}/{role}/policy-{role}.yaml"}))
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
    # (gateway.multiplex_*, plugins.enabled, plugins.entries.<name>.settings), separating the
    # OSH-F64.b single-authority deletions (inherited OneCLI state) from the set keys.
    if diff:
        set_keys = sorted(k for k, v in diff.items() if v is not _DELETE)
        del_keys = sorted(k for k, v in diff.items() if v is _DELETE)
        parts = []
        if set_keys:
            parts.append("set " + ", ".join(set_keys))
        if del_keys:
            parts.append("delete " + ", ".join(del_keys))
        steps.append(Step(f"# edit default config {home}/config.yaml: {'; '.join(parts)}", "default_edit"))
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

    # Wires from the spec (skip any already present; edges are undirected). The spec's
    # `wires` list is authoritative — the loop adds exactly those edges, no more.
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


def _managed_fleet_maps(spec: Dict[str, Any]) -> Tuple[Dict[str, str], Dict[str, str]]:
    """The nv-fleet-gates ``profile_roles`` and ``expected_ssh_host`` maps compose renders into
    the managed fragment for this spec: each served role → orchestrator|worker, and → its ssh
    alias ``openshell-<project>-<role>`` (compose's alias rule, computed without importing it)."""
    project = spec.get("project", "fleet")
    orchestrator = spec.get("orchestrator_profile", "orchestrator")
    roles = _coworker_roles(spec)
    return ({r: ("orchestrator" if r == orchestrator else "worker") for r in roles},
            {r: f"openshell-{project}-{r}" for r in roles})


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

    legacy_snapshots = _legacy_snapshots(home / "plugins")
    profile_plugins: Dict[str, Dict[str, Any]] = {}
    present_profiles: List[str] = []
    for role in roles:
        pdir = _profile_dir(home, role)
        if not pdir.exists():
            continue
        present_profiles.append(role)
        legacy_snapshots += _legacy_snapshots(pdir / "plugins")
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
    # the OSH-F64 write and leave the ssh veto fail-closed. A managed fragment written by an
    # earlier build that dropped the GOV-F23 approval floor likewise must be repaired, so also
    # require the machine-wide approval keys present — else schedule managed_write to backfill.
    approvals_present = bool((managed_config.get("approvals") or {}).get("mode")) and bool(
        ((managed_config.get("security") or {}).get("approval") or {}).get("transport"))
    # §D6 (F64-P2): a fragment still lacking plugins.scan_on_install: false is NOT installed —
    # a re-run would plan a scanned `plugins install`/`--force` the DANGEROUS verdict refuses
    # (--force cannot override it), so the scan-guard write must be scheduled to backfill it.
    scan_guarded = ((managed_config.get("plugins") or {}).get("scan_on_install") is False)
    # A fragment from an older render (other roles, an old ssh host) is stale, not installed:
    # the fleet keys must EQUAL what this spec renders, else the veto checks the wrong map.
    want_roles, want_hosts = _managed_fleet_maps(spec)
    managed_installed = (fleet_settings.get("profile_roles") == want_roles
                         and fleet_settings.get("expected_ssh_host") == want_hosts
                         and approvals_present
                         and scan_guarded)
    backup_present = (home / ("config.yaml" + _BACKUP_SUFFIX)).exists()

    return {
        "default_config": default_config,
        "plugins": installed,
        "profile_plugins": profile_plugins,
        "profiles": present_profiles,
        "managed_config": managed_config,
        "managed_installed": managed_installed,
        "legacy_snapshots": legacy_snapshots,
        "backup_present": backup_present,
        "wires": _collect_wires(default_config),
        "rooms": None,  # existence checked at execution via groups.state (idempotent)
    }


def plan_text(spec_path: Any, ref: str, policy_root: Optional[str] = None) -> str:
    spec = load_spec(spec_path)
    _require_openshell(spec)
    state = collect_home_state(spec, spec_path)
    return render_transcript(build_plan(state, spec_path, ref, policy_root=policy_root))


# --------------------------------------------------------------------------------------
# Apply path — the real run. All hermes work goes through subprocess (never an import),
# and every config mutation is a re-read + atomic deep-merge write that preserves
# unrelated operator keys.
# --------------------------------------------------------------------------------------

def _run(cmd: List[str], env: Optional[Dict[str, str]] = None) -> None:
    import subprocess
    # stdin is closed: these are non-interactive hermes/openshell invocations, and a child left
    # attached to the parent's stdin can consume the apply driver's input (subprocess-stdin guard).
    subprocess.run(cmd, check=True, env=env, stdin=subprocess.DEVNULL)


_REFUSED_ENV_NAMES = ("HERMES_DASHBOARD_SESSION_TOKEN", "API_SERVER_KEY")


def _is_refused_secret_env(name: str) -> bool:
    # The NemoClaw `hermes` wrapper's [SECURITY] startup guard refuses a process whose env
    # carries raw secret-shaped names; the `gateway restart` child needs none of them.
    return name in _REFUSED_ENV_NAMES or name.endswith("_TOKEN_FILE")


def _gateway_restart_env() -> Dict[str, str]:
    # Env for the `hermes gateway restart` child: os.environ minus the refused secret-shaped
    # names, so the wrapper guard does not refuse it. Scoping the child's inherited names does
    # NOT waive the core api_server API_SERVER_KEY guard — a gateway that actually enables
    # api_server still needs a strong key of its own.
    return {k: v for k, v in os.environ.items() if not _is_refused_secret_env(k)}


def _sandbox_exists(name: str) -> bool:
    # Apply-time idempotence for provision_create, decoupled from profile-installed state:
    # whether an OpenShell worker sandbox is already provisioned, from the broker's
    # `openshell sandbox list` (first whitespace column is NAME; the NAME/CREATED/PHASE header's
    # first token never matches a <project>-<role> name). A failed listing raises (check=True)
    # so apply halts on an unknown broker state rather than creating over a live sandbox.
    import subprocess
    proc = subprocess.run(
        ["openshell", "sandbox", "list"], check=True, capture_output=True, text=True
    )
    for line in proc.stdout.splitlines():
        parts = line.split()
        if parts and parts[0] == name:
            return True
    return False


def _atomic_write_yaml(path: Path, data: Dict[str, Any]) -> None:
    # Resolve a symlinked config to its target so os.replace updates the target in place and
    # never detaches an operator's symlink into a plain file. Write the temp beside the target
    # (same directory) so os.replace stays an atomic same-filesystem rename.
    target = Path(os.path.realpath(path)) if path.is_symlink() else path
    tmp = target.with_suffix(target.suffix + ".osh-f64.tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    # Preserve the existing config's permission bits: os.replace adopts the temp file's
    # umask default, which would silently widen a 0600 operator config to 0644.
    if target.exists():
        os.chmod(tmp, target.stat().st_mode & 0o777)
    os.replace(tmp, target)


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


_CAPTURE_TIMEOUT_S = 60
_STATUS_POLLS = 3
_STATUS_POLL_INTERVAL_S = 2.0


def _run_capture(cmd: List[str]) -> str:
    import subprocess
    proc = subprocess.run(cmd, check=True, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=_CAPTURE_TIMEOUT_S)
    return proc.stdout or ""


def _atomic_write_text(path: Path, text: str) -> None:
    # Text twin of _atomic_write_yaml (symlink target kept, mode kept); a new file is 0600
    # because OpenSSH refuses a config file other users can write.
    target = Path(os.path.realpath(path)) if path.is_symlink() else path
    tmp = target.with_name(target.name + ".osh-f64.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, (target.stat().st_mode & 0o777) if target.exists() else 0o600)
    os.replace(tmp, target)


def _persist_ssh_config(sandbox: str, project: str, block: str) -> None:
    """Persist one sandbox's ``openshell sandbox ssh-config`` output into the gateway user's
    managed include ``~/.ssh/config.d/openshell-<project>.conf`` (one marker-delimited block per
    alias, replaced in place on a rerun), and make ``~/.ssh/config`` include it exactly once as
    its FIRST line — OpenSSH applies the first value it reads, so a later operator Host entry
    cannot shadow the alias. Every other line of the operator's config is kept."""
    ssh_dir = Path(os.path.expanduser("~")) / ".ssh"
    inc_rel = f"config.d/openshell-{project}.conf"
    inc_path = ssh_dir / inc_rel
    inc_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    alias = f"openshell-{sandbox}"
    begin, end = f"# >>> {alias}", f"# <<< {alias}"
    new_block = f"{begin}\n{block.rstrip()}\n{end}\n"
    text = inc_path.read_text(encoding="utf-8") if inc_path.exists() else ""
    pattern = re.compile(rf"(?ms)^{re.escape(begin)}\n.*?^{re.escape(end)}\n?")
    if pattern.search(text):
        text = pattern.sub(lambda _m: new_block, text, count=1)
    else:
        text = text + ("\n" if text and not text.endswith("\n") else "") + new_block
    _atomic_write_text(inc_path, text)
    main = ssh_dir / "config"
    lines = main.read_text(encoding="utf-8").splitlines(keepends=True) if main.exists() else []
    include = f"Include {inc_rel}\n"
    kept = [ln for ln in lines if ln.strip() != include.strip()]
    if lines[:1] != [include] or len(kept) != len(lines) - 1:
        _atomic_write_text(main, include + "".join(kept))


def _record_policy_digest(home: Path, sandbox: str) -> Path:
    return home / ".osh-f64" / "sandbox-policy" / f"{sandbox}.sha256"


def _provision_create(cmd: List[str], step: Step, ctx: Dict[str, Any]) -> None:
    """Create a worker sandbox, or re-create it when the policy it was created with differs from
    the current render. The rendered policy's digest is recorded after each create; a sandbox
    with no record (an older install) counts as drifted. A ``--policy-root`` mirror must match
    the render byte for byte, else the install halts before any delete or create."""
    name = cmd[cmd.index("--name") + 1]
    exists = _sandbox_exists(name)
    rendered = Path((step.data or {}).get("rendered_policy") or cmd[cmd.index("--policy") + 1])
    bound = Path(cmd[cmd.index("--policy") + 1])
    if not rendered.is_file():
        raise ValueError(f"{name}: rendered policy {rendered} is missing; run the compose step first")
    want = rendered.read_bytes()
    if bound != rendered and (not bound.is_file() or bound.read_bytes() != want):
        raise ValueError(
            f"{name}: --policy {bound} does not match the rendered policy {rendered}; refresh the "
            "host mirror before installing (a stale mirror would bind an old policy)")
    import hashlib
    digest = hashlib.sha256(want).hexdigest()
    record = _record_policy_digest(Path(ctx["home"]), name)
    if exists:
        recorded = record.read_text(encoding="utf-8").strip() if record.is_file() else None
        if recorded == digest:
            return
        _run(["openshell", "sandbox", "delete", name])
    _run(cmd)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(digest + "\n", encoding="utf-8")


def _gateway_answered(status_stdout: str) -> bool:
    text = (status_stdout or "").lower()
    return "gateway is running" in text or "active (running)" in text


def _restart_gateway_detached(cmd: List[str], home: Path) -> None:
    """Run ``hermes gateway restart`` so the installer returns. Without a service manager the
    release restart runs the new gateway in the foreground of its own process, so the child is
    started in its own session (``setsid -f``, else ``start_new_session``) with stdin closed and
    output to a log; it outlives the installer as the gateway. Under a service manager the child
    restarts the service and exits. Either way the wait is bounded and the installer returns,
    reporting what it saw."""
    import shutil
    import subprocess
    import time
    env = _gateway_restart_env()
    log_path = home / "logs" / "osh-f64-restart.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log:
        if shutil.which("setsid", path=env.get("PATH")):
            subprocess.run(["setsid", "-f", *cmd], check=True, stdin=subprocess.DEVNULL,
                           stdout=log, stderr=subprocess.STDOUT, env=env)
        else:
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                             env=env, start_new_session=True)
    # The first poll waits one interval so a status answer is less likely to come from the
    # gateway being replaced; an answer is reported, not treated as proof of the new one.
    answered = False
    for _attempt in range(_STATUS_POLLS):
        time.sleep(_STATUS_POLL_INTERVAL_S)
        try:
            probe = subprocess.run(["hermes", "gateway", "status"], capture_output=True, text=True,
                                   stdin=subprocess.DEVNULL, env=env, timeout=_CAPTURE_TIMEOUT_S)
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, OSError):
            continue
        if _gateway_answered(getattr(probe, "stdout", "")):
            answered = True
            break
    status = "a gateway answered" if answered else "no gateway answered within the poll budget"
    sys.stderr.write(f"[osh-f64] gateway restart launched detached (log {log_path}); {status}\n")


def _migrate_legacy_snapshots(snapshots: List[str]) -> None:
    """Move every legacy ``<plugins>/<name>.osh-f64.bak`` snapshot (and its plugins dir's
    ``.install-metadata.json.osh-f64.bak``) to that plugins dir's ``_snapshot_dir``. All
    destinations are checked before anything moves, and an existing destination refuses."""
    import shutil
    moves: List[Tuple[Path, Path]] = []
    for raw in snapshots:
        src = Path(raw)
        if not src.is_dir():
            continue
        dest_dir = Path(_snapshot_dir(str(src.parent)))
        moves.append((src, dest_dir / src.name[: -len(_BACKUP_SUFFIX)]))
        side = src.parent / (_SIDECAR + _BACKUP_SUFFIX)
        if side.is_file() and (side, dest_dir / _SIDECAR) not in moves:
            moves.append((side, dest_dir / _SIDECAR))
    clashes = [str(dest) for _src, dest in moves if dest.exists()]
    if clashes:
        raise ValueError(f"refusing to migrate legacy plugin snapshots over existing {clashes}; "
                         "move or remove them by hand, then re-run")
    for src, dest in moves:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))


def _execute_step(step: Step, ctx: Dict[str, Any]) -> None:
    tag = step.tag
    if isinstance(step.command, (list, tuple)):
        cmd = list(step.command)
        if tag == "provision_create":
            _provision_create(cmd, step, ctx)
            return
        if tag == "provision_sshconfig":
            sandbox = cmd[-1]
            project = (ctx.get("spec") or {}).get("project") or "fleet"
            _persist_ssh_config(sandbox, project, _run_capture(cmd))
            return
        if tag == "restart":
            _restart_gateway_detached(cmd, Path(ctx["home"]))
            return
        _run(cmd)
        return
    home = Path(ctx["home"])
    managed_dir = ctx.get("managed_dir") or ""
    if tag == "backup":
        import shutil
        home_cfg = home / "config.yaml"
        home_bak = home_cfg.with_suffix(home_cfg.suffix + _BACKUP_SUFFIX)
        if home_cfg.exists() and not home_bak.exists():
            shutil.copy2(home_cfg, home_bak)
        if managed_dir:
            # Record the managed config's ORIGINAL state before scan_guard_write creates/mutates
            # it: a .bak of its content, or an .absent marker when it did not exist — so rollback
            # never restores the installer's own §D6 scan-guard file (which would leave scanning
            # disabled) and instead deletes it to restore the original absence.
            mcfg = Path(managed_dir, "config.yaml")
            mbak = mcfg.with_suffix(mcfg.suffix + _BACKUP_SUFFIX)
            mabsent = mcfg.with_suffix(mcfg.suffix + _ABSENT_SUFFIX)
            if not mbak.exists() and not mabsent.exists():
                if mcfg.exists():
                    shutil.copy2(mcfg, mbak)
                else:
                    # Record absence even when the managed dir does not exist yet — scan_guard_write
                    # creates it next, so without the marker Phase B would snapshot that new file.
                    mabsent.parent.mkdir(parents=True, exist_ok=True)
                    mabsent.write_text("", encoding="utf-8")
    elif tag == "legacy_snapshot_migrate":
        _migrate_legacy_snapshots(list((step.data or {}).get("snapshots") or []))
    elif tag == "plugin_snapshot":
        import shutil
        payload = step.data or {}
        pdir = Path(payload.get("plugin_dir", ""))
        snap_dir = Path(payload.get("snapshot_dir") or _snapshot_dir(str(pdir.parent)))
        pbak = snap_dir / pdir.name
        if pdir.name and pdir.exists() and not pbak.exists():
            snap_dir.mkdir(parents=True, exist_ok=True)
            shutil.copytree(pdir, pbak)
        side = Path(payload.get("sidecar", ""))
        sbak = snap_dir / side.name
        if payload.get("sidecar") and side.exists() and not sbak.exists():
            snap_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(side, sbak)
    elif tag == "default_edit":
        cfg_path = home / "config.yaml"
        current = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {} if cfg_path.exists() else {}
        _, diff, _ = default_config_diff(current, ctx["spec_path"], backup_present=True)
        _atomic_write_yaml(cfg_path, apply_config_diff(current, diff))
    elif tag == "scan_guard_write":
        if not managed_dir:
            raise ValueError(
                "no managed-config dir resolved (HERMES_MANAGED_DIR unset and /etc/hermes "
                "absent); refusing to write the §D6 scan guard to the cwd"
            )
        managed_path = Path(managed_dir, "config.yaml")
        existing = yaml.safe_load(managed_path.read_text(encoding="utf-8")) or {} if managed_path.exists() else {}
        managed_path.parent.mkdir(parents=True, exist_ok=True)
        # Deep-merge just the guard so any pre-seeded operator managed keys survive; the durable
        # step-4 managed_write later folds in the full rendered fragment (which also carries it).
        _atomic_write_yaml(managed_path, managed_config_diff(existing, {"plugins": {"scan_on_install": False}}))
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


def apply(spec_path: Any, ref: str, *, room_creator=None, room_exists=None, policy_root=None) -> None:
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
    for step in build_plan(state, spec_path, ref, policy_root=policy_root):
        _execute_step(step, ctx)


def phase_a(spec_path: Any, ref: str) -> None:
    """Bootstrap phase: move any legacy in-scan plugin snapshot out of the plugins dir (it
    would shadow the bootstrap install), then back up the ORIGINAL config (a ``plugins install --enable``
    rewrites config.yaml, so a later backup would capture a mutated file), THEN write the
    §D6 scan guard (``plugins.scan_on_install: false``) into the managed config so the
    first-party fleet plugins install offline without the scanner's DANGEROUS verdict
    (which ``--force`` cannot override), THEN install the port's plugins into the default
    home so ``hermes coworker`` exists for Phase B. Idempotent — a re-run skips all three.
    Order is fixed by ``build_plan`` (legacy_snapshot_migrate -> backup -> scan_guard_write ->
    plugin installs)."""
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
        if (step.tag in ("legacy_snapshot_migrate", "backup", "scan_guard_write")
                or is_bootstrap_plugin or is_bootstrap_snapshot):
            _execute_step(step, ctx)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="osh-f64-installer")
    sub = parser.add_subparsers(dest="mode", required=True)
    for mode in ("plan", "apply", "phase-a"):
        p = sub.add_parser(mode)
        p.add_argument("--spec", required=True)
        p.add_argument("--ref", required=True)
        if mode in ("plan", "apply"):
            p.add_argument(
                "--policy-root", default=None,
                help="Host dir the OpenShell broker validates worker --policy paths under; "
                     "rebases each provision-create --policy to <root>/render/<role>/policy-<role>.yaml "
                     "(default: the gateway-internal render path)",
            )
    ns = parser.parse_args(argv)
    if not SHA40.match(ns.ref):
        parser.error("--ref must be a full 40-character commit SHA")
    if ns.mode == "plan":
        sys.stdout.write(plan_text(ns.spec, ns.ref, policy_root=getattr(ns, "policy_root", None)))
    elif ns.mode == "apply":
        apply(ns.spec, ns.ref, policy_root=getattr(ns, "policy_root", None))
    elif ns.mode == "phase-a":
        phase_a(ns.spec, ns.ref)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
