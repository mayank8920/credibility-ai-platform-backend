# =============================================================================
# app/config.py — All settings loaded from .env
# =============================================================================

from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):
    # ── App ───────────────────────────────────────────────────────────────────
    ENVIRONMENT: str = "development"
    SECRET_KEY: str  = "changeme-please-set-in-env"

    # ── Supabase ──────────────────────────────────────────────────────────────
    SUPABASE_URL:              str = ""
    SUPABASE_SERVICE_ROLE_KEY: str = ""
    SUPABASE_ANON_KEY:         str = ""
    SUPABASE_JWT_SECRET:       str = ""

    # ── News APIs ─────────────────────────────────────────────────────────────
    NEWSAPI_KEY: str = ""
    GNEWS_KEY:   str = ""

    # ── AI / Embeddings (Jina AI — free tier) ─────────────────────────────────
    # Get a free key at https://jina.ai — no credit card needed
    JINA_API_KEY: str = ""

    # ── CORS ──────────────────────────────────────────────────────────────────
    # Comma-separated list of allowed frontend origins
    ALLOWED_ORIGINS_STR: str = "http://localhost:3000"

    @property
    def ALLOWED_ORIGINS(self) -> List[str]:
        return [o.strip() for o in self.ALLOWED_ORIGINS_STR.split(",") if o.strip()]

    # ── Rate limits (verifications per day per plan) ──────────────────────────
    DAILY_LIMIT_FREE:       int = 10
    DAILY_LIMIT_PRO:        int = 100
    DAILY_LIMIT_ENTERPRISE: int = 999_999

    # ── Claim cache ───────────────────────────────────────────────────────────
    CLAIM_CACHE_ENABLED:     bool = True
    CLAIM_CACHE_MEMORY_SIZE: int  = 500     # max entries in the in-process dict
    CLAIM_CACHE_TTL_SECONDS: int  = 3600    # 1 hour

    # ── Semantic search ───────────────────────────────────────────────────────
    SEMANTIC_SEARCH_ENABLED:       bool  = True
    SEMANTIC_SIMILARITY_THRESHOLD: float = 0.85   # 0.85 = 85% meaning similarity

    # ── Account credibility blend weight ─────────────────────────────────────
    # 0.15 means account score contributes 15%, content score 85%
    ACCOUNT_CREDIBILITY_WEIGHT: float = 0.15

    model_config = {
        "env_file": ".env",
        "extra": "ignore",
    }


settings = Settings()
