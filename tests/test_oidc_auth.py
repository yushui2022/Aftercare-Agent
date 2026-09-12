from datetime import UTC, datetime, timedelta

import pytest

from aftercare_agent.auth.context import AuthContext
from aftercare_agent.auth.oidc import OidcConfig, VerifiedOidcClaims, auth_context_from_claims
from aftercare_agent.domain.common import ContractViolation, ErrorCode

NOW = datetime(2026, 9, 12, 12, tzinfo=UTC)
CONFIG = OidcConfig(issuer="https://idp.example", audience="aftercare-api")


def claims(**changes: object) -> VerifiedOidcClaims:
    value: dict[str, object] = {
        "issuer": CONFIG.issuer,
        "subject": "user-1",
        "audience": CONFIG.audience,
        "tenant_id": "tenant-1",
        "permissions": frozenset({"case:read"}),
        "case_ids": frozenset({"case-1"}),
        "expires_at": NOW + timedelta(minutes=5),
    }
    value.update(changes)
    return VerifiedOidcClaims.model_validate(value)


def test_verified_claims_produce_case_scoped_non_synthetic_context() -> None:
    context = auth_context_from_claims(claims(), CONFIG, now=NOW)
    assert isinstance(context, AuthContext)
    assert not context.synthetic
    context.require_case("case-1", "case:read")
    with pytest.raises(ContractViolation) as error:
        context.require_case("case-2", "case:read")
    assert error.value.code is ErrorCode.FORBIDDEN


@pytest.mark.parametrize(
    "change",
    [
        {"issuer": "https://other.example"},
        {"audience": "other-api"},
        {"expires_at": NOW - timedelta(seconds=31)},
    ],
)
def test_claims_must_match_deployment_policy(change: dict[str, object]) -> None:
    with pytest.raises(ContractViolation) as error:
        auth_context_from_claims(claims(**change), CONFIG, now=NOW)
    assert error.value.code is ErrorCode.UNAUTHENTICATED
