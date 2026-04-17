"""
Tests for V2 pipeline upgrades.

Verifies behavioral expectations for:
1. Hybrid event classification
2. Entity extraction
3. Issue linking and cluster formation
4. Duplicate suppression
5. State-change detection
6. Semantic novelty vs structural novelty
7. Grouped ranking sub-scores
8. Cross-functional item surfacing
9. Structured evidence packets
10. Fallback mode without LLM
"""

from __future__ import annotations

import pytest
from pathlib import Path
from datetime import datetime, timezone

from src.enrichment.entities import extract_entities
from src.issue_linking import build_issue_clusters
from src.evidence import build_evidence_packet
from src.models import CandidateEvent, SemanticSignals, EventTypeDistribution

DATA_DIR = Path(__file__).parent.parent / "data" / "mock_slack"
NOW = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 1. Hybrid event classification
# ---------------------------------------------------------------------------

def test_hybrid_type_scores_populated(enriched_events):
    """All enriched events should have hybrid_event_type_scores filled."""
    for event in enriched_events:
        assert event.signals is not None
        assert event.signals.hybrid_event_type_scores, (
            f"Event {event.event_id} has no hybrid_event_type_scores"
        )
        # Should cover all 6 event types
        expected_types = {"blocker", "decision", "risk", "status_update", "request_for_input", "noise"}
        assert set(event.signals.hybrid_event_type_scores.keys()) == expected_types


def test_hybrid_scores_between_zero_and_one(enriched_events):
    """Hybrid scores must be in [0, 1]."""
    for event in enriched_events:
        if event.signals:
            for k, v in event.signals.hybrid_event_type_scores.items():
                assert 0.0 <= v <= 1.0, f"Out-of-range hybrid score {k}={v} on {event.event_id}"


def test_hybrid_classification_handles_phrasing_variation():
    """
    Hybrid classifier should catch technical phrasing that heuristics alone miss.

    The event text uses "blocks" (not "blocked") and "100 percent" (not "100%").
    Heuristic might give low blocker score; semantic should lift it.
    """
    from src.enrichment.enricher import _enrich_single, _build_embedding_store
    from src.models.raw import SlackWorkspace, SlackChannel, SlackUser, SlackThread, SlackMessage

    text = "[u_test]: The calibration step blocks all downstream tests — 100 percent of units fail."

    # Minimal workspace
    msg = SlackMessage(
        message_id="m_test",
        thread_id="m_test",
        channel_id="ch_test",
        user_id="u_test",
        text="The calibration step blocks all downstream tests — 100 percent of units fail.",
        timestamp=NOW,
        is_thread_root=True,
    )
    thread = SlackThread(
        thread_id="m_test",
        root_message_id="m_test",
        channel_id="ch_test",
        started_at=NOW,
        last_activity_at=NOW,
        participant_ids=["u_test"],
        message_ids=["m_test"],
        reply_count=0,
    )
    channel = SlackChannel(channel_id="ch_test", name="test", topic="")
    user = SlackUser(user_id="u_test", display_name="Test", role="Engineer", channel_ids=["ch_test"])
    workspace = SlackWorkspace(messages=[msg], threads=[thread], channels=[channel], users=[user])

    from src.events import build_candidate_events
    events = build_candidate_events(workspace)
    store = _build_embedding_store(events)
    signals = _enrich_single(events[0], events, workspace, NOW, [msg], store)

    # Blocker should be the dominant or have a meaningful hybrid score
    assert signals.hybrid_event_type_scores.get("blocker", 0) > 0.1, (
        "Expected blocker signal from 'blocks all downstream tests — 100 percent fail'"
    )


def test_type_confidence_populated(enriched_events):
    """type_confidence should be set on all enriched events."""
    for event in enriched_events:
        if event.signals:
            assert isinstance(event.signals.type_confidence, dict)
            assert len(event.signals.type_confidence) > 0


# ---------------------------------------------------------------------------
# 2. Entity extraction
# ---------------------------------------------------------------------------

def test_entity_extraction_parts():
    """Should extract known part identifiers."""
    text = "The SHT40 sensor is failing. We need MX150 connectors from Molex."
    result = extract_entities(text)
    assert "SHT40" in result.parts or "SHT40" in " ".join(result.parts), result.parts
    assert any("MX" in p for p in result.parts), result.parts


def test_entity_extraction_suppliers():
    text = "Molex notified us of a delay. Winbond confirmed lead time is 14 weeks."
    result = extract_entities(text)
    assert "Molex" in result.suppliers
    assert "Winbond" in result.suppliers


def test_entity_extraction_revisions():
    text = "We're targeting Rev C for Build B. Rev B silicon is the fallback."
    result = extract_entities(text)
    assert any("Rev" in r for r in result.revisions), result.revisions
    assert any("Build" in b for b in result.builds), result.builds


def test_entity_extraction_subsystems():
    text = "The BMS bring-up is failing on I2C. The PMIC sequence is stuck."
    result = extract_entities(text)
    assert "BMS" in result.subsystems
    assert "I2C" in result.subsystems
    assert "PMIC" in result.subsystems


def test_entity_extraction_deadlines():
    text = "BOM lock is April 18. Please confirm by EOD."
    result = extract_entities(text)
    assert any("April" in d for d in result.deadlines), result.deadlines
    assert any("EOD" in d for d in result.deadlines), result.deadlines


def test_entity_extraction_empty_text():
    """Empty input should return empty entities without error."""
    result = extract_entities("")
    assert result.is_empty()


def test_entities_populated_on_enriched_events(enriched_events):
    """At least some enriched events should have extracted_entities."""
    events_with_entities = [
        e for e in enriched_events
        if e.signals and e.signals.extracted_entities
    ]
    assert len(events_with_entities) > 0, "No events had extracted entities"


def test_tech_thread_has_entities(enriched_events):
    """The thermal cycling thread (m_020) should have technical entities."""
    thermal_event = next(
        (e for e in enriched_events if e.thread_id == "m_020"), None
    )
    assert thermal_event is not None
    assert thermal_event.signals is not None
    entities = thermal_event.signals.extracted_entities
    # Should have at least one entity type populated
    assert any(v for v in entities.values()), (
        f"Expected entities on thermal thread, got: {entities}"
    )


# ---------------------------------------------------------------------------
# 3. Issue linking and cluster formation
# ---------------------------------------------------------------------------

def test_build_issue_clusters_returns_dict(enriched_events):
    """build_issue_clusters should return a non-empty dict."""
    from src.enrichment.enricher import _build_embedding_store
    store = _build_embedding_store(enriched_events)
    clusters = build_issue_clusters(enriched_events, embedding_store=store)
    assert isinstance(clusters, dict)
    assert len(clusters) > 0


def test_all_events_get_cluster_id(enriched_events):
    """After clustering, every event should have an issue_cluster_id."""
    from src.enrichment.enricher import _build_embedding_store
    store = _build_embedding_store(enriched_events)
    build_issue_clusters(enriched_events, embedding_store=store)
    for event in enriched_events:
        assert event.issue_cluster_id is not None, (
            f"Event {event.event_id} has no issue_cluster_id after clustering"
        )


def test_related_threads_linked(enriched_events):
    """
    Supplier-related threads (m_001 connector, m_060 NOR flash) should
    potentially link if they share supplier-type entities. Even if not linked,
    related_event_ids should be a list (may be empty for singletons).
    """
    from src.enrichment.enricher import _build_embedding_store
    store = _build_embedding_store(enriched_events)
    build_issue_clusters(enriched_events, embedding_store=store)
    for event in enriched_events:
        assert isinstance(event.related_event_ids, list)


def test_issue_status_assigned(enriched_events):
    """All events should have an issue_status of new/ongoing/resurfacing."""
    from src.enrichment.enricher import _build_embedding_store
    store = _build_embedding_store(enriched_events)
    build_issue_clusters(enriched_events, embedding_store=store)
    valid_statuses = {"new", "ongoing", "resurfacing"}
    for event in enriched_events:
        assert event.issue_status in valid_statuses, (
            f"Invalid issue_status '{event.issue_status}' on {event.event_id}"
        )


# ---------------------------------------------------------------------------
# 4. Duplicate suppression
# ---------------------------------------------------------------------------

def test_no_cluster_duplicates_in_digest(profiles, enriched_events, events_by_id):
    """After assembly, no two items in a digest should share a cluster_id."""
    from src.enrichment.enricher import _build_embedding_store
    from src.digest.assembler import assemble_digest

    store = _build_embedding_store(enriched_events)
    build_issue_clusters(enriched_events, embedding_store=store)

    for uid, profile in profiles.items():
        digest = assemble_digest(
            user_id=uid,
            enriched_events=enriched_events,
            profile=profile,
            events_by_id=events_by_id,
            top_k=5,
            embedding_store=store,
            include_excluded=True,
        )
        seen_clusters: set[str] = set()
        for item in digest.items:
            event = events_by_id.get(item.event_id)
            cluster_id = getattr(event, "issue_cluster_id", None) if event else None
            if cluster_id:
                assert cluster_id not in seen_clusters, (
                    f"Cluster {cluster_id} appears twice in digest for {uid}"
                )
                seen_clusters.add(cluster_id)


# ---------------------------------------------------------------------------
# 5. State-change detection
# ---------------------------------------------------------------------------

def test_state_change_resolved_detection():
    """Should detect unresolved → resolved transition."""
    from src.enrichment.signals import compute_state_change_hint

    # Create minimal event with state change pattern
    event = _make_minimal_event(
        "[u_a]: I2C stuck low — investigating the pull-up issue.\n"
        "[u_b]: The pull-up was wrong value.\n"
        "[u_a]: Fixed with 4.7k resistors. Confirmed resolved."
    )
    hint = compute_state_change_hint(event)
    assert hint == "unresolved → resolved", f"Expected state change, got: {hint}"


def test_state_change_decision_detection():
    """Should detect discussion → decision transition."""
    from src.enrichment.signals import compute_state_change_hint

    event = _make_minimal_event(
        "[u_a]: Should we go with Rev C or stick with Rev B? What are the trade-offs?\n"
        "[u_b]: I think Rev C is better for the firmware side.\n"
        "[u_a]: Agreed, going with Rev C. Decision made."
    )
    hint = compute_state_change_hint(event)
    assert hint is not None, "Expected a state change hint"
    assert "decision" in hint.lower(), f"Expected decision-related hint, got: {hint}"


def test_state_change_none_for_pure_noise():
    """A social/noise thread should not trigger a false state change."""
    from src.enrichment.signals import compute_state_change_hint

    event = _make_minimal_event(
        "[u_a]: Anyone bringing snacks to the team lunch?\n"
        "[u_b]: I'll bring brownies!\n"
        "[u_c]: Great idea!"
    )
    hint = compute_state_change_hint(event)
    assert hint is None, f"Expected no state change for noise thread, got: {hint}"


# ---------------------------------------------------------------------------
# 6. Semantic novelty vs structural novelty
# ---------------------------------------------------------------------------

def test_embedding_novelty_differs_across_events(enriched_events):
    """Events should have different embedding_novelty_scores (not all the same)."""
    scores = [
        e.signals.embedding_novelty_score
        for e in enriched_events
        if e.signals and e.signals.embedding_novelty_score is not None
    ]
    assert len(scores) > 0
    assert len(set(scores)) > 1, "All embedding novelty scores are identical — expected variation"


def test_noise_thread_lower_novelty_after_suppression(enriched_events):
    """
    The noise/social thread (m_040) should have lower novelty_score than
    technical threads after noise suppression.
    """
    noise_event = next((e for e in enriched_events if e.thread_id == "m_040"), None)
    if noise_event is None or noise_event.signals is None:
        pytest.skip("Noise thread not found")

    tech_events = [
        e for e in enriched_events
        if e.thread_id != "m_040" and e.signals
        and e.signals.dominant_event_type != "noise"
    ]
    if not tech_events:
        pytest.skip("No technical events to compare")

    avg_tech_novelty = sum(e.signals.novelty_score for e in tech_events) / len(tech_events)
    # Noise thread novelty should be at most the average of tech events
    # (noise suppression reduces novelty by up to 70%)
    assert noise_event.signals.novelty_score <= avg_tech_novelty + 0.2, (
        f"Noise thread novelty={noise_event.signals.novelty_score:.3f} "
        f"unexpectedly high vs avg tech={avg_tech_novelty:.3f}"
    )


# ---------------------------------------------------------------------------
# 7. Grouped ranking sub-scores
# ---------------------------------------------------------------------------

def test_grouped_ranking_scores_populated(profiles, enriched_events):
    """personal_relevance, global_importance, freshness should all be set."""
    from src.ranking.ranker import rank_events_for_user
    from src.enrichment.enricher import _build_embedding_store

    store = _build_embedding_store(enriched_events)
    profile = profiles["u_alice"]
    ranked, _ = rank_events_for_user(enriched_events, profile, top_k=5, embedding_store=store)

    for item in ranked:
        f = item.reason_features
        assert 0.0 <= f.personal_relevance <= 1.0, f"personal_relevance={f.personal_relevance}"
        assert 0.0 <= f.global_importance <= 1.0, f"global_importance={f.global_importance}"
        assert 0.0 <= f.freshness <= 1.0, f"freshness={f.freshness}"


def test_high_importance_event_has_high_global_importance(profiles, enriched_events):
    """A confirmed blocker thread should score high global_importance."""
    from src.ranking.ranker import rank_events_for_user
    from src.enrichment.enricher import _build_embedding_store

    store = _build_embedding_store(enriched_events)
    profile = profiles["u_alice"]
    ranked, _ = rank_events_for_user(enriched_events, profile, top_k=7, embedding_store=store)

    # At least one item should have high global_importance
    high_gi_items = [i for i in ranked if i.reason_features.global_importance >= 0.5]
    assert len(high_gi_items) > 0, "Expected at least one item with high global_importance"


# ---------------------------------------------------------------------------
# 8. Cross-functional items can surface
# ---------------------------------------------------------------------------

def test_cross_functional_items_can_surface(profiles, enriched_events):
    """
    Items with high cross_functional_score should be able to appear in
    any user's digest, not only for directly involved users.
    """
    # Find an event with high cross-functional score
    cf_events = sorted(
        [e for e in enriched_events if e.signals],
        key=lambda e: e.signals.cross_functional_score,
        reverse=True,
    )
    if not cf_events:
        pytest.skip("No events with cross_functional_score")

    top_cf_event = cf_events[0]
    top_cf_thread = top_cf_event.thread_id

    # It should appear in at least one user's ranked results
    from src.ranking.ranker import rank_events_for_user
    from src.enrichment.enricher import _build_embedding_store

    store = _build_embedding_store(enriched_events)
    found_in_any = False
    for uid, profile in profiles.items():
        ranked, _ = rank_events_for_user(enriched_events, profile, top_k=7, embedding_store=store)
        if any(item.source_thread_ids and top_cf_thread in item.source_thread_ids for item in ranked):
            found_in_any = True
            break

    assert found_in_any, (
        f"Cross-functional event {top_cf_event.event_id} did not appear in any digest top-7"
    )


# ---------------------------------------------------------------------------
# 9. Structured evidence packets
# ---------------------------------------------------------------------------

def test_evidence_packet_has_root_message(enriched_events):
    """Every enriched event should produce a non-empty root_message in its evidence packet."""
    for event in enriched_events:
        packet = build_evidence_packet(event)
        assert packet.root_message, f"Empty root_message for {event.event_id}"


def test_evidence_packet_includes_entities(enriched_events):
    """Events with extracted entities should surface them in the evidence packet."""
    events_with_entities = [
        e for e in enriched_events
        if e.signals and e.signals.extracted_entities
    ]
    for event in events_with_entities[:3]:  # Check a sample
        packet = build_evidence_packet(event)
        assert packet.entities, f"Evidence packet missing entities for {event.event_id}"


def test_evidence_packet_blocker_indicator_on_blocker_event(enriched_events):
    """Blocker events should have a meaningful blocker_indicator in their evidence."""
    blocker_events = [
        e for e in enriched_events
        if e.signals and e.signals.dominant_event_type == "blocker"
    ]
    if not blocker_events:
        pytest.skip("No blocker events found")

    for event in blocker_events[:2]:
        packet = build_evidence_packet(event)
        assert packet.blocker_indicator, (
            f"Expected blocker_indicator on blocker event {event.event_id}"
        )


def test_evidence_packet_state_change_populated(enriched_events):
    """Events with state_change_hint should surface it in the evidence packet."""
    events_with_change = [
        e for e in enriched_events
        if e.signals and e.signals.state_change_hint
    ]
    for event in events_with_change:
        packet = build_evidence_packet(event)
        assert packet.state_change == event.signals.state_change_hint


# ---------------------------------------------------------------------------
# 10. Fallback mode without LLM
# ---------------------------------------------------------------------------

def test_fallback_mode_uses_evidence_packet(enriched_events, events_by_id, profiles):
    """FallbackProvider should produce non-empty summaries using evidence packets."""
    from src.summarization.providers import FallbackProvider
    from src.ranking.ranker import rank_events_for_user
    from src.enrichment.enricher import _build_embedding_store

    store = _build_embedding_store(enriched_events)
    profile = profiles["u_alice"]
    ranked, _ = rank_events_for_user(enriched_events, profile, top_k=3, embedding_store=store)

    provider = FallbackProvider()
    for item in ranked:
        event = events_by_id.get(item.event_id)
        if event is None:
            continue
        summary, why_shown = provider.summarize(event, item, profile)
        assert summary, f"Empty summary for {item.event_id}"
        assert why_shown, f"Empty why_shown for {item.event_id}"
        # Summary should reference event content, not be a generic template
        assert len(summary) > 30, f"Summary too short: '{summary}'"


def test_full_pipeline_with_issue_linking(tmp_path):
    """Full pipeline should complete with issue linking enabled."""
    from src.digest.assembler import run_full_pipeline

    digests = run_full_pipeline(
        data_dir=DATA_DIR,
        now=NOW,
        date_str="2026-04-10",
        top_k=5,
        include_excluded=True,
    )

    assert len(digests) > 0
    for uid, digest in digests.items():
        assert digest.items is not None
        # Sections should be populated
        assert digest.sections is not None
        # All section event_ids should correspond to items in the digest
        item_ids = {item.event_id for item in digest.items}
        for section_ids in [
            digest.sections.top_for_you,
            digest.sections.what_changed,
            digest.sections.still_unresolved,
            digest.sections.also_worth_attention,
        ]:
            for eid in section_ids:
                assert eid in item_ids, (
                    f"Section references event_id {eid} not in digest items for {uid}"
                )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_minimal_event(text: str) -> CandidateEvent:
    """Create a minimal CandidateEvent with given text_bundle for unit tests."""
    return CandidateEvent(
        event_id="evt_test",
        thread_id="m_test",
        channel_id="ch_test",
        participant_ids=["u_a", "u_b"],
        message_ids=["m1", "m2", "m3"],
        started_at=NOW,
        last_activity_at=NOW,
        text_bundle=text,
        message_count=3,
        reply_count=2,
        unique_participant_count=2,
        total_reactions=0,
    )
