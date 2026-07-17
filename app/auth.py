from __future__ import annotations

from typing import Optional

from fastapi import Header, HTTPException

from app.config import settings


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
