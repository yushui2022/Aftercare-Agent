"""Verify SBOM and provenance attestations in a BuildKit OCI export."""

from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
from pathlib import Path
from typing import Any

IDENTITY_LABELS = (
    "org.opencontainers.image.source",
    "org.opencontainers.image.licenses",
    "org.opencontainers.image.version",
    "org.opencontainers.image.revision",
    "io.aftercare.egm.git-sha",
    "io.aftercare.schema.aftercare-migration",
    "io.aftercare.schema.egm",
)


def _json_member(archive: tarfile.TarFile, name: str) -> dict[str, Any]:
    try:
        member = archive.extractfile(name)
    except KeyError as exc:
        raise ValueError(f"OCI archive is missing {name}") from exc
    if member is None:
        raise ValueError(f"OCI archive member {name} is not a regular file")
    value = json.load(member)
    if not isinstance(value, dict):
        raise ValueError(f"OCI archive member {name} is not a JSON object")
    return value


def _blob(archive: tarfile.TarFile, digest: str) -> bytes:
    algorithm, separator, value = digest.partition(":")
    if separator != ":" or algorithm != "sha256" or len(value) != 64:
        raise ValueError(f"unsupported OCI digest: {digest!r}")
    name = f"blobs/sha256/{value}"
    try:
        member = archive.extractfile(name)
    except KeyError as exc:
        raise ValueError(f"OCI archive is missing blob {digest}") from exc
    if member is None:
        raise ValueError(f"OCI archive blob {digest} is not a regular file")
    payload = member.read()
    if hashlib.sha256(payload).hexdigest() != value:
        raise ValueError(f"OCI blob digest mismatch: {digest}")
    return payload


def _blob_json(archive: tarfile.TarFile, digest: str) -> dict[str, Any]:
    try:
        value = json.loads(_blob(archive, digest))
    except json.JSONDecodeError as exc:
        raise ValueError(f"OCI blob {digest} is not JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"OCI blob {digest} is not a JSON object")
    return value


def verify_attestations(path: Path) -> dict[str, Any]:
    """Return a compact evidence record or raise ``ValueError``."""
    with tarfile.open(path, mode="r") as archive:
        outer = _json_member(archive, "index.json")
        outer_manifests = outer.get("manifests")
        if not isinstance(outer_manifests, list) or len(outer_manifests) != 1:
            raise ValueError("OCI index must point to exactly one image index")
        outer_index = outer_manifests[0]
        if not isinstance(outer_index, dict):
            raise ValueError("OCI index descriptor is malformed")
        index_digest = outer_index.get("digest")
        if not isinstance(index_digest, str):
            raise ValueError("OCI index descriptor has no digest")
        image_index = _blob_json(archive, index_digest)
        manifests = image_index.get("manifests")
        if not isinstance(manifests, list):
            raise ValueError("OCI image index has no manifest list")

        image_manifests = [
            item
            for item in manifests
            if isinstance(item, dict)
            and item.get("annotations", {}).get("vnd.docker.reference.type")
            != "attestation-manifest"
        ]
        attestation_manifests = [
            item
            for item in manifests
            if isinstance(item, dict)
            and item.get("annotations", {}).get("vnd.docker.reference.type")
            == "attestation-manifest"
        ]
        if len(image_manifests) != 1 or len(attestation_manifests) != 1:
            raise ValueError("OCI image index must contain one image and one attestation manifest")
        image = image_manifests[0]
        attestation = attestation_manifests[0]
        image_digest = image.get("digest")
        attestation_digest = attestation.get("digest")
        if not isinstance(image_digest, str) or not isinstance(attestation_digest, str):
            raise ValueError("OCI image descriptors are missing digests")
        image_manifest = _blob_json(archive, image_digest)
        config_descriptor = image_manifest.get("config")
        if not isinstance(config_descriptor, dict):
            raise ValueError("OCI image manifest has no config descriptor")
        config_digest = config_descriptor.get("digest")
        if not isinstance(config_digest, str):
            raise ValueError("OCI image config descriptor has no digest")
        image_config = _blob_json(archive, config_digest)
        config = image_config.get("config")
        labels = config.get("Labels") if isinstance(config, dict) else None
        if not isinstance(labels, dict):
            raise ValueError("OCI image config has no labels")
        missing_labels = [
            name
            for name in IDENTITY_LABELS
            if not isinstance(labels.get(name), str) or not labels[name]
        ]
        if missing_labels:
            raise ValueError(f"OCI image config is missing required labels: {missing_labels}")
        attestation_manifest = _blob_json(archive, attestation_digest)
        subject = attestation_manifest.get("subject")
        if not isinstance(subject, dict) or subject.get("digest") != image_digest:
            raise ValueError("attestation subject does not match the image manifest")

        predicates: set[str] = set()
        layer_bytes: dict[str, int] = {}
        for layer in attestation_manifest.get("layers", []):
            if not isinstance(layer, dict):
                raise ValueError("attestation layer descriptor is malformed")
            layer_digest = layer.get("digest")
            if not isinstance(layer_digest, str):
                raise ValueError("attestation layer has no digest")
            layer_bytes[layer_digest] = len(_blob(archive, layer_digest))
            predicate = layer.get("annotations", {}).get("in-toto.io/predicate-type")
            if isinstance(predicate, str):
                predicates.add(predicate)
        required = {"https://spdx.dev/Document", "https://slsa.dev/provenance/v1"}
        missing = required - predicates
        if missing:
            raise ValueError(f"required attestation predicates are missing: {sorted(missing)}")
        return {
            "image_digest": image_digest,
            "image_config_digest": config_digest,
            "image_labels": {name: labels[name] for name in sorted(IDENTITY_LABELS)},
            "attestation_digest": attestation_digest,
            "predicates": sorted(predicates),
            "layer_bytes": layer_bytes,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("oci_archive", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify_attestations(args.oci_archive), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
