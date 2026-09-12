"""Database-free tests for the long-lived Worker control loop."""

from datetime import timedelta
from threading import Event

import pytest

from aftercare_agent.persistence import Database
from aftercare_agent.runtime import WorkerLoopResult, run_daemon
from aftercare_agent.runtime import worker as worker_module


def test_run_daemon_backoffs_when_idle_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    stop = Event()
    calls = 0

    def idle(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        stop.set()
        return None

    monkeypatch.setattr(worker_module, "run_next", idle)
    result = run_daemon(
        Database("postgresql://unused"),
        tenant_id="tenant",
        owner="worker",
        stop_event=stop,
        max_iterations=4,
    )

    assert calls == 1
    assert result == WorkerLoopResult(1, 0, 0, 1, 0)


def test_run_daemon_counts_recoverable_errors_and_honours_iteration_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    errors: list[BaseException] = []
    calls = 0

    def failing(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("temporary")

    monkeypatch.setattr(worker_module, "run_next", failing)
    result = run_daemon(
        Database("postgresql://unused"),
        tenant_id="tenant",
        owner="worker",
        idle_sleep=timedelta(milliseconds=1),
        max_iterations=2,
        on_error=errors.append,
    )

    assert calls == 2
    assert result == WorkerLoopResult(2, 0, 0, 0, 2)
    assert [str(error) for error in errors] == ["temporary", "temporary"]
