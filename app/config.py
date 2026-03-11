# =============================================================================
# app/config.py — Centralised settings loaded from .env
# =============================================================================
# ALL secrets come from environment variables — never hardcoded here.
# Add any new secret to .env first, then add the field below.
# =============================================================================

from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):

    # ── App ───────────────────────────────────────────────────────────────────
    ENVIRONMENT: str = "development"
    SECRET_KEY:  str = "change-me-in-production"

    # ── Supabase ──────────────────────────────────────────────────────────────
    SUPABASE_URL:              str
    SUPABASE_SERVICE_ROLE_KEY: str
    SUPABASE_ANON_KEY:         str
    SUPABASE_JWT_SECRET:       str

    # ── Google OAuth ──────────────────────────────────────────────────────────
    GOOGLE_CLIENT_ID:     str = ""
    GOOGLE_CLIENT_SECRET: str = ""

    # ── News APIs ─────────────────────────────────────────────────────────────
    NEWSAPI_KEY: str = ""
    GNEWS_KEY:   str = ""

    # ── CORS ──────────────────────────────────────────────────────────────────
    ALLOWED_ORIGINS_STR: str = "http://localhost:3000"

    @property
    def ALLOWED_ORIGINS(self) -> List[str]:
        return [o.strip() for o in self.ALLOWED_ORIGINS_STR.split(",")]

    # ── Daily Rate Limits ─────────────────────────────────────────────────────
    DAILY_LIMIT_FREE:       int = 10
    DAILY_LIMIT_PRO:        int = 100
    DAILY_LIMIT_ENTERPRISE: int = 999999

    # ── Content Scoring Weights ───────────────────────────────────────────────
    SCORE_VERIFIED_BONUS:     float = 15.0
    SCORE_FALSE_PENALTY:      float = 20.0
    SCORE_DISPUTED_PENALTY:   float = 8.0
    SCORE_UNVERIFIED_PENALTY: float = 3.0
    SCORE_SOURCE_BONUS:       float = 5.0

    # ── Account Credibility ───────────────────────────────────────────────────
    ACCOUNT_CREDIBILITY_WEIGHT: float = 0.15

    # ── Global Claim Cache ────────────────────────────────────────────────────
    CLAIM_CACHE_MEMORY_SIZE: int  = 500
    CLAIM_CACHE_TTL_SECONDS: int  = 3600
    CLAIM_CACHE_ENABLED:     bool = True

    # ── Semantic Similarity Search ────────────────────────────────────────────
    #
    # Master switch. When True and JINA_API_KEY is set, claims with the same
    # meaning but different wording are matched against the cache.
    # When False (or JINA_API_KEY is missing), falls back to exact hash only.
    #
    SEMANTIC_SEARCH_ENABLED:       bool  = True
    SEMANTIC_SIMILARITY_THRESHOLD: float = 0.85

    # ── Jina AI Embeddings (free tier — replaces sentence-transformers) ───────
    #
    # Free at: https://jina.ai — sign in with Google, copy your API key.
    # Free tier: 1,000,000 tokens (~500,000 claims). No credit card needed.
    # Uses httpx (already installed) — zero extra packages, zero image bloat.
    #
    # Add to .env:
    #   JINA_API_KEY=jina_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
    #
    # If left empty, semantic search is disabled automatically.
    # Exact hash matching still works and the app runs normally without it.
    #
    JINA_API_KEY: str = ""

    class Config:
        env_file = ".env"
        extra    = "ignore"   # silently ignore unknown env vars


# Singleton — imported everywhere else
settings = Settings()
