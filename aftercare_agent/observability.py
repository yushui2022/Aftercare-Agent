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
from collections import deque
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Literal, Protocol


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
        record = SpanRecord(name, dict(attributes))
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
        record = SpanRecord(name, dict(attributes))
        with self._tracer.start_as_current_span(name, attributes=dict(attributes)) as span:
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
        self.metrics.append(metric)

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
        line: dict[str, Any] = {
            "kind": metric.kind,
            "metric": metric.name,
            "value": metric.value,
        }
        if metric.attributes:
            line["attributes"] = dict(metric.attributes)
        self._logger.info(json.dumps(line, sort_keys=True))


class OpenTelemetryMetrics:
    """Optional bridge; importing this class requires the OTel metrics API."""

    def __init__(self, instrumentation_name: str = "aftercare-agent") -> None:
        self._meter = _optional_api("opentelemetry.metrics").get_meter(instrumentation_name)
        self._instruments: dict[str, Any] = {}
        self._lock = Lock()

    def record(self, metric: MetricRecord) -> None:
        with self._lock:
            instrument = self._instruments.get(metric.name)
            if instrument is None:
                instrument = self._create(metric)
                self._instruments[metric.name] = instrument
        if metric.kind == "counter":
            instrument.add(metric.value, dict(metric.attributes))
        else:
            instrument.set(metric.value, dict(metric.attributes))

    def _create(self, metric: MetricRecord) -> Any:
        # One instrument per name: a fresh counter per sample would reset the
        # running total an exporter is meant to accumulate.
        if metric.kind == "counter":
            return self._meter.create_counter(metric.name)
        return self._meter.create_gauge(metric.name)
