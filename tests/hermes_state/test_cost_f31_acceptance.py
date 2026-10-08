"""Acceptance test for COST-F31 — Durable cost ledger + pricing parity (ADOPT).

Pins the invariant future cost work must not break — the same read COST-F29's
enforcement depends on: a session's authoritative, aux- and subagent-inclusive
total is the RECONCILED lineage sum over its ``parent_session_id`` descendants,
where each node's spend is ``max(sessions.estimated_cost_usd,
Σ session_model_usage[task='']) + Σ session_model_usage[task<>'']`` — the
per-call token bins are durable and re-priceable without re-scanning
transcripts, and the ledger keeps the (session, model, provider, mode) grain.

Adopt-track deviation: this file exercises native Hermes surfaces directly (no
plugin discovery) and is expected to PASS on the stock tree — it is a
regression pin, not a fail-before/pass-after plugin test.

Hermetic: ``SessionDB(db_path=...)`` (no HERMES_HOME, no ~/.hermes write), no
network (pricing resolves via the bundled offline snapshot; no base_url passed).
"""

import pytest

from hermes_state import SessionDB
from agent.usage_pricing import (
    CanonicalUsage,
    estimate_usage_cost,
    has_known_pricing,
)

# Candidate priced models — a FIXTURE, not a catalogue assertion. The test uses
# whichever resolves offline; if a future snapshot drops all of them the priced
# tests fail loudly at the precondition (a fixture-maintenance signal).
_KNOWN_MODEL_CANDIDATES = (
    "anthropic/claude-sonnet-4-20250514",
    "anthropic/claude-sonnet-4-5",
    "anthropic/claude-sonnet-4-6",
)
_UNPRICED_MODEL = "cost-f31-test/nonexistent-unpriced-model"


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "cost_f31.db")
    yield session_db
    session_db.close()


@pytest.fixture()
def known_model():
    for m in _KNOWN_MODEL_CANDIDATES:
        if has_known_pricing(m):
            return m
    pytest.fail(
        "no candidate model has offline pricing — update the fixture to a "
        "model present in the bundled pricing snapshot"
    )


def _price(model, *, input_tokens, output_tokens, provider=None):
    """Priced cost of a call, straight from the versioned engine (offline)."""
    result = estimate_usage_cost(
        model,
        CanonicalUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        provider=provider,
    )
    assert result.amount_usd is not None, "known model must price"
    return float(result.amount_usd), result.status


def _usage_rows(db, session_ids):
    """Every persisted ledger row for the given sessions, full route grain."""
    ph = ",".join("?" for _ in session_ids)
    cur = db._conn.execute(
        "SELECT session_id, model, billing_provider, billing_base_url, "
        "billing_mode, task, input_tokens, output_tokens, "
        "estimated_cost_usd, cost_status, cost_source "
        f"FROM session_model_usage WHERE session_id IN ({ph})",
        tuple(session_ids),
    )
    return [dict(r) for r in cur.fetchall()]


def _summary_cost(db, sid):
    row = db._conn.execute(
        "SELECT estimated_cost_usd FROM sessions WHERE id = ?", (sid,)
    ).fetchone()
    return float((row[0] if row else 0.0) or 0.0)


def _ledger_by_task(db, sid, *, aux):
    op = "<> ''" if aux else "= ''"
    row = db._conn.execute(
        "SELECT COALESCE(SUM(estimated_cost_usd), 0) FROM session_model_usage "
        f"WHERE session_id = ? AND task {op}",
        (sid,),
    ).fetchone()
    return float(row[0] or 0.0)


def _node_spend(db, sid):
    """The reconciled per-node spend COST-F29 enforcement reads: the main-loop
    figure is max(summary fast-path, main-loop ledger) so an absolute-update
    residual that lives only on the summary row is not lost; aux ledger rows
    (never on the summary row) are always added."""
    main = max(_summary_cost(db, sid), _ledger_by_task(db, sid, aux=False))
    return main + _ledger_by_task(db, sid, aux=True)


def _lineage(db, root):
    """root plus all parent_session_id descendants (recursive)."""
    rows = db._conn.execute(
        """WITH RECURSIVE tree(id) AS (
               SELECT ?
               UNION ALL
               SELECT s.id FROM sessions s JOIN tree t ON s.parent_session_id = t.id
           )
           SELECT id FROM tree""",
        (root,),
    ).fetchall()
    return [r[0] for r in rows]


def test_ac_cost_f31_1(tmp_path, known_model):
    """Priced calls persist to session_model_usage at (session, model, provider, mode) grain and survive reopen."""
    db_path = tmp_path / "durable.db"
    # Costs here are persistence FIXTURES, not route-accurate prices — this test
    # proves the ledger keeps distinct rows per route, not that any route is
    # priced correctly (that is test_ac_cost_f31_3). Three calls on one
    # (session, model) that differ only by billing_provider or billing_mode must
    # land as three durable rows, proving both dimensions are in the PK.
    writes = [
        {"billing_provider": "anthropic", "billing_mode": "api_key",
         "input_tokens": 1000, "output_tokens": 500,
         "estimated_cost_usd": 0.05, "cost_status": "estimated"},
        {"billing_provider": "openrouter", "billing_mode": "api_key",
         "input_tokens": 800, "output_tokens": 400,
         "estimated_cost_usd": 0.03, "cost_status": "estimated"},
        {"billing_provider": "anthropic", "billing_mode": "subscription_included",
         "input_tokens": 600, "output_tokens": 300,
         "estimated_cost_usd": 0.0, "cost_status": "included"},
    ]
    first = SessionDB(db_path=db_path)
    first.create_session(session_id="s1", source="cli", model=known_model)
    for w in writes:
        first.update_token_counts("s1", model=known_model, api_call_count=1, **w)
    first.close()

    reopened = SessionDB(db_path=db_path)
    try:
        rows = _usage_rows(reopened, ["s1"])
    finally:
        reopened.close()

    assert len(rows) == 3
    by_route = {(r["billing_provider"], r["billing_mode"]): r for r in rows}
    assert set(by_route) == {
        ("anthropic", "api_key"),
        ("openrouter", "api_key"),
        ("anthropic", "subscription_included"),
    }
    for w in writes:
        row = by_route[(w["billing_provider"], w["billing_mode"])]
        assert row["input_tokens"] == w["input_tokens"]
        assert row["output_tokens"] == w["output_tokens"]
        assert row["estimated_cost_usd"] == pytest.approx(w["estimated_cost_usd"])
        assert row["cost_status"] == w["cost_status"]


def test_ac_cost_f31_2(db, known_model):
    """Aux + subagent total is the reconciled lineage sum; neither native summary reader returns it."""
    main1, main_status = _price(known_model, input_tokens=2000, output_tokens=800)
    residual, _ = _price(known_model, input_tokens=900, output_tokens=350)
    aux, _ = _price(known_model, input_tokens=400, output_tokens=100)
    child, child_status = _price(known_model, input_tokens=1500, output_tokens=600)
    grandchild, gc_status = _price(known_model, input_tokens=700, output_tokens=250)

    db.create_session(session_id="p", source="cli", model=known_model)
    # incremental main-loop call: writes BOTH the summary row and a task='' ledger row
    db.update_token_counts(
        "p", input_tokens=2000, output_tokens=800, model=known_model,
        estimated_cost_usd=main1, cost_status=main_status, api_call_count=1,
    )
    # an absolute cumulative update raises the summary above the ledger (a
    # main-loop residual visible ONLY on the summary row — absolute=True writes
    # no session_model_usage row, hermes_state.py:9447)
    db.update_token_counts(
        "p", input_tokens=2000, output_tokens=800, model=known_model,
        estimated_cost_usd=main1 + residual, cost_status=main_status,
        api_call_count=1, absolute=True,
    )
    # aux call: writes session_model_usage ONLY (task='vision'), not the summary row
    db.record_auxiliary_usage(
        "p", task="vision", model=known_model,
        input_tokens=400, output_tokens=100, estimated_cost_usd=aux,
    )
    db.create_session(
        session_id="c", source="delegate", model=known_model, parent_session_id="p",
    )
    db.update_token_counts(
        "c", input_tokens=1500, output_tokens=600, model=known_model,
        estimated_cost_usd=child, cost_status=child_status, api_call_count=1,
    )
    # a grandchild (parent_session_id='c') so the recursive lineage walk is
    # exercised beyond one level
    db.create_session(
        session_id="gc", source="delegate", model=known_model, parent_session_id="c",
    )
    db.update_token_counts(
        "gc", input_tokens=700, output_tokens=250, model=known_model,
        estimated_cost_usd=grandchild, cost_status=gc_status, api_call_count=1,
    )

    # the reconciled lineage total COST-F29 enforcement computes
    reconciled = sum(_node_spend(db, sid) for sid in _lineage(db, "p"))
    assert reconciled == pytest.approx(main1 + residual + aux + child + grandchild)

    rows = _usage_rows(db, ["p", "c", "gc"])
    assert any(r["task"] == "vision" for r in rows), "aux row must be in the ledger"
    assert any(r["session_id"] == "c" for r in rows), "subagent row must be in the ledger"
    assert any(r["session_id"] == "gc" for r in rows), "grandchild row must be in the ledger"

    # a naive raw SUM over the ledger UNDERCOUNTS the absolute-update main-loop
    # residual (it is only on the summary row), so it is not the authoritative read
    naive_ledger = sum(r["estimated_cost_usd"] for r in rows)
    assert naive_ledger == pytest.approx(main1 + aux + child + grandchild)
    assert naive_ledger < reconciled

    # the native summary reader (usage_totals — the fleet/all-profiles sidebar)
    # sums root sessions rows only: it carries the absolute residual but EXCLUDES
    # aux (never on a summary row) and the non-root child session
    native_summary = db.usage_totals(min_message_count=0)["cost_usd"]
    assert native_summary == pytest.approx(main1 + residual)
    assert native_summary < reconciled


def test_ac_cost_f31_3(db, known_model):
    """Persisted token bins are re-priceable at their (model, provider) grain — a reprice, not a re-scan."""
    stored_cost, status = _price(
        known_model, input_tokens=3000, output_tokens=1200, provider="anthropic"
    )
    db.create_session(session_id="r", source="cli", model=known_model)
    db.update_token_counts(
        "r", input_tokens=3000, output_tokens=1200, model=known_model,
        billing_provider="anthropic", estimated_cost_usd=stored_cost,
        cost_status=status, api_call_count=1,
    )

    (row,) = _usage_rows(db, ["r"])
    # reprice from the PERSISTED tokens + PERSISTED route, not from any transcript
    repriced = estimate_usage_cost(
        row["model"],
        CanonicalUsage(
            input_tokens=row["input_tokens"], output_tokens=row["output_tokens"]
        ),
        provider=row["billing_provider"] or None,
        base_url=row["billing_base_url"] or None,
    )
    assert repriced.amount_usd is not None
    assert float(repriced.amount_usd) == pytest.approx(row["estimated_cost_usd"])
    assert has_known_pricing(known_model) is True
    assert has_known_pricing(_UNPRICED_MODEL) is False


def test_ac_cost_f31_4(db, known_model):
    """cost_status (actual|estimated|included|unknown) is carried through per row on the update_token_counts path."""
    # estimated + unknown: driven through the real engine
    est_cost, est_status = _price(known_model, input_tokens=1000, output_tokens=400)
    assert est_status == "estimated"
    unknown = estimate_usage_cost(
        _UNPRICED_MODEL, CanonicalUsage(input_tokens=1000, output_tokens=400)
    )
    assert unknown.status == "unknown"
    assert unknown.amount_usd is None  # fail-open to $0 at the summary level

    db.create_session(session_id="est", source="cli", model=known_model)
    db.update_token_counts(
        "est", input_tokens=1000, output_tokens=400, model=known_model,
        estimated_cost_usd=est_cost, cost_status=est_status, api_call_count=1,
    )
    db.create_session(session_id="unk", source="cli", model=_UNPRICED_MODEL)
    db.update_token_counts(
        "unk", input_tokens=1000, output_tokens=400, model=_UNPRICED_MODEL,
        estimated_cost_usd=0.0, cost_status="unknown", api_call_count=1,
    )
    # actual + included: carried-through column values (reconciled / subscription)
    db.create_session(session_id="act", source="cli", model=known_model)
    db.update_token_counts(
        "act", input_tokens=500, output_tokens=200, model=known_model,
        estimated_cost_usd=est_cost, actual_cost_usd=est_cost,
        cost_status="actual", api_call_count=1,
    )
    db.create_session(session_id="inc", source="cli", model=known_model)
    db.update_token_counts(
        "inc", input_tokens=500, output_tokens=200, model=known_model,
        estimated_cost_usd=0.0, cost_status="included", api_call_count=1,
    )

    by_id = {r["session_id"]: r for r in _usage_rows(db, ["est", "unk", "act", "inc"])}
    assert by_id["est"]["cost_status"] == "estimated"
    assert by_id["act"]["cost_status"] == "actual"
    assert by_id["inc"]["cost_status"] == "included"
    # the unpriced row is distinguishable by STATUS even though its cost reads $0
    assert by_id["unk"]["cost_status"] == "unknown"
    assert by_id["unk"]["estimated_cost_usd"] == pytest.approx(0.0)

    # documented P8 gap: record_auxiliary_usage carries no cost_status/cost_source,
    # so aux rows persist that provenance as NULL (usage-grain provenance is partial)
    db.create_session(session_id="auxp", source="cli", model=known_model)
    db.record_auxiliary_usage(
        "auxp", task="vision", model=known_model,
        input_tokens=100, output_tokens=50, estimated_cost_usd=est_cost,
    )
    (aux_row,) = _usage_rows(db, ["auxp"])
    assert aux_row["cost_status"] is None
    assert aux_row["cost_source"] is None
