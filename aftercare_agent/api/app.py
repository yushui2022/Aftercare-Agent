"""Minimal A1-02 FastAPI adapter; business authority remains in repositories."""

import asyncio
import json
import os
from collections.abc import AsyncIterator, Iterator
from datetime import datetime
from typing import Annotated, Any, Literal, cast
from uuid import uuid4

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from aftercare_agent.auth import AuthContext, JwtJwksVerifier, JwtVerifierConfig, synthetic_context
from aftercare_agent.domain.approvals import ApprovalRecord
from aftercare_agent.domain.common import ContractViolation, ErrorCode, Identifier
from aftercare_agent.domain.events import DomainEvent
from aftercare_agent.domain.reviews import ReviewRecord
from aftercare_agent.domain.runtime import (
    AdmissionKey,
    CaseRecord,
    OpenCaseInput,
    RunRecord,
    SessionRecord,
)
from aftercare_agent.persistence import (
    MAX_PAGE_SIZE,
    AdmissionRepository,
    ApprovalRepository,
    CaseGrantRepository,
    CaseListEntry,
    CaseRepository,
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


class CaseSummaryResponse(BaseModel):
    """One discoverable Case; ``permissions`` is the effective operator scope."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    order_id: str
    status: Literal["OPEN", "IN_REVIEW", "CLOSED"]
    version: int
    created_at: datetime
    permissions: list[str]


class RunSummaryResponse(BaseModel):
    """Run progress for the operator; lease owner/fence stay internal."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    state: Literal[
        "READY",
        "RUNNING",
        "WAITING_INPUT",
        "WAITING_APPROVAL",
        "RETRY_AT",
        "REVIEW",
        "COMPLETED",
        "CANCELLED",
    ]
    input_version: int
    wait_id: str | None
    wait_generation: int | None
    available_at: datetime | None
    lease_until: datetime | None


class CaseListResponse(BaseModel):
    """One page of the operator work queue, newest Case first."""

    model_config = ConfigDict(extra="forbid")

    cases: list[CaseSummaryResponse]
    next_created_at: datetime | None = None
    next_case_id: str | None = None


class CaseDetailResponse(BaseModel):
    """Case header plus its Runs; still no evidence or authorization internals."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    order_id: str
    status: Literal["OPEN", "IN_REVIEW", "CLOSED"]
    version: int
    created_at: datetime
    permissions: list[str]
    runs: list[RunSummaryResponse]


class ReviewListResponse(BaseModel):
    """Case-scoped Review list for the operator work queue."""

    model_config = ConfigDict(extra="forbid")

    reviews: list[ReviewOperatorResponse]


class ApprovalListResponse(BaseModel):
    """Case-scoped Approval list for the operator work queue."""

    model_config = ConfigDict(extra="forbid")

    approvals: list[ApprovalOperatorResponse]


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


def _effective_case_permissions(entry: CaseListEntry, identity: AuthContext) -> frozenset[str]:
    """Effective permissions for one Case, never wider than the identity holds.

    A grant-scoped row is intersected with the token scopes, matching
    ``AuthContext.bind_case_grant``.  A tenant-wide (synthetic/local) read has
    no per-Case grant, so only the identity's own scopes are reported.
    """
    if entry.permissions is None:
        return identity.permissions
    return entry.permissions.intersection(identity.permissions)


def _case_summary(entry: CaseListEntry, identity: AuthContext) -> CaseSummaryResponse:
    return CaseSummaryResponse(
        case_id=entry.case_id,
        order_id=entry.order_id,
        # The column is CHECK-constrained to these literals; the cast records
        # that database guarantee at the response boundary.
        status=cast(Literal["OPEN", "IN_REVIEW", "CLOSED"], entry.status),
        version=entry.version,
        created_at=entry.created_at,
        permissions=sorted(_effective_case_permissions(entry, identity)),
    )


def _run_summary(run: RunRecord) -> RunSummaryResponse:
    """Project a Run without leaking owner, fence or tenant internals."""
    return RunSummaryResponse(
        run_id=run.run_id,
        state=run.state,
        input_version=run.input_version,
        wait_id=run.wait_id,
        wait_generation=run.wait_generation,
        available_at=run.available_at,
        lease_until=run.lease_until,
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


def _authorized_case_row(
    connection: psycopg.Connection[Any],
    identity: AuthContext,
    case_id: str,
    permission: str,
) -> tuple[CaseListEntry, AuthContext]:
    """Authorize a Case and confirm it exists inside the caller's tenant.

    A synthetic principal has no grant row to miss, so without this existence
    check an unknown or other-tenant Case would look like an empty collection
    instead of an access decision.  Real identities already fail closed inside
    ``_authorized_case``; this keeps both paths indistinguishable.
    """
    scoped_case_id, scoped_identity = _authorized_case(connection, identity, case_id, permission)
    entry = CaseRepository().get_case(connection, identity.tenant_id, scoped_case_id)
    if entry is None:
        raise ContractViolation(ErrorCode.FORBIDDEN, "case access denied")
    return entry, scoped_identity


def _authorize_stream_event(database: Database, identity: AuthContext, case_id: str) -> None:
    """Re-check a live stream grant in a short transaction (thread target)."""
    with database.transaction() as connection:
        _authorized_case(connection, identity, case_id, "case:read")


def _format_sse_event(event: DomainEvent) -> str:
    """Render one event using the stable replay cursor contract."""
    return (
        f"id: {event.case_seq}\n"
        f"event: {event.event_type}\n"
        f"data: {json.dumps(event.model_dump(mode='json'), ensure_ascii=False)}\n\n"
    )


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

    @app.get("/v1/cases/{case_id}/runs/{run_id}", response_model=RunSummaryResponse)
    def get_run(case_id: str, run_id: str, identity: Auth) -> RunSummaryResponse:
        try:
            with database.transaction() as connection:
                scoped_case_id, _ = _authorized_case(connection, identity, case_id, "case:read")
                run = RunRepository().get(connection, identity.tenant_id, run_id)
                if run is None or run.case_id != scoped_case_id:
                    raise ContractViolation(ErrorCode.FORBIDDEN, "case access denied")
            return _run_summary(run)
        except ContractViolation as exc:
            raise _error(exc) from exc

    @app.get("/v1/cases", response_model=CaseListResponse)
    def list_cases(
        identity: Auth,
        status_filter: Annotated[
            Literal["OPEN", "IN_REVIEW", "CLOSED"] | None, Query(alias="status")
        ] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
        after_created_at: datetime | None = None,
        after_case_id: str | None = None,
    ) -> CaseListResponse:
        """List the Cases this operator may actually open, newest first.

        Discovery is the missing half of the control plane: without it an
        operator must already know a Case id.  For a real identity the page is
        always narrowed by an active database CaseGrant; the explicitly-enabled
        synthetic local principal may browse its own tenant only.
        """
        try:
            identity.require("case:read")
            with database.transaction() as connection:
                repository = CaseRepository()
                if identity.synthetic:
                    entries = repository.list_for_tenant(
                        connection,
                        tenant_id=identity.tenant_id,
                        status=status_filter,
                        limit=limit,
                        after_created_at=after_created_at,
                        after_case_id=after_case_id,
                    )
                else:
                    entries = repository.list_accessible(
                        connection,
                        tenant_id=identity.tenant_id,
                        subject_id=identity.subject_id,
                        case_ids=identity.case_ids,
                        status=status_filter,
                        limit=limit,
                        after_created_at=after_created_at,
                        after_case_id=after_case_id,
                    )
            # A full page may have more rows behind it; expose the keyset cursor
            # of the last row so the caller can page without offsets.
            last = entries[-1] if len(entries) == limit and entries else None
            return CaseListResponse(
                cases=[_case_summary(entry, identity) for entry in entries],
                next_created_at=None if last is None else last.created_at,
                next_case_id=None if last is None else last.case_id,
            )
        except ContractViolation as exc:
            raise _error(exc) from exc

    @app.get("/v1/cases/{case_id}", response_model=CaseDetailResponse)
    def get_case(case_id: str, identity: Auth) -> CaseDetailResponse:
        """Read one Case header and its Runs inside the caller's Case scope."""
        try:
            with database.transaction() as connection:
                entry, scoped_identity = _authorized_case_row(
                    connection, identity, case_id, "case:read"
                )
                runs = RunRepository().list_for_case(connection, identity.tenant_id, entry.case_id)
            return CaseDetailResponse(
                case_id=entry.case_id,
                order_id=entry.order_id,
                status=cast(Literal["OPEN", "IN_REVIEW", "CLOSED"], entry.status),
                version=entry.version,
                created_at=entry.created_at,
                permissions=sorted(scoped_identity.permissions),
                runs=[_run_summary(run) for run in runs],
            )
        except ContractViolation as exc:
            raise _error(exc) from exc

    @app.get(
        "/v1/cases/{case_id}/reviews",
        response_model=ReviewListResponse,
    )
    def list_reviews(
        case_id: str,
        identity: Auth,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    ) -> ReviewListResponse:
        """List a Case's Reviews so an operator can find pending decisions."""
        try:
            with database.transaction() as connection:
                entry, _ = _authorized_case_row(connection, identity, case_id, "review:read")
                records = ReviewRepository().list_for_case(
                    connection, identity.tenant_id, entry.case_id, limit=limit
                )
            return ReviewListResponse(reviews=[_review_projection(record) for record in records])
        except ContractViolation as exc:
            raise _error(exc) from exc

    @app.get(
        "/v1/cases/{case_id}/approvals",
        response_model=ApprovalListResponse,
    )
    def list_approvals(
        case_id: str,
        identity: Auth,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    ) -> ApprovalListResponse:
        """List a Case's Approvals so an operator can find pending decisions."""
        try:
            with database.transaction() as connection:
                entry, _ = _authorized_case_row(connection, identity, case_id, "approval:read")
                records = ApprovalRepository().list_for_case(
                    connection, identity.tenant_id, entry.case_id, limit=limit
                )
            return ApprovalListResponse(
                approvals=[_approval_projection(record) for record in records]
            )
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
                events_iter: Iterator[DomainEvent] | AsyncIterator[DomainEvent] = PostgresEventTail(
                    database
                ).stream_async(
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

            async def stream() -> AsyncIterator[str]:
                iterator = events_iter
                if follow:
                    async for event in cast(AsyncIterator[DomainEvent], iterator):
                        # A live tail can outlive the transaction that started
                        # the response. Re-resolve in a worker thread so a
                        # revoked grant stops subsequent data without blocking
                        # the event loop while the client is reading.
                        if not identity.synthetic:
                            try:
                                await asyncio.to_thread(
                                    _authorize_stream_event,
                                    database,
                                    identity,
                                    scoped_case_id,
                                )
                            except ContractViolation:
                                return
                        yield _format_sse_event(event)
                    return

                for event in cast(Iterator[DomainEvent], iterator):
                    # A live tail can outlive the transaction that started the
                    # response. Re-resolve before each event; revocation then
                    # stops subsequent data without holding a DB transaction
                    # while the client is reading.
                    if not identity.synthetic:
                        try:
                            await asyncio.to_thread(
                                _authorize_stream_event,
                                database,
                                identity,
                                scoped_case_id,
                            )
                        except ContractViolation:
                            return
                    yield _format_sse_event(event)

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
