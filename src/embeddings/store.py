"""
EmbeddingStore: fits on the workspace corpus, caches per-text embeddings,
and provides cosine similarity utilities.

Usage:
    store = EmbeddingStore()
    store.fit(event_texts, event_ids)
    topic_scores = store.topic_similarity_scores(event_id)
    novelty = store.novelty_score(event_id, other_event_ids)
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .provider import EmbeddingProvider, TfidfEmbeddingProvider
from .prototypes import TOPIC_PROTOTYPES, EVENT_TYPE_PROTOTYPES


def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two L2-normalised vectors."""
    dot = float(np.dot(a, b))
    # Vectors are already normalised by the provider — no division needed
    # but clamp for floating-point safety
    return max(-1.0, min(1.0, dot))


class EmbeddingStore:
    """
    Central embedding cache for the pipeline run.

    Fit once on all event texts + prototypes, then query by event_id.
    """

    def __init__(self, provider: EmbeddingProvider | None = None):
        self._provider = provider or TfidfEmbeddingProvider()
        self._cache: dict[str, np.ndarray] = {}  # key → embedding
        self._topic_vecs: dict[str, np.ndarray] = {}
        self._event_type_vecs: dict[str, np.ndarray] = {}
        self._fitted = False

    def fit_and_embed(
        self,
        texts: list[str],
        keys: list[str],
    ) -> None:
        """
        Fit the provider on all texts (events + prototypes) and cache embeddings.

        texts and keys must be the same length.
        """
        all_prototype_texts = list(TOPIC_PROTOTYPES.values()) + list(EVENT_TYPE_PROTOTYPES.values())
        all_texts = texts + all_prototype_texts

        self._provider.fit(all_texts)
        self._fitted = True

        # Embed and cache event texts
        event_embeddings = self._provider.embed_batch(texts)
        for key, emb in zip(keys, event_embeddings):
            self._cache[key] = emb

        # Embed and cache topic prototypes
        topic_keys = list(TOPIC_PROTOTYPES.keys())
        topic_texts = list(TOPIC_PROTOTYPES.values())
        topic_embeddings = self._provider.embed_batch(topic_texts)
        for k, emb in zip(topic_keys, topic_embeddings):
            self._topic_vecs[k] = emb

        # Embed and cache event type prototypes
        et_keys = list(EVENT_TYPE_PROTOTYPES.keys())
        et_texts = list(EVENT_TYPE_PROTOTYPES.values())
        et_embeddings = self._provider.embed_batch(et_texts)
        for k, emb in zip(et_keys, et_embeddings):
            self._event_type_vecs[k] = emb

    def embed_text(self, text: str) -> np.ndarray:
        """Embed an arbitrary text (e.g., user profile text)."""
        if not self._fitted:
            raise RuntimeError("EmbeddingStore must be fit before use")
        return self._provider.embed(text)

    def topic_similarity_scores(self, key: str) -> dict[str, float]:
        """
        Return cosine similarity between a cached event embedding and each
        topic prototype. Returns scores in [0, 1] range (clipped from [-1, 1]).
        """
        emb = self._cache.get(key)
        if emb is None:
            return {}
        return {
            topic: round(max(0.0, _cosine_sim(emb, proto_vec)), 3)
            for topic, proto_vec in self._topic_vecs.items()
        }

    def novelty_score(self, key: str, other_keys: list[str]) -> float:
        """
        Novelty = 1 - average cosine similarity to all other events.
        High novelty means this event is semantically distinct.
        """
        emb = self._cache.get(key)
        if emb is None or not other_keys:
            return 0.5

        sims = []
        for other_key in other_keys:
            other_emb = self._cache.get(other_key)
            if other_emb is not None:
                sims.append(max(0.0, _cosine_sim(emb, other_emb)))

        if not sims:
            return 0.9

        avg_sim = sum(sims) / len(sims)
        return round(max(0.0, 1.0 - avg_sim), 3)

    def user_profile_affinity(self, key: str, profile_text: str) -> float:
        """
        Cosine similarity between a cached event embedding and a user's
        inferred interest text (synthesised from their active topics).
        """
        emb = self._cache.get(key)
        if emb is None:
            return 0.0
        profile_emb = self.embed_text(profile_text)
        return round(max(0.0, _cosine_sim(emb, profile_emb)), 3)

    def has(self, key: str) -> bool:
        return key in self._cache

    def get(self, key: str) -> np.ndarray | None:
        """Return the cached embedding for a key, or None if not found."""
        return self._cache.get(key)

    @property
    def all_event_ids(self) -> list[str]:
        """All event_ids currently cached in the store."""
        return list(self._cache.keys())

    # ------------------------------------------------------------------
    # Cache-aware classmethod
    # ------------------------------------------------------------------

    @classmethod
    def load_or_fit(
        cls,
        texts: list[str],
        keys: list[str],
        cache_dir: Path | None = None,
        provider: EmbeddingProvider | None = None,
    ) -> "EmbeddingStore":
        """
        Build an EmbeddingStore, reusing cached embeddings if the corpus is unchanged.

        If cache_dir is None, behaves exactly like creating a store and calling fit_and_embed.
        If cache_dir is set and cache is valid, skips embed_batch calls but still re-fits
        the provider so embed_text() / user_profile_affinity() works correctly at ranking time.
        If cache is stale, refits, embeds, and saves a new cache.
        """
        store = cls(provider=provider)

        if cache_dir is None:
            store.fit_and_embed(texts, keys)
            return store

        from src.cache.embedding_cache import EmbeddingCache, compute_corpus_hash

        # Hash includes all texts passed to provider.fit() (events + prototypes)
        proto_topic_texts = list(TOPIC_PROTOTYPES.values())
        proto_et_texts = list(EVENT_TYPE_PROTOTYPES.values())
        all_corpus_texts = texts + proto_topic_texts + proto_et_texts
        corpus_hash = compute_corpus_hash(all_corpus_texts)

        cache = EmbeddingCache(Path(cache_dir))
        cached = cache.load(corpus_hash)

        if cached is not None:
            # Restore pre-computed embeddings from disk
            for key, vec in cached.items():
                if key.startswith("topic__"):
                    store._topic_vecs[key[len("topic__"):]] = vec
                elif key.startswith("et__"):
                    store._event_type_vecs[key[len("et__"):]] = vec
                else:
                    store._cache[key] = vec

            # IMPORTANT: re-fit the provider so embed_text() works for ranking-time queries.
            # Only batch embeddings are cached; the vectorizer state is not.
            store._provider.fit(all_corpus_texts)
            store._fitted = True
        else:
            # Cache miss: full fit + embed, then persist
            store.fit_and_embed(texts, keys)

            # Flatten all embeddings into a single dict using key conventions
            flat: dict[str, np.ndarray] = {}
            flat.update(store._cache)
            for topic_name, vec in store._topic_vecs.items():
                flat[f"topic__{topic_name}"] = vec
            for et_name, vec in store._event_type_vecs.items():
                flat[f"et__{et_name}"] = vec

            cache.save(corpus_hash, flat)

        return store
