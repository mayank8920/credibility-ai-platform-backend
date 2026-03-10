# =============================================================================
# embedding_service.py — AI embeddings for semantic claim search
# sentence-transformers is optional — if not installed, semantic search
# is disabled and the system falls back to exact hash matching only
# =============================================================================

import logging
import numpy as np
from typing import Optional

logger = logging.getLogger(__name__)

# Try to import sentence-transformers — it's optional
try:
    from sentence_transformers import SentenceTransformer
    EMBEDDINGS_AVAILABLE = True
    logger.info("✅ sentence-transformers available — semantic search enabled")
except ImportError:
    EMBEDDINGS_AVAILABLE = False
    logger.warning("⚠️  sentence-transformers not installed — semantic search disabled, using exact match only")


class EmbeddingService:
    """
    Converts claim text into 384-dimensional vectors for semantic similarity search.
    If sentence-transformers is not installed, all methods return None gracefully
    and the system falls back to exact hash matching.
    """

    def __init__(self):
        self._model = None
        self.model_name = "all-MiniLM-L6-v2"
        self.enabled = EMBEDDINGS_AVAILABLE

    def _load_model(self):
        """Lazy-load the model on first use."""
        if not self.enabled:
            return None
        if self._model is None:
            try:
                logger.info(f"Loading embedding model: {self.model_name}")
                self._model = SentenceTransformer(self.model_name)
                logger.info("✅ Embedding model loaded successfully")
            except Exception as e:
                logger.error(f"Failed to load embedding model: {e}")
                self.enabled = False
        return self._model

    def encode(self, text: str) -> Optional[list]:
        """
        Convert text to a vector.
        Returns None if embeddings are not available.
        """
        if not self.enabled:
            return None
        try:
            model = self._load_model()
            if model is None:
                return None
            vector = model.encode(text, normalize_embeddings=True)
            return vector.tolist()
        except Exception as e:
            logger.error(f"Embedding encode failed: {e}")
            return None

    def encode_batch(self, texts: list[str]) -> Optional[list]:
        """
        Convert multiple texts to vectors.
        Returns None if embeddings are not available.
        """
        if not self.enabled:
            return None
        try:
            model = self._load_model()
            if model is None:
                return None
            vectors = model.encode(texts, normalize_embeddings=True)
            return vectors.tolist()
        except Exception as e:
            logger.error(f"Batch embedding failed: {e}")
            return None

    def is_available(self) -> bool:
        return self.enabled


# Singleton instance used across the app
embedding_service = EmbeddingService()
