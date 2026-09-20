from pathlib import Path

import pytest

from aftercare_agent.config import environment_secret
from aftercare_agent.model_adapters import ProviderEndpoint


def test_file_secrets_are_one_line_and_not_ambiguous(tmp_path: Path) -> None:
    secret = tmp_path / "database-url"
    secret.write_text("postgresql://runtime@db/aftercare\n", encoding="utf-8")
    assert environment_secret("DATABASE_URL", environ={"DATABASE_URL_FILE": str(secret)}) == (
        "postgresql://runtime@db/aftercare"
    )
    with pytest.raises(RuntimeError, match="only one"):
        environment_secret(
            "DATABASE_URL",
            environ={"DATABASE_URL": "direct", "DATABASE_URL_FILE": str(secret)},
        )


def test_deployment_database_secret_requires_verify_full_tls(tmp_path: Path) -> None:
    secret = tmp_path / "database-url"
    secret.write_text("postgresql://runtime@db/aftercare?sslmode=require\n", encoding="utf-8")
    env = {
        "DATABASE_URL_FILE": str(secret),
        "AFTERCARE_REQUIRE_DATABASE_TLS": "1",
    }
    with pytest.raises(RuntimeError, match="sslmode=verify-full"):
        environment_secret("DATABASE_URL", environ=env)

    secret.write_text("postgresql://runtime@db/aftercare?sslmode=verify-full\n", encoding="utf-8")
    assert environment_secret("DATABASE_URL", environ=env) is not None


def test_model_provider_key_can_come_from_a_secret_file(tmp_path: Path) -> None:
    secret = tmp_path / "model-key"
    secret.write_text("provider-secret\nsecond-line", encoding="utf-8")
    with pytest.raises(RuntimeError, match="one non-empty line"):
        ProviderEndpoint.from_env({"AFTERCARE_MODEL_API_KEY_FILE": str(secret)})
    secret.write_text("provider-secret\n", encoding="utf-8")
    endpoint = ProviderEndpoint.from_env({"AFTERCARE_MODEL_API_KEY_FILE": str(secret)})
    assert endpoint.api_key == "provider-secret"
