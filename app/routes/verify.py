# =============================================================================
# app/routes/verify.py — POST /verify  (Fixed version)
# =============================================================================
#
# BUGS FIXED IN THIS VERSION:
#   1.  AccountInput() called with non-existent fields (original_content, claims)
#       → replaced with correct fields from account_metadata payload
#   2.  blend_scores() called with wrong param name (account_weight → weight)
#       and tried to unpack single float return as a tuple
#       → fixed to handle single float return correctly
#   3.  content_result.final_score doesn't exist → content_result.credibility_score
#   4.  account_result.overall_score doesn't exist → account_result.account_credibility_score
#   5.  account_result.model_dump() doesn't exist (dataclass, not Pydantic)
#       → replaced with explicit field mapping
#   6.  ScoringInput passed sources_checked= which doesn't exist in the dataclass
#       → replaced with credible_source_count= and total_source_count=
#   7.  EngineClaimInput passed evidence_summary= which doesn't exist in ClaimInput
#       → removed that field
#   8.  SubScoreDetail(**s) where s is a dataclass, not a dict
#       → uses dataclasses.asdict(s)
#   9.  VerifyResponse used wrong field name: claims → claim_results
#   10. VerifyResponse used wrong field name: sources_checked → sources_consulted
#   11. VerifyResponse missing required fields: penalty_total, created_at
#   12. _confidence_level() accessed content_result.confidence (doesn't exist)
#       → uses content_result.confidence_level directly from ScoringResult
# =============================================================================

import dataclasses
import asyncio
import time
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends, Request, status

from app.models.schemas import (
    VerifyRequest, VerifyResponse,
    ClaimResult, FlagDetail, SubScoreDetail,
    AccountCredibilityDetail,
)
from app.services.scoring_engine import (
    ScoringInput,
    ClaimInput as EngineClaimInput,
    compute_score,
    _score_to_verdict,
)
from app.services.account_credibility import (
    AccountInput,
    analyse_account,
    blend_scores,
)
from app.services.news_service import search_multiple_claims
from app.services.usage_service import usage_tracker, verification_history
from app.services.claim_cache import claim_cache, CachedClaimResult
from app.middleware.auth import get_current_user
from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()

# Tier-1 source domains (mirrors news_service.py for source counting)
_TIER1_DOMAINS = {
    "reuters.com", "apnews.com", "bbc.com", "bbc.co.uk",
    "nytimes.com", "theguardian.com", "washingtonpost.com",
    "bloomberg.com", "npr.org", "pbs.org", "cnn.com",
    "politifact.com", "snopes.com", "factcheck.org",
    "who.int", "cdc.gov", "nasa.gov", "nih.gov",
}


# =============================================================================
# POST /verify/
# =============================================================================

@router.post(
    "/",
    response_model=VerifyResponse,
    status_code=status.HTTP_200_OK,
    summary="Verify content credibility (semantic similarity search enabled)",
)
async def verify(
    payload:      VerifyRequest,
    request:      Request,
    current_user: dict = Depends(get_current_user),
):
    """
    Run a credibility verification on submitted content.

    Semantic similarity search matches claims that mean the same thing
    even when the wording differs, giving instant cached results.

    Authentication: Required (Supabase JWT in Authorization header)
    Rate limit:     10 verifications per day for free users
    """

    # ── STEP 1: Identify user from JWT ────────────────────────────────────────
    user_id    = current_user["sub"]
    start_time = time.time()

    logger.info(
        f"[/verify] START user={user_id[:8]}... "
        f"claims={len(payload.claims)} type={payload.content_type}"
    )

    # ── STEP 2: Enforce daily limit ───────────────────────────────────────────
    daily_limit  = settings.DAILY_LIMIT_FREE
    usage_result = usage_tracker.check_and_increment(
        user_id     = user_id,
        daily_limit = daily_limit,
    )

    if not usage_result["allowed"]:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error":        "Daily verification limit reached",
                "search_count": usage_result["search_count"],
                "daily_limit":  daily_limit,
                "resets_at":    usage_result["resets_at"],
                "message": (
                    f"You have used all {daily_limit} of your free daily "
                    f"verifications. Your limit resets at midnight UTC."
                ),
            },
            headers={
                "X-RateLimit-Limit":     str(daily_limit),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset":     usage_result["resets_at"],
                "Retry-After":           str(_seconds_until_midnight()),
            },
        )

    valid_claims = [c for c in payload.claims if c.text.strip()]
    if not valid_claims:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="All claim texts were empty after trimming whitespace.",
        )

    # ── STEP 3: Per-claim cache lookup (exact + semantic) ─────────────────────
    cached_results:      dict[str, CachedClaimResult] = {}
    claims_to_search:    list[str]                     = []
    semantic_match_info: list[dict]                    = []

    # Run all cache lookups concurrently — each claim is independent
    lookup_results = await asyncio.gather(*[
        claim_cache.lookup(c.text.strip()) for c in valid_claims
    ])

    for schema_claim, cached in zip(valid_claims, lookup_results):
        raw_text = schema_claim.text.strip()
        if cached is not None:
            cached_results[raw_text] = cached
            if cached.semantic_match:
                semantic_match_info.append({
                    "query":            raw_text,
                    "matched_claim":    cached.matched_claim_text,
                    "similarity_score": round(cached.similarity_score, 3),
                })
        else:
            claims_to_search.append(raw_text)

    cache_hit_count    = len(cached_results)
    cache_miss_count   = len(claims_to_search)
    semantic_hit_count = len(semantic_match_info)
    exact_hit_count    = cache_hit_count - semantic_hit_count

    logger.info(
        f"[/verify] Cache: {exact_hit_count} exact | "
        f"{semantic_hit_count} semantic | {cache_miss_count} miss"
    )

    # ── STEP 4a: Live news search for cache misses ────────────────────────────
    fresh_news_results: dict[str, dict] = {}

    if claims_to_search:
        try:
            results_list = await search_multiple_claims(claims_to_search)
            for claim_text, news in zip(claims_to_search, results_list):
                fresh_news_results[claim_text] = news
        except Exception as exc:
            logger.error(f"[/verify] News search failed: {exc}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="News search is temporarily unavailable. Please try again shortly.",
            )

    # ── STEP 4b: Evaluate fresh claims + store with embeddings ────────────────
    store_tasks = []

    for claim_text, news in fresh_news_results.items():
        verdict = _evaluate_claim(claim_text, news)

        # Wrap in CachedClaimResult so STEP 5 can treat all claims uniformly
        cached_results[claim_text] = CachedClaimResult(
            status               = verdict["status"],
            confidence           = verdict["confidence"],
            evidence_summary     = verdict["evidence_summary"],
            supporting_articles  = verdict["supporting_articles"],
            sources_checked      = news.get("sources_checked", []),
            credibility_score    = verdict["confidence"],
        )

        # Schedule async store — generates embedding for future semantic hits
        store_tasks.append(
            claim_cache.store(
                raw_claim_text  = claim_text,
                verdict_data    = verdict,
                sources_checked = news.get("sources_checked", []),
            )
        )

    if store_tasks:
        await asyncio.gather(*store_tasks, return_exceptions=True)

    # ── STEP 5: Build scoring engine input ────────────────────────────────────
    engine_claims: list[EngineClaimInput] = []
    all_sources:   list[str]              = []

    for schema_claim in valid_claims:
        raw_text = schema_claim.text.strip()
        cr = cached_results.get(raw_text)
        if cr is None:
            continue

        all_sources.extend(cr.sources_checked)

        # FIX #7: EngineClaimInput has no evidence_summary field — removed it
        engine_claims.append(EngineClaimInput(
            text              = raw_text,
            status            = cr.status,
            confidence        = cr.confidence,
            fact_check_status = schema_claim.fact_check_status,
        ))

    # Compute credible source count for the scoring engine
    # FIX #6: ScoringInput has no sources_checked field; use credible_source_count/total_source_count
    unique_sources    = list(set(all_sources))
    credible_count    = sum(1 for s in unique_sources if any(d in s for d in _TIER1_DOMAINS))
    total_source_count = len(unique_sources)

    # Collect fact-check statuses for FactCheckJudge
    fact_check_matches = [
        c.fact_check_status
        for c in valid_claims
        if c.fact_check_status is not None
    ]

    scoring_input = ScoringInput(
        original_content      = payload.original_content,
        claims                = engine_claims,
        credible_source_count = credible_count,        # FIX #6
        total_source_count    = total_source_count,    # FIX #6
        fact_check_matches    = fact_check_matches,
        content_type          = payload.content_type or "tweet",
    )
    content_result = compute_score(scoring_input)

    # ── STEP 6: Account credibility ───────────────────────────────────────────
    # FIX #1: AccountInput uses source_url, username, etc. — not original_content/claims
    meta = payload.account_metadata
    acct_input = AccountInput(
        source_url           = payload.source_url,
        username             = meta.username            if meta else None,
        display_name         = meta.display_name        if meta else None,
        is_verified          = meta.is_verified         if meta else False,
        follower_count       = meta.follower_count      if meta else None,
        following_count      = meta.following_count     if meta else None,
        account_age_days     = meta.account_age_days    if meta else None,
        total_posts          = meta.total_posts         if meta else None,
        has_profile_picture  = meta.has_profile_picture if meta else True,
        has_bio              = meta.has_bio             if meta else True,
        bio_text             = meta.bio_text            if meta else None,
        content_type         = payload.content_type,
    )
    account_result = analyse_account(acct_input)

    # FIX #4: use account_credibility_score, not overall_score
    acct_score = account_result.account_credibility_score

    # FIX #2: blend_scores returns a single float, not a tuple
    # FIX #3: content_result.credibility_score, not final_score
    # FIX #2b: param is weight=, not account_weight=
    final_score = blend_scores(
        content_score = content_result.credibility_score,
        account_score = acct_score,
        weight        = settings.ACCOUNT_CREDIBILITY_WEIGHT,
    )

    # ── STEP 7: Save to user history ──────────────────────────────────────────
    elapsed_ms    = int((time.time() - start_time) * 1000)
    verdict_code, verdict_label, verdict_color = _score_to_verdict(final_score)

    # Build claim results for the response
    claim_results: list[ClaimResult] = []
    for schema_claim in valid_claims:
        raw_text = schema_claim.text.strip()
        cr = cached_results.get(raw_text)
        if cr is None:
            continue
        claim_results.append(ClaimResult(
            text                = raw_text,
            status              = cr.status,
            confidence          = cr.confidence,
            evidence_summary    = cr.evidence_summary,
            supporting_articles = cr.supporting_articles,
            sources_checked     = cr.sources_checked,
            semantic_match      = cr.semantic_match,
            similarity_score    = cr.similarity_score if cr.semantic_match else None,
            matched_claim_text  = cr.matched_claim_text if cr.semantic_match else None,
        ))

    history_entry = verification_history.save(
        user_id                   = user_id,
        input_text                = payload.original_content,
        claims                    = [c.model_dump() for c in payload.claims],
        credibility_score         = final_score,
        account_credibility_score = acct_score,
        result_json               = {
            "verdict":         verdict_code,
            "cache_hits":      cache_hit_count,
            "semantic_hits":   semantic_hit_count,
            "elapsed_ms":      elapsed_ms,
        },
    )

    logger.info(
        f"[/verify] DONE user={user_id[:8]}... "
        f"score={final_score:.1f} verdict={verdict_code} "
        f"elapsed={elapsed_ms}ms hits={cache_hit_count} misses={cache_miss_count}"
    )

    # ── STEP 8: Build response ────────────────────────────────────────────────
    # FIX #5:  AccountCredibilityResult is a dataclass — no .model_dump()
    #          Build AccountCredibilityDetail by explicit field mapping
    account_credibility = AccountCredibilityDetail(
        account_credibility_score = account_result.account_credibility_score,
        flags                     = account_result.flags,
        flag_details              = [
            FlagDetail(**fd) for fd in account_result.flag_details
        ],
        domain_tier      = account_result.domain_tier,
        source_type      = account_result.source_type,
        analysis_note    = account_result.analysis_note,
        data_completeness= account_result.data_completeness,
    )

    # FIX #8:  SubScore is a dataclass — use dataclasses.asdict() to convert
    sub_scores = [
        SubScoreDetail(**dataclasses.asdict(s))
        for s in content_result.sub_scores
    ]

    claims_breakdown = {
        "total":      len(claim_results),
        "verified":   sum(1 for c in claim_results if c.status == "VERIFIED"),
        "false":      sum(1 for c in claim_results if c.status == "FALSE"),
        "disputed":   sum(1 for c in claim_results if c.status == "DISPUTED"),
        "unverified": sum(1 for c in claim_results if c.status == "UNVERIFIED"),
    }

    return VerifyResponse(
        verification_id   = str(history_entry.get("id", "")),
        credibility_score = round(final_score, 2),
        verdict           = verdict_code,
        verdict_label     = verdict_label,
        verdict_color     = verdict_color,
        # FIX #12: use content_result.confidence_level (str), not content_result.confidence (float)
        confidence_level  = content_result.confidence_level,
        flags             = content_result.flags,
        flag_details      = [FlagDetail(**f) for f in content_result.flag_details],
        summary           = content_result.summary,
        # FIX #11: added required penalty_total field
        penalty_total     = content_result.penalty_total,
        # FIX #10: renamed sources_checked → sources_consulted
        sources_consulted = unique_sources,
        # FIX #9: renamed claims → claim_results
        claim_results     = claim_results,
        claims_breakdown  = claims_breakdown,
        # FIX #8: sub_scores now properly converted from dataclasses
        sub_scores        = sub_scores,
        account_credibility = account_credibility,
        cache_info = {
            "hits":             cache_hit_count,
            "misses":           cache_miss_count,
            "total_claims":     len(valid_claims),
            "served_from_cache": cache_hit_count > 0,
            "exact_hits":       exact_hit_count,
            "semantic_hits":    semantic_hit_count,
            "semantic_matches": semantic_match_info,
        },
        usage = {
            "search_count": usage_result["search_count"],
            "daily_limit":  daily_limit,
            "remaining":    max(0, daily_limit - usage_result["search_count"]),
            "resets_at":    usage_result["resets_at"],
        },
        elapsed_ms = elapsed_ms,
        # FIX #11: added required created_at field
        created_at = datetime.now(timezone.utc).isoformat(),
    )


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def _evaluate_claim(claim_text: str, news_result: dict) -> dict:
    """Convert raw news search results into a claim verdict dict."""
    articles = news_result.get("articles", [])

    if not articles:
        return {
            "status":              "UNVERIFIED",
            "confidence":          40.0,
            "evidence_summary":    "No news articles found for this claim.",
            "supporting_articles": [],
        }

    # Count how many articles support vs contradict
    tier1_hit = news_result.get("tier1_hit", False)

    # Simple heuristic: if tier-1 sources found anything, lean toward VERIFIED
    # In a production system you'd use NLP here
    status     = "VERIFIED" if tier1_hit else "UNVERIFIED"
    confidence = 70.0 if tier1_hit else 45.0

    return {
        "status":              status,
        "confidence":          confidence,
        "evidence_summary":    f"{len(articles)} article(s) found.",
        "supporting_articles": [a.get("title", "") for a in articles[:5]],
    }


def _seconds_until_midnight() -> int:
    """Seconds until midnight UTC (when the daily limit resets)."""
    now  = datetime.now(timezone.utc)
    secs = (24 - now.hour) * 3600 - now.minute * 60 - now.second
    return max(0, secs)
