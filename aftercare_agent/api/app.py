"""Minimal A1-02 FastAPI adapter; business authority remains in repositories."""

import json
import os
from collections.abc import Iterator
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from aftercare_agent.auth import AuthContext, synthetic_context
from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.domain.runtime import (
    AdmissionKey,
    CaseRecord,
    OpenCaseInput,
    RunRecord,
    SessionRecord,
)
from aftercare_agent.persistence import (
    AdmissionRepository,
    Database,
    EventRepository,
    RunRepository,
    migrate,
)


class CaseResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    session_id: str
    run_id: str
    replayed: bool


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
            identity.require("case:read")
            with database.transaction() as connection:
                run = RunRepository().get(connection, identity.tenant_id, run_id)
            if run is None or run.case_id != case_id:
                raise ContractViolation(ErrorCode.FORBIDDEN, "case access denied")
            return run
        except ContractViolation as exc:
            raise _error(exc) from exc

    @app.get("/v1/cases/{case_id}/events")
    def case_events(
        case_id: str,
        identity: Auth,
        after: int = 0,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
        limit: int = 100,
    ) -> StreamingResponse:
        """Return a bounded SSE replay page; live broker tails are a later layer."""
        try:
            identity.require("case:read")
            cursor = after
            if last_event_id is not None:
                try:
                    header_cursor = int(last_event_id)
                except ValueError as exc:
                    raise ContractViolation(
                        ErrorCode.INVALID_INPUT, "Last-Event-ID must be an integer"
                    ) from exc
                cursor = max(cursor, header_cursor)
            with database.transaction() as connection:
                events = EventRepository().list_case_events(
                    connection,
                    tenant_id=identity.tenant_id,
                    case_id=case_id,
                    after_case_seq=cursor,
                    limit=limit,
                )

            def stream() -> Iterator[str]:
                for event in events:
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
