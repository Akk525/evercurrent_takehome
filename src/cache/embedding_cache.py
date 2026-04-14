"""
Disk-based cache for EmbeddingStore state.

Cache key is a hash of all event texts + all prototype texts combined.
If the hash matches, the cached embeddings can be reused.
The cache is stored as a single npz file + a metadata JSON file.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np


def compute_corpus_hash(texts: list[str]) -> str:
    """
    Compute an MD5 hash of all texts joined in order.

    Deterministic: same texts in same order → same hash.
    """
    combined = "\n".join(texts)
    return hashlib.md5(combined.encode("utf-8")).hexdigest()


class EmbeddingCache:
    """
    Simple disk cache for EmbeddingStore pre-computed vectors.

    Layout:
        cache_dir/
            embedding_meta.json   — {"corpus_hash": "<md5>"}
            embedding_data.npz    — {key: np.ndarray, ...}

    Keys in the npz follow the EmbeddingStore naming conventions:
        - event embeddings: key = event_id
        - topic embeddings: key = "topic__{topic_name}"
        - event type embeddings: key = "et__{et_name}"
    """

    def __init__(self, cache_dir: Path) -> None:
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    @property
    def _meta_path(self) -> Path:
        return self._cache_dir / "embedding_meta.json"

    @property
    def _data_path(self) -> Path:
        return self._cache_dir / "embedding_data.npz"

    def is_valid(self, corpus_hash: str) -> bool:
        """
        Return True if the metadata file exists and contains the matching hash.
        """
        if not self._meta_path.exists():
            return False
        try:
            meta = json.loads(self._meta_path.read_text(encoding="utf-8"))
            return meta.get("corpus_hash") == corpus_hash
        except (json.JSONDecodeError, OSError):
            return False

    def load(self, corpus_hash: str) -> dict[str, np.ndarray] | None:
        """
        Load cached embeddings if the hash matches.

        Returns:
            A dict mapping key → np.ndarray if the cache is valid, else None.
        """
        if not self.is_valid(corpus_hash):
            return None
        if not self._data_path.exists():
            return None
        try:
            npz = np.load(str(self._data_path), allow_pickle=False)
            return {key: npz[key] for key in npz.files}
        except (OSError, ValueError):
            return None

    def save(self, corpus_hash: str, embeddings: dict[str, np.ndarray]) -> None:
        """
        Persist embeddings to disk and record the corpus hash.

        Args:
            corpus_hash: MD5 hash of the corpus used to produce these embeddings.
            embeddings:  Flat dict of {key: np.ndarray}.
        """
        # Save metadata first so that partial writes don't produce a stale hit
        meta = {"corpus_hash": corpus_hash}
        self._meta_path.write_text(json.dumps(meta), encoding="utf-8")

        # np.savez expects keyword arguments; unpack the dict
        np.savez(str(self._data_path), **embeddings)
