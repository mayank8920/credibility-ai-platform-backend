# =============================================================================
# app/routes/verify.py — POST /verify/
# =============================================================================
#
# FIXES APPLIED IN THIS VERSION:
#
#   F-03  VerificationHistoryService.save() was missing 9 required NOT NULL
#         columns → every save threw a Postgres constraint violation → 500.
#         FIX: replaced usage_service.verification_history with
#              database.verifications_db which has a complete save() method.
#
#   F-04  _evaluate_claim() returned VERIFIED for ANY tier-1 hit regardless
#         of what the article actually said (e.g. "Vaccines cause autism" →
#         BBC article debunking it → VERIFIED). Scoring engine's FALSE and
#         DISPUTED penalties were never triggered.
#         FIX: Added headline contradiction detection. Titles containing
#              debunk/false/myth/disprove signals alongside claim keywords
#              now return DISPUTED or FALSE instead of VERIFIED.
#
#   F-08  require_quota (rate_limit.py) was never used in this route.
#         usage_tracker.check_and_increment() ran a parallel counter in
#         usage_tracking while the middleware read from usage_limits → two
#         diverging counters, UI always showed 0.
#         FIX: removed the inline usage_tracker call entirely.
#              Added _rate_check: None = Depends(require_quota) dependency
#              which consolidates rate limiting through rate_limit.py →
#              usage_limits table → get_usage_status() RPC.
#              usage_tracker is still imported for the /usage/today response.
# =============================================================================

import dataclasses
import asyncio
import time
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends, Request, status
from fastapi.concurrency import run_in_threadpool

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

# F-03 FIX: import verifications_db from database.py (has all required columns)
# Remove: from app.services.usage_service import usage_tracker, verification_history
from app.services.database import verifications_db
from app.services.usage_service import usage_tracker   # kept for usage info in response

from app.services.claim_cache import claim_cache, CachedClaimResult
from app.middleware.auth import get_current_user

# F-08 FIX: import require_quota so rate limiting uses the consolidated path
from app.middleware.rate_limit import require_quota

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

# F-04: Keywords in article headlines that signal a claim is being CONTRADICTED
_CONTRADICTION_SIGNALS = [
    "debunked", "debunks", "false", "not true", "no evidence",
    "misleading", "misinformation", "disproved", "disproves",
    "myth", "hoax", "fake", "fabricated", "incorrect",
    "fact check", "fact-check", "corrects", "correction",
    "refutes", "refuted", "disputed", "wrong", "inaccurate",
    "no proof", "unproven", "baseless", "unfounded",
]

# F-04: Keywords that confirm a claim is being SUPPORTED
_SUPPORT_SIGNALS = [
    "confirms", "confirmed", "verifies", "verified",
    "study finds", "research shows", "scientists say",
    "officially", "according to", "data shows",
]


# =============================================================================
# POST /verify/
# =============================================================================

@router.post(
    "/",
    response_model=VerifyResponse,
    status_code=status.HTTP_200_OK,
    summary="Verify content credibility",
)
async def verify(
    payload:      VerifyRequest,
    request:      Request,
    current_user: dict = Depends(get_current_user),
    # F-08 FIX: rate limiting now flows through require_quota → usage_limits table
    _rate_check:  None = Depends(require_quota),
):
    """
    Run a credibility verification on submitted content.

    Authentication: Required (Supabase JWT in Authorization header)
    Rate limit:     Enforced via require_quota dependency (usage_limits table)
    """

    # ── STEP 1: Identify user from JWT ────────────────────────────────────────
    user_id    = current_user.get("id") or current_user.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Could not identify user.")
    start_time = time.time()

    logger.info(
        f"[/verify] START user={user_id[:8]}... "
        f"claims={len(payload.claims)} type={payload.content_type}"
    )

    valid_claims = [c for c in payload.claims if c.text.strip()]
    if not valid_claims:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="All claim texts were empty after trimming whitespace.",
        )

    # ── STEP 2: Per-claim cache lookup (exact + semantic) ─────────────────────
    cached_results:      dict[str, CachedClaimResult] = {}
    claims_to_search:    list[str]                     = []
    semantic_match_info: list[dict]                    = []

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

    # ── STEP 3: Live news search for cache misses ─────────────────────────────
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

    # ── STEP 4: Evaluate fresh claims + store with embeddings ─────────────────
    store_tasks = []

    for claim_text, news in fresh_news_results.items():
        # F-04 FIX: _evaluate_claim now does headline contradiction detection
        verdict = _evaluate_claim(claim_text, news)

        cached_results[claim_text] = CachedClaimResult(
            status               = verdict["status"],
            confidence           = verdict["confidence"],
            evidence_summary     = verdict["evidence_summary"],
            supporting_articles  = verdict["supporting_articles"],
            sources_checked      = news.get("sources_checked", []),
            credibility_score    = verdict["confidence"],
        )

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

        engine_claims.append(EngineClaimInput(
            text              = raw_text,
            status            = cr.status,
            confidence        = cr.confidence,
            fact_check_status = schema_claim.fact_check_status,
        ))

    unique_sources     = list(set(all_sources))
    credible_count     = sum(1 for s in unique_sources if any(d in s for d in _TIER1_DOMAINS))
    total_source_count = len(unique_sources)

    fact_check_matches = [
        c.fact_check_status
        for c in valid_claims
        if c.fact_check_status is not None
    ]

    scoring_input = ScoringInput(
        original_content      = payload.original_content,
        claims                = engine_claims,
        credible_source_count = credible_count,
        total_source_count    = total_source_count,
        fact_check_matches    = fact_check_matches,
        content_type          = payload.content_type or "tweet",
    )
    content_result = compute_score(scoring_input)

    # ── STEP 6: Account credibility ───────────────────────────────────────────
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
    acct_score     = account_result.account_credibility_score

    final_score = blend_scores(
        content_score = content_result.credibility_score,
        account_score = acct_score,
        weight        = settings.ACCOUNT_CREDIBILITY_WEIGHT,
    )

    # ── STEP 7: Build claim results ───────────────────────────────────────────
    elapsed_ms                      = int((time.time() - start_time) * 1000)
    verdict_code, verdict_label, verdict_color = _score_to_verdict(final_score)

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

    claims_breakdown = {
        "total":      len(claim_results),
        "verified":   sum(1 for c in claim_results if c.status == "VERIFIED"),
        "false":      sum(1 for c in claim_results if c.status == "FALSE"),
        "disputed":   sum(1 for c in claim_results if c.status == "DISPUTED"),
        "unverified": sum(1 for c in claim_results if c.status == "UNVERIFIED"),
    }

    # ── STEP 8: Save to verification history ──────────────────────────────────
    # F-03 FIX: use verifications_db.save() which writes all NOT NULL columns.
    # The old verification_history.save() only wrote 7 fields and caused
    # a Postgres NOT NULL violation on verdict/verdict_label/etc every time.
    try:
        history_entry = await run_in_threadpool(lambda: verifications_db.save(
            user_id            = user_id,
            input_text         = payload.original_content,
            claims             = [c.model_dump() for c in payload.claims],
            claims_total       = claims_breakdown["total"],
            claims_verified    = claims_breakdown["verified"],
            claims_false       = claims_breakdown["false"],
            claims_disputed    = claims_breakdown["disputed"],
            claims_unverified  = claims_breakdown["unverified"],
            credibility_score  = final_score,
            verdict            = verdict_code,
            verdict_label      = verdict_label,
            verdict_color      = verdict_color,
            summary            = content_result.summary,
            flags              = content_result.flags,
            confidence_level   = content_result.confidence_level,
            result_json        = {
                "verdict":       verdict_code,
                "cache_hits":    cache_hit_count,
                "semantic_hits": semantic_hit_count,
                "elapsed_ms":    elapsed_ms,
            },
            sources_consulted  = unique_sources,
            source_url         = payload.source_url,
            content_type       = payload.content_type or "tweet",
            processing_time_ms = elapsed_ms,
        ))
    except Exception as exc:
        # Log the failure but don't block the response — the user should
        # still get their result even if the history save fails.
        logger.error(f"[/verify] History save failed: {exc}", exc_info=True)
        history_entry = {}

    logger.info(
        f"[/verify] DONE user={user_id[:8]}... "
        f"score={final_score:.1f} verdict={verdict_code} "
        f"elapsed={elapsed_ms}ms hits={cache_hit_count} misses={cache_miss_count}"
    )

    # ── STEP 9: Build and return response ────────────────────────────────────
    account_credibility = AccountCredibilityDetail(
        account_credibility_score = account_result.account_credibility_score,
        flags                     = account_result.flags,
        flag_details              = [
            FlagDetail(**fd) for fd in account_result.flag_details
        ],
        domain_tier       = account_result.domain_tier,
        source_type       = account_result.source_type,
        analysis_note     = account_result.analysis_note,
        data_completeness = account_result.data_completeness,
    )

    sub_scores = [
        SubScoreDetail(**dataclasses.asdict(s))
        for s in content_result.sub_scores
    ]

    # Get current usage for the response (read-only, does not increment)
    usage_status = await run_in_threadpool(usage_db.get_status, user_id)

    return VerifyResponse(
        verification_id   = str(history_entry.get("id", "")),
        credibility_score = round(final_score, 2),
        verdict           = verdict_code,
        verdict_label     = verdict_label,
        verdict_color     = verdict_color,
        confidence_level  = content_result.confidence_level,
        flags             = content_result.flags,
        flag_details      = [FlagDetail(**f) for f in content_result.flag_details],
        summary           = content_result.summary,
        penalty_total     = content_result.penalty_total,
        sources_consulted = unique_sources,
        claim_results     = claim_results,
        claims_breakdown  = claims_breakdown,
        sub_scores        = sub_scores,
        account_credibility = account_credibility,
        cache_info = {
            "hits":              cache_hit_count,
            "misses":            cache_miss_count,
            "total_claims":      len(valid_claims),
            "served_from_cache": cache_hit_count > 0,
            "exact_hits":        exact_hit_count,
            "semantic_hits":     semantic_hit_count,
            "semantic_matches":  semantic_match_info,
        },
        usage = {
            "search_count": usage_status["search_count"],
            "daily_limit":  usage_status["daily_limit"],
            "remaining":    usage_status["remaining"],
            "resets_at":    usage_status["resets_at"],
        },
        elapsed_ms = elapsed_ms,
        created_at = datetime.now(timezone.utc).isoformat(),
    )


# =============================================================================
# HELPER: _evaluate_claim
# =============================================================================
# F-04 FIX: Previous version returned VERIFIED for ANY tier-1 hit regardless
# of what the article said. A BBC article debunking "vaccines cause autism"
# would trigger tier1_hit=True → status="VERIFIED" → inflated score.
#
# New logic:
#   1. Scan every article title for contradiction signals (debunked, false, myth…)
#      combined with keywords from the claim text.
#   2. If contradiction signals found in ≥1 title → DISPUTED (or FALSE if multiple)
#   3. If no contradiction but tier-1 hit → VERIFIED
#   4. If no tier-1 hit but articles found → UNVERIFIED
#   5. No articles → UNVERIFIED
# =============================================================================

def _evaluate_claim(claim_text: str, news_result: dict) -> dict:
    """
    Convert raw news search results into a claim verdict.

    Returns a dict with keys: status, confidence, evidence_summary,
    supporting_articles.
    """
    articles = news_result.get("articles", [])

    if not articles:
        return {
            "status":              "UNVERIFIED",
            "confidence":          40.0,
            "evidence_summary":    "No news articles found for this claim.",
            "supporting_articles": [],
        }

    tier1_hit = news_result.get("tier1_hit", False)

    # Extract meaningful keywords from the claim (3+ letter words, no stop words)
    _STOP = {
        "the", "and", "for", "are", "was", "were", "that", "this",
        "with", "has", "have", "had", "but", "not", "from", "they",
        "will", "all", "can", "its", "been", "who", "did", "into",
    }
    claim_keywords = [
        w.lower() for w in claim_text.split()
        if len(w) >= 4 and w.lower() not in _STOP
    ]

    contradiction_count = 0
    supporting_titles   = []

    for article in articles:
        title = (article.get("title") or "").lower()
        if not title:
            continue

        # Check if this title mentions any claim keyword
        title_mentions_claim = any(kw in title for kw in claim_keywords)

        if title_mentions_claim:
            # Check if the title contains contradiction signals
            has_contradiction = any(sig in title for sig in _CONTRADICTION_SIGNALS)
            if has_contradiction:
                contradiction_count += 1
            else:
                supporting_titles.append(article.get("title", ""))
        else:
            # Title doesn't mention the claim — still include in headline list
            supporting_titles.append(article.get("title", ""))

    total_articles = len(articles)

    # ── Determine verdict from contradiction analysis ─────────────────────────
    if contradiction_count >= 2:
        # Multiple headlines directly contradict this claim
        status     = "FALSE"
        confidence = 85.0
        evidence_summary = (
            f"{contradiction_count} of {total_articles} article(s) directly contradict "
            f"this claim. Multiple independent sources debunk it."
        )
    elif contradiction_count == 1:
        # One headline contradicts — disputed, not definitively false
        status     = "DISPUTED"
        confidence = 65.0
        evidence_summary = (
            f"1 of {total_articles} article(s) contradicts this claim. "
            f"Further verification is recommended."
        )
    elif tier1_hit and supporting_titles:
        # Tier-1 source found, no contradictions — verified
        status     = "VERIFIED"
        confidence = 72.0
        evidence_summary = (
            f"{total_articles} article(s) found from credible sources. "
            f"No contradictions detected."
        )
    elif total_articles > 0:
        # Articles found but no tier-1 source and no direct contradiction
        status     = "UNVERIFIED"
        confidence = 48.0
        evidence_summary = (
            f"{total_articles} article(s) found but none from Tier-1 sources. "
            f"Cannot independently verify this claim."
        )
    else:
        status     = "UNVERIFIED"
        confidence = 40.0
        evidence_summary = "No relevant news articles found for this claim."

    return {
        "status":              status,
        "confidence":          confidence,
        "evidence_summary":    evidence_summary,
        "supporting_articles": [a.get("title", "") for a in articles[:5]],
    }


def _seconds_until_midnight() -> int:
    """Seconds until midnight UTC (when the daily limit resets)."""
    now  = datetime.now(timezone.utc)
    secs = (24 - now.hour) * 3600 - now.minute * 60 - now.second
    return max(0, secs)
