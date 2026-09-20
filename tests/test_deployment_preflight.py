from __future__ import annotations

import json
import os
from pathlib import Path

from deploy.deployment_preflight import preflight


def _env(tmp_path: Path, *, image: str = "registry.example/aftercare@sha256:" + "a" * 64) -> Path:
    migration = tmp_path / "migration-dsn"
    runtime = tmp_path / "runtime-dsn"
    migration.write_text(
        "postgresql://migration@db/aftercare?sslmode=verify-full\n", encoding="utf-8"
    )
    runtime.write_text("postgresql://runtime@db/aftercare?sslmode=verify-full\n", encoding="utf-8")
    if os.name == "posix":
        migration.chmod(0o600)
        runtime.chmod(0o600)
    env = tmp_path / "deployment.env"
    env.write_text(
        "\n".join(
            [
                f"AFTERCARE_IMAGE={image}",
                f"AFTERCARE_MIGRATION_DATABASE_URL_FILE={migration}",
                f"AFTERCARE_RUNTIME_DATABASE_URL_FILE={runtime}",
                "AFTERCARE_REQUIRE_DATABASE_TLS=1",
                "AFTERCARE_OIDC_ISSUER=https://idp.example/",
                "AFTERCARE_OIDC_AUDIENCE=aftercare-api",
                "AFTERCARE_OIDC_JWKS_URL=https://idp.example/.well-known/jwks.json",
                "AFTERCARE_TENANT_ID=tenant-staging",
                "AFTERCARE_API_POOL_MIN_SIZE=1",
                "AFTERCARE_API_POOL_MAX_SIZE=8",
                "AFTERCARE_WORKER_POOL_MIN_SIZE=1",
                "AFTERCARE_WORKER_POOL_MAX_SIZE=4",
            ]
        ),
        encoding="utf-8",
    )
    return env


def test_deployment_preflight_passes_without_printing_secret_contents(tmp_path: Path) -> None:
    report = preflight(_env(tmp_path))

    assert report["status"] == "pass"
    assert "postgresql://" not in json.dumps(report)
    assert all(
        check.get("sslmode") == "verify-full"
        for check in report["checks"]
        if check["name"] in {"migration_database_url", "runtime_database_url"}
    )


def test_deployment_preflight_rejects_mutable_image_and_http_oidc(tmp_path: Path) -> None:
    env = _env(tmp_path, image="registry.example/aftercare:latest")
    env.write_text(
        env.read_text(encoding="utf-8").replace("https://idp.example/", "http://idp.example/"),
        encoding="utf-8",
    )

    report = preflight(env)

    assert report["status"] == "fail"
    assert any("immutable" in error for error in report["errors"])
    assert any("HTTPS" in error for error in report["errors"])


def test_deployment_preflight_rejects_shared_database_secret(tmp_path: Path) -> None:
    env = _env(tmp_path)
    text = env.read_text(encoding="utf-8")
    migration_path = next(
        line.split("=", 1)[1]
        for line in text.splitlines()
        if line.startswith("AFTERCARE_MIGRATION_DATABASE_URL_FILE=")
    )
    env.write_text(
        text.replace(
            next(
                line
                for line in text.splitlines()
                if line.startswith("AFTERCARE_RUNTIME_DATABASE_URL_FILE=")
            ),
            f"AFTERCARE_RUNTIME_DATABASE_URL_FILE={migration_path}",
        ),
        encoding="utf-8",
    )

    report = preflight(env)

    assert report["status"] == "fail"
    assert any("must differ" in error for error in report["errors"])


def test_deployment_preflight_rejects_unknown_metrics_backend(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env.write_text(
        env.read_text(encoding="utf-8") + "\nAFTERCARE_METRICS_BACKEND=unknown\n",
        encoding="utf-8",
    )

    report = preflight(env)

    assert report["status"] == "fail"
    assert any("METRICS_BACKEND" in error for error in report["errors"])


def test_deployment_preflight_rejects_missing_worker_tenant(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env.write_text(
        "\n".join(
            line
            for line in env.read_text(encoding="utf-8").splitlines()
            if not line.startswith("AFTERCARE_TENANT_ID=")
        ),
        encoding="utf-8",
    )

    report = preflight(env)

    assert report["status"] == "fail"
    assert any("TENANT_ID" in error for error in report["errors"])


def test_deployment_preflight_rejects_group_or_world_readable_secret(tmp_path: Path) -> None:
    if os.name != "posix":
        return
    env = _env(tmp_path)
    migration_path = next(
        line.split("=", 1)[1]
        for line in env.read_text(encoding="utf-8").splitlines()
        if line.startswith("AFTERCARE_MIGRATION_DATABASE_URL_FILE=")
    )
    Path(migration_path).chmod(0o640)

    report = preflight(env)

    assert report["status"] == "fail"
    assert any("owner-only" in error for error in report["errors"])


def test_deployment_preflight_rejects_relative_secret_path(tmp_path: Path) -> None:
    env = _env(tmp_path)
    text = env.read_text(encoding="utf-8")
    migration_path = next(
        line.split("=", 1)[1]
        for line in text.splitlines()
        if line.startswith("AFTERCARE_MIGRATION_DATABASE_URL_FILE=")
    )
    env.write_text(
        text.replace(migration_path, "secrets/migration-dsn", 1),
        encoding="utf-8",
    )

    report = preflight(env)

    assert report["status"] == "fail"
    assert any("must be absolute" in error for error in report["errors"])


def test_deployment_preflight_rejects_plaintext_secret_variables(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env.write_text(
        env.read_text(encoding="utf-8")
        + "\nDATABASE_URL=postgresql://inline-secret@db/aftercare\n"
        + "AFTERCARE_MODEL_API_KEY=inline-model-secret\n",
        encoding="utf-8",
    )

    report = preflight(env)

    assert report["status"] == "fail"
    assert any("DATABASE_URL" in error for error in report["errors"])
    assert any("AFTERCARE_MODEL_API_KEY" in error for error in report["errors"])
    assert "inline-secret" not in json.dumps(report)


def test_deployment_preflight_rejects_database_without_hostname_verification(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    migration_path = next(
        Path(line.split("=", 1)[1])
        for line in env.read_text(encoding="utf-8").splitlines()
        if line.startswith("AFTERCARE_MIGRATION_DATABASE_URL_FILE=")
    )
    migration_path.write_text(
        "postgresql://migration@db/aftercare?sslmode=require\n", encoding="utf-8"
    )

    report = preflight(env)

    assert report["status"] == "fail"
    assert any("sslmode=verify-full" in error for error in report["errors"])
