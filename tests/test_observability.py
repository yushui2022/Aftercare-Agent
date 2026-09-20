import importlib.util
import json
import logging

import pytest

from aftercare_agent.observability import (
    InMemoryMetrics,
    InMemoryTracer,
    LoggingMetrics,
    MetricRecord,
    OpenTelemetryMetrics,
    metrics_from_environment,
)


def test_in_memory_tracer_records_bounded_success_and_error() -> None:
    tracer = InMemoryTracer()
    with tracer.span("aftercare.run", {"tenant_id": "tenant-1"}):
        pass
    with pytest.raises(RuntimeError):
        with tracer.span("aftercare.tool", {"case_id": "case-1"}):
            raise RuntimeError("synthetic")
    assert tracer.spans[0].status == "ok"
    assert tracer.spans[1].status == "error"
    assert tracer.spans[1].error_type == "RuntimeError"
    assert "synthetic" not in str(tracer.spans[1].__dict__)


def test_diagnostic_attributes_are_allowlisted_and_length_bounded() -> None:
    tracer = InMemoryTracer()
    with tracer.span(
        "aftercare.run",
        {
            "tenant_id": "tenant-1",
            "prompt": "customer text must not be recorded",
            "run_id": "r" * 200,
            "credential": "secret",
            "nul": "bad\x00value",
        },
    ):
        pass
    assert tracer.spans[0].attributes == {"tenant_id": "tenant-1", "run_id": "r" * 128}


def test_metric_attributes_only_keep_the_component_label() -> None:
    metrics = InMemoryMetrics()
    metrics.record(
        MetricRecord(
            "aftercare.queue.age_seconds",
            3.0,
            attributes={"component": "worker", "case_id": "case-1", "prompt": "secret"},
        )
    )
    assert metrics.metrics[0].attributes == {"component": "worker"}


def test_in_memory_metrics_keeps_the_newest_values_within_its_limit() -> None:
    metrics = InMemoryMetrics(limit=2)
    metrics.record(MetricRecord("aftercare.db.pool.requests.total", 1.0, "counter"))
    metrics.record(MetricRecord("aftercare.db.pool.requests.total", 2.0, "counter"))
    metrics.record(MetricRecord("aftercare.db.pool.requests.waiting", 3.0))
    # A sink a long run cannot grow: the oldest record is the one that goes.
    assert len(metrics.metrics) == 2
    assert metrics.value("aftercare.db.pool.requests.total") == 2.0
    assert metrics.value("aftercare.db.pool.missing") is None
    assert metrics.names() == {
        "aftercare.db.pool.requests.total",
        "aftercare.db.pool.requests.waiting",
    }


def test_logging_metrics_writes_one_bounded_json_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("aftercare_agent.metrics.test")
    metrics = LoggingMetrics(logger)
    with caplog.at_level(logging.INFO, logger=logger.name):
        metrics.record(
            MetricRecord(
                "aftercare.db.pool.connections.in_use",
                3.0,
                "gauge",
                {"component": "api"},
            )
        )
    assert json.loads(caplog.records[0].message) == {
        "attributes": {"component": "api"},
        "kind": "gauge",
        "metric": "aftercare.db.pool.connections.in_use",
        "value": 3.0,
    }


def test_metrics_backend_defaults_to_logging_and_rejects_unknown_values() -> None:
    assert isinstance(metrics_from_environment(environ={}), LoggingMetrics)
    with pytest.raises(RuntimeError, match="must be logging or otel"):
        metrics_from_environment(environ={"AFTERCARE_METRICS_BACKEND": "vendor"})


def test_the_otel_bridge_is_optional() -> None:
    """A deployment without the vendor fails at the bridge, not at import time."""
    if importlib.util.find_spec("opentelemetry") is not None:
        pytest.skip("the OpenTelemetry API is installed in this environment")
    with pytest.raises(ModuleNotFoundError):
        OpenTelemetryMetrics()
