"""Shared v1 value constraints, not an authentication or transaction boundary."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field

type Identifier = Annotated[
    str, Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
]
type NonNegativeInt = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
type PositiveInt = Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]
type SchemaVersion = Annotated[int, Field(strict=True, ge=1, le=1)]
type Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware timestamp required")
    return value.astimezone(UTC)


type UtcDatetime = Annotated[AwareDatetime, AfterValidator(utc)]


class ContractModel(BaseModel):
    """Validate boundary data; trusted code must not bypass validation on writes."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, revalidate_instances="always"
    )


class CaseScope(ContractModel):
    tenant_id: Identifier
    case_id: Identifier


class RunScope(CaseScope):
    run_id: Identifier


class ErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    UNAUTHENTICATED = "unauthenticated"
    FORBIDDEN = "forbidden"
    CONFLICT = "conflict"
    LEASE_LOST = "lease_lost"
    RATE_LIMITED = "rate_limited"
    RETRYABLE = "retryable"
    OUTCOME_UNKNOWN = "outcome_unknown"
    UNSUPPORTED_VERSION = "unsupported_version"
    EVIDENCE_REJECTED = "evidence_rejected"
    BUDGET_EXHAUSTED = "budget_exhausted"


class ContractViolation(ValueError):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def require_same_case(expected: CaseScope, actual: CaseScope) -> None:
    if (expected.tenant_id, expected.case_id) != (actual.tenant_id, actual.case_id):
        raise ContractViolation(ErrorCode.FORBIDDEN, "case scope mismatch")
