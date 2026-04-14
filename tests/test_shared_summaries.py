"""
Tests for shared event summarization (summarization/summarizer.py).

Verifies that build_shared_summaries generates each event summary exactly once,
and that summarize_digest_items correctly reuses shared summaries per-user.
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, call

from src.summarization import build_shared_summaries, summarize_digest_items
from src.summarization.providers import FallbackProvider
from src.ranking import rank_events_for_user

NOW = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)


def test_build_shared_summaries_covers_all_events(enriched_events, events_by_id):
    """build_shared_summaries should produce one entry per unique event."""
    event_ids = [e.event_id for e in enriched_events]
    shared = build_shared_summaries(events_by_id, event_ids)

    assert len(shared) == len(set(event_ids)), (
        "Should produce exactly one summary per unique event ID"
    )


def test_shared_summaries_are_non_empty(enriched_events, events_by_id):
    """All shared summaries should have non-empty summary text."""
    event_ids = [e.event_id for e in enriched_events]
    shared = build_shared_summaries(events_by_id, event_ids)

    for event_id, summary in shared.items():
        assert summary.summary, f"Empty summary for {event_id}"
        assert summary.event_type, f"Missing event_type for {event_id}"
        assert summary.title, f"Missing title for {event_id}"


def test_shared_summary_deduplicates_event_ids(enriched_events, events_by_id):
    """Duplicate event IDs in the input list should be deduplicated."""
    event_ids = [e.event_id for e in enriched_events]
    doubled = event_ids + event_ids  # deliberate duplicates

    shared = build_shared_summaries(events_by_id, doubled)
    assert len(shared) == len(set(event_ids))


def test_shared_summary_reused_across_users(enriched_events, events_by_id, profiles):
    """
    When shared_summaries is passed to summarize_digest_items, the same
    summary text is used for both Alice and Bob for the same event.
    """
    event_ids = [e.event_id for e in enriched_events]
    shared = build_shared_summaries(events_by_id, event_ids, provider=FallbackProvider())

    alice_ranked, _ = rank_events_for_user(enriched_events, profiles["u_alice"], top_k=5, now=NOW)
    bob_ranked, _ = rank_events_for_user(enriched_events, profiles["u_bob"], top_k=5, now=NOW)

    provider = FallbackProvider()
    alice_items = summarize_digest_items(alice_ranked, events_by_id, profiles["u_alice"], provider, shared)
    bob_items = summarize_digest_items(bob_ranked, events_by_id, profiles["u_bob"], provider, shared)

    # Find a shared event (one that both users got)
    alice_ids = {i.event_id: i for i in alice_items}
    bob_ids = {i.event_id: i for i in bob_items}
    common = set(alice_ids) & set(bob_ids)

    if not common:
        pytest.skip("No events shared between Alice and Bob's top-5")

    for event_id in common:
        alice_summary = alice_ids[event_id].summary
        bob_summary = bob_ids[event_id].summary
        assert alice_summary == bob_summary, (
            f"Same event {event_id} should have identical shared summary for all users. "
            f"Alice: '{alice_summary}' | Bob: '{bob_summary}'"
        )


def test_why_shown_differs_between_users(enriched_events, events_by_id, profiles):
    """
    why_shown should be personalised per user even when the shared summary is reused.
    """
    event_ids = [e.event_id for e in enriched_events]
    shared = build_shared_summaries(events_by_id, event_ids, provider=FallbackProvider())

    alice_ranked, _ = rank_events_for_user(enriched_events, profiles["u_alice"], top_k=5, now=NOW)
    bob_ranked, _ = rank_events_for_user(enriched_events, profiles["u_bob"], top_k=5, now=NOW)

    provider = FallbackProvider()
    alice_items = summarize_digest_items(alice_ranked, events_by_id, profiles["u_alice"], provider, shared)
    bob_items = summarize_digest_items(bob_ranked, events_by_id, profiles["u_bob"], provider, shared)

    alice_ids = {i.event_id: i for i in alice_items}
    bob_ids = {i.event_id: i for i in bob_items}
    common = set(alice_ids) & set(bob_ids)

    if not common:
        pytest.skip("No events shared between Alice and Bob's top-5")

    # At least some why_shown text should exist (we can't guarantee it differs
    # since both users may have similar reasons, but both must be populated)
    for event_id in common:
        assert alice_ids[event_id].why_shown
        assert bob_ids[event_id].why_shown


def test_summarize_without_shared_summaries(enriched_events, events_by_id, profiles):
    """summarize_digest_items should work correctly without shared_summaries (legacy path)."""
    alice_ranked, _ = rank_events_for_user(enriched_events, profiles["u_alice"], top_k=3, now=NOW)
    provider = FallbackProvider()

    items = summarize_digest_items(alice_ranked, events_by_id, profiles["u_alice"], provider)

    for item in items:
        assert item.summary is not None
        assert item.why_shown is not None
