from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1] / "deploy" / "kubernetes"


def _documents(name: str) -> list[dict[str, Any]]:
    values = list(yaml.safe_load_all((ROOT / name).read_text(encoding="utf-8")))
    assert all(isinstance(value, dict) for value in values)
    return values


def test_kubernetes_reference_has_the_expected_workload_boundaries() -> None:
    documents = _documents("reference.yaml")

    assert [document["kind"] for document in documents] == [
        "Namespace",
        "ServiceAccount",
        "ConfigMap",
        "Service",
        "Deployment",
        "Deployment",
    ]
    namespace = documents[0]
    assert namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"
    config = next(document for document in documents if document["kind"] == "ConfigMap")
    assert config["data"]["AFTERCARE_TENANT_ID"] == "replace-with-tenant-id"
    for deployment in documents:
        if deployment["kind"] != "Deployment":
            continue
        pod = deployment["spec"]["template"]["spec"]
        container = pod["containers"][0]
        assert pod["automountServiceAccountToken"] is False
        assert pod["securityContext"]["runAsUser"] == 10001
        assert container["securityContext"]["readOnlyRootFilesystem"] is True
        assert container["securityContext"]["allowPrivilegeEscalation"] is False
        assert container["image"].endswith("@sha256:replace-me")
        if deployment["metadata"]["name"] == "aftercare-api":
            assert container["startupProbe"]["httpGet"]["path"] == "/readyz"
            assert container["livenessProbe"]["httpGet"]["path"] == "/healthz"


def test_kubernetes_migration_job_is_one_shot_and_uses_a_separate_secret() -> None:
    document = _documents("migration-job.yaml")[0]

    assert document["kind"] == "Job"
    assert document["spec"]["backoffLimit"] == 0
    pod = document["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert container["command"] == ["/bin/sh", "-ec"]
    assert "aftercare-migrate" in container["args"][0]
    assert "aftercare-rls harden" in container["args"][0]
    assert (
        next(item for item in container["env"] if item["name"] == "AFTERCARE_IMAGE")["value"]
        == container["image"]
    )
    assert pod["volumes"][0]["secret"]["secretName"] == "aftercare-migration-database-url"


def test_kubernetes_migration_and_runtime_use_one_image_reference() -> None:
    reference_images = {
        document["spec"]["template"]["spec"]["containers"][0]["image"]
        for document in _documents("reference.yaml")
        if document["kind"] == "Deployment"
    }
    migration = _documents("migration-job.yaml")[0]
    migration_image = migration["spec"]["template"]["spec"]["containers"][0]["image"]

    check = _documents("rls-check-job.yaml")[0]
    check_container = check["spec"]["template"]["spec"]["containers"][0]
    assert reference_images == {migration_image, check_container["image"]}
    assert check_container["command"] == ["/bin/sh", "-ec"]
    assert "aftercare-rls verify" in check_container["args"][0]
    assert (
        next(item for item in check_container["env"] if item["name"] == "AFTERCARE_IMAGE")["value"]
        == check_container["image"]
    )
    assert check_container["securityContext"]["readOnlyRootFilesystem"] is True
    assert (
        check["spec"]["template"]["spec"]["volumes"][0]["secret"]["secretName"]
        == "aftercare-runtime-database-url"
    )
