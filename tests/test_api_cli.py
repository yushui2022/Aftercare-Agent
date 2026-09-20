from __future__ import annotations

from typing import Any

import pytest

from aftercare_agent.api import cli


def test_api_cli_passes_process_options_to_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def capture(*, host: str, port: int, workers: int, log_level: str) -> None:
        captured.update(
            target="aftercare_agent.api.app:app",
            host=host,
            port=port,
            workers=workers,
            log_level=log_level,
        )

    monkeypatch.setattr(cli, "_run_server", capture)

    assert cli.main(["--host", "127.0.0.1", "--port", "9000", "--workers", "2"]) == 0

    assert captured == {
        "target": "aftercare_agent.api.app:app",
        "host": "127.0.0.1",
        "port": 9000,
        "workers": 2,
        "log_level": "info",
    }


def test_api_cli_rejects_invalid_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_run_server", lambda **_kwargs: None)

    with pytest.raises(SystemExit, match="2"):
        cli.main(["--port", "0"])
