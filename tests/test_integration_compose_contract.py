from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _compose() -> dict[str, Any]:
    value = yaml.safe_load(
        (ROOT / "deploy" / "compose" / "docker-compose.integration.yml").read_text(encoding="utf-8")
    )
    assert isinstance(value, dict)
    return value


def test_integration_compose_archives_machine_readable_rls_results() -> None:
    services = _compose()["services"]
    for name, filename in (("force-rls", "rls-harden.json"), ("runtime-check", "rls-verify.json")):
        command = services[name]["command"]
        assert command[:2] == ["sh", "-ec"]
        assert filename in command[2]
        assert "AFTERCARE_INTEGRATION_EVIDENCE_DIR" in services[name]["volumes"][0]


def test_ci_builds_smoke_from_current_sources_and_uploads_rls_evidence() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "run --no-deps --build --rm smoke" in workflow
    assert "name: integration-rls-evidence" in workflow
    assert "aftercare-integration-evidence/*.json" in workflow
