# Shared learnings & the learnings wiki

A fleet of coworker profiles keeps its collective, hard-won knowledge in **one git
"learnings clone"** — a single directory that every coworker reads and that the
orchestrator synthesises into a browsable wiki. Nothing is copied per bot: the clone is
surfaced through Hermes' stock [external skill directories](./features/skills.md#external-skill-directories),
so a learning authored once is visible to the whole fleet the moment it lands in the clone.

This is a **configuration** feature — it adds no new tool, hook, or system-prompt section.
It composes three surfaces Hermes already ships: `skills.external_dirs`, the bundled
`llm-wiki` skill, and the per-profile cron store. The `nv-coworker-compose` render wires
them onto each rendered profile so a fleet gets the whole design without hand-editing any
`config.yaml`.

## The single store

The clone is a plain directory whose `skills/` subtree holds the shared learnings as
skills (each a `SKILL.md`, invokable and versioned). Point every coworker profile at it
with one line under `skills`:

```yaml
skills:
  external_dirs:
    - /data/shared-learnings/skills   # the fleet's one learnings clone
```

Because `external_dirs` entries are **read-only references scanned in place**, a shared
learning:

- appears in every profile's **skill index** (name + one-line description in the system
  prompt) — the body is loaded on demand, never injected;
- is discoverable in the catalog (`skills_list`), openable with `skill_view`, and callable
  as a `/skill-name` slash command — no different from a local skill;
- is **not copied** into any profile's `~/.hermes/skills/` — one directory feeds N
  profiles.

Newly-authored skills always land in the authoring profile's own
`~/.hermes/skills/` — creation is profile-local by design, so a bot's fresh notes never
leak into the shared clone through the create path. Promotion into the shared clone is a
deliberate step (see [Promotion by PR](#promotion-by-pr)).

## Rendered by `nv-coworker-compose`

Set `shared_learnings_root` at the top of the fleet spec (`coworker-types.yaml`) to the
host path of the clone:

```yaml
shared_learnings_root: /data/shared-learnings
```

The render then enforces, on **every coworker (type) profile** — but never on the
DEFAULT/multiplexer gateway root, which is out of the coworker-learnings scope:

1. **`<clone>/skills` in `skills.external_dirs`** — appended if absent, and any
   pre-existing entry a spine declared is preserved.
2. **`WIKI_PATH` in `terminal.env_passthrough`** — each coworker's tool calls run in a
   per-profile sandbox that forwards only allowlisted env vars, so `WIKI_PATH` must be on
   the allowlist for the sandboxed wiki fold to see it.
3. **A `docker_volumes` mount of the clone root** at `/mnt/shared-learnings` —
   **read-write for the orchestrator** (which builds the wiki), **read-only for every
   other coworker** (defence in depth). The mount target stays outside `/workspace` on
   purpose: the sandbox treats any `:/workspace…` mount as an explicit workspace bind and
   would drop the coworker's default working-directory mount.

The external-dirs auto-mount already exposes `<clone>/skills` read-only inside the
sandbox, but only that subtree — not the clone root where the wiki (`index.md`, `raw/`,
`wiki/`) is written — which is why the explicit clone-root mount is required for the fold.

## The wiki layer

Fleet learnings grow into a large, unstructured pile. The bundled
[`llm-wiki`](./skills/bundled/research/research-llm-wiki) skill synthesises them into a
navigable wiki (sources → concepts → index). MEM-F43 **activates** that fold on the
orchestrator — and only the orchestrator — during fleet onboarding (`hermes onboard
coworker …`). Activation, over the installed profile homes and only when onboarding
otherwise succeeded:

- upserts `WIKI_PATH=/mnt/shared-learnings` into the orchestrator profile's `.env` (the
  container mount path — the fold runs inside the sandbox and reads the clone there;
  every other key in the `.env` is preserved);
- seeds the bundled `llm-wiki` skill into the orchestrator profile so it is loadable
  there; and
- creates exactly **one enabled, recurring `llm-wiki` agent cron job** whose prompt names
  the wiki mount. Re-running onboarding is idempotent — the named job is created once and
  the `.env` key is never duplicated.

Worker profiles and the DEFAULT gateway root get no wiki job and no `WIKI_PATH`: the fold
is an orchestrator responsibility. `WIKI_PATH` rides the profile `.env` (not a cron-job
field — jobs carry no env), forwarded into the sandbox by the `env_passthrough` allowlist
the render added and resolved there to the container mount.

The **quality** of the synthesised wiki is LLM-driven and is deliberately not asserted as
a guarantee; what the fleet configures and proves is that the fold is scheduled, scoped to
the orchestrator, and able to read and write the clone.

## Promotion by PR

The shared clone is a git repository. A coworker that wants to promote a local learning
into the fleet store proposes it as a change to the clone; the orchestrator commits and
pushes the clone, and that push is reviewed as an ordinary pull request. Learnings-as-
skills fit this model well — they are diffable, reviewable, and versioned, unlike opaque
memory rows.

Attribution rides the existing skill provenance: a skill records who created it
(`skill_provenance` / `SkillNode.created_by`), and the per-profile subdirectory convention
inside the clone (`skills/<author>/…`) keeps sources legible.

## The write model (read this before deploying)

`external_dirs` is a **discovery** mechanism, **not a write-protection boundary**. Two
facts follow:

- **Create is safe.** New skills resolve to the authoring profile's local dir, never into
  an `external_dirs` entry.
- **Edit-in-place is not natively blocked.** If the Hermes process can write the clone,
  `skill_manage` edit/patch/delete of an *existing* shared skill rewrites it **in place**
  in the clone. `external_dirs` and config do not stop this.

Because coworker tool sandboxes share one gateway process, filesystem permissions and the
read-only container mount **cannot** distinguish an orchestrator write from a worker write
for these host-side `skill_manage` writes. Shared-clone integrity is therefore delegated
to, in layers:

1. **filesystem permissions** on the clone (make it writable only to the promoting owner);
2. the **read-only container mount** for non-orchestrator coworkers (defence in depth for
   sandboxed access); and
3. a **per-profile write veto** (tracked as LOOP-F37) that denies a non-orchestrator
   `skill_manage` mutation whose resolved target lands inside the clone.

:::warning Deployment is gated on the LOOP-F37 veto
This shared-learnings configuration is complete, but **must not be deployed to a live
fleet** until the LOOP-F37 host-side-writer veto is extended to block *every*
`skill_manage` mutation form (`edit`, full and targeted `patch`, `delete`, `write_file`,
`remove_file`, `operations[]`) that resolves into the clone for a non-orchestrator
profile. Until then the shared clone is not write-protected against a worker's foreground
edits, and filesystem permissions alone cannot tell the orchestrator apart from a worker
inside the shared gateway process.
:::

## Cross-fleet isolation

Each fleet points at its **own** clone via its own `external_dirs` / `shared_learnings_root`.
Two fleets with different clones discover disjoint learnings — fleet A's skills never
appear in fleet B's catalog. Nothing is global; isolation is just "a different directory".
