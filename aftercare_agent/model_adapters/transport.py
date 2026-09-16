"""HTTP transport for a Responses-shaped provider endpoint.

Credentials stay out of the runtime's own modules: this transport reads them
from the process environment, keeps them in the outbound header only, and
never renders them into an exception, a repr or a result.  It owns the network
call and nothing else -- the adapter still validates the wire response, and no
code here executes a tool.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

import httpx

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"

_API_KEY_ENV = ("AFTERCARE_MODEL_API_KEY", "DEEPSEEK_API_KEY")
_BASE_URL_ENV = ("AFTERCARE_MODEL_BASE_URL", "DEEPSEEK_BASE_URL")


def _first_present(env: Mapping[str, str], names: tuple[str, ...]) -> str | None:
    for name in names:
        value = env.get(name)
        if value and value.strip():
            return value.strip()
    return None


@dataclass(frozen=True)
class ProviderEndpoint:
    """Resolved provider coordinates; the key does not leave this object."""

    base_url: str
    api_key: str
    timeout_seconds: float = 90.0

    def __post_init__(self) -> None:
        if not self.base_url.strip():
            raise ValueError("provider base_url must not be empty")
        if not self.api_key.strip():
            raise ValueError("provider api_key must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("provider timeout must be positive")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ProviderEndpoint:
        source = os.environ if env is None else env
        key = _first_present(source, _API_KEY_ENV)
        if key is None:
            raise ValueError("no provider key configured; set " + " or ".join(_API_KEY_ENV))
        base = _first_present(source, _BASE_URL_ENV) or DEFAULT_BASE_URL
        return cls(base_url=base, api_key=key)

    def __repr__(self) -> str:
        # The generated dataclass repr would print the key into any log line
        # or traceback that mentions this object.
        return f"ProviderEndpoint(base_url={self.base_url!r}, api_key='[REDACTED]')"


class ProviderError(RuntimeError):
    """One failed provider call; ``status_code`` feeds the error normaliser."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ResponsesHttpClient:
    """The ``responses.create(**payload)`` seam the Responses adapter expects.

    A scripted stand-in and a real endpoint therefore present the same surface,
    and swapping one for the other cannot change how a result is parsed.
    """

    def __init__(self, endpoint: ProviderEndpoint, *, client: httpx.Client | None = None) -> None:
        self._endpoint = endpoint
        self._client = client or httpx.Client(timeout=endpoint.timeout_seconds)
        self.responses = self

    def create(self, **payload: object) -> Mapping[str, object]:
        url = f"{self._endpoint.base_url.rstrip('/')}/responses"
        try:
            response = self._client.post(
                url,
                headers={"Authorization": f"Bearer {self._endpoint.api_key}"},
                json=payload,
            )
        except httpx.HTTPError as exc:
            # The key travels in a header, so a transport failure cannot echo
            # it; the message still passes through the adapter's redactor.
            raise ProviderError(f"provider transport failed: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise ProviderError(
                f"provider returned HTTP {response.status_code}: {response.text[:200]}",
                status_code=response.status_code,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError("provider returned a non-JSON body") from exc
        if not isinstance(body, Mapping):
            raise ProviderError("provider returned a non-object body")
        return body

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ResponsesHttpClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
