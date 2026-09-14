"""Durable session transcript reference tests (PostgreSQL only)."""

import os
from datetime import UTC, datetime

import pytest

from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.runtime import CaseRecord, SessionMessage, SessionRecord
from aftercare_agent.persistence import (
    Database,
    RunRepository,
    SessionMessageRepository,
    SessionRepository,
    migrate,
)


@pytest.fixture()
def db() -> Database:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        pytest.skip("DATABASE_URL is not configured")
    value = Database(dsn)
    with value.transaction() as connection:
        migrate(connection)
    return value


def _message(seq: int, *, message_id: str = "message-1") -> SessionMessage:
    return SessionMessage(
        tenant_id="session-message-test",
        case_id="case-1",
        session_id="session-1",
        message_id=message_id,
        message_seq=seq,
        role="user",
        message_ref=f"artifact-{message_id}",
        message_sha256="a" * 64,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _seed(db: Database) -> None:
    with db.transaction() as connection:
        connection.execute(
            "DELETE FROM aftercare_session_messages WHERE tenant_id=%s",
            ("session-message-test",),
        )
        connection.execute(
            "DELETE FROM aftercare_sessions WHERE tenant_id=%s",
            ("session-message-test",),
        )
        connection.execute(
            "DELETE FROM aftercare_cases WHERE tenant_id=%s",
            ("session-message-test",),
        )
        RunRepository().create_case(
            connection,
            CaseRecord(
                tenant_id="session-message-test", case_id="case-1", order_id="order-1", version=1
            ),
        )
        SessionRepository().create(
            connection,
            SessionRecord(
                tenant_id="session-message-test",
                case_id="case-1",
                session_id="session-1",
                channel="buyer",
            ),
        )


def test_session_messages_are_contiguous_idempotent_and_case_scoped(db: Database) -> None:
    _seed(db)
    repo = SessionMessageRepository()
    first = _message(1)
    with db.transaction() as connection:
        assert repo.append(connection, first) == first
        assert repo.append(connection, first) == first
        with pytest.raises(ContractViolation) as conflict:
            repo.append(connection, _message(1, message_id="message-2"))
        assert conflict.value.code is ErrorCode.CONFLICT
        assert repo.append(connection, _message(2, message_id="message-2")).message_seq == 2
        assert [
            item.message_id
            for item in repo.list_for_session(
                connection, "session-message-test", "case-1", "session-1"
            )
        ] == ["message-1", "message-2"]


def test_session_message_rejects_wrong_case_or_unbounded_query(db: Database) -> None:
    _seed(db)
    repo = SessionMessageRepository()
    with db.transaction() as connection:
        wrong_case = _message(1).model_copy(update={"case_id": "case-other"})
        with pytest.raises(ContractViolation) as forbidden:
            repo.append(connection, wrong_case)
        assert forbidden.value.code is ErrorCode.FORBIDDEN
        with pytest.raises(ValueError):
            repo.list_for_session(
                connection, "session-message-test", "case-1", "session-1", limit=0
            )


def test_session_message_list_cursor_is_scoped_and_bounded(db: Database) -> None:
    _seed(db)
    repo = SessionMessageRepository()
    with db.transaction() as connection:
        repo.append(connection, _message(1))
        repo.append(connection, _message(2, message_id="message-2"))
        assert [
            item.message_seq
            for item in repo.list_for_session(
                connection, "session-message-test", "case-1", "session-1", after_seq=1
            )
        ] == [2]
        assert repo.list_for_session(connection, "other-tenant", "case-1", "session-1") == ()
