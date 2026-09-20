from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"


def test_release_workflow_verifies_and_binds_kubernetes_profile() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "Verify Kubernetes reference profile against the OCI image" in workflow
    assert "verify_kubernetes_profile.py" in workflow
    assert "kubernetes-profile-evidence.json" in workflow
    assert (
        "--kubernetes-profile-evidence evidence/image/kubernetes-profile-evidence.json" in workflow
    )


def test_ci_tests_the_built_wheel_outside_the_source_tree() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "Verify the built wheel outside the source tree" in workflow
    assert "uv pip install --python" in workflow
    assert 'python" -I -m pytest' in workflow
    assert "tests/test_package_contract.py" in workflow


def test_ci_runs_deployment_preflight_with_synthetic_secret_files() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert 'preflight_root="$(mktemp -d)"' in workflow
    assert "sslmode=verify-full" in workflow
    assert "chmod 600" in workflow
    assert "deploy/deployment_preflight.py" in workflow
    assert "preflight.json" in workflow
