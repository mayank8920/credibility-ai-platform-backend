# =============================================================================
# app/middleware/rate_limit.py — Daily verification limits per user
# =============================================================================
# Plain-English:
#   Every user has a daily limit of verifications based on their plan:
#     Free plan:       10 per day
#     Pro plan:       100 per day
#     Enterprise:   Unlimited
#
#   This middleware:
#     1. Looks up the user's current usage in the database
#     2. If they're under the limit → allows the request + increments counter
#     3. If they're over the limit → returns HTTP 429 with a clear message
#
#   The limit resets at midnight UTC every day.
#
# HOW TO USE IN A ROUTE:
#   @router.post("/verify/")
#   async def verify(
#       payload: VerifyRequest,
#       current_user: dict = Depends(get_current_user),
#       _rate_check:  None = Depends(require_quota),  # ← add this line
#   ):
#       ...
#
# WHERE THIS FILE LIVES:
#   credibility-backend/app/middleware/rate_limit.py
# =============================================================================

from fastapi import Depends, HTTPException, status
from datetime import datetime, timezone, date
from app.middleware.auth import get_current_user
from app.config import settings
import logging

logger = logging.getLogger(__name__)

# Mapping from plan name → daily limit
# These values come from .env so you can adjust without touching code
PLAN_LIMITS = {
    "free":       settings.DAILY_LIMIT_FREE,
    "pro":        settings.DAILY_LIMIT_PRO,
    "enterprise": settings.DAILY_LIMIT_ENTERPRISE,
}


async def require_quota(
    current_user: dict = Depends(get_current_user),
) -> None:
    """
    FastAPI dependency — checks and increments the user's daily usage.

    Must come AFTER get_current_user in a route's dependencies because
    it needs the user_id to look up usage.

    Raises HTTP 429 if the daily limit is reached.
    Returns None if the request is allowed (dependency returns nothing on success).
    """
    user_id = current_user.get("sub") or current_user.get("id")
    if not user_id:
        # This shouldn't happen if get_current_user succeeded, but be safe
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not determine user identity for rate limiting.",
        )

    # Import here to avoid circular imports
    from app.services.database import usage_db

    try:
        # check_and_increment returns:
        #   {"allowed": True/False, "used": N, "limit": N, "plan": "free", ...}
        result = usage_db.check_and_increment(user_id)

        if result is None:
            # Database error — fail open (allow request) to avoid blocking users
            logger.warning(
                f"[RateLimit] Could not check usage for user {user_id} — failing open"
            )
            return None

        allowed = result.get("allowed", True)

        if not allowed:
            plan      = result.get("plan",    "free")
            used      = result.get("used",     0)
            limit     = result.get("limit",    PLAN_LIMITS["free"])
            resets_at = result.get("reset_at", _next_midnight_utc())

            logger.info(
                f"[RateLimit] LIMIT REACHED: user={user_id} plan={plan} "
                f"used={used}/{limit}"
            )

            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error":       "DAILY_LIMIT_REACHED",
                    "message":     (
                        f"You've used all {limit} of your daily verifications on the {plan} plan. "
                        f"Your limit resets at midnight UTC."
                    ),
                    "limit":       limit,
                    "used":        used,
                    "plan":        plan,
                    "resets_at":   resets_at,
                    "upgrade_url": "https://truthlens.vercel.app/upgrade",
                },
                headers={
                    # Standard rate-limit headers (recognised by browsers & API clients)
                    "Retry-After":          str(_seconds_until_midnight()),
                    "X-RateLimit-Limit":    str(limit),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset":    resets_at,
                },
            )

        # Log successful rate-limit pass (helpful for debugging)
        used  = result.get("used",  1)
        limit = result.get("limit", PLAN_LIMITS["free"])
        logger.debug(
            f"[RateLimit] Allowed: user={user_id} used={used}/{limit}"
        )

    except HTTPException:
        raise   # re-raise 429 as-is
    except Exception as e:
        # Unexpected error — fail open (don't block users due to our DB issues)
        logger.error(f"[RateLimit] Unexpected error for user {user_id}: {e}", exc_info=True)
        return None


# =============================================================================
# HELPERS
# =============================================================================

def _next_midnight_utc() -> str:
    """Returns the next midnight UTC as an ISO 8601 string."""
    now   = datetime.now(timezone.utc)
    reset = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if reset <= now:
        from datetime import timedelta
        reset += timedelta(days=1)
    return reset.isoformat()


def _seconds_until_midnight() -> int:
    """Returns seconds until next midnight UTC (for Retry-After header)."""
    now   = datetime.now(timezone.utc)
    reset = now.replace(hour=0, minute=0, second=0, microsecond=0)
    from datetime import timedelta
    if reset <= now:
        reset += timedelta(days=1)
    return int((reset - now).total_seconds())
