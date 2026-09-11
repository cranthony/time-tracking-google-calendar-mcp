import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from workos_auth import WorkOSTokenVerifier

AUTHKIT_DOMAIN = "https://example.authkit.app"
RESOURCE = "https://my-service.onrender.com/mcp"


@pytest.fixture(scope="module")
def keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _token(private_key, **claim_overrides) -> str:
    claims = {
        "iss": AUTHKIT_DOMAIN,
        "aud": RESOURCE,
        "sub": "user_123",
        "client_id": "client_abc",
        "scope": "",
        "exp": int(time.time()) + 300,
    }
    claims.update(claim_overrides)
    return jwt.encode(claims, private_key, algorithm="RS256")


def _verifier(public_key) -> WorkOSTokenVerifier:
    verifier = WorkOSTokenVerifier(authkit_domain=AUTHKIT_DOMAIN, resource=RESOURCE)
    verifier._jwks_client.get_signing_key_from_jwt = lambda token: SimpleNamespace(key=public_key)
    return verifier


class TestWorkOSTokenVerifier:
    async def test_valid_token_returns_access_token(self, keypair):
        private_key, public_key = keypair
        verifier = _verifier(public_key)

        result = await verifier.verify_token(_token(private_key))

        assert result is not None
        assert result.client_id == "client_abc"
        assert result.resource == RESOURCE

    async def test_expired_token_is_rejected(self, keypair):
        private_key, public_key = keypair
        verifier = _verifier(public_key)
        token = _token(private_key, exp=int(time.time()) - 60)

        assert await verifier.verify_token(token) is None

    async def test_wrong_issuer_is_rejected(self, keypair):
        private_key, public_key = keypair
        verifier = _verifier(public_key)
        token = _token(private_key, iss="https://not-workos.example")

        assert await verifier.verify_token(token) is None

    async def test_wrong_audience_is_rejected(self, keypair):
        private_key, public_key = keypair
        verifier = _verifier(public_key)
        token = _token(private_key, aud="https://someone-elses-server.example/mcp")

        assert await verifier.verify_token(token) is None

    async def test_signed_by_a_different_key_is_rejected(self, keypair):
        _, public_key = keypair
        other_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        verifier = _verifier(public_key)
        token = _token(other_private_key)

        assert await verifier.verify_token(token) is None

    async def test_garbage_token_is_rejected(self, keypair):
        _, public_key = keypair
        verifier = _verifier(public_key)

        assert await verifier.verify_token("not-a-jwt") is None

    async def test_scope_claim_is_split_into_a_list(self, keypair):
        private_key, public_key = keypair
        verifier = _verifier(public_key)
        token = _token(private_key, scope="calendar:read calendar:write")

        result = await verifier.verify_token(token)

        assert result.scopes == ["calendar:read", "calendar:write"]
