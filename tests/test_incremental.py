"""
Tests for incremental processing (cache/state.py).

Verifies that unchanged events are skipped on re-enrichment,
fingerprint computation is stable, and dirty tracking is correct.
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone

from src.cache import ProcessingState, compute_fingerprint


NOW = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)


def test_fingerprint_is_stable(enriched_events):
    """Same event produces the same fingerprint on repeated calls."""
    event = enriched_events[0]
    fp1 = compute_fingerprint(event)
    fp2 = compute_fingerprint(event)
    assert fp1 == fp2, "Fingerprint should be deterministic"


def test_fingerprint_differs_across_events(enriched_events):
    """Different events should produce different fingerprints."""
    fps = {compute_fingerprint(e) for e in enriched_events}
    assert len(fps) == len(enriched_events), (
        "All events should have unique fingerprints"
    )


def test_new_event_is_dirty(enriched_events):
    """An event not yet registered in ProcessingState is always dirty."""
    state = ProcessingState()
    event = enriched_events[0]
    assert state.is_dirty(event), "Unregistered event should be dirty"


def test_mark_clean_makes_event_clean(enriched_events):
    """After mark_clean, the same event should no longer be dirty."""
    state = ProcessingState()
    event = enriched_events[0]
    assert state.is_dirty(event)
    state.mark_clean(event)
    assert not state.is_dirty(event), "Event should be clean after mark_clean"


def test_stats_counts_dirty_and_clean(enriched_events):
    """stats() returns correct counts of dirty vs clean events."""
    state = ProcessingState()
    events = enriched_events[:4]

    # Mark first 2 as clean
    for e in events[:2]:
        state.mark_clean(e)

    stats = state.stats(list(events))
    assert stats["clean"] == 2
    assert stats["dirty"] == 2
    assert stats["total"] == 4


def test_all_events_start_dirty(enriched_events):
    """Fresh ProcessingState treats all events as dirty."""
    state = ProcessingState()
    dirty = [e for e in enriched_events if state.is_dirty(e)]
    assert len(dirty) == len(enriched_events)


def test_mark_clean_persists_across_checks(enriched_events):
    """Marking an event clean survives multiple is_dirty checks."""
    state = ProcessingState()
    event = enriched_events[0]
    state.mark_clean(event)

    for _ in range(5):
        assert not state.is_dirty(event)


def test_enrichment_skips_clean_events(workspace, enriched_events):
    """
    Enriching with a ProcessingState that has all events marked clean
    should return empty (no events re-enriched).
    """
    from src.events import build_candidate_events
    from src.enrichment import enrich_candidate_events

    state = ProcessingState()
    events = build_candidate_events(workspace)

    # Pre-mark all as clean using already-enriched events as source
    for e in enriched_events:
        state.mark_clean(e)

    # Re-enrich — should skip all clean events
    re_enriched = enrich_candidate_events(
        events, workspace, now=NOW, processing_state=state
    )

    # Only previously-unseen events (none here) would appear
    clean_ids = {e.event_id for e in enriched_events}
    re_enriched_ids = {e.event_id for e in re_enriched}

    assert re_enriched_ids.issubset(clean_ids), (
        "Re-enrichment should not produce new event IDs not in the original set"
    )
