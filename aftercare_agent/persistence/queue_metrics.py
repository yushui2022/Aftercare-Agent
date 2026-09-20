"""Runnable execution-queue age as a bounded diagnostic metric."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from threading import Event, Lock, Thread
from time import monotonic
from typing import Protocol

from aftercare_agent.observability import MetricRecord, Metrics

QUEUE_METRIC_PREFIX = "aftercare.queue"
DEFAULT_QUEUE_SAMPLER_INTERVAL_SECONDS = 10.0


@dataclass(frozen=True)
class QueueStats:
    """A database-clock snapshot of work that is runnable right now."""

    runnable: int
    oldest_age_seconds: float

    def __post_init__(self) -> None:
        if self.runnable < 0 or self.oldest_age_seconds < 0:
            raise ValueError("queue stats cannot be negative")


class QueueStatsSource(Protocol):
    def queue_stats(self) -> QueueStats: ...


def report_queue_stats(metrics: Metrics, stats: QueueStats, *, component: str) -> None:
    """Publish depth and oldest runnable age with one bounded label."""
    attributes = {"component": component}
    metrics.record(
        MetricRecord(
            f"{QUEUE_METRIC_PREFIX}.runnable_runs", float(stats.runnable), "gauge", attributes
        )
    )
    metrics.record(
        MetricRecord(
            f"{QUEUE_METRIC_PREFIX}.oldest_age_seconds",
            stats.oldest_age_seconds,
            "gauge",
            attributes,
        )
    )


class QueueStatsSampler:
    """Sample queue age periodically; telemetry failure never stops a Worker."""

    def __init__(
        self,
        source: QueueStatsSource,
        metrics: Metrics,
        *,
        component: str,
        interval: timedelta | None = None,
    ) -> None:
        seconds = (
            DEFAULT_QUEUE_SAMPLER_INTERVAL_SECONDS if interval is None else interval.total_seconds()
        )
        if not seconds > 0:
            raise ValueError("queue sampler interval must be positive")
        self.component = component
        self._source = source
        self._metrics = metrics
        self._interval = seconds
        self._stop = Event()
        self._lock = Lock()
        self._failure: Exception | None = None
        self._thread: Thread | None = None
        self._samples = 0

    @property
    def failure(self) -> Exception | None:
        with self._lock:
            return self._failure

    @property
    def samples(self) -> int:
        with self._lock:
            return self._samples

    @property
    def interval(self) -> timedelta:
        return timedelta(seconds=self._interval)

    def start(self) -> None:
        if self._thread is not None or self._stop.is_set():
            raise RuntimeError("queue stats sampler already started or stopped")
        self._thread = Thread(target=self._run, name="aftercare-queue-metrics", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join()

    def sample_once(self) -> bool:
        stats = self._source.queue_stats()
        report_queue_stats(self._metrics, stats, component=self.component)
        with self._lock:
            self._samples += 1
        return True

    def _run(self) -> None:
        deadline = monotonic()
        while True:
            if self._stop.wait(max(deadline - monotonic(), 0.0)):
                return
            try:
                self.sample_once()
            except Exception as exc:
                with self._lock:
                    self._failure = exc
                self._stop.set()
                return
            deadline += self._interval


def queue_sampler_from_environment(
    source: QueueStatsSource,
    metrics: Metrics,
    *,
    component: str,
    environ: Mapping[str, str] | None = None,
) -> QueueStatsSampler | None:
    """Build a queue sampler, enabled by default for Worker processes."""
    values = os.environ if environ is None else environ
    enabled = values.get("AFTERCARE_QUEUE_METRICS", "1")
    if enabled not in {"0", "1"}:
        raise RuntimeError("AFTERCARE_QUEUE_METRICS must be 0 or 1")
    if enabled == "0":
        return None
    raw = values.get("AFTERCARE_QUEUE_METRICS_INTERVAL_SECONDS", "")
    interval: timedelta | None = None
    if raw:
        try:
            seconds = float(raw)
        except ValueError as exc:
            raise RuntimeError("AFTERCARE_QUEUE_METRICS_INTERVAL_SECONDS must be a number") from exc
        if not seconds > 0:
            raise RuntimeError("AFTERCARE_QUEUE_METRICS_INTERVAL_SECONDS must be positive")
        interval = timedelta(seconds=seconds)
    return QueueStatsSampler(source, metrics, component=component, interval=interval)
