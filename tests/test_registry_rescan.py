from __future__ import annotations

import hashlib

import pytest

from deploy.verify_registry_rescan import normalize_registry_scan  # noqa: E402

IMAGE_DIGEST = "sha256:" + "d" * 64
IMAGE_REFERENCE = f"registry.example/aftercare-agent@{IMAGE_DIGEST}"


def _report(
    *, name: str = IMAGE_REFERENCE, vulnerabilities: list[object] | None = None
) -> dict[str, object]:
    return {
        "ArtifactName": name,
        "ArtifactType": "container_image",
        "Metadata": {"RepoDigests": [name]},
        "Results": [{"Vulnerabilities": vulnerabilities or []}],
    }


def test_registry_rescan_binds_clean_report_to_immutable_reference() -> None:
    source = b'{"scan":"clean"}\n'
    evidence = normalize_registry_scan(
        _report(), image_reference=IMAGE_REFERENCE, source_bytes=source
    )

    assert evidence["verified"] is True
    assert evidence["image_digest"] == IMAGE_DIGEST
    assert evidence["findings"] == 0
    assert evidence["source"] == {
        "bytes": len(source),
        "sha256": hashlib.sha256(source).hexdigest(),
    }


def test_registry_rescan_rejects_mutable_reference() -> None:
    with pytest.raises(ValueError, match="immutable"):
        normalize_registry_scan(
            _report(),
            image_reference="registry.example/aftercare-agent:latest",
            source_bytes=b"raw",
        )


def test_registry_rescan_rejects_other_image() -> None:
    with pytest.raises(ValueError, match="does not match"):
        normalize_registry_scan(
            _report(name="registry.example/other@" + IMAGE_DIGEST),
            image_reference=IMAGE_REFERENCE,
            source_bytes=b"raw",
        )


def test_registry_rescan_rejects_vulnerabilities() -> None:
    with pytest.raises(ValueError, match="reported vulnerabilities"):
        normalize_registry_scan(
            _report(vulnerabilities=[{"Severity": "HIGH"}]),
            image_reference=IMAGE_REFERENCE,
            source_bytes=b"raw",
        )
