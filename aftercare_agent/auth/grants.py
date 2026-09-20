"""Database-backed Case grants used as the authoritative API scope.

The bearer token identifies a subject and may carry a case upper bound, but a
token claim is not a database authorization record.  ``CaseGrantRecord`` is a
small immutable value shared by the persistence and HTTP adapters; the
repository that loads it is intentionally kept in ``persistence``.
"""

from datetime import datetime

from pydantic import Field

from aftercare_agent.domain.common import (
    ContractModel,
    ContractViolation,
    ErrorCode,
    Identifier,
    PositiveInt,
    UtcDatetime,
)

#: Permissions a Case grant may carry.  Tenant-level scopes are deliberately
#: absent: ``case:create`` opens Cases for the whole tenant, and the
#: ``grant:*`` scopes administer this authorization table itself.  Delegating
#: either through one Case row would widen a subject's authority beyond that
#: Case.  Being a closed set, this is also the bound on what a grant request
#: may persist: an unrecognized string would otherwise sit in the ACL matching
#: nothing today and possibly something after a later release.
GRANTABLE_CASE_PERMISSIONS: frozenset[str] = frozenset(
    {
        "case:read",
        "review:read",
        "review:decide",
        "review:override",
        "strategy:migrate",
        "approval:read",
        "approval:decide",
    }
)

#: Tenant-level scopes for reading and changing Case grants.
GRANT_READ_PERMISSION = "grant:read"
GRANT_ADMIN_PERMISSION = "grant:admin"

#: The tenant-level administration scopes, and therefore everything an
#: access administrator may be told it can do *with* a Case row it holds no
#: grant on.  Kept separate from ``GRANTABLE_CASE_PERMISSIONS``: that set
#: bounds what may be delegated per Case, this one bounds what an inventory
#: row may report.  Mixing them would let a control-plane listing claim
#: Case-scoped content permissions the caller does not have.
ADMINISTRATION_PERMISSIONS: frozenset[str] = frozenset(
    {GRANT_READ_PERMISSION, GRANT_ADMIN_PERMISSION}
)


def authorize_case_grant(
    *, actor_permissions: frozenset[str], requested: frozenset[str]
) -> frozenset[str]:
    """Validate one delegation and return its canonical permissions.

    Two rules, both fail closed:

    * ``requested`` must be a non-empty subset of ``GRANTABLE_CASE_PERMISSIONS``;
    * the actor may not delegate a permission it does not hold, so the
      administration surface can never be used to escalate.  An administrator
      who must hand out ``approval:decide`` therefore needs that scope too; the
      role assignment belongs to the identity provider, not to this API.

    Granting to oneself is allowed by construction: the second rule already
    makes it a no-op.  Whether the grant is still active is decided when it is
    used, never here.
    """

    if not requested:
        raise ContractViolation(ErrorCode.INVALID_INPUT, "grant permissions cannot be empty")
    if unknown := requested - GRANTABLE_CASE_PERMISSIONS:
        raise ContractViolation(
            ErrorCode.INVALID_INPUT, f"unsupported grant permission: {','.join(sorted(unknown))}"
        )
    if escalation := requested - actor_permissions:
        raise ContractViolation(
            ErrorCode.FORBIDDEN,
            f"cannot grant a permission the actor lacks: {','.join(sorted(escalation))}",
        )
    return frozenset(requested)


class CaseGrantRecord(ContractModel):
    """One active or historical subject-to-Case authorization row."""

    tenant_id: Identifier
    subject_id: Identifier
    case_id: Identifier
    permissions: frozenset[Identifier] = Field(min_length=1)
    revision: PositiveInt
    granted_by: Identifier
    granted_at: UtcDatetime
    expires_at: UtcDatetime | None = None
    revoked_at: UtcDatetime | None = None
    revoked_by: Identifier | None = None
    updated_at: UtcDatetime

    def is_active(self, *, now: datetime) -> bool:
        """Return whether this snapshot grants access at ``now``."""
        current = now.astimezone(self.granted_at.tzinfo)
        return self.revoked_at is None and (self.expires_at is None or current < self.expires_at)
