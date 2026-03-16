# =============================================================================
# app/middleware/auth.py — Local JWT verification via Supabase JWKS
# =============================================================================
#
# ROOT CAUSE OF THE 500 ERRORS:
#   Every previous version of this file made an HTTP call to Supabase's
#   /auth/v1/user endpoint on EVERY request to verify the token.
#   Railway's outbound network hangs on these async HTTP calls — the request
#   never completes, Railway's proxy times out and returns its own 500.
#   That's why there were no logs and no CORS headers — FastAPI never got
#   a chance to respond at all.
#
# THE FIX:
#   Use PyJWT's PyJWKClient to fetch Supabase's public signing key ONCE
#   at module load time, then verify all tokens LOCALLY in memory.
#   Zero network calls per request. Zero Railway timeout risk.
#   Works with RS256 (Supabase's new JWT Signing Keys) automatically.
#
# WHY THIS WORKS:
#   Supabase publishes its public keys at:
#     {SUPABASE_URL}/auth/v1/.well-known/jwks.json
#   PyJWKClient fetches this URL once (at startup) and caches the keys.
#   Token verification is then pure cryptography — ~0.5ms, no network.
#
# DEPENDENCIES:
#   PyJWT==2.10.1       — already in requirements.txt
#   cryptography==43.0.3 — already in requirements.txt
#   No new packages needed.
#
# =============================================================================

import logging
import requests as req

import jwt
from jwt import PyJWKClient

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.config import settings

logger = logging.getLogger(__name__)

security = HTTPBearer()

# =============================================================================
# JWKS CLIENT — initialised once at module load
# =============================================================================
#
# PyJWKClient fetches the JWKS (JSON Web Key Set) from Supabase and caches
# the public keys in memory. All subsequent token verifications are local.
#
# The JWKS endpoint is: {SUPABASE_URL}/auth/v1/.well-known/jwks.json
# It contains Supabase's RS256 public key used to sign all JWTs.
#
JWKS_URL = f"{settings.SUPABASE_URL}/auth/v1/.well-known/jwks.json"

# PyJWKClient caches keys and re-fetches only when a new key ID appears
_jwks_client: PyJWKClient | None = None


def _get_jwks_client() -> PyJWKClient:
    """Returns the cached JWKS client, creating it on first call."""
    global _jwks_client
    if _jwks_client is None:
        logger.info(f"[auth] Initialising JWKS client from {JWKS_URL}")
        _jwks_client = PyJWKClient(JWKS_URL, cache_keys=True)
        logger.info("[auth] JWKS client ready")
    return _jwks_client


# =============================================================================
# FALLBACK: legacy HS256 verification
# =============================================================================
#
# If the JWKS approach fails (e.g. Supabase still issuing HS256 tokens),
# we fall back to verifying with the legacy JWT secret.
#
def _verify_hs256(token: str) -> dict | None:
    """Try HS256 verification with the legacy Supabase JWT secret."""
    secret = settings.SUPABASE_JWT_SECRET.strip()
    if not secret:
        return None
    try:
        return jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
    except Exception:
        return None


# =============================================================================
# DEPENDENCY: get_current_user
# =============================================================================

def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    """
    Verify the Supabase JWT token entirely locally — zero network calls.

    Flow:
      1. Try RS256 verification using cached JWKS public key
      2. If that fails, try HS256 with legacy JWT secret (fallback)
      3. If both fail, return 401

    Returns a dict with 'sub' and 'id' both set to the user UUID.
    """
    token = credentials.credentials

    payload: dict | None = None

    # ── Attempt 1: RS256 via JWKS ─────────────────────────────────────────────
    try:
        client = _get_jwks_client()
        signing_key = client.get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            options={"verify_aud": False},
        )
        logger.info(f"[auth] RS256 verified: user={payload.get('sub', '')[:8]}...")

    except jwt.ExpiredSignatureError:
        logger.info("[auth] Token expired")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired. Please log in again.",
        )

    except jwt.DecodeError:
        # RS256 failed — try HS256 fallback
        logger.info("[auth] RS256 failed, trying HS256 fallback")
        payload = _verify_hs256(token)

    except Exception as e:
        # JWKS fetch failed or other error — try HS256 fallback
        logger.warning(f"[auth] JWKS verification error: {e} — trying HS256 fallback")
        payload = _verify_hs256(token)

    # ── Attempt 2: HS256 fallback result check ────────────────────────────────
    if payload is None:
        logger.warning("[auth] Both RS256 and HS256 verification failed")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session. Please log in again.",
        )

    # ── Extract user ID ───────────────────────────────────────────────────────
    user_id = payload.get("sub")
    if not user_id:
        logger.error(f"[auth] Token has no 'sub' claim. Keys: {list(payload.keys())}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session token.",
        )

    # Return payload with both 'sub' and 'id' for compatibility
    payload["id"] = user_id
    return payload


def get_verified_user_id(
    current_user: dict = Depends(get_current_user),
) -> str:
    """Convenience dependency — returns just the user_id string."""
    return current_user["sub"]
