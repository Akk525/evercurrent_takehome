"""
Tests verifying correct pipeline stage ordering and signal propagation.

Critical invariants:
1. Drift signals are populated when issue_memory_signals is present (not empty on first run)
2. Graph signals are attached to events before ranking — graph_impact_boost participates in score
3. Ownership signals have access to issue memory context
4. Pipeline stages run in dependency order: enrichment → linking → memory → ownership → drift → graph
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.models import CandidateEvent, SemanticSignals, EventTypeDistribution

DATA_DIR = Path(__file__).parent.parent / "data" / "mock_slack"
NOW = datetime(2026, 4, 10, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event_with_memory(
    event_id: str = "evt_test",
    dominant_type: str = "blocker",
    resurfaced_count: int = 3,
    hours_open: float = 72.0,
    last_event_type: str = "decision",
) -> CandidateEvent:
    """Create a CandidateEvent with issue_memory_signals already attached."""
    from datetime import datetime, timezone
    from src.models.derived import SemanticSignals, EventTypeDistribution

    ev = CandidateEvent(
        event_id=event_id,
        thread_id=event_id,
        channel_id="ch_test",
        participant_ids=["u_alice", "u_bob"],
        message_ids=["msg_1", "msg_2"],
        started_at=datetime(2026, 4, 7, 10, 0, tzinfo=timezone.utc),
        last_activity_at=datetime(2026, 4, 10, 8, 0, tzinfo=timezone.utc),
        text_bundle="BMS firmware blocked due to connector issue",
        message_count=4,
        reply_count=3,
        unique_participant_count=2,
        total_reactions=0,
        signals=SemanticSignals(
            title="BMS firmware blocker",
            topic_labels=["firmware", "bms"],
            event_type_dist=EventTypeDistribution(blocker=0.8),
            dominant_event_type=dominant_type,
            urgency_score=0.75,
            importance_score=0.80,
            momentum_score=0.4,
            novelty_score=0.2,
            unresolved_score=0.9,
            cross_functional_score=0.6,
        ),
    )

    # Attach memory signals (simulates what match_and_update_issues would do)
    mem = MagicMock()
    mem.resurfaced_count = resurfaced_count
    mem.issue_age_hours = hours_open
    mem.last_event_type = last_event_type
    mem._record = None  # Prevent MagicMock auto-creation — drift.py checks this explicitly
    mem.issue_persistence_score = min(1.0, hours_open / 120.0)
    mem.issue_escalation_score = 0.6
    mem.memory_label = f"Ongoing for {hours_open:.0f}h"
    ev.issue_memory_signals = mem

    return ev


# ---------------------------------------------------------------------------
# 1. Drift signals use issue memory when available
# ---------------------------------------------------------------------------

class TestDriftUsesIssueMemory:
    def test_drift_fires_when_memory_present(self):
        """detect_drift should produce non-trivial signals when issue_memory_signals is set."""
        from src.enrichment.drift import detect_drift

        event = _make_event_with_memory(
            resurfaced_count=3,
            hours_open=72.0,
            last_event_type="decision",
        )
        result = detect_drift(event)

        assert result.drift_flag is True
        assert result.resurfacing_count == 3
        assert result.repeated_without_resolution is True
        assert result.long_open_flag is True
        assert result.stale_mitigation_flag is True  # last=decision, current=blocker
        assert result.process_debt_score > 0.5

    def test_drift_empty_when_no_memory(self):
        """detect_drift should return safe defaults when issue_memory_signals is None."""
        from src.enrichment.drift import detect_drift

        event = _make_event_with_memory()
        event.issue_memory_signals = None

        result = detect_drift(event)

        assert result.drift_flag is False
        assert result.resurfacing_count == 0
        assert result.process_debt_score == 0.0

    def test_drift_differs_first_vs_ongoing(self):
        """First-time issue has no drift; repeated issue does."""
        from src.enrichment.drift import detect_drift

        first_run = _make_event_with_memory(resurfaced_count=0, hours_open=4.0)
        first_run.issue_memory_signals.resurfaced_count = 0
        first_run.issue_memory_signals.issue_age_hours = 4.0
        result_first = detect_drift(first_run)

        repeat_run = _make_event_with_memory(resurfaced_count=3, hours_open=72.0)
        result_repeat = detect_drift(repeat_run)

        assert result_repeat.process_debt_score > result_first.process_debt_score
        assert result_repeat.drift_flag is True


# ---------------------------------------------------------------------------
# 2. Graph signals flow into ranking
# ---------------------------------------------------------------------------

class TestGraphSignalsInRanking:
    def test_graph_boost_increases_score(self):
        """An event with graph_signals.graph_impact_boost > 0 scores higher than without."""
        from src.ranking import rank_events_for_user
        from src.models import UserContextProfile
        from src.impact.graph_models import GraphSignals

        event_no_graph = _make_event_with_memory(event_id="evt_no_graph")
        event_no_graph.graph_signals = None

        event_with_graph = _make_event_with_memory(event_id="evt_with_graph")
        event_with_graph.graph_signals = GraphSignals(
            downstream_impact_count=3,
            upstream_dependency_count=0,
            blocks_event_ids=["evt_a", "evt_b", "evt_c"],
            depends_on_event_ids=[],
            graph_centrality_score=0.8,
            graph_impact_boost=0.15,
        )

        profile = UserContextProfile(
            user_id="u_alice",
            active_channel_ids=["ch_test"],
            topic_affinities={"firmware": 0.8, "bms": 0.6},
            event_type_affinities={"blocker": 0.9},
            frequent_collaborators=["u_bob"],
            recent_thread_ids=[event_no_graph.thread_id],
            activity_level=0.7,
        )

        ranked_no_graph, _ = rank_events_for_user(
            [event_no_graph], profile, top_k=1, now=NOW
        )
        ranked_with_graph, _ = rank_events_for_user(
            [event_with_graph], profile, top_k=1, now=NOW
        )

        assert ranked_with_graph, "Should return at least one ranked item"
        assert ranked_no_graph, "Should return at least one ranked item"

        score_no_graph = ranked_no_graph[0].score
        score_with_graph = ranked_with_graph[0].score

        assert score_with_graph >= score_no_graph, (
            f"Graph boost should increase score: {score_with_graph} vs {score_no_graph}"
        )
        assert ranked_with_graph[0].reason_features.graph_impact_boost > 0.0

    def test_graph_boost_capped(self):
        """graph_impact_boost should not push final_score above 1.0."""
        from src.ranking import rank_events_for_user
        from src.models import UserContextProfile
        from src.impact.graph_models import GraphSignals

        event = _make_event_with_memory(event_id="evt_cap_test")
        event.graph_signals = GraphSignals(
            downstream_impact_count=10,
            upstream_dependency_count=0,
            blocks_event_ids=[],
            depends_on_event_ids=[],
            graph_centrality_score=1.0,
            graph_impact_boost=0.15,  # maximum possible
        )

        profile = UserContextProfile(
            user_id="u_alice",
            active_channel_ids=["ch_test"],
            topic_affinities={"firmware": 1.0},
            event_type_affinities={"blocker": 1.0},
            frequent_collaborators=["u_bob"],
            recent_thread_ids=[event.thread_id],
            activity_level=1.0,
        )

        ranked, _ = rank_events_for_user([event], profile, top_k=1, now=NOW)
        if ranked:
            assert ranked[0].score <= 1.0, "Score must be capped at 1.0"

    def test_graph_signals_exposed_in_ranking_features(self):
        """RankingFeatures should expose graph_impact_boost and graph_centrality_score."""
        from src.ranking import rank_events_for_user
        from src.models import UserContextProfile
        from src.impact.graph_models import GraphSignals

        event = _make_event_with_memory(event_id="evt_features_test")
        event.graph_signals = GraphSignals(
            downstream_impact_count=2,
            upstream_dependency_count=1,
            blocks_event_ids=["x", "y"],
            depends_on_event_ids=["z"],
            graph_centrality_score=0.5,
            graph_impact_boost=0.10,
        )

        profile = UserContextProfile(
            user_id="u_alice",
            active_channel_ids=["ch_test"],
            topic_affinities={},
            event_type_affinities={},
            frequent_collaborators=[],
            recent_thread_ids=[],
            activity_level=0.5,
        )

        ranked, _ = rank_events_for_user([event], profile, top_k=1, now=NOW)
        assert ranked, "Should produce a ranked item"
        features = ranked[0].reason_features
        assert features.graph_impact_boost == pytest.approx(0.10, abs=1e-4)
        assert features.graph_centrality_score == pytest.approx(0.5, abs=1e-3)


# ---------------------------------------------------------------------------
# 3. Ownership inference after enrichment produces valid signals
# ---------------------------------------------------------------------------

class TestOwnershipAfterEnrichment:
    def test_ownership_infers_from_slack_data(self, workspace):
        """infer_ownership runs cleanly on real workspace events after enrichment."""
        from src.events import build_candidate_events
        from src.enrichment import enrich_candidate_events
        from src.enrichment.ownership import infer_ownership

        events = build_candidate_events(workspace)
        enriched = enrich_candidate_events(events, workspace, now=NOW)

        for event in enriched[:3]:
            signals = infer_ownership(event, workspace)
            assert signals is not None
            assert 0.0 <= signals.likely_owner_confidence <= 1.0
            assert isinstance(signals.accountability_gap_flag, bool)

    def test_ownership_signal_typed(self, workspace):
        """ownership_signals field should accept OwnershipSignals instances."""
        from src.events import build_candidate_events
        from src.enrichment import enrich_candidate_events
        from src.enrichment.ownership import infer_ownership
        from src.enrichment.ownership_models import OwnershipSignals

        events = build_candidate_events(workspace)
        enriched = enrich_candidate_events(events, workspace, now=NOW)

        for event in enriched[:1]:
            sig = infer_ownership(event, workspace)
            event.ownership_signals = sig  # Should not raise
            assert isinstance(event.ownership_signals, OwnershipSignals)


# ---------------------------------------------------------------------------
# 4. Full pipeline order: signals are chained correctly
# ---------------------------------------------------------------------------

class TestFullPipelineOrder:
    def test_drift_signal_populated_via_pipeline(self, workspace):
        """
        If we run the full pipeline (enrichment → memory → drift), drift signals
        should be non-None on at least some events.
        """
        from src.events import build_candidate_events
        from src.enrichment import enrich_candidate_events
        from src.enrichment.enricher import _build_embedding_store
        from src.issue_linking.linker import build_issue_clusters
        from src.issue_memory.store import IssueMemoryStore
        from src.issue_memory.matcher import match_and_update_issues
        from src.enrichment.drift import detect_drift
        import tempfile
        import os

        events = build_candidate_events(workspace)
        embedding_store = _build_embedding_store(events)
        enriched = enrich_candidate_events(events, workspace, now=NOW, embedding_store=embedding_store)
        build_issue_clusters(enriched, embedding_store)

        # Use a temp DB to avoid polluting real issue memory
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp_db = f.name
        try:
            from src.issue_memory import store as memory_store_module
            original_path = memory_store_module.DB_PATH
            memory_store_module.DB_PATH = Path(tmp_db)
            mem_store = IssueMemoryStore()
            match_and_update_issues(enriched, mem_store, NOW)
        finally:
            memory_store_module.DB_PATH = original_path
            os.unlink(tmp_db)

        # Now run drift detection — should have issue memory signals attached
        for event in enriched:
            event.drift_signals = detect_drift(event)

        # All events with issue_memory_signals should have drift_signals
        events_with_memory = [e for e in enriched if e.issue_memory_signals is not None]
        events_with_drift = [e for e in enriched if e.drift_signals is not None]

        assert len(events_with_memory) > 0, "At least some events should have issue memory"
        assert len(events_with_drift) == len(enriched), "All events should have drift signals after detect_drift"

    def test_graph_signals_attached_before_ranking(self, workspace):
        """
        After build_issue_graph + signal attachment, graph_signals should be
        set on events and graph_impact_boost should appear in ranking features.
        """
        from src.events import build_candidate_events
        from src.enrichment import enrich_candidate_events
        from src.enrichment.enricher import _build_embedding_store
        from src.impact.graph import build_issue_graph
        from src.ranking import rank_events_for_user

        events = build_candidate_events(workspace)
        embedding_store = _build_embedding_store(events)
        enriched = enrich_candidate_events(events, workspace, now=NOW, embedding_store=embedding_store)

        # Build graph and attach signals (same as _run_pipeline does)
        _, graph_signals = build_issue_graph(enriched)
        for event in enriched:
            sig = graph_signals.get(event.event_id)
            if sig is not None:
                event.graph_signals = sig

        # Verify at least some events have graph signals
        events_with_graph = [e for e in enriched if e.graph_signals is not None]
        assert len(events_with_graph) > 0, "At least some events should have graph_signals after pipeline"

        # Verify ranking features expose graph fields
        from src.profiles import build_user_profiles
        profiles = build_user_profiles(workspace, enriched, now=NOW)

        for uid, profile in list(profiles.items())[:1]:
            ranked, _ = rank_events_for_user(enriched, profile, top_k=5, now=NOW)
            for item in ranked:
                # graph_impact_boost and graph_centrality_score should be present in features
                assert hasattr(item.reason_features, "graph_impact_boost")
                assert hasattr(item.reason_features, "graph_centrality_score")
                assert item.reason_features.graph_impact_boost >= 0.0
                assert item.reason_features.graph_centrality_score >= 0.0
