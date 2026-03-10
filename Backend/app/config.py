# =============================================================================
# app/config.py — Centralised settings loaded from .env
# =============================================================================
# ALL secrets come from environment variables — never hardcoded here.
# Pydantic BaseSettings reads them automatically from the .env file.
#
# To add a new secret:
#   1. Add it to .env.example with a placeholder value
#   2. Add it here as a field
#   3. Use it anywhere by importing: from app.config import settings
# =============================================================================

from pydantic_settings import BaseSettings
from typing import List


class Settings(BaseSettings):

    # ── App ───────────────────────────────────────────────────────────────────
    ENVIRONMENT: str = "development"   # development | production
    SECRET_KEY:  str = "change-me-in-production"

    # ── Supabase ──────────────────────────────────────────────────────────────
    # All found in: Supabase Dashboard → Settings → API
    SUPABASE_URL:              str
    SUPABASE_SERVICE_ROLE_KEY: str   # Full access — server-side ONLY
    SUPABASE_ANON_KEY:         str   # Public key — respects RLS
    SUPABASE_JWT_SECRET:       str   # Used to verify user tokens

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

    # ── Global Claim Cache (exact hash) ───────────────────────────────────────
    #
    # CLAIM_CACHE_MEMORY_SIZE:
    #   How many claims to hold in the fast in-memory cache.
    #   Each entry uses roughly 2-5 KB. 500 entries ≈ 1-2 MB RAM.
    CLAIM_CACHE_MEMORY_SIZE: int  = 500
    CLAIM_CACHE_TTL_SECONDS: int  = 3600   # 1 hour
    CLAIM_CACHE_ENABLED:     bool = True

    # ── Semantic Similarity Search (NEW) ──────────────────────────────────────
    #
    # SEMANTIC_SEARCH_ENABLED:
    #   Master switch for semantic similarity search.
    #   When True, claims that MEAN the same thing (even with different words)
    #   are matched and served from cache without running a new news search.
    #
    #   Set to False to fall back to exact-hash-only matching (faster, less smart).
    #   Default: True
    #
    SEMANTIC_SEARCH_ENABLED: bool = True

    # SEMANTIC_SIMILARITY_THRESHOLD:
    #   Minimum cosine similarity score (0.0–1.0) to consider two claims a match.
    #
    #   HOW TO CHOOSE A THRESHOLD:
    #     0.95 = Very strict — nearly identical wording required
    #            ("Bank collapse" vs "Bank collapses" — YES)
    #            ("Major bank collapses" vs "Big bank fails" — probably NO)
    #
    #     0.85 = Recommended — same claim, different phrasing
    #            ("Major bank collapsing tomorrow" vs "Big bank will collapse tomorrow" — YES)
    #            ("Bank scandal" vs "Bank collapse" — probably NO)
    #
    #     0.75 = Lenient — related topic (may be too broad)
    #            ("Bank in trouble" vs "Financial sector crisis" — maybe YES)
    #            Risk: different claims being treated as the same
    #
    #   We recommend 0.85 as a safe starting point.
    #   Lower it carefully if you see valid paraphrases being missed.
    #   Raise it if you see different claims being merged incorrectly.
    #   Default: 0.85
    #
    SEMANTIC_SIMILARITY_THRESHOLD: float = 0.85

    # EMBEDDING_PROVIDER:
    #   Which AI model to use for generating embeddings.
    #
    #   "local"  → sentence-transformers/all-MiniLM-L6-v2  (DEFAULT)
    #              Free, runs on your server, no API key needed
    #              Downloads ~90MB model on first run (cached automatically)
    #              Speed: ~5-20ms per claim on a modern CPU
    #              Dimensions: 384
    #
    #   "openai" → OpenAI text-embedding-3-small
    #              Requires OPENAI_API_KEY to be set
    #              Cost: ~$0.00002 per 1,000 claims (essentially free at MVP scale)
    #              Speed: ~100-300ms per claim (network dependent)
    #              Dimensions: 1536 (requires schema change if switching from "local")
    #
    #   IMPORTANT: Do not switch providers after you have stored embeddings.
    #   Different providers produce incompatible embeddings — all existing
    #   rows would need to be re-embedded. Stick with "local" for the MVP.
    #   Default: "local"
    #
    EMBEDDING_PROVIDER: str = "local"

    # OPENAI_API_KEY:
    #   Required only when EMBEDDING_PROVIDER=openai.
    #   Get one at: platform.openai.com → API keys
    #   Leave empty when using the local provider.
    #
    OPENAI_API_KEY: str = ""

    # EMBEDDING_CACHE_WARM_ON_STARTUP:
    #   When True, the embedding model is loaded into memory when the server
    #   starts (rather than on the first request).
    #
    #   Pro: first user doesn't wait for model to load (~2s)
    #   Con: server startup takes ~2 seconds longer
    #   Default: True
    #
    EMBEDDING_CACHE_WARM_ON_STARTUP: bool = True

    class Config:
        env_file = ".env"
        extra    = "ignore"   # silently ignore unknown env vars


# Singleton — imported everywhere else
settings = Settings()
