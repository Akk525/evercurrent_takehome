"""
Tests for the shared context and misalignment detection layer.

Covers:
  1. Empty events → empty SharedContextView
  2. High-importance event → appears in globally_critical
  3. High cross_functional_score event → appears in cross_functional_hotspots
  4. Low-importance events → not in globally_critical
  5. Misalignment detected when high importance + many participants + differing affinities
  6. shared_context_score is in [0, 1] for all items
  7. SharedContextView serializes to dict (model_dump works)
  8. Misalignment NOT detected when participants align with event topic
  9. Cross-functional hotspot requires participant count >= 3
  10. detect_misalignments returns empty list for empty events
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.models import CandidateEvent, UserContextProfile
from src.models.derived import SemanticSignals, EventTypeDistribution
from src.digest.shared_context import build_shared_context, detect_misalignments
from src.digest.shared_context_models import (
    SharedContextView,
    SharedContextItem,
    MisalignmentSignal,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)


def _make_event(
    event_id: str = "evt_001",
    importance: float = 0.0,
    urgency: float = 0.0,
    unresolved: float = 0.0,
    cross_functional: float = 0.0,
    momentum: float = 0.0,
    dominant_event_type: str = "status_update",
    topic_labels: list[str] | None = None,
    participant_ids: list[str] | None = None,
    unique_participant_count: int = 2,
) -> CandidateEvent:
    signals = SemanticSignals(
        title=f"Event {event_id}",
        topic_labels=topic_labels or ["firmware"],
        event_type_dist=EventTypeDistribution(),
        dominant_event_type=dominant_event_type,
        urgency_score=urgency,
        momentum_score=momentum,
        novelty_score=0.5,
        unresolved_score=unresolved,
        importance_score=importance,
        cross_functional_score=cross_functional,
        confidence=0.7,
    )
    return CandidateEvent(
        event_id=event_id,
        thread_id=f"thread_{event_id}",
        channel_id="C_general",
        participant_ids=participant_ids or ["U1", "U2"],
        message_ids=["M1", "M2"],
        started_at=_NOW,
        last_activity_at=_NOW,
        text_bundle="some text",
        message_count=5,
        reply_count=4,
        unique_participant_count=unique_participant_count,
        total_reactions=2,
        signals=signals,
    )


def _make_profile(
    user_id: str,
    primary_topic: str = "firmware",
    primary_event_type: str = "blocker",
) -> UserContextProfile:
    return UserContextProfile(
        user_id=user_id,
        topic_affinities={primary_topic: 0.8, "supply_chain": 0.2},
        event_type_affinities={primary_event_type: 0.7, "status_update": 0.3},
        active_channel_ids=["C_general"],
        frequent_collaborators=[],
        recent_thread_ids=[],
        activity_level=0.5,
        interaction_weights={},
    )


# ---------------------------------------------------------------------------
# Test 1: Empty events → empty SharedContextView
# ---------------------------------------------------------------------------

def test_empty_events_returns_empty_view():
    view = build_shared_context([], {})
    assert isinstance(view, SharedContextView)
    assert view.globally_critical == []
    assert view.cross_functional_hotspots == []
    assert view.misalignments == []


# ---------------------------------------------------------------------------
# Test 2: High-importance event appears in globally_critical
# ---------------------------------------------------------------------------

def test_high_importance_event_in_globally_critical():
    event = _make_event(
        event_id="evt_blocker",
        importance=0.80,
        urgency=0.70,
        dominant_event_type="blocker",
        participant_ids=["U1", "U2"],
        unique_participant_count=2,
    )
    view = build_shared_context([event], {})
    assert len(view.globally_critical) == 1
    assert view.globally_critical[0].event_id == "evt_blocker"


# ---------------------------------------------------------------------------
# Test 3: High cross_functional_score event appears in cross_functional_hotspots
# ---------------------------------------------------------------------------

def test_high_cf_event_in_hotspots():
    event = _make_event(
        event_id="evt_cf",
        cross_functional=0.75,
        importance=0.55,
        participant_ids=["U1", "U2", "U3"],
        unique_participant_count=3,
    )
    view = build_shared_context([event], {})
    assert len(view.cross_functional_hotspots) == 1
    assert view.cross_functional_hotspots[0].event_id == "evt_cf"


# ---------------------------------------------------------------------------
# Test 4: Low-importance events are NOT in globally_critical
# ---------------------------------------------------------------------------

def test_low_importance_not_in_globally_critical():
    events = [
        _make_event("e1", importance=0.3, urgency=0.2, unresolved=0.2),
        _make_event("e2", importance=0.4, urgency=0.1, unresolved=0.3),
    ]
    view = build_shared_context(events, {})
    assert view.globally_critical == []


# ---------------------------------------------------------------------------
# Test 5: Misalignment detected — high importance, many participants, differing topics
# ---------------------------------------------------------------------------

def test_misalignment_detected_with_differing_affinities():
    # Event is about "firmware" but one participant's primary affinity is "supply_chain"
    event = _make_event(
        event_id="evt_misalign",
        importance=0.75,
        cross_functional=0.6,
        dominant_event_type="blocker",
        topic_labels=["firmware"],
        participant_ids=["U1", "U2", "U3"],
        unique_participant_count=3,
    )
    profiles = {
        "U1": _make_profile("U1", primary_topic="firmware"),
        "U2": _make_profile("U2", primary_topic="supply_chain"),  # different camp
        "U3": _make_profile("U3", primary_topic="supply_chain"),  # different camp
    }
    misalignments = detect_misalignments([event], profiles)
    assert len(misalignments) == 1
    m = misalignments[0]
    assert m.misalignment_flag is True
    assert m.event_id == "evt_misalign"
    assert m.confidence > 0.0
    assert len(m.misalignment_reason) > 0


# ---------------------------------------------------------------------------
# Test 6: shared_context_score is in [0, 1] for all globally_critical items
# ---------------------------------------------------------------------------

def test_shared_context_score_in_range():
    events = [
        _make_event(
            event_id=f"evt_{i}",
            importance=0.7 + i * 0.05,
            urgency=0.6,
            cross_functional=0.5 + i * 0.05,
            participant_ids=["U1", "U2"],
            unique_participant_count=2,
        )
        for i in range(4)
    ]
    view = build_shared_context(events, {})
    for item in view.globally_critical:
        assert 0.0 <= item.shared_context_score <= 1.0, (
            f"shared_context_score out of range: {item.shared_context_score}"
        )


# ---------------------------------------------------------------------------
# Test 7: SharedContextView serializes to dict via model_dump
# ---------------------------------------------------------------------------

def test_shared_context_view_model_dump():
    event = _make_event(
        event_id="evt_serial",
        importance=0.8,
        urgency=0.6,
        participant_ids=["U1", "U2"],
        unique_participant_count=2,
    )
    view = build_shared_context([event], {})
    d = view.model_dump()
    assert isinstance(d, dict)
    assert "globally_critical" in d
    assert "cross_functional_hotspots" in d
    assert "misalignments" in d
    assert "generated_for_user_id" in d


# ---------------------------------------------------------------------------
# Test 8: No misalignment when participants align with event topic
# ---------------------------------------------------------------------------

def test_no_misalignment_when_participants_aligned():
    event = _make_event(
        event_id="evt_aligned",
        importance=0.75,
        cross_functional=0.6,
        dominant_event_type="blocker",
        topic_labels=["firmware"],
        participant_ids=["U1", "U2", "U3"],
        unique_participant_count=3,
    )
    profiles = {
        "U1": _make_profile("U1", primary_topic="firmware"),
        "U2": _make_profile("U2", primary_topic="firmware"),
        "U3": _make_profile("U3", primary_topic="firmware"),
    }
    misalignments = detect_misalignments([event], profiles)
    # Only ownership ambiguity may fire here (pattern 1), but no topic camp divergence
    # Confirm: no differing_event_type_views (all have blocker as primary)
    for m in misalignments:
        assert m.differing_event_type_views == {}


# ---------------------------------------------------------------------------
# Test 9: Cross-functional hotspot requires participant count >= 3
# ---------------------------------------------------------------------------

def test_cf_hotspot_requires_min_participants():
    # 2 participants only — should NOT appear in hotspots even with high cf_score
    event_too_few = _make_event(
        event_id="evt_few",
        cross_functional=0.75,
        importance=0.6,
        participant_ids=["U1", "U2"],
        unique_participant_count=2,
    )
    # 3 participants — should appear
    event_enough = _make_event(
        event_id="evt_enough",
        cross_functional=0.75,
        importance=0.6,
        participant_ids=["U1", "U2", "U3"],
        unique_participant_count=3,
    )
    view = build_shared_context([event_too_few, event_enough], {})
    hotspot_ids = [item.event_id for item in view.cross_functional_hotspots]
    assert "evt_enough" in hotspot_ids
    assert "evt_few" not in hotspot_ids


# ---------------------------------------------------------------------------
# Test 10: detect_misalignments returns empty list for empty events
# ---------------------------------------------------------------------------

def test_detect_misalignments_empty_events():
    result = detect_misalignments([], {})
    assert result == []


# ---------------------------------------------------------------------------
# Test 11: Event with None signals does not crash either function
# ---------------------------------------------------------------------------

def test_none_signals_handled_gracefully():
    event = CandidateEvent(
        event_id="evt_no_signals",
        thread_id="thread_x",
        channel_id="C_general",
        participant_ids=["U1", "U2"],
        message_ids=["M1"],
        started_at=_NOW,
        last_activity_at=_NOW,
        text_bundle="some text",
        message_count=2,
        reply_count=1,
        unique_participant_count=2,
        total_reactions=0,
        signals=None,  # No signals
    )
    view = build_shared_context([event], {})
    assert isinstance(view, SharedContextView)
    misalignments = detect_misalignments([event], {})
    assert isinstance(misalignments, list)


# ---------------------------------------------------------------------------
# Test 12: generated_for_user_id propagated correctly
# ---------------------------------------------------------------------------

def test_generated_for_user_id_propagated():
    view = build_shared_context([], {}, generated_for_user_id="U_alice")
    assert view.generated_for_user_id == "U_alice"

    view_org = build_shared_context([], {})
    assert view_org.generated_for_user_id is None
