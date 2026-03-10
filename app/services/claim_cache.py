# =============================================================================
# app/services/claim_cache.py — Global Claim Database (with Semantic Search)
# =============================================================================
#
# PLAIN ENGLISH — What this file does:
#
#   This file is a "smart librarian" who sits between the user's question
#   and the expensive news search. Now upgraded with SEMANTIC SEARCH — it can
#   find claims that MEAN the same thing even with different wording.
#
#   PREVIOUS FLOW (exact hash only):
#     "Major bank collapsing"  → hash ABC123 → not found → live search
#     "Big bank will collapse" → hash XYZ789 → not found → live search again!
#     (Wasteful — both mean the same thing)
#
#   NEW FLOW (with semantic search):
#     "Major bank collapsing"  → hash ABC123 → not found → semantic search → miss → live search → store
#     "Big bank will collapse" → hash XYZ789 → not found → semantic search → HIT (93% similar)! → return stored result instantly
#
# FOUR-LAYER ARCHITECTURE (was three layers):
#
#   Layer 1: Python in-memory dict (TTLCache)            < 0.001ms
#   ├── Exact hash match only
#   └── Fastest possible — no network, no DB
#
#   Layer 2: Supabase — exact hash lookup (PostgreSQL)   ~10–30ms
#   ├── Still try exact match first (cheaper than vector search)
#   └── WHERE claim_hash = '<sha256>'
#
#   Layer 2b: Supabase — semantic similarity search      ~20–50ms  ← NEW
#   ├── Only runs if Layers 1 and 2 both miss
#   ├── Calls find_similar_claim() SQL function (pgvector)
#   ├── Returns match if cosine similarity > SEMANTIC_SIMILARITY_THRESHOLD (default 0.85)
#   └── Example: "Big bank collapse" matches "Major bank collapsing" at 93%
#
#   Layer 3: Live news API search                        ~500–3000ms
#   ├── Only runs if no match found in layers 1, 2, or 2b
#   └── Result is stored with embedding for future semantic matches
#
# WHERE THIS FILE LIVES:
#   credibility-backend/app/services/claim_cache.py
#
# HOW TO USE IN verify.py (unchanged from before):
#   from app.services.claim_cache import claim_cache
#
#   result = claim_cache.lookup(claim_text)
#   if result:
#       # cache hit (could be exact OR semantic match)
#       use result.semantic_match to know which it was
#   else:
#       # live search, then:
#       await claim_cache.store(claim_text, verdict_data, sources)
# =============================================================================

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from supabase import create_client, Client
from app.config import settings

logger = logging.getLogger(__name__)


# =============================================================================
# DATA TYPES
# =============================================================================

@dataclass
class CachedClaimResult:
    """
    Everything returned when a claim is found in the cache.

    Two new fields added for semantic search transparency:
      semantic_match:    True if this was a semantic (meaning-based) match,
                         False if it was an exact hash match
      similarity_score:  The cosine similarity (0.0–1.0) for semantic matches,
                         1.0 for exact matches
      matched_claim_text: The original claim text that was matched (may differ
                          from the query if it was a semantic match)
    """
    # The claim's verdict from the scoring engine
    status:              str    # "VERIFIED" | "FALSE" | "DISPUTED" | "UNVERIFIED"
    confidence:          float  # 0–100
    evidence_summary:    str
    supporting_articles: list
    sources_checked:     list

    # Scores
    credibility_score:          float
    account_credibility_score:  Optional[float] = None

    # Cache metadata
    claim_hash:          str    = ""
    verification_count:  int    = 1
    first_verified_at:   Optional[str] = None
    last_verified_at:    Optional[str] = None
    from_memory_cache:   bool   = False

    # ── NEW: Semantic search metadata ─────────────────────────────────────────
    semantic_match:      bool   = False   # True = matched by meaning, not exact text
    similarity_score:    float  = 1.0     # 1.0 for exact match, 0.80–0.99 for semantic
    matched_claim_text:  str    = ""      # what was stored (may differ from query)


# =============================================================================
# LAYER 1: IN-MEMORY TTL CACHE (unchanged from previous version)
# =============================================================================

class TTLCache:
    """
    Fixed-size in-memory cache with time-to-live expiry.
    No extra packages needed — uses Python's OrderedDict.
    """

    def __init__(self, max_size: int = 500, ttl_seconds: int = 3600):
        self._store:       OrderedDict[str, tuple[CachedClaimResult, float]] = OrderedDict()
        self._max_size     = max_size
        self._ttl          = ttl_seconds
        self._hits         = 0
        self._misses       = 0

    def get(self, key: str) -> Optional[CachedClaimResult]:
        if key not in self._store:
            self._misses += 1
            return None
        value, expiry = self._store[key]
        if time.time() > expiry:
            del self._store[key]
            self._misses += 1
            return None
        self._store.move_to_end(key)
        self._hits += 1
        return value

    def set(self, key: str, value: CachedClaimResult) -> None:
        if key in self._store:
            self._store.move_to_end(key)
        self._store[key] = (value, time.time() + self._ttl)
        if len(self._store) > self._max_size:
            self._store.popitem(last=False)   # evict oldest entry (LRU)

    def clear(self) -> None:
        self._store.clear()
        self._hits = self._misses = 0

    @property
    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "size":      len(self._store),
            "max_size":  self._max_size,
            "hits":      self._hits,
            "misses":    self._misses,
            "hit_rate":  round(self._hits / total, 3) if total > 0 else 0.0,
            "ttl_sec":   self._ttl,
        }


# =============================================================================
# NORMALISATION AND HASHING (unchanged)
# =============================================================================

def normalize_claim(text: str) -> str:
    """Lowercase, remove punctuation, collapse whitespace."""
    t = text.lower().strip()
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def hash_claim(normalised_text: str) -> str:
    """SHA-256 of the normalised text → 64 hex chars."""
    return hashlib.sha256(normalised_text.encode("utf-8")).hexdigest()


def normalize_and_hash(raw_text: str) -> tuple[str, str]:
    """Convenience: normalise then hash. Returns (normalised, hash)."""
    n = normalize_claim(raw_text)
    return n, hash_claim(n)


# =============================================================================
# LAYER 2 + 2b: GLOBAL CLAIM DATABASE (with Semantic Search)
# =============================================================================

class GlobalClaimDatabase:
    """
    Handles all Supabase interactions for the global claims table.

    Two lookup methods:
      lookup(hash)             → exact match by SHA-256 hash  (Layer 2)
      semantic_lookup(vector)  → cosine similarity search     (Layer 2b)  ← NEW

    IMPORTANT: semantic_lookup is async because it needs to generate an
    embedding before querying the database. The ClaimCacheService.lookup()
    method is therefore also async.
    """

    def __init__(self):
        self._client: Optional[Client] = None

    @property
    def _db(self) -> Client:
        if self._client is None:
            self._client = create_client(
                settings.SUPABASE_URL,
                settings.SUPABASE_SERVICE_ROLE_KEY,
            )
        return self._client

    # ── EXACT HASH LOOKUP (Layer 2) ────────────────────────────────────────────

    def lookup(self, claim_hash: str) -> Optional[CachedClaimResult]:
        """
        Find a claim by its exact SHA-256 hash.
        Returns None if not found.
        """
        try:
            resp = (
                self._db.table("global_claims")
                .select(
                    "claim_text, claim_hash, credibility_score, "
                    "account_credibility_score, sources_checked, "
                    "verification_result, first_verified_at, "
                    "last_verified_at, verification_count"
                )
                .eq("claim_hash", claim_hash)
                .limit(1)
                .execute()
            )
            if resp.data:
                row = resp.data[0]
                return self._row_to_cached_result(row, semantic_match=False, similarity=1.0)
            return None

        except Exception as exc:
            logger.error(f"[GlobalClaimDB] Exact lookup failed: {exc}", exc_info=True)
            return None

    # ── SEMANTIC SIMILARITY LOOKUP (Layer 2b) ──────────────────────────────────

    async def semantic_lookup(
        self,
        raw_claim_text: str,
        threshold: float = 0.85,
    ) -> Optional[CachedClaimResult]:
        """
        Find a semantically similar claim using pgvector cosine similarity.

        Flow:
          1. Generate embedding for the incoming claim (AI model, ~5–20ms)
          2. Call the find_similar_claim() SQL function (pgvector, ~10–30ms)
          3. If the best match has similarity >= threshold → return it
          4. Otherwise → return None (caller will do live news search)

        Args:
            raw_claim_text: The incoming claim text
            threshold:      Minimum cosine similarity to count as a match
                            Default: 0.85 (from settings.SEMANTIC_SIMILARITY_THRESHOLD)

        Returns:
            CachedClaimResult with semantic_match=True if found,
            None if no similar claim exists above the threshold.
        """
        # ── Step 1: generate the embedding ───────────────────────────────────
        # We import here to avoid circular imports
        from app.services.embedding_service import embedding_service

        if not embedding_service.is_available():
            logger.warning("[GlobalClaimDB] Embedding service not available — skipping semantic search")
            return None

        embedding = await embedding_service.embed(raw_claim_text)
        if embedding is None:
            logger.warning("[GlobalClaimDB] Embedding generation returned None — skipping semantic search")
            return None

        # ── Step 2: query the database using the find_similar_claim() function ─
        #
        # PLAIN ENGLISH:
        #   We call the SQL function we created in the schema migration.
        #   It takes our embedding vector and finds the single closest match
        #   in the global_claims table using cosine distance (<=> operator).
        #
        #   The function returns 0 or 1 rows:
        #     0 rows = no claim is similar enough
        #     1 row  = a match was found, with the similarity score
        #
        # FORMAT: pgvector expects the embedding as a string like "[0.12,-0.34,...]"
        embedding_str = "[" + ",".join(f"{x:.8f}" for x in embedding) + "]"

        try:
            resp = self._db.rpc(
                "find_similar_claim",
                {
                    "query_embedding":    embedding_str,
                    "similarity_threshold": threshold,
                },
            ).execute()

        except Exception as exc:
            logger.error(
                f"[GlobalClaimDB] Semantic search RPC failed: {exc}",
                exc_info=True,
            )
            return None

        if not resp.data:
            logger.debug(
                f"[GlobalClaimDB] Semantic MISS for: '{raw_claim_text[:60]}...' "
                f"(no match above {threshold:.0%})"
            )
            return None

        # ── Step 3: check the similarity score ────────────────────────────────
        row = resp.data[0]
        similarity = float(row.get("similarity_score", 0.0))

        if similarity < threshold:
            logger.debug(
                f"[GlobalClaimDB] Semantic NEAR-MISS: best={similarity:.3f} "
                f"< threshold={threshold:.2f} for '{raw_claim_text[:60]}'"
            )
            return None

        # ── Step 4: we have a match! ──────────────────────────────────────────
        matched_text = row.get("claim_text", "")
        logger.info(
            f"[GlobalClaimDB] Semantic HIT: {similarity:.3f} similarity\n"
            f"  Query:   '{raw_claim_text[:80]}'\n"
            f"  Matched: '{matched_text[:80]}'"
        )

        # Record the cache hit so analytics stay accurate
        try:
            self.record_hit(row["claim_hash"])
        except Exception:
            pass   # non-critical

        return self._row_to_cached_result(
            row,
            semantic_match=True,
            similarity=similarity,
        )

    # ── STORE ─────────────────────────────────────────────────────────────────

    def store(
        self,
        claim_text:                str,
        claim_hash:                str,
        credibility_score:         float,
        account_credibility_score: Optional[float],
        sources_checked:           list,
        verification_result:       dict,
    ) -> bool:
        """
        Store a new verified claim in the global_claims table.
        Returns True if the row was newly inserted, False if it already existed.
        (Embedding is stored separately by ClaimCacheService.store() after this call.)
        """
        try:
            resp = self._db.rpc(
                "upsert_global_claim",
                {
                    "p_claim_text":                claim_text,
                    "p_claim_hash":                claim_hash,
                    "p_credibility_score":         credibility_score,
                    "p_account_credibility_score": account_credibility_score,
                    "p_sources_checked":           sources_checked,
                    "p_verification_result":       verification_result,
                },
            ).execute()
            result = (resp.data or [None])[0]
            return result == "inserted"
        except Exception as exc:
            logger.error(f"[GlobalClaimDB] Store failed: {exc}", exc_info=True)
            return False

    def store_embedding(
        self,
        claim_hash:  str,
        embedding:   list[float],
        model_name:  str = "all-MiniLM-L6-v2",
    ) -> None:
        """
        Save the AI embedding for a claim that was just stored.

        This is called AFTER store() because we need the row to exist first.
        Uses the update_claim_embedding() SQL function we created in the migration.

        Args:
            claim_hash: SHA-256 hash of the normalised claim (the row's key)
            embedding:  List of 384 floats from the embedding model
            model_name: Which model was used (for version tracking)
        """
        if not embedding:
            return

        embedding_str = "[" + ",".join(f"{x:.8f}" for x in embedding) + "]"

        try:
            self._db.rpc(
                "update_claim_embedding",
                {
                    "p_claim_hash": claim_hash,
                    "p_embedding":  embedding_str,
                    "p_model":      model_name,
                },
            ).execute()
            logger.debug(
                f"[GlobalClaimDB] Embedding stored: hash={claim_hash[:16]}... "
                f"dims={len(embedding)} model={model_name}"
            )
        except Exception as exc:
            # Non-fatal: if embedding storage fails, the claim is still cached
            # via exact hash; semantic search just won't work for this row
            logger.error(
                f"[GlobalClaimDB] Embedding storage failed (non-fatal): {exc}",
                exc_info=True,
            )

    def record_hit(self, claim_hash: str) -> None:
        """Increment verification_count and update last_verified_at."""
        try:
            self._db.rpc("touch_global_claim", {"p_claim_hash": claim_hash}).execute()
        except Exception as exc:
            logger.warning(f"[GlobalClaimDB] record_hit failed (non-fatal): {exc}")

    def get_stats(self) -> dict:
        """Returns aggregate stats from the claim_cache_stats view."""
        try:
            resp = self._db.table("claim_cache_stats").select("*").execute()
            return resp.data[0] if resp.data else {}
        except Exception as exc:
            logger.error(f"[GlobalClaimDB] get_stats failed: {exc}")
            return {}

    def get_top_claims(self, limit: int = 20) -> list:
        """Returns the most frequently reused claims."""
        try:
            resp = (
                self._db.table("global_claims")
                .select(
                    "claim_text, verification_count, credibility_score, "
                    "verification_result, last_verified_at"
                )
                .order("verification_count", desc=True)
                .limit(limit)
                .execute()
            )
            return resp.data or []
        except Exception as exc:
            logger.error(f"[GlobalClaimDB] get_top_claims failed: {exc}")
            return []

    # ── INTERNAL HELPERS ───────────────────────────────────────────────────────

    @staticmethod
    def _row_to_cached_result(
        row: dict,
        semantic_match: bool,
        similarity: float,
    ) -> CachedClaimResult:
        """Convert a database row dict into a CachedClaimResult object."""
        vr = row.get("verification_result") or {}
        return CachedClaimResult(
            status               = vr.get("status",             "UNVERIFIED"),
            confidence           = float(vr.get("confidence",   50.0)),
            evidence_summary     = vr.get("evidence_summary",   ""),
            supporting_articles  = vr.get("supporting_articles", []),
            sources_checked      = row.get("sources_checked",   []),
            credibility_score    = float(row.get("credibility_score", 50.0)),
            account_credibility_score = (
                float(row["account_credibility_score"])
                if row.get("account_credibility_score") is not None
                else None
            ),
            claim_hash           = row.get("claim_hash",         ""),
            verification_count   = int(row.get("verification_count", 1)),
            first_verified_at    = str(row.get("first_verified_at", "")),
            last_verified_at     = str(row.get("last_verified_at",  "")),
            from_memory_cache    = False,
            # ── Semantic fields ────────────────────────────────────────────────
            semantic_match       = semantic_match,
            similarity_score     = similarity,
            matched_claim_text   = row.get("claim_text", ""),
        )


# =============================================================================
# CLAIM CACHE SERVICE (the unified 4-layer interface)
# =============================================================================

class ClaimCacheService:
    """
    Unified cache service combining all four lookup layers.

    This is the ONLY class that verify.py needs to import.
    Everything else in this file is an implementation detail.

    LOOKUP ORDER:
      1. Memory cache (exact hash)   → < 0.001ms
      2. Database (exact hash)       → ~10–30ms
      2b. Database (semantic search) → ~20–50ms  ← NEW
      3. Return None                 → caller runs live search

    STORE ORDER (after a live search completes):
      1. Store claim row in database
      2. Generate and store embedding in database (enables future semantic matches)
      3. Store in memory cache (enables Layer 1 hits)
    """

    def __init__(
        self,
        memory_cache_size:    int = 500,
        memory_cache_ttl_sec: int = 3600,
    ):
        self._memory = TTLCache(
            max_size    = memory_cache_size,
            ttl_seconds = memory_cache_ttl_sec,
        )
        self._db = GlobalClaimDatabase()

        # Read the semantic search threshold from config
        # Default: 0.85 (85% similarity = same claim)
        self._semantic_threshold = getattr(
            settings, "SEMANTIC_SIMILARITY_THRESHOLD", 0.85
        )
        self._semantic_enabled = getattr(
            settings, "SEMANTIC_SEARCH_ENABLED", True
        )

        logger.info(
            f"[ClaimCache] Init: memory_size={memory_cache_size} "
            f"ttl={memory_cache_ttl_sec}s "
            f"semantic={'ON' if self._semantic_enabled else 'OFF'} "
            f"threshold={self._semantic_threshold}"
        )

    # ── PUBLIC API: LOOKUP ─────────────────────────────────────────────────────

    async def lookup(self, raw_claim_text: str) -> Optional[CachedClaimResult]:
        """
        Look up a claim through all cache layers.

        ⚠️  BREAKING CHANGE: This method is now ASYNC because semantic search
        requires generating an embedding (async operation).
        Update verify.py to use:   cached = await claim_cache.lookup(text)

        Flow:
          1. Normalise + hash the input text
          2. Check in-memory cache (Layer 1) — exact hash
          3. Check database (Layer 2) — exact hash
          4. If semantic search is enabled and embedding service is available:
             Check database (Layer 2b) — cosine similarity
          5. Return result or None

        Args:
            raw_claim_text: The claim as submitted (any case, any punctuation)

        Returns:
            CachedClaimResult if found (check .semantic_match to see which layer matched)
            None if this claim has never been verified before
        """
        if not raw_claim_text or not raw_claim_text.strip():
            return None

        _, claim_hash = normalize_and_hash(raw_claim_text)

        # ── Layer 1: memory cache (exact hash) ────────────────────────────────
        memory_result = self._memory.get(claim_hash)
        if memory_result is not None:
            memory_result.from_memory_cache = True
            logger.info(
                f"[ClaimCache] L1 MEMORY HIT: hash={claim_hash[:16]}... "
                f"status={memory_result.status} "
                f"reuses={memory_result.verification_count}"
            )
            try:
                self._db.record_hit(claim_hash)
            except Exception:
                pass
            return memory_result

        # ── Layer 2: database (exact hash) ────────────────────────────────────
        db_result = self._db.lookup(claim_hash)
        if db_result is not None:
            logger.info(
                f"[ClaimCache] L2 DB EXACT HIT: hash={claim_hash[:16]}... "
                f"status={db_result.status} "
                f"reuses={db_result.verification_count}"
            )
            self._memory.set(claim_hash, db_result)
            return db_result

        # ── Layer 2b: database (semantic similarity) ────────────────────────
        if self._semantic_enabled:
            semantic_result = await self._db.semantic_lookup(
                raw_claim_text=raw_claim_text,
                threshold=self._semantic_threshold,
            )
            if semantic_result is not None:
                logger.info(
                    f"[ClaimCache] L2b SEMANTIC HIT: "
                    f"similarity={semantic_result.similarity_score:.3f} "
                    f"status={semantic_result.status}\n"
                    f"  Query:   '{raw_claim_text[:70]}'\n"
                    f"  Matched: '{semantic_result.matched_claim_text[:70]}'"
                )
                # Store in memory under the QUERY's hash for fast future exact matches
                self._memory.set(claim_hash, semantic_result)
                return semantic_result

        # ── All layers missed ─────────────────────────────────────────────────
        logger.info(
            f"[ClaimCache] ALL MISS: hash={claim_hash[:16]}... "
            f"'{ raw_claim_text[:60]}' → live search required"
        )
        return None

    # ── PUBLIC API: STORE ──────────────────────────────────────────────────────

    async def store(
        self,
        raw_claim_text:             str,
        verdict_data:               dict,
        sources_checked:            list,
        account_credibility_score:  Optional[float] = None,
    ) -> None:
        """
        Store a freshly verified claim in both the database and memory cache.
        Now also generates and stores the embedding for semantic search.

        ⚠️  BREAKING CHANGE: Now ASYNC (embedding generation is async).
        Update verify.py to use:   await claim_cache.store(...)

        Args:
            raw_claim_text:  The original claim text
            verdict_data:    Dict with: status, confidence, evidence_summary,
                             supporting_articles
            sources_checked: List of domain strings
            account_credibility_score: Optional trust score for the source
        """
        if not raw_claim_text or not raw_claim_text.strip():
            return

        normalised, claim_hash = normalize_and_hash(raw_claim_text)
        credibility_score      = float(verdict_data.get("confidence", 50.0))

        verification_result = {
            "status":              verdict_data.get("status",             "UNVERIFIED"),
            "confidence":          credibility_score,
            "evidence_summary":    verdict_data.get("evidence_summary",   ""),
            "supporting_articles": verdict_data.get("supporting_articles", []),
        }

        # ── Step 1: Store the claim row ───────────────────────────────────────
        inserted = self._db.store(
            claim_text                = raw_claim_text,
            claim_hash                = claim_hash,
            credibility_score         = credibility_score,
            account_credibility_score = account_credibility_score,
            sources_checked           = sources_checked,
            verification_result       = verification_result,
        )

        if not inserted:
            # Row already existed (race condition) — still generate embedding
            # in case it was stored without one
            logger.debug(f"[ClaimCache] Row already exists: hash={claim_hash[:16]}...")

        # ── Step 2: Generate and store the embedding ──────────────────────────
        #
        # PLAIN ENGLISH:
        #   We generate the AI embedding for this claim and save it to the
        #   database so that future SIMILAR (but not identical) claims can
        #   find this result through semantic search.
        #
        #   This is the key operation that enables semantic matching.
        #   Without this step, Layer 2b would never find anything.
        #
        try:
            from app.services.embedding_service import embedding_service
            embedding = await embedding_service.embed(raw_claim_text)
            if embedding:
                self._db.store_embedding(
                    claim_hash = claim_hash,
                    embedding  = embedding,
                    model_name = embedding_service.model_name,
                )
        except Exception as exc:
            # Non-fatal: the claim is still cached via exact hash
            # Semantic search just won't work for this specific claim
            logger.error(
                f"[ClaimCache] Embedding generation/storage failed (non-fatal): {exc}",
                exc_info=True,
            )

        # ── Step 3: Update the memory cache ───────────────────────────────────
        cached = CachedClaimResult(
            status               = verification_result["status"],
            confidence           = credibility_score,
            evidence_summary     = verification_result["evidence_summary"],
            supporting_articles  = verification_result["supporting_articles"],
            sources_checked      = sources_checked,
            credibility_score    = credibility_score,
            account_credibility_score = account_credibility_score,
            claim_hash           = claim_hash,
            verification_count   = 1,
            first_verified_at    = datetime.now(timezone.utc).isoformat(),
            last_verified_at     = datetime.now(timezone.utc).isoformat(),
        )
        self._memory.set(claim_hash, cached)
        logger.info(
            f"[ClaimCache] Stored new claim: hash={claim_hash[:16]}... "
            f"status={verification_result['status']} "
            f"score={credibility_score:.1f}"
        )

    # ── PUBLIC API: STATS AND ADMIN ────────────────────────────────────────────

    def get_memory_stats(self) -> dict:
        return self._memory.stats

    def get_db_stats(self) -> dict:
        return self._db.get_stats()

    def get_top_claims(self, limit: int = 20) -> list:
        return self._db.get_top_claims(limit)

    def clear_memory(self) -> None:
        self._memory.clear()
        logger.info("[ClaimCache] In-memory cache cleared.")

    @staticmethod
    def normalize(text: str) -> str:
        return normalize_claim(text)

    @staticmethod
    def hash(text: str) -> str:
        _, h = normalize_and_hash(text)
        return h


# =============================================================================
# MODULE-LEVEL SINGLETON
# =============================================================================

claim_cache = ClaimCacheService(
    memory_cache_size    = getattr(settings, "CLAIM_CACHE_MEMORY_SIZE",    500),
    memory_cache_ttl_sec = getattr(settings, "CLAIM_CACHE_TTL_SECONDS",    3600),
)
