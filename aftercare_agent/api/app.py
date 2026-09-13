"""Minimal A1-02 FastAPI adapter; business authority remains in repositories."""

import json
import os
from collections.abc import Iterator
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import uuid4

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from aftercare_agent.auth import AuthContext, JwtJwksVerifier, JwtVerifierConfig, synthetic_context
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
    CaseGrantRepository,
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


def _authorized_case(
    connection: psycopg.Connection[Any],
    identity: AuthContext,
    case_id: str,
    permission: str,
) -> tuple[str, AuthContext]:
    """Resolve a DB CaseGrant and bind it before a case-scoped operation.

    The caller owns the transaction.  Keeping resolution here (rather than in
    a FastAPI dependency) makes the grant row lock cover the subsequent read or
    write and avoids a revoke/authorize time-of-check gap.
    """
    scoped_case_id = _identifier(case_id, "case_id")
    if identity.synthetic:
        identity.require_case(scoped_case_id, permission)
        return scoped_case_id, identity
    grant = CaseGrantRepository().resolve(
        connection,
        identity.tenant_id,
        identity.subject_id,
        scoped_case_id,
    )
    if grant is None:
        raise ContractViolation(ErrorCode.FORBIDDEN, "case access denied")
    scoped_identity = identity.bind_case_grant(grant)
    scoped_identity.require_case(scoped_case_id, permission)
    return scoped_case_id, scoped_identity


def _error(exc: ContractViolation) -> HTTPException:
    mapping = {
        ErrorCode.UNAUTHENTICATED: status.HTTP_401_UNAUTHORIZED,
        ErrorCode.FORBIDDEN: status.HTTP_403_FORBIDDEN,
        ErrorCode.CONFLICT: status.HTTP_409_CONFLICT,
        ErrorCode.RETRYABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
    }
    return HTTPException(
        status_code=mapping.get(exc.code, status.HTTP_400_BAD_REQUEST),
        detail=exc.code.value,
        headers={"WWW-Authenticate": "Bearer"} if exc.code is ErrorCode.UNAUTHENTICATED else None,
    )


def create_app(
    database: Database,
    *,
    allow_synthetic: bool = False,
    oidc_verifier: JwtJwksVerifier | None = None,
) -> FastAPI:
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
        authorization = request.headers.get("Authorization")
        tenant = request.headers.get("X-Synthetic-Tenant")
        subject = request.headers.get("X-Synthetic-Subject")
        try:
            # A supplied bearer token always takes precedence.  In particular,
            # an invalid token must never fall back to a synthetic header.
            if authorization is not None:
                if oidc_verifier is None:
                    raise ContractViolation(
                        ErrorCode.UNAUTHENTICATED, "OIDC authentication is not configured"
                    )
                return oidc_verifier.verify(authorization)
            if tenant is None or subject is None:
                raise ContractViolation(ErrorCode.UNAUTHENTICATED, "authentication required")
            # Once a real verifier is configured, synthetic identities are
            # disabled even if a stale test flag remains in the environment.
            return synthetic_context(
                enabled=allow_synthetic and oidc_verifier is None,
                tenant_id=tenant,
                subject_id=subject,
            )
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
                grants = CaseGrantRepository()
                if result.replayed:
                    # A tenant-scoped idempotency replay must not disclose a
                    # previously-created Case to a new subject.
                    if (
                        grants.resolve(
                            connection,
                            identity.tenant_id,
                            identity.subject_id,
                            result.case.case_id,
                        )
                        is None
                    ):
                        raise ContractViolation(ErrorCode.FORBIDDEN, "case access denied")
                else:
                    # The creator receives only a Case read grant.  Operator
                    # and action scopes are separate explicit grants.
                    grants.grant(
                        connection,
                        tenant_id=identity.tenant_id,
                        subject_id=identity.subject_id,
                        case_id=result.case.case_id,
                        permissions=("case:read",),
                        granted_by=identity.subject_id,
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
            with database.transaction() as connection:
                scoped_case_id, _ = _authorized_case(connection, identity, case_id, "case:read")
                run = RunRepository().get(connection, identity.tenant_id, run_id)
                if run is None or run.case_id != scoped_case_id:
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
            scoped_review_id = _identifier(review_id, "review_id")
            with database.transaction() as connection:
                scoped_case_id, _ = _authorized_case(connection, identity, case_id, "review:read")
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
            scoped_review_id = _identifier(review_id, "review_id")
            decision_key = _required_idempotency_key(idempotency_key)
            with database.transaction() as connection:
                scoped_case_id, _ = _authorized_case(connection, identity, case_id, "review:decide")
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
            scoped_approval_id = _identifier(approval_id, "approval_id")
            with database.transaction() as connection:
                scoped_case_id, _ = _authorized_case(connection, identity, case_id, "approval:read")
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
            scoped_approval_id = _identifier(approval_id, "approval_id")
            decision_key = _required_idempotency_key(idempotency_key)
            with database.transaction() as connection:
                scoped_case_id, _ = _authorized_case(
                    connection, identity, case_id, "approval:decide"
                )
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
            scoped_case_id = _identifier(case_id, "case_id")
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
                with database.transaction() as connection:
                    _authorized_case(connection, identity, scoped_case_id, "case:read")
                events_iter = PostgresEventTail(database).stream(
                    tenant_id=identity.tenant_id,
                    case_id=scoped_case_id,
                    after_case_seq=cursor,
                    limit=limit,
                    wait_seconds=wait_seconds,
                )
            else:
                with database.transaction() as connection:
                    _authorized_case(connection, identity, scoped_case_id, "case:read")
                    events_iter = iter(
                        EventRepository().list_case_events(
                            connection,
                            tenant_id=identity.tenant_id,
                            case_id=scoped_case_id,
                            after_case_seq=cursor,
                            limit=limit,
                        )
                    )

            def stream() -> Iterator[str]:
                for event in events_iter:
                    # A live tail can outlive the transaction that started the
                    # response.  Re-resolve before each event; revocation then
                    # stops subsequent data without holding a DB transaction
                    # while the client is reading.
                    if not identity.synthetic:
                        try:
                            with database.transaction() as connection:
                                _authorized_case(connection, identity, scoped_case_id, "case:read")
                        except ContractViolation:
                            return
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
    oidc_values = {
        "issuer": os.environ.get("AFTERCARE_OIDC_ISSUER"),
        "audience": os.environ.get("AFTERCARE_OIDC_AUDIENCE"),
        "jwks_url": os.environ.get("AFTERCARE_OIDC_JWKS_URL"),
    }
    configured = [value is not None for value in oidc_values.values()]
    if any(configured) and not all(configured):
        raise RuntimeError(
            "AFTERCARE_OIDC_ISSUER, AFTERCARE_OIDC_AUDIENCE and "
            "AFTERCARE_OIDC_JWKS_URL must be configured together"
        )
    synthetic_enabled = os.environ.get("AFTERCARE_ALLOW_SYNTHETIC_IDENTITY", "0")
    if synthetic_enabled not in {"0", "1"}:
        raise RuntimeError("AFTERCARE_ALLOW_SYNTHETIC_IDENTITY must be 0 or 1")
    verifier = None
    if all(configured):
        if synthetic_enabled == "1":
            raise RuntimeError("synthetic identity cannot be enabled with OIDC")
        require_case_ids = os.environ.get("AFTERCARE_OIDC_REQUIRE_CASE_IDS", "0")
        if require_case_ids not in {"0", "1"}:
            raise RuntimeError("AFTERCARE_OIDC_REQUIRE_CASE_IDS must be 0 or 1")
        issuer = oidc_values["issuer"]
        audience = oidc_values["audience"]
        jwks_url = oidc_values["jwks_url"]
        assert issuer is not None and audience is not None and jwks_url is not None
        verifier = JwtJwksVerifier(
            JwtVerifierConfig(
                issuer=issuer,
                audience=audience,
                jwks_url=jwks_url,
                require_case_ids=require_case_ids == "1",
            )
        )
    return create_app(
        Database(dsn),
        allow_synthetic=synthetic_enabled == "1",
        oidc_verifier=verifier,
    )


app = create_default_app()
