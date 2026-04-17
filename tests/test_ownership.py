"""
Tests for ownership and accountability inference.

Tests focus on behaviour, not just execution:
- dominant replier becomes likely_owner
- root author who follows up becomes likely_owner
- single-participant threads raise accountability_gap_flag
- blocker events with low confidence raise accountability_gap_flag
- ownership_evidence is non-empty when an owner is found
- key_contributor_ids excludes the likely_owner
- likely_owner_confidence is in [0, 1]
- function / team inference from topic_labels
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone

from src.models import (
    CandidateEvent,
    SemanticSignals,
    EventTypeDistribution,
    SlackMessage,
    SlackThread,
    SlackUser,
    SlackWorkspace,
)
from src.enrichment import infer_ownership, OwnershipSignals

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_TS = datetime(2026, 4, 10, 9, 0, 0, tzinfo=timezone.utc)


def _ts(offset_minutes: int = 0) -> datetime:
    from datetime import timedelta
    return _BASE_TS + timedelta(minutes=offset_minutes)


def _make_workspace(messages: list[SlackMessage], users: list[SlackUser] | None = None) -> SlackWorkspace:
    return SlackWorkspace(
        users=users or [],
        channels=[],
        messages=messages,
        threads=[],
    )


def _make_event(
    thread_id: str = "thread_001",
    participant_ids: list[str] | None = None,
    message_ids: list[str] | None = None,
    reply_count: int = 0,
    unique_participant_count: int = 1,
    signals: SemanticSignals | None = None,
    text_bundle: str = "some discussion",
) -> CandidateEvent:
    return CandidateEvent(
        event_id=f"evt_{thread_id}",
        thread_id=thread_id,
        channel_id="channel_001",
        participant_ids=participant_ids or ["u_alice"],
        message_ids=message_ids or ["msg_001"],
        started_at=_ts(0),
        last_activity_at=_ts(30),
        text_bundle=text_bundle,
        message_count=max(1, reply_count + 1),
        reply_count=reply_count,
        unique_participant_count=unique_participant_count,
        total_reactions=0,
        signals=signals,
    )


def _make_root_message(thread_id: str, user_id: str, msg_id: str = "msg_001") -> SlackMessage:
    return SlackMessage(
        message_id=msg_id,
        thread_id=thread_id,
        channel_id="channel_001",
        user_id=user_id,
        text="I noticed an issue with the I2C bus, investigating now.",
        timestamp=_ts(0),
        is_thread_root=True,
    )


def _make_reply(
    thread_id: str,
    user_id: str,
    msg_id: str,
    text: str = "Looking into it.",
    mentions: list[str] | None = None,
    offset: int = 5,
) -> SlackMessage:
    return SlackMessage(
        message_id=msg_id,
        thread_id=thread_id,
        channel_id="channel_001",
        user_id=user_id,
        text=text,
        timestamp=_ts(offset),
        is_thread_root=False,
        mentions=mentions or [],
    )


def _blocker_signals() -> SemanticSignals:
    return SemanticSignals(
        title="[BLOCKER] firmware hang",
        topic_labels=["firmware"],
        event_type_dist=EventTypeDistribution(blocker=0.9),
        dominant_event_type="blocker",
        urgency_score=0.8,
        momentum_score=0.5,
        novelty_score=0.5,
        unresolved_score=0.8,
        importance_score=0.9,
        confidence=0.85,
    )


def _firmware_signals() -> SemanticSignals:
    return SemanticSignals(
        title="Firmware discussion",
        topic_labels=["firmware"],
        event_type_dist=EventTypeDistribution(status_update=0.6),
        dominant_event_type="status_update",
        urgency_score=0.3,
        momentum_score=0.4,
        novelty_score=0.5,
        unresolved_score=0.3,
        importance_score=0.4,
        confidence=0.7,
    )


def _supply_chain_signals() -> SemanticSignals:
    return SemanticSignals(
        title="Supply chain delay",
        topic_labels=["supply_chain"],
        event_type_dist=EventTypeDistribution(risk=0.7),
        dominant_event_type="risk",
        urgency_score=0.6,
        momentum_score=0.4,
        novelty_score=0.6,
        unresolved_score=0.6,
        importance_score=0.7,
        confidence=0.75,
    )


# ---------------------------------------------------------------------------
# Test 1: Dominant replier becomes likely_owner
# ---------------------------------------------------------------------------

def test_dominant_replier_becomes_likely_owner():
    """
    When one user sends significantly more replies than others,
    they should be identified as the likely_owner.
    """
    thread_id = "thread_t1"
    participants = ["u_alice", "u_bob", "u_carol"]

    messages = [
        _make_root_message(thread_id, "u_alice", "msg_r1"),
        # u_bob sends 4 replies — dominant replier
        _make_reply(thread_id, "u_bob", "msg_b1", offset=5),
        _make_reply(thread_id, "u_bob", "msg_b2", offset=10),
        _make_reply(thread_id, "u_bob", "msg_b3", offset=15),
        _make_reply(thread_id, "u_bob", "msg_b4", offset=20),
        # u_carol sends 1 reply
        _make_reply(thread_id, "u_carol", "msg_c1", offset=25),
    ]

    workspace = _make_workspace(messages)
    event = _make_event(
        thread_id=thread_id,
        participant_ids=participants,
        reply_count=5,
        unique_participant_count=3,
    )

    result = infer_ownership(event, workspace)

    assert result.likely_owner_user_id == "u_bob", (
        f"Expected u_bob as dominant replier, got {result.likely_owner_user_id}"
    )
    assert result.likely_owner_confidence > 0.3


# ---------------------------------------------------------------------------
# Test 2: Root author who follows up becomes likely_owner
# ---------------------------------------------------------------------------

def test_root_author_with_followup_becomes_owner():
    """
    If the root author also has significant follow-up replies,
    they should be considered the likely_owner.
    """
    thread_id = "thread_t2"
    participants = ["u_alice", "u_bob"]

    messages = [
        _make_root_message(thread_id, "u_alice", "msg_r1"),
        # Alice (root) also sends the most replies
        _make_reply(thread_id, "u_alice", "msg_a1", text="I'll fix this by EOD.", offset=5),
        _make_reply(thread_id, "u_alice", "msg_a2", text="I updated the firmware init sequence.", offset=10),
        _make_reply(thread_id, "u_alice", "msg_a3", text="I confirmed it's working now.", offset=15),
        # Bob sends one reply
        _make_reply(thread_id, "u_bob", "msg_b1", offset=20),
    ]

    workspace = _make_workspace(messages)
    event = _make_event(
        thread_id=thread_id,
        participant_ids=participants,
        reply_count=4,
        unique_participant_count=2,
    )

    result = infer_ownership(event, workspace)

    assert result.likely_owner_user_id == "u_alice", (
        f"Expected u_alice (root + dominant replier), got {result.likely_owner_user_id}"
    )
    assert result.likely_owner_confidence >= 0.3


# ---------------------------------------------------------------------------
# Test 3: Single-participant thread → accountability_gap_flag
# ---------------------------------------------------------------------------

def test_single_participant_no_replies_flags_gap():
    """
    A thread with only one participant and no replies suggests the message
    was dropped or ignored — should flag accountability gap.
    """
    thread_id = "thread_t3"

    messages = [
        _make_root_message(thread_id, "u_alice", "msg_r1"),
    ]

    workspace = _make_workspace(messages)
    event = _make_event(
        thread_id=thread_id,
        participant_ids=["u_alice"],
        reply_count=0,
        unique_participant_count=1,
    )

    result = infer_ownership(event, workspace)

    assert result.accountability_gap_flag is True, (
        "Single-participant, no-reply thread should flag accountability gap"
    )
    assert len(result.accountability_gap_reason) > 0


# ---------------------------------------------------------------------------
# Test 4: Blocker event with low owner confidence → accountability_gap_flag
# ---------------------------------------------------------------------------

def test_blocker_with_low_confidence_flags_gap():
    """
    A blocker event with spread ownership (no dominant owner > 0.5)
    should raise the accountability_gap_flag.
    """
    thread_id = "thread_t4"
    # 3 equally active participants — no clear owner
    participants = ["u_alice", "u_bob", "u_carol"]

    messages = [
        _make_root_message(thread_id, "u_alice", "msg_r1"),
        _make_reply(thread_id, "u_bob", "msg_b1", offset=5),
        _make_reply(thread_id, "u_carol", "msg_c1", offset=10),
        _make_reply(thread_id, "u_alice", "msg_a1", offset=15),
        _make_reply(thread_id, "u_bob", "msg_b2", offset=20),
        _make_reply(thread_id, "u_carol", "msg_c2", offset=25),
    ]

    workspace = _make_workspace(messages)
    event = _make_event(
        thread_id=thread_id,
        participant_ids=participants,
        reply_count=5,
        unique_participant_count=3,
        signals=_blocker_signals(),
    )

    result = infer_ownership(event, workspace)

    # With equal distribution, confidence for any single user < 0.5 on a blocker → gap
    if result.likely_owner_confidence < 0.5:
        assert result.accountability_gap_flag is True, (
            "Blocker event with no confident owner should flag accountability gap. "
            f"Confidence was {result.likely_owner_confidence}"
        )


# ---------------------------------------------------------------------------
# Test 5: ownership_evidence is non-empty when owner is found
# ---------------------------------------------------------------------------

def test_ownership_evidence_non_empty_when_owner_found():
    """
    When a likely_owner is confidently identified, ownership_evidence
    should contain at least one human-readable string.
    """
    thread_id = "thread_t5"
    participants = ["u_alice", "u_bob"]

    messages = [
        _make_root_message(thread_id, "u_alice", "msg_r1"),
        _make_reply(thread_id, "u_alice", "msg_a1", text="I'll fix this.", offset=5),
        _make_reply(thread_id, "u_alice", "msg_a2", text="I updated the driver.", offset=10),
        _make_reply(thread_id, "u_alice", "msg_a3", text="I confirmed the fix works.", offset=15),
        _make_reply(thread_id, "u_bob", "msg_b1", offset=20),
    ]

    workspace = _make_workspace(messages)
    event = _make_event(
        thread_id=thread_id,
        participant_ids=participants,
        reply_count=4,
        unique_participant_count=2,
    )

    result = infer_ownership(event, workspace)

    assert result.likely_owner_user_id is not None, "Expected an owner to be identified"
    assert len(result.ownership_evidence) > 0, (
        "ownership_evidence should be non-empty when an owner is found"
    )
    # Evidence strings should be meaningful
    for item in result.ownership_evidence:
        assert isinstance(item, str)
        assert len(item) > 5


# ---------------------------------------------------------------------------
# Test 6: key_contributor_ids excludes the likely_owner
# ---------------------------------------------------------------------------

def test_key_contributors_exclude_likely_owner():
    """
    key_contributor_ids must not include the likely_owner_user_id.
    """
    thread_id = "thread_t6"
    participants = ["u_alice", "u_bob", "u_carol"]

    messages = [
        _make_root_message(thread_id, "u_alice", "msg_r1"),
        # Alice dominates
        _make_reply(thread_id, "u_alice", "msg_a1", text="I will handle this.", offset=5),
        _make_reply(thread_id, "u_alice", "msg_a2", text="I sent the update.", offset=10),
        _make_reply(thread_id, "u_alice", "msg_a3", offset=15),
        # Bob and Carol both contribute enough to be key_contributors
        _make_reply(thread_id, "u_bob", "msg_b1", offset=20),
        _make_reply(thread_id, "u_bob", "msg_b2", offset=25),
        _make_reply(thread_id, "u_carol", "msg_c1", offset=30),
        _make_reply(thread_id, "u_carol", "msg_c2", offset=35),
    ]

    workspace = _make_workspace(messages)
    event = _make_event(
        thread_id=thread_id,
        participant_ids=participants,
        reply_count=7,
        unique_participant_count=3,
    )

    result = infer_ownership(event, workspace)

    if result.likely_owner_user_id is not None:
        assert result.likely_owner_user_id not in result.key_contributor_ids, (
            f"likely_owner ({result.likely_owner_user_id}) should not appear in key_contributor_ids. "
            f"Got: {result.key_contributor_ids}"
        )


# ---------------------------------------------------------------------------
# Test 7: likely_owner_confidence is always in [0, 1]
# ---------------------------------------------------------------------------

def test_owner_confidence_in_valid_range():
    """
    likely_owner_confidence must always be a float in [0, 1].
    Covers several scenarios including empty workspace.
    """
    scenarios = [
        # Scenario A: Normal multi-participant thread
        (
            _make_event(
                thread_id="thread_s1",
                participant_ids=["u_alice", "u_bob"],
                reply_count=3,
                unique_participant_count=2,
            ),
            _make_workspace([
                _make_root_message("thread_s1", "u_alice", "msg_r1"),
                _make_reply("thread_s1", "u_bob", "msg_b1", offset=5),
                _make_reply("thread_s1", "u_bob", "msg_b2", offset=10),
                _make_reply("thread_s1", "u_bob", "msg_b3", offset=15),
            ]),
        ),
        # Scenario B: Single participant, no replies
        (
            _make_event(
                thread_id="thread_s2",
                participant_ids=["u_alice"],
                reply_count=0,
                unique_participant_count=1,
            ),
            _make_workspace([
                _make_root_message("thread_s2", "u_alice", "msg_r1"),
            ]),
        ),
        # Scenario C: No messages at all in workspace for this thread
        (
            _make_event(
                thread_id="thread_s3",
                participant_ids=["u_alice", "u_bob"],
                reply_count=2,
                unique_participant_count=2,
            ),
            _make_workspace([]),  # Empty workspace
        ),
    ]

    for event, workspace in scenarios:
        result = infer_ownership(event, workspace)
        assert 0.0 <= result.likely_owner_confidence <= 1.0, (
            f"Confidence {result.likely_owner_confidence} out of [0,1] range "
            f"for scenario with thread_id={event.thread_id}"
        )


# ---------------------------------------------------------------------------
# Test 8: Function inference from topic_labels
# ---------------------------------------------------------------------------

def test_function_inference_from_topic_labels():
    """
    When signals carry topic_labels, the likely_function_or_team field
    should be populated with the correct team mapping.
    """
    test_cases = [
        ("firmware", "Firmware"),
        ("supply_chain", "Supply Chain"),
        ("thermal", "Hardware"),
        ("testing", "Test/QA"),
        ("pcb", "Hardware"),
    ]

    for topic_label, expected_function in test_cases:
        signals = SemanticSignals(
            title="test event",
            topic_labels=[topic_label],
            event_type_dist=EventTypeDistribution(status_update=0.5),
            dominant_event_type="status_update",
            urgency_score=0.3,
            momentum_score=0.3,
            novelty_score=0.3,
            unresolved_score=0.3,
            importance_score=0.3,
            confidence=0.5,
        )

        event = _make_event(
            thread_id=f"thread_{topic_label}",
            participant_ids=["u_alice", "u_bob"],
            reply_count=2,
            unique_participant_count=2,
            signals=signals,
        )
        messages = [
            _make_root_message(f"thread_{topic_label}", "u_alice", "msg_r1"),
            _make_reply(f"thread_{topic_label}", "u_bob", "msg_b1", offset=5),
            _make_reply(f"thread_{topic_label}", "u_alice", "msg_a1", offset=10),
        ]
        workspace = _make_workspace(messages)

        result = infer_ownership(event, workspace)

        assert result.likely_function_or_team == expected_function, (
            f"topic_label='{topic_label}' should map to '{expected_function}', "
            f"got '{result.likely_function_or_team}'"
        )


# ---------------------------------------------------------------------------
# Test 9: Mention patterns boost the mentioned user
# ---------------------------------------------------------------------------

def test_mention_patterns_boost_mentioned_user():
    """
    A user who is @mentioned across multiple messages in a thread
    should receive a higher score — suggesting domain expertise or accountability.
    """
    thread_id = "thread_t9"
    participants = ["u_alice", "u_bob", "u_carol"]

    messages = [
        _make_root_message(thread_id, "u_alice", "msg_r1"),
        _make_reply(
            thread_id, "u_alice", "msg_a1",
            text="@u_carol can you review this?",
            mentions=["u_carol"],
            offset=5,
        ),
        _make_reply(
            thread_id, "u_bob", "msg_b1",
            text="@u_carol you're the expert here, right?",
            mentions=["u_carol"],
            offset=10,
        ),
        _make_reply(thread_id, "u_carol", "msg_c1", text="Yes, I'll take a look.", offset=15),
    ]

    workspace = _make_workspace(messages)
    event = _make_event(
        thread_id=thread_id,
        participant_ids=participants,
        reply_count=3,
        unique_participant_count=3,
    )

    result = infer_ownership(event, workspace)

    # u_carol is mentioned twice — should appear as owner or key contributor
    involved = (
        [result.likely_owner_user_id] + result.key_contributor_ids
        if result.likely_owner_user_id
        else result.key_contributor_ids
    )
    assert "u_carol" in involved, (
        "u_carol (most-mentioned user) should appear as owner or key contributor. "
        f"Owner: {result.likely_owner_user_id}, Contributors: {result.key_contributor_ids}"
    )


# ---------------------------------------------------------------------------
# Test 10: Action-taking language is recognised
# ---------------------------------------------------------------------------

def test_action_language_detected():
    """
    A user who uses action-taking phrases should receive an evidence entry
    about action-taking language.
    """
    thread_id = "thread_t10"
    participants = ["u_alice", "u_bob"]

    messages = [
        _make_root_message(thread_id, "u_alice", "msg_r1"),
        _make_reply(
            thread_id, "u_bob", "msg_b1",
            text="I'll fix the timing issue today.",
            offset=5,
        ),
        _make_reply(
            thread_id, "u_bob", "msg_b2",
            text="I updated the register map.",
            offset=10,
        ),
    ]

    workspace = _make_workspace(messages)
    event = _make_event(
        thread_id=thread_id,
        participant_ids=participants,
        reply_count=2,
        unique_participant_count=2,
    )

    result = infer_ownership(event, workspace)

    # At least one evidence string should mention action-taking language
    action_evidence = [
        e for e in result.ownership_evidence
        if "action" in e.lower() or "ownership" in e.lower()
    ]
    assert len(action_evidence) > 0, (
        "Expected ownership_evidence to include action-taking language detection. "
        f"Got: {result.ownership_evidence}"
    )
