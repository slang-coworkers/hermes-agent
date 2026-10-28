---
title: "Onboarding Projects and Coworkers"
description: "Coming from NanoClaw? Map its project- and coworker-onboarding onto Hermes: a coworker is a profile (create_profile / install_distribution), the fleet onboard_coworker and onboard_project verbs, and the stock /init repo scan."
---

# Onboarding Projects and Coworkers

## Coming from NanoClaw

In NanoClaw you onboard two kinds of thing:

- a **project** — you point the tool at a repository, it inspects the tree and emits
  an agent configuration; and
- a **coworker** — you call `create_agent` and a new bot appears, routable by its role
  description.

Hermes already ships both, split across two layers: the **stock onboarding primitives**
that have been in Hermes since before this fleet, and the **fleet-batch onboarding verbs**
added by the merged `nv-coworker-compose` carrier. This page is the map between the
NanoClaw behaviour and the Hermes surface that covers it — it is a runbook, not a
reference: each section links the canonical page rather than restating it.

This page claims **no full parity**. A Hermes coworker is a *profile*, not a container,
and `onboard_project` emits a *distribution scaffold*, not a running runtime. The
deliberate differences and their owners are collected in
[§ Equivalence](#equivalence) below.

> **Citation forms used on this page.** Stock surfaces present in the pinned release are
> cited twice — `tag: <file>:<line>` (the read-only release tree at tag `v2026.8.31`) and
> `main: <file>:<line>` (resolved against `main` @ `08b140d14`) — so a reader on either
> tree can find them. Fork-only carrier seams, absent from the release tag, are cited
> `baseline: release/v2026.8.31-e2e-fixed @ <sha7> — <path> <function>` **by function
> name** (the baseline head moves as siblings merge, so only the pinned tree earns an
> exact line). Doc-only references (a doc page, a CLI subcommand, the importers) may be
> `tag:`-only.

## A coworker is a profile

There is no separate "coworker" primitive in Hermes: **a Bot _is_ a profile**
([Bot Mode](./bot-mode.md), `tag: website/docs/user-guide/bot-mode.md:8-14`) — an
isolated bundle of config, memory, skills, credentials, and chat history under
`~/.hermes/profiles/<name>/`. Creating a coworker by hand is therefore creating a profile.

- **The primitive:** `create_profile`
  (`tag: hermes_cli/profiles.py:1177` | `main: hermes_cli/profiles.py:813`), signature
  `create_profile(name, clone_from=None, clone_all=False, clone_config=False, no_alias=False, no_skills=False, description=None) -> Path`.
  The `description` is the role text the kanban decomposer routes on; it persists to the
  profile's `profile.yaml` and round-trips back through `list_profiles`
  (`tag: hermes_cli/profiles.py:1029` | `main: hermes_cli/profiles.py:688`), whose
  `ProfileInfo.description` field carries it.
- **From the CLI:** `hermes profile create <name> --description "<role>"`
  (`--clone` / `--clone-all` to seed from an existing profile;
  `tag: hermes_cli/subcommands/profile.py:29-64`).
- **Bootstrapping the very first agent:** the `hermes setup` wizard.

So the manual, single-coworker NanoClaw path — "make me one more bot that does X" — is a
`create_profile` call whose `description` makes the new profile routable, and
`list_profiles` is how the fleet enumerates the roster it just grew. This is
**proven hermetically by AC-SELF-F56-5**: two profiles are created with distinct
descriptions and each description round-trips through `list_profiles` unaliased.

See [Profiles](./profiles.md) for the full profile model and
[Multi-Profile Gateways](./multi-profile-gateways.md) for running many at once.

## Installing a whole agent

A coworker often is not built field-by-field but **installed whole** from a
distribution — a packaged agent (`SOUL.md` spine, `config.yaml`, `mcp.json`, skills,
manifest). The primitive is `install_distribution`
(`tag: hermes_cli/profile_distribution.py:662` |
`main: hermes_cli/profile_distribution.py:403`), signature
`install_distribution(source, name=None, force=False, create_alias=False) -> InstallPlan`,
which materialises the distribution `source` into a new profile under the profiles root.

A **local-directory** `source` needs no network — the git clone is a transport-layer
concern for URL sources only. This is the stock primitive the fleet `onboard_coworker`
verb rides to install each rendered coworker type, and it is
**proven hermetically by AC-SELF-F56-6**: two local distribution directories install as
two distinct enumerable profiles with their manifest identity and payload intact, and any
attempt to reach the network fails the test.

See [Profile Distributions](./profile-distributions.md) for authoring and sharing a
distribution.

## Fleet coworker onboarding — the merged carrier

Onboarding a **fleet** — many coworker types at once, each appearing in the desktop Bots
list and wired into standing rooms — is the job of the merged `nv-coworker-compose`
carrier's `hermes onboard coworker <coworker-types.yaml>` verb
(`baseline: release/v2026.8.31-e2e-fixed @ a817c4d — plugins/nv-coworker-compose/__init__.py onboard_coworker`).
For each declared coworker type it:

1. **Renders and installs one profile** through the **real `install_distribution` path**
   above (`baseline: … plugins/nv-coworker-compose/__init__.py _install` →
   `install_distribution(force=True)`) — so every onboarded coworker is
   distribution-backed, not a bare `create_profile`.
2. **Writes the desktop Bots-list identity** by dispatching one `profiles.configure`
   RPC per profile carrying a `ui_meta['hermes-bots']` blob (title, description, shape)
   via per-key compare-and-set revisions — **never a raw `profile.yaml` write** and never
   clobbering a competing `ui_meta` key.
3. **Creates the standing rooms** by dispatching one `groups.create` per declared room
   with its member set, **idempotently** — a second onboard re-dispatches the same room
   ids without a duplicate create.

The batch outcome — one distribution-backed profile per declared type before the verb
returns success — is **proven hermetically by AC-SELF-F56-7** (the real
`onboard_coworker` → `install_distribution` path over a stateful RPC fake). The
per-room and per-profile dispatch shapes are re-proved by the carrier's own merged nodes,
**AC-SELF-F56-2** (idempotent `groups.create`) and **AC-SELF-F56-3** (the
`profiles.configure` / `hermes-bots` compare-and-set write). The desktop Bots-list
_rendering_ itself is the carrier's merged Playwright spec
`apps/desktop/e2e/loop-f35-ac1.spec.ts` (out of this page's hermetic tier; its pytest
backing is AC-SELF-F56-3 plus the installed roster in AC-SELF-F56-7).

## Onboarding a project

NanoClaw's "point at a repo → get an agent config" maps to **three distinct Hermes
paths**, described separately because they produce different artifacts:

- **Stock `/init` — a repo-inspection prompt → `AGENTS.md`.** `hermes /init` builds a
  prompt that inspects the working tree and yields an `AGENTS.md` for the project
  (`build_init_prompt_for_cwd`, `tag: hermes_cli/init_command.py:133` |
  `main: hermes_cli/init_command.py:82`). This is the closest analogue to NanoClaw's
  "inspect the tree and emit a config", and it is model-driven.
- **Stock `hermes project create` — register a multi-folder Project.** Registers one or
  more source folders as a named Project so the agent can work across them
  (`_cmd_create` in `hermes_cli/projects_cmd.py`,
  `tag: hermes_cli/projects_cmd.py:191` | `main: hermes_cli/projects_cmd.py:134`).
- **Fleet sugar `hermes onboard project <url|path>` — a project distribution scaffold.**
  The carrier verb `onboard_project`
  (`baseline: release/v2026.8.31-e2e-fixed @ a817c4d — plugins/nv-coworker-compose/__init__.py onboard_project`)
  performs a **bounded, deterministic filesystem scan** (`_scan_source_files`, `max_files`
  default 64 — no auxiliary model, no reuse of the `/init` prompt) and then
  `_write_project_scaffold` emits a distribution scaffold: `distribution.yaml`, a
  `SOUL.md` spine, `skills/<capability>/SKILL.md`, and `mcp.json`. A git-URL source is
  fetched via `_clone_repo` first; binaries and `.env` secrets never enter the scaffold.

The scaffold contract, the bounded scan, and the no-secrets guarantee are
**proven hermetically by AC-SELF-F56-4** (the carrier's merged node). Note the
difference from NanoClaw: `onboard_project` emits a repo-scanned *distribution scaffold*
you then install with `install_distribution`, not a live runtime.

## Onboarding from other agent frameworks

Coworkers can also be brought in from other frameworks (referenced here, not proven on
this page):

- **OpenClaw migrate** — `hermes_cli/claw.py`.
- **Claude Code / Codex import** — `hermes_cli/agent_import.py` maps `CLAUDE.md` /
  `AGENTS.md` / `mcpServers` / skills into a Hermes profile.

## Equivalence — what maps and what does not {#equivalence}

This row claims no full parity; it takes no credit it cannot back. Owners are named so a
reader knows which surface proves which behaviour.

| NanoClaw behaviour | Hermes covering surface | Deliberate difference / owner |
|---|---|---|
| Onboard a coworker (`create_agent` → a new bot) | A coworker is a profile: the manual single-coworker path is stock `create_profile`; the fleet-batch `onboard_coworker` verb installs one distribution per declared type via stock `install_distribution` | Profiles are not containers. Manual creation = `create_profile` (AC-SELF-F56-5); fleet-batch = `install_distribution` (AC-SELF-F56-6) driven by `onboard_coworker` (AC-SELF-F56-7). `onboard_coworker` does not call `create_profile`. Owner: LOOP-F35 for the verb, stock Hermes for the primitives. |
| A new bot appears, routable by role | `onboard_coworker` writes each bot's `ui_meta['hermes-bots']` identity (the Bots-list row); role routing rides the profile `description` that `create_profile` persists and `list_profiles` surfaces | The desktop Bots-list *render* is the carrier's Playwright spec `apps/desktop/e2e/loop-f35-ac1.spec.ts` (out of this pytest tier); its hermetic backing is AC-SELF-F56-3 (the `ui_meta` write) + AC-SELF-F56-7 (installed roster); the `description` round-trip is AC-SELF-F56-5. Owner: LOOP-F35 (identity) + stock Hermes (`description`). |
| Onboard a project (repo → agent config) | Stock `/init` repo scan → `AGENTS.md` + `hermes project create`; fleet sugar `onboard_project` → a project distribution scaffold | Hermes emits a repo-scanned distribution scaffold, not a runtime; the scaffold contract is AC-SELF-F56-4. Owner: LOOP-F35 for the verb, stock `/init` for the scan. |

## See also / how this is proven

The behaviours on this page are verified hermetically by
`tests/plugins/test_self_f56_acceptance.py` (the new criteria AC-SELF-F56-5, AC-SELF-F56-6,
AC-SELF-F56-7) and by the merged carrier's own nodes
`tests/plugins/test_nv_coworker_compose_acceptance.py::test_ac_self_f56_{2,3,4}` for
AC-SELF-F56-2, AC-SELF-F56-3 and AC-SELF-F56-4.

Related pages:

- [Profiles](./profiles.md)
- [Profile Distributions](./profile-distributions.md)
- [Bot Mode](./bot-mode.md)
- [Multi-Profile Gateways](./multi-profile-gateways.md)
- [Plugin developer guide](../developer-guide/plugins/index.md)
