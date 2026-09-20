"""Validate deployment profile inputs without contacting external systems."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from aftercare_agent.config import require_database_tls

_DIGEST = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
_SECRET_LIMIT = 64 * 1024
_FORBIDDEN_PLAINTEXT_SECRETS = frozenset(
    {
        "DATABASE_URL",
        "AFTERCARE_MODEL_API_KEY",
        "AFTERCARE_OIDC_INTROSPECTION_CLIENT_SECRET",
    }
)


def _image_digest(image: str) -> str | None:
    match = _DIGEST.fullmatch(image)
    if match is None:
        return None
    return "sha256:" + image.rsplit("@sha256:", 1)[1]


def _read_env(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot read deployment env file: {exc}") from exc
    values: dict[str, str] = {}
    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"deployment env line {number} is not KEY=VALUE")
        name, value = line.split("=", 1)
        name = name.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            raise ValueError(f"deployment env line {number} has an invalid variable name")
        if name in values:
            raise ValueError(f"deployment env repeats {name}")
        values[name] = value.strip()
    return values


def _check_secret(
    path_text: str,
    name: str,
    *,
    require_tls: bool = False,
) -> dict[str, Any]:
    path = Path(path_text)
    if not path.is_absolute():
        raise ValueError(f"{name} secret file path must be absolute")
    try:
        payload = path.read_bytes()
        mode = path.stat().st_mode & 0o777
    except OSError as exc:
        raise ValueError(f"{name} secret file is not readable: {exc}") from exc
    if os.name == "posix" and mode & 0o077:
        raise ValueError(f"{name} secret file must be owner-only (mode 0600 or stricter)")
    if not payload or len(payload) > _SECRET_LIMIT:
        raise ValueError(f"{name} secret file is empty or larger than 64 KiB")
    try:
        value = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{name} secret file is not UTF-8") from exc
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    if not value or "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError(f"{name} secret file must contain one non-empty line")
    result: dict[str, Any] = {
        "name": name,
        "status": "pass",
        "bytes": len(payload),
        "mode": oct(mode),
    }
    if require_tls:
        try:
            result["sslmode"] = require_database_tls(value)
        except RuntimeError as exc:
            raise ValueError(f"{name}: {exc}") from exc
    return result


def _https(value: str, name: str) -> dict[str, str]:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"{name} must be an HTTPS URL")
    return {"name": name, "status": "pass"}


def preflight(env_file: Path) -> dict[str, Any]:
    values = _read_env(env_file)
    errors: list[str] = []
    checks: list[dict[str, Any]] = []

    for name in sorted(_FORBIDDEN_PLAINTEXT_SECRETS.intersection(values)):
        errors.append(f"{name} must not be set in deployment env; use a secret file")

    image = values.get("AFTERCARE_IMAGE", "")
    image_digest = _image_digest(image)
    if image_digest is None:
        errors.append("AFTERCARE_IMAGE must use an immutable sha256 digest")
    else:
        checks.append({"name": "immutable_image", "status": "pass"})

    migration = values.get("AFTERCARE_MIGRATION_DATABASE_URL_FILE", "")
    runtime = values.get("AFTERCARE_RUNTIME_DATABASE_URL_FILE", "")
    require_tls = values.get("AFTERCARE_REQUIRE_DATABASE_TLS") == "1"
    if not require_tls:
        errors.append("AFTERCARE_REQUIRE_DATABASE_TLS must be 1 in the deployment profile")
    if not migration or not runtime:
        errors.append("migration and runtime database secret paths are required")
    elif Path(migration).resolve() == Path(runtime).resolve():
        errors.append("migration and runtime database secret paths must differ")
    else:
        for path_text, name in (
            (migration, "migration_database_url"),
            (runtime, "runtime_database_url"),
        ):
            try:
                checks.append(_check_secret(path_text, name, require_tls=require_tls))
            except ValueError as exc:
                errors.append(str(exc))

    for name in ("AFTERCARE_OIDC_ISSUER", "AFTERCARE_OIDC_JWKS_URL"):
        value = values.get(name, "")
        if not value:
            errors.append(f"{name} is required")
        else:
            try:
                checks.append(_https(value, name))
            except ValueError as exc:
                errors.append(str(exc))
    if not values.get("AFTERCARE_OIDC_AUDIENCE", ""):
        errors.append("AFTERCARE_OIDC_AUDIENCE is required")
    else:
        checks.append({"name": "oidc_audience", "status": "pass"})

    tenant_id = values.get("AFTERCARE_TENANT_ID", "").strip()
    if not tenant_id or tenant_id == "replace-with-tenant-id":
        errors.append("AFTERCARE_TENANT_ID is required for tenant-pinned Workers")
    else:
        checks.append({"name": "worker_tenant", "status": "pass"})

    metrics_backend = values.get("AFTERCARE_METRICS_BACKEND", "logging").strip().lower()
    if metrics_backend not in {"logging", "otel"}:
        errors.append("AFTERCARE_METRICS_BACKEND must be logging or otel")
    else:
        checks.append({"name": "metrics_backend", "status": "pass", "backend": metrics_backend})

    for prefix in ("AFTERCARE_API_POOL", "AFTERCARE_WORKER_POOL"):
        try:
            minimum = int(values.get(f"{prefix}_MIN_SIZE", "1"))
            maximum = int(values.get(f"{prefix}_MAX_SIZE", "1"))
            if minimum < 1 or maximum < minimum:
                raise ValueError
        except ValueError:
            errors.append(f"{prefix}_MIN_SIZE/MAX_SIZE must be positive and ordered")
        else:
            checks.append({"name": f"{prefix.lower()}_bounds", "status": "pass"})

    return {
        "schema_version": 1,
        "verifier": "aftercare-preflight",
        "image_digest": image_digest,
        "status": "fail" if errors else "pass",
        "checks": checks,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = preflight(args.env_file)
    except ValueError as exc:
        report = {
            "schema_version": 1,
            "verifier": "aftercare-preflight",
            "image_digest": None,
            "status": "fail",
            "checks": [],
            "errors": [str(exc)],
        }
    serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
