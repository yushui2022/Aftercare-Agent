"""Build a deterministic release evidence manifest from CI evidence files."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from typing import Any, cast

REQUIRED_LABELS = (
    "org.opencontainers.image.source",
    "org.opencontainers.image.licenses",
    "org.opencontainers.image.version",
    "org.opencontainers.image.revision",
    "io.aftercare.egm.git-sha",
    "io.aftercare.schema.aftercare-migration",
    "io.aftercare.schema.egm",
)
PROMOTION_GATES = (
    "image_signature",
    "target_registry_rescan",
    "target_environment_deployment",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_KUBERNETES_PROFILE_CHECKS = {
    "image_consistency",
    "runtime_config",
    "security_baseline",
    "database_role_boundary",
    "job_execution_boundary",
    "runtime_operability",
}
_KUBERNETES_PROFILE_FILES = {"reference.yaml", "migration-job.yaml", "rls-check-job.yaml"}


def _evaluate_deployment_acceptance(record: dict[str, Any]) -> dict[str, Any]:
    """Load the sibling validator from module and direct-script entry points."""
    try:
        module = import_module("deploy.deployment_acceptance")
    except ModuleNotFoundError:  # pragma: no cover - direct ``python deploy/*.py`` entry point
        module = import_module("deployment_acceptance")
    evaluator = cast(Callable[[dict[str, Any]], dict[str, Any]], module.evaluate)
    return evaluator(record)


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON evidence {path}: {exc}") from exc


def _file_digest(path: Path) -> dict[str, int | str]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read evidence bytes {path}: {exc}") from exc
    return {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}


def _image(value: Any) -> dict[str, Any]:
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise ValueError("image metadata must be a one-item docker inspect array")
    item = value[0]
    config = item.get("Config")
    if not isinstance(config, dict):
        raise ValueError("image metadata has no Config object")
    labels = config.get("Labels")
    if not isinstance(labels, dict):
        raise ValueError("image metadata has no labels")
    missing = [
        name
        for name in REQUIRED_LABELS
        if not isinstance(labels.get(name), str) or not labels[name]
    ]
    if missing:
        raise ValueError(f"image is missing required labels: {missing}")
    image_id = item.get("Id")
    if not isinstance(image_id, str) or not image_id.startswith("sha256:"):
        raise ValueError("image metadata has no sha256 image id")
    return {
        "id": image_id,
        "user": config.get("User"),
        "labels": {name: labels[name] for name in sorted(REQUIRED_LABELS)},
        "repo_digests": sorted(item.get("RepoDigests", []))
        if isinstance(item.get("RepoDigests", []), list)
        else [],
    }


def _pip_audit(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("pip-audit report must be an object")
    dependencies = value.get("dependencies")
    fixes = value.get("fixes")
    if not isinstance(dependencies, list) or not isinstance(fixes, list):
        raise ValueError("pip-audit report has an unexpected shape")
    vulnerabilities = sum(
        len(item.get("vulns", []))
        for item in dependencies
        if isinstance(item, dict) and isinstance(item.get("vulns", []), list)
    )
    return {
        "dependencies": len(dependencies),
        "vulnerabilities": vulnerabilities,
        "fixes": len(fixes),
    }


def _trivy(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Trivy report must be an object")
    results = value.get("Results", [])
    if not isinstance(results, list):
        raise ValueError("Trivy report has an unexpected Results value")
    findings = 0
    severities: dict[str, int] = {}
    for result in results:
        if not isinstance(result, dict):
            raise ValueError("Trivy result is malformed")
        vulnerabilities = result.get("Vulnerabilities") or []
        if not isinstance(vulnerabilities, list):
            raise ValueError("Trivy vulnerabilities value is malformed")
        findings += len(vulnerabilities)
        for vulnerability in vulnerabilities:
            if not isinstance(vulnerability, dict):
                raise ValueError("Trivy vulnerability is malformed")
            severity = vulnerability.get("Severity", "UNKNOWN")
            if not isinstance(severity, str):
                raise ValueError("Trivy vulnerability severity is malformed")
            severities[severity] = severities.get(severity, 0) + 1
    metadata = value.get("Metadata")
    image_id = metadata.get("ImageID") if isinstance(metadata, dict) else None
    return {
        "artifact_id": value.get("ArtifactID"),
        "image_id": image_id,
        "findings": findings,
        "severities": dict(sorted(severities.items())),
    }


def _gitleaks(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("version") != "2.1.0":
        raise ValueError("Gitleaks report must be SARIF 2.1.0")
    runs = value.get("runs")
    if not isinstance(runs, list):
        raise ValueError("Gitleaks SARIF has no runs")
    findings = sum(
        len(run.get("results", []))
        for run in runs
        if isinstance(run, dict) and isinstance(run.get("results", []), list)
    )
    return {"findings": findings, "runs": len(runs)}


def _signature(
    value: Any, *, image_digest: str, source: Path, source_bytes: bytes
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("signature evidence must be an object")
    if value.get("schema_version") != 1 or value.get("verifier") != "cosign":
        raise ValueError("signature evidence has an unsupported schema")
    if value.get("verified") is not True or value.get("image_digest") != image_digest:
        raise ValueError("signature evidence is not verified for the attested image")
    source_record = value.get("source")
    if not isinstance(source_record, dict):
        raise ValueError("signature evidence has no source record")
    if (
        source_record.get("bytes") != len(source_bytes)
        or source_record.get("sha256") != hashlib.sha256(source_bytes).hexdigest()
    ):
        raise ValueError(f"signature evidence does not match raw cosign output {source}")
    if not isinstance(value.get("signature_count"), int) or value["signature_count"] < 1:
        raise ValueError("signature evidence has no verified signatures")
    identities = value.get("identities")
    if not isinstance(identities, list) or not identities:
        raise ValueError("signature evidence has no verified identities")
    return value


def _registry_rescan(
    value: Any, *, image_digest: str, source: Path, source_bytes: bytes
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("registry rescan evidence must be an object")
    if value.get("schema_version") != 1 or value.get("verifier") != "trivy-registry":
        raise ValueError("registry rescan evidence has an unsupported schema")
    if value.get("verified") is not True or value.get("image_digest") != image_digest:
        raise ValueError("registry rescan is not verified for the attested image")
    if value.get("findings") != 0:
        raise ValueError("registry rescan contains vulnerability findings")
    source_record = value.get("source")
    if not isinstance(source_record, dict):
        raise ValueError("registry rescan evidence has no source record")
    if (
        source_record.get("bytes") != len(source_bytes)
        or source_record.get("sha256") != hashlib.sha256(source_bytes).hexdigest()
    ):
        raise ValueError(f"registry rescan evidence does not match raw scanner output {source}")
    return value


def _pitr_profile(manifest_path: Path, evidence_path: Path, verified_path: Path) -> dict[str, Any]:
    """Bind the CI PITR profile's three portable JSON artifacts together."""

    verified = _read(verified_path)
    if not isinstance(verified, dict):
        raise ValueError("PITR verified evidence must be an object")
    if verified.get("schema_version") != 1 or verified.get("verifier") != "aftercare-pitr":
        raise ValueError("PITR verified evidence has an unsupported schema")
    if verified.get("decision") != "pass":
        raise ValueError("PITR profile is not a passing verified result")
    manifest_record = _file_digest(manifest_path)
    evidence_record = _file_digest(evidence_path)
    if verified.get("manifest_sha256") != manifest_record["sha256"]:
        raise ValueError("PITR verified evidence does not match its manifest")
    if verified.get("evidence_sha256") != evidence_record["sha256"]:
        raise ValueError("PITR verified evidence does not match its recovery report")
    for name in ("base_backup_sha256", "manifest_sha256", "evidence_sha256"):
        value = verified.get(name)
        if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
            raise ValueError(f"PITR verified evidence has an invalid {name}")
    return {
        "verifier": "aftercare-pitr",
        "decision": "pass",
        "manifest_sha256": manifest_record["sha256"],
        "evidence_sha256": evidence_record["sha256"],
        "base_backup_sha256": verified["base_backup_sha256"],
        "files": {
            "manifest": manifest_record,
            "evidence": evidence_record,
            "verified": _file_digest(verified_path),
        },
    }


def _kubernetes_profile(value: Any, *, image_digest: str) -> dict[str, Any]:
    """Validate an optional offline Kubernetes profile report for this image."""

    if not isinstance(value, dict):
        raise ValueError("Kubernetes profile evidence must be an object")
    if value.get("schema_version") != 1 or value.get("verifier") != "aftercare-kubernetes-profile":
        raise ValueError("Kubernetes profile evidence has an unsupported schema")
    if value.get("status") != "pass" or value.get("image_digest") != image_digest:
        raise ValueError("Kubernetes profile evidence is not verified for the attested image")
    errors = value.get("errors")
    checks = value.get("checks")
    if errors != [] or not isinstance(checks, list):
        raise ValueError("Kubernetes profile evidence is not a clean passing report")
    inputs = value.get("inputs")
    if not isinstance(inputs, dict) or set(inputs) != _KUBERNETES_PROFILE_FILES:
        raise ValueError("Kubernetes profile evidence has incomplete manifest inputs")
    for filename, record in inputs.items():
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("bytes"), int)
            or isinstance(record.get("bytes"), bool)
            or record["bytes"] <= 0
            or not isinstance(record.get("sha256"), str)
            or _SHA256.fullmatch(record["sha256"]) is None
        ):
            raise ValueError(f"Kubernetes profile evidence has an invalid input record: {filename}")
    names = {item.get("name") for item in checks if isinstance(item, dict)}
    if (
        len(checks) != len(_KUBERNETES_PROFILE_CHECKS)
        or names != _KUBERNETES_PROFILE_CHECKS
        or any(not isinstance(item, dict) or item.get("status") != "pass" for item in checks)
    ):
        raise ValueError("Kubernetes profile evidence does not contain all passing checks")
    return value


def build_manifest(
    *,
    image_metadata: Path,
    oci_evidence: Path,
    pip_audit: Path,
    trivy: Path,
    gitleaks: Path,
    signature_evidence: Path | None = None,
    signature_source: Path | None = None,
    registry_rescan_evidence: Path | None = None,
    registry_rescan_source: Path | None = None,
    deployment_acceptance: Path | None = None,
    pitr_manifest: Path | None = None,
    pitr_evidence: Path | None = None,
    pitr_verified: Path | None = None,
    kubernetes_profile_evidence: Path | None = None,
) -> dict[str, Any]:
    if (signature_evidence is None) != (signature_source is None):
        raise ValueError("signature evidence and raw cosign output must be supplied together")
    if (registry_rescan_evidence is None) != (registry_rescan_source is None):
        raise ValueError(
            "registry rescan evidence and raw scanner output must be supplied together"
        )
    pitr_paths = (pitr_manifest, pitr_evidence, pitr_verified)
    if any(path is not None for path in pitr_paths) and not all(
        path is not None for path in pitr_paths
    ):
        raise ValueError(
            "PITR manifest, recovery report, and verified output must be supplied together"
        )
    image = _image(_read(image_metadata))
    attestations = _read(oci_evidence)
    if not isinstance(attestations, dict):
        raise ValueError("OCI evidence must be an object")
    predicates = attestations.get("predicates")
    if not isinstance(predicates, list) or not {
        "https://spdx.dev/Document",
        "https://slsa.dev/provenance/v1",
    }.issubset(predicates):
        raise ValueError("OCI evidence is missing SPDX or SLSA predicates")
    if attestations.get("image_digest") is None:
        raise ValueError("OCI evidence has no image digest")
    oci_labels = attestations.get("image_labels")
    if not isinstance(oci_labels, dict):
        raise ValueError("OCI evidence has no image labels")
    if {name: oci_labels.get(name) for name in REQUIRED_LABELS} != image["labels"]:
        raise ValueError("OCI attestation labels do not match the inspected image")

    security = {
        "pip_audit": _pip_audit(_read(pip_audit)),
        "trivy": _trivy(_read(trivy)),
        "gitleaks": _gitleaks(_read(gitleaks)),
    }
    if security["pip_audit"]["vulnerabilities"]:
        raise ValueError("pip-audit reported vulnerabilities")
    if security["trivy"]["findings"]:
        raise ValueError("Trivy reported vulnerabilities")
    trivy_image_id = security["trivy"]["image_id"]
    if trivy_image_id is not None and trivy_image_id != image["id"]:
        raise ValueError("Trivy report does not describe the inspected image")
    if security["gitleaks"]["findings"]:
        raise ValueError("Gitleaks reported findings")
    signature: dict[str, Any] | None = None
    signature_source_record: dict[str, int | str] | None = None
    if signature_evidence is not None and signature_source is not None:
        try:
            raw_bytes = signature_source.read_bytes()
        except OSError as exc:
            raise ValueError(f"cannot read raw cosign output {signature_source}: {exc}") from exc
        signature = _signature(
            _read(signature_evidence),
            image_digest=attestations["image_digest"],
            source=signature_source,
            source_bytes=raw_bytes,
        )
        signature_source_record = _file_digest(signature_source)
    registry_rescan: dict[str, Any] | None = None
    registry_source_record: dict[str, int | str] | None = None
    if registry_rescan_evidence is not None and registry_rescan_source is not None:
        try:
            raw_registry_bytes = registry_rescan_source.read_bytes()
        except OSError as exc:
            raise ValueError(
                f"cannot read raw registry scanner output {registry_rescan_source}: {exc}"
            ) from exc
        registry_rescan = _registry_rescan(
            _read(registry_rescan_evidence),
            image_digest=attestations["image_digest"],
            source=registry_rescan_source,
            source_bytes=raw_registry_bytes,
        )
        registry_source_record = _file_digest(registry_rescan_source)
    deployment: dict[str, Any] | None = None
    if deployment_acceptance is not None:
        record = _read(deployment_acceptance)
        if not isinstance(record, dict):
            raise ValueError("deployment acceptance record must be an object")
        deployment = _evaluate_deployment_acceptance(record)
        if deployment["decision"] != "pass":
            raise ValueError("deployment acceptance has not passed all required checks")
        if deployment["image_digest"] != attestations["image_digest"]:
            raise ValueError("deployment acceptance does not match the attested image")
    pitr: dict[str, Any] | None = None
    if pitr_manifest is not None and pitr_evidence is not None and pitr_verified is not None:
        pitr = _pitr_profile(pitr_manifest, pitr_evidence, pitr_verified)
    kubernetes_profile: dict[str, Any] | None = None
    if kubernetes_profile_evidence is not None:
        kubernetes_profile = _kubernetes_profile(
            _read(kubernetes_profile_evidence), image_digest=attestations["image_digest"]
        )
    promotion_gates = {name: {"status": "open"} for name in PROMOTION_GATES}
    unresolved_gates = list(PROMOTION_GATES)
    if signature is not None:
        promotion_gates["image_signature"] = {"status": "passed"}
        unresolved_gates.remove("image_signature")
    if registry_rescan is not None:
        promotion_gates["target_registry_rescan"] = {"status": "passed"}
        unresolved_gates.remove("target_registry_rescan")
    if deployment is not None:
        promotion_gates["target_environment_deployment"] = {"status": "passed"}
        unresolved_gates.remove("target_environment_deployment")
    evidence_files = {
        "image_metadata": _file_digest(image_metadata),
        "oci_evidence": _file_digest(oci_evidence),
        "pip_audit": _file_digest(pip_audit),
        "trivy": _file_digest(trivy),
        "gitleaks": _file_digest(gitleaks),
    }
    if signature_evidence is not None:
        evidence_files["signature_evidence"] = _file_digest(signature_evidence)
    if signature_source_record is not None:
        evidence_files["signature_source"] = signature_source_record
    if registry_rescan_evidence is not None:
        evidence_files["registry_rescan_evidence"] = _file_digest(registry_rescan_evidence)
    if registry_source_record is not None:
        evidence_files["registry_rescan_source"] = registry_source_record
    if deployment_acceptance is not None:
        evidence_files["deployment_acceptance"] = _file_digest(deployment_acceptance)
    if pitr is not None:
        evidence_files.update(
            {
                "pitr_manifest": pitr["files"]["manifest"],
                "pitr_evidence": pitr["files"]["evidence"],
                "pitr_verified": pitr["files"]["verified"],
            }
        )
    if kubernetes_profile_evidence is not None:
        evidence_files["kubernetes_profile_evidence"] = _file_digest(kubernetes_profile_evidence)
    result: dict[str, Any] = {
        "schema_version": 1,
        "status": "pass",
        "image": image,
        "build": {
            "source_revision": image["labels"]["org.opencontainers.image.revision"],
            "aftercare_version": image["labels"]["org.opencontainers.image.version"],
            "egm_git_sha": image["labels"]["io.aftercare.egm.git-sha"],
            "aftercare_migration": image["labels"]["io.aftercare.schema.aftercare-migration"],
            "egm_schema": image["labels"]["io.aftercare.schema.egm"],
        },
        "attestations": attestations,
        "security": security,
        "evidence_files": evidence_files,
        "promotion_gates": promotion_gates,
        "unresolved_gates": unresolved_gates,
    }
    if signature is not None:
        result["signature"] = signature
    if registry_rescan is not None:
        result["registry_rescan"] = registry_rescan
    if deployment is not None:
        result["deployment_acceptance"] = deployment
    if pitr is not None:
        result["pitr_profile"] = {key: value for key, value in pitr.items() if key != "files"}
    if kubernetes_profile is not None:
        result["kubernetes_profile"] = kubernetes_profile
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("image-metadata", "oci-evidence", "pip-audit", "trivy", "gitleaks"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--signature-evidence", type=Path)
    parser.add_argument("--signature-source", type=Path)
    parser.add_argument("--registry-rescan-evidence", type=Path)
    parser.add_argument("--registry-rescan-source", type=Path)
    parser.add_argument("--deployment-acceptance", type=Path)
    parser.add_argument("--pitr-manifest", type=Path)
    parser.add_argument("--pitr-evidence", type=Path)
    parser.add_argument("--pitr-verified", type=Path)
    parser.add_argument("--kubernetes-profile-evidence", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = build_manifest(
        image_metadata=args.image_metadata,
        oci_evidence=args.oci_evidence,
        pip_audit=args.pip_audit,
        trivy=args.trivy,
        gitleaks=args.gitleaks,
        signature_evidence=args.signature_evidence,
        signature_source=args.signature_source,
        registry_rescan_evidence=args.registry_rescan_evidence,
        registry_rescan_source=args.registry_rescan_source,
        deployment_acceptance=args.deployment_acceptance,
        pitr_manifest=args.pitr_manifest,
        pitr_evidence=args.pitr_evidence,
        pitr_verified=args.pitr_verified,
        kubernetes_profile_evidence=args.kubernetes_profile_evidence,
    )
    args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(manifest, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
