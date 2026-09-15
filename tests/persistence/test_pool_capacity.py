"""The capacity probe against a real PostgreSQL; skipped without DATABASE_URL.

The sweep reports numbers, so the checks here are about the contract around
them: every run accounts for every unit, a pool never exceeds its ceiling, a
saturated pool is reported as failures instead of an exception, and the
recommendation only ever names a size whose runs met the budget.
"""

import pytest

from aftercare_agent.capacity import Workload, run_probe, run_workload
from aftercare_agent.persistence import Database


def _workloads() -> tuple[Workload, ...]:
    return (
        Workload("steady", concurrency=4, rounds=5),
        Workload("burst", concurrency=4, rounds=5),
        Workload("wake", concurrency=4, rounds=3, cadence_seconds=0.02),
    )


def test_a_sweep_accounts_for_every_unit_within_the_ceiling(dsn: str) -> None:
    workloads = _workloads()
    report = run_probe(
        dsn,
        workloads=workloads,
        max_sizes=(1, 4),
        p95_budget_ms=250.0,
        acquire_timeout_seconds=1.0,
    )
    assert report.postgres_version != "unknown"
    assert len(report.results) == len(workloads) * 2
    for result, workload in zip(report.results, workloads * 2, strict=True):
        assert result.workload == workload.name
        assert result.units + result.failures + result.errors == workload.units
        assert len(result.latency_ms) == result.units
        assert result.in_use_high_water <= result.max_size
        # The pool never opens more connections than the size it was given.
        assert result.connections_opened <= result.max_size
        assert result.connection_errors == 0
        assert result.failures == 0 and result.errors == 0
    assert report.sizing.max_size is not None
    assert "sizing: max_size=" in report.table()


def test_a_saturated_pool_is_a_reported_failure_not_an_exception(dsn: str) -> None:
    workload = Workload("burst", concurrency=8, rounds=2)
    report = run_probe(
        dsn,
        workloads=(workload,),
        max_sizes=(1,),
        p95_budget_ms=250.0,
        # A borrower that cannot be served in a microsecond is refused: the
        # probe has to record that as a failure rather than wait it out or
        # let the exception escape.
        acquire_timeout_seconds=0.000001,
    )
    result = report.results[0]
    assert result.failures >= 1
    assert result.units + result.failures + result.errors == workload.units
    assert report.sizing.max_size is None
    assert "no swept size met" in report.sizing.reason


def test_a_pool_narrower_than_its_workload_queues(dsn: str) -> None:
    """A sweep is only evidence if a narrow pool shows up as queue time."""
    workload = Workload("burst", concurrency=4, rounds=5)
    report = run_probe(
        dsn,
        workloads=(workload,),
        max_sizes=(1, 4),
        p95_budget_ms=25.0,
        acquire_timeout_seconds=5.0,
        service_time_seconds=0.01,
    )
    narrow, sized = report.results
    assert narrow.failures == 0 and sized.failures == 0
    # Four units of work holding a 10 ms slot each cannot share one slot
    # without queueing; a pool the size of the workload never has to.
    assert narrow.wait_ms > 0
    assert narrow.p95_ms > sized.p95_ms


def test_a_run_needs_a_pool_to_describe(dsn: str) -> None:
    with pytest.raises(RuntimeError, match="pooled Database"):
        run_workload(Database.direct(dsn), Workload("steady", concurrency=1, rounds=1))
