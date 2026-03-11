# =============================================================================
# app/services/embedding_service.py — AI embeddings via Jina AI (free tier)
# =============================================================================
#
# WHY JINA AI:
#   • Completely free — 1,000,000 tokens free, no credit card needed
#   • Get your key at: https://jina.ai  (sign in with Google → copy API key)
#   • Zero Docker image bloat — uses httpx which is already in requirements.txt
#   • No new packages needed at all
#
# HOW IT WORKS:
#   One HTTPS POST to api.jina.ai with the claim text.
#   Returns a 768-dimensional embedding vector.
#
# GRACEFUL FALLBACK:
#   If JINA_API_KEY is not set in .env, is_available() returns False.
#   claim_cache.py checks this — if False, semantic search is skipped and
#   the system uses exact SHA-256 hash matching only.
#   Every other feature (verify, history, rate limits) keeps working.
#
# EMBEDDING DIMENSIONS: 768
#   If your Supabase global_claims table currently has a vector column
#   with a different size (384 from sentence-transformers, or 1536 from
#   OpenAI), run this SQL once in the Supabase SQL editor to fix it:
#
#     ALTER TABLE global_claims
#       ALTER COLUMN embedding TYPE vector(768)
#       USING embedding::vector(768);
#
#   Or if the column doesn't exist yet, the migration script handles it.
#
# WHERE THIS FILE LIVES:
#   credibility-backend/app/services/embedding_service.py
# =============================================================================

from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Jina embedding API endpoint and model
JINA_API_URL = "https://api.jina.ai/v1/embeddings"
JINA_MODEL   = "jina-embeddings-v2-base-en"   # 768 dims, English, free tier


class EmbeddingService:
    """
    Converts claim text into 768-dimensional vectors using the Jina AI API.

    Free tier: 1,000,000 tokens — enough for ~500,000 claims.
    No local model, no GPU, no extra packages. Just httpx (already installed).

    If JINA_API_KEY is not set, is_available() returns False and all
    methods return None gracefully. Exact hash matching continues working.
    """

    def __init__(self):
        self.model_name   = JINA_MODEL
        self._api_key:    Optional[str]  = None
        self._enabled:    Optional[bool] = None   # resolved lazily on first use

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _resolve_enabled(self) -> bool:
        """Check once whether Jina embeddings are usable. Result is cached."""
        try:
            from app.config import settings
            key = settings.JINA_API_KEY
        except Exception:
            key = ""

        if not key or not key.strip():
            logger.warning(
                "⚠️  JINA_API_KEY not set — semantic search disabled. "
                "Get a free key at https://jina.ai and add it to your .env: "
                "JINA_API_KEY=jina_xxxxxxxxxxxxxxxx  "
                "Exact hash matching will still work fine without it."
            )
            return False

        self._api_key = key.strip()
        logger.info(
            f"✅ Jina AI embedding service ready "
            f"(model={JINA_MODEL}, dims=768, free tier)"
        )
        return True

    @property
    def enabled(self) -> bool:
        if self._enabled is None:
            self._enabled = self._resolve_enabled()
        return self._enabled

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }

    # ── Public API — used by claim_cache.py ───────────────────────────────────

    async def embed(self, text: str) -> Optional[list]:
        """
        Convert a single claim text into a 768-dimensional embedding vector.

        Called as:  embedding = await embedding_service.embed(claim_text)

        Returns a list of 768 floats on success, None on any failure.
        Failures are logged but never raised — callers treat None as a cache miss
        and proceed with live news search.
        """
        if not self.enabled or not text or not text.strip():
            return None

        payload = {
            "model": JINA_MODEL,
            "input": [{"text": text.strip()}],
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(
                    JINA_API_URL,
                    headers=self._headers(),
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
                return data["data"][0]["embedding"]

        except httpx.TimeoutException:
            logger.warning("[EmbeddingService] Jina API timed out — skipping semantic search for this claim")
            return None
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 402:
                logger.error(
                    "[EmbeddingService] Jina free tier quota exhausted. "
                    "Get a new key or increase quota at https://jina.ai"
                )
            else:
                logger.error(f"[EmbeddingService] Jina API HTTP error {exc.response.status_code}: {exc}")
            return None
        except Exception as exc:
            logger.error(f"[EmbeddingService] embed() failed: {exc}", exc_info=True)
            return None

    async def embed_batch(self, texts: list[str]) -> Optional[list[list]]:
        """
        Embed multiple texts in a single API call (more token-efficient than looping).

        Returns a list of vectors in the same order as input, or None on failure.
        Jina supports up to 2,048 inputs per request.
        """
        if not self.enabled or not texts:
            return None

        clean = [t.strip() for t in texts if t and t.strip()]
        if not clean:
            return None

        payload = {
            "model": JINA_MODEL,
            "input": [{"text": t} for t in clean],
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    JINA_API_URL,
                    headers=self._headers(),
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
                # Jina returns results sorted by index — same order as input
                return [item["embedding"] for item in data["data"]]

        except Exception as exc:
            logger.error(f"[EmbeddingService] embed_batch() failed: {exc}", exc_info=True)
            return None

    # Sync shim kept so nothing breaks if encode() is called somewhere
    def encode(self, text: str) -> Optional[list]:
        """
        Legacy sync shim — always returns None.
        Use `await embed()` everywhere in this codebase.
        """
        logger.warning(
            "[EmbeddingService] encode() called but Jina is async-only. "
            "Use `await embed()` instead. Returning None."
        )
        return None

    def is_available(self) -> bool:
        """Returns True if JINA_API_KEY is set and the service is ready."""
        return self.enabled


# Module-level singleton — imported by claim_cache.py
embedding_service = EmbeddingService()
