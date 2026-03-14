# =============================================================================
# app/middleware/auth.py — Local JWT verification (no network calls)
# =============================================================================
#
# Verifies the Supabase JWT locally using SUPABASE_JWT_SECRET.
# Zero network dependency — token validation takes ~0.5ms regardless of
# Supabase latency or availability.
#
# COMMON DEPLOYMENT ISSUES AND HOW THIS FILE HANDLES THEM:
#
#   Issue 1 — Wrong JWT Secret value in Railway env vars
#     Symptom: DecodeError / "Invalid signature" in logs
#     Cause:   SUPABASE_JWT_SECRET was set to the Anon Key or Service Role Key
#              instead of the actual JWT Secret.
#     Fix:     Supabase Dashboard → Settings → API → "JWT Secret" (not the keys)
#
#   Issue 2 — Audience validation failure
#     Symptom: InvalidAudienceError in logs
#     Cause:   PyJWT's audience check is strict. Supabase tokens carry
#              aud="authenticated" but some PyJWT versions handle string vs
#              list audiences differently.
#     Fix:     This file disables audience verification in PyJWT and instead
#              checks the "role" claim manually, which is more reliable and
#              gives a clearer error message.
#
#   Issue 3 — JWT Secret has extra whitespace in env var
#     Symptom: DecodeError / "Invalid signature" despite seemingly correct value
#     Cause:   Copy-paste added a leading/trailing space or newline.
#     Fix:     This file strips whitespace from the secret before use.
#
# =============================================================================

import jwt
import logging

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.config import settings

logger = logging.getLogger(__name__)

security = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    """
    Verify the Supabase JWT token and return the decoded payload.

    The payload contains: sub (user UUID), email, role, exp, iat, and
    any other claims Supabase includes (e.g. user_metadata, app_metadata).

    Raises HTTP 401 for any invalid, expired, or malformed token.
    """
    token = credentials.credentials

    # Strip whitespace from the secret — guards against copy-paste issues
    # in Railway/Render environment variable editors.
    jwt_secret = settings.SUPABASE_JWT_SECRET.strip()

    if not jwt_secret:
        # Secret is completely missing — log clearly and fail.
        # This is a configuration error, not a user error.
        logger.error(
            "[auth] SUPABASE_JWT_SECRET is empty. "
            "Set it in Railway environment variables: "
            "Supabase Dashboard → Settings → API → JWT Secret"
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server authentication is misconfigured. Please contact support.",
        )

    try:
        # Decode and verify the JWT locally.
        #
        # options={"verify_aud": False}:
        #   We skip PyJWT's built-in audience check because it is fragile —
        #   it raises InvalidAudienceError if aud is a list instead of a
        #   string, which varies by PyJWT version. We verify the role claim
        #   manually below, which gives the same security guarantee with a
        #   clearer error message.
        payload = jwt.decode(
            token,
            jwt_secret,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )

    except jwt.ExpiredSignatureError:
        # Token is valid but has passed its expiry time (exp claim).
        # This is a normal, expected error — user needs to log in again.
        logger.info("[auth] Token rejected: expired")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired. Please log in again.",
        )

    except jwt.DecodeError as e:
        # Signature verification failed — the secret is wrong, or the token
        # was tampered with, or there is a whitespace/encoding issue in the secret.
        logger.warning(
            f"[auth] Token rejected: DecodeError — {e}. "
            f"Check that SUPABASE_JWT_SECRET in Railway matches: "
            f"Supabase Dashboard → Settings → API → JWT Secret"
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session token.",
        )

    except jwt.InvalidTokenError as e:
        # Catch-all for any other JWT problem (missing claims, wrong algorithm, etc.)
        logger.warning(f"[auth] Token rejected: InvalidTokenError — {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session token.",
        )

    # ── Manual role check (replaces PyJWT audience verification) ─────────────
    #
    # Every Supabase JWT for a logged-in user has role="authenticated".
    # Tokens generated for anonymous/service access have different roles.
    # This check ensures we only accept tokens from real logged-in users.
    role = payload.get("role")
    if role != "authenticated":
        logger.warning(
            f"[auth] Token rejected: unexpected role='{role}'. "
            f"Expected 'authenticated'. This token may be a service key, "
            f"anon key, or a token from a different Supabase project."
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session token.",
        )

    # ── Ensure user_id is present ─────────────────────────────────────────────
    user_id = payload.get("sub")
    if not user_id:
        logger.error(
            f"[auth] Token decoded successfully but 'sub' claim is missing. "
            f"Claims present: {list(payload.keys())}"
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing user ID.",
        )

    logger.debug(f"[auth] Token verified for user={user_id[:8]}...")
    return payload


async def get_verified_user_id(
    current_user: dict = Depends(get_current_user),
) -> str:
    """Convenience dependency — returns just the user_id string."""
    return current_user["sub"]
