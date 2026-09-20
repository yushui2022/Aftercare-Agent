from __future__ import annotations

import pytest

from aftercare_agent.persistence.rls import image_digest_from_reference, normalize_image_digest


def test_rls_cli_extracts_the_digest_from_the_workload_image() -> None:
    digest = "sha256:" + "a" * 64

    assert image_digest_from_reference(f"registry.example/aftercare@{digest}") == digest


@pytest.mark.parametrize(
    "value",
    [
        "sha256:" + "A" * 64,
        "sha256:" + "a" * 63,
        "sha256:" + "a" * 65,
        "latest",
        "@sha256:" + "a" * 64,
    ],
)
def test_rls_cli_rejects_non_immutable_image_identity(value: str) -> None:
    with pytest.raises(ValueError):
        if "@" in value:
            image_digest_from_reference(value)
        else:
            normalize_image_digest(value)
