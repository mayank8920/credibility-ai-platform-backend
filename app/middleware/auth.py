# =============================================================================
# app/middleware/auth.py — Supabase token verification via anon client
# =============================================================================
#
# Uses the anon client singleton from database.py to call get_user(jwt).
# The anon client's auth.get_user(jwt) is the correct method for verifying
# a user's JWT token — it works with both HS256 and RS256 signed tokens.
#
# WHY ANON CLIENT, NOT ADMIN CLIENT:
#   The admin client (SERVICE_ROLE_KEY) has a different auth API.
#   Its get_user() takes a user UUID, not a JWT token.
#   The anon client (ANON_KEY) has the standard GoTrue auth API where
#   get_user(jwt) accepts a token and returns the user if valid.
#
# PERFORMANCE:
#   Uses the singleton connection from get_anon() — one persistent
#   connection reused across all requests, no TCP/TLS handshake per call.
#
# =============================================================================

import logging

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.services.database import get_anon

logger = logging.getLogger(__name__)

security = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    """
    Verify the Supabase JWT token using the anon client's get_user(jwt).

    Works with all Supabase signing methods (HS256 legacy and RS256 new).
    Returns a user dict with both 'id' and 'sub' set to the user's UUID.

    Raises HTTP 401 for invalid/expired tokens.
    Raises HTTP 503 if Supabase itself is unreachable.
    """
    token = credentials.credentials

    try:
        db = get_anon()
        response = db.auth.get_user(token)

        if response is None or response.user is None:
            logger.warning("[auth] Token rejected: get_user returned no user")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired session. Please log in again.",
            )

        user = response.user

        # Expose both "id" and "sub" pointing to the same UUID.
        # verify.py uses current_user.get("sub") or current_user.get("id")
        # — both will work correctly with this dict.
        user_dict = {
            "id":            user.id,
            "sub":           user.id,
            "email":         user.email,
            "role":          "authenticated",
            "phone":         getattr(user, "phone", None),
            "user_metadata": getattr(user, "user_metadata", {}),
            "app_metadata":  getattr(user, "app_metadata", {}),
        }

        logger.debug(f"[auth] Token verified for user={user.id[:8]}...")
        return user_dict

    except HTTPException:
        raise

    except Exception as e:
        error_str = str(e).lower()

        if "invalid" in error_str or "expired" in error_str or "jwt" in error_str:
            logger.warning(f"[auth] Token rejected: {e}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired session. Please log in again.",
            )

        logger.error(
            f"[auth] Supabase auth check failed: {e}",
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service temporarily unavailable. Please try again.",
        )


async def get_verified_user_id(
    current_user: dict = Depends(get_current_user),
) -> str:
    """Convenience dependency — returns just the user_id string."""
    return current_user["sub"]
