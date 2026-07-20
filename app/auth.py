from __future__ import annotations

from functools import lru_cache
from typing import Any, Optional

import jwt
from fastapi import Cookie, Depends, Header, HTTPException, Request

from app.config import settings
from app.revo_auth import IdentityVerifier

COOKIE_NAME = "revo_access"


@lru_cache(maxsize=1)
def _verifier() -> Optional[IdentityVerifier]:
    issuer = (settings.identity_issuer_url or "").strip()
    client_id = (settings.identity_client_id or "").strip()
    if not issuer or not client_id:
        return None
    return IdentityVerifier(issuer, client_id)


def identity_enabled() -> bool:
    return _verifier() is not None


def extract_token(
    request: Request,
    authorization: Optional[str] = None,
    cookie_token: Optional[str] = None,
) -> Optional[str]:
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if token:
            return token
    if cookie_token and cookie_token.strip():
        return cookie_token.strip()
    return None


def verify_token(token: str) -> dict[str, Any]:
    verifier = _verifier()
    if verifier is None:
        raise HTTPException(503, "Identity not configured")
    try:
        return verifier.verify(token)
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}") from exc


def require_identity(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    revo_access: Optional[str] = Cookie(default=None, alias=COOKIE_NAME),
) -> dict[str, Any]:
    """Require a valid IdP access JWT (Bearer or session cookie)."""
    if not identity_enabled():
        return {"sub": "local", "roles": ["admin", "uploader", "viewer"]}
    token = extract_token(request, authorization, revo_access)
    if not token:
        raise HTTPException(status_code=401, detail="Missing access token")
    return verify_token(token)


def require_uploader(
    claims: dict[str, Any] = Depends(require_identity),
) -> dict[str, Any]:
    verifier = _verifier()
    if verifier is None:
        return claims
    if not verifier.has_role(claims, "uploader", "admin"):
        raise HTTPException(status_code=403, detail="Requires role uploader or admin")
    return claims


def optional_identity(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    revo_access: Optional[str] = Cookie(default=None, alias=COOKIE_NAME),
) -> Any:
    """Return claims dict, or None when identity is on but user is anonymous."""
    if not identity_enabled():
        return {"sub": "local", "roles": ["admin", "uploader", "viewer"]}
    token = extract_token(request, authorization, revo_access)
    if not token:
        return None
    try:
        return verify_token(token)
    except HTTPException:
        return None


def require_api_key(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
) -> None:
    """
    Protect report/read APIs.

    - If API_KEY is empty (local default): open access.
    - If API_KEY is set (Railway): require matching X-API-Key header.
    """
    expected = (settings.api_key or "").strip()
    if not expected:
        return
    if not x_api_key or x_api_key.strip() != expected:
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid X-API-Key header",
        )
