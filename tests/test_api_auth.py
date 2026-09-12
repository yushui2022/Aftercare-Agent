import pytest
from pydantic import ValidationError

from aftercare_agent.api.app import create_default_app
from aftercare_agent.auth import SyntheticAuthError, synthetic_context
from aftercare_agent.domain.runtime import OpenCaseInput


def test_synthetic_identity_is_explicitly_disabled_by_default() -> None:
    with pytest.raises(SyntheticAuthError):
        synthetic_context(enabled=False, tenant_id="tenant-1", subject_id="subject-1")


def test_open_case_input_rejects_authority_fields() -> None:
    with pytest.raises(ValidationError):
        OpenCaseInput(
            order_id="order-1",
            channel="synthetic",
            message_ref="message-1",
            message_sha256="a" * 64,
            tenant_id="attacker",  # type: ignore[call-arg]
        )


def test_synthetic_identity_headers_use_identifier_constraints() -> None:
    with pytest.raises(SyntheticAuthError):
        synthetic_context(enabled=True, tenant_id="tenant with spaces", subject_id="subject-1")


def test_default_app_can_be_imported_without_database_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    app = create_default_app()
    assert app.title == "Aftercare Agent"
