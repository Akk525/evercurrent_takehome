"""
RankingConfig: encapsulates weights and top-k settings.

Allows per-user weight overrides without touching scoring logic.
ML-free — purely rule-based and transparent.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# Global defaults — reused when no override is specified
DEFAULT_WEIGHTS: dict[str, float] = {
    "user_affinity": 0.28,
    "importance": 0.24,
    "urgency": 0.20,
    "momentum": 0.10,
    "novelty": 0.08,
    "recency": 0.05,
    "embedding_affinity": 0.05,  # New: embedding-based semantic affinity
}


@dataclass
class RankingConfig:
    """
    Configuration for the ranking stage.

    weights:            Global default weight vector (must sum to ≤ 1.0 to avoid unbounded scores)
    per_user_weights:   Optional per-user weight overrides (keyed by user_id)
    top_k:              Number of items to select per digest

    Example per-user override — boost urgency for on-call engineers:
        config = RankingConfig(
            per_user_weights={"u_diana": {"urgency": 0.35, "importance": 0.25}}
        )

    Partial overrides are merged with global defaults — you only need to specify
    the weights you want to change.
    """
    weights: dict[str, float] = field(default_factory=lambda: DEFAULT_WEIGHTS.copy())
    per_user_weights: dict[str, dict[str, float]] = field(default_factory=dict)
    top_k: int = 5

    def weights_for_user(self, user_id: str) -> dict[str, float]:
        """
        Return the effective weight vector for a given user.
        User-specific entries override individual keys; unspecified keys use global defaults.
        """
        overrides = self.per_user_weights.get(user_id, {})
        if not overrides:
            return self.weights

        merged = self.weights.copy()
        merged.update(overrides)

        # Normalise so weights sum to 1.0 (prevents score drift from partial overrides)
        total = sum(merged.values())
        if total > 0:
            merged = {k: v / total for k, v in merged.items()}

        return merged
