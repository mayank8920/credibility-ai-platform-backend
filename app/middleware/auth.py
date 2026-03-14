# =============================================================================
# app/middleware/auth.py — Supabase token verification (persistent client)
# =============================================================================
#
# Uses a single persistent httpx.AsyncClient created once at module load.
# This is the key difference from the original broken version, which created
# a brand new client (full TCP + TLS handshake) on every single request.
#
# One client, one connection pool, reused for all requests.
# Works with all Supabase signing methods (HS256 legacy and RS256 new).
#
# =============================================================================

import httpx
import logging

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.config import settings

logger = logging.getLogger(__name__)

security = HTTPBearer()

# ── Single persistent client — created ONCE at module load ───────────────────
# This is the critical fix. The original code did:
#   async with httpx.AsyncClient(timeout=10) as client:   ← new client every request
# That means a full TCP + TLS handshake on every single auth check.
# Under load this causes connection exhaustion → timeouts → 401s.
#
# This client is created once when the module is imported and reused
# for every request. Connection pooling is handled automatically by httpx.
_auth_client = httpx.AsyncClient(
    timeout=httpx.Timeout(10.0, connect=5.0),
    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
)

_SUPABASE_USER_URL = f"{settings.SUPABASE_URL}/auth/v1/user"


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    """
    Verify the Supabase JWT by calling /auth/v1/user with the token.

    Supabase verifies the token on their side — works with both the
    legacy HS256 secret and the new RS256 JWT Signing Keys automatically.

    Uses a persistent httpx client (module-level singleton) so there is
    no TCP/TLS handshake overhead per request.

    Returns a dict with both 'id' and 'sub' set to the user's UUID.
    Raises HTTP 401 for invalid/expired tokens.
    Raises HTTP 503 if Supabase is unreachable.
    """
    token = credentials.credentials

    try:
        response = await _auth_client.get(
            _SUPABASE_USER_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "apikey": settings.SUPABASE_ANON_KEY,
            },
        )

        if response.status_code == 401:
            logger.warning("[auth] Token rejected by Supabase: 401")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired session. Please log in again.",
            )

        if response.status_code != 200:
            logger.error(
                f"[auth] Supabase /auth/v1/user returned unexpected "
                f"status {response.status_code}: {response.text[:200]}"
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Authentication service temporarily unavailable. Please try again.",
            )

        user = response.json()
        user_id = user.get("id")

        if not user_id:
            logger.error(f"[auth] Supabase response missing 'id'. Keys: {list(user.keys())}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid session token.",
            )

        # Expose both "id" and "sub" — verify.py uses .get("sub") or .get("id")
        user["sub"] = user_id

        logger.debug(f"[auth] Token verified for user={user_id[:8]}...")
        return user

    except HTTPException:
        raise

    except httpx.TimeoutException:
        logger.error("[auth] Supabase auth check timed out after 10s")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service timed out. Please try again.",
        )

    except httpx.RequestError as e:
        logger.error(f"[auth] Network error reaching Supabase: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service temporarily unavailable. Please try again.",
        )


async def get_verified_user_id(
    current_user: dict = Depends(get_current_user),
) -> str:
    """Convenience dependency — returns just the user_id string."""
    return current_user["sub"]
