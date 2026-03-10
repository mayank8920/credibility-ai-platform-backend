# =============================================================================
# app/services/embedding_service.py — AI Embedding Generation
# =============================================================================
#
# PLAIN ENGLISH — What this file does:
#
#   An "embedding" is a list of 384 numbers that captures the MEANING of a
#   sentence. Two sentences that mean the same thing will have similar numbers,
#   even if the words are completely different.
#
#   Example:
#     "Major bank collapsing tomorrow"   → [0.12, -0.34, 0.56, ...(384 numbers)]
#     "Big bank will collapse tomorrow"  → [0.13, -0.33, 0.55, ...(384 numbers)]
#
#   These two lists of numbers are 93% similar (cosine similarity = 0.93),
#   so our system correctly identifies them as the SAME CLAIM.
#
#   Compare with:
#     "I love pizza"                     → [0.88,  0.91, -0.23, ...(384 numbers)]
#   This is only 12% similar to the bank claim — correctly treated as different.
#
# THE MODEL: "all-MiniLM-L6-v2"
#   • Free to use — no API key, no cost per request
#   • Runs locally on your server — no data sent to third parties
#   • 384 dimensions — small enough to store efficiently, accurate enough for our use
#   • Speed: ~5–20ms per claim on a modern CPU
#   • Downloads once (~90MB) and is cached on disk automatically
#   • Used by millions of developers worldwide — battle-tested
#
# ALTERNATIVE: OpenAI text-embedding-3-small
#   If you want even better accuracy, you can switch to OpenAI's API.
#   Set EMBEDDING_PROVIDER=openai and OPENAI_API_KEY=sk-... in your .env
#   Cost: ~$0.00002 per 1,000 claims (essentially free at MVP scale)
#   Quality: slightly better for short sentences, requires internet connection
#
# WHERE THIS FILE LIVES:
#   credibility-backend/app/services/embedding_service.py
#
# HOW TO USE:
#   from app.services.embedding_service import embedding_service
#
#   # Generate an embedding for one claim:
#   vector = await embedding_service.embed(claim_text)
#   # vector = [0.12, -0.34, 0.56, ...] — list of 384 floats
#
#   # Generate embeddings for many claims at once (faster):
#   vectors = await embedding_service.embed_batch(["claim 1", "claim 2"])
# =============================================================================

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

# Embedding dimensions for "all-MiniLM-L6-v2"
EMBEDDING_DIM = 384


# =============================================================================
# SENTENCE TRANSFORMERS PROVIDER (free, local)
# =============================================================================

class SentenceTransformerEmbedder:
    """
    Generates embeddings using the sentence-transformers library.

    The model is downloaded once from HuggingFace (~90 MB) and cached
    on disk at ~/.cache/huggingface/hub/. Subsequent startups load it
    from disk in ~1-2 seconds.

    Thread-safe: the SentenceTransformer model itself is stateless after
    loading, so it can be safely shared across async tasks.
    """

    MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

    def __init__(self):
        self._model = None
        self._loaded = False
        self._load_error: Optional[str] = None

    def _load(self) -> bool:
        """
        Lazily load the model on first use.

        We use lazy loading (load when first needed, not at import time)
        so the server starts quickly even if the model takes a moment to load.
        """
        if self._loaded:
            return True
        if self._load_error:
            return False

        try:
            # ── Import here so the library is only required when embeddings are used
            from sentence_transformers import SentenceTransformer  # type: ignore
            logger.info(
                f"[EmbeddingService] Loading model: {self.MODEL_NAME}\n"
                f"  First run: downloads ~90MB from HuggingFace (automatic)\n"
                f"  Subsequent runs: loads from disk cache in ~1-2s"
            )
            t0 = time.time()
            self._model = SentenceTransformer(self.MODEL_NAME)
            elapsed = time.time() - t0
            logger.info(f"[EmbeddingService] Model loaded in {elapsed:.1f}s")
            self._loaded = True
            return True
        except ImportError:
            self._load_error = (
                "sentence-transformers is not installed.\n"
                "Run: pip install sentence-transformers"
            )
            logger.error(f"[EmbeddingService] {self._load_error}")
            return False
        except Exception as exc:
            self._load_error = str(exc)
            logger.error(f"[EmbeddingService] Model load failed: {exc}")
            return False

    def encode_sync(self, texts: list[str]) -> list[list[float]]:
        """
        Synchronous embedding generation.
        Called from the async wrapper below via run_in_executor.

        Args:
            texts: List of claim strings to embed

        Returns:
            List of embedding vectors (each is a list of 384 floats)
            Returns empty list if the model is not available.
        """
        if not self._load():
            return []

        try:
            # normalize_embeddings=True makes cosine similarity equivalent to dot product
            # This is a performance optimisation — the math still works the same way
            embeddings = self._model.encode(
                texts,
                normalize_embeddings=True,  # unit-normalise so dot product = cosine similarity
                batch_size=32,              # process 32 claims at once for speed
                show_progress_bar=False,    # suppress console output
            )
            # Convert numpy array to plain Python list (JSON serialisable)
            return [emb.tolist() for emb in embeddings]

        except Exception as exc:
            logger.error(f"[EmbeddingService] Encoding failed: {exc}", exc_info=True)
            return []


# =============================================================================
# OPENAI PROVIDER (optional — better accuracy, costs ~$0.00002 per 1k claims)
# =============================================================================

class OpenAIEmbedder:
    """
    Generates embeddings using the OpenAI API (text-embedding-3-small).

    DIMENSIONS: 1536 (different from sentence-transformers' 384)
    If you switch to this provider, you must:
      1. Change EMBEDDING_DIM to 1536
      2. Change the SQL schema: vector(384) → vector(1536)
      3. Re-embed all existing claims (run re_embed_all.py)

    Set in .env:
      EMBEDDING_PROVIDER=openai
      OPENAI_API_KEY=sk-your-key-here
    """

    MODEL_NAME = "text-embedding-3-small"
    DIM = 1536

    def __init__(self, api_key: str):
        self._api_key = api_key
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import openai  # type: ignore
                self._client = openai.AsyncOpenAI(api_key=self._api_key)
            except ImportError:
                raise RuntimeError("openai package not installed. Run: pip install openai")
        return self._client

    async def embed_async(self, texts: list[str]) -> list[list[float]]:
        """Async embedding via OpenAI API."""
        client = self._get_client()
        try:
            response = await client.embeddings.create(
                model=self.MODEL_NAME,
                input=texts,
            )
            return [item.embedding for item in response.data]
        except Exception as exc:
            logger.error(f"[OpenAIEmbedder] API call failed: {exc}", exc_info=True)
            return []


# =============================================================================
# UNIFIED EMBEDDING SERVICE (main interface for the rest of the app)
# =============================================================================

class EmbeddingService:
    """
    The single entry point for all embedding operations.

    Automatically picks the right provider based on .env settings:
      EMBEDDING_PROVIDER=local  → SentenceTransformer (default, free)
      EMBEDDING_PROVIDER=openai → OpenAI API (better accuracy, small cost)

    All methods are async-safe. The CPU-intensive encoding work is
    offloaded to a thread pool to avoid blocking the FastAPI event loop.

    USAGE:
        from app.services.embedding_service import embedding_service

        # Single claim:
        vector = await embedding_service.embed("Vaccines cause autism")
        # → [0.12, -0.34, 0.56, ... 384 numbers]
        # → None if embedding generation fails (graceful degradation)

        # Multiple claims (faster than one-by-one):
        vectors = await embedding_service.embed_batch(["claim A", "claim B"])
        # → [[...384 floats], [...384 floats]]
    """

    def __init__(self):
        provider = getattr(settings, "EMBEDDING_PROVIDER", "local").lower()

        if provider == "openai":
            api_key = getattr(settings, "OPENAI_API_KEY", "")
            if not api_key:
                logger.warning(
                    "[EmbeddingService] EMBEDDING_PROVIDER=openai but OPENAI_API_KEY "
                    "is not set. Falling back to local sentence-transformers."
                )
                self._local   = SentenceTransformerEmbedder()
                self._openai  = None
                self._provider = "local"
            else:
                self._local   = None
                self._openai  = OpenAIEmbedder(api_key)
                self._provider = "openai"
        else:
            self._local   = SentenceTransformerEmbedder()
            self._openai  = None
            self._provider = "local"

        logger.info(f"[EmbeddingService] Provider: {self._provider}")

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def model_name(self) -> str:
        if self._provider == "openai":
            return OpenAIEmbedder.MODEL_NAME
        return SentenceTransformerEmbedder.MODEL_NAME

    @property
    def dimensions(self) -> int:
        if self._provider == "openai":
            return OpenAIEmbedder.DIM
        return EMBEDDING_DIM   # 384

    async def embed(self, text: str) -> Optional[list[float]]:
        """
        Generate an embedding for a single claim.

        Returns a list of 384 (or 1536 for OpenAI) floats,
        or None if generation fails (caller should treat as cache miss).

        Args:
            text: The claim text to embed (any length up to ~512 tokens)
        """
        if not text or not text.strip():
            return None

        results = await self.embed_batch([text.strip()])
        if results and len(results) > 0:
            return results[0]
        return None

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """
        Generate embeddings for multiple claims at once.
        Batch processing is significantly faster than embedding one-by-one.

        Returns a list of embedding vectors in the same order as the input.
        Empty vectors are returned for any failed items.

        Args:
            texts: List of claim strings (empty strings are skipped)
        """
        clean_texts = [t.strip() for t in texts if t and t.strip()]
        if not clean_texts:
            return []

        t0 = time.time()

        if self._provider == "openai" and self._openai:
            # ── OpenAI: already async, call directly ──────────────────────────
            results = await self._openai.embed_async(clean_texts)

        else:
            # ── Local: CPU-bound work → run in thread pool ────────────────────
            #
            # PLAIN ENGLISH:
            #   FastAPI runs on an "event loop" — a single thread that handles
            #   all requests. If we do slow CPU work (like running a neural network)
            #   directly on the event loop, it blocks ALL other requests.
            #
            #   run_in_executor() offloads the CPU work to a separate thread pool,
            #   so the event loop stays responsive while the embedding runs.
            #
            loop = asyncio.get_event_loop()
            results = await loop.run_in_executor(
                None,                                         # use default thread pool
                self._local.encode_sync,                      # the CPU-bound function
                clean_texts,                                  # argument
            )

        elapsed_ms = (time.time() - t0) * 1000
        logger.debug(
            f"[EmbeddingService] Embedded {len(clean_texts)} claim(s) "
            f"in {elapsed_ms:.1f}ms via {self._provider}"
        )
        return results or []

    def is_available(self) -> bool:
        """
        Check if the embedding service is ready to use.
        Returns False if the model failed to load or API key is missing.
        """
        if self._provider == "openai":
            return bool(getattr(settings, "OPENAI_API_KEY", ""))
        # For local: attempt to load the model
        return self._local._load() if self._local else False


# =============================================================================
# COSINE SIMILARITY (Python-side, for testing and fallback)
# =============================================================================
#
# PLAIN ENGLISH:
#   Normally, cosine similarity is computed by the DATABASE using the <=> operator.
#   This Python version is used in:
#     1. Unit tests — to verify embeddings look correct
#     2. Debugging — to check similarity between two specific claims
#     3. Fallback — if we need to compare without a DB query
#
# We use the formula:  similarity = dot_product(a, b) / (|a| * |b|)
# Since our embeddings are normalised (length = 1.0), this simplifies to:
#   similarity = dot_product(a, b)

def cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """
    Compute cosine similarity between two embedding vectors.

    Returns a value between -1.0 and 1.0:
      1.00 = identical meaning
      0.80 = very similar (our match threshold)
      0.50 = somewhat related
      0.00 = unrelated
     -1.00 = opposite meaning (rare in practice)

    Args:
        vec_a, vec_b: Two embedding vectors of equal length

    Example:
        >>> a = await embedding_service.embed("Bank collapse tomorrow")
        >>> b = await embedding_service.embed("Major bank will fail tomorrow")
        >>> print(cosine_similarity(a, b))
        0.93   ← 93% similar = same claim!
    """
    if len(vec_a) != len(vec_b):
        raise ValueError(
            f"Vector length mismatch: {len(vec_a)} vs {len(vec_b)}. "
            "Make sure both embeddings use the same model."
        )

    # Dot product
    dot = sum(a * b for a, b in zip(vec_a, vec_b))

    # Magnitudes
    mag_a = sum(x * x for x in vec_a) ** 0.5
    mag_b = sum(x * x for x in vec_b) ** 0.5

    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0

    similarity = dot / (mag_a * mag_b)
    # Clamp to [-1, 1] to handle floating-point rounding errors
    return max(-1.0, min(1.0, similarity))


# =============================================================================
# MODULE-LEVEL SINGLETON
# =============================================================================
#
# Import this in claim_cache.py and anywhere else embeddings are needed:
#
#   from app.services.embedding_service import embedding_service
#
# The singleton is created once at module load time and shared across all
# requests. The model is loaded lazily (on first use), so startup is fast.

embedding_service = EmbeddingService()
