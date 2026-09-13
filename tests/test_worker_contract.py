"""Database-independent Worker lease safety checks."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import Event
from typing import Any, cast

import pytest

import aftercare_agent.runtime.worker as worker_module
from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.runtime import ExecutionClaim
from aftercare_agent.persistence import Database, SlotReservation
from aftercare_agent.runtime import LeaseHeartbeat


class _Database:
    @contextmanager
    def transaction(self) -> Iterator[Any]:
        yield object()


class _RunRepository:
    def renew(self, connection: Any, claim: ExecutionClaim, lease: timedelta) -> None:
        return None


class _AdmissionRepository:
    def __init__(self, called: Event) -> None:
        self._called = called

    def renew_slot(self, connection: Any, claim: ExecutionClaim, lease: timedelta) -> datetime:
        self._called.set()
        raise ContractViolation(ErrorCode.LEASE_LOST, "execution slot lease lost")


def test_heartbeat_stops_when_reserved_slot_renewal_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing/expired slot cannot be treated as a successful heartbeat."""
    called = Event()
    monkeypatch.setattr(worker_module, "RunRepository", _RunRepository)
    monkeypatch.setattr(
        worker_module,
        "AdmissionRepository",
        lambda: _AdmissionRepository(called),
    )
    claim = ExecutionClaim(
        tenant_id="tenant-1",
        case_id="case-1",
        run_id="run-1",
        owner="worker-1",
        fencing_token=1,
    )
    slot = SlotReservation(
        tenant_id=claim.tenant_id,
        case_id=claim.case_id,
        run_id=claim.run_id,
        owner=claim.owner,
        fencing_token=claim.fencing_token,
        lease_until=datetime.now(UTC) + timedelta(seconds=1),
    )
    heartbeat = LeaseHeartbeat(
        cast(Database, _Database()),
        claim,
        timedelta(seconds=1),
        slot=slot,
        interval=timedelta(milliseconds=1),
    )

    heartbeat._run()

    assert called.is_set()
    failure = heartbeat.failure
    assert isinstance(failure, ContractViolation)
    assert failure.code is ErrorCode.LEASE_LOST
