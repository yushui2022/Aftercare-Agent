"""Small fail-closed configuration readers shared by process entry points."""

import os
from collections.abc import Mapping
from pathlib import Path

from psycopg import ProgrammingError
from psycopg.conninfo import conninfo_to_dict

MAX_SECRET_FILE_BYTES = 64 * 1024


def require_database_tls(dsn: str) -> str:
    """Require hostname and CA verification for a PostgreSQL connection string."""
    try:
        sslmode = conninfo_to_dict(dsn).get("sslmode")
    except ProgrammingError as exc:
        raise RuntimeError("DATABASE_URL is not a valid PostgreSQL connection string") from exc
    if sslmode != "verify-full":
        raise RuntimeError(
            "DATABASE_URL must set sslmode=verify-full when AFTERCARE_REQUIRE_DATABASE_TLS=1"
        )
    return sslmode


def environment_secret(
    name: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Read ``NAME`` or one-line UTF-8 ``NAME_FILE``, never both.

    File-backed values keep credentials out of container environment metadata.
    One final line ending is accepted because orchestrator-mounted secret files
    commonly contain it; embedded newlines, NULs, empty files, and oversized
    files are rejected before a credential reaches a client library.
    """
    source = os.environ if environ is None else environ
    file_name = f"{name}_FILE"
    direct = source.get(name)
    secret_path = source.get(file_name)
    if direct is not None and secret_path is not None:
        raise RuntimeError(f"configure only one of {name} and {file_name}")
    if secret_path is None:
        value = direct
    else:
        if not secret_path.strip():
            raise RuntimeError(f"{file_name} must name a readable secret file")
        try:
            with Path(secret_path).open("rb") as handle:
                raw = handle.read(MAX_SECRET_FILE_BYTES + 1)
        except OSError as exc:
            raise RuntimeError(f"{file_name} cannot be read") from exc
        if len(raw) > MAX_SECRET_FILE_BYTES:
            raise RuntimeError(f"{file_name} exceeds {MAX_SECRET_FILE_BYTES} bytes")
        try:
            value = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeError(f"{file_name} must contain UTF-8 text") from exc
        if value.endswith("\r\n"):
            value = value[:-2]
        elif value.endswith("\n"):
            value = value[:-1]
        if not value or "\r" in value or "\n" in value or "\x00" in value:
            raise RuntimeError(f"{file_name} must contain exactly one non-empty line")
    if name == "DATABASE_URL" and source.get("AFTERCARE_REQUIRE_DATABASE_TLS") == "1" and value:
        require_database_tls(value)
    return value
