"""Offline installed-package checks, separate from PostgreSQL behavior tests.

Aftercare may be editable during development. EGM must come from the reviewed
Git revision; source-directory and wheel isolation are checked separately.
"""

import json
from importlib.metadata import distribution
from importlib.resources import as_file, files

from evidence_gated_memory.schemas.builtin import AFTERCARE
from evidence_gated_memory.schemas.loader import load_schema
from evidence_gated_memory.storage.postgres import PostgresProvider

EGM_COMMIT = "9c7c5d196f8e703fdc7c70546cff0dc94cc78dcd"
EGM_REPOSITORY = "https://github.com/yushui2022/Evidence-Gated-Memory.git"


def test_aftercare_metadata_pins_reviewed_egm_with_postgres() -> None:
    installed = distribution("aftercare-agent")

    assert installed.metadata["Name"] == "aftercare-agent"
    assert installed.version == "0.1.0a0"
    requirements = {item.replace(" ", "") for item in installed.requires or []}
    expected = f"evidence-gated-memory[postgres]@git+{EGM_REPOSITORY}@{EGM_COMMIT}"
    assert expected in requirements


def test_egm_install_origin_is_the_reviewed_git_commit() -> None:
    installed = distribution("evidence-gated-memory")
    assert installed.version == "0.6.0"
    direct_url = installed.read_text("direct_url.json")
    assert direct_url is not None, "EGM must retain its installed Git provenance"
    origin: object = json.loads(direct_url)
    assert isinstance(origin, dict)
    assert origin.get("url") == EGM_REPOSITORY
    assert "dir_info" not in origin, "An adjacent checkout is not the pinned Git dependency"
    assert "archive_info" not in origin
    vcs_info = origin.get("vcs_info")
    assert isinstance(vcs_info, dict)
    assert vcs_info.get("vcs") == "git"
    assert vcs_info.get("commit_id") == EGM_COMMIT


def test_installed_packages_include_typing_markers() -> None:
    assert files("aftercare_agent").joinpath("py.typed").is_file()
    assert files("evidence_gated_memory").joinpath("py.typed").is_file()


def test_installed_aftercare_schema_loads_from_package_resources() -> None:
    resource = files("evidence_gated_memory.schemas.builtin").joinpath("aftercare.yaml")
    assert resource.is_file()
    assert AFTERCARE.read_bytes() == resource.read_bytes()
    with as_file(resource) as schema_path:
        schema = load_schema(schema_path)

    assert schema.name == "aftercare"
    assert schema.claim_type("refund_completed") is not None
    assert schema.require_terminal_state_gate is True


def test_installed_postgres_provider_and_migration_are_available() -> None:
    resource = files("evidence_gated_memory.storage").joinpath("postgres_schema.sql")
    assert resource.is_file()
    migration = resource.read_text(encoding="utf-8")
    for table in ("egm_schema_version", "egm_cases", "egm_records", "egm_operations", "egm_audit"):
        assert f"CREATE TABLE IF NOT EXISTS {table} (" in migration
    assert PostgresProvider.__module__ == "evidence_gated_memory.storage.postgres"
    assert callable(PostgresProvider.open)
    assert callable(PostgresProvider.join)
