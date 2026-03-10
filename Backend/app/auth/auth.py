# =============================================================================
# app/middleware/auth.py — Supabase JWT verification
# =============================================================================
# Plain-English:
#   When a user logs in, Supabase gives them a "JWT token" — a long string
#   that proves who they are. The frontend sends this token with every
#   request to FastAPI in the "Authorization" header:
#
#     Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
#
#   This file:
#     1. Extracts the token from the header
#     2. Verifies it was genuinely signed by Supabase (not forged)
#     3. Returns the user's ID so the route can use it
#
# If the token is missing, expired, or fake → 401 Unauthorized
# If the token is valid → returns the user's data
#
# WHERE THIS FILE LIVES:
#   credibility-backend/app/middleware/auth.py
# =============================================================================

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt                 # PyJWT library
from app.config import settings
import logging

logger = logging.getLogger(__name__)

# HTTPBearer automatically extracts the token from "Authorization: Bearer <token>"
bearer_scheme = HTTPBearer(auto_error=False)


# =============================================================================
# get_current_user — REQUIRED auth (use on all protected endpoints)
# =============================================================================

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> dict:
    """
    FastAPI dependency that verifies a Supabase JWT and returns the user payload.

    Inject into any route that requires authentication:

        @router.post("/verify/")
        async def verify(
            payload: VerifyRequest,
            user = Depends(get_current_user),   # ← this line
        ):
            user_id = user["sub"]  # Supabase user UUID

    The decoded payload contains:
        {
          "sub":   "uuid-of-the-user",        ← most important: the user's ID
          "email": "user@example.com",
          "role":  "authenticated",
          "exp":   1234567890,                ← expiry timestamp
          "iat":   1234567890,                ← issued-at timestamp
        }
    """
    # ── Step 1: Check a token was actually provided ───────────────────────────
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "MISSING_TOKEN",
                "message": "No authentication token provided. Please log in.",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    # ── Step 2: Verify the token signature ────────────────────────────────────
    # jwt.decode checks:
    #   • The token was signed by Supabase (using SUPABASE_JWT_SECRET)
    #   • The token hasn't expired
    #   • The audience is "authenticated" (a logged-in user, not a service)
    try:
        payload = jwt.decode(
            token,
            settings.SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            audience="authenticated",    # Supabase sets this for user JWTs
        )
        logger.debug(f"[Auth] Verified user: {payload.get('sub')}")
        return payload

    except jwt.ExpiredSignatureError:
        # Token is valid but has expired — user needs to log in again
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "TOKEN_EXPIRED",
                "message": "Your session has expired. Please log in again.",
            },
        )
    except jwt.InvalidAudienceError:
        # Token exists but isn't a user token (e.g. service role token)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "INVALID_TOKEN_AUDIENCE",
                "message": "Invalid token type. Expected a user session token.",
            },
        )
    except jwt.InvalidTokenError as e:
        # Any other JWT error: tampered, malformed, wrong secret, etc.
        logger.warning(f"[Auth] Invalid JWT: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "INVALID_TOKEN",
                "message": "Invalid authentication token. Please log in again.",
            },
        )


# =============================================================================
# get_optional_user — OPTIONAL auth (use on endpoints that work for guests too)
# =============================================================================

async def get_optional_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> dict | None:
    """
    Like get_current_user but returns None instead of raising 401.

    Use this for endpoints that work for both guests AND logged-in users:

        @router.post("/verify/")
        async def verify(
            payload: VerifyRequest,
            user = Depends(get_optional_user),   # ← guests allowed
        ):
            if user:
                user_id = user["sub"]   # logged-in user
            else:
                user_id = None          # guest — result won't be saved
    """
    if credentials is None:
        return None
    try:
        return await get_current_user(credentials)
    except HTTPException:
        return None


# =============================================================================
# get_verified_user_id — Convenience: just the UUID string
# =============================================================================

async def get_verified_user_id(
    user: dict = Depends(get_current_user),
) -> str:
    """
    Convenience dependency that returns just the user's UUID string.

    Use when you only need the ID and nothing else:

        @router.get("/history/")
        async def history(user_id: str = Depends(get_verified_user_id)):
            records = db.get_history(user_id)
    """
    user_id = user.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not extract user ID from token.",
        )
    return user_id
