"""Normalize a target-registry Trivy scan for release promotion evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

_REFERENCE = re.compile(r"^([^\s]+)@(sha256:[0-9a-f]{64})$")


def _read(path: Path) -> tuple[Any, bytes]:
    try:
        payload = path.read_bytes()
        return json.loads(payload), payload
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read registry scan JSON {path}: {exc}") from exc


def normalize_registry_scan(
    value: Any, *, image_reference: str, source_bytes: bytes
) -> dict[str, Any]:
    """Require a clean scan for one immutable registry reference."""
    match = _REFERENCE.fullmatch(image_reference)
    if match is None:
        raise ValueError("image reference must include an immutable sha256 digest")
    image_digest = match.group(2)
    if not isinstance(value, dict):
        raise ValueError("Trivy registry report must be an object")
    if value.get("ArtifactType") != "container_image":
        raise ValueError("Trivy registry report is not for a container image")
    artifact_name = value.get("ArtifactName")
    metadata = value.get("Metadata")
    repo_digests = metadata.get("RepoDigests", []) if isinstance(metadata, dict) else []
    if artifact_name != image_reference and (
        not isinstance(repo_digests, list) or image_reference not in repo_digests
    ):
        raise ValueError("Trivy registry report does not match the immutable image reference")
    results = value.get("Results", [])
    if not isinstance(results, list):
        raise ValueError("Trivy registry report has an invalid Results value")
    findings = 0
    severities: dict[str, int] = {}
    for result in results:
        if not isinstance(result, dict):
            raise ValueError("Trivy registry result is malformed")
        vulnerabilities = result.get("Vulnerabilities") or []
        if not isinstance(vulnerabilities, list):
            raise ValueError("Trivy registry vulnerabilities value is malformed")
        findings += len(vulnerabilities)
        for vulnerability in vulnerabilities:
            if not isinstance(vulnerability, dict):
                raise ValueError("Trivy registry vulnerability is malformed")
            severity = vulnerability.get("Severity", "UNKNOWN")
            if not isinstance(severity, str):
                raise ValueError("Trivy registry vulnerability severity is malformed")
            severities[severity] = severities.get(severity, 0) + 1
    if findings:
        raise ValueError("target registry scan reported vulnerabilities")
    return {
        "schema_version": 1,
        "verifier": "trivy-registry",
        "verified": True,
        "image_reference": image_reference,
        "image_digest": image_digest,
        "findings": findings,
        "severities": dict(sorted(severities.items())),
        "source": {
            "bytes": len(source_bytes),
            "sha256": hashlib.sha256(source_bytes).hexdigest(),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Trivy JSON report")
    parser.add_argument("--image-reference", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value, source_bytes = _read(args.input)
    try:
        evidence = normalize_registry_scan(
            value,
            image_reference=args.image_reference,
            source_bytes=source_bytes,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
