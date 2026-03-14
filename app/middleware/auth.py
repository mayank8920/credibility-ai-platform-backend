# =============================================================================
# app/middleware/auth.py — Supabase token verification
# =============================================================================
#
# Uses per-request httpx client — the same pattern as the original code
# that was working in production. The original bug was that ALL exceptions
# (timeouts, network errors, bugs) were caught by a single bare
# "except Exception" and converted to 401, making every infrastructure
# failure look like an auth failure to the user.
#
# This version fixes that by catching specific exceptions and returning
# the correct HTTP status code for each failure type:
#   - Invalid/expired token from Supabase  → 401
#   - Supabase timeout or network error    → 503 (not 401)
#   - Any unexpected Python error          → 503 with full error logged
#
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
) -> dict:
    """
    Verify the Supabase JWT by calling /auth/v1/user.

    Works with all Supabase signing methods (HS256 and RS256) because
    Supabase verifies the token on their side.

    Returns the user dict with both 'id' and 'sub' set to the user UUID.
    """
    token = credentials.credentials

    logger.debug("[auth] Verifying token...")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                SUPABASE_USER_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "apikey":        settings.SUPABASE_ANON_KEY,
                },
            )

        # ── Supabase rejected the token ───────────────────────────────────────
        if response.status_code == 401:
            logger.warning("[auth] Token rejected by Supabase: 401")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired session. Please log in again.",
            )

        # ── Supabase returned an unexpected error ─────────────────────────────
        if response.status_code != 200:
            logger.error(
                f"[auth] Supabase /auth/v1/user returned {response.status_code}: "
                f"{response.text[:300]}"
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication service temporarily unavailable. Please try again.",
            )

        # ── Token is valid — extract user ─────────────────────────────────────
        user = response.json()
        user_id = user.get("id")

        if not user_id:
            logger.error(
                f"[auth] Supabase returned 200 but no 'id' in response. "
                f"Keys present: {list(user.keys())}"
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid session token.",
            )

        # Add "sub" as alias for "id" — verify.py uses .get("sub") or .get("id")
        user["sub"] = user_id

        logger.debug(f"[auth] Token verified for user={user_id[:8]}...")
        return user

    except HTTPException:
        raise   # re-raise our own exceptions as-is

    except httpx.TimeoutException:
        # Supabase took longer than 10 seconds — infrastructure issue, not user's fault
        logger.error(
            "[auth] Supabase auth check timed out after 10s. "
            "This is an infrastructure issue between Railway and Supabase."
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication timed out. Please try again in a moment.",
        )

    except httpx.RequestError as e:
        # Network error reaching Supabase
        logger.error(f"[auth] Network error reaching Supabase: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service temporarily unavailable. Please try again.",
        )

    except Exception as e:
        # Catch-all — log the full traceback so we can see it in Railway logs
        logger.error(f"[auth] Unexpected error during auth: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service temporarily unavailable. Please try again.",
        )


async def get_verified_user_id(
    current_user: dict = Depends(get_current_user),
) -> str:
    """Convenience dependency — returns just the user_id string."""
    return current_user["sub"]
