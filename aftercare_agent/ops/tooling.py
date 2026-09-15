"""Plumbing shared by the operator tools: processes, connections and boundaries.

Nothing here knows what a backup is: it locates PostgreSQL client binaries,
keeps a password off a command line, splits a DSN into libpq arguments, and
gives the file boundaries one base class that rejects unknown keys.
"""

import os
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from pydantic import BaseModel, ConfigDict

DUMP_ENV = "AFTERCARE_PG_DUMP"
RESTORE_ENV = "AFTERCARE_PG_RESTORE"
DEFAULT_COMMAND_TIMEOUT_SECONDS = 3600.0
MAINTENANCE_DATABASES = ("postgres", "template1")
DATABASE_NAME_PATTERN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_$-]{0,62}")
_VERSION = re.compile(r"\b(\d+)(?:\.\d+)*\b")


class OpsError(RuntimeError):
    """An operator-facing failure whose message is worth printing verbatim."""


class MissingToolError(OpsError):
    """A required PostgreSQL client binary is not installed or not runnable."""


class IncompatibleToolError(OpsError):
    """A client binary cannot read the server or archive it was pointed at."""


class OpsModel(BaseModel):
    """A boundary with a file: an unknown key means the reader is out of date."""

    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True)
class Tool:
    """One PostgreSQL client binary and the major version it speaks."""

    name: str
    path: str
    version: str
    major: int


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    """The seam that lets tooling tests run without a PostgreSQL client."""

    def run(
        self, argv: Sequence[str], *, env: Mapping[str, str] | None = None
    ) -> CommandResult: ...


class SubprocessRunner:
    """Run a client binary without a shell, with a timeout and decoded output."""

    def __init__(self, timeout: float = DEFAULT_COMMAND_TIMEOUT_SECONDS) -> None:
        self.timeout = timeout

    def run(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None) -> CommandResult:
        merged = None if env is None else {**os.environ, **env}
        try:
            completed = subprocess.run(
                list(argv),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=merged,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise OpsError(f"{argv[0]} did not finish within {self.timeout:g}s") from exc
        return CommandResult(
            argv=tuple(argv),
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


def parse_major(version_text: str) -> int:
    """The major version out of ``pg_dump (PostgreSQL) 16.13 (Ubuntu ...)``."""
    match = _VERSION.search(version_text)
    if match is None:
        raise OpsError(f"cannot read a version number from {version_text!r}")
    return int(match.group(1))


def resolve_tool(
    name: str,
    *,
    env_var: str,
    runner: CommandRunner,
    required_major: int | None = None,
) -> Tool:
    """Locate a client binary, ask it its own version, and check it can serve.

    ``pg_dump`` refuses to read a server newer than itself, so a missing or
    stale client has to fail here, where an operator can act on the message,
    rather than after a half-written dump.
    """
    path = os.environ.get(env_var) or shutil.which(name)
    if not path:
        raise MissingToolError(
            f"{name} is not on PATH; install the PostgreSQL client or set {env_var}"
        )
    result = runner.run((path, "--version"))
    if result.returncode != 0:
        raise MissingToolError(
            f"{path} --version failed with exit code {result.returncode}: {result.stderr.strip()}"
        )
    version = (result.stdout or result.stderr).strip()
    major = parse_major(version)
    if required_major is not None and major < required_major:
        raise IncompatibleToolError(
            f"{name} {major} cannot read PostgreSQL {required_major}; install a matching client"
        )
    return Tool(name=name, path=path, version=version, major=major)


def validate_database_name(name: str) -> str:
    """Refuse a scratch database name that is not a plain identifier."""
    if not DATABASE_NAME_PATTERN.fullmatch(name):
        raise OpsError(f"database name {name!r} takes letters, digits, underscore, dash and dollar")
    return name


def connection_parameters(dsn: str) -> dict[str, str]:
    """Split a DSN into libpq keyword arguments."""
    if not dsn:
        raise OpsError("a database URL is required")
    try:
        parameters = conninfo_to_dict(dsn)
    except psycopg.Error as exc:
        raise OpsError(f"cannot parse the database URL: {exc}") from exc
    if not parameters:
        raise OpsError(f"cannot parse the database URL: {dsn!r}")
    return {str(key): str(value) for key, value in parameters.items()}


def dsn_with_database(dsn: str, database: str) -> str:
    parameters = connection_parameters(dsn)
    parameters["dbname"] = database
    return make_conninfo(**parameters)


def conninfo_without_password(dsn: str) -> str:
    """The DSN a child process may see on its command line."""
    parameters = connection_parameters(dsn)
    parameters.pop("password", None)
    return make_conninfo(**parameters)


def child_environment(dsn: str) -> Mapping[str, str]:
    """The password a client binary needs, without putting it on a command line."""
    password = connection_parameters(dsn).get("password")
    return {} if password is None else {"PGPASSWORD": password}
