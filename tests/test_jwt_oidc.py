"""Offline JWT/JWKS boundary tests; no real IdP or network is used."""

import base64
from datetime import UTC, datetime, timedelta

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from aftercare_agent.auth.oidc import JwtJwksVerifier, JwtVerifierConfig
from aftercare_agent.domain.common import ContractViolation, ErrorCode

ISSUER = "https://idp.example"
AUDIENCE = "aftercare-api"
NOW = datetime(2026, 9, 13, 12, tzinfo=UTC)


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
        "scope": "case:read review:read",
        "case_ids": ["case-1"],
        "iat": int(NOW.timestamp()),
        "exp": int((NOW + timedelta(minutes=5)).timestamp()),
    }
    payload.update(changes)
    return jwt.encode(payload, private, algorithm="RS256", headers={"kid": "key-1", "typ": "JWT"})


def _verifier(
    jwk: dict[str, object], *, config: JwtVerifierConfig | None = None
) -> tuple[JwtJwksVerifier, list[int]]:
    calls: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"keys": [jwk]})

    client = httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=False)
    return JwtJwksVerifier(config or _config(), client=client), calls


def test_valid_bearer_verifies_signature_and_maps_scopes(
    key_material: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    private, jwk = key_material
    verifier, calls = _verifier(jwk)
    context = verifier.verify(f"Bearer {_token(private)}", now=NOW)
    assert context.subject_id == "user-1"
    assert context.tenant_id == "tenant-1"
    assert context.permissions == frozenset({"case:read", "review:read"})
    assert context.case_ids == frozenset({"case-1"})
    assert calls == [1]


def test_audience_array_is_supported(
    key_material: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    private, jwk = key_material
    verifier, _ = _verifier(jwk)
    audience = ["other-api", AUDIENCE]
    context = verifier.verify(f"Bearer {_token(private, aud=audience)}", now=NOW)
    assert context.tenant_id == "tenant-1"


@pytest.mark.parametrize(
    "authorization",
    ["", "Basic abc", "Bearer", "Bearer a b", "bearer   ", "Bearer not.a.jwt"],
)
def test_bearer_parser_rejects_ambiguous_values(
    key_material: tuple[rsa.RSAPrivateKey, dict[str, object]], authorization: str
) -> None:
    _, jwk = key_material
    verifier, _ = _verifier(jwk)
    with pytest.raises(ContractViolation) as error:
        verifier.verify(authorization, now=NOW)
    assert error.value.code is ErrorCode.UNAUTHENTICATED


@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "https://other.example"},
        {"aud": "other-api"},
        {"exp": int((NOW - timedelta(seconds=31)).timestamp())},
        {"nbf": int((NOW + timedelta(minutes=1)).timestamp())},
        {"iat": int((NOW + timedelta(minutes=1)).timestamp())},
        {"exp": int((NOW + timedelta(hours=2)).timestamp())},
    ],
)
def test_claim_policy_rejects_wrong_issuer_audience_time_or_lifetime(
    key_material: tuple[rsa.RSAPrivateKey, dict[str, object]], changes: dict[str, object]
) -> None:
    private, jwk = key_material
    verifier, _ = _verifier(jwk)
    with pytest.raises(ContractViolation) as error:
        verifier.verify(f"Bearer {_token(private, **changes)}", now=NOW)
    assert error.value.code is ErrorCode.UNAUTHENTICATED


def test_unknown_kid_refreshes_once_then_fails_closed(
    key_material: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    private, jwk = key_material
    verifier, calls = _verifier(jwk)
    token = jwt.encode(
        {
            "iss": ISSUER,
            "sub": "user-1",
            "aud": AUDIENCE,
            "tenant_id": "tenant-1",
            "iat": int(NOW.timestamp()),
            "exp": int((NOW + timedelta(minutes=5)).timestamp()),
        },
        private,
        algorithm="RS256",
        headers={"kid": "unknown", "typ": "JWT"},
    )
    for _ in range(2):
        with pytest.raises(ContractViolation):
            verifier.verify(f"Bearer {token}", now=NOW)
    assert calls == [1]


def test_jwks_failure_is_auth_failure_and_does_not_use_stale_key(
    key_material: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    private, jwk = key_material
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"keys": [jwk]})
        return httpx.Response(503, text="unavailable")

    client = httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=False)
    verifier = JwtJwksVerifier(_config(jwks_cache_seconds=1), client=client)
    token = f"Bearer {_token(private)}"
    assert verifier.verify(token, now=NOW).subject_id == "user-1"
    # Expire the cache without sleeping by changing its monotonic timestamp.
    verifier._fetched_at -= 2
    with pytest.raises(ContractViolation) as error:
        verifier.verify(token, now=NOW)
    assert error.value.code is ErrorCode.UNAUTHENTICATED


def test_non_https_jwks_url_is_rejected() -> None:
    with pytest.raises(ValueError, match="https"):
        _config(jwks_url="http://idp.example/jwks")


def test_case_ids_are_an_optional_token_attenuation_bound() -> None:
    assert _config().require_case_ids is False


def test_case_scope_claim_has_a_bounded_size(
    key_material: tuple[rsa.RSAPrivateKey, dict[str, object]],
) -> None:
    private, jwk = key_material
    verifier, _ = _verifier(jwk, config=_config(max_case_ids=1))
    token = _token(private, case_ids=["case-1", "case-2"])
    with pytest.raises(ContractViolation) as error:
        verifier.verify(f"Bearer {token}", now=NOW)
    assert error.value.code is ErrorCode.UNAUTHENTICATED
