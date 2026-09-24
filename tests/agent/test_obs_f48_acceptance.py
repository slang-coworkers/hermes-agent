"""Acceptance contract for OBS-F48 — Fleet metrics and outcome analytics (adopt-track).

AC-OBS-F48-4..-6 characterize shipped Hermes behaviour (native gateway/process + active-
profile-cron health telemetry and per-profile usage/cost analytics); AC-OBS-F48-7 asserts
the doc deliverable. Behaviour
contracts only — no model lists / version literals / enumeration counts, no network,
nothing written under ~/.hermes.
"""

import re
from pathlib import Path

import pytest

from agent.monitoring.emitter import (
    MonitoringEmitter,
    emit,
    get_emitter,
    reset_emitter_for_tests,
)
from agent.monitoring.events import GatewayHealthEvent
from agent.monitoring.gateway_health import build_gateway_health_snapshot
from agent.monitoring.cron_health import build_cron_health_snapshot
from agent.monitoring.otlp_exporter import _span_attrs
from agent.monitoring import gateway_health_export as ghe
from agent.monitoring.gateway_health_export import _gateway_health_event
from agent.insights import InsightsEngine
from hermes_state import SessionDB
from cron import jobs as cron_jobs


class _FakeMeter:
    def __init__(self):
        self.gauges = []

    def create_observable_gauge(self, name, callbacks=None):
        self.gauges.append(name)


class _FakeMeterProvider:
    def __init__(self, metric_readers=None, resource=None):
        self.metric_readers = metric_readers or []
        self.meter = _FakeMeter()

    def get_meter(self, name):
        return self.meter


class _FakeMetricReader:
    def __init__(self, exporter, export_interval_millis=None):
        self.exporter = exporter


class _FakeMetricExporter:
    def __init__(self, endpoint=None, headers=None):
        self.endpoint = endpoint


class _FakeResource:
    @staticmethod
    def create(attrs):
        return {"attrs": attrs}


def _fake_metrics_sdk():
    # A minimal OTel-metrics stand-in so _start_metric_provider runs hermetically,
    # without the optional [otlp] extra installed.
    return {
        "OTLPMetricExporter": _FakeMetricExporter,
        "PeriodicExportingMetricReader": _FakeMetricReader,
        "MeterProvider": _FakeMeterProvider,
        "Resource": _FakeResource,
        "Observation": lambda value, attributes=None: (value, attributes),
    }


_EXPORT_CFG = {
    "monitoring": {
        "gateway_health_export": {"enabled": True, "metrics_enabled": True},
        "export": {"otlp": {"enabled": True,
                            "endpoint": "http://collector:4318/v1/traces"}},
    }
}


def test_ac_obs_f48_4(monkeypatch, tmp_path):
    """Summarizes AC-OBS-F48-4: the native health-metrics plane produces gateway/process + active-profile-cron gauges, registers them on the OTLP metric path, and its event egress is opt-in, fail-open and subscriber-isolated."""
    gw = build_gateway_health_snapshot(
        {"gateway_state": "running", "active_agents": 1,
         "platforms": {"telegram": {"state": "connected"}}},
        gateway_running=True, profile="p", install_id="i", version="v",
    )
    gw_names = {m.name for m in gw.metrics}
    assert "hermes.gateway.up" in gw_names
    assert "hermes.platform.up" in gw_names
    assert all("profile" not in m.attributes and "hermes.profile" not in m.attributes
               for m in gw.metrics)

    with cron_jobs.use_cron_store(tmp_path / "profile-a"):
        cron_jobs.save_jobs([{"id": "a", "enabled": True}], replace=True)
        cron_a = {m.name: m.value for m in build_cron_health_snapshot().metrics}
    with cron_jobs.use_cron_store(tmp_path / "profile-b"):
        cron_jobs.save_jobs([], replace=True)
        cron_b = {m.name: m.value for m in build_cron_health_snapshot().metrics}
    assert "hermes.cron.jobs.overdue" in cron_a
    assert cron_a["hermes.cron.jobs.enabled"] == 1
    assert cron_b["hermes.cron.jobs.enabled"] == 0

    # Metric export path: the health gauges are registered as OTel observable
    # gauges by _start_metric_provider (distinct from the event emitter), and
    # the exporter targets the /v1/metrics endpoint.
    provider = ghe._start_metric_provider(_EXPORT_CFG, _fake_metrics_sdk())
    registered = set(provider.meter.gauges)
    assert {"hermes.gateway.up", "hermes.platform.up",
            "hermes.cron.jobs.overdue"} <= registered
    assert provider.metric_readers[0].exporter.endpoint.endswith("/v1/metrics")

    # Fail-open runtime: with the OTLP SDK unavailable, export disables cleanly
    # instead of raising.
    def _no_sdk(*args, **kwargs):
        raise RuntimeError("otlp sdk absent")

    monkeypatch.setattr(ghe, "_require_metrics_sdk", _no_sdk)
    runtime = ghe.start_gateway_health_export(_EXPORT_CFG)
    assert runtime.enabled is False
    assert runtime.reason == "otlp_unavailable"

    # Exercise the process singleton rather than only a local emitter.
    try:
        reset_emitter_for_tests(MonitoringEmitter(enabled=False))
        emit(GatewayHealthEvent(name="gw", gateway_state="running"))
        assert get_emitter().stats()["queued"] == 0
    finally:
        reset_emitter_for_tests()

    # Fail-isolated: a raising subscriber registered first must not stop a
    # later subscriber from receiving the batch.
    seen = []

    def raising(_batch):
        raise RuntimeError("subscriber failure")

    em = MonitoringEmitter(enabled=True)
    try:
        em.subscribe(raising)
        em.subscribe(lambda batch: seen.extend(batch))
        em.emit(GatewayHealthEvent(name="gw", gateway_state="running"))
        em.flush()
    finally:
        em.close()
    assert len(seen) == 1
    assert seen[0]["event"] == "gateway_health"
    assert seen[0]["name"] == "gw"


def test_ac_obs_f48_5():
    """The OTLP export plane is closed to unknown event kinds."""
    assert _gateway_health_event({"event": "gateway_health"}) is True
    assert _gateway_health_event({"event": "cron_execution"}) is True
    assert _gateway_health_event({"event": "plugin_custom"}) is False

    known = _span_attrs(
        {"event": "gateway_health", "name": "gw", "gateway_state": "running"}
    )
    assert known["hermes.event"] == "gateway_health"
    # Values pass content-free redaction, so assert on the KEYS, not the values.
    assert "hermes.name" in known
    assert "hermes.gateway_state" in known

    # The P8 regression sentinel: a custom kind carries no payload attribute.
    unknown = _span_attrs(
        {"event": "plugin_custom", "cost_usd": 3.5, "profile": "p"}
    )
    assert unknown == {"hermes.event": "plugin_custom"}


def test_ac_obs_f48_6(tmp_path):
    """Native usage/cost analytics attribute tokens/cost per model across a profile's sessions."""
    db = SessionDB(db_path=tmp_path / "obs.db")
    try:
        db.create_session(session_id="sw", source="cli",
                          model="deepseek/deepseek-v4-pro")
        db.update_token_counts(
            "sw", input_tokens=40000, output_tokens=8000,
            model="deepseek/deepseek-v4-pro", billing_provider="deepseek",
            estimated_cost_usd=1.25, actual_cost_usd=1.0,
            cost_status="estimated", cost_source="provider", api_call_count=2,
        )
        db.update_session_model("sw", "anthropic/claude-opus-4.8")
        db.update_token_counts(
            "sw", input_tokens=50000, output_tokens=4000,
            model="anthropic/claude-opus-4.8", billing_provider="openrouter",
            estimated_cost_usd=2.5, actual_cost_usd=2.0,
            cost_status="estimated", cost_source="provider", api_call_count=3,
        )
        db.create_session(session_id="s2", source="cli", model="openai/gpt-5.2")
        db.update_token_counts(
            "s2", input_tokens=1000, output_tokens=200,
            model="openai/gpt-5.2", billing_provider="openai",
            estimated_cost_usd=0.5, actual_cost_usd=0.4,
            cost_status="estimated", cost_source="provider", api_call_count=1,
        )
        db.flush_token_counts()
        report = InsightsEngine(db).generate(days=30)
    finally:
        db.close()

    assert report["overview"]["total_sessions"] == 2
    models = {m["model"]: m for m in report["models"]}
    # The mid-session switch is split across both models, not dumped on the first.
    assert models["deepseek-v4-pro"]["input_tokens"] == 40000
    assert models["claude-opus-4.8"]["input_tokens"] == 50000
    # Session 2's model rolls up only if sessions are aggregated within the profile.
    assert "gpt-5.2" in models
    assert report["overview"]["estimated_cost"] == pytest.approx(
        sum(m["cost"] for m in report["models"])
    )


def test_ac_obs_f48_7():
    """The adopt doc page exists and maps NanoClaw fleet-metrics onto Hermes."""
    repo_root = Path(__file__).resolve().parents[2]
    page = repo_root / "website" / "docs" / "user-guide" / "fleet-metrics.md"
    assert page.exists(), f"missing adopt doc page: {page}"
    text = page.read_text(encoding="utf-8")
    low = text.lower()

    for marker in (
        "monitoring.gateway_health_export.enabled",
        "monitoring.export.otlp.enabled",
        "monitoring.export.otlp.endpoint",
        "hermes monitoring status",
        "/analytics",
        "dashboard.show_token_analytics",
        "InsightsEngine",
        "session_model_usage",
        "usage_totals",
        "nv-artifact",
        "GOV-F25",
        "hermes outcomes funnel --json",
    ):
        assert marker in text, f"doc page missing required topic: {marker}"

    assert "adopt" in low
    assert "no plugin" in low
    assert re.search(r"gateway(?:-instance)?/process-scoped", low)
    assert re.search(r"cron.{0,200}active/default profile", low, re.DOTALL)

    # Require an actionable interim path while the P8 gap remains open.
    assert "allow-list" in low or "allow list" in low or "closed event set" in low
    assert "p8" in low
    assert "prometheus textfile" in low
    assert (
        "not publishable" in low
        or "not published" in low
        or "cannot" in low
        or "no supported surface" in low
    )
    assert "a plugin can publish a custom gauge through the otlp exporter" not in low
    # Keep the tag/main evidence machine-checkable, and require at least one
    # pair that actually names a monitoring source file on BOTH sides.
    citation_pairs = re.findall(r"tag:\s*`([^`]+)`\s*\|\s*main:\s*`([^`]+)`", text)
    assert citation_pairs, "doc page has no complete `tag: … | main: …` citation pair"
    assert any(
        "agent/monitoring/" in tag and "agent/monitoring/" in main
        and re.search(r"\.py:\d+", tag) and re.search(r"\.py:\d+", main)
        for tag, main in citation_pairs
    ), "no tag/main pair names an agent/monitoring file with a line number on both sides"
