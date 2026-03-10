# =============================================================================
# app/routes/user.py — GET /user and PATCH /user/profile (authenticated)
# =============================================================================

from fastapi import APIRouter, Depends, HTTPException, status
from app.middleware.auth import get_current_user, get_verified_user_id
from app.models.schemas import UserProfile, UpdateProfileRequest
from app.services.database import users_db, usage_db
import logging

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get(
    "/",
    summary="Get the current user's profile",
)
async def get_profile(
    user_id: str = Depends(get_verified_user_id),
):
    profile = users_db.get_by_id(user_id)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User profile not found. It may not have been created yet.",
        )
    return profile


@router.patch(
    "/",
    summary="Update profile (full_name, avatar_url)",
)
async def update_profile(
    updates:    UpdateProfileRequest,
    user_id:    str = Depends(get_verified_user_id),
):
    """
    Update the current user's profile.
    Only full_name and avatar_url can be changed.
    Plan, user_id, email cannot be changed here.
    """
    updated = users_db.update_profile(user_id, updates.model_dump(exclude_none=True))
    if not updated:
        raise HTTPException(status_code=400, detail="Nothing to update.")
    return updated


@router.get(
    "/usage",
    summary="Get the current user's daily verification usage",
)
async def get_usage(
    user_id: str = Depends(get_verified_user_id),
):
    """
    Returns current daily usage, plan limits, and reset time.
    Used by the frontend to show the usage indicator in the sidebar.

    Example response:
        {
          "used": 4, "limit": 10, "remaining": 6,
          "allowed": true, "plan": "free",
          "reset_at": "2025-01-15T00:00:00+00:00"
        }
    """
    return usage_db.get_status(user_id)


@router.get(
    "/stats",
    summary="Get aggregate stats for the current user",
)
async def get_stats(
    user_id: str = Depends(get_verified_user_id),
):
    return users_db.get_stats(user_id)
