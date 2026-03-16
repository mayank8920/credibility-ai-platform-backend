# =============================================================================
# app/routes/history.py — GET /history (authenticated)
# =============================================================================

from fastapi import APIRouter, Depends, Query
from app.middleware.auth import get_verified_user_id
from app.models.schemas import HistoryResponse
from app.services.database import verifications_db
import logging

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get(
    "/",
    response_model=HistoryResponse,
    summary="Get the current user's verification history",
)
async def get_history(
    page:            int           = Query(default=1,  ge=1),
    page_size:       int           = Query(default=20, ge=1, le=100),
    verdict_filter:  str | None    = Query(default=None),
    user_id:         str           = Depends(get_verified_user_id),   # ← auth required
):
    """
    Returns paginated verification history for the current user.

    Security: user_id comes from the verified JWT — users can ONLY
    see their own history.

    Optional filter by verdict: VERIFIED | MOSTLY_TRUE | QUESTIONABLE | MISLEADING | FALSE
    """
    result = verifications_db.get_history(
        user_id        = user_id,
        page           = page,
        per_page       = page_size,
        verdict_filter = verdict_filter,
    )
    return HistoryResponse(**result)


@router.get(
    "/{verification_id}",
    summary="Get a single verification by ID",
)
async def get_verification(
    verification_id: str,
    user_id:         str = Depends(get_verified_user_id),
):
    """
    Fetch the full result for a single verification.
    Only returns the record if it belongs to the requesting user.
    """
    record = verifications_db.get_by_id(verification_id, user_id)
    if not record:
        from fastapi import HTTPException, status
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Verification not found or does not belong to your account.",
        )
    return record
