"""Database-backed Case grants used as the authoritative API scope.

The bearer token identifies a subject and may carry a case upper bound, but a
token claim is not a database authorization record.  ``CaseGrantRecord`` is a
small immutable value shared by the persistence and HTTP adapters; the
repository that loads it is intentionally kept in ``persistence``.
"""

from datetime import datetime

from pydantic import Field

from aftercare_agent.domain.common import ContractModel, Identifier, PositiveInt, UtcDatetime


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
