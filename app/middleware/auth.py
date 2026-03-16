# =============================================================================
# app/middleware/auth.py — Supabase token verification (PRODUCTION SAFE)
# =============================================================================

import httpx
import logging

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.config import settings

logger = logging.getLogger(__name__)

security = HTTPBearer()

SUPABASE_USER_URL = f"{settings.SUPABASE_URL}/auth/v1/user"


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    token = credentials.credentials

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                SUPABASE_USER_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "apikey": settings.SUPABASE_ANON_KEY,
                },
            )

        if response.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired session",
            )

        return response.json()

    except Exception as e:
        logger.error(f"Auth validation failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed",
        )


async def get_verified_user_id(
    current_user: dict = Depends(get_current_user),
):
    user_id = current_user.get("id")

    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User ID missing",
        )

    return user_id
