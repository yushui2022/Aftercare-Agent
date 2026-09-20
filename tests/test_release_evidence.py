from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from deploy.release_evidence import build_manifest
from deploy.verify_cosign_evidence import normalize_cosign_output
from deploy.verify_registry_rescan import normalize_registry_scan


def _write_evidence(
    tmp_path: Path, *, trivy_findings: list[dict[str, object]] | None = None
) -> dict[str, Path]:
    labels = {
        "org.opencontainers.image.source": "https://github.com/yushui2022/Aftercare-Agent",
        "org.opencontainers.image.licenses": "MIT",
        "org.opencontainers.image.version": "0.1.0a0",
        "org.opencontainers.image.revision": "b" * 40,
        "io.aftercare.egm.git-sha": "c" * 40,
        "io.aftercare.schema.aftercare-migration": "20",
        "io.aftercare.schema.egm": "1",
    }
    image = [
        {
            "Id": "sha256:" + "a" * 64,
            "RepoDigests": [],
            "Config": {
                "User": "10001:10001",
                "Labels": labels,
            },
        }
    ]
    paths = {
        "image_metadata": tmp_path / "image.json",
        "oci_evidence": tmp_path / "oci.json",
        "pip_audit": tmp_path / "pip.json",
        "trivy": tmp_path / "trivy.json",
        "gitleaks": tmp_path / "gitleaks.json",
    }
    paths["image_metadata"].write_text(json.dumps(image), encoding="utf-8")
    paths["oci_evidence"].write_text(
        json.dumps(
            {
                "image_digest": "sha256:" + "d" * 64,
                "image_labels": labels,
                "predicates": ["https://spdx.dev/Document", "https://slsa.dev/provenance/v1"],
            }
        ),
        encoding="utf-8",
    )
    paths["pip_audit"].write_text(json.dumps({"dependencies": [], "fixes": []}), encoding="utf-8")
    paths["trivy"].write_text(
        json.dumps({"Results": [{"Vulnerabilities": trivy_findings or []}]}), encoding="utf-8"
    )
    paths["gitleaks"].write_text(
        json.dumps({"version": "2.1.0", "runs": [{"results": []}]}), encoding="utf-8"
    )
    return paths


def test_build_manifest_records_pass_and_open_gates(tmp_path: Path) -> None:
    paths = _write_evidence(tmp_path)
    manifest = build_manifest(**paths)

    assert manifest["status"] == "pass"
    assert manifest["image"]["user"] == "10001:10001"
    assert manifest["build"]["aftercare_version"] == "0.1.0a0"
    assert len(manifest["evidence_files"]["oci_evidence"]["sha256"]) == 64
    assert "image_signature" in manifest["unresolved_gates"]


def test_build_manifest_fails_on_trivy_findings(tmp_path: Path) -> None:
    paths = _write_evidence(tmp_path, trivy_findings=[{"Severity": "HIGH"}])

    with pytest.raises(ValueError, match="Trivy reported"):
        build_manifest(**paths)


def test_build_manifest_fails_on_missing_image_label(tmp_path: Path) -> None:
    paths = _write_evidence(tmp_path)
    image = json.loads(paths["image_metadata"].read_text(encoding="utf-8"))
    del image[0]["Config"]["Labels"]["io.aftercare.schema.egm"]
    paths["image_metadata"].write_text(json.dumps(image), encoding="utf-8")

    with pytest.raises(ValueError, match="required labels"):
        build_manifest(**paths)


def test_build_manifest_fails_on_attested_label_mismatch(tmp_path: Path) -> None:
    paths = _write_evidence(tmp_path)
    oci = json.loads(paths["oci_evidence"].read_text(encoding="utf-8"))
    oci["image_labels"]["io.aftercare.schema.egm"] = "2"
    paths["oci_evidence"].write_text(json.dumps(oci), encoding="utf-8")

    with pytest.raises(ValueError, match="attestation labels"):
        build_manifest(**paths)


def test_build_manifest_passes_image_signature_with_raw_cosign_output(tmp_path: Path) -> None:
    paths = _write_evidence(tmp_path)
    raw = [
        {
            "critical": {
                "type": "cosign container image signature",
                "image": {"docker-manifest-digest": "sha256:" + "d" * 64},
            },
            "optional": {
                "Issuer": "https://token.actions.githubusercontent.com",
                "Subject": "repo:yushui2022/Aftercare-Agent:ref:refs/heads/main",
            },
        }
    ]
    source = json.dumps(raw, sort_keys=True).encode("utf-8")
    raw_path = tmp_path / "cosign.json"
    evidence_path = tmp_path / "signature-evidence.json"
    raw_path.write_bytes(source)
    evidence_path.write_text(
        json.dumps(
            normalize_cosign_output(
                raw,
                image_digest="sha256:" + "d" * 64,
                issuer="https://token.actions.githubusercontent.com",
                subject_regexp=r"repo:yushui2022/Aftercare-Agent:ref:refs/heads/main",
                source_bytes=source,
            )
        ),
        encoding="utf-8",
    )

    manifest = build_manifest(
        **paths,
        signature_evidence=evidence_path,
        signature_source=raw_path,
    )

    assert manifest["promotion_gates"]["image_signature"] == {"status": "passed"}
    assert "image_signature" not in manifest["unresolved_gates"]
    assert manifest["signature"]["verified"] is True


def test_build_manifest_rejects_signature_for_different_attestation(tmp_path: Path) -> None:
    paths = _write_evidence(tmp_path)
    raw_path = tmp_path / "cosign.json"
    evidence_path = tmp_path / "signature-evidence.json"
    source = b"raw cosign output"
    raw_path.write_bytes(source)
    evidence_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "verifier": "cosign",
                "verified": True,
                "image_digest": "sha256:" + "e" * 64,
                "signature_count": 1,
                "identities": [{"issuer": "issuer", "subject": "subject"}],
                "source": {"bytes": len(source), "sha256": hashlib.sha256(source).hexdigest()},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="not verified for the attested image"):
        build_manifest(**paths, signature_evidence=evidence_path, signature_source=raw_path)


def test_build_manifest_passes_target_registry_rescan(tmp_path: Path) -> None:
    paths = _write_evidence(tmp_path)
    image_digest = "sha256:" + "d" * 64
    image_reference = f"registry.example/aftercare-agent@{image_digest}"
    raw = {
        "ArtifactName": image_reference,
        "ArtifactType": "container_image",
        "Metadata": {"RepoDigests": [image_reference]},
        "Results": [{"Vulnerabilities": []}],
    }
    source = json.dumps(raw, sort_keys=True).encode("utf-8")
    raw_path = tmp_path / "registry-scan.json"
    evidence_path = tmp_path / "registry-evidence.json"
    raw_path.write_bytes(source)
    evidence_path.write_text(
        json.dumps(
            normalize_registry_scan(
                raw,
                image_reference=image_reference,
                source_bytes=source,
            )
        ),
        encoding="utf-8",
    )

    manifest = build_manifest(
        **paths,
        registry_rescan_evidence=evidence_path,
        registry_rescan_source=raw_path,
    )

    assert manifest["promotion_gates"]["target_registry_rescan"] == {"status": "passed"}
    assert "target_registry_rescan" not in manifest["unresolved_gates"]


def test_build_manifest_passes_matching_deployment_acceptance(tmp_path: Path) -> None:
    paths = _write_evidence(tmp_path)
    record_path = tmp_path / "deployment-acceptance.json"
    checks: dict[str, dict[str, object]] = {
        name: {"status": "pass", "evidence": f"https://evidence.example/{name}"}
        for name in (
            "preflight",
            "migration",
            "runtime_permissions",
            "tenant_isolation",
            "readiness",
            "worker_recovery",
            "secret_rotation",
            "rollback",
        )
    }
    checks["tenant_isolation"].update(
        {
            "verifier": "aftercare-rls",
            "decision": "pass",
            "image_digest": "sha256:" + "d" * 64,
            "runtime_role": "aftercare_runtime",
            "tenant_table_count": 35,
            "empty_context_rows": 0,
            "cross_tenant_write": "rejected",
            "cross_tenant_update": "hidden",
            "cross_tenant_delete": "hidden",
            "hardened": True,
            "source": {
                "harden": {"bytes": 10, "sha256": "e" * 64},
                "verify": {"bytes": 11, "sha256": "f" * 64},
            },
            "evidence_sha256": "a" * 64,
            "evidence_bytes": 100,
        }
    )
    checks["pitr"] = {
        "status": "pass",
        "evidence": "https://evidence.example/pitr-verified.json",
        "verifier": "aftercare-pitr",
        "decision": "pass",
        "manifest_sha256": "b" * 64,
        "evidence_sha256": "c" * 64,
        "base_backup_sha256": "d" * 64,
    }
    checks["preflight"].update(
        {
            "schema_version": 1,
            "verifier": "aftercare-preflight",
            "status_result": "pass",
            "image_digest": "sha256:" + "d" * 64,
            "checks": [
                {
                    "name": name,
                    "status": "pass",
                    **(
                        {"sslmode": "verify-full"}
                        if name in {"migration_database_url", "runtime_database_url"}
                        else {}
                    ),
                }
                for name in (
                    "immutable_image",
                    "migration_database_url",
                    "runtime_database_url",
                    "oidc_issuer",
                    "oidc_jwks_url",
                    "oidc_audience",
                    "worker_tenant",
                    "metrics_backend",
                    "aftercare_api_pool_bounds",
                    "aftercare_worker_pool_bounds",
                )
            ],
            "errors": [],
            "evidence_sha256": "e" * 64,
            "evidence_bytes": 100,
        }
    )
    record_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "environment": "staging",
                "image_digest": "sha256:" + "d" * 64,
                "checks": checks,
            }
        ),
        encoding="utf-8",
    )

    manifest = build_manifest(**paths, deployment_acceptance=record_path)

    assert manifest["promotion_gates"]["target_environment_deployment"] == {"status": "passed"}
    assert "target_environment_deployment" not in manifest["unresolved_gates"]
    assert manifest["deployment_acceptance"]["pitr_evidence"]["decision"] == "pass"
    assert manifest["deployment_acceptance"]["pitr_evidence"]["evidence_sha256"] == "c" * 64
    assert (
        manifest["deployment_acceptance"]["tenant_isolation_evidence"]["verifier"]
        == "aftercare-rls"
    )


def test_build_manifest_binds_ci_pitr_profile_files(tmp_path: Path) -> None:
    paths = _write_evidence(tmp_path)
    pitr_dir = tmp_path / "pitr"
    pitr_dir.mkdir()
    manifest_path = pitr_dir / "base.manifest.json"
    evidence_path = pitr_dir / "pitr-run.json"
    verified_path = pitr_dir / "pitr-verified.json"
    manifest_path.write_text('{"artifact":"base.tar"}\n', encoding="utf-8")
    evidence_path.write_text('{"schema_version":1}\n', encoding="utf-8")
    manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    evidence_sha = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    verified_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "verifier": "aftercare-pitr",
                "decision": "pass",
                "manifest_sha256": manifest_sha,
                "evidence_sha256": evidence_sha,
                "base_backup_sha256": "e" * 64,
            }
        ),
        encoding="utf-8",
    )

    result = build_manifest(
        **paths,
        pitr_manifest=manifest_path,
        pitr_evidence=evidence_path,
        pitr_verified=verified_path,
    )

    assert result["pitr_profile"]["decision"] == "pass"
    assert result["evidence_files"]["pitr_verified"]["bytes"] == verified_path.stat().st_size


def test_build_manifest_binds_matching_kubernetes_profile(tmp_path: Path) -> None:
    paths = _write_evidence(tmp_path)
    profile_path = tmp_path / "kubernetes-profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "verifier": "aftercare-kubernetes-profile",
                "image_digest": "sha256:" + "d" * 64,
                "status": "pass",
                "checks": [
                    {"name": name, "status": "pass"}
                    for name in (
                        "image_consistency",
                        "runtime_config",
                        "security_baseline",
                        "database_role_boundary",
                        "job_execution_boundary",
                        "runtime_operability",
                    )
                ],
                "errors": [],
                "inputs": {
                    filename: {"bytes": 100, "sha256": "f" * 64}
                    for filename in ("reference.yaml", "migration-job.yaml", "rls-check-job.yaml")
                },
            }
        ),
        encoding="utf-8",
    )

    result = build_manifest(**paths, kubernetes_profile_evidence=profile_path)

    assert result["kubernetes_profile"]["verifier"] == "aftercare-kubernetes-profile"
    assert (
        result["evidence_files"]["kubernetes_profile_evidence"]["bytes"]
        == profile_path.stat().st_size
    )


def test_build_manifest_rejects_kubernetes_profile_for_different_image(tmp_path: Path) -> None:
    paths = _write_evidence(tmp_path)
    profile_path = tmp_path / "kubernetes-profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "verifier": "aftercare-kubernetes-profile",
                "image_digest": "sha256:" + "e" * 64,
                "status": "pass",
                "checks": [],
                "errors": [],
                "inputs": {},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="not verified for the attested image"):
        build_manifest(**paths, kubernetes_profile_evidence=profile_path)
