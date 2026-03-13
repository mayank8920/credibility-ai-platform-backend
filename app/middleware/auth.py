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

# =============================================================================
# app/middleware/auth.py — Supabase token verification (PRODUCTION SAFE)
# =============================================================================

import httpx
import logging

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.config import settings

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=True)

SUPABASE_USER_URL = f"{settings.SUPABASE_URL}/auth/v1/user"


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> dict:
    """
    Production-safe Supabase authentication.

    Instead of manually decoding JWT (which breaks with RS256 / JWK),
    we ask Supabase Auth server to validate the token.

    If token is valid → returns user JSON
    If invalid / expired → returns 401
    """

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
            logger.warning(
                f"[Auth] Supabase rejected token. Status={response.status_code}"
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired session",
            )

        return response.json()

    except httpx.RequestError as exc:
        logger.error(f"[Auth] Supabase auth request failed: {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service unavailable",
        )


async def get_verified_user_id(
    current_user: dict = Depends(get_current_user),
) -> str:
    user_id = current_user.get("id")

    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User ID not found in Supabase response",
        )

    return user_idJWT payload.",
        )
    return user_id
