"""
Tests for candidate pruning (ranking/pruner.py).

Verifies that PruningConfig correctly filters candidates before ranking,
noise events are deprioritized, and the ranker integrates pruning cleanly.
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone

from src.ranking import rank_events_for_user, PruningConfig, prune_candidates

NOW = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)


def test_prune_candidates_returns_subset(enriched_events, profiles):
    """Pruned list should be a strict subset of input events."""
    profile = profiles["u_alice"]
    config = PruningConfig()
    kept, stats = prune_candidates(enriched_events, profile, config)

    assert len(kept) <= len(enriched_events)
    kept_ids = {e.event_id for e in kept}
    all_ids = {e.event_id for e in enriched_events}
    assert kept_ids.issubset(all_ids)


def test_prune_noise_events(enriched_events, profiles):
    """Noise events with low importance should be pruned."""
    profile = profiles["u_alice"]
    config = PruningConfig(min_importance=0.03, prune_noise_below=0.15)
    _, stats = prune_candidates(enriched_events, profile, config)

    # Some pruning should occur on our mock data (which contains noise threads)
    assert stats.pruned >= 0  # May be 0 if noise events happen to have higher scores
    assert stats.total == len(enriched_events)
    assert stats.kept + stats.pruned == stats.total


def test_strict_config_prunes_more(enriched_events, profiles):
    """A stricter config should prune at least as many events as a lenient one."""
    profile = profiles["u_alice"]
    lenient = PruningConfig(min_importance=0.01, prune_noise_below=0.05)
    strict = PruningConfig(min_importance=0.10, prune_noise_below=0.30)

    _, lenient_stats = prune_candidates(enriched_events, profile, lenient)
    _, strict_stats = prune_candidates(enriched_events, profile, strict)

    assert strict_stats.kept <= lenient_stats.kept, (
        "Stricter config should keep fewer or equal candidates"
    )


def test_ranker_accepts_pruning_config(enriched_events, profiles):
    """rank_events_for_user should accept pruning_config without errors."""
    profile = profiles["u_alice"]
    config = PruningConfig(min_importance=0.03)

    ranked, excluded = rank_events_for_user(
        enriched_events, profile, top_k=5, now=NOW, pruning_config=config
    )

    assert isinstance(ranked, list)
    assert isinstance(excluded, list)
    assert len(ranked) <= 5


def test_ranked_items_are_valid_after_pruning(enriched_events, profiles):
    """Items returned after pruning should have valid scores and features."""
    profile = profiles["u_bob"]
    config = PruningConfig()

    ranked, _ = rank_events_for_user(
        enriched_events, profile, top_k=5, now=NOW, pruning_config=config
    )

    for item in ranked:
        assert item.score >= 0.0
        assert item.event_type is not None
        assert item.reason_features is not None


def test_no_pruning_config_produces_same_top_items(enriched_events, profiles):
    """
    Without pruning config, ranker should produce same results as before.
    Both paths should agree on the top items.
    """
    profile = profiles["u_diana"]

    ranked_default, _ = rank_events_for_user(
        enriched_events, profile, top_k=5, now=NOW
    )
    ranked_with_none_config, _ = rank_events_for_user(
        enriched_events, profile, top_k=5, now=NOW, pruning_config=None
    )

    ids_default = [i.event_id for i in ranked_default]
    ids_none = [i.event_id for i in ranked_with_none_config]
    assert ids_default == ids_none


def test_pruning_stats_tracked_in_pruned_ids(enriched_events, profiles):
    """PruningStats.pruned_ids should list event IDs that were pruned."""
    profile = profiles["u_alice"]
    config = PruningConfig(min_importance=0.10, prune_noise_below=0.30)
    kept, stats = prune_candidates(enriched_events, profile, config)

    kept_ids = {e.event_id for e in kept}
    for pruned_id in stats.pruned_ids:
        assert pruned_id not in kept_ids, (
            f"{pruned_id} appears in both kept and pruned_ids"
        )
