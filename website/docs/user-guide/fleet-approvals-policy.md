---
sidebar_position: 75
title: "Fleet approvals policy (on-call runbook)"
description: "How nv-coworker-compose pins the fleet-safe approvals policy — the machine-wide managed fragment, the per-role command_allowlist / render-enforced approvals.deny floor, and the DEFAULT-empty in-gateway launch baseline — how to deploy the managed fragment, the native process-global allowlist boundary, and the NanoClaw approvals term map."
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

:::warning Scope — this is a CONFIG row, not the full unattended-command guarantee
GOV-F23 pins the fleet-safe approvals **config** and a **best-effort in-surface
`approvals.deny` floor** for the common, combined-flag and global-option
force-push / tag / release spellings. It does **NOT** by itself meet the
unattended-*dangerous-command* invariant. Two gaps remain open and are owned by the
dedicated core-enforcement row **`GOV-ENF`** (with an allied `P8` core ask), taking
**no safety-completion credit** here:

- **Detector-evading / shell-wrapped spellings** slip the anchored `fnmatch`
  block-list (`cd repo && git tag v1`, `env gh release …`, `command git push origin
  +main:main`, a global option splitting `git push`, a combined `-f` cluster of 4+
  chars) and are also missed by the core dangerous-command detector — so they are
  **permitted past the native guard** when the Tirith layer allows or is absent
  (recorded honestly by **AC-GOV-F23-7**, a present-and-labelled fail-open, asserting
  the authorization DECISION only against a hermetic fixture — never an actual
  outbound).
- The **cross-profile `command_allowlist` union leak** for in-gateway multiplex
  sessions (see the process-global boundary section) — recorded by **AC-GOV-F23-4b**.

Until `GOV-ENF` lands, the compensating control is a **runtime/deployment ACL** that
contains each unattended identity's credential at the remote (force-update / tag /
release denied server-side regardless of the local guard). See *Scope and limits*
below.
:::

## What the render pins, and where

Hermes' approval keys are all **top-level `approvals.*`** (plus the top-level
`command_allowlist`); `security.approval` holds only the presentation-only
`transport` / `transport_fallback` (`hermes_cli/config_defaults.py:2665-2674`).
The render splits them into two immutable scopes.

### 1. The machine-wide managed fragment — the fleet-uniform policy

`nv-coworker-compose` emits one file, `<out_root>/managed/config.yaml`, carrying
the eight fleet-uniform keys, written **explicitly and never by assumption** so a
re-pin or an upstream default flip cannot silently loosen them:

| key | rendered value | reader (release tree) |
|---|---|---|
| `approvals.mode` | `smart` | `tools/approval.py:3462` (via `_get_approval_config`) |
| `approvals.timeout` | `300` | `tools/approval.py:3511` |
| `approvals.cron_mode` | `deny` | `tools/approval.py:3539` |
| `approvals.single_query_mode` | `deny` | `tools/approval.py:3552` |
| `approvals.unattended_mode` | `deny` | `tools/approval.py:3571` |
| `approvals.denial_breaker_threshold` | `3` | `tools/approval.py:2760` |
| `security.approval.transport` | `builtin` | `tools/approval.py:4303-4316` (human-approval / gateway path only) |
| `security.approval.transport_fallback` | `deny` | `tools/approval.py:4303-4316` (human-approval / gateway path only) |

The two `security.approval.*` keys are **presentation-path fail-closed hardening**,
NOT part of the unattended fail-safe: the transport is read only on the
human-approval / gateway path (`tools/approval.py:4303-4316`, called from
`:5138/:5631`, both *after* the non-interactive block), never on the unattended deny
path. Pinning them immutable denies flipping `transport_fallback` to `builtin`
(which would re-route a failed plugin transport to a built-in surface instead of
denying), so the human-approval path stays fail-closed.

Every value equals the stock default (the six `approvals.*` at
`hermes_cli/config_defaults.py:2558-2576`, the two `security.approval.*` at
`:2671-2674`), so **the fleet is no more permissive if the fragment is not yet
installed**: stock Hermes already denies **recognized** unattended dangerous
commands (deny-if-detected) and fails the transport closed — fragment-absence safety
only, not a claim every dangerous spelling is blocked (the AC-7 fail-open class is
unchanged either way). The fragment's value is (a) a single machine-wide source of
truth (DRY across N profiles) and (b) root-owned immutability — placing
`approvals.mode` in managed scope makes an interactive `/approvals` mode change
refuse with *"Approval mode is managed and cannot be changed."*
(`hermes_cli/approval_mode.py:54-66`, the `set_config_value` raise at `:63`). These
eight keys live **only** in the managed fragment; they are stripped from every
rendered profile config (the disjoint split), emptied `approvals` /
`security.approval` / `security` mappings pruned so no empty dict is written and an
unrelated `security.*` sibling survives.

### 2. `command_allowlist` (per-role passthrough) + `approvals.deny` (render-enforced floor)

Each profile config carries two per-profile keys — one a spec passthrough, one a
render-enforced fleet floor:

- a top-level **`command_allowlist`** — a **per-role spec passthrough**: the exact
  flagged-dangerous verbs that role legitimately runs (the fixer's build/test verbs,
  `git reset --hard*`, …), declared in the coworker type's `config:` block and passed
  through unchanged (except the DEFAULT profile's, forced to `[]`). It is read
  TOP-LEVEL (`config.get("command_allowlist")`, `tools/approval.py:3188`); a nested
  `agent.command_allowlist` has **no reader** and would be silently inert. It is the
  approval-bypass list, checked *before* dangerous-command detection (short-circuit
  at `tools/approval.py:4784`, ahead of every `detect_dangerous_command` call).
- an **`approvals.deny`** floor — **render-ENFORCED, not a per-role declaration.**
  The render **OVERWRITES** `approvals.deny` on *every* rendered profile (builder,
  reviewer, DEFAULT) with exactly the fixed 12-glob `_GOV_APPROVALS_DENY_FLOOR`,
  discarding whatever the coworker type or spine declared — so a spec that declares a
  weak or absent deny still ships the full floor (a fleet-safety property the render
  guarantees rather than one each type must remember to repeat identically). The 12
  globs (order-tolerant `fnmatch`, matched by `_match_user_deny_rule`,
  `tools/approval.py:809`/`:821`, blocked at `:826-838`, wired *before* the yolo/off
  bypass at `:4772`, so a floor verb can never be allowlisted):

  ```
  git *push* --forc*         # --force / --force-with-lease, any argument order
  git *push* -f              # standalone -f at command end
  git *push* -f *            # standalone -f mid-command
  git *push* -f[!- ]*        # force cluster, f-first (-fu, -fq, -fuq…)
  git *push* -[!- ]f*        # force cluster, f-second (-uf, -qf, -ufq…)
  git *push* -[!- ][!- ]f*   # force cluster, f-third (-uqf)
  git push* *+*              # +refspec force-push, unquoted or shell-quoted
  git -*push* *+*            # +refspec force-push behind a git global option
  git tag *                  # tag create / delete / move
  git -* tag *               # tag verb behind a git global option
  gh release *               # gh release create / delete / edit
  gh -* release *            # gh release verb behind a gh global option
  ```

  Each cluster glob uses the `[!- ]` (not-dash, not-space) class so `*` cannot cross
  a space into a branch name — a naive `git *push* -*f*` would over-block a normal
  `git push -u origin fix`. This is a **best-effort in-surface backstop**, not an
  exhaustive detector: a shell-wrapped or chained spelling still slips this anchored
  block-list (AC-7 / `GOV-ENF`, above).

Guard order (`check_all_command_guards`, `tools/approval.py:4730`): hardline
`:4753` → sudo `:4763` → `approvals.deny` `:4772` → yolo/off `:4780` →
**permanent allowlist `:4784`** → per-context mode+detection (single-query
`:4808/:4809`, cron `:4876/:4878`). The allowlist precedes detection but *follows*
the hardline / sudo / deny floors.

### 3. The DEFAULT/multiplexer profile — `command_allowlist: []`, the in-gateway launch baseline

The render forces the DEFAULT profile's top-level `command_allowlist` to `[]`
(explicit empty), overriding any declared value, and writes the same enforced
12-glob `approvals.deny` floor there as on every other profile. The empty allowlist
is the in-gateway **launch baseline** (not steady-state isolation) — see the
process-global boundary below.

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
policy. Merge the full rendered eight-key fragment (both the `approvals.*` and the
`security.approval.*` keys) over whatever is already present, then rename atomically
so a concurrent reader never sees a truncated file:

```bash
MANAGED="${HERMES_MANAGED_DIR:-/etc/hermes}"   # get_managed_dir() resolution
PYTHON="$(command -v python3)"
# sudo because the default managed dir (/etc/hermes) is root-owned; drop sudo
# only when $HERMES_MANAGED_DIR points at a dir you already own.
sudo "$PYTHON" - "$MANAGED/config.yaml" ./fleet-out/managed/config.yaml <<'PY'
import os, sys, tempfile, yaml
target, fragment = sys.argv[1], sys.argv[2]
existing = {}
if os.path.exists(target):
    with open(target, encoding="utf-8") as fh:
        existing = yaml.safe_load(fh) or {}
frag = yaml.safe_load(open(fragment, encoding="utf-8")) or {}
# recursive deep-merge: the fragment's eight keys win per leaf; every
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
`command_allowlist: []` is the in-gateway launch baseline (next section), so it
must reach the gateway's launch profile: render with `hermes coworker compose`
and apply `./fleet-out/default/config.yaml` to the launch profile (the gateway
root, profile name `default`) — then restart the gateway so the process reloads
`_permanent_approved` from the empty allowlist at import. Verify with
`hermes -p default approvals test` (or a `python3 -c` over the merged config) that
the launch profile's `command_allowlist` is `[]` before trusting the in-gateway
launch baseline.

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

The DEFAULT-empty render sets only the **in-gateway LAUNCH BASELINE**, without a
core change: rendering the DEFAULT (launch/multiplexer) profile's
`command_allowlist` empty means the gateway process's `_permanent_approved` is empty
**at start**, so at launch in-gateway multiplex cron/webhook/api sessions find
nothing to bypass at the allowlist short-circuit (`tools/approval.py:4784`) and fall
through to the deny resolvers (fresh/managed = `deny`). Separate-process
`hermes chat -q` workers keep their own `_permanent_approved` at import, so their
per-role allowlists are honored with no cross-profile leak.

**This is a launch baseline, NOT steady-state isolation — the cross-profile union
leak stays open (AC-4b, no isolation credit).** `load_permanent_allowlist()`
**UNIONs** each profile's allowlist into the ONE shared process-global set and never
replaces it (`tools/approval.py:3056-3065`), and the allowlist short-circuit
(`:4784`) precedes the cron deny (`:4876`). So once *any* in-gateway profile's
session init unions its allowlist in — or a human chooses *"always"* in one
interactive profile (`approve_permanent` + `save_permanent_allowlist`, `:5300-5303`)
— that entry is visible to every other in-gateway session and is **approved before
the cron/unattended deny even runs**, until the gateway restarts. The empty DEFAULT
does not close this; it is a **core behavior GOV-F23 cannot fix**, owned by
`GOV-ENF` / the `P8` core ask (key `_permanent_approved` by `hermes_home_key()`,
`hermes_constants.py:142`, lazy-load per active home, make
`load_permanent_allowlist()` REPLACE that home's bucket even when empty). GOV-F23
takes **no isolation credit** for the in-gateway multiplex path.

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

## Scope and limits

GOV-F23 is a **CONFIG row**. It pins the fleet-safe approvals config, the eight
managed keys, and the best-effort in-surface `approvals.deny` floor — and it
**does not** by itself meet the unattended-*dangerous-command* invariant. Two gaps
stay open, both owned by the dedicated core-enforcement row **`GOV-ENF`** (with the
allied `P8` core ask), and GOV-F23 takes **no safety-completion credit**:

1. **Detector-evading / shell-wrapped spellings** (AC-GOV-F23-7). The core
   dangerous-command detector's force-push patterns match only `--forc*` and a
   standalone `-f` (`tools/approval.py:1227-1228`) and have **no `git tag` /
   `gh release` / `+refspec` entry anywhere** (`:945-1271`); the anchored `fnmatch`
   deny floor closes the direct, combined-flag and global-option forms but not a verb
   split by a shell wrapper / chain / var-prefix (`cd repo && git tag v1`,
   `command git push origin +main:main`, a global option splitting `git push`). The
   robust fix is a segment/argv-aware pre-execution matcher plus detector coverage
   for tag/release/+refspec — both **core changes**, `GOV-ENF`.
2. **Cross-profile `command_allowlist` union leak** for in-gateway multiplex sessions
   (AC-GOV-F23-4b) — see the process-global boundary section; the `P8` core ask.

**Runtime/deployment compensating control (Ship gate B — a deploy precondition, not
a merge blocker).** Before any fleet identity makes a real-credential **unattended**
run against a target repo, contain that identity's credential at the remote via a
deployment ACL, so the irreversible-outbound operations AC-7 can let slip locally are
still **denied server-side**:

- **Identities:** every non-orchestrator profile that runs unattended (the coworker
  roles — `builder`, `reviewer`, and any `hermes chat -q` worker the board
  dispatches), each under the exact credential it runs with. The elevated
  orchestrator profile is the human-gated escalation target (topology rule 4), out of
  scope.
- **Negative operations, each expected DENIED at the remote regardless of the local
  guard:** create/update/delete a release; create/update/delete a tag (incl. force
  `-f`); force-update or delete a protected ref (`+refspec`, `--force`, delete);
  bypass a repo ruleset / required review; modify branch- or tag-protection settings
  (each failing with HTTP 403 / protected-ref rejection). If ordinary repo-write
  inherently retains release/tag capability for an identity, run that identity under a
  narrower brokered token instead.
- **`GOV-ENF` is the lifter:** the parsed, fail-closed pre-execution matcher is what
  lets this runtime containment be relaxed; until it lands, the containment stays in
  force.

## Escape hatch (documented, not shipped)

A future release wanting unattended approval holds delivered out-of-band (e.g.
DM'd) can register an approval transport via
`PluginContext.register_approval_transport` (`hermes_cli/plugins.py:1740`) /
`security.approval.transport` (`hermes_cli/config_defaults.py:2671-2673`). It is
**presentation-only** — it "cannot detect, suppress, or auto-approve commands
outside a correlated human response" (`hermes_cli/config_defaults.py:2665-2670`),
so it structurally cannot implement "deny when unattended" (that is the native
resolvers' job). **No transport plugin ships for O1–O8.** Because GOV-F23 pins
`security.approval.transport: builtin` / `transport_fallback: deny` in the managed
fragment (§1), adopting such a transport is a deliberate managed-scope change (edit
the machine-wide fragment), not a per-profile config edit a coworker could make.

## Checklist across a re-pin / migrate

- **Re-pin:** re-render and re-deploy the managed fragment; the eight values are
  pinned explicitly, so a default flip in the new tree does not change them, but a
  new key would only take effect once re-deployed.
- **`hermes config migrate`:** the per-role `command_allowlist` and
  `approvals.deny` in a profile config survive migration unchanged (no migration
  touches them, `hermes_cli/config_migrations.py`) — verified by the GOV-F23
  acceptance test's migrate-survival criterion.
- **Never** set any resolver or `approvals.mode` to `approve` in a rendered file;
  the render refuses a coworker type named `managed` (managed-fragment dir
  collision) and rejects a malformed per-role `command_allowlist`, a malformed
  `approvals.deny`, or a non-mapping `approvals` block.
