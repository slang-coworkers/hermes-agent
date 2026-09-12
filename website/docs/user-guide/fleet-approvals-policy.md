---
sidebar_position: 75
title: "Fleet approvals policy (on-call runbook)"
description: "How nv-coworker-compose pins the fleet-safe approvals policy — the machine-wide managed fragment, the per-role command_allowlist / approvals.deny floor, and the DEFAULT-empty in-gateway fail-safe floor — how to deploy the managed fragment, the native process-global allowlist boundary, and the NanoClaw approvals term map."
---

# Fleet approvals policy (on-call runbook)

This runbook covers how a `nv-coworker-compose`-rendered fleet keeps dangerous
commands from running unattended without a human, and the operational steps that
keep that guarantee true across a deploy, a re-pin, and a `hermes config migrate`.
It is scoped to fleets rendered by the `nv-coworker-compose` plugin (the DEFAULT
multiplexer profile plus every coworker profile). Reader `file:line` citations are
against the pinned release tree (`v2026.8.31`, commit `29112bef`).

The safety property this delivers: **a `hermes chat -q` worker (the shape the
kanban dispatcher spawns, `hermes_cli/kanban_db.py:10726`) that hits a
non-allowlisted flagged-dangerous command is refused instantly — denied, not held
for a human — and nothing is set to `approve`.** The interactive orchestrator Bot
Chat keeps a working human gate because the fleet mode stays `smart`.

## What the render pins, and where

Hermes' approval keys are all **top-level `approvals.*`** (plus the top-level
`command_allowlist`); `security.approval` holds only the presentation-only
`transport` / `transport_fallback` (`hermes_cli/config_defaults.py:2665-2674`).
The render splits them into two immutable scopes.

### 1. The machine-wide managed fragment — the fleet-uniform policy

`nv-coworker-compose` emits one file, `<out_root>/managed/config.yaml`, carrying
the six fleet-uniform keys, written **explicitly and never by assumption** so a
re-pin or an upstream default flip cannot silently loosen them:

| key | rendered value | reader (release tree) |
|---|---|---|
| `approvals.mode` | `smart` | `tools/approval.py:3462` (via `_get_approval_config`) |
| `approvals.timeout` | `300` | `tools/approval.py:3511` |
| `approvals.cron_mode` | `deny` | `tools/approval.py:3539` |
| `approvals.single_query_mode` | `deny` | `tools/approval.py:3552` |
| `approvals.unattended_mode` | `deny` | `tools/approval.py:3571` |
| `approvals.denial_breaker_threshold` | `3` | `tools/approval.py:2760` |

Every value equals the stock default (`hermes_cli/config_defaults.py:2558-2576`),
so **the fleet fails safe if the fragment is not yet installed**: stock Hermes
already denies unattended dangerous commands. The fragment's value is (a) a single
machine-wide source of truth (DRY across N profiles) and (b) root-owned
immutability — placing `approvals.mode` in managed scope makes an interactive
`/approvals` mode change refuse with *"Approval mode is managed and cannot be
changed."* (`hermes_cli/approval_mode.py:54-66`, the `set_config_value` raise at
`:63`). These six keys live **only** in the managed fragment; they are stripped
from every rendered profile config (the disjoint split).

### 2. Per-role `command_allowlist` + `approvals.deny` — in each profile config

Each coworker type declares, in its `config:` block:

- a top-level **`command_allowlist`** — the exact flagged-dangerous verbs that role
  legitimately runs (the fixer's build/test verbs, `git reset --hard*`, …). It is
  read TOP-LEVEL (`config.get("command_allowlist")`, `tools/approval.py:3188`); a
  nested `agent.command_allowlist` has **no reader** and would be silently inert.
  It is the approval-bypass list, checked *before* dangerous-command detection
  (short-circuit at `tools/approval.py:4784`, ahead of every
  `detect_dangerous_command` call).
- an **`approvals.deny`** floor — verbs that must never run unattended
  (`git push --force*`, `git push -f*`, `git tag *`, `gh release *`). It is a
  fresh-config, per-profile-correct read (`_match_user_deny_rule`,
  `tools/approval.py:809`, blocked at `:826-838`, wired *before* the yolo/off
  bypass at `:4772`) and outranks the allowlist, so a floor verb can never be
  allowlisted.

Guard order (`check_all_command_guards`, `tools/approval.py:4730`): hardline
`:4753` → sudo `:4763` → `approvals.deny` `:4772` → yolo/off `:4780` →
**permanent allowlist `:4784`** → per-context mode+detection (single-query
`:4808/:4809`, cron `:4876/:4878`). The allowlist precedes detection but *follows*
the hardline / sudo / deny floors.

### 3. The DEFAULT/multiplexer profile — `command_allowlist: []`, the in-gateway floor

The render forces the DEFAULT profile's top-level `command_allowlist` to `[]`
(explicit empty), overriding any declared value. This is the in-gateway fail-safe
floor — see the process-global boundary below.

## Deploying the managed fragment

The rendered `<out_root>/managed/config.yaml` is **not** installed by
`hermes onboard coworker` — that verb renders into a `TemporaryDirectory`
(`plugins/nv-coworker-compose/__init__.py` `_run_onboard`) and installs only the
coworker TYPE profiles, so the temp dir (and the fragment in it) is discarded when
onboarding returns. Always render with the **persistent** path and deploy the
fragment yourself:

```bash
hermes coworker compose coworker-types.yaml --out ./fleet-out
# → ./fleet-out/<profile>/…  and  ./fleet-out/managed/config.yaml
```

Install the fragment to the managed directory. `get_managed_dir()`
(`hermes_cli/managed_scope.py:52-71`) resolves `$HERMES_MANAGED_DIR` first
(`:65-68`), else `/etc/hermes` (`:33,:71`).

**Deploy it merge-preserving and atomically — never blind-overwrite.** The managed
`config.yaml` is a single machine-wide file that may already carry unrelated
managed policy from other fleet rows, so replacing the whole file would drop that
policy. Merge only the rendered `approvals.*` block over whatever is already
present, then rename atomically so a concurrent reader never sees a truncated
file:

```bash
# MANAGED=/etc/hermes (default) or "$HERMES_MANAGED_DIR" when set.
python3 - "$MANAGED/config.yaml" ./fleet-out/managed/config.yaml <<'PY'
import os, sys, tempfile, yaml
target, fragment = sys.argv[1], sys.argv[2]
existing = {}
if os.path.exists(target):
    with open(target, encoding="utf-8") as fh:
        existing = yaml.safe_load(fh) or {}
frag = yaml.safe_load(open(fragment, encoding="utf-8")) or {}
# recursive deep-merge: the fragment's approvals.* keys win per leaf; every
# unrelated managed key already in the file is preserved.
def merge(base, over):
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = merge(out.get(k), v) if k in out else v
        return out
    return over
merged = merge(existing, frag)
d = os.path.dirname(target) or "."
os.makedirs(d, exist_ok=True)
fd, tmp = tempfile.mkstemp(dir=d, prefix=".config.", suffix=".yaml")
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    yaml.safe_dump(merged, fh, sort_keys=False)
os.replace(tmp, target)      # atomic on every OS
PY
# then root-own it so a coworker cannot edit it (see below):
sudo chown root:root "$MANAGED/config.yaml" && sudo chmod 0644 "$MANAGED/config.yaml"
```

After installing, `hermes` invalidates its managed cache on the next
config read (`invalidate_managed_cache`, `hermes_cli/managed_scope.py:74-78`;
`_load_config_impl` folds the managed file's mtime/size into the cache signature
and applies the overlay, `hermes_cli/config.py:3936-4053`, deep-merge managed-wins
at `apply_managed_overlay` `:137-176`).

Root-own the file (`0644`, owned by `root`) so a coworker cannot edit it — the
same immutability the ISO-F13 invariant relies on (no profile mounts
`$HERMES_HOME`).

### The DEFAULT (launch) profile config is a separate deploy step

`hermes onboard coworker` renders in a `TemporaryDirectory` and installs only the
coworker TYPE profiles, so neither the managed fragment (above) NOR the rendered
DEFAULT profile config is deployed by onboarding. The DEFAULT profile's
`command_allowlist: []` is the in-gateway fail-safe floor (next section), so it
must reach the gateway's launch profile: render with `hermes coworker compose`
and apply `./fleet-out/default/config.yaml` to the launch profile (the gateway
root, profile name `default`) — then restart the gateway so the process reloads
`_permanent_approved` from the empty allowlist at import. Verify with
`hermes -p default approvals test` (or a `python3 -c` over the merged config) that
the launch profile's `command_allowlist` is `[]` before trusting the in-gateway
floor.

## The native process-global allowlist boundary (why DEFAULT is empty)

The permanent `command_allowlist` is consumed through a **process-global** set
`_permanent_approved` (`tools/approval.py:3158-3159`), populated by
`load_permanent_allowlist()` which **unions, never replaces**
(`:3056-3065`), loaded **once at module import** from the launching process's
`HERMES_HOME` (`:5970-5971`), and never re-loaded per turn or per profile. This
has two consequences:

- **Separate-process workers honor their own allowlist.** The kanban dispatcher
  spawns each worker as a `hermes -p <profile> chat -q` subprocess
  (`hermes_cli/kanban_db.py:10726`) with its own `HERMES_HOME` → its own
  `_permanent_approved` at import. `single_query_mode` governs it (the `-q` path
  exports `HERMES_SINGLE_QUERY_SESSION=1`, `cli.py:22013,22021`, comment names
  kanban workers `:22026`). The rendered per-role allowlist is honored here.
- **In-gateway multiplex sessions read the LAUNCH profile's allowlist, not their
  own.** Multiplex cron runs each secondary profile inside the shared gateway
  process via `set_hermes_home_override` + `cron_tick`
  (`cron/scheduler_provider.py:729-754`). That override re-scopes the *fresh-config*
  readers — `approvals.deny` and `cron_mode`/`unattended_mode` are per-profile-
  correct in-gateway — but it does **not** re-scope the module-global
  `_permanent_approved`.

The render answers this **without a core change**: rendering the DEFAULT
(launch/multiplexer) profile's `command_allowlist` empty means the gateway
process's `_permanent_approved` is empty at start, so in-gateway multiplex
cron/webhook/api sessions find nothing to bypass at the allowlist short-circuit
(`tools/approval.py:4784`) and fall through to the deny resolvers (fresh/managed =
`deny`) → **denied (fail safe)**. Non-empty per-role allowlists live only on the
separate-process worker profiles, where they are honored with no cross-profile
leak.

**Residual (a filed P8 core ask, not yet built).** Because `_permanent_approved`
is process-global, a human choosing *"always"* in one interactive profile
(`approve_permanent` + `save_permanent_allowlist`, `tools/approval.py:5300-5303`)
widens the in-gateway bypass set until the gateway restarts, and a per-profile
bypass allowlist is still not honored for in-gateway sessions. The DEFAULT-empty
render makes this **fail safe** today; the per-profile-honoring fix (key
`_permanent_approved` by `hermes_home_key()`, `hermes_constants.py:142`, lazy-load
per active home, make `load_permanent_allowlist()` REPLACE that home's bucket even
when empty) is a generic core widening filed for a future release.

## NanoClaw approvals primitive → native term map

This CONFIGURE row ports NanoClaw's approvals primitive onto Hermes' native gate;
it renders policy, it does not build a new engine.

| NanoClaw primitive | Native Hermes equivalent |
|---|---|
| approver picking | the orchestrator's interactive Bot Chat human gate (fleet mode stays `smart`, so a human-present session prompts) |
| reject-with-reason | `/deny <reason>` → the recorded `_ApprovalEntry.reason` |
| ghost / timeout finalize | the gateway approval timeout, fail-closed — *"Silence is not consent"* (`tools/approval.py:3955`) |
| unattended deny | the three resolvers `cron_mode` / `unattended_mode` / `single_query_mode`, all `deny` |

**Scope boundary.** This row delivers the *refused instantly* half — the rendered
config that makes an unattended worker DENIED rather than held. The *comments on
its card* and *hands to the orchestrator's Bot Chat* halves are realized by other
surfaces (the kanban dispatcher; the interactive orchestrator profile + rooms),
not by this rendered config.

## Escape hatch (documented, not shipped)

A future release wanting unattended approval holds delivered out-of-band (e.g.
DM'd) can register an approval transport via
`PluginContext.register_approval_transport` (`hermes_cli/plugins.py:1740`) /
`security.approval.transport` (`hermes_cli/config_defaults.py:2671-2673`). It is
**presentation-only** — it "cannot detect, suppress, or auto-approve commands
outside a correlated human response" (`hermes_cli/config_defaults.py:2665-2670`),
so it structurally cannot implement "deny when unattended" (that is the native
resolvers' job). **No transport plugin ships for O1–O8.**

## Checklist across a re-pin / migrate

- **Re-pin:** re-render and re-deploy the managed fragment; the six values are
  pinned explicitly, so a default flip in the new tree does not change them, but a
  new key would only take effect once re-deployed.
- **`hermes config migrate`:** the per-role `command_allowlist` and
  `approvals.deny` in a profile config survive migration unchanged (no migration
  touches them, `hermes_cli/config_migrations.py`) — verified by the GOV-F23
  acceptance test's migrate-survival criterion.
- **Never** set any resolver or `approvals.mode` to `approve` in a rendered file;
  the render refuses a coworker type named `managed` (managed-fragment dir
  collision) and rejects a malformed `command_allowlist` / `approvals.deny`.
