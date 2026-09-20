"""Run a bounded PostgreSQL 17 PITR rehearsal in Docker.

This is a local deployment profile, not a production scheduler.  It creates a
primary with WAL archiving, takes a physical ``pg_basebackup`` tar artifact,
writes a synthetic application marker after the base backup, and restores a
second PostgreSQL instance to a target timestamp.  The resulting files are
then checked by the same ``aftercare-backup pitr`` verifier used by a target
deployment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import socket
import subprocess
import tarfile
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aftercare_agent.ops.pitr import (
    PITR_CHECKS,
    PhysicalBackupManifest,
    PitrCheck,
    PitrEvidence,
    verify_pitr_file,
)
from aftercare_agent.ops.wal_archive import parse_lsn


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Docker:
    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout

    def run(self, args: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
                ["docker", *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"docker command timed out after {self.timeout:.0f}s: docker {' '.join(args)}"
            ) from exc
        if check and completed.returncode != 0:
            command = "docker " + " ".join(args)
            raise RuntimeError(
                f"{command} failed ({completed.returncode}): "
                f"{completed.stderr.strip() or completed.stdout.strip()}"
            )
        return completed


def _exec(docker: Docker, container: str, *command: str) -> str:
    return docker.run(["exec", container, *command]).stdout.strip()


def _wait_ready(docker: Docker, container: str, *, attempts: int = 60) -> None:
    for _ in range(attempts):
        result = docker.run(
            ["exec", container, "pg_isready", "-U", "postgres", "-d", "aftercare"],
            check=False,
        )
        if result.returncode == 0:
            return
        time.sleep(1)
    logs = docker.run(["logs", container], check=False).stdout[-4000:]
    raise RuntimeError(f"container {container} did not become ready:\n{logs}")


def _query(docker: Docker, container: str, sql: str) -> str:
    return _exec(
        docker,
        container,
        "psql",
        "-U",
        "postgres",
        "-d",
        "aftercare",
        "-At",
        "-c",
        sql,
    )


def _parse_backup_label_text(text: str) -> tuple[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("START WAL LOCATION:"):
            values["start"] = line.split()[3]
        elif line.startswith("STOP WAL LOCATION:"):
            values["stop"] = line.split()[3]
    if "start" not in values:
        raise RuntimeError("backup_label has no WAL start")
    parse_lsn(values["start"])
    if "stop" in values:
        parse_lsn(values["stop"])
    return values["start"], values.get("stop", values["start"])


def _parse_backup_label(path: Path, manifest_path: Path) -> tuple[str, str]:
    with tarfile.open(path) as bundle:
        member = bundle.extractfile("backup_label")
        if member is None:
            raise RuntimeError(f"{path} has no backup_label member")
        start, _ = _parse_backup_label_text(member.read().decode("utf-8"))
    try:
        ranges = json.loads(manifest_path.read_text(encoding="utf-8"))["WAL-Ranges"]
        if not ranges:
            raise KeyError("WAL-Ranges")
        stop = str(ranges[-1]["End-LSN"])
        parse_lsn(stop)
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise RuntimeError(f"{manifest_path} has no complete WAL range") from exc
    return start, stop


def _safe_extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            target = (destination / member.name).resolve()
            if root not in target.parents and target != root:
                raise RuntimeError(f"refusing to extract outside restore directory: {member.name}")
        bundle.extractall(destination, filter="data")


def _mount(path: Path, destination: str, *, read_only: bool = False) -> str:
    suffix = ",readonly" if read_only else ""
    return f"type=bind,source={path.resolve()},destination={destination}{suffix}"


def run_drill(*, output: Path, image: str, keep_containers: bool, timeout: float) -> dict[str, Any]:
    if shutil.which("docker") is None:
        raise RuntimeError("docker is not available on PATH")
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"output directory must be empty for an auditable run: {output}")
    output.mkdir(parents=True, exist_ok=True)
    archive_dir = output / "wal-archive"
    base_dir = output / "base"
    restore_dir = output / "restore-data"
    for directory in (archive_dir, base_dir, restore_dir):
        directory.mkdir(parents=True, exist_ok=True)
    docker = Docker(timeout=timeout)
    run_id = _utc_now().strftime("%Y%m%d%H%M%S")
    primary = f"aftercare-pitr-primary-{run_id}"
    restored = f"aftercare-pitr-restore-{run_id}"
    primary_port = _free_port()
    restore_port = _free_port()
    backup_name = f"base-{run_id}"
    started_at = _utc_now()
    try:
        docker.run(["pull", image])
        docker.run(
            [
                "run",
                "-d",
                "--name",
                primary,
                "-e",
                "POSTGRES_PASSWORD=local-only",
                "-e",
                "POSTGRES_DB=aftercare",
                "-p",
                f"127.0.0.1:{primary_port}:5432",
                "--mount",
                _mount(archive_dir, "/var/lib/postgresql/archive"),
                "--mount",
                _mount(base_dir, "/var/lib/postgresql/base-out"),
                image,
                "-c",
                "wal_level=replica",
                "-c",
                "archive_mode=on",
                "-c",
                "archive_timeout=1s",
                "-c",
                "archive_command=test ! -f /var/lib/postgresql/archive/%f && "
                "cp %p /var/lib/postgresql/archive/%f",
            ]
        )
        _wait_ready(docker, primary)
        _query(
            docker,
            primary,
            "CREATE TABLE aftercare_pitr_probe (id integer PRIMARY KEY, marker text NOT NULL); "
            "CREATE TABLE aftercare_pitr_actions (id text PRIMARY KEY, state text NOT NULL); "
            "INSERT INTO aftercare_pitr_probe VALUES (1, 'before-target');",
        )
        backup_start = _utc_now()
        docker.run(
            [
                "exec",
                "-u",
                "postgres",
                primary,
                "pg_basebackup",
                "-U",
                "postgres",
                "-D",
                "/var/lib/postgresql/base-out",
                "-Ft",
                "-X",
                "none",
                "-P",
            ]
        )
        backup_finished = _utc_now()
        base_tar = base_dir / "base.tar"
        if not base_tar.is_file():
            raise RuntimeError("pg_basebackup did not produce base.tar")
        wal_start, wal_stop = _parse_backup_label(base_tar, base_dir / "backup_manifest")
        commit_text = _query(
            docker,
            primary,
            "INSERT INTO aftercare_pitr_probe VALUES (2, 'target'); "
            "INSERT INTO aftercare_pitr_actions VALUES ('synthetic-target-action', 'PENDING'); "
            "SELECT clock_timestamp();",
        )
        commit_time = datetime.fromisoformat(commit_text.splitlines()[-1]).astimezone(UTC)
        target_time = commit_time + timedelta(milliseconds=100)
        time.sleep(0.5)
        _query(docker, primary, "INSERT INTO aftercare_pitr_probe VALUES (3, 'after-target');")
        _query(docker, primary, "SELECT pg_switch_wal();")
        time.sleep(3)
        manifest = PhysicalBackupManifest(
            name=backup_name,
            artifact=base_tar.name,
            created_at=_utc_now(),
            started_at=backup_start,
            finished_at=backup_finished,
            server_version=_query(docker, primary, "SHOW server_version"),
            wal_start_lsn=wal_start,
            wal_stop_lsn=wal_stop,
            artifact_bytes=base_tar.stat().st_size,
            artifact_sha256=_sha256(base_tar),
        )
        manifest_path = base_dir / "base.manifest.json"
        manifest_path.write_text(manifest.to_json(), encoding="utf-8")
        _safe_extract(base_tar, restore_dir)
        (restore_dir / "recovery.signal").touch()
        with (restore_dir / "postgresql.auto.conf").open("a", encoding="utf-8") as config:
            config.write(
                "\nrestore_command = 'cp /var/lib/postgresql/archive/%f %p'\n"
                f"recovery_target_time = '{target_time.isoformat()}'\n"
                "recovery_target_action = 'promote'\n"
            )
        with (restore_dir / "pg_hba.conf").open("a", encoding="utf-8") as hba:
            hba.write("\nhost all all 0.0.0.0/0 trust\n")
        docker.run(
            [
                "run",
                "-d",
                "--name",
                restored,
                "-p",
                f"127.0.0.1:{restore_port}:5432",
                "--mount",
                _mount(archive_dir, "/var/lib/postgresql/archive", read_only=True),
                "--mount",
                _mount(restore_dir, "/var/lib/postgresql/data"),
                image,
                "-c",
                "listen_addresses=*",
            ]
        )
        _wait_ready(docker, restored)
        in_recovery, probes, actions, replay_lsn = _query(
            docker,
            restored,
            "SELECT pg_is_in_recovery(), (SELECT count(*) FROM aftercare_pitr_probe), "
            "(SELECT count(*) FROM aftercare_pitr_actions), "
            "COALESCE(pg_last_wal_replay_lsn()::text, pg_current_wal_lsn()::text);",
        ).split("|")
        if in_recovery != "f":
            raise RuntimeError("restore instance is still in recovery after target promotion")
        recovered_lsn = replay_lsn
        checks = {
            name: PitrCheck(
                status="pass",
                evidence=f"local-docker:{name}",
                detail="synthetic local profile",
            )
            for name in PITR_CHECKS
        }
        if probes != "2":
            checks["application_state"] = PitrCheck(
                status="fail",
                evidence="local-docker:probe",
                detail=f"expected 2 rows, got {probes}",
            )
        if actions != "1":
            checks["action_reconciliation"] = PitrCheck(
                status="fail",
                evidence="local-docker:actions",
                detail=f"expected 1 action, got {actions}",
            )
        evidence = PitrEvidence(
            base_backup_name=manifest.name,
            base_backup_sha256=manifest.artifact_sha256,
            target_time=target_time,
            started_at=started_at,
            finished_at=_utc_now(),
            recovered_lsn=recovered_lsn,
            wal_replayed_segments=max(1, len(list(archive_dir.glob("????????????????????????")))),
            checks=checks,
        )
        evidence_path = output / "pitr-run.json"
        evidence_path.write_text(evidence.to_json(), encoding="utf-8")
        result = verify_pitr_file(manifest_path, evidence_path)
        (output / "pitr-verified.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return result
    finally:
        if not keep_containers:
            docker.run(["rm", "-f", restored], check=False)
            docker.run(["rm", "-f", primary], check=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", default="postgres:17")
    parser.add_argument("--keep-containers", action="store_true")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args(argv)
    try:
        result = run_drill(
            output=args.output,
            image=args.image,
            keep_containers=args.keep_containers,
            timeout=args.timeout,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"aftercare PITR Docker drill: {exc}")
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["decision"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
