"""
Tests for embedding cache (cache/embedding_cache.py).

Verifies corpus hash computation, cache miss/hit behaviour,
and that EmbeddingStore.load_or_fit works correctly.
"""

from __future__ import annotations

import pytest
import tempfile
from pathlib import Path
from datetime import datetime, timezone

from src.cache import EmbeddingCache, compute_corpus_hash


NOW = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)


def test_corpus_hash_is_stable():
    """Same texts produce the same hash."""
    texts = ["hello world", "firmware blocker", "supply chain risk"]
    h1 = compute_corpus_hash(texts)
    h2 = compute_corpus_hash(texts)
    assert h1 == h2


def test_corpus_hash_differs_on_different_texts():
    """Different corpora produce different hashes."""
    h1 = compute_corpus_hash(["a", "b", "c"])
    h2 = compute_corpus_hash(["a", "b", "d"])
    assert h1 != h2


def test_cache_miss_on_empty_dir():
    """A fresh cache dir produces a miss for any corpus hash."""
    with tempfile.TemporaryDirectory() as tmp:
        cache = EmbeddingCache(Path(tmp))
        assert not cache.is_valid("any_hash")
        assert cache.load("any_hash") is None


def test_cache_save_and_load():
    """Saved embeddings can be reloaded correctly."""
    import numpy as np

    with tempfile.TemporaryDirectory() as tmp:
        cache = EmbeddingCache(Path(tmp))
        corpus_hash = "test_hash_abc"

        embeddings = {
            "evt_001": np.array([0.1, 0.2, 0.3]),
            "evt_002": np.array([0.4, 0.5, 0.6]),
        }
        cache.save(corpus_hash, embeddings)

        assert cache.is_valid(corpus_hash)
        loaded = cache.load(corpus_hash)
        assert loaded is not None
        assert set(loaded.keys()) == {"evt_001", "evt_002"}
        assert abs(loaded["evt_001"][0] - 0.1) < 1e-6


def test_cache_invalidated_on_different_hash():
    """A different corpus hash does not match an existing cached entry."""
    import numpy as np

    with tempfile.TemporaryDirectory() as tmp:
        cache = EmbeddingCache(Path(tmp))
        cache.save("hash_v1", {"evt_001": np.array([0.1, 0.2])})

        assert not cache.is_valid("hash_v2")
        assert cache.load("hash_v2") is None


def test_embedding_store_load_or_fit_builds_store(enriched_events):
    """
    EmbeddingStore.load_or_fit should return a valid store with
    embeddings for all enriched events when no cache dir is provided.
    """
    from src.embeddings import EmbeddingStore

    texts = [e.text_bundle for e in enriched_events]
    keys = [e.event_id for e in enriched_events]

    store = EmbeddingStore.load_or_fit(texts, keys)

    assert store is not None
    assert len(store.all_event_ids) == len(enriched_events)
    for event_id in keys:
        vec = store.get(event_id)
        assert vec is not None, f"Missing embedding for {event_id}"
        assert vec.shape[0] > 0
