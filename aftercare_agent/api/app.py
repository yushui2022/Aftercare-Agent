"""Minimal A1-02 FastAPI adapter; business authority remains in repositories."""

import json
import os
from collections.abc import Iterator
from datetime import datetime
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from aftercare_agent.auth import AuthContext, synthetic_context
from aftercare_agent.domain.approvals import ApprovalRecord
from aftercare_agent.domain.common import ContractViolation, ErrorCode, Identifier
from aftercare_agent.domain.reviews import ReviewRecord
from aftercare_agent.domain.runtime import (
    AdmissionKey,
    CaseRecord,
    OpenCaseInput,
    RunRecord,
    SessionRecord,
)
from aftercare_agent.persistence import (
    AdmissionRepository,
    ApprovalRepository,
    Database,
    EventRepository,
    ReviewRepository,
    RunRepository,
    migrate,
)
from aftercare_agent.runtime import PostgresEventTail


class CaseResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    session_id: str
    run_id: str
    replayed: bool


class ReviewDecisionInput(BaseModel):
    """Operator decision; identity and scope always come from the request context."""

    model_config = ConfigDict(extra="forbid", strict=True)

    decision: Literal["CONTINUE", "CANCEL"]
    decision_reason: str | None = Field(default=None, max_length=2000)


class ApprovalDecisionInput(BaseModel):
    """Approval decision; approver, tenant and case are never body fields."""

    model_config = ConfigDict(extra="forbid", strict=True)

    decision: Literal["APPROVED", "REJECTED"]
    decision_reason: str | None = Field(default=None, max_length=2000)


class ReviewOperatorResponse(BaseModel):
    """Public Review projection; internal evidence and authorization fields stay private."""

    model_config = ConfigDict(extra="forbid")

    review_id: str
    case_id: str
    run_id: str
    reason_code: str
    decision: Literal["CONTINUE", "CANCEL"] | None
    actor: str | None
    decision_reason: str | None
    created_at: datetime
    decided_at: datetime | None
    updated_at: datetime


class ApprovalOperatorResponse(BaseModel):
    """Public Approval projection; hashes, policy and wait bindings stay private."""

    model_config = ConfigDict(extra="forbid")

    approval_id: str
    case_id: str
    run_id: str | None
    action_id: str
    decision: Literal["PENDING", "APPROVED", "REJECTED", "EXPIRED", "CANCELLED"]
    actor: str | None
    decision_reason: str | None
    expires_at: datetime
    created_at: datetime
    decided_at: datetime | None
    updated_at: datetime


def _identifier(value: str, field: str) -> str:
    try:
        return TypeAdapter(Identifier).validate_python(value)
    except ValidationError as exc:
        raise ContractViolation(ErrorCode.INVALID_INPUT, f"invalid {field}") from exc


def _required_idempotency_key(value: str | None) -> str:
    if value is None:
        raise ContractViolation(ErrorCode.INVALID_INPUT, "Idempotency-Key is required")
    return _identifier(value, "Idempotency-Key")


def _review_projection(record: ReviewRecord) -> ReviewOperatorResponse:
    return ReviewOperatorResponse(
        review_id=record.review_id,
        case_id=record.case_id,
        run_id=record.run_id,
        reason_code=record.reason_code,
        decision=record.decision,
        actor=record.reviewer,
        decision_reason=record.decision_reason,
        created_at=record.created_at,
        decided_at=record.decided_at,
        updated_at=record.updated_at,
    )


def _approval_projection(record: ApprovalRecord) -> ApprovalOperatorResponse:
    return ApprovalOperatorResponse(
        approval_id=record.approval_id,
        case_id=record.case_id,
        run_id=record.run_id,
        action_id=record.action_id,
        decision=record.decision,
        actor=record.approver,
        decision_reason=record.decision_reason,
        expires_at=record.expires_at,
        created_at=record.created_at,
        decided_at=record.decided_at,
        updated_at=record.updated_at,
    )


def _error(exc: ContractViolation) -> HTTPException:
    mapping = {
        ErrorCode.UNAUTHENTICATED: status.HTTP_401_UNAUTHORIZED,
        ErrorCode.FORBIDDEN: status.HTTP_403_FORBIDDEN,
        ErrorCode.CONFLICT: status.HTTP_409_CONFLICT,
        ErrorCode.RETRYABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
    }
    return HTTPException(
        status_code=mapping.get(exc.code, status.HTTP_400_BAD_REQUEST), detail=exc.code.value
    )


def create_app(database: Database, *, allow_synthetic: bool = False) -> FastAPI:
    app = FastAPI(title="Aftercare Agent", version="v1")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """Liveness only: no dependency check and safe for process probes."""
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> dict[str, str]:
        """Readiness: verify the configured database connection is usable."""
        try:
            with database.connection() as connection:
                connection.execute("SELECT 1")
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="not_ready"
            ) from exc
        return {"status": "ready"}

    def auth(request: Request) -> AuthContext:
        tenant = request.headers.get("X-Synthetic-Tenant")
        subject = request.headers.get("X-Synthetic-Subject")
        try:
            if tenant is None or subject is None:
                raise ContractViolation(ErrorCode.UNAUTHENTICATED, "authentication required")
            return synthetic_context(enabled=allow_synthetic, tenant_id=tenant, subject_id=subject)
        except ContractViolation as exc:
            raise _error(exc) from exc

    Auth = Annotated[AuthContext, Depends(auth)]

    @app.on_event("startup")
    def startup() -> None:
        with database.transaction() as connection:
            migrate(connection)

    @app.post("/v1/cases", response_model=CaseResponse, status_code=status.HTTP_201_CREATED)
    def open_case(
        body: OpenCaseInput,
        identity: Auth,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> CaseResponse:
        try:
            identity.require("case:create")
            if idempotency_key is None:
                raise ContractViolation(ErrorCode.INVALID_INPUT, "Idempotency-Key is required")
            case_id, session_id, run_id = (uuid4().hex for _ in range(3))
            case = CaseRecord(
                tenant_id=identity.tenant_id, case_id=case_id, order_id=body.order_id, version=1
            )
            session = SessionRecord(
                tenant_id=identity.tenant_id,
                case_id=case_id,
                session_id=session_id,
                channel=body.channel,
            )
            run = RunRecord(
                tenant_id=identity.tenant_id,
                case_id=case_id,
                run_id=run_id,
                session_id=session_id,
                definition_version="v1",
                input_version=1,
            )
            with database.transaction() as connection:
                result = AdmissionRepository().open(
                    connection,
                    AdmissionKey(tenant_id=identity.tenant_id, idempotency_key=idempotency_key),
                    body,
                    case=case,
                    session=session,
                    run=run,
                )
            return CaseResponse(
                case_id=result.case.case_id,
                session_id=result.session.session_id,
                run_id=result.run.run_id,
                replayed=result.replayed,
            )
        except ContractViolation as exc:
            raise _error(exc) from exc

    @app.get("/v1/cases/{case_id}/runs/{run_id}", response_model=RunRecord)
    def get_run(case_id: str, run_id: str, identity: Auth) -> RunRecord:
        try:
            identity.require_case(case_id, "case:read")
            with database.transaction() as connection:
                run = RunRepository().get(connection, identity.tenant_id, run_id)
            if run is None or run.case_id != case_id:
                raise ContractViolation(ErrorCode.FORBIDDEN, "case access denied")
            return run
        except ContractViolation as exc:
            raise _error(exc) from exc

    @app.get(
        "/v1/cases/{case_id}/reviews/{review_id}",
        response_model=ReviewOperatorResponse,
    )
    def get_review(case_id: str, review_id: str, identity: Auth) -> ReviewOperatorResponse:
        """Read one review only inside the caller's Case authorization scope."""
        try:
            scoped_case_id = _identifier(case_id, "case_id")
            scoped_review_id = _identifier(review_id, "review_id")
            identity.require_case(scoped_case_id, "review:read")
            with database.transaction() as connection:
                review = ReviewRepository().get(connection, identity.tenant_id, scoped_review_id)
            if review is None or review.case_id != scoped_case_id:
                # Keep tenant/case/review existence indistinguishable to callers.
                raise ContractViolation(ErrorCode.FORBIDDEN, "review access denied")
            return _review_projection(review)
        except ContractViolation as exc:
            raise _error(exc) from exc

    @app.post(
        "/v1/cases/{case_id}/reviews/{review_id}/decision",
        response_model=ReviewOperatorResponse,
    )
    def decide_review(
        case_id: str,
        review_id: str,
        body: ReviewDecisionInput,
        identity: Auth,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> ReviewOperatorResponse:
        """Apply a human Review decision using the authenticated subject as reviewer."""
        try:
            scoped_case_id = _identifier(case_id, "case_id")
            scoped_review_id = _identifier(review_id, "review_id")
            identity.require_case(scoped_case_id, "review:decide")
            decision_key = _required_idempotency_key(idempotency_key)
            with database.transaction() as connection:
                repository = ReviewRepository()
                review = repository.get(connection, identity.tenant_id, scoped_review_id)
                if review is None or review.case_id != scoped_case_id:
                    raise ContractViolation(ErrorCode.FORBIDDEN, "review access denied")
                result = repository.decide(
                    connection,
                    identity.tenant_id,
                    scoped_review_id,
                    reviewer=identity.subject_id,
                    decision=body.decision,
                    decision_idempotency_key=decision_key,
                    decision_reason=body.decision_reason,
                )
                return _review_projection(result)
        except ContractViolation as exc:
            raise _error(exc) from exc

    @app.get(
        "/v1/cases/{case_id}/approvals/{approval_id}",
        response_model=ApprovalOperatorResponse,
    )
    def get_approval(case_id: str, approval_id: str, identity: Auth) -> ApprovalOperatorResponse:
        """Read one approval only inside the caller's Case authorization scope."""
        try:
            scoped_case_id = _identifier(case_id, "case_id")
            scoped_approval_id = _identifier(approval_id, "approval_id")
            identity.require_case(scoped_case_id, "approval:read")
            with database.transaction() as connection:
                approval = ApprovalRepository().get(
                    connection, identity.tenant_id, scoped_approval_id
                )
            if approval is None or approval.case_id != scoped_case_id:
                raise ContractViolation(ErrorCode.FORBIDDEN, "approval access denied")
            return _approval_projection(approval)
        except ContractViolation as exc:
            raise _error(exc) from exc

    @app.post(
        "/v1/cases/{case_id}/approvals/{approval_id}/decision",
        response_model=ApprovalOperatorResponse,
    )
    def decide_approval(
        case_id: str,
        approval_id: str,
        body: ApprovalDecisionInput,
        identity: Auth,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> ApprovalOperatorResponse:
        """Apply an approval decision using the authenticated subject as approver."""
        try:
            scoped_case_id = _identifier(case_id, "case_id")
            scoped_approval_id = _identifier(approval_id, "approval_id")
            identity.require_case(scoped_case_id, "approval:decide")
            decision_key = _required_idempotency_key(idempotency_key)
            with database.transaction() as connection:
                repository = ApprovalRepository()
                approval = repository.get(connection, identity.tenant_id, scoped_approval_id)
                if approval is None or approval.case_id != scoped_case_id:
                    raise ContractViolation(ErrorCode.FORBIDDEN, "approval access denied")
                result = repository.decide(
                    connection,
                    identity.tenant_id,
                    scoped_approval_id,
                    approver=identity.subject_id,
                    decision=body.decision,
                    decision_idempotency_key=decision_key,
                    decision_reason=body.decision_reason,
                )
                return _approval_projection(result)
        except ContractViolation as exc:
            raise _error(exc) from exc

    @app.get("/v1/cases/{case_id}/events")
    def case_events(
        case_id: str,
        identity: Auth,
        after: int = 0,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
        limit: int = 100,
        follow: bool = False,
        wait_seconds: float = 15.0,
    ) -> StreamingResponse:
        """Return replay, or a bounded PostgreSQL polling tail after replay."""
        try:
            identity.require_case(case_id, "case:read")
            cursor = after
            if last_event_id is not None:
                try:
                    header_cursor = int(last_event_id)
                except ValueError as exc:
                    raise ContractViolation(
                        ErrorCode.INVALID_INPUT, "Last-Event-ID must be an integer"
                    ) from exc
                cursor = max(cursor, header_cursor)
            if follow:
                PostgresEventTail.validate(
                    after_case_seq=cursor,
                    limit=limit,
                    wait_seconds=wait_seconds,
                    poll_seconds=0.5,
                )
                events_iter = PostgresEventTail(database).stream(
                    tenant_id=identity.tenant_id,
                    case_id=case_id,
                    after_case_seq=cursor,
                    limit=limit,
                    wait_seconds=wait_seconds,
                )
            else:
                with database.transaction() as connection:
                    events_iter = iter(
                        EventRepository().list_case_events(
                            connection,
                            tenant_id=identity.tenant_id,
                            case_id=case_id,
                            after_case_seq=cursor,
                            limit=limit,
                        )
                    )

            def stream() -> Iterator[str]:
                for event in events_iter:
                    yield (
                        f"id: {event.case_seq}\n"
                        f"event: {event.event_type}\n"
                        f"data: {json.dumps(event.model_dump(mode='json'), ensure_ascii=False)}\n\n"
                    )

            return StreamingResponse(stream(), media_type="text/event-stream")
        except ContractViolation as exc:
            raise _error(exc) from exc

    return app


def create_default_app() -> FastAPI:
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        app = FastAPI(title="Aftercare Agent", version="v1")

        @app.get("/healthz")
        def healthz() -> dict[str, str]:
            return {"status": "ok"}

        return app
    return create_app(
        Database(dsn), allow_synthetic=os.environ.get("AFTERCARE_ALLOW_SYNTHETIC_IDENTITY") == "1"
    )


app = create_default_app()
