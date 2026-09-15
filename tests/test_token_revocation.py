"""D-01 revocation checks: offline RFC 7662 introspection boundary tests.

No real IdP, network or database is used.  A locally verified JWT proves only
that the issuer signed the token; these tests pin down the extra step that
decides whether the token is still active.  The API-level cases stop at the
authentication boundary and therefore never reach PostgreSQL.
"""

import base64
import hashlib
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from aftercare_agent.api.app import create_app, create_default_app
from aftercare_agent.auth import (
    CachedIntrospector,
    HttpTokenIntrospector,
    IntrospectionConfig,
    IntrospectionVerdict,
    JwtJwksVerifier,
    JwtVerifierConfig,
    TokenAccessGuard,
    parse_verdict,
    token_fingerprint,
)
from aftercare_agent.domain.common import ContractViolation, ErrorCode
from aftercare_agent.persistence import Database

ISSUER = "https://idp.example"
AUDIENCE = "aftercare-api"
ENDPOINT = "https://idp.example/oauth2/introspect"
NOW = datetime(2026, 9, 15, 12, tzinfo=UTC)


def _b64(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@pytest.fixture()
def key_material() -> tuple[rsa.RSAPrivateKey, dict[str, object]]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = private.public_key().public_numbers()
    jwk: dict[str, object] = {
        "kty": "RSA",
        "kid": "key-1",
        "alg": "RS256",
        "use": "sig",
        "n": _b64(numbers.n),
        "e": _b64(numbers.e),
    }
    return private, jwk


def _config(**changes: object) -> JwtVerifierConfig:
    value: dict[str, object] = {
        "issuer": ISSUER,
        "audience": AUDIENCE,
        "jwks_url": f"{ISSUER}/.well-known/jwks.json",
        "algorithms": ("RS256",),
        "jwks_cache_seconds": 300,
        "jwks_refresh_cooldown_seconds": 5,
    }
    value.update(changes)
    return JwtVerifierConfig.model_validate(value)


def _token(private: rsa.RSAPrivateKey, **changes: object) -> str:
    payload: dict[str, object] = {
        "iss": ISSUER,
        "sub": "user-1",
        "aud": AUDIENCE,
        "tenant_id": "tenant-1",
        "scope": "case:read",
        "iat": int(NOW.timestamp()),
        "exp": int((NOW + timedelta(minutes=5)).timestamp()),
    }
    payload.update(changes)
    return jwt.encode(payload, private, algorithm="RS256", headers={"kid": "key-1", "typ": "JWT"})


def _verifier(jwk: dict[str, object]) -> JwtJwksVerifier:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"keys": [jwk]})

    client = httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=False)
    return JwtJwksVerifier(_config(), client=client)


class _FakeIntrospector:
    """Records tokens; returns a fixed verdict or raises it."""

    def __init__(self, outcome: IntrospectionVerdict | Exception) -> None:
        self.outcome = outcome
        self.calls: list[str] = []

    def introspect(self, token: str, *, now: datetime) -> IntrospectionVerdict:
        self.calls.append(token)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class _Clock:
    """Deterministic stand-in for ``time.monotonic``."""

    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


def _guard(
    jwk: dict[str, object], introspector: _FakeIntrospector | None = None
) -> TokenAccessGuard:
    return TokenAccessGuard(_verifier(jwk), introspector=introspector)


def _introspector(
    handler: Callable[[httpx.Request], httpx.Response], **changes: object
) -> tuple[HttpTokenIntrospector, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.Client(transport=httpx.MockTransport(capture), follow_redirects=False)
    config = IntrospectionConfig(endpoint=ENDPOINT, **changes)  # type: ignore[arg-type]
    return HttpTokenIntrospector(config, client=client), seen


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://idp.example/introspect",
        "https://idp.example/introspect?token=abc",
        "https://idp.example/introspect#fragment",
        "https:///introspect",
        "introspect",
    ],
)
def test_endpoint_must_be_static_https(endpoint: str) -> None:
    """The endpoint is deployment config and can never be token-controlled."""
    with pytest.raises(ValueError, match="https"):
        IntrospectionConfig(endpoint=endpoint)


@pytest.mark.parametrize(
    "changes",
    [
        {"request_timeout_seconds": 0},
        {"request_timeout_seconds": 60},
        {"cache_seconds": -1},
        {"cache_seconds": 3600},
        {"max_cache_entries": 0},
        {"max_response_bytes": 64},
    ],
)
def test_policy_bounds_are_enforced(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        IntrospectionConfig(endpoint=ENDPOINT, **changes)  # type: ignore[arg-type]


def test_default_policy_is_bounded() -> None:
    config = IntrospectionConfig(endpoint=ENDPOINT)
    assert config.request_timeout_seconds == 3.0
    assert config.cache_seconds == 5
    assert config.max_cache_entries == 4096
    assert config.max_response_bytes == 262_144


def test_credentials_are_not_part_of_the_contract_model() -> None:
    """Secrets live in the injected client, never in a validated contract."""
    assert set(IntrospectionConfig.model_fields) == {
        "endpoint",
        "request_timeout_seconds",
        "cache_seconds",
        "max_cache_entries",
        "max_response_bytes",
    }


def test_token_fingerprint_is_stable_and_not_reversible() -> None:
    fingerprint = token_fingerprint("secret-token")
    assert fingerprint == token_fingerprint("secret-token")
    assert fingerprint != token_fingerprint("secret-token-2")
    assert fingerprint == hashlib.sha256(b"secret-token").hexdigest()
    assert "secret-token" not in fingerprint


def test_inactive_verdict_carries_no_claims() -> None:
    verdict = parse_verdict({"active": False, "sub": "user-1", "tenant_id": "tenant-1"})
    assert verdict.active is False
    assert verdict.subject_id is None
    assert verdict.tenant_id is None


def test_active_verdict_reads_only_documented_fields() -> None:
    expires = NOW + timedelta(minutes=1)
    verdict = parse_verdict(
        {
            "active": True,
            "sub": "user-1",
            "tenant_id": "tenant-1",
            "exp": int(expires.timestamp()),
            "scope": "case:read",
            "client_id": "workbench",
            "jti": "opaque",
        }
    )
    assert verdict.active is True
    assert verdict.subject_id == "user-1"
    assert verdict.tenant_id == "tenant-1"
    # RFC 7662 leaves the field set to the issuer; unknown keys are tolerated.
    assert verdict.expires_at == expires


@pytest.mark.parametrize(
    "payload",
    [
        "not-an-object",
        ["active", True],
        {"sub": "user-1"},
        {"active": "true"},
        {"active": 1},
        {"active": None},
        {},
    ],
)
def test_a_missing_or_non_boolean_active_flag_is_rejected(payload: object) -> None:
    with pytest.raises(ContractViolation) as error:
        parse_verdict(payload)
    assert error.value.code is ErrorCode.UNAUTHENTICATED


@pytest.mark.parametrize(
    "payload",
    [
        {"active": True, "exp": "1234567890"},
        {"active": True, "exp": True},
        {"active": True, "exp": float("nan")},
        {"active": True, "exp": float("inf")},
        {"active": True, "exp": 1e308 * 10},
        {"active": True, "sub": 7},
        {"active": True, "sub": "user with spaces"},
        {"active": True, "tenant_id": ""},
    ],
)
def test_ill_typed_claims_are_rejected_rather_than_coerced(payload: object) -> None:
    with pytest.raises(ContractViolation) as error:
        parse_verdict(payload)
    assert error.value.code is ErrorCode.UNAUTHENTICATED


def test_introspection_posts_the_token_as_a_form_field() -> None:
    introspector, seen = _introspector(lambda _: httpx.Response(200, json={"active": True}))
    verdict = introspector.introspect("secret-token", now=NOW)
    assert verdict.active is True
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == ENDPOINT
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert request.headers["accept"] == "application/json"
    body = request.content.decode()
    assert "token=secret-token" in body
    assert "token_type_hint=access_token" in body


def test_the_caller_supplies_the_provider_credentials() -> None:
    """The introspector never invents credentials; they belong to the client."""
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"active": True})

    client = httpx.Client(
        transport=httpx.MockTransport(handle),
        follow_redirects=False,
        auth=httpx.BasicAuth("aftercare-api", "provider-secret"),
    )
    introspector = HttpTokenIntrospector(IntrospectionConfig(endpoint=ENDPOINT), client=client)
    introspector.introspect("token-1", now=NOW)
    assert seen[0].headers["authorization"].startswith("Basic ")


@pytest.mark.parametrize(
    "handler",
    [
        lambda _: httpx.Response(500, text="provider error"),
        lambda _: httpx.Response(401, text="bad credentials"),
        lambda _: httpx.Response(200, text="not json"),
        lambda _: httpx.Response(200, json=[1, 2, 3]),
        lambda _: httpx.Response(200, json={}),
        lambda _: httpx.Response(302, headers={"location": "https://elsewhere.example"}),
    ],
)
def test_provider_failures_are_authentication_failures(
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    introspector, _ = _introspector(handler)
    with pytest.raises(ContractViolation) as error:
        introspector.introspect("token-1", now=NOW)
    assert error.value.code is ErrorCode.UNAUTHENTICATED


def test_transport_failure_is_an_authentication_failure() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("idp unreachable", request=request)

    introspector, _ = _introspector(handle)
    with pytest.raises(ContractViolation) as error:
        introspector.introspect("token-1", now=NOW)
    assert error.value.code is ErrorCode.UNAUTHENTICATED


def test_unparseable_declared_length_fails_closed() -> None:
    introspector, _ = _introspector(
        lambda _: httpx.Response(200, json={"active": True}, headers={"content-length": "many"})
    )
    with pytest.raises(ContractViolation) as error:
        introspector.introspect("token-1", now=NOW)
    assert error.value.code is ErrorCode.UNAUTHENTICATED


def test_oversized_response_is_rejected_before_parsing() -> None:
    oversized = {"active": True, "padding": "x" * 4096}
    introspector, _ = _introspector(
        lambda _: httpx.Response(200, json=oversized), max_response_bytes=1024
    )
    with pytest.raises(ContractViolation) as error:
        introspector.introspect("token-1", now=NOW)
    assert error.value.code is ErrorCode.UNAUTHENTICATED


class _CountingIntrospector:
    def __init__(self, verdict: IntrospectionVerdict) -> None:
        self.verdict = verdict
        self.tokens: list[str] = []

    def introspect(self, token: str, *, now: datetime) -> IntrospectionVerdict:
        self.tokens.append(token)
        return self.verdict


def test_repeated_checks_hit_the_provider_once_inside_the_ttl() -> None:
    inner = _CountingIntrospector(IntrospectionVerdict(active=True))
    cache = CachedIntrospector(inner, ttl_seconds=5, max_entries=8)
    for _ in range(3):
        assert cache.introspect("token-1", now=NOW).active is True
    assert inner.tokens == ["token-1"]


def test_a_distinct_token_always_calls_the_provider() -> None:
    inner = _CountingIntrospector(IntrospectionVerdict(active=True))
    cache = CachedIntrospector(inner, ttl_seconds=5, max_entries=8)
    cache.introspect("token-1", now=NOW)
    cache.introspect("token-2", now=NOW)
    assert inner.tokens == ["token-1", "token-2"]


def test_an_entry_expires_after_its_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(time, "monotonic", clock)
    inner = _CountingIntrospector(IntrospectionVerdict(active=True))
    cache = CachedIntrospector(inner, ttl_seconds=5, max_entries=8)
    cache.introspect("token-1", now=NOW)
    clock.value += 4
    cache.introspect("token-1", now=NOW)
    assert len(inner.tokens) == 1
    clock.value += 2
    cache.introspect("token-1", now=NOW)
    assert len(inner.tokens) == 2
    assert len(cache) == 1


def test_caching_can_be_disabled() -> None:
    inner = _CountingIntrospector(IntrospectionVerdict(active=True))
    cache = CachedIntrospector(inner, ttl_seconds=0, max_entries=8)
    cache.introspect("token-1", now=NOW)
    cache.introspect("token-1", now=NOW)
    assert len(inner.tokens) == 2
    assert len(cache) == 0


def test_an_already_expired_verdict_is_never_cached() -> None:
    """A verdict that is expired on arrival must not be replayed for its TTL."""
    inner = _CountingIntrospector(IntrospectionVerdict(active=True, expires_at=NOW))
    cache = CachedIntrospector(inner, ttl_seconds=300, max_entries=8)
    cache.introspect("token-1", now=NOW)
    cache.introspect("token-1", now=NOW)
    assert len(inner.tokens) == 2
    assert len(cache) == 0


def test_cache_is_bounded_and_evicts_the_oldest_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(time, "monotonic", clock)
    inner = _CountingIntrospector(IntrospectionVerdict(active=True))
    cache = CachedIntrospector(inner, ttl_seconds=300, max_entries=2)
    for index in range(3):
        clock.value += 1
        cache.introspect(f"token-{index}", now=NOW)
    assert len(cache) == 2
    cache.introspect("token-0", now=NOW)
    assert inner.tokens == ["token-0", "token-1", "token-2", "token-0"]


def test_the_cache_never_stores_the_token_itself() -> None:
    inner = _CountingIntrospector(IntrospectionVerdict(active=True))
    cache = CachedIntrospector(inner, ttl_seconds=30, max_entries=8)
    cache.introspect("super-secret-token", now=NOW)
    assert list(cache._entries) == [token_fingerprint("super-secret-token")]
    assert "super-secret-token" not in repr(cache._entries)


@pytest.mark.parametrize("changes", [{"ttl_seconds": -1}, {"max_entries": 0}])
def test_invalid_cache_policy_is_rejected(changes: dict[str, int]) -> None:
    inner = _CountingIntrospector(IntrospectionVerdict(active=True))
    values: dict[str, int] = {"ttl_seconds": 5, "max_entries": 8}
    values.update(changes)
    with pytest.raises(ContractViolation) as error:
        CachedIntrospector(inner, **values)
    assert error.value.code is ErrorCode.INVALID_INPUT


def test_a_revoked_token_is_rejected_after_the_signature_verifies(
    key_material: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    private, jwk = key_material
    guard = _guard(jwk, _FakeIntrospector(IntrospectionVerdict(active=False)))
    with pytest.raises(ContractViolation) as error:
        guard.authorize(f"Bearer {_token(private)}", now=NOW)
    assert error.value.code is ErrorCode.UNAUTHENTICATED


def test_an_active_token_is_accepted_and_the_provider_is_consulted(
    key_material: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    private, jwk = key_material
    introspector = _FakeIntrospector(
        IntrospectionVerdict(
            active=True,
            subject_id="user-1",
            tenant_id="tenant-1",
            expires_at=NOW + timedelta(minutes=4),
        )
    )
    context = _guard(jwk, introspector).authorize(f"Bearer {_token(private)}", now=NOW)
    assert context.subject_id == "user-1"
    assert context.tenant_id == "tenant-1"
    assert introspector.calls == [_token(private)]


def test_without_an_introspector_no_revocation_check_happens(
    key_material: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    private, jwk = key_material
    assert _guard(jwk).authorize(f"Bearer {_token(private)}", now=NOW).subject_id == "user-1"


@pytest.mark.parametrize(
    "verdict",
    [
        IntrospectionVerdict(active=True, subject_id="someone-else"),
        IntrospectionVerdict(active=True, tenant_id="other-tenant"),
        IntrospectionVerdict(active=True, expires_at=NOW),
    ],
)
def test_a_verdict_that_contradicts_the_token_is_rejected(
    key_material: tuple[rsa.RSAPrivateKey, dict[str, object]], verdict: IntrospectionVerdict
) -> None:
    private, jwk = key_material
    guard = _guard(jwk, _FakeIntrospector(verdict))
    with pytest.raises(ContractViolation) as error:
        guard.authorize(f"Bearer {_token(private)}", now=NOW)
    assert error.value.code is ErrorCode.UNAUTHENTICATED


def test_provider_outage_fails_closed(
    key_material: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    private, jwk = key_material
    guard = _guard(jwk, _FakeIntrospector(RuntimeError("provider exploded")))
    with pytest.raises(ContractViolation) as error:
        guard.authorize(f"Bearer {_token(private)}", now=NOW)
    assert error.value.code is ErrorCode.UNAUTHENTICATED


def test_a_bad_bearer_never_reaches_the_provider(
    key_material: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    _, jwk = key_material
    introspector = _FakeIntrospector(IntrospectionVerdict(active=True))
    with pytest.raises(ContractViolation):
        _guard(jwk, introspector).authorize("Bearer not.a.jwt", now=NOW)
    assert introspector.calls == []


def test_a_provider_contract_violation_is_preserved(
    key_material: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    private, jwk = key_material
    failure = ContractViolation(ErrorCode.UNAUTHENTICATED, "provider rejected the token")
    guard = _guard(jwk, _FakeIntrospector(failure))
    with pytest.raises(ContractViolation, match="provider rejected the token"):
        guard.authorize(f"Bearer {_token(private)}", now=NOW)


def _api_client(
    jwk: dict[str, object],
    introspector: _FakeIntrospector | None,
    *,
    allow_synthetic: bool = False,
) -> TestClient:
    """An app whose auth boundary runs before any PostgreSQL access."""
    return TestClient(
        create_app(
            Database("postgresql://unused"),
            allow_synthetic=allow_synthetic,
            oidc_verifier=_verifier(jwk),
            introspector=introspector,
        )
    )


def test_api_rejects_a_revoked_bearer_token(
    key_material: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    private, jwk = key_material
    response = _api_client(jwk, _FakeIntrospector(IntrospectionVerdict(active=False))).get(
        "/v1/cases/case-1/runs/run-1", headers={"Authorization": f"Bearer {_token(private)}"}
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "unauthenticated"
    assert response.headers["www-authenticate"] == "Bearer"


def test_api_does_not_downgrade_a_revoked_token_to_a_synthetic_identity(
    key_material: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    private, jwk = key_material
    client = _api_client(
        jwk,
        _FakeIntrospector(IntrospectionVerdict(active=False)),
        allow_synthetic=True,
    )
    response = client.get(
        "/v1/cases/case-1/runs/run-1",
        headers={
            "Authorization": f"Bearer {_token(private)}",
            "X-Synthetic-Tenant": "tenant-1",
            "X-Synthetic-Subject": "user-1",
        },
    )
    assert response.status_code == 401


def test_api_rejects_a_token_whose_introspection_subject_differs(
    key_material: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    private, jwk = key_material
    client = _api_client(
        jwk, _FakeIntrospector(IntrospectionVerdict(active=True, subject_id="other-user"))
    )
    response = client.get(
        "/v1/cases/case-1/runs/run-1", headers={"Authorization": f"Bearer {_token(private)}"}
    )
    assert response.status_code == 401


def test_api_rejects_a_token_when_the_provider_is_unavailable(
    key_material: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    private, jwk = key_material
    client = _api_client(jwk, _FakeIntrospector(httpx.ConnectError("idp unreachable")))
    response = client.get(
        "/v1/cases/case-1/runs/run-1", headers={"Authorization": f"Bearer {_token(private)}"}
    )
    assert response.status_code == 401


OIDC_ENV = {
    "AFTERCARE_OIDC_ISSUER": ISSUER,
    "AFTERCARE_OIDC_AUDIENCE": AUDIENCE,
    "AFTERCARE_OIDC_JWKS_URL": f"{ISSUER}/.well-known/jwks.json",
}
INTROSPECTION_ENV = (
    "AFTERCARE_OIDC_INTROSPECTION_URL",
    "AFTERCARE_OIDC_INTROSPECTION_CLIENT_ID",
    "AFTERCARE_OIDC_INTROSPECTION_CLIENT_SECRET",
)


def _environment(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    for name in (
        "AFTERCARE_ALLOW_SYNTHETIC_IDENTITY",
        "AFTERCARE_OIDC_REQUIRE_CASE_IDS",
        *INTROSPECTION_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_introspection_url_requires_real_oidc_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(monkeypatch, AFTERCARE_OIDC_INTROSPECTION_URL=ENDPOINT)
    with pytest.raises(RuntimeError, match="OIDC issuer"):
        create_default_app()


@pytest.mark.parametrize(
    "missing",
    ["AFTERCARE_OIDC_INTROSPECTION_CLIENT_ID", "AFTERCARE_OIDC_INTROSPECTION_CLIENT_SECRET"],
)
def test_introspection_url_requires_client_credentials(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    credentials = {
        "AFTERCARE_OIDC_INTROSPECTION_CLIENT_ID": "aftercare-api",
        "AFTERCARE_OIDC_INTROSPECTION_CLIENT_SECRET": "provider-secret",
    }
    credentials[missing] = ""
    _environment(
        monkeypatch,
        **OIDC_ENV,
        AFTERCARE_OIDC_INTROSPECTION_URL=ENDPOINT,
        **credentials,
    )
    with pytest.raises(RuntimeError, match="CLIENT_SECRET"):
        create_default_app()


def test_introspection_url_must_be_https(monkeypatch: pytest.MonkeyPatch) -> None:
    _environment(
        monkeypatch,
        **OIDC_ENV,
        AFTERCARE_OIDC_INTROSPECTION_URL="http://idp.example/introspect",
        AFTERCARE_OIDC_INTROSPECTION_CLIENT_ID="aftercare-api",
        AFTERCARE_OIDC_INTROSPECTION_CLIENT_SECRET="provider-secret",
    )
    with pytest.raises(RuntimeError, match="https"):
        create_default_app()


def test_no_shutdown_hook_is_registered_without_introspection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(monkeypatch, **OIDC_ENV)
    assert create_default_app().router.on_shutdown == []


def test_configured_introspection_registers_an_owned_client_shutdown_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(
        monkeypatch,
        **OIDC_ENV,
        AFTERCARE_OIDC_INTROSPECTION_URL=ENDPOINT,
        AFTERCARE_OIDC_INTROSPECTION_CLIENT_ID="aftercare-api",
        AFTERCARE_OIDC_INTROSPECTION_CLIENT_SECRET="provider-secret",
    )
    app = create_default_app()
    assert len(app.router.on_shutdown) == 1
    app.router.on_shutdown[0]()  # closes the owned httpx client; safe to repeat
