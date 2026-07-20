"""Shared JWT verification helpers for REVO apps consuming revo-identity."""

from __future__ import annotations

import time
from typing import Any

import jwt
from jwt import PyJWKClient


class IdentityVerifier:
    def __init__(self, issuer_url: str, audience: str, *, jwks_cache_seconds: int = 300):
        self.issuer = issuer_url.rstrip("/")
        self.audience = audience
        self._jwks = PyJWKClient(f"{self.issuer}/.well-known/jwks.json", cache_keys=True)
        self._jwks_cache_seconds = jwks_cache_seconds
        self._last_fetch = 0.0

    def verify(self, token: str) -> dict[str, Any]:
        signing_key = self._jwks.get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=self.audience,
            issuer=self.issuer,
            options={"require": ["exp", "sub", "iss", "aud"]},
        )
        if payload.get("typ") != "access":
            raise jwt.InvalidTokenError("Invalid token type")
        return payload

    def has_role(self, payload: dict[str, Any], *roles: str) -> bool:
        have = {str(r).lower() for r in payload.get("roles") or []}
        return any(r.lower() in have for r in roles)
