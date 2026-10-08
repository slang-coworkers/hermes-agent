---
title: "Skill Engine and Zero-Downtime Updates"
description: "Coming from NanoClaw: how Hermes' skill engine, profile-distribution updates, and 30-second refresh cache map onto the NanoClaw skill-apply / update / refresh-cron stack."
sidebar_label: "Skill Engine & Updates"
---

# Skill Engine and Zero-Downtime Updates

**Coming from NanoClaw?** NanoClaw shipped a bespoke stack for four related
jobs: a `skill-apply` engine that applied `SKILL.md` directives deterministically
so setup and the agent installed identically, an `/update-nanoclaw` command that
pulled upstream transactionally with a snapshot to roll back to, an hourly
`refresh-skills-cron` sweep that made on-disk skill edits live without a
restart, and a pnpm `minimumReleaseAge` policy that held back freshly-published
dependencies. **Hermes covers the same operational goals with capabilities it
already ships** — a skill engine, a profile-distribution update pipeline, a
30-second skill discovery cache, and a Skills Hub provenance chain — but the
mapping is **not one-for-one**: Hermes has no `nc:` directive-fence engine (that
is deferred, SELF-F57.a), the hourly cross-profile fetch is tracked as separate
work, and Hermes has no time-based release-aging gate. This page is the map: each
NanoClaw behaviour, the closest Hermes surface, where it matches and where it does
not, and the code behind it.

:::note Citation convention
Code references are given in dual-anchor form so you can find them in either
tree:

- **`tag:`** — the pinned release, tag **v2026.8.31** (commit `29112bef`), the
  authoritative source. Every `tag:` line here was verified against that tree.
- **`main@08b140d14:`** — the same symbol on the fork's upstream-tracking `main`
  at commit `08b140d1`, for readers working against a newer checkout. `main`
  moves, so these anchors are pinned to that exact commit and are **not** claims
  about `main`'s current line numbers.

A few surfaces are cited `tag:` only where the release-vs-`main` line could not
be dual-resolved; those are noted inline.
:::

## What maps to what

| NanoClaw behaviour | Hermes surface | Key code |
|---|---|---|
| `skill-apply.ts` — apply `SKILL.md` directive fences deterministically, idempotently, journaled, so "install once, identically" | **No direct equivalent.** Hermes uses plain prose `SKILL.md`, not a `nc:` fence grammar, and has no transactional directive journal (the fence engine is deferred, SELF-F57.a). The closest stock primitive for "everyone installs the identical tree" is SHA-pinned source install: `hermes plugins install <repo> --ref <40-char SHA>` pins the plugin's **source** to an immutable commit; the manifest *declares* required env and dependencies (declared, **not** auto-installed) | `tag:` `hermes_cli/subcommands/plugins.py:41`, `hermes_cli/plugins_cmd.py:591` · `main@08b140d14:` `hermes_cli/subcommands/plugins.py:28`, `hermes_cli/plugins_cmd.py:449` |
| `/update-nanoclaw` — transactional upstream update with snapshot | The **fleet-update pipeline**: read-only plan phase, JSON receipts, a cross-process update marker, an image-managed refusal, and fresh-interpreter restart recovery. This is the gateway self-update path (`hermes update`), distinct from the in-place profile update below; see [Version identity](#version-identity) for the fleet version matrix it reports | `tag:` `hermes_cli/update_inventory.py:1`, `hermes_cli/update_receipt.py:6`, `hermes_cli/update_lock.py:13`, `hermes_cli/update_contract.py:11`, `hermes_cli/update_restart_recovery.py:3` · `main@08b140d14:` `hermes_cli/update_inventory.py:1`, `hermes_cli/update_receipt.py:1`, `hermes_cli/update_lock.py:1`, `hermes_cli/update_contract.py:1`, `hermes_cli/update_restart_recovery.py:1` |
| `refresh-skills-cron.sh` — per-container skill copy + hourly refresh, live with no restart | The **30-second skill-index discovery cache** plus `skills.external_dirs` plus a script-only `hermes cron` job — see [Zero-downtime refresh](#zero-downtime-refresh) | `tag:` `tools/skills_tool.py:101`, `hermes_cli/config_defaults.py:2250`, `hermes_cli/subcommands/cron.py:53` · `main@08b140d14:` `tools/skills_tool.py:36`, `hermes_cli/config_defaults.py:1315`, `hermes_cli/subcommands/cron.py:41` |
| pnpm `minimumReleaseAge` — supply-chain policy | Skill **provenance** for hub-installed skills: quarantine → scanner → content-hash lock → per-mutation audit ledger — see [Supply-chain provenance](#supply-chain-provenance) | `tag:` `hermes_cli/skills_hub.py:548`, `tools/skills_hub.py:4009`, `tools/skills_guard.py:922` · `main@08b140d14:` `hermes_cli/skills_hub.py:645`, `tools/skills_hub.py:195`, `tools/skills_guard.py:464` |

Two NanoClaw pieces are **out of scope for this page** and tracked separately:
the `nc:` directive-fence engine itself (deferred as SELF-F57.a — Hermes uses
prose `SKILL.md`, not a fence grammar), and the *hourly external-fetch /
cross-profile fan-out* half of `refresh-skills-cron` (a separate configure row,
SELF-F57.b). This page covers the **engine-level** properties: on-disk change
live in a running process with no restart, a versioned in-place update that
preserves user data, and the provenance chain.

The rest of this page expands each mapping, with the code that backs it.

## Versioned profile updates

Where NanoClaw's `/update-nanoclaw` pulled the whole install forward, Hermes
updates a **profile distribution** in place. A distribution is a git repository
(or, for local authoring and tests, a local directory) that packages a complete
agent — SOUL, config, skills, cron, MCP; see
[Profile Distributions](./profile-distributions.md). The two commands are:

```bash
hermes profile install <repo> --name mybot   # first-time install
hermes profile update mybot                   # pull the new version in place
```

Both funnel through one copier, `_copy_dist_payload`
(`tag:` `hermes_cli/profile_distribution.py:563`; `main@08b140d14:` `:358`),
driven by `install_distribution`
(`tag:` `:662`; `main@08b140d14:` `:403`) and `update_distribution`
(`tag:` `:705`; `main@08b140d14:` `:435`). The update is **versioned**: the new
distribution's `version` from `distribution.yaml` is recorded in the profile's
on-disk manifest, so `hermes profile info` reports the version you updated to.

### What an update replaces, and what it never touches

The copier resolves the **distribution-owned** set — the files an update is
allowed to overwrite — two ways:

- **Manifest declares a non-empty `distribution_owned` list.** The copy uses that path-aware
  allowlist for the ordinary payload entries — a listed *directory* is replaced
  wholesale, so a file the new version dropped (a renamed or removed skill) does
  not linger (`tag:` `hermes_cli/profile_distribution.py:604-625`;
  `main@08b140d14:` the logic moved to the `_owned_entries` helper — `:337`, with
  the "Path-aware allowlist" comment at `:346`). Two categories are written
  **outside** the allowlist, by design: the normalized `distribution.yaml`
  manifest is *always* rewritten to record the resolved name/source/version
  (`tag:` `:651-652`, `write_manifest`), and `.env.EXAMPLE` may be written — a
  shipped `.env.template` is renamed to it (`tag:` `:617-619`) or one is
  synthesized from the manifest's `env_requires` (`tag:` `:645-649`). Nor is
  "replace each listed entry" unconditional: a *listed* `config.yaml` is
  deliberately skipped on update when `preserve_config` is set
  (`tag:` `:620-622`) — the behaviour the config.yaml section below relies on.
- **Manifest omits it (or declares an empty list).** The copy ships every staged top-level entry **except**
  the user-owned exclusion set `USER_OWNED_EXCLUDE`
  (`tag:` `:101`; `main@08b140d14:` `:35`). It deliberately does **not** narrow
  to `DEFAULT_DIST_OWNED` — the code comment says so explicitly
  (`tag:` `:626-630`; `main@08b140d14:` `:340`).

`DEFAULT_DIST_OWNED` (`tag:` `:88`; `main@08b140d14:` `:31`) is the *documented*
"distribution-owned" set returned by `DistributionManifest.owned_paths()`
(`tag:` `:239`, tag-only) — the answer to "what does this distribution claim to own" — not
the filter the copier applies when a manifest omits the key. Reproduce the
canonical owned-vs-preserved table at
[`profile-distributions.md`](./profile-distributions.md) (release anchor
`website/docs/user-guide/profile-distributions.md:266`).

Everything in `USER_OWNED_EXCLUDE` is preserved: memories, sessions, `state.db`,
`auth.json`, `.env`. An update is **scoped to the named profile** — a sibling
profile under the same `HERMES_HOME` is untouched, because the copier only ever
writes into the target profile's directory. This is the property NanoClaw got
from its snapshot-and-restore dance; Hermes gets it structurally, by never
writing user-owned paths in the first place.

### `config.yaml`: preserved by default

`config.yaml` is treated as user-owned by default — your model choice, your
tuning, your MCP wiring survive an update. Pass `--force-config` to take the
distribution's `config.yaml` instead:

```bash
hermes profile update mybot                  # keeps your config.yaml
hermes profile update mybot --force-config   # replaces it with the distribution's
```

The flag lives at `tag:` `hermes_cli/subcommands/profile.py:195`
(`main@08b140d14:` `:122`) and is threaded through the dispatcher
(`tag:` `hermes_cli/main.py:11554` → `hermes_cli/profile_distribution.py:746`;
`main@08b140d14:` the profile-command bodies were extracted from `main.py` into
`hermes_cli/profile_cmd.py`, `_profile_update:435`). `update_distribution` maps
it to `preserves_config = not force_config` (`tag:` `:746`; `main@08b140d14:` `:452`), so the default
(`force_config=False`) preserves and `--force-config` replaces.

## Zero-downtime refresh

NanoClaw ran an hourly cron to copy skills into each container so on-disk edits
went live without a restart. Hermes makes on-disk skill changes live *within a
running process* through its **skill discovery cache**, with a bounded, ≤30-second
staleness window and no restart at all.

The cache is `_SKILLS_CACHE` (`tag:` `tools/skills_tool.py:101`;
`main@08b140d14:` `:36`), with a TTL of `_SKILLS_CACHE_TTL_SECONDS = 30.0`
(`tag:` `:102`; `main@08b140d14:` `:37`). Each scan computes a signature over the
scanned directories (`_skills_scan_signature`, `tag:` `:107`;
`main@08b140d14:` `:40`) that `_find_all_skills` consults before serving a cached
result (`tag:` `:687`; `main@08b140d14:` `:187`). Two cases behave differently,
and the difference is the whole point:

- **Add or remove a skill *directory*** in a scanned path → the signature
  changes → the next scan picks it up **immediately**, with no cache clear and no
  restart.
- **Edit a `SKILL.md`'s *content* in place** (same directory set) → the directory
  signature is unchanged, so the change is served from cache until the TTL
  elapses, then reflected on the next scan — **bounded by ≤30 seconds**, still no
  restart. The gotcha is documented in-code at `tag:` `tools/skills_tool.py:98`
  (`main@08b140d14:` `:35`).

Because each profile has its own `HERMES_HOME`, a refresh in one profile's skills
directory disturbs **no other profile**.

:::caution The 30-second window is for skill *discovery*, not a running gateway's system prompt
The 30-second `_SKILLS_CACHE` governs skill **discovery** — what
`_find_all_skills()` and the `skills` tool return. The skill index **embedded in
a system prompt** is a *separate* process-wide cache (`_SKILLS_PROMPT_CACHE`,
`tag:` `agent/prompt_builder.py:1512`; `main@08b140d14:` `:1055`) with **no TTL
and no filesystem revalidation**: it is keyed on the directory paths and config,
and a cache hit returns the built prompt without re-reading disk, so an in-place
`SKILL.md` content edit at the same path is invisible to it. A fresh session in
the same process does **not** on its own pick the edit up — with the same key it
hits the cache. The prompt-embedded index refreshes only when that cache is
explicitly cleared (`clear_skills_system_prompt_cache`, `tag:` `:1523`;
`main@08b140d14:` `:1065`), evicted from its 32-entry LRU, or the process
restarts — never merely because the 30-second discovery TTL elapsed.
`/reload-skills` rebuilds the slash-command map and **deliberately does not**
invalidate that prompt cache, precisely to preserve per-conversation
prompt-prefix caching, which Hermes treats as sacred
(`tag:` `agent/skill_commands.py:580`, docstring `:587`;
`main@08b140d14:` `:450`). Bottom line: a newly-added skill becomes
**discoverable** within ≤30 seconds live, but its appearance in a running
gateway's system prompts follows the prompt cache's own invalidation, not the
discovery TTL.
:::

:::warning External skill directories bypass the security scanner
Skills installed through the [Skills Hub](#supply-chain-provenance) are scanned
before install. Skills read from `skills.external_dirs` and other local
directories are **not** run through that scanner — `_find_all_skills` consumes the
resolved external dirs directly (`tag:` `tools/skills_tool.py:720-725`;
`main@08b140d14:` `:183`, in the `_skill_search_dirs` helper) via
`get_external_skills_dirs` (`tag:` `agent/skill_utils.py:533`;
`main@08b140d14:` `:332`). A zero-downtime refresh therefore ships **whatever is
on disk** to every profile that reads a shared external directory. Treat a shared
`external_dirs` path as a trust boundary: only point profiles at directories you
control.
:::

### Wiring a live-refreshing shared skills directory

Two config surfaces plus one cron job reproduce NanoClaw's refresh sweep without
the copy step:

- `skills.external_dirs` — additional folders scanned alongside the profile's own
  `skills/` (`tag:` `hermes_cli/config_defaults.py:2250`;
  `main@08b140d14:` `:1315`). See
  [External Skill Directories](./features/skills.md#external-skill-directories).
- A **script-only** `hermes cron` job (`--script … --no-agent`) to run any
  periodic maintenance without spending a model turn
  (`tag:` `hermes_cli/subcommands/cron.py:53`;
  `main@08b140d14:` `:41` for `--script`, `:47` for `--no-agent`). See
  [Cron](./features/cron.md).

The *hourly external-fetch and cross-profile fan-out* half of NanoClaw's cron —
pulling a remote skills repo on a schedule and distributing it — is tracked as a
separate configure row and is not part of this engine-level mapping.

## Supply-chain provenance

pnpm's `minimumReleaseAge` holds back a dependency until it has been published
for a set time, on the theory that a compromised release is often caught and
yanked within hours. Hermes' Skills Hub is the **analogous** surface, but the
control is different in kind — and it is worth stating plainly:

:::note Analogous, not equivalent
Hermes has **no time-based release-aging gate**. There is no
`minimumReleaseAge` equivalent that delays a skill because it was published
recently. What Hermes provides instead is a **content-provenance chain**:
quarantine → scan → content-hash lock → per-mutation audit ledger. It defends
against *tampering and known-bad content*, not against *freshness*.
:::

When you install a hub skill, `do_install` stages it, then runs the security
scanner before it lands (`tag:` `hermes_cli/skills_hub.py:543`, calling
`scan_skill_cached` at `:714`; `main@08b140d14:` `:645`). The scanner itself is
`scan_skill_cached` (`tag:` `tools/skills_guard.py:922`;
`main@08b140d14:` `:464`). What was installed is then recorded in a lock file,
`HubLockFile` (`tag:` `tools/skills_hub.py:4008`; `main@08b140d14:` `:195`), which
stores the scan verdict and a content hash for each skill
(`tag:` `tools/skills_hub.py:4050-4051`; `main@08b140d14:` `:230-231`). Each
mutation is recorded to an audit ledger on a **best-effort** basis —
`append_audit_log` swallows a write failure and logs it at debug rather than
aborting (`tag:` `tools/skills_hub.py:4127`, the `except OSError` at `:4139-4141`;
`main@08b140d14:` `:286`). On update, `do_update` re-derives the on-disk content
hash and compares it against the recorded one, so a locally-tampered skill is
detected (`tag:` `hermes_cli/skills_hub.py:1114`, `:1142-1149`;
`main@08b140d14:` `:826`, the recorded-vs-disk compare at `:817`, `:820-821`).

As noted under [Zero-downtime refresh](#zero-downtime-refresh), this chain gates
**hub-installed** skills. Skills read from `skills.external_dirs` or other local
directories do not
pass through the scanner — that is the trust boundary to keep in mind when you
adopt the zero-downtime refresh for a shared directory.

## Version identity

NanoClaw answered "what version am I running?" from its updater. Hermes answers
it in two layers:

- **Distribution version** — `hermes profile info` prints the installed
  distribution's manifest: name, version, and `source` (a git URL or local path)
  (`tag:` `hermes_cli/main.py:11582`; `main@08b140d14:` `hermes_cli/profile_cmd.py:476`,
  which prints name/version/source at `:485-487`). This is the version an update
  moved you to.
- **Running-code identity** — the gateway's own code SHA and a per-profile
  `current | stale | unknown | down` matrix come from the update pipeline:
  `collect_fleet_versions` (`tag:` `hermes_cli/update_receipt.py:303`;
  `main@08b140d14:` `:281`), rendered by `print_fleet_version_matrix`
  (`tag:` `:461`; `main@08b140d14:` `:345`), over the code identity from
  `get_code_identity` (`tag:` `hermes_cli/build_info.py:101`;
  `main@08b140d14:` `:74`).

Together these are the "which version, and is it up to date" that
`/update-nanoclaw` reported after an update.

## Limitations: no ref pinning yet

There is one gap to state honestly. `hermes plugins install` accepts an
**immutable 40-character commit SHA** via `--ref`
(`tag:` `hermes_cli/subcommands/plugins.py:41`, validated in
`_normalize_exact_revision` at `hermes_cli/plugins_cmd.py:591` against the strict
40-hex `_EXACT_COMMIT_RE` at `:560`; `main@08b140d14:`
`hermes_cli/subcommands/plugins.py:28`, `hermes_cli/plugins_cmd.py:449` (validation),
`:421` (regex)).

**`hermes profile install` and `hermes profile update` do not.** Their argument
parsers accept only `source` / `--name` / `--alias` / `--force` / `-y` (install,
`tag:` `hermes_cli/subcommands/profile.py:162-181`; `main@08b140d14:` `:101-112`)
and `profile_name` / `--force-config` / `-y` (update, `tag:` `:193-201`;
`main@08b140d14:` `:120-124`) — there is no `--ref` / `--sha` / `--pin`. Under the
hood, `_git_clone` runs a bare `git clone --depth 1` with no `--branch` or checkout
(`tag:` `hermes_cli/profile_distribution.py:391-407`, the clone at `:397`;
`main@08b140d14:` `:230-252`, clone at `:235`), which
resolves to the repository's **default branch**. So a distribution is pinned only
by its `source` (whatever the default branch points at) plus the separately
reported running-code SHA above — you cannot install or update a distribution to
a chosen commit.

:::caution Docstring vs. reality
The module docstring at `tag:` `hermes_cli/profile_distribution.py:23-27`
(tag-only — this `#<ref>` claim is not present at `main@08b140d14`) states a git
URL may carry `#<ref>` "to pin a tag / branch / commit SHA." No code parses that
fragment today, and the user docs confirm ref pinning is not shipped —
[`profile-distributions.md`](./profile-distributions.md) "Pin to a specific
version" (release anchor `:552-556`). Treat the docstring as aspirational until
ref pinning lands.
:::

If you need reproducibility today, resolve the SHA out of band (record the commit
your `source` branch pointed at) and re-`update` when you deliberately want the
new tip.

## Acceptance and proof

This Adopt row's **discovery-cache and profile-distribution** behaviour — the
directly executable mappings above — is proved by a hermetic acceptance test at
`tests/hermes_cli/test_self_f57_acceptance.py`; the broader comparison rows
(plugin SHA-pinning, the fleet-update pipeline, cron, Skills Hub provenance, and
version reporting) are documented against their cited implementation anchors
rather than exercised here. Because these are capabilities Hermes already ships,
the test **passes on the stock tree** — that is the point: it demonstrates Hermes
does the job. It drives the shipped functions directly (no plugin), and its six
checks carry their own negative controls so a regression fails them:

1. A new skill in a **scanned** directory is discovered on the next scan with no
   cache clear; a skill in an **unconfigured** directory is never returned.
2. An in-place `SKILL.md` content edit is served from cache until the index TTL
   elapses, then reflected — bounded, no restart.
3. `hermes profile update` replaces the distribution-owned files, records the new
   version (by rewriting the `distribution.yaml` manifest), drops a stale owned
   entry, and leaves the deliberately-unlisted `README.md` absent.
4. The same update leaves user-owned data (`.env`, `auth.json`, `state.db`,
   `memories/`, `sessions/`) byte-for-byte untouched and leaves a sibling profile
   undisturbed.
5. `config.yaml` is preserved by default and replaced only with `--force-config`.
6. When `distribution_owned` is **omitted**, the legacy copy-all path still
   preserves `USER_OWNED_EXCLUDE` data (`.env`, `state.db`, `memories/`) — even
   against conflicting incoming values — while installing and refreshing the
   unlisted, non-user-owned payload (e.g. `README.md`).

## See also

- [Profile Distributions](./profile-distributions.md) — the full distribution
  format, `distribution.yaml`, and the owned-vs-preserved table.
- [Skills System](./features/skills.md) — how skills are discovered, the Skills
  Hub, and external skill directories.
- [Cron](./features/cron.md) — scheduled jobs, including script-only
  `--no-agent` jobs.
- [Checkpoints and `/rollback`](./checkpoints-and-rollback.md) — the
  filesystem-level safety net complementary to versioned updates.
