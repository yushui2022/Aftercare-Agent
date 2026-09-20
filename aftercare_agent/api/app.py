"""Minimal A1-02 FastAPI adapter; business authority remains in repositories."""

import asyncio
import json
import os
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any, Literal, cast
from uuid import uuid4

import httpx
import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from aftercare_agent.auth import (
    AuthContext,
    CachedIntrospector,
    HttpTokenIntrospector,
    IntrospectionConfig,
    JwtJwksVerifier,
    JwtVerifierConfig,
    TokenAccessGuard,
    TokenIntrospector,
    TokenVerifier,
    synthetic_context,
)
from aftercare_agent.auth.grants import (
    ADMINISTRATION_PERMISSIONS,
    GRANT_ADMIN_PERMISSION,
    GRANT_READ_PERMISSION,
    GRANTABLE_CASE_PERMISSIONS,
    CaseGrantRecord,
    authorize_case_grant,
)
from aftercare_agent.config import environment_secret
from aftercare_agent.domain.approvals import ApprovalRecord
from aftercare_agent.domain.common import ContractViolation, ErrorCode, Identifier
from aftercare_agent.domain.events import DomainEvent
from aftercare_agent.domain.protocol import Checkpoint, RouteReason
from aftercare_agent.domain.reviews import (
    MAX_OVERRIDE_DEADLINE_EXTENSION_SECONDS,
    ReviewOverrideRequest,
    ReviewRecord,
)
from aftercare_agent.domain.runtime import (
    AdmissionKey,
    CaseRecord,
    OpenCaseInput,
    RunRecord,
    SessionRecord,
)
from aftercare_agent.domain.strategy_migrations import (
    StrategyMigrationRecord,
    StrategyMigrationRequest,
)
from aftercare_agent.observability import Metrics, metrics_from_environment
from aftercare_agent.persistence import (
    MAX_PAGE_SIZE,
    AdmissionRepository,
    ApprovalRepository,
    CaseGrantRepository,
    CaseListEntry,
    CaseRepository,
    CheckpointRepository,
    Database,
    EventRepository,
    ReviewRepository,
    RunRepository,
    StrategyMigrationRepository,
    assert_schema_current,
    migrate,
    sampler_from_environment,
)
from aftercare_agent.runtime import PostgresEventTail


class CaseResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    session_id: str
    run_id: str
    replayed: bool


class ReviewOverrideInput(BaseModel):
    """Monotonic budget/deadline additions for a CONTINUE decision."""

    model_config = ConfigDict(extra="forbid", strict=True)

    checkpoint_version: int = Field(ge=1)
    model_calls_add: int = Field(default=0, ge=0)
    tool_calls_add: int = Field(default=0, ge=0)
    cost_microusd_add: int = Field(default=0, ge=0)
    deadline_extension_seconds: int = Field(
        default=0, ge=0, le=MAX_OVERRIDE_DEADLINE_EXTENSION_SECONDS
    )
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def has_effect(self) -> "ReviewOverrideInput":
        if not any(
            (
                self.model_calls_add,
                self.tool_calls_add,
                self.cost_microusd_add,
                self.deadline_extension_seconds,
            )
        ):
            raise ValueError("review override must add budget or extend the deadline")
        return self


class ReviewDecisionInput(BaseModel):
    """Operator decision; identity and scope always come from the request context."""

    model_config = ConfigDict(extra="forbid", strict=True)

    decision: Literal["CONTINUE", "CANCEL"]
    decision_reason: str | None = Field(default=None, max_length=2000)
    override: ReviewOverrideInput | None = None


class StrategyMigrationInput(BaseModel):
    """Explicit strategy identity change; the pending Review remains required."""

    model_config = ConfigDict(extra="forbid", strict=True)

    checkpoint_version: int = Field(ge=1)
    old_strategy_id: Identifier
    old_model_config_version: Identifier
    old_policy_version: Identifier
    old_tool_schema_version: Identifier
    new_strategy_id: Identifier
    new_model_config_version: Identifier
    new_policy_version: Identifier
    new_tool_schema_version: Identifier
    reason: str = Field(min_length=1, max_length=2000)


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


class StrategyMigrationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    migration_id: str
    case_id: str
    run_id: str
    checkpoint_version_before: int
    checkpoint_version_after: int
    old_strategy_id: str
    old_model_config_version: str
    old_policy_version: str
    old_tool_schema_version: str
    new_strategy_id: str
    new_model_config_version: str
    new_policy_version: str
    new_tool_schema_version: str
    reason: str
    migrated_by: str
    idempotency_key: str
    created_at: datetime


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
    route_reason: RouteReason | None


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


class CaseGrantInput(BaseModel):
    """One delegation; tenant, Case and actor always come from the context.

    Identifiers and revisions stay strict so a JSON string can never pass for a
    subject or a revision, but ``expires_at`` is the one field a JSON body must
    send as an RFC 3339 string.  The timezone requirement stays a handler check
    because a naive local timestamp is an input error rather than a parse error.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    subject_id: Identifier
    permissions: list[Identifier] = Field(min_length=1, max_length=16)
    expires_at: datetime | None = Field(default=None, strict=False)
    expected_revision: int | None = Field(default=None, ge=1)


class CaseGrantRevokeInput(BaseModel):
    """Revocation carries the revision the caller believes is current."""

    model_config = ConfigDict(extra="forbid", strict=True)

    expected_revision: int = Field(ge=1)


class CaseGrantResponse(BaseModel):
    """Public grant projection; the tenant stays a server-side scope."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    subject_id: str
    permissions: list[str]
    revision: int
    granted_by: str
    granted_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    revoked_by: str | None
    updated_at: datetime


class CaseGrantListResponse(BaseModel):
    """Case-scoped grant list for the access-administration view.

    ``delegable`` and ``can_administer`` describe the *caller*, not the Case.
    The client cannot derive them from the Case projection: that projection is
    the intersection with a grant, so it understates a tenant administrator
    (who may hold no grant here at all).  Both values are the caller's own
    scopes, so they disclose nothing it did not present.
    """

    model_config = ConfigDict(extra="forbid")

    grants: list[CaseGrantResponse]
    delegable: list[str]
    can_administer: bool


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


def _strategy_migration_projection(record: StrategyMigrationRecord) -> StrategyMigrationResponse:
    return StrategyMigrationResponse(
        migration_id=record.migration_id,
        case_id=record.case_id,
        run_id=record.run_id,
        checkpoint_version_before=record.checkpoint_version_before,
        checkpoint_version_after=record.checkpoint_version_after,
        old_strategy_id=record.old_strategy_id,
        old_model_config_version=record.old_model_config_version,
        old_policy_version=record.old_policy_version,
        old_tool_schema_version=record.old_tool_schema_version,
        new_strategy_id=record.new_strategy_id,
        new_model_config_version=record.new_model_config_version,
        new_policy_version=record.new_policy_version,
        new_tool_schema_version=record.new_tool_schema_version,
        reason=record.reason,
        migrated_by=record.migrated_by,
        idempotency_key=record.idempotency_key,
        created_at=record.created_at,
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


def _grant_projection(record: CaseGrantRecord) -> CaseGrantResponse:
    """Project one grant without widening what an operator may read.

    The tenant is omitted because it is already the caller's scope, and the
    audit columns (who granted or revoked, and when) are included because an
    access administration view is exactly where that must be visible.
    """
    return CaseGrantResponse(
        case_id=record.case_id,
        subject_id=record.subject_id,
        permissions=sorted(record.permissions),
        revision=record.revision,
        granted_by=record.granted_by,
        granted_at=record.granted_at,
        expires_at=record.expires_at,
        revoked_at=record.revoked_at,
        revoked_by=record.revoked_by,
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


def _administration_summary(entry: CaseListEntry, identity: AuthContext) -> CaseSummaryResponse:
    """Project one Case for the access-administration inventory.

    ``permissions`` reports only what the caller may do *with this row*: its
    tenant-level administration scopes.  It deliberately does not reuse the
    Case-scoped projection -- a caller holding no grant on this Case has no
    Case permission here, and reporting ``case:read`` would tell the
    workbench to open content the next request would refuse.  The identity's
    own scopes are already known to it, so this discloses nothing new.
    """
    return CaseSummaryResponse(
        case_id=entry.case_id,
        order_id=entry.order_id,
        status=cast(Literal["OPEN", "IN_REVIEW", "CLOSED"], entry.status),
        version=entry.version,
        created_at=entry.created_at,
        permissions=sorted(identity.permissions.intersection(ADMINISTRATION_PERMISSIONS)),
    )


def _run_summary(run: RunRecord, checkpoint: Checkpoint | None = None) -> RunSummaryResponse:
    """Project a Run without leaking owner, fence or tenant internals."""
    route_reason = None
    if (
        checkpoint is not None
        and checkpoint.run_id == run.run_id
        and run.state in ("REVIEW", "RETRY_AT")
    ):
        route_reason = checkpoint.route_reason
    return RunSummaryResponse(
        run_id=run.run_id,
        state=run.state,
        input_version=run.input_version,
        wait_id=run.wait_id,
        wait_generation=run.wait_generation,
        available_at=run.available_at,
        lease_until=run.lease_until,
        route_reason=route_reason,
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
    with database.transaction(
        tenant_id=identity.tenant_id, subject_id=identity.subject_id
    ) as connection:
        _authorized_case(connection, identity, case_id, "case:read")


def _administered_case(
    connection: psycopg.Connection[Any],
    identity: AuthContext,
    case_id: str,
    permission: str,
) -> str:
    """Authorize a tenant-level grant-administration operation.

    Unlike the Case-scoped routes this does not resolve a CaseGrant: the actor
    is a tenant administrator, so the check is the tenant-level scope plus the
    Case actually existing inside the actor's tenant.  What that administrator
    may delegate is bounded separately by ``authorize_case_grant``, and the
    bound is what keeps this route from becoming an escalation path.
    """
    scoped_case_id = _identifier(case_id, "case_id")
    identity.require(permission)
    if CaseRepository().get_case(connection, identity.tenant_id, scoped_case_id) is None:
        # Unknown Cases and other tenants' Cases stay indistinguishable.
        raise ContractViolation(ErrorCode.FORBIDDEN, "case access denied")
    return scoped_case_id


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
    auto_migrate: bool = True,
    oidc_verifier: TokenVerifier | None = None,
    introspector: TokenIntrospector | None = None,
    metrics: Metrics | None = None,
    on_shutdown: Callable[[], None] | None = None,
) -> FastAPI:
    # A configured introspector turns on revocation checking; without a
    # verifier there is no token to check, so both stay unset together.
    guard = (
        TokenAccessGuard(oidc_verifier, introspector=introspector)
        if oidc_verifier is not None
        else None
    )
    # No sink means no sampler thread: a caller that has nowhere to publish
    # pool health should not pay for collecting it.
    sampler = (
        None if metrics is None else sampler_from_environment(database, metrics, component="api")
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Open and pre-warm the pool before the first request borrows from it,
        # so an unreachable database fails the start and not a caller.
        try:
            database.startup()
            with database.transaction() as connection:
                if auto_migrate:
                    migrate(connection)
                assert_schema_current(connection)
            if sampler is not None:
                sampler.start()
            yield
        finally:
            # Diagnostics never hold up a shutdown: stopping waits for at most
            # one read of the pool counters, not for any work in flight.
            try:
                if sampler is not None:
                    sampler.stop()
            finally:
                if on_shutdown is not None:
                    on_shutdown()

    app = FastAPI(title="Aftercare Agent", version="v1", lifespan=lifespan)
    # Kept as a narrow diagnostic seam for embedding hosts that need to
    # explicitly release resources after a failed lifespan start.  Normal
    # servers should let the lifespan invoke it.
    if on_shutdown is not None:
        app.state.aftercare_on_shutdown = on_shutdown

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """Liveness only: no dependency check and safe for process probes."""
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> dict[str, str]:
        """Readiness: verify connectivity and the exact application schema."""
        try:
            with database.connection() as connection:
                connection.execute("SELECT 1")
                assert_schema_current(connection)
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
                if guard is None:
                    raise ContractViolation(
                        ErrorCode.UNAUTHENTICATED, "OIDC authentication is not configured"
                    )
                return guard.authorize(authorization)
            if tenant is None or subject is None:
                raise ContractViolation(ErrorCode.UNAUTHENTICATED, "authentication required")
            # Once a real verifier is configured, synthetic identities are
            # disabled even if a stale test flag remains in the environment.
            return synthetic_context(
                enabled=allow_synthetic and guard is None,
                tenant_id=tenant,
                subject_id=subject,
            )
        except ContractViolation as exc:
            raise _error(exc) from exc

    Auth = Annotated[AuthContext, Depends(auth)]

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
            with database.transaction(
                tenant_id=identity.tenant_id, subject_id=identity.subject_id
            ) as connection:
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
            with database.transaction(
                tenant_id=identity.tenant_id, subject_id=identity.subject_id
            ) as connection:
                scoped_case_id, _ = _authorized_case(connection, identity, case_id, "case:read")
                run = RunRepository().get(connection, identity.tenant_id, run_id)
                if run is None or run.case_id != scoped_case_id:
                    raise ContractViolation(ErrorCode.FORBIDDEN, "case access denied")
                checkpoint = CheckpointRepository().get_latest(
                    connection, identity.tenant_id, run_id
                )
            return _run_summary(run, checkpoint)
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
            with database.transaction(
                tenant_id=identity.tenant_id, subject_id=identity.subject_id
            ) as connection:
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
            with database.transaction(
                tenant_id=identity.tenant_id, subject_id=identity.subject_id
            ) as connection:
                entry, scoped_identity = _authorized_case_row(
                    connection, identity, case_id, "case:read"
                )
                runs = RunRepository().list_for_case(connection, identity.tenant_id, entry.case_id)
                checkpoints = CheckpointRepository().latest_for_case(
                    connection, identity.tenant_id, entry.case_id
                )
            return CaseDetailResponse(
                case_id=entry.case_id,
                order_id=entry.order_id,
                status=cast(Literal["OPEN", "IN_REVIEW", "CLOSED"], entry.status),
                version=entry.version,
                created_at=entry.created_at,
                permissions=sorted(scoped_identity.permissions),
                runs=[_run_summary(run, checkpoints.get(run.run_id)) for run in runs],
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
            with database.transaction(
                tenant_id=identity.tenant_id, subject_id=identity.subject_id
            ) as connection:
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
            with database.transaction(
                tenant_id=identity.tenant_id, subject_id=identity.subject_id
            ) as connection:
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
            with database.transaction(
                tenant_id=identity.tenant_id, subject_id=identity.subject_id
            ) as connection:
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
            with database.transaction(
                tenant_id=identity.tenant_id, subject_id=identity.subject_id
            ) as connection:
                scoped_case_id, scoped_identity = _authorized_case(
                    connection, identity, case_id, "review:decide"
                )
                repository = ReviewRepository()
                review = repository.get(connection, identity.tenant_id, scoped_review_id)
                if review is None or review.case_id != scoped_case_id:
                    raise ContractViolation(ErrorCode.FORBIDDEN, "review access denied")
                override = None
                if body.override is not None:
                    scoped_identity.require("review:override")
                    override = ReviewOverrideRequest.model_validate(body.override.model_dump())
                result = repository.decide(
                    connection,
                    identity.tenant_id,
                    scoped_review_id,
                    reviewer=identity.subject_id,
                    decision=body.decision,
                    decision_idempotency_key=decision_key,
                    decision_reason=body.decision_reason,
                    override=override,
                )
                return _review_projection(result)
        except ContractViolation as exc:
            raise _error(exc) from exc

    @app.post(
        "/v1/cases/{case_id}/runs/{run_id}/strategy-migration",
        response_model=StrategyMigrationResponse,
    )
    def migrate_strategy(
        case_id: str,
        run_id: str,
        body: StrategyMigrationInput,
        identity: Auth,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> StrategyMigrationResponse:
        """Migrate a stopped Run's strategy identity without approving its Review."""
        try:
            scoped_run_id = _identifier(run_id, "run_id")
            migration_key = _required_idempotency_key(idempotency_key)
            with database.transaction(
                tenant_id=identity.tenant_id, subject_id=identity.subject_id
            ) as connection:
                scoped_case_id, scoped_identity = _authorized_case(
                    connection, identity, case_id, "strategy:migrate"
                )
                scoped_identity.require("strategy:migrate")
                request = StrategyMigrationRequest.model_validate(
                    {
                        **body.model_dump(),
                        "tenant_id": identity.tenant_id,
                        "case_id": scoped_case_id,
                        "run_id": scoped_run_id,
                        "idempotency_key": migration_key,
                    }
                )
                result, _ = StrategyMigrationRepository().migrate(
                    connection, request, migrated_by=identity.subject_id
                )
            return _strategy_migration_projection(result)
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
            with database.transaction(
                tenant_id=identity.tenant_id, subject_id=identity.subject_id
            ) as connection:
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
            with database.transaction(
                tenant_id=identity.tenant_id, subject_id=identity.subject_id
            ) as connection:
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

    @app.get("/v1/cases/{case_id}/grants", response_model=CaseGrantListResponse)
    def list_case_grants(
        case_id: str,
        identity: Auth,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    ) -> CaseGrantListResponse:
        """List a Case's grants so access can be audited and handed over."""
        try:
            with database.transaction(
                tenant_id=identity.tenant_id, subject_id=identity.subject_id
            ) as connection:
                scoped_case_id = _administered_case(
                    connection, identity, case_id, GRANT_READ_PERMISSION
                )
                records = CaseGrantRepository().list_for_case(
                    connection, identity.tenant_id, scoped_case_id, limit=limit
                )
            return CaseGrantListResponse(
                grants=[_grant_projection(record) for record in records],
                # What this caller may hand out here, decided by the server
                # instead of inferred from a Case projection that may hold no
                # grant at all.
                delegable=sorted(identity.permissions.intersection(GRANTABLE_CASE_PERMISSIONS)),
                can_administer=GRANT_ADMIN_PERMISSION in identity.permissions,
            )
        except ContractViolation as exc:
            raise _error(exc) from exc

    @app.post("/v1/cases/{case_id}/grants", response_model=CaseGrantResponse)
    def put_case_grant(case_id: str, body: CaseGrantInput, identity: Auth) -> CaseGrantResponse:
        """Grant or replace one subject's access to a Case.

        Optimistic concurrency replaces an idempotency key: replacing an
        existing grant requires ``expected_revision`` from a prior read, so a
        blind retry after a timeout cannot silently apply twice.  A retried
        first grant finds the row and answers ``409`` for the same reason.
        """
        try:
            requested = frozenset(body.permissions)
            with database.transaction(
                tenant_id=identity.tenant_id, subject_id=identity.subject_id
            ) as connection:
                scoped_case_id = _administered_case(
                    connection, identity, case_id, GRANT_ADMIN_PERMISSION
                )
                if body.expires_at is not None:
                    if body.expires_at.tzinfo is None:
                        raise ContractViolation(
                            ErrorCode.INVALID_INPUT, "expires_at must be timezone-aware"
                        )
                    expired = connection.execute(
                        "SELECT clock_timestamp() >= %s", (body.expires_at,)
                    ).fetchone()
                    if expired is not None and expired[0]:
                        raise ContractViolation(
                            ErrorCode.INVALID_INPUT, "expires_at must be in the future"
                        )
                granted = authorize_case_grant(
                    actor_permissions=identity.permissions, requested=requested
                )
                record = CaseGrantRepository().grant(
                    connection,
                    tenant_id=identity.tenant_id,
                    subject_id=body.subject_id,
                    case_id=scoped_case_id,
                    permissions=sorted(granted),
                    granted_by=identity.subject_id,
                    expires_at=body.expires_at,
                    expected_revision=body.expected_revision,
                )
                return _grant_projection(record)
        except ContractViolation as exc:
            raise _error(exc) from exc

    @app.post("/v1/cases/{case_id}/grants/{subject_id}/revoke", response_model=CaseGrantResponse)
    def revoke_case_grant(
        case_id: str, subject_id: str, body: CaseGrantRevokeInput, identity: Auth
    ) -> CaseGrantResponse:
        """Revoke one subject's access; the row stays for audit and revision checks."""
        try:
            scoped_subject_id = _identifier(subject_id, "subject_id")
            with database.transaction(
                tenant_id=identity.tenant_id, subject_id=identity.subject_id
            ) as connection:
                scoped_case_id = _administered_case(
                    connection, identity, case_id, GRANT_ADMIN_PERMISSION
                )
                record = CaseGrantRepository().revoke(
                    connection,
                    tenant_id=identity.tenant_id,
                    subject_id=scoped_subject_id,
                    case_id=scoped_case_id,
                    revoked_by=identity.subject_id,
                    expected_revision=body.expected_revision,
                )
                return _grant_projection(record)
        except ContractViolation as exc:
            raise _error(exc) from exc

    @app.get("/v1/administration/cases", response_model=CaseListResponse)
    def list_administrable_cases(
        identity: Auth,
        status_filter: Annotated[
            Literal["OPEN", "IN_REVIEW", "CLOSED"] | None, Query(alias="status")
        ] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
        after_created_at: datetime | None = None,
        after_case_id: str | None = None,
    ) -> CaseListResponse:
        """List the tenant's Cases for an access administrator.

        Handing a Case over needs discovery: an administrator who is not a
        participant cannot invent a Case id, and its own operator queue only
        contains Cases it already holds a grant for.  This page is the
        control-plane inventory and stays narrow on purpose -- identifiers,
        status and version, never Case content, Runs or the event stream, all
        of which keep resolving through a per-Case grant.
        """
        try:
            identity.require(GRANT_READ_PERMISSION)
            with database.transaction(
                tenant_id=identity.tenant_id, subject_id=identity.subject_id
            ) as connection:
                entries = CaseRepository().list_for_tenant(
                    connection,
                    tenant_id=identity.tenant_id,
                    case_ids=identity.case_ids,
                    status=status_filter,
                    limit=limit,
                    after_created_at=after_created_at,
                    after_case_id=after_case_id,
                )
            last = entries[-1] if len(entries) == limit and entries else None
            return CaseListResponse(
                cases=[_administration_summary(entry, identity) for entry in entries],
                next_created_at=None if last is None else last.created_at,
                next_case_id=None if last is None else last.case_id,
            )
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
                with database.transaction(
                    tenant_id=identity.tenant_id, subject_id=identity.subject_id
                ) as connection:
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
                with database.transaction(
                    tenant_id=identity.tenant_id, subject_id=identity.subject_id
                ) as connection:
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


def _binary_environment_flag(name: str, default: str) -> bool:
    value = os.environ.get(name, default)
    if value not in {"0", "1"}:
        raise RuntimeError(f"{name} must be 0 or 1")
    return value == "1"


def create_default_app() -> FastAPI:
    dsn = environment_secret("DATABASE_URL") or ""
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
    introspector = None
    introspection_client = None
    endpoint = os.environ.get("AFTERCARE_OIDC_INTROSPECTION_URL")
    if endpoint is not None:
        if verifier is None:
            raise RuntimeError(
                "AFTERCARE_OIDC_INTROSPECTION_URL requires the OIDC issuer, "
                "audience and JWKS URL to be configured"
            )
        client_id = os.environ.get("AFTERCARE_OIDC_INTROSPECTION_CLIENT_ID")
        client_secret = environment_secret("AFTERCARE_OIDC_INTROSPECTION_CLIENT_SECRET")
        if not client_id or not client_secret:
            raise RuntimeError(
                "AFTERCARE_OIDC_INTROSPECTION_CLIENT_ID and "
                "AFTERCARE_OIDC_INTROSPECTION_CLIENT_SECRET are required with the "
                "introspection URL"
            )
        try:
            introspection = IntrospectionConfig(endpoint=endpoint)
        except ValidationError as exc:
            raise RuntimeError(
                "AFTERCARE_OIDC_INTROSPECTION_URL must be an https URL without query or fragment"
            ) from exc
        # Credentials live only in this client; no contract model stores them.
        introspection_client = httpx.Client(
            timeout=introspection.request_timeout_seconds,
            follow_redirects=False,
            auth=httpx.BasicAuth(client_id, client_secret),
        )
        introspector = CachedIntrospector(
            HttpTokenIntrospector(introspection, client=introspection_client),
            ttl_seconds=introspection.cache_seconds,
            max_entries=introspection.max_cache_entries,
        )
    # This factory owns the Database, so it owns the pool that Database opens.
    database = Database(dsn)

    def close_owned_resources() -> None:
        # An app built around a caller's Database (tests, embeddings) leaves
        # that Database to its owner; this one built its own.  The lifespan
        # invokes this after the sampler and keeps the operation idempotent.
        if introspection_client is not None:
            introspection_client.close()
        database.close()

    app = create_app(
        database,
        allow_synthetic=synthetic_enabled == "1",
        auto_migrate=_binary_environment_flag("AFTERCARE_AUTO_MIGRATE", "1"),
        oidc_verifier=verifier,
        introspector=introspector,
        # The API owns its process log, so pool health reaches operators with
        # no extra dependency; an exporter can replace this sink later (C-03).
        metrics=metrics_from_environment(),
        on_shutdown=close_owned_resources,
    )

    return app


app = create_default_app()
