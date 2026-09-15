"""Pool metrics and the sampler, exercised without a database.

The point of these checks is the contract a metric has to keep: a bounded
label set, numbers instead of identifiers, and a sampler that loses a
diagnostic rather than failing the process it was asked to observe.
"""

import dataclasses
from collections.abc import Callable
from datetime import timedelta
from time import monotonic, sleep

import pytest

from aftercare_agent.observability import InMemoryMetrics
from aftercare_agent.persistence import (
    PoolStats,
    PoolStatsSampler,
    report_pool_stats,
    sampler_from_environment,
)


def _stats(
    *,
    min_size: int = 1,
    max_size: int = 8,
    size: int = 4,
    available: int = 1,
    waiting: int = 0,
    requests: int = 0,
    queued: int = 0,
    request_errors: int = 0,
    wait_ms: int = 0,
    connections: int = 4,
    connection_errors: int = 0,
    connections_lost: int = 0,
) -> PoolStats:
    return PoolStats(
        min_size=min_size,
        max_size=max_size,
        size=size,
        available=available,
        waiting=waiting,
        requests=requests,
        queued=queued,
        request_errors=request_errors,
        wait_ms=wait_ms,
        connections=connections,
        connection_errors=connection_errors,
        connections_lost=connections_lost,
    )


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 5.0) -> bool:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if predicate():
            return True
        sleep(0.005)
    return predicate()


class _Source:
    """A pool snapshot source with a scripted answer per call."""

    def __init__(self, *answers: PoolStats | Exception | None) -> None:
        self._answers = list(answers)
        self.calls = 0

    def stats(self) -> PoolStats | None:
        self.calls += 1
        answer = self._answers[min(self.calls - 1, len(self._answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_a_snapshot_reports_slots_but_never_work() -> None:
    stats = _stats()
    assert stats.in_use == 3
    # Numbers only: a snapshot has no field a case, tenant or statement could
    # hide in, so publishing one cannot leak work.
    for field in dataclasses.fields(PoolStats):
        assert isinstance(getattr(stats, field.name), int)


def test_report_pool_stats_publishes_bounded_names_and_labels() -> None:
    metrics = InMemoryMetrics()
    report_pool_stats(
        metrics,
        _stats(
            max_size=8,
            size=4,
            available=1,
            waiting=2,
            requests=10,
            queued=3,
            request_errors=1,
            wait_ms=40,
            connections=4,
            connections_lost=1,
        ),
        component="api",
    )
    assert metrics.value("aftercare.db.pool.connections.in_use") == 3.0
    assert metrics.value("aftercare.db.pool.connections.available") == 1.0
    assert metrics.value("aftercare.db.pool.connections.max") == 8.0
    assert metrics.value("aftercare.db.pool.requests.waiting") == 2.0
    assert metrics.value("aftercare.db.pool.requests.total") == 10.0
    assert metrics.value("aftercare.db.pool.connections.lost") == 1.0
    kinds = {metric.name: metric.kind for metric in metrics.metrics}
    assert kinds["aftercare.db.pool.connections.in_use"] == "gauge"
    assert kinds["aftercare.db.pool.requests.errors"] == "counter"
    # Every label is static, and two records never share one dict.
    for metric in metrics.metrics:
        assert metric.attributes == {"component": "api"}
    first = metrics.metrics[0]
    first.attributes["component"] = "mutated"
    assert metrics.metrics[1].attributes == {"component": "api"}


def test_sampling_a_database_without_a_pool_publishes_nothing() -> None:
    metrics = InMemoryMetrics()
    sampler = PoolStatsSampler(_Source(None), metrics, component="api")
    assert sampler.sample_once() is False
    assert sampler.samples == 0
    assert len(metrics.metrics) == 0


def test_the_sampler_publishes_immediately_and_stops_on_request() -> None:
    metrics = InMemoryMetrics()
    sampler = PoolStatsSampler(
        _Source(_stats()), metrics, component="api", interval=timedelta(seconds=0.02)
    )
    sampler.start()
    try:
        assert _wait_until(lambda: sampler.samples >= 2)
    finally:
        sampler.stop()
    settled = sampler.samples
    sleep(0.05)
    assert sampler.samples == settled
    assert sampler.failure is None
    assert metrics.value("aftercare.db.pool.connections.in_use") == 3.0


def test_a_sampler_that_cannot_read_the_pool_stops_instead_of_failing_work() -> None:
    metrics = InMemoryMetrics()
    sampler = PoolStatsSampler(
        _Source(ValueError("pool is gone")),
        metrics,
        component="worker",
        interval=timedelta(seconds=0.01),
    )
    sampler.start()
    try:
        assert _wait_until(lambda: sampler.failure is not None)
    finally:
        sampler.stop()
    assert isinstance(sampler.failure, ValueError)
    assert len(metrics.metrics) == 0


def test_a_sampler_starts_once_and_needs_a_positive_interval() -> None:
    sampler = PoolStatsSampler(_Source(_stats()), InMemoryMetrics(), component="api")
    sampler.stop()
    with pytest.raises(RuntimeError, match="already started or stopped"):
        sampler.start()
    with pytest.raises(ValueError, match="positive"):
        PoolStatsSampler(
            _Source(_stats()),
            InMemoryMetrics(),
            component="api",
            interval=timedelta(seconds=0),
        )


def test_the_environment_switch_decides_whether_a_process_samples() -> None:
    metrics = InMemoryMetrics()
    source = _Source(_stats())
    assert (
        sampler_from_environment(
            source, metrics, component="api", environ={"AFTERCARE_POOL_METRICS": "0"}
        )
        is None
    )
    sampler = sampler_from_environment(
        source,
        metrics,
        component="worker",
        environ={"AFTERCARE_POOL_METRICS_INTERVAL_SECONDS": "2.5"},
    )
    assert sampler is not None
    assert sampler.interval == timedelta(seconds=2.5)
    assert sampler.component == "worker"
    with pytest.raises(RuntimeError, match="must be 0 or 1"):
        sampler_from_environment(
            source, metrics, component="api", environ={"AFTERCARE_POOL_METRICS": "yes"}
        )
    with pytest.raises(RuntimeError, match="must be a number"):
        sampler_from_environment(
            source,
            metrics,
            component="api",
            environ={"AFTERCARE_POOL_METRICS_INTERVAL_SECONDS": "soon"},
        )
    with pytest.raises(RuntimeError, match="must be positive"):
        sampler_from_environment(
            source,
            metrics,
            component="api",
            environ={"AFTERCARE_POOL_METRICS_INTERVAL_SECONDS": "0"},
        )
