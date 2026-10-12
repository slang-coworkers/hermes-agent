"""Kanban board watcher methods for GatewayRunner.

Background loops that subscribe to kanban boards, deliver notifications and
artifacts, and drive the multi-agent dispatcher. They use only ``self`` state,
so they live on a mixin ``GatewayRunner`` inherits. Per-tick work lives in
``kanban_watchers_notifier`` / ``kanban_watchers_dispatcher``; shared plumbing
in ``kanban_watchers_common``.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import time
from pathlib import Path
from typing import Any, Optional

from gateway.kanban_watchers_common import (
    _acquire_singleton_lock,
    _kanban_dispatch_allowed,
    _release_singleton_lock,
    _resolve_auto_decompose_settings,
    _gc_retention_days,
    _to_thread_process_service,
    logger,
)
from gateway.kanban_watchers_notifier import _KanbanNotification, _notifier_collect
from gateway.kanban_watchers_dispatcher import (
    _KanbanDispatcher,
    _log_spawn_results,
    _resolve_dispatcher_settings,
)

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"}
_GC_INTERVAL_SECONDS = 3600.0
_HEALTH_WINDOW = 6

# Generic durable-retry sub policy (retry_policy='durable'): a failing delivery
# is retried with exponential capped backoff and NEVER dropped; on sustained
# failure an operator alert is logged. Values are deliberately small
# at the low end (a transient blip retries within seconds) and capped so a dead
# owner does not hot-loop the notifier.
_DURABLE_BACKOFF_BASE_SECONDS = 2.0
_DURABLE_BACKOFF_CAP_SECONDS = 300.0
# Consecutive failures after which the durable sub earns a loud operator alert
# (still never dropped).
_DURABLE_ALERT_THRESHOLD = 5


class GatewayKanbanWatchersMixin:
    """Kanban watcher / notifier / dispatcher loops for GatewayRunner."""

    def _owns_kanban_dispatcher_lock(self) -> bool:
        return getattr(self, "_kanban_dispatcher_lock_handle", None) is not None

    def _release_kanban_dispatcher_lock(self) -> None:
        """Clear notifier-visible ownership before releasing the OS lock."""
        handle = getattr(self, "_kanban_dispatcher_lock_handle", None)
        self._kanban_dispatcher_lock_handle = None
        _release_singleton_lock(handle)

    async def _sleep_between_ticks(self, interval: float) -> None:
        """Sleep *interval* (floored to 1s) in 1s slices so stop() never waits a full interval."""
        interval = max(interval, 1.0)
        slept = 0.0
        while slept < interval and self._running:
            await asyncio.sleep(min(1.0, interval - slept))
            slept += 1.0

    async def _kanban_notifier_watcher(self, interval: float = 5.0) -> None:
        """Poll ``kanban_notify_subs`` and deliver terminal events to users.

        Per subscription, claims ``task_events`` newer than the stored cursor
        (kinds in TERMINAL_KINDS), sends one message per event, then advances
        the cursor. The subscription is removed only when the task is
        ``archived``: ``done`` is reversible, so the cursor — not unsubscribing
        — is the dedup mechanism (unsub-on-terminal dropped users when the
        dispatcher respawned a crashed task). All SQLite work runs in a thread;
        one tick's failure never stops the next.
        """
        try:
            from hermes_cli.config import load_config as _load_config

            cfg = _load_config()
            kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        except Exception as exc:
            logger.warning("kanban notifier: cannot load config (%s); continuing enabled", exc)
            kanban_cfg = {}
        if not kanban_cfg.get("notify_in_gateway", True):
            logger.info("kanban notifier: disabled via config kanban.notify_in_gateway=false")
            return

        from gateway.config import Platform as _Platform
        try:
            from hermes_cli import kanban_db as _kb
        except Exception:
            logger.warning("kanban notifier: kanban_db not importable; notifier disabled")
            return

        sub_fail_counts: dict[tuple, int] = getattr(self, "_kanban_sub_fail_counts", {})
        self._kanban_sub_fail_counts = sub_fail_counts
        notifier_profile = getattr(self, "_kanban_notifier_profile", None) or self._active_profile_name()
        self._kanban_notifier_profile = notifier_profile

        # Initial delay so the gateway can finish wiring adapters.
        await asyncio.sleep(5)

        # Stale done-sub GC: subs survive ``done``, so boards that never
        # archive would accumulate rows scanned every tick. One DELETE per
        # board, at startup (0 → first tick) and at most hourly.
        _gc_next_at = 0.0

        while self._running:
            try:
                _gc_due = time.monotonic() >= _gc_next_at
                _retention = 30
                if _gc_due:
                    _gc_next_at = time.monotonic() + _GC_INTERVAL_SECONDS
                    _retention = _gc_retention_days()

                deliveries = await asyncio.to_thread(
                    _notifier_collect, self, _kb,
                    notifier_profile=notifier_profile, gc_due=_gc_due, gc_retention_days=_retention,
                )
                for d in deliveries:
                    await _KanbanNotification(
                        self, d, platform_cls=_Platform, sub_fail_counts=sub_fail_counts,
                    ).deliver()
            except Exception as exc:
                logger.warning("kanban notifier tick failed: %s", exc)
            await self._sleep_between_ticks(interval)

    def _kanban_sub_op(self, board: Optional[str], op: str, sub: dict, **extra: Any) -> Any:
        """Sync helper (runs in to_thread): call ``kanban_db_notify.<op>`` for one subscription on its board."""
        from hermes_cli import kanban_db_connect as _kbc
        from hermes_cli import kanban_db_notify as _kbn
        conn = _kbc.connect(board=board)
        try:
            return getattr(_kbn, op)(
                conn, task_id=sub["task_id"], platform=sub["platform"], chat_id=sub["chat_id"],
                thread_id=sub.get("thread_id") or "", **extra,
            )
        finally:
            conn.close()

    def _kanban_advance(self, sub: dict, cursor: int, board: Optional[str] = None) -> None:
        self._kanban_sub_op(board, "advance_notify_cursor", sub, new_cursor=cursor)

    def _kanban_unsub(self, sub: dict, board: Optional[str] = None) -> None:
        self._kanban_sub_op(board, "remove_notify_sub", sub)

    def _kanban_rewind(self, sub: dict, claimed_cursor: int, old_cursor: int, board: Optional[str] = None) -> None:
        """Undo a claimed notification cursor after send failure."""
        self._kanban_sub_op(board, "rewind_notify_cursor", sub, claimed_cursor=claimed_cursor, old_cursor=old_cursor)

    def _kanban_claim(
        self, sub: dict, expected_cursor: int, token: str,
        lease_until: int, now: int, board: Optional[str] = None,
    ) -> bool:
        """Sync helper: take the durable-delivery lease on ``sub``. Runs in
        to_thread. Returns True iff this drainer claimed the event range."""
        return self._kanban_sub_op(
            board, "claim_notify_sub_lease", sub,
            expected_cursor=expected_cursor, token=token, lease_until=lease_until, now=now,
        )

    def _kanban_advance_release(
        self, sub: dict, expected_cursor: int, new_cursor: int, token: str,
        board: Optional[str] = None,
    ) -> bool:
        """Sync helper: advance the cursor and release the lease in one CAS
        after a confirmed delivery. Runs in to_thread."""
        return self._kanban_sub_op(
            board, "advance_and_release_notify_sub", sub,
            expected_cursor=expected_cursor, new_cursor=new_cursor, token=token,
        )

    def _kanban_release(
        self, sub: dict, token: str, board: Optional[str] = None,
    ) -> None:
        """Sync helper: release the durable-delivery lease held by ``token``
        without advancing the cursor. Runs in to_thread."""
        self._kanban_sub_op(board, "release_notify_sub_lease", sub, token=token)

    def _render_durable_event(self, ev, task, sub, board_slug) -> Optional[str]:
        """Render ONE durable event to its wake text.

        The body is built ONLY from the event's own immutable payload / kind and
        the sub's task id — never from mutable task fields (assignee, title) —
        because the wake carries a stable ``Idempotency-Key`` and the api_server
        fingerprint hashes the body: a body that changed between a persisted-
        but-unacked attempt and its retry would defeat the dedup and double-run.
        A ``notification`` event carries the caller's free-form ``message``
        (already the rendered event); other kinds get a concise per-event line.
        """
        if ev.kind == "notification":
            note = ""
            if isinstance(ev.payload, dict):
                note = str(ev.payload.get("message") or "")
            return note or f"Kanban {sub['task_id']} notification"
        return f"Kanban {sub['task_id']}: {ev.kind}"

    def _note_durable_failure(self, sub_key, cause) -> None:
        """Record a durable-sub delivery failure: bump the fail count, schedule
        an exponential capped backoff, and — on sustained failure — emit a loud
        operator alert. Never drops the sub."""
        fail_counts = getattr(self, "_kanban_sub_fail_counts", None)
        if fail_counts is None:
            fail_counts = self._kanban_sub_fail_counts = {}
        backoff = getattr(self, "_kanban_durable_backoff", None)
        if backoff is None:
            backoff = self._kanban_durable_backoff = {}
        fails = fail_counts.get(sub_key, 0) + 1
        fail_counts[sub_key] = fails
        delay = min(
            _DURABLE_BACKOFF_CAP_SECONDS,
            # Cap the exponent before computing it — an unbounded shift on a
            # long-poisoned sub is pointless work (the cap dominates anyway).
            _DURABLE_BACKOFF_BASE_SECONDS * (2 ** min(fails - 1, 20)),
        )
        backoff[sub_key] = {"next_attempt": time.monotonic() + delay, "fails": fails}
        if fails >= _DURABLE_ALERT_THRESHOLD:
            logger.error(
                "kanban notifier: DURABLE sub %s sustained delivery failure "
                "(%d attempts, backoff %.1fs) — NOT dropping; operator "
                "attention required: %s",
                sub_key, fails, delay, cause,
            )
        else:
            logger.warning(
                "kanban notifier: durable wake failed for %s "
                "(attempt %d, backoff %.1fs): %s",
                sub_key, fails, delay, cause,
            )

    async def _deliver_durable_notifications(self, d: dict) -> None:
        """Crash-safe, per-event delivery of a durable notify sub to its owner.

        Peek-then-advance: each event is delivered individually and the cursor
        advances to that event id ONLY after ``deliver_wake`` succeeds — an
        api_server self-post that confirmed the persist ack (it raises
        otherwise), deduped by a stable Idempotency-Key. Confirmable durable
        delivery exists only for the api_server transport (its self-post carries
        the persist ack); a durable sub on any other (push) transport, or one
        whose route this gateway cannot authorize, is retained and alerted
        rather than advanced. A failure stops at that event (cursor contiguity),
        applies capped backoff, and NEVER deletes the sub; the next tick retries
        from here.
        """
        from gateway.config import Platform as _Platform
        from gateway.kanban_watchers_notifier import _KanbanNotification, _adapter_for_subscription
        from gateway.wake import (
            _RETRY_DELAYS_SECONDS,
            WAKE_TURN_TIMEOUT_SECONDS,
            adapter_supports_push,
            deliver_wake,
        )

        sub = d["sub"]
        task = d.get("task")
        board_slug = d.get("board")
        events = d.get("events") or []
        owner_profile = sub.get("notifier_profile") or None
        sub_key = (
            sub["task_id"], sub["platform"],
            sub["chat_id"], sub.get("thread_id") or "",
        )

        fail_counts = getattr(self, "_kanban_sub_fail_counts", None)
        if fail_counts is None:
            fail_counts = self._kanban_sub_fail_counts = {}
        backoff = getattr(self, "_kanban_durable_backoff", None)
        if backoff is None:
            backoff = self._kanban_durable_backoff = {}

        # Still inside the capped-backoff window from a prior failure — leave the
        # cursor untouched and retry a later tick.
        state = backoff.get(sub_key)
        if state and state.get("next_attempt", 0.0) > time.monotonic():
            return

        try:
            plat = _Platform((sub.get("platform") or "").lower())
        except ValueError:
            # An unroutable platform must not hot-loop: retain, back off, alert.
            self._note_durable_failure(
                sub_key, f"durable sub has unroutable platform {sub.get('platform')!r}"
            )
            return
        # The same fail-closed route rule the batched path applies: a secondary
        # owner never borrows the default profile's adapter unless the route (or
        # the served profile's own session store) authorizes it. Reads the
        # served store, so it runs off the event loop.
        adapter = await asyncio.to_thread(_adapter_for_subscription, self, plat, sub, owner_profile)
        if adapter is None:
            self._note_durable_failure(
                sub_key, f"no authorized {plat.value} route for owner {owner_profile or 'default'}"
            )
            return
        if adapter_supports_push(adapter):
            # A push wake (synthetic MessageEvent through handle_message) returns
            # after SPAWNING the turn — it is NOT a persistence ack, so advancing
            # the cursor on its return would drop the event if the spawned turn
            # fails. Durable delivery is confirmable only for api_server
            # (self-post + X-Hermes-Turn-Persisted ack); a durable sub on a push
            # transport is therefore retained (never advanced, never dropped) and
            # surfaces as a sustained-failure alert rather than being delivered.
            self._note_durable_failure(
                sub_key,
                f"durable delivery is unconfirmable on push transport {plat.value}",
            )
            return

        # The served owner's runtime scope, exactly as the batched wake uses it;
        # a served (non-default) profile then takes the in-process route, where
        # require_persist_ack fails closed (no persistence receipt there).
        notification = _KanbanNotification(
            self, d, platform_cls=_Platform, sub_fail_counts=fail_counts,
        )
        notification.plat = plat
        served_profile = notification._served_wake_profile()

        # Hold the owner-fenced lease comfortably beyond the whole deliver_wake
        # envelope (WAKE_TURN_TIMEOUT_SECONDS per attempt across 1 +
        # len(_RETRY_DELAYS_SECONDS) attempts) so it can never expire
        # mid-delivery and let a second drainer double-deliver. A lost holder —
        # process death, task cancellation, or an unhandled failure that skips
        # the release — waits out this expiry, and recovery is then
        # at-least-once: the persist ack blocks a cursor advance, but the finite
        # idempotency cache cannot guarantee dedup once the lease has outlived
        # the cache TTL.
        lease_seconds = int(
            WAKE_TURN_TIMEOUT_SECONDS * (1 + len(_RETRY_DELAYS_SECONDS))
            + sum(_RETRY_DELAYS_SECONDS)
        ) + 300

        # Peek-then-advance: the cursor advances only after a persist-confirmed
        # ack, so the expected pre-delivery cursor starts at the peeked snapshot
        # and moves forward one event at a time as each delivery is confirmed.
        expected_cursor = int(d.get("old_cursor") or 0)
        for ev in events:
            text = self._render_durable_event(ev, task, sub, board_slug)
            token = secrets.token_hex(16)
            now = int(time.time())
            # Owner-fenced claim: take an exclusive, non-advancing lease on this
            # sub row at the current cursor. rowcount 0 ⇒ another live drainer
            # holds the range (or the cursor already moved) ⇒ stop without
            # delivering, so concurrent drainers deliver each event exactly once.
            claimed = await _to_thread_process_service(
                self._kanban_claim, sub, expected_cursor, token,
                now + lease_seconds, now, board_slug,
            )
            if not claimed:
                return
            if text is None:
                # Nothing to deliver for this kind — advance past it (cursor
                # contiguity) and release the lease in one CAS.
                await _to_thread_process_service(
                    self._kanban_advance_release, sub, expected_cursor, ev.id,
                    token, board_slug,
                )
                expected_cursor = ev.id
                continue
            key = None
            if isinstance(ev.payload, dict):
                key = ev.payload.get("idempotency_key")
            key = key or f"{sub['task_id']}:{ev.id}"
            try:
                async with notification._owner_scope():
                    await deliver_wake(
                        adapter,
                        text=text,
                        session_id=sub["chat_id"],
                        profile=served_profile,
                        idempotency_key=key,
                        require_persist_ack=True,
                    )
            except Exception as exc:
                # Delivery unconfirmed: release the lease immediately (do NOT
                # wait for expiry) so the next tick re-delivers, leave the
                # cursor unmoved, back off, and NEVER drop the durable sub.
                await _to_thread_process_service(
                    self._kanban_release, sub, token, board_slug,
                )
                self._note_durable_failure(sub_key, exc)
                return
            # Delivery confirmed (deliver_wake raised otherwise) → advance the
            # cursor past this event and release the lease atomically (SEEN).
            await _to_thread_process_service(
                self._kanban_advance_release, sub, expected_cursor, ev.id,
                token, board_slug,
            )
            expected_cursor = ev.id
            fail_counts.pop(sub_key, None)
            backoff.pop(sub_key, None)
            logger.info(
                "kanban notifier: durable wake delivered task=%s event=%s owner=%s",
                sub["task_id"], ev.id, owner_profile or "default",
            )

    async def _deliver_kanban_artifacts(self, *, adapter, chat_id: str, metadata: dict, event_payload: Optional[dict], task) -> None:
        """Upload artifact files referenced by a completed kanban task.

        Sources, in priority order: ``event_payload['artifacts']``,
        ``event_payload['summary']``, then ``task.result`` (legacy). Paths are
        deduplicated, missing files are skipped (may be mentioned for
        reference only), and upload errors are logged, never raised.
        """
        raw_paths: list[str] = []
        prose_paths: list[str] = []
        if isinstance(event_payload, dict):
            raw = event_payload.get("artifacts")
            if isinstance(raw, (list, tuple)):
                raw_paths += [item for item in raw if isinstance(item, str)]
            summary = event_payload.get("summary")
            if isinstance(summary, str) and summary:
                prose_paths += adapter.extract_local_files(summary)[0]
        if task is not None and getattr(task, "result", None):
            prose_paths += adapter.extract_local_files(str(task.result))[0]
        # A staged copy and the scratch original it was copied from are the
        # same deliverable; on a review handoff the original still exists, so
        # prose mentions of it must not upload the file a second time.
        staged_names = {os.path.basename(p) for p in raw_paths}
        raw_paths += [p for p in prose_paths if os.path.basename(p) not in staged_names]
        candidates: list[str] = []
        for path in raw_paths:
            expanded = os.path.expanduser(path) if path else ""
            if expanded and expanded not in candidates and os.path.isfile(expanded):
                candidates.append(expanded)
        if not candidates:
            return

        from gateway.platforms.base import BasePlatformAdapter
        candidates = BasePlatformAdapter.filter_local_delivery_paths(candidates)
        if not candidates:
            return

        from urllib.parse import quote as _quote

        # Images ride one send_multiple_images call (batch uploads on Signal/Slack).
        image_paths = [p for p in candidates if Path(p).suffix.lower() in _IMAGE_EXTS]
        other_paths = [p for p in candidates if Path(p).suffix.lower() not in _IMAGE_EXTS]
        if image_paths:
            try:
                batch = [(f"file://{_quote(p)}", "") for p in image_paths]
                await adapter.send_multiple_images(chat_id=chat_id, images=batch, metadata=metadata)
            except Exception as exc:
                logger.warning("kanban notifier: image batch upload failed: %s", exc)
        for path in other_paths:
            try:
                if Path(path).suffix.lower() in _VIDEO_EXTS:
                    await adapter.send_video(chat_id=chat_id, video_path=path, metadata=metadata)
                else:
                    await adapter.send_document(chat_id=chat_id, file_path=path, metadata=metadata)
            except Exception as exc:
                logger.warning("kanban notifier: artifact upload (%s) failed: %s", path, exc)

    def _kanban_dispatcher_boot(self) -> Optional[tuple]:
        """Resolve config, kanban_db and the singleton lock; None when the dispatcher must not run.

        Config is read once at boot (restart to apply), except the auto-decompose
        toggle which is re-read every tick. The env var is an escape hatch to
        disable without editing YAML.
        """
        try:
            from hermes_cli.config import load_config as _load_config
        except Exception:
            logger.warning("kanban dispatcher: config loader unavailable; disabled")
            return None
        env_override = os.environ.get("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "").strip().lower()
        if env_override in {"0", "false", "no", "off"}:
            logger.info("kanban dispatcher: disabled via HERMES_KANBAN_DISPATCH_IN_GATEWAY env")
            return None
        try:
            cfg = _load_config()
        except Exception as exc:
            logger.warning("kanban dispatcher: cannot load config (%s); disabled", exc)
            return None
        kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        if not kanban_cfg.get("dispatch_in_gateway", True):
            logger.info("kanban dispatcher: disabled via config kanban.dispatch_in_gateway=false")
            return None
        try:
            from hermes_cli import kanban_db as _kb
        except Exception:
            logger.warning("kanban dispatcher: kanban_db not importable; dispatcher disabled")
            return None

        # Single-dispatcher backstop (see _acquire_singleton_lock). The lock
        # lives at the machine-global kanban root, so it serialises ALL gateways.
        self._kanban_dispatcher_lock_handle = None
        _lock_path = _kb.kanban_home() / "kanban" / ".dispatcher.lock"
        _lock_handle, _lock_state = _acquire_singleton_lock(_lock_path)
        if _lock_state == "contended":
            logger.info("kanban dispatcher: another gateway already holds the dispatcher "
                        "lock (%s); this gateway will NOT dispatch.", _lock_path)
            return None
        if _lock_state == "held":
            self._kanban_dispatcher_lock_handle = _lock_handle  # hold for process lifetime
            logger.info("kanban dispatcher: holding singleton dispatcher lock (%s)", _lock_path)
        else:
            logger.warning("kanban dispatcher: advisory lock unavailable at %s; proceeding "
                           "on config control alone.", _lock_path)
        return _load_config, _kb, kanban_cfg

    async def _kanban_dispatcher_watcher(self) -> None:
        """Embedded kanban dispatcher — one tick every `dispatch_interval_seconds`.

        Gated by `kanban.dispatch_in_gateway` (default True); when false the
        loop exits and an external `hermes kanban daemon` is expected. Each
        tick runs :func:`kanban_db_dispatch.dispatch_once` in a thread; one tick's
        failure never stops the next. Shutdown: ``self._running`` is checked
        between ticks and the in-flight ``to_thread`` returns on its own.
        """
        boot = self._kanban_dispatcher_boot()
        if boot is None:
            return
        _load_config, _kb, kanban_cfg = boot
        settings = _resolve_dispatcher_settings(kanban_cfg, _kb)
        interval = settings.interval

        # Initial delay so adapters are wired before workers spawn (matches the notifier).
        await asyncio.sleep(5)

        # Health telemetry (mirrors `_cmd_daemon`): warn when the ready queue
        # is non-empty but spawns are 0 for N consecutive ticks — usually a
        # broken PATH, missing venv, or credential loss.
        bad_ticks = 0
        last_warn_at = 0
        results: Optional[list] = None
        dispatcher = _KanbanDispatcher(_kb, settings)

        logger.info("kanban dispatcher: embedded in gateway (interval=%.1fs)", interval)
        while self._running:
            try:
                # Reap zombies before per-board work so a board DB failure
                # cannot block cleanup of unrelated workers.
                from hermes_cli import kanban_db_dispatch as _kbd
                pids = await _to_thread_process_service(_kbd.reap_worker_zombies)
                if pids:
                    logger.info("kanban dispatcher: reaped %d zombie worker(s), pids=%s", len(pids), pids)
            except Exception:
                logger.exception("kanban dispatcher: zombie reaper failed")

            try:
                # Emergency stop (`hermes pause`): no auto-decompose or
                # dispatch while paused; running workers finish naturally.
                if not _kanban_dispatch_allowed():
                    bad_ticks = 0
                else:
                    # Re-read the auto-decompose toggle live so disabling it
                    # takes effect on the next tick, not on restart.
                    _ad_enabled, _ad_per_tick = _resolve_auto_decompose_settings(_load_config)
                    # See #49638.
                    if _ad_enabled:
                        await _to_thread_process_service(dispatcher.auto_decompose_tick, _ad_per_tick)
                    results = await _to_thread_process_service(dispatcher.tick_once)
                    any_spawned = _log_spawn_results(results)
                    ready_pending = await _to_thread_process_service(dispatcher.ready_nonempty)
                    bad_ticks = bad_ticks + 1 if ready_pending and not any_spawned else 0
                now = int(time.time())
                if bad_ticks >= _HEALTH_WINDOW and now - last_warn_at >= 300:
                    held = _kbd.describe_suppression(res for _slug, res in (results or []))
                    logger.warning(
                        "kanban dispatcher stuck: ready queue non-empty for "
                        "%d consecutive ticks but 0 workers spawned.%s Check "
                        "profile health (venv, PATH, credentials) and "
                        "`hermes kanban list --status ready`.",
                        bad_ticks, f" Last tick held back: {held}." if held else "",
                    )
                    last_warn_at = now
            except asyncio.CancelledError:
                logger.debug("kanban dispatcher: cancelled")
                self._release_kanban_dispatcher_lock()
                raise
            except Exception:
                logger.exception("kanban dispatcher: unexpected watcher error")

            await self._sleep_between_ticks(interval)

        self._release_kanban_dispatcher_lock()


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
from typing import Callable  # noqa: F401,E402
from contextvars import Context  # noqa: F401,E402
import logging  # noqa: F401,E402
import re  # noqa: F401,E402
import sqlite3  # noqa: F401,E402


_PLUGIN_COMPAT_LAZY = {
    't': ('agent.i18n', 't'),
}


def __getattr__(name):  # PEP 562 — lazy so no import cycles
    target = _PLUGIN_COMPAT_LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib
    from hermes_cli.plugin_compat import warn_once
    warn_once(__name__, name, *target)
    return getattr(importlib.import_module(target[0]), target[1])
# ---- END PLUGIN-COMPAT ----
