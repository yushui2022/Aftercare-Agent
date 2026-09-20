"""Small tracing and metrics boundary with deterministic test sinks.

The core package does not require an observability vendor. Production can
adapt :class:`Tracer` and :class:`Metrics` to OpenTelemetry; tests use
:class:`InMemoryTracer` and :class:`InMemoryMetrics` to prove names and
bounded attributes without exporting customer data.

Metrics carry names, numbers and a small static label set.  No customer
identifier, DSN, credential or payload body belongs in one: a counter that
needs a case id to be useful is a diagnostic record, not a metric.
"""

import importlib
import json
import logging
import os
from collections import deque
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Literal, Protocol

# Diagnostic attributes are an allowlist, not a best-effort convention.  The
# values are intentionally short as well: identifiers help correlate a trace,
# while prompt text, provider responses and credentials must never reach a
# span or metric sink.
_SPAN_ATTRIBUTE_KEYS = frozenset(
    {
        "tenant_id",
        "case_id",
        "run_id",
        "step_id",
        "action_id",
        "event_type",
        "correlation_id",
        "operation",
        "outcome",
    }
)
_METRIC_ATTRIBUTE_KEYS = frozenset({"component"})
_MAX_ATTRIBUTE_VALUE = 128


def _bounded_attributes(
    attributes: Mapping[str, str], *, allowed: frozenset[str]
) -> dict[str, str]:
    """Return a small, allowlisted copy suitable for diagnostics."""

    bounded: dict[str, str] = {}
    for key, value in attributes.items():
        if key not in allowed:
            continue
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if not value or "\x00" in value:
            continue
        bounded[key] = value[:_MAX_ATTRIBUTE_VALUE]
    return bounded


@dataclass
class SpanRecord:
    name: str
    attributes: dict[str, str] = field(default_factory=dict)
    status: str = "ok"
    error_type: str | None = None


class Tracer(Protocol):
    @contextmanager
    def span(self, name: str, attributes: Mapping[str, str]) -> Iterator[SpanRecord]: ...


class InMemoryTracer:
    """Thread-safe enough for deterministic tests; never stores payload bodies."""

    def __init__(self) -> None:
        self.spans: list[SpanRecord] = []

    @contextmanager
    def span(self, name: str, attributes: Mapping[str, str]) -> Iterator[SpanRecord]:
        record = SpanRecord(name, _bounded_attributes(attributes, allowed=_SPAN_ATTRIBUTE_KEYS))
        self.spans.append(record)
        try:
            yield record
        except Exception as exc:
            record.status = "error"
            record.error_type = type(exc).__name__
            raise


def _optional_api(module: str) -> Any:
    """Import an optional vendor module on first use.

    The core package must not depend on an observability vendor, so neither
    bridge imports one at module scope; both fail with ModuleNotFoundError
    when a deployment asks for a bridge it did not install.
    """
    return importlib.import_module(module)


class OpenTelemetryTracer:
    """Optional bridge; importing this class requires the OTel API package."""

    def __init__(self, instrumentation_name: str = "aftercare-agent") -> None:
        self._tracer = _optional_api("opentelemetry.trace").get_tracer(instrumentation_name)

    @contextmanager
    def span(self, name: str, attributes: Mapping[str, str]) -> Iterator[SpanRecord]:
        bounded = _bounded_attributes(attributes, allowed=_SPAN_ATTRIBUTE_KEYS)
        record = SpanRecord(name, bounded)
        with self._tracer.start_as_current_span(name, attributes=bounded) as span:
            try:
                yield record
            except Exception as exc:
                record.status = "error"
                record.error_type = type(exc).__name__
                span.record_exception(exc)
                raise


MetricKind = Literal["counter", "gauge"]


@dataclass
class MetricRecord:
    """One measurement: a name, a number, and a bounded label set."""

    name: str
    value: float
    kind: MetricKind = "gauge"
    attributes: dict[str, str] = field(default_factory=dict)


class Metrics(Protocol):
    def record(self, metric: MetricRecord) -> None: ...


class InMemoryMetrics:
    """Deterministic sink for tests; bounded so a long run cannot grow memory."""

    def __init__(self, limit: int = 4096) -> None:
        self.metrics: deque[MetricRecord] = deque(maxlen=limit)

    def record(self, metric: MetricRecord) -> None:
        self.metrics.append(
            MetricRecord(
                metric.name,
                metric.value,
                metric.kind,
                _bounded_attributes(metric.attributes, allowed=_METRIC_ATTRIBUTE_KEYS),
            )
        )

    def value(self, name: str) -> float | None:
        """The most recent value recorded under *name*, or ``None``."""
        for metric in reversed(self.metrics):
            if metric.name == name:
                return metric.value
        return None

    def names(self) -> set[str]:
        return {metric.name for metric in self.metrics}


class LoggingMetrics:
    """Vendor-free sink: one JSON line per sample on the standard logger.

    This is what a deployment gets before an exporter is configured (C-03):
    pool health reaches the process log with no extra dependency, and an
    exporter can replace this sink without touching the sampler.
    """

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("aftercare_agent.metrics")

    def record(self, metric: MetricRecord) -> None:
        bounded = _bounded_attributes(metric.attributes, allowed=_METRIC_ATTRIBUTE_KEYS)
        line: dict[str, Any] = {
            "kind": metric.kind,
            "metric": metric.name,
            "value": metric.value,
        }
        if bounded:
            line["attributes"] = bounded
        self._logger.info(json.dumps(line, sort_keys=True))


class OpenTelemetryMetrics:
    """Optional bridge; importing this class requires the OTel metrics API."""

    def __init__(self, instrumentation_name: str = "aftercare-agent") -> None:
        self._meter = _optional_api("opentelemetry.metrics").get_meter(instrumentation_name)
        self._instruments: dict[str, Any] = {}
        self._lock = Lock()

    def record(self, metric: MetricRecord) -> None:
        bounded = _bounded_attributes(metric.attributes, allowed=_METRIC_ATTRIBUTE_KEYS)
        with self._lock:
            instrument = self._instruments.get(metric.name)
            if instrument is None:
                instrument = self._create(metric)
                self._instruments[metric.name] = instrument
        if metric.kind == "counter":
            instrument.add(metric.value, bounded)
        else:
            instrument.set(metric.value, bounded)

    def _create(self, metric: MetricRecord) -> Any:
        # One instrument per name: a fresh counter per sample would reset the
        # running total an exporter is meant to accumulate.
        if metric.kind == "counter":
            return self._meter.create_counter(metric.name)
        return self._meter.create_gauge(metric.name)


def metrics_from_environment(
    *,
    environ: Mapping[str, str] | None = None,
    logger: logging.Logger | None = None,
) -> Metrics:
    """Select the process metric sink without making OTel a core dependency.

    ``logging`` is the default and is always available.  ``otel`` is an
    explicit deployment choice: the process must provide the optional
    OpenTelemetry API/SDK and its exporter setup.  Unknown values fail closed
    rather than silently falling back to a sink the operator did not choose.
    """
    values = os.environ if environ is None else environ
    backend = values.get("AFTERCARE_METRICS_BACKEND", "logging").strip().lower()
    if backend == "logging":
        return LoggingMetrics(logger)
    if backend == "otel":
        return OpenTelemetryMetrics()
    raise RuntimeError("AFTERCARE_METRICS_BACKEND must be logging or otel")
