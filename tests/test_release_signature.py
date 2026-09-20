from __future__ import annotations

import hashlib

import pytest

from deploy.verify_cosign_evidence import normalize_cosign_output  # noqa: E402

IMAGE_DIGEST = "sha256:" + "d" * 64
ISSUER = "https://token.actions.githubusercontent.com"
SUBJECT = "repo:yushui2022/Aftercare-Agent:ref:refs/heads/main"


def _raw(*, digest: str = IMAGE_DIGEST, subject: str = SUBJECT) -> list[dict[str, object]]:
    return [
        {
            "critical": {
                "type": "cosign container image signature",
                "image": {"docker-manifest-digest": digest},
            },
            "optional": {"Issuer": ISSUER, "Subject": subject},
        }
    ]


def test_cosign_evidence_binds_digest_and_identity() -> None:
    source = b'{"verified":true}\n'
    evidence = normalize_cosign_output(
        _raw(),
        image_digest=IMAGE_DIGEST,
        issuer=ISSUER,
        subject_regexp=r"repo:yushui2022/Aftercare-Agent:ref:refs/heads/main",
        source_bytes=source,
    )

    assert evidence["verified"] is True
    assert evidence["image_digest"] == IMAGE_DIGEST
    assert evidence["signature_count"] == 1
    assert evidence["source"] == {
        "bytes": len(source),
        "sha256": hashlib.sha256(source).hexdigest(),
    }


def test_cosign_evidence_rejects_other_manifest() -> None:
    with pytest.raises(ValueError, match="does not match"):
        normalize_cosign_output(
            _raw(digest="sha256:" + "e" * 64),
            image_digest=IMAGE_DIGEST,
            issuer=ISSUER,
            subject_regexp=r"repo:.*",
            source_bytes=b"raw",
        )


def test_cosign_evidence_rejects_other_workflow_identity() -> None:
    with pytest.raises(ValueError, match="certificate identity"):
        normalize_cosign_output(
            _raw(subject="repo:someone-else/Aftercare-Agent:ref:refs/heads/main"),
            image_digest=IMAGE_DIGEST,
            issuer=ISSUER,
            subject_regexp=r"repo:yushui2022/Aftercare-Agent:ref:refs/heads/main",
            source_bytes=b"raw",
        )


def test_cosign_evidence_rejects_missing_certificate_identity() -> None:
    raw = _raw()
    del raw[0]["optional"]
    with pytest.raises(ValueError, match="no certificate identity"):
        normalize_cosign_output(
            raw,
            image_digest=IMAGE_DIGEST,
            issuer=ISSUER,
            subject_regexp=r"repo:.*",
            source_bytes=b"raw",
        )
