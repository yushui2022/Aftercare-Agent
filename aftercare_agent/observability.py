"""Small tracing boundary with a deterministic test sink.

The core package does not require an observability vendor. Production can
adapt :class:`Tracer` to OpenTelemetry; tests use :class:`InMemoryTracer` to
prove span names and bounded attributes without exporting customer data.
"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Protocol


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


class OpenTelemetryTracer:
    """Optional bridge; importing this class requires the OTel API package."""

    def __init__(self, instrumentation_name: str = "aftercare-agent") -> None:
        from opentelemetry import trace  # type: ignore[import-not-found]

        self._tracer = trace.get_tracer(instrumentation_name)

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
