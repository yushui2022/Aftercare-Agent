"""Normalize and validate output from a successful ``cosign verify`` run.

Cosign performs the cryptographic verification.  This script prevents a valid
signature for a different image or identity from being attached to the
Aftercare release evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

SIGNATURE_TYPE = "cosign container image signature"


def _read(path: Path) -> tuple[Any, bytes]:
    try:
        payload = path.read_bytes()
        return json.loads(payload), payload
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read cosign JSON {path}: {exc}") from exc


def normalize_cosign_output(
    value: Any,
    *,
    image_digest: str,
    issuer: str,
    subject_regexp: str,
    source_bytes: bytes,
) -> dict[str, Any]:
    """Validate cosign's verified JSON output and return stable evidence."""
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest):
        raise ValueError("image digest must be a sha256 digest")
    if not issuer or not subject_regexp:
        raise ValueError("issuer and subject regexp are required")
    try:
        subject_pattern = re.compile(subject_regexp)
    except re.error as exc:
        raise ValueError(f"invalid certificate identity regexp: {exc}") from exc
    if not isinstance(value, list) or not value:
        raise ValueError("cosign output must be a non-empty JSON array")

    identities: set[tuple[str, str]] = set()
    for index, signature in enumerate(value):
        if not isinstance(signature, dict):
            raise ValueError(f"cosign signature {index} is not an object")
        critical = signature.get("critical")
        if not isinstance(critical, dict) or critical.get("type") != SIGNATURE_TYPE:
            raise ValueError(f"cosign signature {index} has an invalid critical type")
        image = critical.get("image")
        if not isinstance(image, dict) or image.get("docker-manifest-digest") != image_digest:
            raise ValueError(f"cosign signature {index} does not match the image digest")
        optional = signature.get("optional")
        if not isinstance(optional, dict):
            raise ValueError(f"cosign signature {index} has no certificate identity")
        signed_issuer = optional.get("Issuer")
        signed_subject = optional.get("Subject")
        if signed_issuer != issuer:
            raise ValueError(f"cosign signature {index} has an unexpected OIDC issuer")
        if not isinstance(signed_subject, str) or subject_pattern.fullmatch(signed_subject) is None:
            raise ValueError(f"cosign signature {index} has an unexpected certificate identity")
        identities.add((signed_issuer, signed_subject))

    return {
        "schema_version": 1,
        "verifier": "cosign",
        "verified": True,
        "image_digest": image_digest,
        "signature_count": len(value),
        "identities": [
            {"issuer": signed_issuer, "subject": signed_subject}
            for signed_issuer, signed_subject in sorted(identities)
        ],
        "source": {
            "bytes": len(source_bytes),
            "sha256": hashlib.sha256(source_bytes).hexdigest(),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", type=Path, required=True, help="cosign verify --output=json file"
    )
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--certificate-oidc-issuer", required=True)
    parser.add_argument("--certificate-identity-regexp", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value, source_bytes = _read(args.input)
    try:
        evidence = normalize_cosign_output(
            value,
            image_digest=args.image_digest,
            issuer=args.certificate_oidc_issuer,
            subject_regexp=args.certificate_identity_regexp,
            source_bytes=source_bytes,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
