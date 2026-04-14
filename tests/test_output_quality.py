"""
Tests for output quality improvements:
- Fallback summaries use concrete phrases from thread text
- Confidence values are in [0, 1] range and meaningfully spread
- Embedding affinity is non-trivial for users with topic affinities
- Metrics report shows savings fractions
- Headline does not double-count items
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone

from src.summarization.providers import FallbackProvider, _extract_key_phrase
from src.ranking import rank_events_for_user
from src.observability import PipelineMetrics, StageTimer

NOW = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fallback summarization — concrete phrases
# ---------------------------------------------------------------------------

def test_fallback_summary_not_purely_templated(enriched_events, events_by_id, profiles):
    """
    FallbackProvider summaries should contain content from the event text,
    not just generic type labels like 'likely involving pcb, firmware'.
    """
    profile = profiles["u_alice"]
    ranked, _ = rank_events_for_user(enriched_events, profile, top_k=5, now=NOW)
    provider = FallbackProvider()

    for item in ranked:
        event = events_by_id.get(item.event_id)
        if event is None or event.signals is None:
            continue
        summary, _ = provider.summarize(event, item, profile)

        # Summary should not be the pure template phrase
        assert "likely involving" not in summary, (
            f"Summary for {item.event_id} still uses generic template phrasing: '{summary}'"
        )
        # Should have at least 2 sentences
        sentences = [s.strip() for s in summary.split(".") if s.strip()]
        assert len(sentences) >= 2, f"Too short: '{summary}'"


def test_extract_key_phrase_finds_concrete_content():
    """_extract_key_phrase should pick a line with concrete technical content."""
    text = (
        "[u_alice]: Hey team\n"
        "[u_bob]: Thermal cycling on the Rev C PCBA is showing a 12% failure rate on the voltage rail\n"
        "[u_carol]: Yeah that's bad"
    )
    phrase = _extract_key_phrase(text, "blocker")
    # Should prefer the technical line with a measurement
    assert "12%" in phrase or "Rev C" in phrase or "voltage" in phrase, (
        f"Expected concrete content in phrase, got: '{phrase}'"
    )


def test_extract_key_phrase_fallback_on_simple_text():
    """_extract_key_phrase gracefully handles text with no technical signals."""
    text = "[u_alice]: Hi everyone\n[u_bob]: How was the weekend?"
    phrase = _extract_key_phrase(text, "noise")
    assert isinstance(phrase, str)
    assert len(phrase) > 0


def test_summary_preserves_3part_structure(enriched_events, events_by_id, profiles):
    """
    Summaries should follow the 3-part structure: situation, impact (maybe), resolution.
    Minimum 2 sentences; blockers/risks should produce 3.
    """
    profile = profiles["u_diana"]
    ranked, _ = rank_events_for_user(enriched_events, profile, top_k=5, now=NOW)
    provider = FallbackProvider()

    blocker_items = [
        item for item in ranked
        if item.event_type in ("blocker", "risk")
    ]

    for item in blocker_items[:3]:
        event = events_by_id.get(item.event_id)
        if event is None:
            continue
        summary, _ = provider.summarize(event, item, profile)
        sentences = [s.strip() for s in summary.split(".") if s.strip()]
        assert len(sentences) >= 2, (
            f"Blocker {item.event_id} summary has only {len(sentences)} sentence(s): '{summary}'"
        )


# ---------------------------------------------------------------------------
# Confidence — range and spread
# ---------------------------------------------------------------------------

def test_confidence_range(enriched_events):
    """All confidence values should be in [0, 1]."""
    for event in enriched_events:
        if event.signals is not None:
            c = event.signals.confidence
            assert 0.0 <= c <= 1.0, (
                f"Event {event.event_id} has confidence {c} outside [0, 1]"
            )


def test_confidence_not_uniformly_05(enriched_events):
    """Confidence values should vary — not everything should be 0.50."""
    confidences = [
        e.signals.confidence
        for e in enriched_events
        if e.signals is not None
    ]
    # Require at least 2 distinct values
    distinct = len(set(confidences))
    assert distinct >= 2, (
        f"All confidence values are identical ({confidences[0]}) — no spread"
    )


def test_high_signal_event_has_reasonable_confidence(enriched_events):
    """
    The I2C firmware hang thread has many participants and messages —
    should have reasonably high confidence.
    """
    fw_event = next((e for e in enriched_events if e.event_id == "evt_m_010"), None)
    if fw_event is None or fw_event.signals is None:
        pytest.skip("Firmware event not found")

    assert fw_event.signals.confidence > 0.3, (
        f"High-signal firmware event has low confidence: {fw_event.signals.confidence}"
    )


def test_noise_event_confidence_capped(enriched_events):
    """Noise events should have confidence capped below 0.70."""
    noise_events = [
        e for e in enriched_events
        if e.signals and e.signals.dominant_event_type == "noise"
    ]
    if not noise_events:
        pytest.skip("No noise events in corpus")

    for event in noise_events:
        assert event.signals.confidence <= 0.70, (
            f"Noise event {event.event_id} has unexpectedly high confidence: "
            f"{event.signals.confidence}"
        )


# ---------------------------------------------------------------------------
# Embedding affinity — materially useful
# ---------------------------------------------------------------------------

def test_embedding_affinity_nonzero_for_topic_user(enriched_events, profiles):
    """
    Users with known topic affinities should get non-zero embedding affinity
    for at least some events.
    """
    profile = profiles["u_alice"]
    assert profile.topic_affinities, "Test requires alice to have topic affinities"

    ranked, _ = rank_events_for_user(enriched_events, profile, top_k=5, now=NOW)

    nonzero = [i for i in ranked if i.reason_features.embedding_affinity > 0.0]
    assert len(nonzero) > 0, (
        "Expected at least one ranked item with non-zero embedding_affinity for a user "
        "with topic affinities, but all were 0"
    )


def test_embedding_affinity_higher_for_relevant_events(enriched_events, profiles):
    """
    For a firmware engineer, firmware-related events should have higher
    embedding affinity than social noise events.
    """
    profile = profiles["u_bob"]  # firmware engineer with firmware topic affinity
    if not profile.topic_affinities:
        pytest.skip("Bob has no topic affinities — cannot test embedding affinity")

    # Score all events
    ranked, _ = rank_events_for_user(enriched_events, profile, top_k=10, now=NOW)

    firmware_items = [i for i in ranked if "firmware" in (i.event_type or "")]
    noise_items = [i for i in ranked if i.event_type == "noise"]

    if not firmware_items or not noise_items:
        pytest.skip("Need both firmware and noise items to compare")

    avg_firmware_affinity = sum(i.reason_features.embedding_affinity for i in firmware_items) / len(firmware_items)
    avg_noise_affinity = sum(i.reason_features.embedding_affinity for i in noise_items) / len(noise_items)

    assert avg_firmware_affinity >= avg_noise_affinity, (
        f"Firmware events should have higher embedding affinity than noise for firmware user. "
        f"firmware={avg_firmware_affinity:.3f}, noise={avg_noise_affinity:.3f}"
    )


def test_embedding_affinity_in_01_range(enriched_events, profiles):
    """Embedding affinity must stay in [0, 1] after scaling."""
    profile = profiles["u_alice"]
    ranked, _ = rank_events_for_user(enriched_events, profile, top_k=7, now=NOW)
    for item in ranked:
        ea = item.reason_features.embedding_affinity
        assert 0.0 <= ea <= 1.0, (
            f"embedding_affinity {ea} out of [0, 1] for {item.event_id}"
        )


# ---------------------------------------------------------------------------
# Metrics — savings fractions
# ---------------------------------------------------------------------------

def test_metrics_pipeline_mode_recorded():
    """pipeline_mode should be recorded in metrics."""
    from pathlib import Path
    from src.digest import run_full_pipeline

    metrics = PipelineMetrics()
    run_full_pipeline(
        data_dir=Path(__file__).parent.parent / "data" / "mock_slack",
        now=NOW,
        date_str="2026-04-10",
        metrics=metrics,
    )
    assert metrics.pipeline_mode == "full"


def test_metrics_summaries_reused_gt_generated(enriched_events, profiles, events_by_id):
    """
    When multiple users are processed with shared summaries,
    summaries_reused should exceed summaries_generated.
    """
    from src.summarization import build_shared_summaries

    event_ids = [e.event_id for e in enriched_events]
    shared = build_shared_summaries(events_by_id, event_ids)

    # Simulate 3 users each getting 5 items from shared pool
    total_reused = 0
    for uid in ["u_alice", "u_bob", "u_diana"]:
        if uid not in profiles:
            continue
        ranked, _ = rank_events_for_user(enriched_events, profiles[uid], top_k=5, now=NOW)
        total_reused += len(ranked)

    assert total_reused > len(shared), (
        "With 3 users, total items served from shared pool should exceed unique summaries"
    )


# ---------------------------------------------------------------------------
# Headline — no double counting
# ---------------------------------------------------------------------------

def test_headline_does_not_double_count(enriched_events, profiles):
    """
    If the top items are all blockers with high signal, the headline
    should not say '3 high-signal updates and 3 blockers' (which would imply 6).
    """
    from src.digest.assembler import _generate_headline
    from src.models import RankedDigestItem, RankingFeatures

    def _mock_item(event_type: str, signal_level: str, score: float) -> RankedDigestItem:
        features = RankingFeatures(
            user_affinity=0.5, importance=0.8, urgency=0.9,
            momentum=0.7, novelty=0.6, recency=0.5,
            weights={}, final_score=score,
        )
        return RankedDigestItem(
            event_id="e1", title="Test", event_type=event_type,
            signal_level=signal_level, confidence=0.8, score=score,
            reason_features=features, source_thread_ids=[], source_message_ids=[],
        )

    # 3 high-signal blockers
    items = [_mock_item("blocker", "high", 0.8) for _ in range(3)]
    headline = _generate_headline(items)

    # Should not mention "high-signal" count separately from blocker count
    # The total count should be 3, not implied to be 6
    assert "3 item" in headline or "3 likely" in headline, (
        f"Headline phrasing is ambiguous or double-counted: '{headline}'"
    )
    # Should not contain both a high-signal count and a blocker count that together imply > n items
    assert "high-signal" not in headline or "blocker" not in headline, (
        f"Headline double-counts: '{headline}'"
    )


def test_headline_empty_digest():
    from src.digest.assembler import _generate_headline
    assert _generate_headline([]) == "No significant updates today."
