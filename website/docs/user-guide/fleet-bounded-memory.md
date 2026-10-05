---
sidebar_position: 8
title: "Fleet bounded memory"
description: "How a coworker fleet keeps always-loaded context bounded — the curator for the skills index, per-agent memory caps, and native in-turn overflow consolidation"
---

# Fleet bounded memory

A coworker fleet composed by `nv-coworker-compose` renders one profile per role
(the DEFAULT multiplexer plus every coworker). Each profile carries its own
always-loaded context: the system prompt, the agent-created **skills index**, and
the curated memory files. Left unmanaged, that context grows without bound — the
skills index accumulates near-duplicate agent-created skills and the memory files
grow with every write — which inflates every turn's prompt and cost.

This page describes how the fleet keeps always-loaded context bounded. It is the
Hermes-native realisation of what the port calls **OKF synthesis**: consolidate
and prune accumulated knowledge on a gated schedule, with rollback safety and no
destructive deletion. There are three complementary legs, only the first of which
this row configures.

## The curator — skills-index hygiene

The [curator](/user-guide/features/curator) is Hermes' auxiliary-model maintenance
pass over the **agent-created skills** index — the part of always-loaded context
that grows as the self-improvement loop saves new skills. It maps directly onto
OKF synthesis: it runs on a per-role schedule, applies deterministic
`active → stale → archived` transitions, optionally runs an LLM consolidation
("umbrella-building") pass, and **never auto-deletes** — the worst outcome is a
recoverable archive. Every real pass first attempts a timestamped snapshot of the
skills tree, so a pass can be rolled back with `hermes curator rollback`.

The curator is **eventual** hygiene: scheduled consolidation and archival, not a
hard per-turn cap. It complements — it does not replace — the hard bounds in the
other two legs below.

### What the render enforces, and what it carries through

`nv-coworker-compose` writes a `curator` block into every rendered profile's
`config.yaml`. It splits into two kinds of key.

**Safety invariants — forced on every profile, fleet-wide.** These are enforced
in the render (not hand-authored into a spine, which a re-pin or a new coworker
type could silently omit or set unsafe):

| key | rendered value | why it is a fleet invariant |
|---|---|---|
| `curator.enabled` | `true` | The skills-index hygiene leg only runs when the curator is enabled; forced explicitly so a re-pin to a tree that defaults it off cannot silently leave a profile's skills index unbounded. |
| `curator.backup.enabled` | `true` | Each real pass attempts a pre-run snapshot, making the pass rollback-able. The snapshot is best-effort (it never blocks the run), so this guarantees an *attempt*, not survival of a disk failure. |
| `curator.backup.keep` | `max(declared, 5)` | A **floor**, not a fixed value: a spine that legitimately retains a deeper history is preserved, while a below-floor / `0` / absent value is raised to `5`. A fixed `5` would delete a spine's older snapshots; without any floor the runtime clamps a raw `0` to `1`, which is not a deliberate fleet retention depth. |

**Per-role tuning — carried through from the resolved spec, never forced.** The
render does not invent these values (it is a generic composer over an arbitrary
`coworker-types.yaml`, so it cannot know role semantics); they flow from each
profile's resolved spec (a spine's, a type's, or `default_config`'s `curator:`
block) and the render preserves them unchanged:

| key | default | authoring guideline |
|---|---|---|
| `curator.interval_hours` | `168` (7 days) | How often the curator may run for this role. Raise it for a role that rarely creates skills; lower it for a prolific one. |
| `curator.min_idle_hours` | `2` | Only run when the role has been idle at least this long, so a pass never competes with active work. |
| `curator.consolidate` | `false` | Set `true` **only** for roles whose skill sets actually overlap — consolidation costs an aux-model pass every run, so it is wasted on a role with a narrow, non-overlapping skill set. |
| `curator.prune_builtins` | `true` | Whether unused bundled built-in skills may also be archived for this role (hub-installed skills are always exempt). Set `false` to keep all built-ins for a role. |

A role whose spec leaves the tuning unset gets the safety floor plus these sane
defaults, which is a valid per-role tuning.

## Memory caps — the hard always-loaded bound

The curated memory files (`MEMORY.md` / `USER.md`) are bounded by two hard
character caps that `nv-coworker-compose` also floors per profile:

- `memory.memory_char_limit` — the cap on the always-loaded agent memory file.
- `memory.user_char_limit` — the cap on the always-loaded user memory file.

These are a hard write-capacity gate, not eventual hygiene: an `add` that would
push a memory file over its cap is refused and the current entries are returned
for in-turn consolidation (see below), so the memory leg of always-loaded context
is bounded independently of any scheduled pass. They are configured by the sibling
row MEM-F41 and are named here so the bounded-memory story is complete.

## Native in-turn overflow consolidation

The third leg is stock Hermes and needs no configuration. When a memory write
would overflow its cap, the model is handed the current entries **in-turn** and
told to consolidate on the spot, before the write is accepted — this native
in-turn memory-overflow consolidation is bounded per turn
(`_MAX_CONSOLIDATION_FAILURES_PER_TURN = 3`) so it can never loop. Post-turn,
automatic memory and skill saves are handled by `background_review`, which is
enabled by default and can be turned off with `auxiliary.background_review.enabled: false`.
The in-turn overflow consolidation is always on and activation-independent.

Together these three legs — the curator (skills-index hygiene), the MEM-F41
memory caps, and native in-turn overflow consolidation plus post-turn
`background_review` — are the fleet's bounded-always-loaded-memory story. There is
deliberately **no** `okf-gate.py` script, no `context_from` chain, and no second
synthesis loop: the 2026-09-08 fleet-topology re-baseline dropped them in favour
of this native configuration.

## The operator's view and recovery

The curator's state and history are inspected and recovered with the stock CLI
(no fleet-specific command):

```bash
hermes curator status     # last run, next eligible run, per-skill state
hermes curator rollback   # restore the skills tree from a pre-run snapshot
```

Run these under the profile you want to inspect (`hermes -p <profile> curator
status`). `rollback` restores from the most recent `backup.enabled` snapshot, which
is why `backup.enabled: true` and a sane `backup.keep` are enforced fleet-wide.

## Activation under the one-gateway multiplex topology

The curator reads its config and state through the profile's own `HERMES_HOME`, so
the rendered per-profile `curator` block takes effect for whatever process runs
under that profile. Two native call sites fire it, both internally gated by
`interval_hours`: CLI startup, and the shared gateway's housekeeping tick.

Under the fleet's one-gateway multiplex topology the housekeeping tick currently
runs the curator only in the launch/DEFAULT profile's scope, so a **coworker's**
curator fires when that coworker's own process runs — an interactive
`hermes -p <coworker>` session — rather than from the shared gateway. This does
not make the rendered config inert (it is read wherever the profile's own process
runs the curator), and it does not affect the hard always-loaded bound, which is
the memory caps plus native in-turn overflow consolidation (both
activation-independent). Generalising the shared gateway's housekeeping tick to
run every served profile's curator inline is a tracked upstream (P8) ask; it is
not required for the skills-index leg to work per profile.

## See also

- [Curator](/user-guide/features/curator) — the skills-index maintenance pass this fleet enables
- [Memory](/user-guide/features/memory) — how the always-loaded memory files and background review work
