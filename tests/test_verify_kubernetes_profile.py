from __future__ import annotations

import shutil
from pathlib import Path

from deploy.verify_kubernetes_profile import verify_profile

ROOT = Path(__file__).resolve().parents[1] / "deploy" / "kubernetes"
IMAGE = "registry.example/aftercare-agent@sha256:" + "a" * 64
ISSUER = "https://idp.example/"
AUDIENCE = "aftercare-api"
JWKS = "https://idp.example/.well-known/jwks.json"
TENANT = "tenant-staging"


def _rendered(tmp_path: Path) -> Path:
    directory = tmp_path / "kubernetes"
    shutil.copytree(ROOT, directory)
    for path in directory.glob("*.yaml"):
        text = path.read_text(encoding="utf-8")
        text = text.replace("registry.example/aftercare-agent@sha256:replace-me", IMAGE)
        text = text.replace("https://idp.example/", ISSUER)
        text = text.replace("https://idp.example/.well-known/jwks.json", JWKS)
        text = text.replace("replace-with-tenant-id", TENANT)
        path.write_text(text, encoding="utf-8")
    return directory


def test_rendered_profile_passes_without_contacting_a_cluster(tmp_path: Path) -> None:
    report = verify_profile(
        _rendered(tmp_path),
        image=IMAGE,
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_url=JWKS,
        tenant_id=TENANT,
    )

    assert report["status"] == "pass"
    assert {check["name"] for check in report["checks"]} == {
        "image_consistency",
        "runtime_config",
        "security_baseline",
        "database_role_boundary",
        "job_execution_boundary",
        "runtime_operability",
    }
    assert set(report["inputs"]) == {"reference.yaml", "migration-job.yaml", "rls-check-job.yaml"}
    assert all(record["bytes"] > 0 for record in report["inputs"].values())


def test_profile_rejects_a_placeholder_tenant(tmp_path: Path) -> None:
    report = verify_profile(
        _rendered(tmp_path),
        image=IMAGE,
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_url=JWKS,
        tenant_id="replace-with-tenant-id",
    )

    assert report["status"] == "fail"
    assert any("concrete tenant" in error for error in report["errors"])


def test_profile_rejects_image_drift(tmp_path: Path) -> None:
    directory = _rendered(tmp_path)
    worker = directory / "reference.yaml"
    worker.write_text(
        worker.read_text(encoding="utf-8").replace(IMAGE, IMAGE.replace("a" * 64, "b" * 64), 1),
        encoding="utf-8",
    )

    report = verify_profile(
        directory,
        image=IMAGE,
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_url=JWKS,
        tenant_id=TENANT,
    )

    assert report["status"] == "fail"
    assert any("supplied immutable image" in error for error in report["errors"])


def test_profile_rejects_migration_runtime_secret_reuse(tmp_path: Path) -> None:
    directory = _rendered(tmp_path)
    migration = directory / "migration-job.yaml"
    migration.write_text(
        migration.read_text(encoding="utf-8").replace(
            "aftercare-migration-database-url", "aftercare-runtime-database-url"
        ),
        encoding="utf-8",
    )

    report = verify_profile(
        directory,
        image=IMAGE,
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_url=JWKS,
        tenant_id=TENANT,
    )

    assert report["status"] == "fail"
    assert any("separate database identities" in error for error in report["errors"])


def test_profile_rejects_workload_without_database_tls_enforcement(tmp_path: Path) -> None:
    directory = _rendered(tmp_path)
    migration = directory / "migration-job.yaml"
    migration.write_text(
        migration.read_text(encoding="utf-8").replace(
            '            - name: AFTERCARE_REQUIRE_DATABASE_TLS\n              value: "1"\n',
            "",
            1,
        ),
        encoding="utf-8",
    )

    report = verify_profile(
        directory,
        image=IMAGE,
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_url=JWKS,
        tenant_id=TENANT,
    )

    assert report["status"] == "fail"
    assert any("identities and TLS" in error for error in report["errors"])


def test_profile_rejects_world_readable_secret_projection(tmp_path: Path) -> None:
    directory = _rendered(tmp_path)
    migration = directory / "migration-job.yaml"
    migration.write_text(
        migration.read_text(encoding="utf-8").replace("defaultMode: 256", "defaultMode: 420", 1),
        encoding="utf-8",
    )

    report = verify_profile(
        directory,
        image=IMAGE,
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_url=JWKS,
        tenant_id=TENANT,
    )

    assert report["status"] == "fail"
    assert any("separate database identities" in error for error in report["errors"])


def test_profile_rejects_default_service_account_binding(tmp_path: Path) -> None:
    directory = _rendered(tmp_path)
    reference = directory / "reference.yaml"
    reference.write_text(
        reference.read_text(encoding="utf-8").replace(
            "serviceAccountName: aftercare", "serviceAccountName: default", 1
        ),
        encoding="utf-8",
    )

    report = verify_profile(
        directory,
        image=IMAGE,
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_url=JWKS,
        tenant_id=TENANT,
    )

    assert report["status"] == "fail"
    assert any("security baseline" in error for error in report["errors"])


def test_profile_rejects_public_api_service(tmp_path: Path) -> None:
    directory = _rendered(tmp_path)
    reference = directory / "reference.yaml"
    reference.write_text(
        reference.read_text(encoding="utf-8").replace("type: ClusterIP", "type: LoadBalancer", 1),
        encoding="utf-8",
    )

    report = verify_profile(
        directory,
        image=IMAGE,
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_url=JWKS,
        tenant_id=TENANT,
    )

    assert report["status"] == "fail"
    assert any("security baseline" in error for error in report["errors"])


def test_profile_rejects_service_without_api_selector(tmp_path: Path) -> None:
    directory = _rendered(tmp_path)
    reference = directory / "reference.yaml"
    reference.write_text(
        reference.read_text(encoding="utf-8").replace(
            "app.kubernetes.io/name: aftercare-api\n  ports:",
            "app.kubernetes.io/name: wrong-api\n  ports:",
            1,
        ),
        encoding="utf-8",
    )

    report = verify_profile(
        directory,
        image=IMAGE,
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_url=JWKS,
        tenant_id=TENANT,
    )

    assert report["status"] == "fail"
    assert any("security baseline" in error for error in report["errors"])


def test_profile_rejects_missing_api_readiness_probe(tmp_path: Path) -> None:
    directory = _rendered(tmp_path)
    reference = directory / "reference.yaml"
    reference.write_text(
        reference.read_text(encoding="utf-8").replace("path: /readyz", "path: /healthz", 1),
        encoding="utf-8",
    )

    report = verify_profile(
        directory,
        image=IMAGE,
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_url=JWKS,
        tenant_id=TENANT,
    )

    assert report["status"] == "fail"
    assert any("resources" in error for error in report["errors"])


def test_profile_returns_structured_failure_for_malformed_probe(tmp_path: Path) -> None:
    directory = _rendered(tmp_path)
    reference = directory / "reference.yaml"
    reference.write_text(
        reference.read_text(encoding="utf-8").replace(
            "          readinessProbe:\n", "          readinessProbe: malformed\n", 1
        ),
        encoding="utf-8",
    )

    report = verify_profile(
        directory,
        image=IMAGE,
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_url=JWKS,
        tenant_id=TENANT,
    )

    assert report["status"] == "fail"
    assert report["errors"]
