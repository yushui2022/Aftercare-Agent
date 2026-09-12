"""Minimal A1-02 FastAPI adapter; business authority remains in repositories."""

import os
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
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
from aftercare_agent.persistence import AdmissionRepository, Database, RunRepository, migrate


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

    return app


def create_default_app() -> FastAPI:
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        return FastAPI(title="Aftercare Agent", version="v1")
    return create_app(
        Database(dsn), allow_synthetic=os.environ.get("AFTERCARE_ALLOW_SYNTHETIC_IDENTITY") == "1"
    )


app = create_default_app()
