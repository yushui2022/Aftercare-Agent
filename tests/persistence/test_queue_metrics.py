"""Bounded runnable-queue diagnostics and one PostgreSQL state check."""

from collections.abc import Callable
from datetime import timedelta
from time import monotonic, sleep
from uuid import uuid4

from aftercare_agent.observability import InMemoryMetrics
from aftercare_agent.persistence import Database, RunRepository
from aftercare_agent.persistence.queue_metrics import (
    QueueStats,
    QueueStatsSampler,
    report_queue_stats,
)


class _Source:
    def __init__(self, stats: QueueStats | None = None, error: Exception | None = None) -> None:
        self.stats = stats
        self.error = error

    def queue_stats(self) -> QueueStats:
        if self.error is not None:
            raise self.error
        assert self.stats is not None
        return self.stats


def _wait_until(predicate: Callable[[], bool], timeout: float = 1.0) -> bool:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if predicate():
            return True
        sleep(0.005)
    return predicate()


def test_report_queue_stats_uses_only_component_label() -> None:
    metrics = InMemoryMetrics()
    report_queue_stats(metrics, QueueStats(runnable=4, oldest_age_seconds=2.5), component="worker")
    assert metrics.value("aftercare.queue.runnable_runs") == 4.0
    assert metrics.value("aftercare.queue.oldest_age_seconds") == 2.5
    assert all(record.attributes == {"component": "worker"} for record in metrics.metrics)


def test_sampler_stops_when_diagnostic_query_fails() -> None:
    sampler = QueueStatsSampler(
        _Source(error=RuntimeError("synthetic")),
        InMemoryMetrics(),
        component="worker",
        interval=timedelta(seconds=0.01),
    )
    sampler.start()
    try:
        assert _wait_until(lambda: sampler.failure is not None)
    finally:
        sampler.stop()
    assert isinstance(sampler.failure, RuntimeError)


def test_queue_stats_follow_runnable_state_without_claiming(db: Database) -> None:
    tenant = f"queue-metrics-{uuid4().hex[:12]}"
    with db.transaction() as connection:
        for suffix in ("ready", "due", "future"):
            connection.execute(
                "INSERT INTO aftercare_cases(tenant_id,case_id,order_id,version) "
                "VALUES (%s,%s,%s,1)",
                (tenant, f"case-{suffix}", f"order-{suffix}"),
            )
        connection.execute(
            "INSERT INTO aftercare_runs(tenant_id,case_id,run_id,definition_version,"
            "input_version,state,available_at) VALUES "
            "(%s,'case-ready','run-ready','v1',1,'READY',NULL),"
            "(%s,'case-due','run-due','v1',1,'RETRY_AT',clock_timestamp()-interval '1 second'),"
            "(%s,'case-future','run-future','v1',1,'RETRY_AT',clock_timestamp()+interval '1 hour')",
            (tenant, tenant, tenant),
        )
        connection.execute(
            "UPDATE aftercare_execution_queue SET "
            "enqueued_at=clock_timestamp()-interval '5 seconds' "
            "WHERE tenant_id=%s AND run_id='run-ready'",
            (tenant,),
        )
    repository = RunRepository()
    with db.transaction() as connection:
        before = repository.queue_stats(connection, tenant)
        claim = repository.claim_next(connection, tenant, "metrics-worker", timedelta(seconds=30))
    assert before.runnable == 2
    assert before.oldest_age_seconds >= 4.0
    assert claim is not None
    with db.transaction() as connection:
        after = repository.queue_stats(connection, tenant)
    assert after.runnable == 1
    with db.transaction() as connection:
        connection.execute(
            "UPDATE aftercare_runs SET lease_until=clock_timestamp()-interval '1 second' "
            "WHERE tenant_id=%s AND run_id=%s",
            (tenant, claim.run_id),
        )
        reclaimed = repository.queue_stats(connection, tenant)
    assert reclaimed.runnable == 2
