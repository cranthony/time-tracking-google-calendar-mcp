"""Verifying bearer tokens issued by WorkOS AuthKit.

This app never issues tokens or holds a password: over the streamable-HTTP
transport, this server is an OAuth 2.1 *Resource Server* only. WorkOS
AuthKit is the *Authorization Server* -- it handles Claude.ai's Dynamic
Client Registration, the PKCE authorization-code flow, and token issuance.
All this module does is decide whether a bearer token WorkOS issued is
still valid: check its signature against WorkOS's published JWKS, and that
its issuer/audience/expiry are right.

See the README's "Deploying" section for the WorkOS AuthKit setup this
depends on (enabling Dynamic Client Registration/Client ID Metadata
Document, and registering this server's URL as a Resource Indicator).
"""

from __future__ import annotations

import asyncio

import jwt
from mcp.server.auth.provider import AccessToken, TokenVerifier


class WorkOSTokenVerifier(TokenVerifier):
    """Verifies a bearer token was issued by `authkit_domain` for
    `resource` (this server's own public URL) -- see the module docstring.
    """

    def __init__(self, *, authkit_domain: str, resource: str) -> None:
        self._authkit_domain = authkit_domain
        self._resource = resource
        # cache_keys=True: fetch WorkOS's signing keys once and reuse them
        # by `kid` for `lifespan` seconds, rather than hitting the network
        # on every single request.
        self._jwks_client = jwt.PyJWKClient(f"{authkit_domain}/oauth2/jwks", cache_keys=True)

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            # jwt.PyJWKClient/jwt.decode do a blocking network call (at
            # least the first time, and whenever the key cache expires) --
            # run them off the event loop rather than stalling it.
            claims = await asyncio.to_thread(self._verify, token)
        except jwt.PyJWTError:
            return None
        return AccessToken(
            token=token,
            client_id=claims.get("client_id", claims.get("sub", "")),
            scopes=claims.get("scope", "").split(),
            expires_at=claims.get("exp"),
            resource=self._resource,
        )

    def _verify(self, token: str) -> dict:
        signing_key = self._jwks_client.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=self._authkit_domain,
            audience=self._resource,
            options={"require": ["exp", "iss", "aud"]},
        )
