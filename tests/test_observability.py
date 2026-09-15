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


def test_the_otel_bridge_is_optional() -> None:
    """A deployment without the vendor fails at the bridge, not at import time."""
    if importlib.util.find_spec("opentelemetry") is not None:
        pytest.skip("the OpenTelemetry API is installed in this environment")
    with pytest.raises(ModuleNotFoundError):
        OpenTelemetryMetrics()
