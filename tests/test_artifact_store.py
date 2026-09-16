from hashlib import sha256
from pathlib import Path

import pytest

from aftercare_agent.artifacts import ContentAddressedArtifactStore
from aftercare_agent.domain.common import CaseScope, ContractViolation, ErrorCode

SCOPE = CaseScope(tenant_id="tenant-1", case_id="case-1")


def test_put_is_addressed_by_digest_and_survives_a_replay(tmp_path: Path) -> None:
    store = ContentAddressedArtifactStore(tmp_path / "artifacts")
    first = store.put(SCOPE, reference_id="tool-result:call-1", content=b'{"a":1}')
    assert first.sha256 == sha256(b'{"a":1}').hexdigest()
    replay = store.put(SCOPE, reference_id="tool-result:call-1", content=b'{"a":1}')
    assert replay == first
    assert store.read(SCOPE, first) == b'{"a":1}'


def test_tampered_content_is_reported_instead_of_returned(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    store = ContentAddressedArtifactStore(root)
    reference = store.put(SCOPE, reference_id="r", content=b"{}")
    (root / "tenant-1" / "case-1" / reference.sha256).write_bytes(b"[]")
    with pytest.raises(ContractViolation) as error:
        store.read(SCOPE, reference)
    assert error.value.code is ErrorCode.CONFLICT


def test_reading_another_cases_artifact_is_refused(tmp_path: Path) -> None:
    store = ContentAddressedArtifactStore(tmp_path / "artifacts")
    reference = store.put(SCOPE, reference_id="r", content=b"{}")
    other = CaseScope(tenant_id="tenant-2", case_id="case-1")
    with pytest.raises(ContractViolation) as error:
        store.read(other, reference)
    assert error.value.code is ErrorCode.FORBIDDEN


def test_scope_identifiers_cannot_escape_the_store_root(tmp_path: Path) -> None:
    store = ContentAddressedArtifactStore(tmp_path / "artifacts")
    escaping = CaseScope(tenant_id="tenant-1/..", case_id="case-1")
    with pytest.raises(ContractViolation) as error:
        store.put(escaping, reference_id="r", content=b"{}")
    assert error.value.code is ErrorCode.INVALID_INPUT
    assert list((tmp_path / "artifacts").iterdir()) == []


def test_size_budget_and_empty_content_are_enforced(tmp_path: Path) -> None:
    store = ContentAddressedArtifactStore(tmp_path / "artifacts", max_bytes=2)
    with pytest.raises(ContractViolation) as error:
        store.put(SCOPE, reference_id="r", content=b"abc")
    assert error.value.code is ErrorCode.BUDGET_EXHAUSTED
    with pytest.raises(ContractViolation) as error:
        store.put(SCOPE, reference_id="r", content=b"")
    assert error.value.code is ErrorCode.INVALID_INPUT


def test_missing_content_is_not_silently_empty(tmp_path: Path) -> None:
    store = ContentAddressedArtifactStore(tmp_path / "artifacts")
    reference = store.put(SCOPE, reference_id="r", content=b"{}")
    (tmp_path / "artifacts" / "tenant-1" / "case-1" / reference.sha256).unlink()
    assert store.holds(SCOPE, reference) is False
    with pytest.raises(ContractViolation) as error:
        store.read(SCOPE, reference)
    assert error.value.code is ErrorCode.CONFLICT
