"""Authentication context; synthetic identity is explicit and test-only."""

from dataclasses import dataclass, replace

from pydantic import TypeAdapter, ValidationError

from aftercare_agent.domain.common import ContractViolation, ErrorCode, Identifier

from .grants import CaseGrantRecord


class SyntheticAuthError(ContractViolation):
    pass


@dataclass(frozen=True)
class AuthContext:
    subject_id: Identifier
    tenant_id: Identifier
    permissions: frozenset[str]
    synthetic: bool = False
    case_ids: frozenset[Identifier] | None = None
    case_scope_resolved: bool = False
    case_grant_revision: int | None = None

    def require(self, permission: str) -> None:
        if permission not in self.permissions:
            raise ContractViolation(ErrorCode.FORBIDDEN, "permission denied")

    def require_case(self, case_id: str, permission: str) -> None:
        # A verified token is an identity and a requested upper bound, not the
        # database authorization record.  Production callers must bind one
        # active CaseGrant in the same short transaction as their operation.
        if not self.synthetic and not self.case_scope_resolved:
            raise ContractViolation(ErrorCode.FORBIDDEN, "case grant is unresolved")
        if self.case_ids is not None and case_id not in self.case_ids:
            raise ContractViolation(ErrorCode.FORBIDDEN, "case access denied")
        self.require(permission)

    def bind_case_grant(self, grant: CaseGrantRecord) -> "AuthContext":
        """Narrow this identity to one DB-authoritative Case grant.

        ``case_ids`` in a token is only an upper bound: it can reduce access,
        never add a Case absent from the database grant.  Permissions are also
        intersected with the token scopes, so a database row cannot elevate a
        bearer token into a role it did not present.
        """
        if self.synthetic:
            raise ContractViolation(ErrorCode.FORBIDDEN, "synthetic identity cannot bind grants")
        if grant.tenant_id != self.tenant_id or grant.subject_id != self.subject_id:
            raise ContractViolation(ErrorCode.FORBIDDEN, "case grant identity mismatch")
        if self.case_ids is not None and grant.case_id not in self.case_ids:
            raise ContractViolation(ErrorCode.FORBIDDEN, "case access denied")
        permissions = frozenset(self.permissions.intersection(grant.permissions))
        if not permissions:
            raise ContractViolation(ErrorCode.FORBIDDEN, "case grant permission denied")
        return replace(
            self,
            permissions=permissions,
            case_ids=frozenset({grant.case_id}),
            case_scope_resolved=True,
            case_grant_revision=grant.revision,
        )


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
        # The synthetic identity is an explicitly opt-in local/test principal.
        # Grant the operator scopes so the control-plane routes can be exercised
        # without introducing request-controlled permission headers.  Production
        # identities receive their permissions from the verified OIDC claims.
        permissions=frozenset(
            {
                "case:create",
                "case:read",
                "review:read",
                "review:decide",
                "review:override",
                "strategy:migrate",
                "approval:read",
                "approval:decide",
                # Grant administration is a tenant-level operator scope, so the
                # local identity can exercise the administration surface end to
                # end.  It stays test-only: a configured verifier disables
                # synthetic identities entirely.
                "grant:read",
                "grant:admin",
            }
        ),
        synthetic=True,
    )
