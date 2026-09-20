from __future__ import annotations

from typing import Any

import pytest

from deploy.release_policy import evaluate_manifest


def _manifest(*, unresolved: list[str]) -> dict[str, Any]:
    gates = {
        "image_signature": {"status": "open"},
        "target_registry_rescan": {"status": "open"},
        "target_environment_deployment": {"status": "open"},
    }
    for name in gates:
        if name not in unresolved:
            gates[name] = {"status": "passed"}
    return {
        "schema_version": 1,
        "status": "pass",
        "build": {"source_revision": "a" * 40},
        "promotion_gates": gates,
        "unresolved_gates": unresolved,
    }


def test_release_policy_holds_until_all_promotion_gates_are_resolved() -> None:
    decision = evaluate_manifest(
        _manifest(unresolved=["target_environment_deployment", "image_signature"])
    )

    assert decision == {
        "schema_version": 1,
        "decision": "hold",
        "source_revision": "a" * 40,
        "unresolved_gates": ["image_signature", "target_environment_deployment"],
    }


def test_release_policy_promotes_a_fully_verified_manifest() -> None:
    assert evaluate_manifest(_manifest(unresolved=[]))["decision"] == "promote"


def test_release_policy_rejects_unknown_gate() -> None:
    with pytest.raises(ValueError, match="unknown gates"):
        evaluate_manifest(_manifest(unresolved=["manual_exception"]))


def test_release_policy_rejects_gate_list_tampering() -> None:
    manifest = _manifest(unresolved=["image_signature"])
    manifest["unresolved_gates"] = []

    with pytest.raises(ValueError, match="statuses and unresolved"):
        evaluate_manifest(manifest)
