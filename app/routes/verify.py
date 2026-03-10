# =============================================================================
# app/routes/verify.py — POST /verify  (with Semantic Similarity Search)
# =============================================================================
#
# PLAIN ENGLISH — Updated flow with semantic similarity search:
#
#   STEP 1  Token check           ← is this a real logged-in user?
#   STEP 2  Daily limit check     ← have they hit their 10/day limit?
#   STEP 3  Per-claim cache check ← UPGRADED: now uses semantic search too
#              ↓ EXACT match  → return stored result instantly
#              ↓ SEMANTIC match → "Big bank collapse" matches "Major bank collapsing"
#              ↓ CACHE MISS   → run news search
#   STEP 4  Run scoring engine    ← uses cached + fresh results together
#   STEP 5  Account credibility   ← score the source/account
#   STEP 6  Save to user history  ← write to verification_history
#   STEP 7  Return response       ← includes semantic_match info
#
# KEY CHANGES FROM PREVIOUS VERSION:
#   1. claim_cache.lookup() is now ASYNC — use "await" (semantic search needs it)
#   2. claim_cache.store()  is now ASYNC — use "await" (embedding generation)
#   3. cache_info in the response now includes "semantic_matches" count
#      and "semantic_similarity" for the matched claim's score
#
# WHERE THIS FILE LIVES:
#   credibility-backend/app/routes/verify.py
# =============================================================================

from fastapi import APIRouter, HTTPException, Depends, Request, status
from datetime import datetime, timezone
import asyncio
import time
import hashlib
import logging

from app.models.schemas import (
    VerifyRequest, VerifyResponse,
    ClaimResult, FlagDetail, SubScoreDetail,
    AccountCredibilityDetail,
)
from app.services.scoring_engine import (
    ScoringInput, ClaimInput as EngineClaimInput,
    compute_score, _score_to_verdict,
)
from app.services.account_credibility import (
    AccountInput, analyse_account, blend_scores,
)
from app.services.news_service import search_multiple_claims
from app.services.usage_service import usage_tracker, verification_history
from app.services.claim_cache import claim_cache, CachedClaimResult
from app.middleware.auth import get_current_user
from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()


# =============================================================================
# POST /verify/  — Main verification endpoint (with semantic cache)
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

    Now uses semantic similarity search to match claims that MEAN the same
    thing even when the exact wording differs.

    Example:
        "Major bank collapsing tomorrow"   → first user triggers live search
        "Big bank will collapse tomorrow"  → second user gets instant result!
                                             (93% similarity > 0.85 threshold)

    Authentication: Required (Supabase JWT in Authorization header)
    Rate limit:     10 verifications per day for free users
    """

    # ──────────────────────────────────────────────────────────────────────────
    # STEP 1: Identify the user from their JWT
    # ──────────────────────────────────────────────────────────────────────────
    user_id    = current_user["sub"]
    user_email = current_user.get("email", "")
    start_time = time.time()

    logger.info(
        f"[/verify] START user={user_id[:8]}... "
        f"claims={len(payload.claims)} type={payload.content_type}"
    )

    # ──────────────────────────────────────────────────────────────────────────
    # STEP 2: Check and enforce daily limit
    # ──────────────────────────────────────────────────────────────────────────
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

    # ──────────────────────────────────────────────────────────────────────────
    # STEP 3: Per-claim cache check (UPGRADED with semantic search)
    # ──────────────────────────────────────────────────────────────────────────
    #
    # PLAIN ENGLISH:
    #   For each claim, we now check FOUR layers instead of two:
    #
    #   EXACT MATCH  (same text, different capitalisation/punctuation):
    #     "Vaccines CAUSE autism!!" → same as "vaccines cause autism"
    #     → found via SHA-256 hash in memory or database
    #
    #   SEMANTIC MATCH (different words, same meaning):
    #     "Big bank will collapse tomorrow"
    #     → generates AI embedding → searches for similar stored embeddings
    #     → matches "Major bank collapsing tomorrow" at 93% similarity
    #     → returns stored result WITHOUT running a news search
    #
    #   MISS:
    #     No match found at any similarity level → run live news search

    cached_results:   dict[str, CachedClaimResult] = {}
    claims_to_search: list[str]                     = []
    semantic_match_info: list[dict]                 = []   # for the response

    # ⚠️ claim_cache.lookup is now ASYNC — we run all lookups concurrently
    # for speed (each lookup is independent)
    lookup_tasks = [
        claim_cache.lookup(schema_claim.text.strip())
        for schema_claim in valid_claims
    ]
    lookup_results = await asyncio.gather(*lookup_tasks)

    for schema_claim, cached in zip(valid_claims, lookup_results):
        raw_text = schema_claim.text.strip()

        if cached is not None:
            cached_results[raw_text] = cached

            if cached.semantic_match:
                logger.info(
                    f"[/verify] SEMANTIC HIT: '{raw_text[:60]}' "
                    f"→ '{cached.matched_claim_text[:60]}' "
                    f"similarity={cached.similarity_score:.3f}"
                )
                semantic_match_info.append({
                    "query":             raw_text,
                    "matched_claim":     cached.matched_claim_text,
                    "similarity_score":  round(cached.similarity_score, 3),
                })
            else:
                layer = "memory" if cached.from_memory_cache else "db"
                logger.info(
                    f"[/verify] EXACT HIT [{layer}]: '{raw_text[:60]}' "
                    f"status={cached.status} reuses={cached.verification_count}"
                )
        else:
            claims_to_search.append(raw_text)

    cache_hit_count      = len(cached_results)
    cache_miss_count     = len(claims_to_search)
    semantic_hit_count   = len(semantic_match_info)
    exact_hit_count      = cache_hit_count - semantic_hit_count

    logger.info(
        f"[/verify] Cache summary: "
        f"{exact_hit_count} exact hits, "
        f"{semantic_hit_count} semantic hits, "
        f"{cache_miss_count} misses"
    )

    # ──────────────────────────────────────────────────────────────────────────
    # STEP 4a: Live news search ONLY for cache misses
    # ──────────────────────────────────────────────────────────────────────────
    fresh_news_results: dict[str, dict] = {}

    if claims_to_search:
        try:
            news_results_list = await search_multiple_claims(claims_to_search)
            for claim_text, news in zip(claims_to_search, news_results_list):
                fresh_news_results[claim_text] = news
        except Exception as exc:
            logger.error(f"[/verify] News search failed: {exc}", exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="News search is temporarily unavailable. Please try again shortly.",
            )

    # ──────────────────────────────────────────────────────────────────────────
    # STEP 4b: Evaluate fresh claims and store them (with embeddings)
    # ──────────────────────────────────────────────────────────────────────────
    #
    # For each cache miss, we:
    #   1. Evaluate the news results → get verdict + confidence
    #   2. Store in global_claims (including embedding) → future semantic hits
    #   3. Add to cached_results for use in the scoring engine
    #
    # ⚠️ claim_cache.store is now ASYNC — use "await"

    store_tasks = []
    fresh_claim_verdicts: dict[str, dict] = {}

    for claim_text, news in fresh_news_results.items():
        verdict = _evaluate_claim(claim_text, news)
        fresh_claim_verdicts[claim_text] = verdict

        # Convert to CachedClaimResult so scoring engine can treat it uniformly
        cached_results[claim_text] = CachedClaimResult(
            status               = verdict["status"],
            confidence           = verdict["confidence"],
            evidence_summary     = verdict["evidence_summary"],
            supporting_articles  = verdict["supporting_articles"],
            sources_checked      = news.get("sources_checked", []),
            credibility_score    = verdict["confidence"],
        )

        # Schedule async store (runs after we build the response)
        store_tasks.append(
            claim_cache.store(
                raw_claim_text  = claim_text,
                verdict_data    = verdict,
                sources_checked = news.get("sources_checked", []),
            )
        )

    # Run all store operations concurrently (each independently generates an embedding)
    if store_tasks:
        await asyncio.gather(*store_tasks, return_exceptions=True)

    # ──────────────────────────────────────────────────────────────────────────
    # STEP 5: Run the scoring engine over all claims (cached + fresh)
    # ──────────────────────────────────────────────────────────────────────────
    engine_claims = []
    all_sources:   list[str] = []

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
            evidence_summary  = cr.evidence_summary,
            fact_check_status = schema_claim.fact_check_status,
        ))

    scoring_input = ScoringInput(
        original_content = payload.original_content,
        claims           = engine_claims,
        sources_checked  = list(set(all_sources)),
        content_type     = payload.content_type or "tweet",
    )
    content_result = compute_score(scoring_input)

    # ──────────────────────────────────────────────────────────────────────────
    # STEP 6: Account credibility
    # ──────────────────────────────────────────────────────────────────────────
    acct_input    = AccountInput(
        original_content = payload.original_content,
        source_url       = payload.source_url,
        claims           = [c.text for c in engine_claims],
    )
    account_result        = analyse_account(acct_input)
    final_score, acct_score = blend_scores(
        content_score  = content_result.final_score,
        account_score  = account_result.overall_score,
        account_weight = settings.ACCOUNT_CREDIBILITY_WEIGHT,
    )

    # ──────────────────────────────────────────────────────────────────────────
    # STEP 7: Save to user verification history
    # ──────────────────────────────────────────────────────────────────────────
    elapsed_ms    = int((time.time() - start_time) * 1000)
    final_verdict = _score_to_verdict(final_score)

    claim_results = [
        ClaimResult(
            text                = cr_text,
            status              = cached_results[cr_text].status,
            confidence          = cached_results[cr_text].confidence,
            evidence_summary    = cached_results[cr_text].evidence_summary,
            supporting_articles = cached_results[cr_text].supporting_articles,
            sources_checked     = cached_results[cr_text].sources_checked,
            # NEW: surface semantic match info to the frontend
            semantic_match      = cached_results[cr_text].semantic_match,
            similarity_score    = cached_results[cr_text].similarity_score if cached_results[cr_text].semantic_match else None,
            matched_claim_text  = cached_results[cr_text].matched_claim_text if cached_results[cr_text].semantic_match else None,
        )
        for cr_text in [c.text for c in engine_claims]
        if cr_text in cached_results
    ]

    history_entry = verification_history.save(
        user_id                  = user_id,
        input_text               = payload.original_content,
        claims                   = [c.model_dump() for c in payload.claims],
        credibility_score        = final_score,
        account_credibility_score= acct_score,
        result_json              = {
            "verdict":            final_verdict,
            "cache_hits":         cache_hit_count,
            "semantic_hits":      semantic_hit_count,
            "elapsed_ms":         elapsed_ms,
        },
    )

    logger.info(
        f"[/verify] DONE user={user_id[:8]}... "
        f"score={final_score:.1f} verdict={final_verdict} "
        f"elapsed={elapsed_ms}ms "
        f"exact_hits={exact_hit_count} semantic_hits={semantic_hit_count} misses={cache_miss_count}"
    )

    # ──────────────────────────────────────────────────────────────────────────
    # STEP 8: Build and return the response
    # ──────────────────────────────────────────────────────────────────────────
    return VerifyResponse(
        verification_id           = str(history_entry.get("id", "")),
        credibility_score         = round(final_score, 2),
        account_credibility_score = round(acct_score, 2),
        verdict                   = final_verdict,
        verdict_label             = _verdict_label(final_verdict),
        verdict_color             = _verdict_color(final_verdict),
        confidence_level          = _confidence_level(content_result),
        flags                     = content_result.flags,
        flag_details              = [FlagDetail(**f) for f in content_result.flag_details],
        summary                   = content_result.summary,
        sources_checked           = list(set(all_sources)),
        claims                    = claim_results,
        claims_breakdown          = _claims_breakdown(claim_results),
        sub_scores                = [SubScoreDetail(**s) for s in content_result.sub_scores],
        account_credibility       = AccountCredibilityDetail(
            overall_score = acct_score,
            **account_result.model_dump(exclude={"overall_score"}),
        ),
        cache_info = {
            "hits":              cache_hit_count,
            "misses":            cache_miss_count,
            "total_claims":      len(valid_claims),
            "served_from_cache": cache_hit_count > 0,
            # ── New semantic search fields ─────────────────────────────────
            "exact_hits":        exact_hit_count,
            "semantic_hits":     semantic_hit_count,
            "semantic_matches":  semantic_match_info,  # details of each semantic match
        },
        usage = {
            "search_count": usage_result["search_count"],
            "daily_limit":  daily_limit,
            "remaining":    max(0, daily_limit - usage_result["search_count"]),
            "resets_at":    usage_result["resets_at"],
        },
        elapsed_ms = elapsed_ms,
    )


# =============================================================================
# HELPER FUNCTIONS (unchanged from previous version)
# =============================================================================

def _evaluate_claim(claim_text: str, news_result: dict) -> dict:
    """Convert raw news search results into a verdict dict."""
    articles        = news_result.get("articles", [])
    contradictions  = news_result.get("contradictions", 0)
    confirmations   = news_result.get("confirmations", 0)

    if not articles:
        return {
            "status":             "UNVERIFIED",
            "confidence":         40.0,
            "evidence_summary":   "No news articles found for this claim.",
            "supporting_articles": [],
        }

    if contradictions > confirmations:
        status     = "FALSE"
        confidence = min(90.0, 50.0 + contradictions * 10.0)
    elif confirmations > contradictions:
        status     = "VERIFIED"
        confidence = min(90.0, 50.0 + confirmations * 10.0)
    else:
        status     = "DISPUTED"
        confidence = 50.0

    return {
        "status":             status,
        "confidence":         confidence,
        "evidence_summary":   news_result.get("summary", f"{len(articles)} articles found."),
        "supporting_articles": [a.get("title", "") for a in articles[:5]],
    }


def _seconds_until_midnight() -> int:
    """Seconds until midnight UTC (when the daily limit resets)."""
    now  = datetime.now(timezone.utc)
    secs = (24 - now.hour) * 3600 - now.minute * 60 - now.second
    return max(0, secs)


def _verdict_label(verdict: str) -> str:
    labels = {
        "VERIFIED":    "Verified",
        "MOSTLY_TRUE": "Mostly True",
        "QUESTIONABLE":"Questionable",
        "MISLEADING":  "Misleading",
        "FALSE":       "False",
    }
    return labels.get(verdict, verdict)


def _verdict_color(verdict: str) -> str:
    colors = {
        "VERIFIED":    "#16A34A",
        "MOSTLY_TRUE": "#65A30D",
        "QUESTIONABLE":"#D97706",
        "MISLEADING":  "#EA580C",
        "FALSE":       "#DC2626",
    }
    return colors.get(verdict, "#64748B")


def _confidence_level(content_result) -> str:
    score = getattr(content_result, "confidence", 50.0)
    if score >= 75:
        return "HIGH"
    if score >= 50:
        return "MEDIUM"
    return "LOW"


def _claims_breakdown(claim_results: list) -> dict:
    statuses = [c.status for c in claim_results]
    return {
        "total":      len(statuses),
        "verified":   statuses.count("VERIFIED"),
        "false":      statuses.count("FALSE"),
        "disputed":   statuses.count("DISPUTED"),
        "unverified": statuses.count("UNVERIFIED"),
    }
