from fastapi import APIRouter, Depends, Query, HTTPException, status
from app.middleware.auth import get_verified_user_id
from app.models.schemas import HistoryResponse
from app.services.database import verifications_db
import logging

logger = logging.getLogger(__name__)
router = APIRouter()


def get_history(
    page:           int        = Query(default=1,  ge=1),
    page_size:      int        = Query(default=20, ge=1, le=100),
    verdict_filter: str | None = Query(default=None),
    user_id:        str        = Depends(get_verified_user_id),
):
    result = verifications_db.get_history(
        user_id        = user_id,
        page           = page,
        per_page       = page_size,
        verdict_filter = verdict_filter,
    )
    return HistoryResponse(**result)


def get_verification(
    verification_id: str,
    user_id:         str = Depends(get_verified_user_id),
):
    record = verifications_db.get_by_id(verification_id, user_id)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Verification not found.",
        )
    return record


router.add_api_route("/",                  get_history,      methods=["GET"], response_model=HistoryResponse)
router.add_api_route("/{verification_id}", get_verification, methods=["GET"])
