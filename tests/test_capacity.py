"""The capacity sweep's arithmetic, without running a workload."""

import pytest

from aftercare_agent.capacity import (
    ProbeReport,
    RunResult,
    Sizing,
    Workload,
    _parse_sizes,
    _parse_workloads,
    main,
    percentile,
    recommend_size,
)


def _result(
    workload: str,
    max_size: int,
    *,
    failures: int = 0,
    errors: int = 0,
    latencies: tuple[float, ...] = (1.0,),
    in_use: int = 1,
) -> RunResult:
    return RunResult(
        workload=workload,
        max_size=max_size,
        # ``units`` counts completed work: a refused borrow is a failure.
        units=len(latencies),
        failures=failures,
        errors=errors,
        latency_ms=latencies,
        in_use_high_water=in_use,
        waiting_high_water=0,
        watcher_samples=1,
        wait_ms=0,
        connections_opened=1,
        connection_errors=0,
        wall_seconds=0.1,
    )


def test_percentile_is_the_smallest_value_at_or_above_the_rank() -> None:
    assert percentile([], 0.95) == 0.0
    assert percentile([5.0], 0.95) == 5.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.95) == 4.0
    assert percentile([4.0, 1.0, 3.0, 2.0], 0.95) == 4.0


def test_a_run_separates_refused_borrows_from_service_time() -> None:
    result = _result("burst", 1, failures=3, latencies=(2.0, 4.0))
    # A refused borrow ended at the acquire timeout, so it is a failure and
    # not a latency sample.
    assert result.units == 2
    assert result.failures == 3
    assert result.met_budget is False
    assert result.p95_ms == 4.0
    assert result.max_ms == 4.0
    assert _result("steady", 2, latencies=(1.0,)).met_budget is True


def test_the_recommendation_is_the_smallest_size_that_met_the_budget() -> None:
    results = [
        _result("steady", 1, latencies=(30.0,)),
        _result("burst", 1, failures=1, latencies=(1.0,)),
        _result("steady", 2, latencies=(10.0,)),
        _result("burst", 2, latencies=(12.0,)),
        _result("steady", 4, latencies=(5.0,)),
    ]
    sizing = recommend_size(results, p95_budget_ms=25.0)
    assert sizing.max_size == 2
    assert "p95 <= 25 ms" in sizing.reason


def test_a_size_that_used_its_timeout_never_counts_as_sized() -> None:
    """A fast p95 with refused borrowers is a saturated pool, not a good size."""
    results = [
        _result("steady", 1, latencies=(1.0,), failures=4),
        _result("burst", 1, latencies=(1.0,)),
    ]
    sizing = recommend_size(results, p95_budget_ms=25.0)
    assert sizing.max_size is None
    assert "no swept size met" in sizing.reason
    assert "steady" in sizing.reason


def test_a_sweep_without_results_says_so() -> None:
    assert recommend_size([], p95_budget_ms=25.0) == Sizing(None, "no results")


def test_a_workload_validates_its_own_shape() -> None:
    assert Workload("wake", concurrency=2, rounds=3, cadence_seconds=0.01).units == 6
    with pytest.raises(ValueError, match="unknown workload"):
        Workload("torrent", concurrency=1, rounds=1)
    with pytest.raises(ValueError, match="positive"):
        Workload("steady", concurrency=0, rounds=1)
    with pytest.raises(ValueError, match="positive cadence"):
        Workload("wake", concurrency=1, rounds=1)
    with pytest.raises(ValueError, match="only the wake"):
        Workload("burst", concurrency=1, rounds=1, cadence_seconds=0.01)


def test_the_table_states_the_chosen_size_and_its_reason() -> None:
    workload = Workload("steady", concurrency=2, rounds=1)
    report = ProbeReport(
        generated_at="2026-09-15T00:00:00+00:00",
        python_version="3.13.15",
        platform_name="Test",
        postgres_version="16.13",
        acquire_timeout_seconds=1.0,
        service_time_seconds=0.0,
        min_size=1,
        p95_budget_ms=25.0,
        workloads=(workload,),
        results=(_result("steady", 2, latencies=(1.0,)),),
        sizing=Sizing(2, "smallest size with no failures and p95 <= 25 ms"),
    )
    table = report.table()
    assert "sizing: max_size=2" in table
    assert "workload" in table and "steady" in table
    document = report.to_json()
    assert document["sizing"] == {
        "max_size": 2,
        "reason": "smallest size with no failures and p95 <= 25 ms",
    }
    # A published number has to say what it is not.
    assert "not an SLA" in str(document["note"])
    assert document["postgres_version"] == "16.13"


def test_the_command_line_rejects_what_it_cannot_measure() -> None:
    with pytest.raises(SystemExit):
        _parse_sizes("8,zero")
    with pytest.raises(SystemExit):
        _parse_sizes("0")
    with pytest.raises(SystemExit):
        _parse_workloads("steady,torrent", concurrency=1, rounds=1, cadence=0.05)
    with pytest.raises(SystemExit):
        _parse_workloads(" ", concurrency=1, rounds=1, cadence=0.05)


def test_without_a_dsn_the_command_explains_instead_of_guessing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main([]) == 2


def test_a_prewarm_wider_than_the_pool_is_refused_before_measuring() -> None:
    with pytest.raises(SystemExit, match="smallest swept size"):
        main(["--dsn", "postgresql://unused", "--max-sizes", "2,4", "--min-size", "4"])
