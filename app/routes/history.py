# app/routes/history.py
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from app.middleware.auth import get_verified_user_id
from app.models.schemas import HistoryResponse
from app.services.database import verifications_db
import logging
import traceback

logger = logging.getLogger(__name__)
router = APIRouter()

@router.get("/", summary="Get the current user's verification history")
async def get_history(
    page:           int        = Query(default=1,    ge=1),
    page_size:      int        = Query(default=20,   ge=1, le=100),
    verdict_filter: str | None = Query(default=None),
    user_id:        str        = Depends(get_verified_user_id),
):
    try:
        logger.info(f"[history] Fetching history for user={user_id[:8]}... page={page}")

        result = verifications_db.get_history(
            user_id        = user_id,
            page           = page,
            per_page       = page_size,
            verdict_filter = verdict_filter,
        )

        logger.info(f"[history] Got {result.get('total', 0)} records")

        return HistoryResponse(**result)

    except Exception as e:
        # Log the FULL traceback so it appears in Railway logs
        logger.error(
            f"[history] CRASH for user={user_id[:8]}...\n"
            f"{traceback.format_exc()}"
        )
        return JSONResponse(
            status_code=500,
            content={"detail": f"History fetch failed: {str(e)}"},
        )


@router.get("/{verification_id}", summary="Get a single verification by ID")
async def get_verification(
    verification_id: str,
    user_id:         str = Depends(get_verified_user_id),
):
    try:
        record = verifications_db.get_by_id(verification_id, user_id)
        if not record:
            from fastapi import HTTPException, status
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Verification not found or does not belong to your account.",
            )
        return record
    except Exception as e:
        logger.error(f"[history] get_verification CRASH:\n{traceback.format_exc()}")
        return JSONResponse(
            status_code=500,
            content={"detail": f"Failed: {str(e)}"},
        )
