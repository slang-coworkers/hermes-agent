---
title: slang-coworkers Pause, Restart & Wake
description: Mapping the NanoClaw control-plane behaviours (on_wake message, race-free restart, paused kill switch) onto the native Hermes ESTOP sentinel, drain-then-exit gateway restart, and kanban worker-recovery surfaces.
---

# slang-coworkers Pause, Restart & Wake

NanoClaw's coworker runtime had three control-plane behaviours: an **on_wake
message** re-delivered to a worker on every (re)spawn so it re-read its task and
continued; a **race-free restart** that drained in-flight work before exiting
and re-armed interrupted sessions on the next boot; and a **paused kill switch**
that halted new work for one coworker (or the whole fleet) without killing what
was already running.

Every one of these is already **native Hermes** behaviour. This row therefore
**adopts** the native surface — there is **no slang-coworkers plugin and no core
patch** — and this page maps the three NanoClaw behaviours onto the Hermes
surfaces that provide them. The hermetic acceptance test
`tests/test_iso_f12_lifecycle_acceptance.py` proves the load-bearing behaviours.

All `tag:` `file:line` references are relative to the pinned release tree
(`NousResearch/hermes-agent`, tag `v2026.8.31` @ `29112bef`) — the authoritative
citation source. Each mapping row also carries a `main:` line for the same
symbol on the moving upstream branch; `main:` line numbers are verified against
`NousResearch/hermes-agent` `upstream/main` @ `d177b119e9`. Several modules were
split out of the `gateway/run.py` and `hermes_cli/kanban_db.py` monoliths after
the tag, so a `main:` path can differ from its `tag:` path (noted per mapping).

## Behaviour mapping

| NanoClaw behaviour | Hermes surface | Citation (tag: / main:) |
|---|---|---|
| **Paused kill switch** — `hermes -p <bot> pause`/`resume` (and in-band `/pause`) halt new work without killing what is already running | Profile-aware ESTOP sentinel: `is_engaged()` gates new gateway turns, cron fires and kanban spawns, and `paused_reply()` answers a paused inbound with a notice; in-flight and internal work are never touched | tag: `agent/estop.py:93` `is_engaged` · main: `agent/estop.py:52` `is_engaged` |
| **Race-free restart** — drain in-flight work, then exit for an external supervisor to restart the process | `GatewayRunner.request_restart` marks draining and defers `stop()` until the active turn finishes, then `stop()` exits with `GATEWAY_SERVICE_RESTART_EXIT_CODE` (`EX_TEMPFAIL`, 75); the `resume_pending` marker re-arms interrupted sessions on the next boot | tag: `gateway/run.py:12447` `request_restart` · main: `gateway/run_shutdown.py:1518` `request_restart` |
| **on_wake message** — re-delivered to a worker on every (re)spawn so it re-reads its task | Native kanban worker recovery: `dispatch_once` reclaims a stale/crashed card and the real `_default_spawn` respawns a worker on the **same** durable task id, and `KANBAN_GUIDANCE`/`build_worker_context` make it re-read its card and prior attempts on respawn | tag: `hermes_cli/kanban_db.py:10720` `_default_spawn` · main: `hermes_cli/kanban_db_dispatch.py:2513` `_default_spawn` |

## Paused kill switch → the ESTOP sentinel

`hermes -p <bot> pause` writes a profile-scoped `ESTOP` sentinel through
`cmd_pause` → `engage(reason=…)` (tag `hermes_cli/subcommands/pause.py:17` · main
`:13`; `engage` tag `agent/estop.py:110`), and `hermes -p <bot> resume` removes it
through `cmd_resume` → `disengage()` (tag `hermes_cli/subcommands/pause.py:36` ·
main `:31`; `disengage` tag `agent/estop.py:129`). An operator on a messaging
platform can do the same in-band with `/pause`, handled by
`_handle_pause_command` (tag `gateway/run.py:17838` · main
`gateway/run_busy.py:882`).

While the sentinel is present, `is_engaged()` (tag `agent/estop.py:93` · main
`agent/estop.py:52`) returns `True` and new control-plane dispatch stops at three
native gates that read it:

- the **gateway turn gate** answers a new inbound turn with the `paused_reply()`
  notice instead of running it — it reads `paused_reply()` directly (tag
  `gateway/run.py:18261`, the `if not is_internal:` guard, through the drop at
  `:18314`);
- the **cron scheduler** skips its due-job scan through the shared `check_paused()`
  helper (tag `cron/scheduler.py:7921`);
- the **kanban dispatcher** declines to spawn — `_kanban_dispatch_allowed()`
  returns `False` via the same `check_paused()` helper (tag
  `gateway/kanban_watchers.py:84` · main `gateway/kanban_watchers_common.py:100`).

Resuming clears the sentinel and every gate restores on the next tick. The switch
never touches in-flight or internal work (see *Wake and in-flight work* below).

## Race-free restart → drain-then-exit under a supervisor

`GatewayRunner.request_restart` (tag `gateway/run.py:12447` · main
`gateway/run_shutdown.py:1518`) marks the runner draining and defers `stop()`
until in-flight work finishes. That wait is **bounded** by a configurable drain
timeout (`agent.restart_drain_timeout`): a turn that finishes inside the window is
never cut off, but if the timeout expires `stop()` marks the still-running
sessions `resume_pending` and interrupts them (tag `gateway/run.py:16180`, the
drain-timeout branch; `_interrupt_running_agents` tag `gateway/run.py:11400`; main
`gateway/run_shutdown.py:1716`) rather than hanging the restart. When the drain
finishes, `stop()` assigns `GATEWAY_SERVICE_RESTART_EXIT_CODE` — `os.EX_TEMPFAIL`
(75) — (constant tag `gateway/restart.py:11` · main `gateway/restart.py:10`;
assigned in `stop()` tag `gateway/run.py:16507` · main
`gateway/run_shutdown.py:1930`) so an external service manager restarts the
process. Hermes recognises that it is running under such a supervisor from the
environment via `EXTERNAL_GATEWAY_SUPERVISOR_ENV` / `is_gateway_supervisor_process`
(tag `gateway/restart.py:22` / `:66` · main `gateway/restart.py:34` / `:77`); both
the constant and the check live in `restart.py`. The CLI self-restart path is
`_request_gateway_self_restart` (tag `hermes_cli/gateway.py:294` · main `:253`).

Across the restart, the `resume_pending` marker on `SessionEntry` (tag
`gateway/session.py:854` · main `gateway/session.py:512`) re-arms interrupted
sessions. `SessionStore.mark_resume_pending` / `clear_resume_pending` (tag
`gateway/session.py:3284` / `:3313` · main `gateway/session_lifecycle.py:199` /
`:210`) persist the marker to `state.db`, and the after-turn predicate
`_should_clear_resume_pending_after_turn` (tag `gateway/run.py:4532` · main
`gateway/run.py:3139`) keeps it on an interrupted, failed or partial result and
clears it only on a clean completion. On the next boot,
`_schedule_resume_pending_sessions` (tag `gateway/run.py:13064`) redispatches each
**eligible** `resume_pending` session — a recent marker, an allowed recovery
reason (`restart_timeout`/`shutdown_timeout`/`restart_interrupted`), an authorized
source and an available adapter, with no run already active — as exactly one
internal continuation turn; sessions that do not qualify stay pending for a later
reconnect or user message.

## Wake and in-flight work are exempt from the kill switch

The ESTOP gate only applies to *new inbound* turns: its guard is `if not
is_internal:` (tag `gateway/run.py:18261`). `deliver_wake` (tag
`gateway/wake.py:56` · main `gateway/wake.py:54`) is the separate creator-session
notification that wakes an existing session after a background completion — it is
**not** the worker-recovery path in the mapping above. A **push-capable** wake is
delivered as an internal turn — its push route builds a synthetic
`MessageEvent(..., internal=True)` and calls `adapter.handle_message`, which the
`if not is_internal:` guard lets straight through the pause gate. A **non-push**
wake instead self-POSTs the session id and never traverses the inbound gate at
all, taken when `adapter_supports_push` is `False` (tag `gateway/wake.py:45` ·
main `gateway/wake.py:30`, i.e. `getattr(adapter, "supports_async_delivery",
True)`). Either way a pause never drops a wake.

The persistent per-profile tool sandbox is **reused, not torn down**, when a turn
ends: `cleanup()` in persist mode leaves the running container untouched and only
drops the in-process handle (tag `tools/environments/docker.py:1997`). The next
environment construction attempts label-keyed reuse of that container (tag
`tools/environments/docker.py:1448`) and builds a fresh one only when no
compatible container exists (`if not reused:` tag `tools/environments/docker.py:1517`).
Pause and wake never request a teardown themselves, so neither rebuilds a
coworker's sandbox out from under its in-flight work.

## The one documented difference: a paused inbound is answered, not queued

This is the single behavioural gap between NanoClaw's on_wake-replay and the
Hermes adopt. In NanoClaw a message that arrived while paused was spooled and
replayed on resume. In Hermes a paused **inbound** turn is answered with the
`paused_reply()` **notice** and then **dropped** — it is **not queued** for
replay. The gate returns the notice string in place of enqueueing the turn (tag
`gateway/run.py:18314` `return _paused_notice`, logged at `:18310`). This is
distinct from the startup-restore queue `_queue_startup_restore_event` (tag
`gateway/run.py:12527`), which is restart continuity, not pause replay.

If replaying paused inbounds is ever required, it belongs in a
`pre_gateway_dispatch` hooks plugin that spools and replays them — deliberately
out of scope for this adopt row, and recorded here as the one difference.

## Recognized commands stay live while paused

Recognized slash commands still pass the gate while paused, so a messaging-only
operator can resume in-band: `/status`, `/help` and `/pause off` are allowed
through (tag `gateway/run.py:18267`-`18307`). Running sessions and approval
replies pass too; only brand-new inbound turns receive the notice.

## Fleet vs per-bot

`hermes -p <bot> pause` writes the sentinel under that one profile's home, so a
sibling coworker keeps running. The fleet-root `<root>/ESTOP` sentinel engages
every profile at once (tag `agent/estop.py:74`-`107`; profile-root derivation
`hermes_constants.py:183`). `is_engaged()` is deliberately fleet-root-inclusive:
it returns `True` when either the profile sentinel or the fleet-root sentinel is
present, which is why a fleet-wide pause halts every bot while a per-bot pause
isolates one.

## How to verify

The hermetic acceptance test `tests/test_iso_f12_lifecycle_acceptance.py` drives
these behaviours on a temporary `HERMES_HOME` (run it with `scripts/run_tests.sh
tests/test_iso_f12_lifecycle_acceptance.py`, never bare `pytest`). Eight of its
nine tests (`test_ac_iso_f12_1`..`_7` and `_9`) are green-on-base
adopt-conformance proofs of the native surface; the ninth, `test_ac_iso_f12_8`,
is the own-diff doc-content negative control — it fails on the base tree (this
page absent) and passes once this page exists.
