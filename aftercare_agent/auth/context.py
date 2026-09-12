"""Authentication context; synthetic identity is explicit and test-only."""

from dataclasses import dataclass

from pydantic import TypeAdapter, ValidationError

from aftercare_agent.domain.common import ContractViolation, ErrorCode, Identifier


class SyntheticAuthError(ContractViolation):
    pass


@dataclass(frozen=True)
class AuthContext:
    subject_id: Identifier
    tenant_id: Identifier
    permissions: frozenset[str]
    synthetic: bool = False
    case_ids: frozenset[Identifier] | None = None

    def require(self, permission: str) -> None:
        if permission not in self.permissions:
            raise ContractViolation(ErrorCode.FORBIDDEN, "permission denied")

    def require_case(self, case_id: str, permission: str) -> None:
        if self.case_ids is not None and case_id not in self.case_ids:
            raise ContractViolation(ErrorCode.FORBIDDEN, "case access denied")
        self.require(permission)


def synthetic_context(*, enabled: bool, tenant_id: str, subject_id: str) -> AuthContext:
    if not enabled:
        raise SyntheticAuthError(ErrorCode.UNAUTHENTICATED, "synthetic identity is disabled")
    try:
        tenant: str = TypeAdapter(Identifier).validate_python(tenant_id)
        subject: str = TypeAdapter(Identifier).validate_python(subject_id)
    except ValidationError as exc:
        raise SyntheticAuthError(ErrorCode.INVALID_INPUT, "invalid synthetic identity") from exc
    return AuthContext(
        subject_id=subject,
        tenant_id=tenant,
        permissions=frozenset({"case:create", "case:read"}),
        synthetic=True,
    )
