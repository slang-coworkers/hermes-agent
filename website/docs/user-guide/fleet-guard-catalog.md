---
sidebar_position: 78
title: "Fleet guard catalog (NanoClaw → Hermes runbook)"
description: "How NanoClaw's allow/hold/deny guard catalog maps onto Hermes: the fleet's single nv-fleet-gates pre_tool_call veto is the enumerable, alias-canonicalised restricted tool-name catalog; the native pre_tool_call directive machinery is the chokepoint; drift is caught by a load-time restricted-set scan (loud, not a fail-close) with a gate-time fail-closed backstop. Names the residual (nominal-action catalog + days-later grant replay → GOV-F22.a)."
---

# Fleet guard catalog (NanoClaw → Hermes)

This runbook maps NanoClaw's **guard catalog** — the frozen `allow | hold | deny`
action registry with days-later grant replay — onto the Hermes surface that
covers its load-safe, enforceable subset. It is a **mapping runbook, not a plugin
reference**: for the restricted-set integrity mechanics themselves (the load-time
scan, the deferral rule, the gate-time backstop) it links to
[nv-fleet-gates — the fleet's single pre_tool_call veto](../developer-guide/plugins/nv-fleet-gates.md#restricted-set-integrity-load-time-refusal--gate-time-backstop)
rather than restating them.

**Scope.** The covering surface is the `nv-fleet-gates` fleet veto plus Hermes's
native `pre_tool_call` directive machinery and native human-approval ladder. Two
parts of the NanoClaw catalog are **not** covered here — a nominal-action catalog
decoupled from tool names, and days-later grant replay — and are dispositioned to
follow-up row **GOV-F22.a** (see [What is not covered](#5-what-is-not-covered-residual--gov-f22a)).

**Citation policy (this page is a citation-accuracy surface).** Native Hermes
primitives are exact `file:line` in the **pinned release tree** (`tag:`,
v2026.8.31, commit `29112bef` — the sole authoritative source). The `nv-fleet-gates`
seams are a **merged fork-baseline artifact** (absent from the release tag and from
upstream `main`), so they are cited **symbolically by function name** at baseline
`release/v2026.8.31-e2e-fixed` — fork-branch line numbers drift as sibling rows
squash-merge, so a line number there would rot. The `main:` anchors are reproduced
for orientation only and are **advisory** (a moving ref, not re-verified against a
pinned tree).

## 1. What NanoClaw's guard catalog did

In NanoClaw a *guard catalog* is a frozen registry of nominal actions, each tagged
`allow`, `hold`, or `deny`: enumerable, duplicate-refusing, and enforced at the
tool/command chokepoint. `deny` refuses the action outright; `allow` lets it
through; `hold` blocks it now, **persists** the request, and **replays** it when a
human approves later — possibly hours or days on. This is the behaviour being
ported. (Context only — no citation; it is the source design, not a Hermes surface.)

## 2. How Hermes covers it

The fleet installs **exactly one** `pre_tool_call` callback for the whole gateway —
the `nv-fleet-gates` veto — and *that veto is the catalog*:

- **The enumerable action list** is the veto's `_RESTRICTED_CANON` — the set of
  restricted **tool names** it floors (with `_CORE_PRESENCE_GROUPS` recording the
  accepted legacy+canonical spellings per group). It enumerates registry
  *tool names*, not arbitrary nominal actions: the deny/hold-eligible list is
  keyed on the running tool registry, which is exactly why the residual
  nominal-action catalog is out of scope (§5).
- **The chokepoint** is Hermes's native `pre_tool_call` directive machinery: the
  first valid directive wins, resolved through a single entry point and fired
  **once** per tool execution at the dispatch seam. The veto does not build a new
  chokepoint — it rides the native one.
- **The allow/hold/deny grain** is realised by the veto's directive returns, run as
  one ordered, block-before-approve predicate chain (§3).

The mapping, behaviour by behaviour (the mechanics live in the linked plugin doc;
this table is the NanoClaw → Hermes correspondence with its citation of record):

| NanoClaw guard-catalog behaviour | Hermes covering surface | citation |
|---|---|---|
| Enumerable action registry, keyed on the nominal name | veto `_RESTRICTED_CANON` (restricted tool-name set) + `_CORE_PRESENCE_GROUPS` (legacy+canonical spellings per group) | baseline `plugins/nv-fleet-gates/__init__.py` `_RESTRICTED_CANON`, `_CORE_PRESENCE_GROUPS` |
| Catalog integrity-checked so a stale entry can't *silently* fail open | load-time refusal `register()` → `_scan_restricted()` over `registry.get_all_tool_names()`, gated by `_registry_essentially_complete()`; gate-time fail-closed backstop `_restricted_block()` | baseline `plugins/nv-fleet-gates/__init__.py` `register`, `_scan_restricted`, `_registry_essentially_complete`, `_restricted_block` |
| Catalog match survives a tool rename | alias-canonicalisation `canonicalise()` over a **vendored** `_LEGACY_TOOL_ALIASES` (vendored because there is no alias table at the tag, and on `main` it is a private `_`-name outside the compatibility contract) | baseline `plugins/nv-fleet-gates/aliases.py` `canonicalise`, `_LEGACY_TOOL_ALIASES` |
| allow / hold / deny grain | veto directives `{action: block}` (deny) / `{action: approve, rule_key}` (hold, from `_wiring_approve` on a gated edge); ordered chain, block-before-approve, whole body `try/except BaseException → block` (fail-closed) | baseline `plugins/nv-fleet-gates/__init__.py` `_gate` |
| Enforced at the tool chokepoint | native `pre_tool_call` directive machinery: `_PreToolCallDirective`, `_get_pre_tool_call_directive_details` (first valid directive wins), `resolve_pre_tool_block` (single entry point), `_resolve_block_from_details` (fail-closed on deny/timeout/gate-error), fired once at the single dispatch seam | tag: `hermes_cli/plugins.py:6570`, `:6589`, `:6736`, `:6771`; single-fire `model_tools.py:1425-1449` · main (advisory): `hermes_cli/plugins.py:1752`, `:1771`, `:1834`, `:1842`; `model_tools.py:697-717` |
| Name-keyed gate must survive the rename cliff | at the pin the tool registers as `cronjob` with **no** alias table; `canonicalise()` matches on the canonical name on both trees | tag: `tools/cronjob_tools.py:2106` `name="cronjob"`; `_LEGACY_TOOL_ALIASES` **absent at tag** · main (advisory): `model_tools.py:552-555` (the five renames), resolved before the hook at `model_tools.py:821` |

## 3. allow / hold / deny mapping

The three NanoClaw tags map onto Hermes directives, and it matters *which* Hermes
path each one takes:

| catalog tag | Hermes realisation | path |
|---|---|---|
| **deny** | the veto returns `{action: block, message: …}` | the block is enforced by the native directive resolver; a **message-less** block is dropped by core (a silent fail-open), so every block carries a non-empty `message` |
| **hold** | the veto returns `{action: approve, rule_key: "wire:<from>:<to>"}` from `_wiring_approve` on a **gated** edge | routed into Hermes's **native human-approval path**: `_resolve_block_from_details` → `request_tool_approval` (`plugin_rule:<rule_key>`, fail-closed when no human) → `_run_approval_gate` (once / session / always / deny) |
| **allow** | **no directive** — the predicate chain returns nothing and the tool proceeds | — |

`hold` is produced by a **separate predicate** (`_wiring_approve`) from the one that
produces `deny`, and it never pre-empts a block: every blocking predicate is
evaluated before the gated-edge approve, so a marked delivery on a gated edge that
still lacks its critique is **blocked, not approved**.

Two adjacent surfaces are deliberately *not* the path a plugin directive takes, and
are cited only for orientation:

- **`check_all_command_guards`** is the **command-guard** allow/hold/deny
  **tier-order reference** (hardline → sudo → `approvals.deny` → yolo/off →
  permanent allowlist → per-context mode). It is a *sibling* path for shell
  commands, **not** the one a plugin's `pre_tool_call` directive travels. Cited as
  a tier-order reference only.
- **`save_permanent_allowlist`** persists an `always` grant. That is persistent
  *allow-listing*, **not** the days-later *request replay* of the NanoClaw catalog
  (the native path is same-turn; it does not resurrect a timed-out request — see §5).

Citations (native, release tree): `tools/approval.py:4166` `request_tool_approval`,
`:3760` `_run_approval_gate`, `:3197` `save_permanent_allowlist`, `:4730`
`check_all_command_guards` (tier-order reference); directive resolver
`hermes_cli/plugins.py:6771` `_resolve_block_from_details`. Advisory `main:`
anchors: `tools/approval.py:914`, `:783`, `:978`, `:342`. Veto predicate order:
baseline `plugins/nv-fleet-gates/__init__.py` `_gate`.

## 4. Drift detection + the two-tier fail-safe

The original GOV-F22 defect was a **fail-open** one: a name-keyed gate that
*silently* stops matching when a tool is renamed. Registry drift maps to two
distinct outcomes — the mapping is what this runbook records; the scan criteria,
the deferral conditions, and the backstop implementation are documented once, in
[Restricted-set integrity: load-time refusal + gate-time backstop](../developer-guide/plugins/nv-fleet-gates.md#restricted-set-integrity-load-time-refusal--gate-time-backstop),
which this page does **not** duplicate:

- **Load-time tier — loud detection, not a fail-close.** On an essentially-complete
  registry a renamed/removed restricted-core tool (or an un-gated shell-capable one)
  turns the *silent* missed match into a *loud* plugin-load failure. It does **not**
  fail-close Hermes — a disabled veto is not enforcing calls (see the caveat).
- **Gate-time tier — the authoritative per-call fail-closed backstop.** On a
  materially partial / still-initializing registry the veto stays loaded and
  `_restricted_block()` denies **every gated call fail-closed** until restricted-set
  integrity is restored.

Renames are absorbed before the match by vendored **alias-canonicalisation**
(`canonicalise()` over the vendored `_LEGACY_TOOL_ALIASES`), so the pinned spelling
and the `main` spelling hit the same predicate.

:::warning A fail-load is loud detection — it does not by itself refuse gateway service
Raising in `register()` disables the plugin, and a disabled veto is **removed** — for
the rest of the toolset that is fail-**open**, not fail-closed. So the load-time
refusal's value is **silent → loud**, not a service refusal; the authoritative
per-call fail-closed guarantee is the gate-time backstop `_restricted_block()`.
Making the gateway *refuse to serve* while the plugin's `error` is set is out of scope
for this surface (→ GOV-F22.a, or an existing baseline startup check).
:::

Citations (baseline, symbolic — by function name, since fork-branch line numbers
drift): `plugins/nv-fleet-gates/__init__.py` `register`, `_scan_restricted`,
`_registry_essentially_complete`, `_restricted_block`;
`plugins/nv-fleet-gates/aliases.py` `canonicalise`;
`plugins/nv-fleet-gates/predicates.py` `looks_dangerous_name`.

## 5. What is not covered (residual → GOV-F22.a)

The covering surface keys its catalog on **registry tool names**, and Hermes's
native approval gate is **synchronous** (bounded by `approvals.timeout`). Two parts
of the NanoClaw guard catalog are therefore **not** covered here, and take no
`covered` credit:

1. **A nominal-action catalog decoupled from tool names** — arbitrary
   `guarded(<action>)` / `unguarded()` grain over plugin handlers, beyond the
   tool-name-keyed restricted set.
2. **Days-later grant replay** — a `hold` that persists the blocked request and
   *replays* it when a human approves hours or days later. The native path is
   same-turn (block / approve within the timeout); it does not resurrect a
   timed-out request.

Both are dispositioned to follow-up row **GOV-F22.a** (`CONFIGURE | BUILD`): a small
plugin owning a `held_requests` store keyed by `rule_key`, replayed on a later
approval — *if* the fleet outcomes actually require days-later replay. Design caveat
for that row (recorded so this page stays honest, not built here): `ctx.dispatch_tool`
calls `registry.dispatch` directly and would **bypass** `pre_tool_call`, and
`ctx.inject_message` starts a **new turn** rather than replaying the exact request —
so GOV-F22.a must re-validate through the gate and bind an authorization token,
never raw re-dispatch.

## 6. See also

- [nv-fleet-gates — the fleet's single pre_tool_call veto](../developer-guide/plugins/nv-fleet-gates.md) — the plugin reference; the [Restricted-set integrity](../developer-guide/plugins/nv-fleet-gates.md#restricted-set-integrity-load-time-refusal--gate-time-backstop) section is the mechanics this page maps.
- [Fleet approvals policy (on-call runbook)](./fleet-approvals-policy.md) — the fleet-uniform `approvals.*` config and the `command_allowlist` / `approvals.deny` floor that the human-approval path (the `hold` target) runs under.
