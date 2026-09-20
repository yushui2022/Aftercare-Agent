"""Verify the rendered Kubernetes reference profile without contacting a cluster."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

_IMAGE = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
_FILES = ("reference.yaml", "migration-job.yaml", "rls-check-job.yaml")


def _documents(path: Path) -> list[dict[str, Any]]:
    try:
        values = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read Kubernetes manifest {path}: {exc}") from exc
    if not values or any(not isinstance(value, dict) for value in values):
        raise ValueError(f"Kubernetes manifest {path} must contain object documents")
    return values


def _file_record(path: Path) -> dict[str, int | str]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read Kubernetes manifest {path}: {exc}") from exc
    return {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}


def _env_value(container: dict[str, Any], name: str) -> str | None:
    for item in container.get("env", []):
        if isinstance(item, dict) and item.get("name") == name:
            value = item.get("value")
            return value if isinstance(value, str) else None
    return None


def _workloads(documents: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    workloads: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for document in documents:
        if document.get("kind") not in {"Deployment", "Job"}:
            continue
        template = document.get("spec", {}).get("template", {})
        spec = template.get("spec", {})
        containers = spec.get("containers", [])
        if not isinstance(containers, list) or not containers:
            raise ValueError(f"{document.get('metadata', {}).get('name')} has no containers")
        for container in containers:
            if not isinstance(container, dict):
                raise ValueError("Kubernetes container entry must be an object")
            workloads.append((document, container))
    return workloads


def _secret_names(document: dict[str, Any]) -> set[str]:
    spec = document.get("spec", {}).get("template", {}).get("spec", {})
    names: set[str] = set()
    for volume in spec.get("volumes", []):
        if not isinstance(volume, dict):
            continue
        secret = volume.get("secret")
        if isinstance(secret, dict) and isinstance(secret.get("secretName"), str):
            names.add(secret["secretName"])
    return names


def _secret_modes(document: dict[str, Any]) -> list[int]:
    spec = document.get("spec", {}).get("template", {}).get("spec", {})
    modes: list[int] = []
    for volume in spec.get("volumes", []):
        if not isinstance(volume, dict):
            continue
        secret = volume.get("secret")
        if isinstance(secret, dict) and "secretName" in secret:
            mode = secret.get("defaultMode")
            modes.append(mode if isinstance(mode, int) and not isinstance(mode, bool) else -1)
    return modes


def verify_profile(
    directory: Path,
    *,
    image: str,
    issuer: str,
    audience: str,
    jwks_url: str,
    tenant_id: str,
) -> dict[str, Any]:
    errors: list[str] = []
    checks: list[dict[str, Any]] = []
    if not _IMAGE.fullmatch(image):
        errors.append("image must use an immutable sha256 digest")
    if not tenant_id or tenant_id == "replace-with-tenant-id":
        errors.append("tenant_id must be a concrete tenant value")
    for value, name in ((issuer, "issuer"), (jwks_url, "jwks_url")):
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.netloc:
            errors.append(f"{name} must be an HTTPS URL")
    if not audience:
        errors.append("audience must not be empty")

    loaded: list[dict[str, Any]] = []
    inputs: dict[str, dict[str, int | str]] = {}
    for filename in _FILES:
        try:
            path = directory / filename
            loaded.extend(_documents(path))
            inputs[filename] = _file_record(path)
        except ValueError as exc:
            errors.append(str(exc))
    by_name = {document.get("metadata", {}).get("name"): document for document in loaded}
    workloads: list[tuple[dict[str, Any], dict[str, Any]]] = []
    if not errors or loaded:
        try:
            workloads = _workloads(loaded)
        except ValueError as exc:
            errors.append(str(exc))

    expected_workloads = {
        "aftercare-api",
        "aftercare-worker",
        "aftercare-migrate",
        "aftercare-rls-check",
    }
    actual_workloads = {document.get("metadata", {}).get("name") for document, _ in workloads}
    if actual_workloads != expected_workloads:
        errors.append("Kubernetes profile does not contain the fixed API/Worker/Job workload set")
    if workloads and all(container.get("image") == image for _, container in workloads):
        checks.append({"name": "image_consistency", "status": "pass"})
    else:
        errors.append("every Kubernetes workload must use the supplied immutable image")
    for document, container in workloads:
        if document.get("kind") == "Job" and _env_value(container, "AFTERCARE_IMAGE") != image:
            errors.append(
                f"{document.get('metadata', {}).get('name')} AFTERCARE_IMAGE must match its image"
            )

    config = by_name.get("aftercare-runtime-config")
    data = config.get("data", {}) if isinstance(config, dict) else {}
    expected_config = {
        "AFTERCARE_OIDC_ISSUER": issuer,
        "AFTERCARE_OIDC_AUDIENCE": audience,
        "AFTERCARE_OIDC_JWKS_URL": jwks_url,
        "AFTERCARE_TENANT_ID": tenant_id,
    }
    if all(data.get(name) == value for name, value in expected_config.items()):
        checks.append({"name": "runtime_config", "status": "pass"})
    else:
        errors.append("runtime ConfigMap does not match the supplied OIDC and tenant values")

    security_ok = True
    namespace = next(
        (
            document
            for document in loaded
            if document.get("kind") == "Namespace"
            and document.get("metadata", {}).get("name") == "aftercare"
        ),
        None,
    )
    namespace_labels = (
        namespace.get("metadata", {}).get("labels", {}) if isinstance(namespace, dict) else {}
    )
    service_account = next(
        (
            document
            for document in loaded
            if document.get("kind") == "ServiceAccount"
            and document.get("metadata", {}).get("name") == "aftercare"
        ),
        None,
    )
    api_service = next(
        (
            document
            for document in loaded
            if document.get("kind") == "Service"
            and document.get("metadata", {}).get("name") == "aftercare-api"
        ),
        None,
    )
    service_spec = api_service.get("spec", {}) if isinstance(api_service, dict) else {}
    service_ports = service_spec.get("ports", []) if isinstance(service_spec, dict) else []
    service_ok = (
        isinstance(service_spec, dict)
        and service_spec.get("type") == "ClusterIP"
        and isinstance(service_ports, list)
        and len(service_ports) == 1
        and isinstance(service_ports[0], dict)
        and service_ports[0].get("name") == "http"
        and service_ports[0].get("port") == 80
        and service_ports[0].get("targetPort") == "http"
        and service_spec.get("selector") == {"app.kubernetes.io/name": "aftercare-api"}
    )
    service_account_ok = (
        isinstance(service_account, dict)
        and service_account.get("kind") == "ServiceAccount"
        and service_account.get("automountServiceAccountToken") is False
    )
    namespace_ok = (
        isinstance(namespace_labels, dict)
        and namespace_labels.get("pod-security.kubernetes.io/enforce") == "restricted"
        and namespace_labels.get("pod-security.kubernetes.io/audit") == "restricted"
        and namespace_labels.get("pod-security.kubernetes.io/warn") == "restricted"
    )
    for document, container in workloads:
        spec = document["spec"]["template"]["spec"]
        pod_security = spec.get("securityContext", {})
        container_security = container.get("securityContext", {})
        if not (
            spec.get("automountServiceAccountToken") is False
            and pod_security.get("runAsNonRoot") is True
            and pod_security.get("runAsUser") == 10001
            and pod_security.get("runAsGroup") == 10001
            and pod_security.get("seccompProfile", {}).get("type") == "RuntimeDefault"
            and container_security.get("allowPrivilegeEscalation") is False
            and container_security.get("readOnlyRootFilesystem") is True
            and "ALL" in container_security.get("capabilities", {}).get("drop", [])
            and spec.get("serviceAccountName") == "aftercare"
        ):
            security_ok = False
    if security_ok and namespace_ok and service_account_ok and service_ok and workloads:
        checks.append({"name": "security_baseline", "status": "pass"})
    else:
        errors.append(
            "namespace, ServiceAccount, Service and workloads must retain the security baseline"
        )

    database_boundary_ok = True
    expected_secrets = {
        "aftercare-migrate": {"aftercare-migration-database-url"},
        "aftercare-rls-check": {"aftercare-runtime-database-url"},
        "aftercare-api": {"aftercare-runtime-database-url"},
        "aftercare-worker": {"aftercare-runtime-database-url"},
    }
    for document, container in workloads:
        name = document.get("metadata", {}).get("name")
        if (
            name not in expected_secrets
            or _secret_names(document) != expected_secrets[name]
            or _secret_modes(document) != [256]
            or _env_value(container, "DATABASE_URL_FILE") != "/run/secrets/aftercare_database_url"
            or _env_value(container, "AFTERCARE_REQUIRE_DATABASE_TLS") != "1"
        ):
            database_boundary_ok = False
        if (
            name in {"aftercare-api", "aftercare-worker"}
            and _env_value(container, "AFTERCARE_AUTO_MIGRATE") != "0"
        ):
            database_boundary_ok = False
        if (
            name == "aftercare-worker"
            and _env_value(container, "AFTERCARE_WORKER_REQUIRE_TENANT") != "1"
        ):
            database_boundary_ok = False
    if database_boundary_ok and workloads:
        checks.append({"name": "database_role_boundary", "status": "pass"})
    else:
        errors.append(
            "migration and runtime workloads must retain separate database identities and TLS"
        )

    job_boundary_ok = all(
        document.get("spec", {}).get("backoffLimit") == 0
        and document.get("spec", {}).get("template", {}).get("spec", {}).get("restartPolicy")
        == "Never"
        for document, _ in workloads
        if document.get("kind") == "Job"
    )
    if job_boundary_ok and workloads:
        checks.append({"name": "job_execution_boundary", "status": "pass"})
    else:
        errors.append("migration and RLS Jobs must be one-shot and fail without automatic replay")

    operability_ok = True
    for document, container in workloads:
        name = document.get("metadata", {}).get("name")
        resources = container.get("resources", {})
        requests = resources.get("requests", {}) if isinstance(resources, dict) else {}
        limits = resources.get("limits", {}) if isinstance(resources, dict) else {}
        if not all(
            isinstance(values, dict)
            and all(isinstance(values.get(key), str) and values[key] for key in ("cpu", "memory"))
            for values in (requests, limits)
        ):
            operability_ok = False
        if name in {"aftercare-api", "aftercare-worker"}:
            grace = (
                document.get("spec", {})
                .get("template", {})
                .get("spec", {})
                .get("terminationGracePeriodSeconds")
            )
            if not isinstance(grace, int) or grace <= 0:
                operability_ok = False
        if name == "aftercare-api":
            probes = {
                probe: container.get(probe, {})
                for probe in ("readinessProbe", "startupProbe", "livenessProbe")
            }
            expected_paths = {
                "readinessProbe": "/readyz",
                "startupProbe": "/readyz",
                "livenessProbe": "/healthz",
            }
            for probe, expected_path in expected_paths.items():
                probe_value = probes[probe]
                http_get = probe_value.get("httpGet", {}) if isinstance(probe_value, dict) else {}
                if not isinstance(http_get, dict):
                    http_get = {}
                if http_get.get("path") != expected_path or http_get.get("port") != "http":
                    operability_ok = False
    if operability_ok and workloads:
        checks.append({"name": "runtime_operability", "status": "pass"})
    else:
        errors.append("workloads must define resources, graceful termination, and API probes")

    return {
        "schema_version": 1,
        "verifier": "aftercare-kubernetes-profile",
        "image_digest": image.split("@", 1)[1] if "@" in image else None,
        "status": "fail" if errors else "pass",
        "checks": checks,
        "errors": errors,
        "inputs": inputs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--issuer", required=True)
    parser.add_argument("--audience", required=True)
    parser.add_argument("--jwks-url", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = verify_profile(
            args.directory,
            image=args.image,
            issuer=args.issuer,
            audience=args.audience,
            jwks_url=args.jwks_url,
            tenant_id=args.tenant_id,
        )
    except ValueError as exc:
        report = {
            "schema_version": 1,
            "verifier": "aftercare-kubernetes-profile",
            "image_digest": None,
            "status": "fail",
            "checks": [],
            "errors": [str(exc)],
            "inputs": {},
        }
    serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
