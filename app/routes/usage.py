# =============================================================================
# app/routes/usage.py — GET /usage/today  (MISSING FILE — created now)
# =============================================================================
# FIX: main.py imports this module but it didn't exist, causing an
# immediate ImportError crash on startup.
#
# Also fixes the mismatch between:
#   DebugPanel.tsx → calls GET /usage/today
#   user.py       → had GET /user/usage  (different URL)
#
# Now BOTH endpoints exist and return the same data shape.
# =============================================================================

from fastapi import APIRouter, Depends
from app.middleware.auth import get_verified_user_id
from app.services.database import usage_db
import logging

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get(
    "/today",
    summary="Get today's usage stats for the current user",
    description=(
        "Returns how many verifications the user has run today, "
        "their plan limit, and how many remain. "
        "Used by the DebugPanel and sidebar usage indicator."
    ),
)
async def get_today_usage(
    user_id: str = Depends(get_verified_user_id),
):
    """
    Returns:
        {
          "search_count": 3,
          "daily_limit":  10,
          "remaining":    7,
          "allowed":      true,
          "plan":         "free",
          "reset_at":     "2025-01-15T00:00:00+00:00"
        }

    Note: "search_count" is aliased from "used" so the DebugPanel
    frontend code (which reads data.search_count) works correctly.
    """
    status = usage_db.get_status(user_id)

    # DebugPanel reads: data.search_count, data.daily_limit, data.remaining
    # usage_db.get_status() returns: used, limit, remaining
    # We return both names so both old and new frontend code works.
    return {
        "search_count": status.get("used", 0),        # ← alias for DebugPanel
        "used":         status.get("used", 0),
        "daily_limit":  status.get("limit", 10),
        "limit":        status.get("limit", 10),
        "remaining":    status.get("remaining", 10),
        "allowed":      status.get("allowed", True),
        "plan":         status.get("plan", "free"),
        "reset_at":     status.get("reset_at", ""),
    }
