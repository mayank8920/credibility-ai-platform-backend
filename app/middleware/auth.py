# =============================================================================
# app/middleware/auth.py — Supabase token verification (synchronous)
# =============================================================================
#
# Uses the synchronous `requests` library instead of async httpx.
# FastAPI automatically runs synchronous dependencies in a thread pool,
# so this is fully non-blocking from FastAPI's perspective.
#
# WHY SYNC INSTEAD OF ASYNC:
#   httpx.AsyncClient inside async FastAPI dependencies can silently fail
#   at the transport layer in certain Railway + Python 3.11 + uvicorn
#   configurations — producing a 500 with no logs and no CORS headers
#   because the failure happens below FastAPI's exception handling layer.
#
#   The synchronous `requests` library has no asyncio dependency and no
#   event loop interaction — it works reliably in all environments.
#   `requests` is already in requirements.txt so no new packages needed.
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
    """
    Verify the Supabase JWT by calling /auth/v1/user.

    Synchronous function — FastAPI runs it in a thread pool automatically.
    Works with all Supabase signing methods (HS256 and RS256).

    Returns the user dict with both 'id' and 'sub' set to the user UUID.
    """
    token = credentials.credentials

    logger.info("[auth] Verifying token...")

    try:
        response = req.get(
            SUPABASE_USER_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "apikey":        settings.SUPABASE_ANON_KEY,
            },
            timeout=10,
        )

        logger.info(f"[auth] Supabase responded with status {response.status_code}")

        if response.status_code == 401:
            logger.warning("[auth] Token rejected by Supabase: 401")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired session. Please log in again.",
            )

        if response.status_code != 200:
            logger.error(
                f"[auth] Supabase returned unexpected status {response.status_code}: "
                f"{response.text[:200]}"
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication service temporarily unavailable. Please try again.",
            )

        user = response.json()
        user_id = user.get("id")

        if not user_id:
            logger.error(f"[auth] No 'id' in Supabase response. Keys: {list(user.keys())}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid session token.",
            )

        user["sub"] = user_id
        logger.info(f"[auth] Token verified for user={user_id[:8]}...")
        return user

    except HTTPException:
        raise

    except req.exceptions.Timeout:
        logger.error("[auth] Supabase auth check timed out after 10s")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication timed out. Please try again.",
        )

    except req.exceptions.RequestException as e:
        logger.error(f"[auth] Network error reaching Supabase: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service temporarily unavailable. Please try again.",
        )

    except Exception as e:
        logger.error(f"[auth] Unexpected error: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service temporarily unavailable. Please try again.",
        )


def get_verified_user_id(
    current_user: dict = Depends(get_current_user),
) -> str:
    """Convenience dependency — returns just the user_id string."""
    return current_user["sub"]
