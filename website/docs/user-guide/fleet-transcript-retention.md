---
sidebar_position: 71
title: "Fleet transcript retention (on-call runbook)"
description: "The four retention keys nv-coworker-compose pins on every fleet profile, why they are enforced unconditionally, and the pin-move / migration checklist that keeps task evidence on disk."
---

# Fleet transcript retention (on-call runbook)

This runbook covers how the fleet keeps conversation transcripts on disk and
searchable, and the operational steps that protect that guarantee across a
release re-pin and a `hermes config migrate`. It is scoped to fleets rendered by
the `nv-coworker-compose` plugin (the DEFAULT multiplexer profile plus every
coworker profile).

## The four pinned retention keys

`nv-coworker-compose` writes these four keys into **every** profile `config.yaml`
it renders — the DEFAULT multiplexer and every coworker alike — **explicitly and
never by assumption**. They are set to the fleet-safe value whether the input
spec omits the key or declares a different one, so neither an omission nor a
stale/unsafe declaration inherited from a spine can leave a profile unprotected.

| key | rendered value | what reads it | why this value |
|---|---|---|---|
| `sessions.auto_prune` | `false` | The startup prune sweep (`maybe_auto_prune_and_vacuum`, gated in `cli.py` and `gateway/run.py`). When `true`, it deletes the on-disk transcript files — `{sid}.json`, `{sid}.jsonl` and `request_dump_{sid}_*.json` — via `_remove_session_files`, not just database rows. | Keep transcripts on disk. Equal to the pin's schema default (`false`), but pinned **explicitly** so a re-pin to a tree whose default has flipped to `true` cannot silently start deleting task evidence. |
| `sessions.auto_archive` | `true` | `maybe_auto_archive` → `archive_stale_sessions`, gated in `cli.py` / `gateway/run.py` and on a gateway timer. | Safe active-list cleanup: any unpinned session idle for `sessions.auto_archive_days` (default `3`) — **including an unended one** — is soft-hidden from the default list, but its messages and transcript files stay on disk and `session_search`-able, and archiving is reversible. This soft-hide **does not shrink `state.db`** — only a prune + `VACUUM` reclaims space, which retention forbids. Differs from the pin default (`false`), so it is behaviorally load-bearing. |
| `compression.in_place` | `true` | The compaction path (`agent/conversation_compression.py`). | With `in_place: true`, a compaction rewrites the live message list on the same session id and **soft-archives** the pre-compaction turns instead of deleting them: summarized-away rows stay `session_search`-able as `active=0, compacted=1`, while verbatim carried-forward tail originals are hidden from search as `active=0, compacted=0` (their live copies preserve the content). Nothing is deleted. Equal to the pin default, pinned explicitly so a re-pin cannot drop it. |
| `checkpoints.auto_prune` | `false` | The checkpoint prune sweep (`maybe_auto_prune_checkpoints`, gated in `cli.py` / `gateway/run.py`). | The requirement's "explicit decision rather than the `true` default": keep checkpoints and prune them deliberately from this runbook. Differs from the pin default (`true`), so it is behaviorally load-bearing. |

Reader `file:line` citations for the pin (`v2026.8.31`) are recorded in the
MEM-F44 ADR. The prune/archive sweep relocated from `hermes_state.py` to
`hermes_state_maintenance.py` on upstream `main` after the pin — a detail to
re-confirm at each re-pin (below), not something this runbook or the render
imports.

`sessions.auto_archive_days` stays at its default (`3`). It is tunable per fleet
but is intentionally **not** pinned by the render — it is outside the requirement's
named-key set.

## Why the render enforces, and why `hermes config migrate` keeps the keys

The render writes each key with an override-or-insert (never `setdefault`), so a
spine that inherited an unsafe `sessions.auto_prune: true` is corrected rather
than passed through.

Those explicit values then **survive `hermes config migrate`**. `save_config`
folds every leaf literally present on disk into a preserved set whose
short-circuit fires *before* the "value equals the schema default" strip — so
even a default-equal explicit key (`sessions.auto_prune: false`,
`compression.in_place: true`) is kept verbatim rather than stripped. No migration
step at the pin reads, resets or removes `sessions.*`, `checkpoints.*`, or
`compression.in_place`. This is exactly why the render writes the keys
explicitly: an implicit (defaulted) value would not be preserved this way.

## Pin-move checklist (run after every re-pin)

After the operator moves the release pin (the fleet's re-pin step):

1. **Re-render every profile** from the current `coworker-types.yaml` with
   `nv-coworker-compose`.
2. **Re-assert the four keys** on the raw rendered `config.yaml` of the DEFAULT
   profile and every coworker (the MEM-F44 acceptance test does this
   mechanically — run it against the new pin).
3. **Check the upstream default for `sessions.auto_prune`.** Upstream `main`
   flipped it to `true`; if the new pin carries that flip, an un-pinned profile
   would begin deleting transcript + `request_dump_*` files on the next
   CLI/gateway start. The explicit `false` the render writes is what prevents
   this — confirm it is present.
4. **Re-confirm the sweep's module path.** The prune/archive sweep moved to
   `hermes_state_maintenance.py` on `main`; if the new pin includes that move,
   update any incident references (the render is unaffected — it writes config
   only and imports no sweep module).

## Config-migration irreversibility

The pinned release is schema version 39. Before adopting a newer pin, inspect
every intervening migration for steps that **rewrite profile files** and treat
those as irreversible. Upstream adds a `SOUL.md`-rewriting migration after the
pin (in the `_config_version` `39 → 41` range); when present in the target tree
it rewrites the `SOUL.md` of profiles that carry the legacy section it targets,
not unconditionally every profile — so verify what it touches in the specific
tree before running it. The DEFAULT profile lives at the `$HERMES_HOME` root;
each coworker lives under `$HERMES_HOME/profiles/<name>/`, and each has its own
`config.yaml` that migrates separately.

Fleet migration procedure:

1. **Back up**, at minimum:
   - DEFAULT: `$HERMES_HOME/config.yaml` and `$HERMES_HOME/SOUL.md`.
   - Each coworker: `$HERMES_HOME/profiles/<name>/config.yaml` and
     `$HERMES_HOME/profiles/<name>/SOUL.md`.
2. **Migrate each profile** (the config is per-profile, so migrate them one by
   one — `--profile`/`-p` selects the profile's `HERMES_HOME`):
   ```bash
   hermes -p default config migrate              # DEFAULT (multiplexer) profile — name it explicitly
   hermes -p <name> config migrate               # repeat for every coworker
   ```
3. **Re-render `SOUL.md` from the compose plugin** afterwards for any profile
   whose source-owned `SOUL.md` a migration changed, so the soul matches the
   composed source rather than the migrator's rewrite.

The four retention keys themselves are preserved by migration (above); the
irreversibility concern is the *`SOUL.md` rewrite*, not the retention keys.

## Deliberate pruning is a runbook action, never automatic

With `auto_prune: false` pinned on both sessions and checkpoints, the stores grow
until an operator prunes them by hand. Storage pressure is handled here, not by
flipping the pinned keys:

```bash
# Sessions — delete old ENDED sessions (this deletes on-disk transcripts):
hermes sessions prune --older-than 30        # e.g. older than 30 days
hermes sessions prune --source cron --older-than 60

# Checkpoints — prune the checkpoint store:
hermes checkpoints prune --retention-days 7
hermes checkpoints prune --max-size-mb 200
```

Prune narrowly (time window / source / title filters) so you remove only what you
mean to. `sessions.auto_archive: true` already keeps the active list tidy without
deleting anything, so day-to-day you should rarely need `sessions prune`.

> **Upstream ask (informational, P8).** Transcript-file deletion
> (`_remove_session_files`) is coupled to DB-row pruning in `sessions.auto_prune`.
> A fleet that wants to prune stale rows while retaining on-disk transcripts
> cannot today. The fleet's ask to upstream is to make transcript-file deletion
> opt-in **separately** from row pruning. This does not affect the runbook: pin
> `sessions.auto_prune: false` and prune deliberately.

## Adopted (no plugin ships for these)

Two adjacent capabilities are **already provided by stock Hermes** and are
configured, not built, by this fleet:

- **Fresh project memory per task.** With `worktree_sync: true` (the default),
  `hermes -w` and the `/worktree new` slash command base each task's worktree on
  the **freshly-fetched remote tip**, not the local clone's `HEAD`. No
  "refresh-clones" startup hook is needed or shipped.
- **Compaction-summary steering (optional seam).** If a fleet ever needs to steer
  what a compaction summary keeps, the seam is a memory provider's
  `on_pre_compress(messages)` or a `pre_llm_call` shell hook — **not** a
  PreCompact copy hook. Nothing in the current fleet requires it, so no such hook
  ships.

## See also

- [Sessions](./sessions.md) — session persistence, search, and the
  `sessions.auto_prune` / `retention_days` cleanup mechanics.
- [Checkpoints and rollback](./checkpoints-and-rollback.md) — the checkpoint
  store and `checkpoints.auto_prune`.
- [Context compression and caching](../developer-guide/context-compression-and-caching.md)
  — `compression.in_place` soft-archiving.
