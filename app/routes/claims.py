# =============================================================================
# app/routes/claims.py — GET /claims/stats  (MISSING FILE — created now)
# =============================================================================
# FIX: main.py imports this module but it didn't exist, causing an
# immediate ImportError crash on startup.
#
# DebugPanel.tsx calls: GET /claims/stats
# Expected response: { database: { total_cached_claims, reuses_saved } }
# =============================================================================

from fastapi import APIRouter, Depends
from app.middleware.auth import get_verified_user_id
from app.services.database import get_admin
import logging

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get(
    "/stats",
    summary="Get global claim cache statistics",
    description="Returns aggregate stats from the global_claims cache table.",
)
async def get_cache_stats(
    user_id: str = Depends(get_verified_user_id),
):
    """
    Returns the stats shape that DebugPanel.tsx expects:
    {
      "database": {
        "total_cached_claims": 142,
        "reuses_saved": 89,
        "avg_credibility_score": 61.4,
        "claims_with_embeddings": 120,
        "embedding_coverage_pct": 84.5
      }
    }
    """
    try:
        db = get_admin()
        response = (
            db.table("claim_cache_stats")   # uses the view defined in the schema
            .select("*")
            .maybe_single()
            .execute()
        )
        stats = response.data or {}
        return {"database": stats}

    except Exception as e:
        logger.error(f"[Claims] Failed to fetch cache stats: {e}")
        return {
            "database": {
                "total_cached_claims": 0,
                "reuses_saved": 0,
                "avg_credibility_score": None,
                "claims_with_embeddings": 0,
                "embedding_coverage_pct": 0,
            }
        }
