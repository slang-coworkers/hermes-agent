"""ISO-F11 — Idempotent claim/ack lifecycle with retry (ADOPT-row acceptance test).

Drives the native Hermes lifecycle mechanisms directly (no plugin, no PluginManager);
because Hermes already provides them, the file passes on the stock v2026.8.31 tree.
Non-vacuity is carried by each function's differential assertion — one that a plausible
regression would flip. Hermetic — each test roots its own tmp DB; no network, no live
gateway, nothing written under ~/.hermes.
"""

import asyncio
import threading
import time

import pytest


def test_ac_iso_f11_1(tmp_path):
    """AC-ISO-F11-1: Reserving a (scope, key) with a fixed fingerprint admits exactly one run; the
    reservation survives a process restart (a fresh store on the same DB file resolves
    reused)."""
    from gateway.platforms.api_server_run_idempotency import RunIdempotencyStore

    db = str(tmp_path / "runs.db")
    fp = "fingerprint-of-request-body"
    status = {"status": "queued"}

    s1 = RunIdempotencyStore(db)
    assert s1.durable is True
    assert s1.reserve("tenant-A", "key-1", fp, "run_abc", status)[0] == "created"
    outcome, rec = s1.reserve("tenant-A", "key-1", fp, "run_zzz", status)
    assert outcome == "reused"
    assert rec["run_id"] == "run_abc"
    assert s1.reserve("tenant-A", "key-1", "other-fingerprint", "run_q", status)[0] == "conflict"
    assert s1.reserve("tenant-A", "key-2", fp, "run_def", status)[0] == "created"
    s1.close()

    s2 = RunIdempotencyStore(db)
    outcome2, rec2 = s2.lookup("tenant-A", "key-1", fp)
    assert outcome2 == "reused"
    assert rec2["run_id"] == "run_abc"
    s2.close()

    mem = RunIdempotencyStore(":memory:")
    assert mem.durable is False
    assert mem.lookup("t", "k", fp) == ("missing", None)
    mem.close()


@pytest.fixture
def kanban_conn(tmp_path, monkeypatch):
    import hermes_cli.kanban_db as kb

    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.init_db()
    conn = kb.connect()
    try:
        yield kb, conn
    finally:
        conn.close()


def _status(conn, tid):
    return conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0]


def test_ac_iso_f11_2(kanban_conn):
    """AC-ISO-F11-2: Under a real two-connection race exactly one claim wins (atomic CAS, not
    check-then-update); heartbeat_claim extends the live claim only for the winning
    claimer."""
    kb, conn = kanban_conn
    tid = kb.create_task(conn, title="lifecycle-2", assignee="w")

    # Sync both writers at the BEGIN IMMEDIATE boundary (not at claim_task entry): both
    # would-be claims finish their pre-check before either commits, so a non-atomic
    # check-then-unconditional-update implementation deterministically lets both win.
    begin_barrier = threading.Barrier(2)

    class _BeginBarrierConn:
        def __init__(self, c):
            self._c = c
            self._synced = False

        def execute(self, statement, parameters=()):
            if not self._synced and statement.strip().upper() == "BEGIN IMMEDIATE":
                self._synced = True
                begin_barrier.wait(timeout=5)
            return self._c.execute(statement, parameters)

        def __getattr__(self, name):
            return getattr(self._c, name)

    results = {}

    def worker(i):
        c = _BeginBarrierConn(kb.connect())
        try:
            results[i] = kb.claim_task(c, tid, claimer=f"worker-{i}", ttl_seconds=30)
        finally:
            c.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [i for i, r in results.items() if r is not None]
    assert len(winners) == 1
    assert sum(1 for r in results.values() if r is None) == 1
    owner = f"worker-{winners[0]}"
    lock, status, before = conn.execute(
        "SELECT claim_lock, status, claim_expires FROM tasks WHERE id=?", (tid,)
    ).fetchone()
    assert lock == owner
    assert status == "running"

    assert kb.heartbeat_claim(conn, tid, claimer=owner, ttl_seconds=3600) is True
    after = conn.execute("SELECT claim_expires FROM tasks WHERE id=?", (tid,)).fetchone()[0]
    assert after > before

    assert kb.heartbeat_claim(conn, tid, claimer=f"worker-{1 - winners[0]}") is False


def test_ac_iso_f11_3(kanban_conn, monkeypatch):
    """AC-ISO-F11-3: An expired claim is reclaimed by release_stale_claims (task returns to ready);
    a host-local running task whose worker PID is not alive is reclaimed by
    detect_crashed_workers."""
    import hermes_cli.kanban_db as kb_mod
    kb, conn = kanban_conn

    # differential control, asserted before any monkeypatch: a fresh claim is not reclaimed
    fresh = kb.create_task(conn, title="lifecycle-3-fresh", assignee="w")
    assert kb.claim_task(conn, fresh, claimer="w") is not None
    assert kb.release_stale_claims(conn) == 0
    assert _status(conn, fresh) == "running"

    reclaim = kb.create_task(conn, title="lifecycle-3-reclaim", assignee="w")
    assert kb.claim_task(conn, reclaim, claimer="w") is not None
    conn.execute("UPDATE tasks SET claim_expires=? WHERE id=?", (int(time.time()) - 100, reclaim))
    conn.commit()
    assert kb.release_stale_claims(conn) >= 1
    assert _status(conn, reclaim) == "ready"
    assert _status(conn, fresh) == "running"

    # detect_crashed_workers only inspects host-local claims and honors a launch grace.
    # Differential control: a LIVE host-local worker must survive — dropping the
    # _pid_alive guard (kanban_db.py:8924) would reclaim it and duplicate live work.
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    live_pid, dead_pid = 11111, 98765
    alive = kb.create_task(conn, title="lifecycle-3-alive", assignee="w")
    assert kb.claim_task(conn, alive) is not None     # default claimer => host-local lock
    kb._set_worker_pid(conn, alive, live_pid)
    crashed = kb.create_task(conn, title="lifecycle-3-crash", assignee="w")
    assert kb.claim_task(conn, crashed) is not None
    kb._set_worker_pid(conn, crashed, dead_pid)
    monkeypatch.setattr(kb_mod, "_pid_alive", lambda pid: pid == live_pid)
    reclaimed = kb.detect_crashed_workers(conn)
    assert crashed in reclaimed
    assert alive not in reclaimed
    assert _status(conn, crashed) == "ready"
    assert _status(conn, alive) == "running"


def _ob_state(dl, oid):
    conn = dl._connect()
    try:
        row = conn.execute(
            "SELECT state FROM delivery_obligations WHERE obligation_id=?", (oid,)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _ob_attempts(dl, oid):
    conn = dl._connect()
    try:
        row = conn.execute(
            "SELECT attempts FROM delivery_obligations WHERE obligation_id=?", (oid,)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def test_ac_iso_f11_4(tmp_path, monkeypatch):
    """AC-ISO-F11-4: The delivery ledger drives pending->attempting->delivered, bounds retries at
    MAX_ATTEMPTS, and on recovery actually prepends RECOVERED_MARKER to an in-flight
    obligation while redelivering a never-started one plainly."""
    from unittest.mock import AsyncMock, MagicMock

    import gateway.delivery_ledger as dl
    from gateway.config import Platform
    from gateway.run import GatewayRunner

    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setattr(dl, "_db_path", lambda: home / "state.db")
    # recovered rows must read as orphaned (owner not alive) for the sweep to reclaim them
    monkeypatch.setattr(dl, "_owner_alive", lambda *a, **k: False)

    assert isinstance(dl.RECOVERED_MARKER, str) and dl.RECOVERED_MARKER
    assert isinstance(dl.MAX_ATTEMPTS, int) and dl.MAX_ATTEMPTS >= 1

    def _record(oid, content):
        dl.record_obligation(obligation_id=oid, session_key="agent:main:slack:channel:" + oid,
                             platform="slack", chat_id=oid, thread_id=None, content=content)

    _record("ob-deliv", "body:deliv")
    assert _ob_state(dl, "ob-deliv") == "pending"
    dl.mark_attempting("ob-deliv")
    assert _ob_state(dl, "ob-deliv") == "attempting"
    dl.mark_delivered("ob-deliv")
    assert _ob_state(dl, "ob-deliv") == "delivered"

    _record("ob-abandon", "body:abandon")
    conn = dl._connect()
    conn.execute("UPDATE delivery_obligations SET state='attempting', attempts=? WHERE obligation_id=?",
                 (dl.MAX_ATTEMPTS, "ob-abandon"))
    conn.commit()
    conn.close()
    dl.sweep_recoverable()
    assert _ob_state(dl, "ob-abandon") == "abandoned"

    _record("ob-attempt", "body:attempt")
    dl.mark_attempting("ob-attempt")
    _record("ob-pending", "body:pending")

    adapter = MagicMock()
    adapter.send = AsyncMock(return_value=MagicMock(success=True, error=""))
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.SLACK: adapter}
    runner._profile_adapters = {}
    runner._active_profile_name = lambda: "default"
    _store = MagicMock()
    _store.clear_resume_pending = AsyncMock()
    _store._store = None
    runner.session_store = None
    runner._async_session_store = _store

    asyncio.run(runner._redeliver_pending_obligations())

    sent = [c.kwargs["content"] for c in adapter.send.call_args_list]
    assert sorted(sent) == sorted([dl.RECOVERED_MARKER + "body:attempt", "body:pending"])
    # a successful recovered send must ack the ledger (delivered), or it would replay forever
    assert _ob_state(dl, "ob-attempt") == "delivered"
    assert _ob_state(dl, "ob-pending") == "delivered"
    # the sweep incremented attempts 0->1 for each reclaimed obligation; a no-increment
    # mutant (attempts=attempts, delivery_ledger.py:370-376) leaves them at 0 and a
    # permanently-failing target would be reclaimed and redelivered forever, never abandoned
    assert _ob_attempts(dl, "ob-attempt") == 1
    assert _ob_attempts(dl, "ob-pending") == 1


def _make_store(tmp_path):
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from hermes_state import SessionDB

    # write_sessions_json=False so recovery can only come from state.db — the legacy
    # sessions.json mirror is not written and cannot mask a broken state.db path
    store = SessionStore(sessions_dir=tmp_path / "sessions",
                         config=GatewayConfig(write_sessions_json=False))
    if store._db is not None:
        store._db.close()
    store._db = SessionDB(db_path=tmp_path / "state.db")
    return store


def test_ac_iso_f11_5(tmp_path):
    """AC-ISO-F11-5: Restart safety: a turn interrupted by a restart is recovered across process
    boundaries (a fresh store on the same state.db promotes the durable active-turn
    marker to resume_pending), and the per-session turn lease serializes turns and
    fails closed when the session is already held."""
    from gateway.config import Platform
    from gateway.session import SessionSource
    from gateway.turn_lease import SessionTurnLeaseRegistry, TurnLeaseTimeoutError

    def src(platform, chat, user):
        return SessionSource(platform=platform, chat_id=chat, user_id=user, chat_type="channel")

    store1 = _make_store(tmp_path)
    a = store1.get_or_create_session(src(Platform.DISCORD, "chan", "u1"))
    assert store1.mark_turn_active(a.session_key)
    store1.get_or_create_session(src(Platform.TELEGRAM, "idle", "u2"))
    store1._db.close()
    assert not (tmp_path / "sessions" / "sessions.json").exists()

    store2 = _make_store(tmp_path)
    assert store2.recover_interrupted_turns(max_age_seconds=3600) == 1
    ea = store2.get_or_create_session(src(Platform.DISCORD, "chan", "u1"))
    assert ea.resume_pending is True
    assert ea.resume_reason == "restart_interrupted"
    assert ea.active_turn_token is None
    ei = store2.get_or_create_session(src(Platform.TELEGRAM, "idle", "u2"))
    assert ei.resume_pending is False
    assert store2.clear_resume_pending(ei.session_key) is False
    assert store2.clear_resume_pending(ea.session_key) is True
    assert store2.get_or_create_session(src(Platform.DISCORD, "chan", "u1")).resume_pending is False

    async def _lease_scenario():
        registry = SessionTurnLeaseRegistry()
        holder = await registry.acquire("sess-x", owner_key="key-a", generation=1, timeout=1)
        assert holder is not None
        # per-session scope: an unrelated session acquires immediately while sess-x is held;
        # a global-lease mutant (all ids -> one shared lock) would block here and time out
        other = await registry.acquire("sess-y", owner_key="key-c", generation=1, timeout=0.5)
        assert other is not None
        assert registry.release(other) is True
        with pytest.raises(TurnLeaseTimeoutError):
            await registry.acquire("sess-x", owner_key="key-b", generation=1, timeout=0.02)
        assert registry.release(holder) is True
        successor = await registry.acquire("sess-x", owner_key="key-b", generation=2, timeout=1)
        assert successor is not None
        assert registry.release(successor) is True
        assert registry.release(successor) is False

    asyncio.run(_lease_scenario())


def test_ac_iso_f11_6():
    """AC-ISO-F11-6: The webhook platform drops an already-seen delivery id within the idempotency
    TTL and admits it again once the TTL has elapsed."""
    from gateway.config import PlatformConfig
    from gateway.platforms.webhook import WebhookAdapter

    adapter = WebhookAdapter(PlatformConfig(enabled=True, extra={"routes": {}, "rate_limit": 30}))
    adapter._idempotency_ttl = 10

    assert adapter._record_delivery_id("d-1", now=100.0) is True
    assert adapter._record_delivery_id("d-1", now=100.5) is False
    assert adapter._record_delivery_id("d-2", now=100.6) is True
    assert adapter._record_delivery_id("d-1", now=200.0) is True


def test_ac_iso_f11_7(kanban_conn):
    """AC-ISO-F11-7: create_task with a repeated idempotency_key returns the existing task id rather
    than creating a duplicate; a distinct key creates a distinct task."""
    kb, conn = kanban_conn

    first = kb.create_task(conn, title="idem-a", assignee="w", idempotency_key="k1")
    again = kb.create_task(conn, title="idem-a-again", assignee="w", idempotency_key="k1")
    assert again == first
    rows = conn.execute("SELECT COUNT(*) FROM tasks WHERE idempotency_key=?", ("k1",)).fetchone()[0]
    assert rows == 1
    other = kb.create_task(conn, title="idem-b", assignee="w", idempotency_key="k2")
    assert other != first


def test_ac_iso_f11_8(kanban_conn):
    """AC-ISO-F11-8: check_respawn_guard defers a respawn behind a machine-readable reason when the
    last failure is an auth blocker, and permits it (None) for a clean task;
    _retry_status_for_run resolves a normal run to 'ready' and a review-lane run to
    'review'."""
    kb, conn = kanban_conn

    clean = kb.create_task(conn, title="guard-clean", assignee="w")
    assert kb.check_respawn_guard(conn, clean) is None

    blocked = kb.create_task(conn, title="guard-blocked", assignee="w")
    conn.execute("UPDATE tasks SET last_failure_error=? WHERE id=?", ("401 unauthorized", blocked))
    conn.commit()
    assert kb.check_respawn_guard(conn, blocked) == "blocker_auth"

    claimed = kb.create_task(conn, title="guard-retry", assignee="w")
    assert kb.claim_task(conn, claimed, claimer="w") is not None
    assert kb._retry_status_for_run(conn, claimed) == "ready"

    review = kb.create_task(conn, title="guard-review", assignee="w")
    assert kb.claim_task(conn, review, claimer="w") is not None
    run_id = kb.get_task(conn, review).current_run_id
    assert kb.request_review(conn, review, expected_run_id=run_id) is True
    assert kb.get_task(conn, review).status == "review"
    assert kb.claim_review_task(conn, review) is not None
    assert kb._retry_status_for_run(conn, review) == "review"


class _Proc:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_ac_iso_f11_9(tmp_path, monkeypatch):
    """AC-ISO-F11-9: Bot-to-bot delivery retries exactly once for a transient (retryable)
    failure and does not retry a non-retryable one; the retry decision is keyed on a
    machine-readable failure code, not on string-matching at the call site."""
    from tools import bot_failure_reasons as bfr
    from tools import bot_mode_dm

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

    assert bfr.retry_action(bfr.classify_agent_error("server error - overloaded")) != bfr.RETRY_NONE
    assert bfr.retry_action(bfr.classify_agent_error("No LLM provider configured")) == bfr.RETRY_NONE

    # _run_delivery imports classify_agent_error inside the function, so patch its source
    # module; opaque stderr tokens (matching no substring rule) force it to key on the code.
    def _fake_classify(text):
        if "XX_TRANSIENT_XX" in text:
            return bfr.PROVIDER_SERVER_ERROR   # AUTO_RETRYABLE -> retry_action != RETRY_NONE
        if "XX_FATAL_XX" in text:
            return bfr.MISSING_CONFIG          # not retryable -> retry_action == RETRY_NONE
        return bfr.UNKNOWN

    monkeypatch.setattr(bfr, "classify_agent_error", _fake_classify)

    dm = tmp_path / "dm-transient.txt"
    dm.write_text("hello", encoding="utf-8")
    calls = []

    def _fake_run(argv, **kw):
        calls.append(list(argv))
        if len(calls) == 1:
            return _Proc(1, stderr="XX_TRANSIENT_XX")
        return _Proc(0, stdout="the reply text")

    monkeypatch.setattr(bot_mode_dm.subprocess, "run", _fake_run)
    rc = bot_mode_dm._run_delivery(["hermes", "-p", "ops", "chat"], str(dm), stdin_file=False)
    assert rc == 0
    assert len(calls) == 2                         # retried once on the CODE (opaque stderr)
    assert calls[0] == calls[1]                    # the retry replays the same session, not a new one

    dm2 = tmp_path / "dm-fatal.txt"
    dm2.write_text("hello", encoding="utf-8")
    calls2 = []

    def _fake_run2(argv, **kw):
        calls2.append(list(argv))
        return _Proc(1, stderr="XX_FATAL_XX")

    monkeypatch.setattr(bot_mode_dm.subprocess, "run", _fake_run2)
    rc2 = bot_mode_dm._run_delivery(["hermes", "-p", "ops", "chat"], str(dm2), stdin_file=False)
    assert rc2 == 1
    assert len(calls2) == 1

    dm3 = tmp_path / "dm-transient-exhausted.txt"
    dm3.write_text("hello", encoding="utf-8")
    exhausted = []

    def _always_transient(argv, **kw):
        exhausted.append(list(argv))
        return _Proc(1, stderr="XX_TRANSIENT_XX")

    monkeypatch.setattr(bot_mode_dm.subprocess, "run", _always_transient)
    rc3 = bot_mode_dm._run_delivery(["hermes", "-p", "ops", "chat"], str(dm3), stdin_file=False)
    assert rc3 == 1
    assert len(exhausted) == 2
    assert exhausted[0] == exhausted[1]
