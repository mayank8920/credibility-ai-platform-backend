# =============================================================================
# app/middleware/auth.py — JWT verification dependency
# =============================================================================
# HOW TO USE IN A ROUTE:
#   from app.middleware.auth import get_current_user, get_verified_user_id
#
#   # Full JWT payload dict (has sub, email, role, exp, etc.)
#   current_user: dict = Depends(get_current_user)
#
#   # Just the user's UUID string — use this when you only need the ID
#   user_id: str = Depends(get_verified_user_id)
# =============================================================================

import logging
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.config import settings

logger = logging.getLogger(__name__)

# HTTPBearer extracts the token from the "Authorization: Bearer <token>" header
_bearer = HTTPBearer(auto_error=True)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> dict:
    """
    FastAPI dependency — verifies the Supabase JWT and returns the decoded payload.

    The payload contains: sub (user UUID), email, role, exp, iat, etc.

    Raises HTTP 401 if:
      • No Authorization header is present
      • The token is malformed
      • The token has expired
      • The signature is invalid
    """
    token = credentials.credentials

    if not settings.SUPABASE_JWT_SECRET:
        # If secret isn't configured yet, warn loudly but don't crash the whole app
        logger.error(
            "SUPABASE_JWT_SECRET is not set in .env — "
            "all authenticated endpoints will return 401."
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service is not configured. Contact support.",
        )

    try:
        payload = jwt.decode(
            token,
            settings.SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            options={"verify_aud": False},   # Supabase JWTs don't always include aud
        )
        return payload

    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your session has expired. Please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError as exc:
        logger.warning(f"[Auth] Invalid JWT: {exc}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except Exception as exc:
        logger.error(f"[Auth] Unexpected error during JWT decode: {exc}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed.",
        )


async def get_verified_user_id(
    current_user: dict = Depends(get_current_user),
) -> str:
    """
    Convenience dependency — returns just the user's UUID string.

    Use this in routes that only need the user_id, not the full JWT payload.
    """
    user_id = current_user.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User ID (sub) not found in JWT payload.",
        )
    return user_id
