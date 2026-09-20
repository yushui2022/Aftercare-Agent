"""Process entry point for the Aftercare HTTP API."""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence

import uvicorn


def _run_server(*, host: str, port: int, workers: int, log_level: str) -> None:
    uvicorn.run(
        "aftercare_agent.api.app:app",
        host=host,
        port=port,
        workers=workers,
        log_level=log_level,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("AFTERCARE_API_HOST", "0.0.0.0"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("AFTERCARE_API_PORT", "8000")),
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=int(os.environ.get("AFTERCARE_API_WORKERS", "1")),
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("AFTERCARE_API_LOG_LEVEL", "info"),
    )
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.workers < 1:
        parser.error("--workers must be positive")
    _run_server(host=args.host, port=args.port, workers=args.workers, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
