"""
Lightweight candidate pruning.

Applied before per-user ranking to eliminate events with no plausible relevance.
Pruning must be conservative — it is better to keep an event that turns out irrelevant
than to prune one that was important.

Pruning criteria (ALL are optional and configurable):
- Minimum importance threshold: events with importance < threshold are pruned
  (default: 0.03 — very low, only eliminates clear noise)
- Minimum signal filter: prune only if dominant_event_type == 'noise' AND importance < threshold
- Topic overlap: keep if event shares at least one topic with user's top topics
  OR if user has no known topics (don't prune new users)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.models import CandidateEvent, UserContextProfile


@dataclass
class PruningConfig:
    """
    Configuration for the lightweight candidate pruning stage.

    Defaults are deliberately very permissive — pruning should only eliminate
    events that are unambiguously irrelevant (pure noise with very low importance).
    """
    min_importance: float = 0.03       # Very permissive — only prunes clear noise
    prune_noise_below: float = 0.15    # Prune noise events with importance < this
    topic_filter_enabled: bool = True  # Use topic overlap as additional signal
    channel_filter_enabled: bool = False  # Off by default — too aggressive


@dataclass
class PruningStats:
    """Summary statistics from a single pruning pass."""
    total: int
    kept: int
    pruned: int
    pruned_ids: list[str] = field(default_factory=list)


def prune_candidates(
    events: list[CandidateEvent],
    profile: UserContextProfile,
    config: PruningConfig,
) -> tuple[list[CandidateEvent], PruningStats]:
    """
    Apply lightweight candidate pruning before detailed per-user scoring.

    Returns a (kept_events, stats) tuple. Pruning is conservative:
    events are only removed when there is strong evidence of irrelevance.

    Rules applied in order:
    1. Events without signals are always skipped (not counted as pruned).
    2. Prune if importance_score < config.min_importance.
    3. Prune if dominant_event_type == "noise" AND importance_score < config.prune_noise_below.
    4. If topic_filter_enabled AND user has known topics AND event has topic labels:
       - Keep if ANY event topic appears in the user's top 8 topic affinities.
       - But if the user has fewer than 3 known topics, keep anyway (don't over-filter new users).
    5. Otherwise keep.
    """
    kept: list[CandidateEvent] = []
    pruned_ids: list[str] = []

    # Precompute user's top 8 topic affinities (by weight)
    user_topics: set[str] = set()
    if profile.topic_affinities:
        sorted_topics = sorted(
            profile.topic_affinities.items(), key=lambda kv: kv[1], reverse=True
        )
        user_topics = {t for t, _ in sorted_topics[:8]}

    user_has_few_topics = len(profile.topic_affinities) < 3

    for event in events:
        # Events without signals cannot be scored; skip without counting as pruned
        if event.signals is None:
            continue

        signals = event.signals

        # Rule 1: Below minimum importance floor
        if signals.importance_score < config.min_importance:
            pruned_ids.append(event.event_id)
            continue

        # Rule 2: Noise event below noise-specific threshold
        if (
            signals.dominant_event_type == "noise"
            and signals.importance_score < config.prune_noise_below
        ):
            pruned_ids.append(event.event_id)
            continue

        # Rule 3: Topic filter (optional, skipped for new/sparse users)
        if (
            config.topic_filter_enabled
            and user_topics  # User has known topics
            and not user_has_few_topics  # User is not a new/sparse user
            and signals.topic_labels  # Event has topic labels
        ):
            event_topics = set(signals.topic_labels)
            if not event_topics & user_topics:
                pruned_ids.append(event.event_id)
                continue

        kept.append(event)

    total = len(kept) + len(pruned_ids)
    stats = PruningStats(
        total=total,
        kept=len(kept),
        pruned=len(pruned_ids),
        pruned_ids=pruned_ids,
    )
    return kept, stats
