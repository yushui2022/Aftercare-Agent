"""A real cluster that really archives; skipped when its binaries are absent.

Contiguity can be tested with file names alone, but "does the archiver advance"
and "does the archive reach the dump" can only be answered by a server that has
actually copied a segment.  This module runs its own cluster with
``archive_mode = on`` and an ``archive_command`` that copies into a temporary
directory, so the archive under test is a real one.  ``archive_timeout`` is
short (2 s) so a quiet test does not have to fill a 16MB segment.

The cluster is thrown away with the temporary directory, and nothing here
touches the database named by ``DATABASE_URL``.

The drill also needs ``pg_dump``, because one of the questions it answers is
whether the archive reaches the newest dump.  A directory that has the server
but not the client cannot run these tests, so it counts as absent here --- and
the directory that is found goes on ``PATH``, because the production code under
test looks for its client there.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Never

import psycopg
import pytest
from psycopg import sql

from aftercare_agent.ops.backup import create_backup, main
from aftercare_agent.ops.wal_archive import (
    Segment,
    parse_lsn,
    parse_segment_size,
    segment_holding,
)
from aftercare_agent.persistence import Database, migrate

REQUIRED_TOOLS = ("initdb", "pg_ctl", "postgres", "pg_dump")
REQUIRE_ENV = "AFTERCARE_REQUIRE_DRILLS"
ARCHIVE_SCRIPT = """import shutil
import sys

shutil.copyfile(sys.argv[1], {target} + "/" + sys.argv[2])
"""
BINDIR_CANDIDATES = (
    "/usr/lib/postgresql/17/bin",
    "/usr/lib/postgresql/16/bin",
    "/usr/local/pgsql/bin",
    r"D:\postgresql\16\bin",
)


def _has_tool(directory: Path, name: str) -> bool:
    return (directory / name).exists() or (directory / f"{name}.exe").exists()


def _skip_or_fail(reason: str) -> Never:
    """Skip where the tools may legitimately be absent; fail where they must exist.

    A developer without a PostgreSQL installation is entitled to a skip that
    says why.  CI is not that developer: it installs the tools on purpose, so a
    drill that skips there means the job ran less than it claims to have run.
    A green job over a skipped drill is the one outcome this gate exists to
    prevent, so ``AFTERCARE_REQUIRE_DRILLS=1`` turns the skip into a failure.
    """
    if os.environ.get(REQUIRE_ENV) == "1":
        pytest.fail(f"{REQUIRE_ENV}=1 but this drill cannot run: {reason}")
    pytest.skip(reason)


def _bindir() -> Path:
    """Where the server binaries are, or a skip that says what is missing."""
    candidates: list[Path] = []
    configured = os.environ.get("AFTERCARE_PG_BINDIR")
    if configured:
        candidates.append(Path(configured))
    for name in ("pg_ctl", "pg_dump"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found).parent)
    candidates.extend(Path(candidate) for candidate in BINDIR_CANDIDATES)
    for candidate in candidates:
        if all(_has_tool(candidate, tool) for tool in REQUIRED_TOOLS):
            return candidate
    _skip_or_fail(
        "no PostgreSQL server binaries carrying their client tools "
        "(initdb/pg_ctl/postgres/pg_dump) were found together; "
        "install the server package or set AFTERCARE_PG_BINDIR"
    )


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _archive_command(script: Path) -> str:
    """The archive_command this cluster runs.

    Paths go in with forward slashes: a postgresql.conf string literal eats
    backslashes, so a Windows path written the usual way arrives at cmd.exe
    with the separators gone.  Python and cmd both accept forward slashes.
    """
    return f'"{Path(sys.executable).as_posix()}" "{script.as_posix()}" %p %f'


def _tool(bindir: Path, name: str) -> str:
    executable = bindir / f"{name}.exe"
    return str(executable if executable.exists() else bindir / name)


@dataclass
class ArchivedCluster:
    """One throwaway cluster that archives into its own directory."""

    bindir: Path
    data: Path
    archive: Path
    script: Path
    port: int
    admin_dsn: str
    database: str

    @property
    def dsn(self) -> str:
        return f"{self.admin_dsn.rsplit('/', 1)[0]}/{self.database}"

    def control(self, *arguments: str) -> int:
        """Run pg_ctl with its output in a file, and return its exit status.

        The output must not go to a pipe: on Windows the postmaster that
        ``pg_ctl start`` leaves running can inherit the pipe, and a caller
        waiting for that pipe waits on a server that is already up.
        """
        with self.pg_ctl_log.open("a", encoding="utf-8") as handle:
            completed = subprocess.run(
                [_tool(self.bindir, "pg_ctl"), "-D", str(self.data), *arguments],
                stdout=handle,
                stderr=subprocess.STDOUT,
                timeout=120,
                check=False,
            )
        return completed.returncode

    @property
    def pg_ctl_log(self) -> Path:
        return self.data.parent / "pg_ctl.log"

    def stop(self) -> None:
        self.control("stop", "-m", "immediate", "-w")

    def connect(self, *, autocommit: bool = True) -> psycopg.Connection[tuple[object, ...]]:
        return psycopg.connect(self.admin_dsn, autocommit=autocommit)

    def create_database(self) -> None:
        with self.connect() as connection:
            connection.execute(f'CREATE DATABASE "{self.database}"')

    def switch_wal(self) -> None:
        """End the current segment, after writing something into it.

        ``pg_switch_wal()`` returns without switching when the current segment
        has not been written to since the last switch, and an idle test cluster
        writes nothing by itself; without the insert these tests would depend on
        a segment that never ends.
        """
        with self.connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS wal_noise (i integer)")
            connection.execute("INSERT INTO wal_noise VALUES (1)")
            connection.execute("SELECT pg_switch_wal()")

    def set_archive_command(self, command: str) -> None:
        with self.connect() as connection:
            # ALTER SYSTEM takes no parameters, so the value is quoted as a literal.
            connection.execute(
                sql.SQL("ALTER SYSTEM SET archive_command = {}").format(sql.Literal(command))
            )
            connection.execute("SELECT pg_reload_conf()")

    def archived_segments(self) -> list[Path]:
        return [
            path
            for path in sorted(self.archive.iterdir())
            if len(path.name) == 24 and not path.name.endswith((".partial", ".history"))
        ]

    def wait_for(self, predicate: Callable[[], bool], *, seconds: float = 45.0) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.25)
        return False


def _initdb(bindir: Path, data: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            _tool(bindir, "initdb"),
            "-D",
            str(data),
            "-U",
            "aftercare",
            "-A",
            "trust",
            "-E",
            "UTF8",
            "--no-sync",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def _start_failure(cluster: ArchivedCluster, root: Path) -> str:
    """Why the cluster would not start, which ``pg_ctl`` alone never says.

    ``pg_ctl`` reports only that it waited and the server stopped; the reason is
    in the postmaster's own log, which ``-l`` puts beside the data directory.
    A skip that cannot say what went wrong is not evidence of anything.
    """
    logs = [cluster.pg_ctl_log, root / "server.log"]
    parts = [
        log.read_text(encoding="utf-8", errors="replace").strip()[-400:]
        for log in logs
        if log.exists()
    ]
    return " / ".join(part for part in parts if part) or "no log was written"


def _start_cluster(bindir: Path, root: Path) -> ArchivedCluster:
    data = root / "data"
    archive = root / "archive"
    socket = root / "socket"
    archive.mkdir(parents=True, exist_ok=True)
    socket.mkdir(parents=True, exist_ok=True)
    script = root / "archive_copy.py"
    script.write_text(ARCHIVE_SCRIPT.format(target=repr(archive.as_posix())), encoding="utf-8")
    created = _initdb(bindir, data)
    if created.returncode != 0:
        _skip_or_fail(f"initdb failed here: {created.stderr.strip()[-400:]}")
    conf = data / "postgresql.conf"
    base = conf.read_text(encoding="utf-8")
    settings = [
        "listen_addresses = '127.0.0.1'",
        "archive_mode = on",
        "archive_timeout = 2s",
        "fsync = off",
        "full_page_writes = off",
    ]
    if os.name != "nt":
        # Debian and Ubuntu build the default Unix socket into
        # /var/run/postgresql, which belongs to their own postgres user: a
        # throwaway cluster started by anyone else stops there before it
        # listens at all.  Keep every file this cluster makes inside its own
        # directory, which no system user owns.  Windows has no such socket.
        settings.append(f"unix_socket_directories = '{socket.as_posix()}'")
    for _ in range(3):
        port = _free_port()
        command = _archive_command(script)
        conf.write_text(
            base
            + "\n"
            + "".join(f"{setting}\n" for setting in [*settings, f"port = {port}"])
            + f"archive_command = '{command}'\n",
            encoding="utf-8",
        )
        cluster = ArchivedCluster(
            bindir=bindir,
            data=data,
            archive=archive,
            script=script,
            port=port,
            admin_dsn=f"postgresql://aftercare@127.0.0.1:{port}/postgres",
            database="aftercare",
        )
        if cluster.control("start", "-l", str(root / "server.log"), "-w", "-t", "60") == 0:
            return cluster
    _skip_or_fail(f"could not start a scratch PostgreSQL cluster: {_start_failure(cluster, root)}")


@pytest.fixture(scope="module")
def cluster(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ArchivedCluster]:
    bindir = _bindir()
    root = tmp_path_factory.mktemp("wal-cluster")
    running = _start_cluster(bindir, root)
    running.create_database()
    previous_path = os.environ.get("PATH", "")
    os.environ["PATH"] = os.pathsep.join([str(bindir), previous_path])
    try:
        yield running
    finally:
        os.environ["PATH"] = previous_path
        running.stop()


@pytest.fixture()
def migrated(cluster: ArchivedCluster) -> ArchivedCluster:
    database = Database(cluster.dsn)
    try:
        with database.transaction() as connection:
            migrate(connection)
    finally:
        database.close()
    return cluster


def _archiver_row(cluster: ArchivedCluster) -> tuple[int, str | None, str | None]:
    with cluster.connect() as connection:
        row = connection.execute(
            "SELECT failed_count, last_failed_wal, last_archived_wal FROM pg_stat_archiver"
        ).fetchone()
    assert row is not None
    failed_count, failed_wal, archived_wal = row
    return (
        failed_count if isinstance(failed_count, int) else 0,
        None if failed_wal is None else str(failed_wal),
        None if archived_wal is None else str(archived_wal),
    )


def _server_segment_bytes(cluster: ArchivedCluster) -> int:
    with cluster.connect() as connection:
        row = connection.execute("SHOW wal_segment_size").fetchone()
    assert row is not None
    return parse_segment_size(str(row[0]))


def _newest_archived(cluster: ArchivedCluster) -> Segment | None:
    parsed = [Segment.parse(path.name) for path in cluster.archived_segments()]
    segments = [segment for segment in parsed if segment is not None]
    return segments[-1] if segments else None


def _reaches(cluster: ArchivedCluster, log: int, segno: int) -> bool:
    newest = _newest_archived(cluster)
    return newest is not None and (newest.log, newest.segno) >= (log, segno)


def test_a_real_dump_is_covered_by_the_archive_it_started(
    cluster: ArchivedCluster,
    migrated: ArchivedCluster,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    directory = tmp_path / "backups"
    manifest = create_backup(cluster.dsn, directory, name="wal-1")
    assert manifest.manifest_version == 2
    assert manifest.wal_lsn is not None
    needed = segment_holding(parse_lsn(manifest.wal_lsn), _server_segment_bytes(cluster))

    cluster.switch_wal()
    assert cluster.wait_for(lambda: _reaches(cluster, *needed), seconds=60), (
        f"the segment holding {manifest.wal_lsn} never reached the archive"
    )

    report_path = tmp_path / "wal.json"
    code = main(
        [
            "wal",
            "--archive-dir",
            str(cluster.archive),
            "--dsn",
            cluster.dsn,
            "--directory",
            str(directory),
            "--archive-lag-seconds",
            "600",
            "--json",
            str(report_path),
        ]
    )
    printed = capsys.readouterr().out
    assert code == 0, printed
    assert "covered" in printed
    assert "gaps             none" in printed
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["coverage"]["manifest"] == "wal-1"
    assert payload["archiver"]["stuck_on_failure"] is False


def test_an_archive_command_that_fails_is_visible_before_the_data_is_lost(
    cluster: ArchivedCluster,
    migrated: ArchivedCluster,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    directory = tmp_path / "backups"
    create_backup(cluster.dsn, directory, name="wal-2")
    _, _, last_archived_wal = _archiver_row(cluster)
    assert last_archived_wal is not None
    # "exit 1" fails under both sh -c and cmd /c, so the archiver cannot copy
    # anything and retries the same segment -- the failure that stays invisible
    # to every other signal a deployment watches.
    cluster.set_archive_command("exit 1")
    try:
        cluster.switch_wal()
        assert cluster.wait_for(lambda: _archiver_row(cluster)[1] is not None, seconds=60), (
            "the archiver never reported a failed copy"
        )
        code = main(["wal", "--archive-dir", str(cluster.archive), "--dsn", cluster.dsn])
        printed = capsys.readouterr().out
        assert code == 1, printed
        assert "not advancing" in printed
    finally:
        cluster.set_archive_command(_archive_command(cluster.script))


def test_a_segment_deleted_from_the_archive_is_a_hole(
    cluster: ArchivedCluster,
    migrated: ArchivedCluster,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for _ in range(3):
        cluster.switch_wal()
    assert cluster.wait_for(lambda: len(cluster.archived_segments()) >= 3, seconds=60), (
        "the archive never held enough segments to have a middle one"
    )
    segments = cluster.archived_segments()
    segments[len(segments) // 2].unlink()
    # No --dsn: this is the check that has to work when the server is gone, which
    # is the moment an archive is the only thing left.
    code = main(["wal", "--archive-dir", str(cluster.archive)])
    printed = capsys.readouterr().out
    assert code == 1, printed
    assert "hole" in printed
    assert "not read" in printed
