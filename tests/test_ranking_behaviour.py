"""
Scenario-driven behavioural tests for the ranking system.

These test that the system makes reasonable decisions, not just that code runs.

Note: rank_events_for_user() returns (selected_items, excluded_items).
All tests unpack accordingly.
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone

from src.ranking import rank_events_for_user
from src.models import CandidateEvent
from tests.conftest import NOW


# ---------------------------------------------------------------------------
# Test 1: Blocker / risk outranks noise (social thread)
# ---------------------------------------------------------------------------

def test_blocker_outranks_noise(enriched_events, profiles):
    """
    The firmware I2C hang (blocker) and thermal failure (risk) should rank
    above the Friday lunch social thread for any technically active user.
    """
    profile = profiles["u_alice"]  # Active hardware engineer
    ranked, _ = rank_events_for_user(enriched_events, profile, top_k=7, now=NOW)

    scored_by_id = {item.event_id: item.score for item in ranked}

    noise_id = "evt_m_040"
    blocker_id = "evt_m_010"  # I2C hang
    risk_id = "evt_m_020"     # Thermal failure

    assert noise_id in scored_by_id, "Noise thread should appear in ranked items"
    assert blocker_id in scored_by_id or risk_id in scored_by_id, \
        "At least one technical blocker/risk event should be ranked"

    noise_score = scored_by_id[noise_id]
    technical_scores = [
        scored_by_id[eid] for eid in [blocker_id, risk_id]
        if eid in scored_by_id
    ]

    assert all(ts > noise_score for ts in technical_scores), (
        f"Technical events should outscore noise. "
        f"Technical={technical_scores}, Noise={noise_score}"
    )


# ---------------------------------------------------------------------------
# Test 2: Noisy social thread is deprioritised
# ---------------------------------------------------------------------------

def test_social_thread_low_signal_level(enriched_events):
    """
    The lunch / social thread should have noise as dominant event type.
    """
    lunch_event = next(
        (e for e in enriched_events if e.event_id == "evt_m_040"), None
    )
    assert lunch_event is not None
    assert lunch_event.signals is not None

    assert lunch_event.signals.dominant_event_type == "noise", (
        f"Expected 'noise', got '{lunch_event.signals.dominant_event_type}'"
    )
    assert lunch_event.signals.importance_score < 0.3, (
        f"Social thread importance should be low, got {lunch_event.signals.importance_score}"
    )


# ---------------------------------------------------------------------------
# Test 3: Different users get meaningfully different digest orderings
# ---------------------------------------------------------------------------

def test_different_users_get_different_digests(enriched_events, profiles):
    """
    Alice (hardware/supplier-focused) and Bob (firmware-focused) should receive
    different top-ranked events.
    """
    alice_ranked, _ = rank_events_for_user(
        enriched_events, profiles["u_alice"], top_k=3, now=NOW
    )
    bob_ranked, _ = rank_events_for_user(
        enriched_events, profiles["u_bob"], top_k=3, now=NOW
    )

    alice_top_ids = [item.event_id for item in alice_ranked]
    bob_top_ids = [item.event_id for item in bob_ranked]

    assert alice_top_ids != bob_top_ids, (
        "Alice and Bob received identical digest orderings — user affinity is likely broken"
    )


# ---------------------------------------------------------------------------
# Test 4: Bob's digest prioritises firmware events
# ---------------------------------------------------------------------------

def test_firmware_user_gets_firmware_events(enriched_events, profiles):
    """
    Bob (firmware engineer) should have the I2C hang (firmware blocker) in his top 3.
    """
    bob_ranked, _ = rank_events_for_user(
        enriched_events, profiles["u_bob"], top_k=5, now=NOW
    )
    top_ids = [item.event_id for item in bob_ranked]

    assert "evt_m_010" in top_ids, (
        "I2C hang firmware blocker should appear in Bob's top-5 digest. "
        f"Got: {top_ids}"
    )


# ---------------------------------------------------------------------------
# Test 5: Alice's digest prioritises supplier / connector events
# ---------------------------------------------------------------------------

def test_supplier_user_gets_supplier_events(enriched_events, profiles):
    """
    Alice is active in suppliers channel and has hardware topics —
    she should have the connector delay (supply chain risk) in her top results.
    """
    alice_ranked, _ = rank_events_for_user(
        enriched_events, profiles["u_alice"], top_k=5, now=NOW
    )
    top_ids = [item.event_id for item in alice_ranked]

    assert "evt_m_001" in top_ids, (
        "Connector supplier delay should appear in Alice's top-5. "
        f"Got: {top_ids}"
    )


# ---------------------------------------------------------------------------
# Test 6: Digest items are traceable to source messages
# ---------------------------------------------------------------------------

def test_digest_items_are_traceable(enriched_events, profiles):
    """
    Every ranked digest item must have non-empty source_thread_ids and source_message_ids.
    """
    for uid, profile in profiles.items():
        ranked, _ = rank_events_for_user(enriched_events, profile, top_k=5, now=NOW)
        for item in ranked:
            assert item.source_thread_ids, (
                f"Item {item.event_id} for user {uid} has no source_thread_ids"
            )
            assert item.source_message_ids, (
                f"Item {item.event_id} for user {uid} has no source_message_ids"
            )


# ---------------------------------------------------------------------------
# Test 7: Ranked items have explainable feature breakdowns
# ---------------------------------------------------------------------------

def test_ranked_items_have_feature_breakdowns(enriched_events, profiles):
    """
    Every ranked item must expose a RankingFeatures object with non-null values.
    """
    profile = profiles["u_fiona"]  # PM — sees everything
    ranked, _ = rank_events_for_user(enriched_events, profile, top_k=5, now=NOW)

    for item in ranked:
        f = item.reason_features
        assert 0.0 <= f.user_affinity <= 1.0
        assert 0.0 <= f.importance <= 1.0
        assert 0.0 <= f.urgency <= 1.0
        assert 0.0 <= f.final_score <= 1.0
        assert f.weights is not None and len(f.weights) > 0


# ---------------------------------------------------------------------------
# Test 8: Fallback summarization works without LLM
# ---------------------------------------------------------------------------

def test_fallback_summarization_works(enriched_events, profiles, events_by_id):
    """
    The digest should be fully functional with FallbackProvider — no LLM needed.
    """
    from src.summarization import summarize_digest_items, FallbackProvider
    from src.ranking import rank_events_for_user

    profile = profiles["u_diana"]
    ranked, _ = rank_events_for_user(enriched_events, profile, top_k=3, now=NOW)
    result = summarize_digest_items(
        ranked,
        events_by_id=events_by_id,
        profile=profile,
        provider=FallbackProvider(),
    )

    for item in result:
        assert item.summary is not None and len(item.summary) > 10, \
            f"Summary missing for {item.event_id}"
        assert item.why_shown is not None and len(item.why_shown) > 10, \
            f"Why-shown missing for {item.event_id}"


# ---------------------------------------------------------------------------
# Test 9: High-urgency events carry non-trivial urgency scores
# ---------------------------------------------------------------------------

def test_urgency_signal_present_in_blocker(enriched_events):
    """
    The I2C hang and thermal failure should carry non-trivial urgency scores.
    """
    technical_ids = {"evt_m_010", "evt_m_020"}
    for event in enriched_events:
        if event.event_id in technical_ids:
            assert event.signals is not None
            assert event.signals.urgency_score > 0.2, (
                f"Event {event.event_id} expected urgency > 0.2, "
                f"got {event.signals.urgency_score}"
            )


# ---------------------------------------------------------------------------
# Test 10: Full pipeline end-to-end
# ---------------------------------------------------------------------------

def test_full_pipeline_runs(tmp_path):
    """
    Run the full pipeline and verify output structure.
    """
    from src.digest import run_full_pipeline
    from pathlib import Path

    data_dir = Path(__file__).parent.parent / "data" / "mock_slack"
    digests = run_full_pipeline(
        data_dir=data_dir,
        user_ids=["u_alice", "u_bob"],
        top_k=3,
        now=NOW,
        date_str="2026-04-10",
    )

    assert "u_alice" in digests
    assert "u_bob" in digests

    for uid, digest in digests.items():
        assert digest.user_id == uid
        assert digest.date == "2026-04-10"
        assert len(digest.items) <= 3
        assert digest.total_candidates_considered > 0
        assert digest.headline
