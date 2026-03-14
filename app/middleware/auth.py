# =============================================================================
# app/middleware/auth.py — Supabase token verification via admin client
# =============================================================================
#
# WHY THIS APPROACH:
#
#   The local JWT verification approach (PyJWT with HS256) stopped working
#   because Supabase migrated this project to new JWT Signing Keys (RS256
#   asymmetric signing). New tokens are signed with a private key — the
#   legacy HS256 secret can no longer verify them.
#
#   This version uses the Supabase admin client that is already initialised
#   as a singleton in database.py. It calls get_user(token) which works
#   regardless of whether Supabase uses HS256 or RS256 — Supabase handles
#   the verification internally on their side.
#
# WHY THIS IS SAFE AND PERFORMANT:
#
#   The original auth.py (before our changes) created a brand new httpx
#   client on every single request — a full TCP + TLS handshake each time.
#   Under load this caused connection exhaustion and timeout-based 401s.
#
#   This version reuses the singleton admin client from database.py which
#   has persistent connection pooling built in. One connection is established
#   at startup and reused for all requests — no per-request TCP handshake.
#
#   The tradeoff vs local JWT verification:
#     Local JWT:  ~0.5ms, zero network, but broken with RS256 migration
#     This file:  ~20-50ms, one Supabase call, works with all signing methods
#
#   20-50ms is acceptable — the connection is persistent so there is no
#   TCP/TLS overhead on each request.
#
# =============================================================================

import logging

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.services.database import get_admin

logger = logging.getLogger(__name__)

security = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    """
    Verify the Supabase JWT token and return the user object.

    Uses the singleton Supabase admin client from database.py.
    Reuses the persistent connection — no new TCP handshake per request.

    Returns a dict with at minimum: id (user UUID), email, role.
    The 'sub' key is added as an alias for 'id' so that downstream
    code using either current_user["sub"] or current_user["id"] works.

    Raises HTTP 401 for any invalid, expired, or unrecognised token.
    """
    token = credentials.credentials

    try:
        db = get_admin()
        response = db.auth.get_user(token)

        if response is None or response.user is None:
            logger.warning("[auth] Token rejected: get_user returned no user")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired session. Please log in again.",
            )

        user = response.user

        # Build a clean dict that the rest of the codebase can use.
        # We expose both "id" and "sub" pointing to the same UUID so that
        # verify.py's current_user.get("sub") or current_user.get("id")
        # works correctly regardless of which key callers use.
        user_dict = {
            "id":            user.id,
            "sub":           user.id,   # alias — same value as "id"
            "email":         user.email,
            "role":          "authenticated",
            "phone":         getattr(user, "phone", None),
            "user_metadata": getattr(user, "user_metadata", {}),
            "app_metadata":  getattr(user, "app_metadata", {}),
        }

        logger.debug(f"[auth] Token verified for user={user.id[:8]}...")
        return user_dict

    except HTTPException:
        raise   # re-raise our own 401s as-is

    except Exception as e:
        error_str = str(e).lower()

        # Supabase returns specific error messages we can map to clear responses
        if "invalid" in error_str or "expired" in error_str or "jwt" in error_str:
            logger.warning(f"[auth] Token rejected: {e}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired session. Please log in again.",
            )

        # Supabase itself is unreachable (network error, timeout, etc.)
        # Log it clearly so you can see it in Railway logs.
        logger.error(
            f"[auth] Supabase auth check failed — this is a connectivity "
            f"issue between Railway and Supabase, not a user error: {e}",
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
