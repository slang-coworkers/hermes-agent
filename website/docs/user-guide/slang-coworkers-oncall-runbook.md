---
title: slang-coworkers On-Call Runbook
description: Operating the slang-coworkers Hermes fleet — config migrations, fleet-wide updates, triage, uninstall, and the one-gateway multiplex model.
---

# slang-coworkers On-Call Runbook

Operational procedures for the slang-coworkers Hermes fleet. The fleet runs
**one gateway** — the default profile's — in Bot Mode, with every coworker as a
[profile](./profiles.md) of that gateway. Everything on this page is **native
Hermes** behaviour; nothing here is a slang-coworkers plugin or a core patch.
This page adopts and documents the native ops stack, and the hermetic
acceptance test `tests/hermes_cli/test_ops_f58a_acceptance.py` proves the
load-bearing behaviours.

All `file:line` references are relative to the pinned release tree
`NousResearch/hermes-agent` tag **`v2026.8.31`** (commit
`29112bef099274229cadff79cdff7bf7b99c4b77`). Where a symbol moved on `main`,
the tag path is authoritative here.

> Run every command **on the gateway host**, as the user that owns
> `$HERMES_HOME`. These are operator commands — an agent never invokes them,
> and they are inert inside a coworker sandbox.

---

## 1. Fleet topology (read this first)

- **One gateway per fleet.** The container runs the **default profile's**
  gateway with `gateway.multiplex_profiles: true` and a
  `multiplex_profile_allowlist` naming the coworker roster. Those keys are
  rendered onto the default profile by the `nv-coworker-compose` plugin
  (LOOP-F35) and read natively at `gateway/config.py:1283` and consumed at
  `gateway/run.py:2470`. OPS-F58.a does **not** re-render them.
- **Coworker s6 slots are left down by design.** `container_boot`
  (`hermes_cli/container_boot.py:95` `reconcile_profile_gateways`) autostarts
  **only** a slot whose per-profile `gateway_state.json`
  (`hermes_cli/container_boot.py:394`) records `desired_state: "running"`
  (`_AUTOSTART_STATES = frozenset({"running"})`, `hermes_cli/container_boot.py:39`,
  read via `_read_desired_state`, `:374`). A coworker profile left `stopped`,
  or with no `gateway_state.json` at all, is **registered but not started**.
  The result: **only the default multiplexer slot starts**; every coworker is
  served *through* that one gateway, not by a gateway of its own.
- **Supervision.** Host systemd services default to `Type=simple`; setting
  `gateway.systemd_watchdog_seconds` above zero switches the unit to
  `Type=notify` with `WatchdogSec` (`gateway/config.py:990-992`,
  `hermes_cli/gateway.py:3929-3932`). launchd uses `KeepAlive` on macOS.
  `gateway/shutdown_watchdog.py` is a hard-exit backstop: if a graceful
  shutdown wedges it dumps thread stacks and forces process exit (`os._exit`)
  so the supervisor can restart the gateway — it is not the component that
  performs the clean drain.

### Do not start a second gateway for a coworker

Under the s6 fleet image, `hermes -p <bot> gateway start` dispatches to the
service manager and returns (`hermes_cli/gateway.py:8141`
`_dispatch_via_service_manager_if_s6`). The s6-supervised `gateway run` child it
brings up then reaches the native **double-bind guard**
`_guard_named_profile_under_multiplexer` (`hermes_cli/gateway.py:6257`, invoked
from `run_gateway` at `:6439`). When
`named_profile_served_by_running_multiplexer()` (`:6181`) is true — i.e. the
default multiplexer is already serving that profile — the guard prints the
double-bind error and calls `sys.exit(GATEWAY_FATAL_CONFIG_EXIT_CODE)`
(`:6306`), where `GATEWAY_FATAL_CONFIG_EXIT_CODE = 78` (`EX_CONFIG`,
`gateway/restart.py:17`).

**Be precise about what fails and where.** This is a **hard error** in the
**supervised `gateway run` child** — the child exits `EX_CONFIG` (78) so the
second gateway never comes up. It is **not** a non-zero return from the
`gateway start` command itself; `start` has already dispatched to s6 and
returned. On-call staff should read "the second gateway refused to start
(EX_CONFIG 78 in the supervised child)", not "the `start` command exited 78".

**Deliberate override (rare — you must mean it).** The guard is not limited to
the s6-supervised child: `run_gateway` calls
`_guard_named_profile_under_multiplexer(force=force)` on **every** foreground
path (`hermes_cli/gateway.py:6426-6439`), so it fires under `--no-supervise`
too — `--force` is exactly what bypasses it (`if force: return`,
`hermes_cli/gateway.py:6270-6271`). To foreground a specific coworker's gateway
anyway, name the profile with the global `-p <bot>` flag and pass both flags:

```bash
hermes -p <bot> gateway run --no-supervise --force
```

`--no-supervise` (or `HERMES_GATEWAY_NO_SUPERVISE`) skips the `run`→s6 redirect
(`hermes_cli/gateway.py:8297-8306`) so `run_gateway` executes in the foreground;
`--force` then skips the double-bind guard inside it. The flag combination is
`hermes gateway run --no-supervise --force`, here scoped to the coworker with
`-p <bot>`. Both flags live on the `gateway run` parser
(`hermes_cli/subcommands/gateway.py:66`, `:76`); the `gateway start` parser has
**neither** (`hermes_cli/subcommands/gateway.py:102-103`). Bare
`hermes gateway run …` with no `-p` targets the **default** profile — under the
multiplexer that double-binds the default gateway's own tokens and sessions, so
always scope the override to the intended coworker and stop it when the debug
session is done.

---

## 2. Config migration — `hermes config migrate`

Hermes' config schema evolves through a **table-driven** migration registry
`MIGRATIONS` (`hermes_cli/config_migrations.py:869`) — an ascending list of
`(target_version, migrate_fn)` entries. It enforces a **support floor**
`SUPPORT_FLOOR_VERSION = 12` (`hermes_cli/config_migrations.py:53`): a config
from the floor up to the current top (12–39) is migrated up to the top; a
config already at the top is left unchanged, and the migrator never downgrades
a newer one. A config **below** the floor is refused and left byte-for-byte
untouched. At the pinned
tag the top version is **39** (`(39, _migrate_to_39)`,
`hermes_cli/config_migrations.py:892`; `DEFAULT_CONFIG["_config_version"] = 39`,
`hermes_cli/config_defaults.py:3964`). Floor enforcement and version stamping
live in `migrate_config` (`hermes_cli/config.py:2532`), not in the low-level
`run_migrations`.

Routine migration on a single profile:

```bash
hermes config migrate
```

### Irreversible pin-move procedure (39 → 41)

> ⚠️ Treat a base pin-move that crosses config version **39 → 41** as
> **irreversible**. On a base newer than the pin, the `_migrate_to_41`
> migration **rewrites `SOUL.md` in every profile** (`main` behaviour;
> `hermes_cli/config_migrations.py:_migrate_to_41` on the newer base — **not
> present at the pinned `v2026.8.31`, which tops out at 39**). This is a
> specified future pin-move step, documented now so it is in place when the
> base moves; it is not verified native behaviour at the current pin.

Because it rewrites `SOUL.md`, there is no clean rollback. Before moving the
base from a 39-era pin to one that contains 41:

1. **Take a consistent backup first with `hermes backup`.** Use
   `hermes backup -o /safe/path/pre-v41.zip` (`hermes_cli/subcommands/backup.py:26`),
   which snapshots each `state.db` through SQLite's `.backup()` API
   (`hermes_cli/backup.py:374-385`) — safe while the gateway is writing. Do
   **not** `cp -a`/`tar` a live `$HERMES_HOME`: `state.db` runs in WAL mode
   (`hermes_state.py:10`) and a raw copy can miss uncheckpointed WAL data or
   tear the DB (`hermes_state.py:691`). If you must raw-copy, stop every gateway
   first.
2. Move the base pin, then run `hermes config migrate` on each profile.
3. **Re-render `SOUL.md`** in every affected profile from the
   `nv-coworker-compose` plugin (the compose render is the source of truth for
   `SOUL.md`; the migration's rewrite must be reconciled against it).
4. Verify `_config_version` reached the new top on each profile and that
   `SOUL.md` matches the compose output.

---

## 3. Fleet-wide migration + safety net — `hermes update`

`hermes update` is the fleet-wide "migrations runner". After re-pinning, run it
**once on the gateway host**; it upgrades the whole fleet, not just the active
profile:

```bash
hermes update
```

What it does, in order:

1. **Best-effort per-profile snapshot.** Before mutating anything, it attempts
   a quick **snapshot** of the active profile and each configured named sibling
   (`hermes_cli/backup.py:1978` `create_pre_update_snapshots_all_profiles`;
   quick snapshots via `create_quick_snapshot`). This is **best-effort** — if
   backups are disabled or a snapshot fails, the update still proceeds; the
   snapshot does **not** block the update, so it is **not** a guaranteed
   backup.
2. **Sibling config migration.** It migrates each *configured named sibling
   profile's* `config.yaml` non-interactively up to the current version, and
   prints the profiles it migrated (`hermes_cli/update_cmd.py:229`
   `_migrate_sibling_profile_configs`, invoked at `:470`, successes printed at
   `:472`). The **active** profile is migrated by the normal single-profile
   path, not this sibling pass. A sibling that fails to migrate is **skipped**
   (left to the startup fallback) and logged only at debug level
   (`:280-287`, `:476-477`) — a failed sibling is not a blocking error.
3. **Cron-jobs restore safety net.** When a snapshot exists, each profile's
   `cron/jobs.json` is re-hydrated from **its own** snapshot if the update
   emptied it (`restore_cron_jobs_if_emptied`,
   `hermes_cli/update_cmd.py:488`; `restore_cron_jobs_all_profiles`, `:506`;
   `hermes_cli/backup.py:2009`). Healthy profiles are left untouched.

**On-call procedure:**

1. Take an **explicit `$HERMES_HOME` backup** first (the built-in snapshot is
   best-effort, not guaranteed).
2. Run `hermes update` from the gateway host.
3. Verify each profile's `_config_version` and its `cron/jobs.json` after the
   run — confirm the **cron** jobs are present (the **restore** safety net
   re-hydrates from the per-profile **snapshot** only if they were emptied).

---

## 4. State DB self-heals on open (no migrations runner)

Operators do **not** run a state-DB migrations runner. Every time a `state.db`
is opened, Hermes **declaratively reconciles** its columns
(`hermes_state_schema.py:701` `_reconcile_columns`) — a table missing a
reconcilable column has it re-added on open, with no version-gated migration
and no data loss (the design comment at `hermes_state_schema.py:969` notes it
is "impossible for reordered or inserted migrations to skip columns").
`SCHEMA_VERSION = 26` at the pin (`hermes_state_common.py:355`). If a DB looks
schema-stale, **just reopen it** (restart the gateway) — the open path repairs
it.

---

## 5. Logging — rotating + redacting

File logging is config-driven and rotates. The `agent.log` handler is a
`RotatingFileHandler` sized by `logging.max_size_mb` and `logging.backup_count`
(`hermes_logging.py:329-336`, read from config at `:964-965`; defaults
`max_size_mb: 5`, `backup_count: 3` at `hermes_cli/config_defaults.py:3041-3042`).
When secret redaction is enabled — the default — records are scrubbed by
`RedactingFormatter` (`agent/redact.py:1430`) before they reach the log file.
Redaction is **not** an absolute guarantee: it matches only known secret
patterns (`agent/redact.py:795`), and it can be turned off with
`security.redact_secrets: false` in `config.yaml` or `HERMES_REDACT_SECRETS=false`
(`agent/redact.py:68-77`). Treat log files as sensitive — an unrecognized
secret or PII can still land in them.

To change rotation, set the keys in the profile's `config.yaml` (not an env
var):

```yaml
logging:
  max_size_mb: 10
  backup_count: 5
```

Then restart the gateway so the handler is rebuilt at the new size.

---

## 6. Triage — `hermes doctor` and `hermes debug share`

**First, health-check the host:**

```bash
hermes doctor
```

`hermes doctor` (`hermes_cli/subcommands/doctor.py:12` parser,
`hermes_cli/doctor.py:1236` `run_doctor`) reports on s6 supervision, the
gateway service, and profiles — the fastest way to see whether the single
gateway and the coworker slots are in the expected state.

**Then, capture a redacted report to hand off — without publishing it:**

```bash
hermes debug share --local
```

`hermes debug share` (`hermes_cli/subcommands/debug.py:13` parser,
`hermes_cli/debug.py:846` `run_debug_share`) redacts by default. The
`--local` flag (`hermes_cli/subcommands/debug.py:55`) renders the report to
local output and **never uploads** it — the `upload_to_pastebin`
(`hermes_cli/debug.py:329`) path is only reached *without* `--local` (the
`--local` branch is at `hermes_cli/debug.py:854-874`). It is "no-report-upload"
rather than strictly network-free: before rendering, it runs a best-effort
sweep of previously-created pastes that can issue a `DELETE` to paste.rs
(`hermes_cli/debug.py:858`), and only when a pending-paste record exists. Use
`--local` when you need to attach a report to an incident ticket without
publishing the report itself.
Only drop `--local` (and never add `--no-redact`) if you have explicitly
decided the report may be uploaded.

---

## 7. Uninstall — always dry-run first

**Preview what would be removed, non-destructively:**

```bash
hermes uninstall --dry-run
```

`hermes uninstall --dry-run` (`hermes_cli/subcommands/uninstall.py:42`;
`run_uninstall` dry-run branch at `hermes_cli/uninstall.py:661-667`,
plan printer `_print_uninstall_dry_run` at `:798`) removes **nothing** — it
never reaches the destructive worker and prints a "would remove" plan.

A **real** uninstall removes more, and the mode matters. `hermes uninstall`
without `--full` keeps your config/data; `hermes uninstall --full` deletes
`$HERMES_HOME` entirely (`shutil.rmtree`, `hermes_cli/uninstall.py:971-985`).
Because `--dry-run` composes with `--full` (`run_uninstall` branches on
`dry_run` before doing anything, `hermes_cli/uninstall.py:661`; the plan printer
takes `full_uninstall`, `:809-818`), **preview the exact mode you will run** —
never review one plan and then execute a different mode:

```bash
hermes uninstall --full --dry-run     # preview the FULL removal plan
# ...only after reviewing THAT plan and taking a `hermes backup`:
hermes uninstall --full --yes
```

`--full` / `--gui` (`hermes_cli/subcommands/uninstall.py:23`, `:28`) confirm
interactively unless you pass `--yes` (`hermes_cli/subcommands/uninstall.py:39`).
Note the `--yes` fast path sets `remove_profiles=False`
(`hermes_cli/uninstall.py:676-691`), so it does **not** tear down named-profile
services/aliases — for a full fleet teardown run the interactive
`hermes uninstall --full` (no `--yes`) or clean those services first.

On-call rule: **always `--dry-run` first** (matching the mode you will run),
keep a backup, then run the real command deliberately.

---

## 8. Retention — see MEM-F44

Session/checkpoint retention (`sessions.auto_prune`, `checkpoints.auto_prune`)
and the transcript + `request_dump_*` sweep are owned by **MEM-F44**, not this
row. Note that `sessions.auto_prune` is `False` by default at the pinned tag
(`hermes_cli/config_defaults.py:3400`) but is flipped `True` on `main`, so a
config migration or pin-move must **re-assert** `sessions.auto_prune: false` on
the default (multiplexer) profile and on every coworker profile if the fleet
wants pruning off. That re-assertion is MEM-F44's render deliverable; this
runbook only cross-references it — see MEM-F44 for the retention policy and the
sweep behaviour.

---

## Quick reference

| Task | Command | Native anchor |
|---|---|---|
| Migrate one profile's config | `hermes config migrate` | `config_migrations.py:869`, floor `:53`, top 39 `:892` |
| Fleet-wide update + migrate siblings | `hermes update` | `update_cmd.py:229`, cron restore `:488`/`:506` |
| Health check | `hermes doctor` | `doctor.py:1236` |
| Local triage report (no report upload) | `hermes debug share --local` | `debug.py:846`, `--local` `:854-874` |
| Preview uninstall (match the mode) | `hermes uninstall --dry-run` / `--full --dry-run` | `uninstall.py:661`, `:809-818` |
| Real full uninstall (scripted) | `hermes uninstall --full --yes` | `subcommands/uninstall.py:39`; skips profiles `uninstall.py:676-691` |
| Force a foreground coworker gateway (override the double-bind guard) | `hermes -p <bot> gateway run --no-supervise --force` | guard `gateway.py:6426-6439`, exit 78 `:6306` |

**Reminders:** the fleet runs one gateway; **only the default** multiplexer
slot starts (coworkers left `desired_state != "running"`). Do not start a
second gateway for a coworker — `hermes -p <bot> gateway start` triggers the
supervised child's EX_CONFIG **hard error** (78); the deliberate override is
`hermes -p <bot> gateway run --no-supervise --force`. Treat the **39 → 41**
pin-move as **irreversible** (it rewrites `SOUL.md`): back up first with
`hermes backup`, migrate, re-render `SOUL.md`.
