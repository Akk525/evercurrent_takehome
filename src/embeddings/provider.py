"""
Embedding provider abstraction.

Default: TfidfEmbeddingProvider — zero download, fits on the local corpus.
Optional: SentenceTransformerProvider — proper semantic embeddings if installed.

Both expose the same interface: embed(text) -> np.ndarray (L2-normalised).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


class EmbeddingProvider(ABC):
    """Abstract interface for text embedding."""

    @abstractmethod
    def fit(self, texts: list[str]) -> None:
        """Fit the model on a corpus of texts (if needed)."""
        ...

    @abstractmethod
    def embed(self, text: str) -> np.ndarray:
        """Return a normalised 1-D embedding vector for a single text."""
        ...

    def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        """Embed a list of texts. Default: call embed() per item."""
        return [self.embed(t) for t in texts]


class TfidfEmbeddingProvider(EmbeddingProvider):
    """
    TF-IDF based embeddings.

    Lightweight and local — no model downloads required.
    Fit on the corpus at startup to capture domain vocabulary.

    Trade-off: captures lexical similarity well (same jargon → similar vectors)
    but misses true semantic relationships. Good enough for domain-specific
    engineering text with consistent terminology.
    """

    def __init__(self, max_features: int = 1000, ngram_range: tuple = (1, 2)):
        from sklearn.feature_extraction.text import TfidfVectorizer
        self._vectorizer = TfidfVectorizer(
            max_features=max_features,
            ngram_range=ngram_range,
            sublinear_tf=True,  # Log-scale TF to reduce dominance of very frequent terms
            min_df=1,
        )
        self._fitted = False

    def fit(self, texts: list[str]) -> None:
        self._vectorizer.fit(texts)
        self._fitted = True

    def embed(self, text: str) -> np.ndarray:
        if not self._fitted:
            raise RuntimeError("TfidfEmbeddingProvider must be fit() before embed()")
        vec = self._vectorizer.transform([text])
        arr = np.asarray(vec.todense(), dtype=np.float32)[0]
        norm = np.linalg.norm(arr)
        return arr / norm if norm > 0 else arr

    def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        if not self._fitted:
            raise RuntimeError("TfidfEmbeddingProvider must be fit() before embed()")
        matrix = self._vectorizer.transform(texts)
        rows = np.asarray(matrix.todense(), dtype=np.float32)
        result = []
        for row in rows:
            norm = np.linalg.norm(row)
            result.append(row / norm if norm > 0 else row)
        return result


def get_embedding_provider(provider_name: str | None = None) -> EmbeddingProvider:
    """
    Factory: return the configured embedding provider.

    Resolution order:
        1. `provider_name` argument (if passed explicitly)
        2. DIGEST_EMBEDDING_PROVIDER environment variable
        3. Default: "tfidf"

    Valid values:
        "tfidf"                — TF-IDF (default, no downloads required)
        "sentence-transformers" — all-MiniLM-L6-v2 (requires package install)

    Falls back to TF-IDF if sentence-transformers is requested but not installed,
    printing a warning so the misconfiguration is visible.
    """
    import os

    name = provider_name or os.environ.get("DIGEST_EMBEDDING_PROVIDER", "tfidf")

    if name in ("sentence-transformers", "sentence_transformers", "st"):
        try:
            return SentenceTransformerProvider()
        except RuntimeError as e:
            print(f"[embeddings] WARNING: {e}. Falling back to TF-IDF.")
            return TfidfEmbeddingProvider()

    return TfidfEmbeddingProvider()


class SentenceTransformerProvider(EmbeddingProvider):
    """
    Semantic embeddings via sentence-transformers.

    Much better at cross-domain similarity than TF-IDF.
    Requires: pip install sentence-transformers

    Swap in by passing provider=SentenceTransformerProvider() to EmbeddingStore.
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        try:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(model_name)
        except ImportError:
            raise RuntimeError(
                "sentence-transformers not installed. Run: pip install sentence-transformers"
            )

    def fit(self, texts: list[str]) -> None:
        pass  # No fitting required — pre-trained model

    def embed(self, text: str) -> np.ndarray:
        vec = self._model.encode(text, normalize_embeddings=True)
        return np.array(vec, dtype=np.float32)

    def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        vecs = self._model.encode(texts, normalize_embeddings=True)
        return [np.array(v, dtype=np.float32) for v in vecs]
