# =============================================================================
# app/middleware/auth.py — Supabase token verification (sync, thread pool safe)
# =============================================================================
#
# IMPORTANT: This is a sync function (def, not async def).
# FastAPI runs sync dependencies in a thread pool automatically.
# This prevents supabase-py's blocking httpx calls from blocking
# the async event loop, which was causing 500 errors with no logs.
#
# =============================================================================

import requests as req
import logging

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.config import settings

logger = logging.getLogger(__name__)

security = HTTPBearer()

SUPABASE_USER_URL = f"{settings.SUPABASE_URL}/auth/v1/user"


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    token = credentials.credentials
    logger.info("[auth] Verifying token...")

    try:
        response = req.get(
            SUPABASE_USER_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "apikey": settings.SUPABASE_ANON_KEY,
            },
            timeout=10,
        )

        logger.info(f"[auth] Supabase status: {response.status_code}")

        if response.status_code == 401:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired session. Please log in again.",
            )

        if response.status_code != 200:
            logger.error(f"[auth] Supabase error: {response.status_code} {response.text[:200]}")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication service temporarily unavailable.",
            )

        user = response.json()
        user_id = user.get("id")

        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid session token.",
            )

        user["sub"] = user_id
        logger.info(f"[auth] Verified user={user_id[:8]}...")
        return user

    except HTTPException:
        raise

    except req.exceptions.Timeout:
        logger.error("[auth] Supabase timeout")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication timed out. Please try again.",
        )

    except Exception as e:
        logger.error(f"[auth] Unexpected error: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service temporarily unavailable.",
        )


def get_verified_user_id(
    current_user: dict = Depends(get_current_user),
) -> str:
    return current_user["sub"]
