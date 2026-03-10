# ============================================================
# app/services/news_service.py — Search live news sources
# ============================================================
# Uses two free news APIs to find articles about each claim:
#   • NewsAPI  (newsapi.org  — 100 req/day free)
#   • GNews    (gnews.io     — 100 req/day free)
#
# Together that's 200 free searches per day — plenty for an MVP.
# ============================================================

import httpx
import asyncio
import logging
from typing import List
from app.config import settings

logger = logging.getLogger(__name__)

# ── Trusted high-quality news sources ────────────────────────
# These are weighted more heavily in scoring.
TIER_1_SOURCES = {
    "reuters.com", "apnews.com", "bbc.com", "bbc.co.uk",
    "nytimes.com", "theguardian.com", "washingtonpost.com",
    "bloomberg.com", "economist.com", "ft.com",
    "npr.org", "pbs.org", "cnn.com", "nbcnews.com",
    "abcnews.go.com", "cbsnews.com", "wsj.com",
    "politifact.com", "snopes.com", "factcheck.org",
    "who.int", "cdc.gov", "nasa.gov", "nih.gov",
}


# ─────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────

async def search_claim(claim_text: str) -> dict:
    """
    Main entry point. Searches both NewsAPI and GNews in parallel.
    Returns a combined, deduplicated list of articles and source names.

    Returns:
        {
          "articles": [{"title": ..., "source": ..., "url": ..., "description": ...}],
          "sources_checked": ["Reuters", "BBC News", ...],
          "tier1_hit": bool,       # True if a trusted source was found
          "total_found": int,
        }
    """
    # Run both API calls at the same time (parallel, not sequential)
    newsapi_task = _search_newsapi(claim_text)
    gnews_task   = _search_gnews(claim_text)

    newsapi_results, gnews_results = await asyncio.gather(
        newsapi_task, gnews_task, return_exceptions=True
    )

    articles: List[dict] = []

    if not isinstance(newsapi_results, Exception):
        articles.extend(newsapi_results)
    else:
        logger.warning(f"NewsAPI failed for '{claim_text[:40]}': {newsapi_results}")

    if not isinstance(gnews_results, Exception):
        articles.extend(gnews_results)
    else:
        logger.warning(f"GNews failed for '{claim_text[:40]}': {gnews_results}")

    # Deduplicate by title
    seen_titles = set()
    unique_articles = []
    for a in articles:
        title_key = a["title"].lower()[:60]
        if title_key not in seen_titles:
            seen_titles.add(title_key)
            unique_articles.append(a)

    # Collect unique source names
    sources_checked = list({a["source"] for a in unique_articles if a.get("source")})

    # Check if any Tier-1 source responded
    tier1_hit = any(
        _is_tier1(a.get("url", ""))
        for a in unique_articles
    )

    return {
        "articles": unique_articles[:10],   # cap at 10 per claim
        "sources_checked": sources_checked,
        "tier1_hit": tier1_hit,
        "total_found": len(unique_articles),
    }


async def search_multiple_claims(claims: List[str]) -> List[dict]:
    """
    Searches news for several claims in parallel.
    Returns results in the same order as the input list.
    """
    tasks = [search_claim(c) for c in claims]
    return await asyncio.gather(*tasks)


# ─────────────────────────────────────────────────────────────
# NEWSAPI.ORG
# ─────────────────────────────────────────────────────────────

async def _search_newsapi(query: str) -> List[dict]:
    """
    Calls the NewsAPI /everything endpoint.
    Free plan: 100 requests / day, English sources only.
    """
    if not settings.NEWSAPI_KEY:
        logger.debug("NEWSAPI_KEY not set — skipping NewsAPI search")
        return []

    params = {
        "q": _clean_query(query),
        "language": "en",
        "sortBy": "relevancy",
        "pageSize": 5,
        "apiKey": settings.NEWSAPI_KEY,
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            "https://newsapi.org/v2/everything",
            params=params,
        )
        response.raise_for_status()
        data = response.json()

    if data.get("status") != "ok":
        logger.warning(f"NewsAPI error: {data.get('message')}")
        return []

    articles = []
    for article in data.get("articles", []):
        articles.append({
            "title": article.get("title", ""),
            "source": article.get("source", {}).get("name", "Unknown"),
            "url": article.get("url", ""),
            "description": (article.get("description") or "")[:300],
            "published_at": article.get("publishedAt", ""),
            "provider": "newsapi",
        })

    return articles


# ─────────────────────────────────────────────────────────────
# GNEWS.IO
# ─────────────────────────────────────────────────────────────

async def _search_gnews(query: str) -> List[dict]:
    """
    Calls the GNews /search endpoint.
    Free plan: 100 requests / day, 10 articles per response.
    """
    if not settings.GNEWS_KEY:
        logger.debug("GNEWS_KEY not set — skipping GNews search")
        return []

    params = {
        "q": _clean_query(query),
        "lang": "en",
        "max": 5,
        "token": settings.GNEWS_KEY,
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            "https://gnews.io/api/v4/search",
            params=params,
        )
        response.raise_for_status()
        data = response.json()

    articles = []
    for article in data.get("articles", []):
        source = article.get("source", {})
        articles.append({
            "title": article.get("title", ""),
            "source": source.get("name", "Unknown"),
            "url": article.get("url", ""),
            "description": (article.get("description") or "")[:300],
            "published_at": article.get("publishedAt", ""),
            "provider": "gnews",
        })

    return articles


# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────

def _clean_query(text: str) -> str:
    """Trims and shortens a claim to a search-friendly query."""
    # Remove quotes, trim whitespace, cap at 100 chars
    cleaned = text.replace('"', "").replace("'", "").strip()
    # Take first 10 words to avoid overly specific queries
    words = cleaned.split()[:10]
    return " ".join(words)


def _is_tier1(url: str) -> bool:
    """Returns True if the article URL belongs to a Tier-1 trusted source."""
    url_lower = url.lower()
    return any(domain in url_lower for domain in TIER_1_SOURCES)
