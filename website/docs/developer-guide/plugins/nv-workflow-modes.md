# nv-workflow-modes — the ported plan-modes and /implement

`nv-workflow-modes` is a first-party directory plugin that ships the NanoClaw
workflow-command bodies stock Hermes has no builtin for — the `investigate`,
`review` and `research` plan-modes plus `/implement` — as `/slash`-invocable
**workflow-body skills**. Each one seeds a normal agent turn with a durable,
multi-step workflow; the mode is fixed by the command slug and the text typed
after the command is passed to the workflow as its user instruction.

Stock Hermes already ships the default plan mode as the builtin `/plan` and a
separate `/review` command (an independent subagent). Those are left untouched;
this plugin only fills the four residual bodies.

## Install

The plugin is opt-in. Enable it in `config.yaml`, then run the installer once for
the active profile:

```yaml
plugins:
  enabled:
    - nv-workflow-modes
```

```bash
hermes workflow-modes install     # copy the four bodies into $HERMES_HOME/skills
hermes workflow-modes list        # show each mode's install status
```

The installer copies the four bundled `skills/<mode>/SKILL.md` bodies into the
active profile's `get_hermes_home()/skills/<mode>/`, marking each with a
`.installed-by` file that names this plugin. It is **idempotent** (re-running over
its own dirs reproduces byte-identical bodies) and **atomic**: if a mode path
already holds a skill this plugin did not create, the entire install is refused
and nothing is written — pass `--force` to replace it (its contents are lost).
After a successful install, run `/reload-skills` (or restart the gateway) so the
skill scanner mints the new `/slash` commands.

## The four ported modes

| command | kind | what the workflow body does |
|---|---|---|
| `/plan-investigate <topic>` | read-only | investigate a question/system and return an evidence-backed report |
| `/plan-review <target>` | read-only | review work inline and return findings by severity plus a verdict |
| `/plan-research <topic>` | read-only | research across multiple sources, synthesize, and state uncertainties |
| `/implement <task>` | execution | execute an approved plan: edit code, write/run tests, validate |

Because the full `SKILL.md` body is loaded into the turn verbatim, each mode is a
real, distinguishable workflow — not a stub — and the topic typed after the
command arrives as the skill's user instruction.

## Equivalence (NanoClaw plan-modes + /implement, mapped)

NanoClaw exposes a multi-mode `/plan` (modes plan, investigate, review, research)
plus a separate `/implement` command. Hermes at v2026.8.31 already ships the
default plan mode as the builtin `/plan`, but has **no builtin** for the other
three plan-modes or for `/implement`. This plugin ports exactly those four
residual bodies. The honest mapping:

| NanoClaw | Hermes surface at v2026.8.31 | disposition |
|---|---|---|
| `/plan` (default) | builtin `/plan` — planning-only, writes `.hermes/plans/`, "without executing" | ADOPT (stock) |
| `/plan review` (review MODE) | no stock equivalent; the stock `/review` is a different capability (below) | PORT → `/plan-review` |
| `/plan investigate` | none | PORT → `/plan-investigate` |
| `/plan research` | none | PORT → `/plan-research` |
| `/implement` | none (no stock builtin) | PORT → `/implement` |

**Stock `/review` is not a plan-review workflow.** Hermes' builtin `/review`
spawns an **independent subagent** to review the work just discussed — a
background capability over a conversation snapshot, **not** a plan-review workflow
body. `/plan-review` is the ported review *mode* and is intentionally distinct;
the two are not equivalent.

**Interface divergence — surfaced, not hidden.** This does not reproduce
NanoClaw's literal `/plan <mode>` token. A plugin cannot register or override the
builtin `/plan`, and a skill named `plan` will not auto-register as `/plan`
(the core-command collision guard wins). So the four bodies take distinct,
non-colliding `/slash` names — `/plan-investigate`, `/plan-review`,
`/plan-research` and `/implement` — with the mode fixed by the slug and the topic
after the command passed as the skill's user instruction (`user_instruction`),
e.g. `/plan-investigate why does startup stall`. That is not the literal
`/plan <mode>` mode-argument form NanoClaw uses.

**Orchestrator ruling (2026-09-24, Option A).** The divergence was surfaced to the
orchestrator, which accepted the aliases as **behavioral parity** and declined the
CORE-CHANGE. The literal `/plan <mode>` token remains a documented, reversible core
change the operator may elect later; the aliases deliver the behavior with zero
core edit.

**Mapped seams (dual `tag:`/`main:` citations).** Anchored at the release tag
`v2026.8.31` and at upstream `main` (read from
`raw.githubusercontent.com/NousResearch/hermes-agent/main`, which has moved past
the tag):

- the `/slash` mint + turn-seed the aliases ride on: tag: `agent/skill_commands.py:427` (scan_skill_commands) | main: `agent/skill_commands.py:391`
- the builtin-`/plan` planning-only contract: tag: `hermes_cli/commands.py:230` (CommandDef "plan") | main: `hermes_cli/commands.py:132`
- the single-mode planning prompt a literal `/plan <mode>` would have to branch: tag: `agent/plan_prompt.py:78` (build_plan_prompt) | main: `agent/plan_prompt.py:60`

## Placement and upstreaming

`nv-workflow-modes` is a first-party plugin, intentionally vendored under
`plugins/` in this fork. It is not a third-party-product integration, so it is not
subject to the upstream policy that vendor/SaaS plugins ship as their own repo. For
standalone distribution it installs at `~/.hermes/plugins/nv-workflow-modes` or
ships via a `hermes_agent.plugins` pip entry point — the same discovery path any
user-installed plugin takes. All behaviour lives in `config.yaml`
(`plugins.enabled`); the plugin adds no `HERMES_*` environment variable and keeps
no durable state outside the profile's skills tree.
